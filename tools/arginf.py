#!/usr/bin/env python3
"""arginf contributor tool (Python 3.11+, standard library only).

    python3 tools/arginf.py hash Theorems/<hash>/Statement.lean
    python3 tools/arginf.py new [--import Mathlib] "<Lean Prop>"
    python3 tools/arginf.py check [--base upstream/main] # same checks as CI
    python3 tools/arginf.py check-all                    # every folder (main runs this)
    python3 tools/arginf.py kernel [<hash> ...]          # kernel re-check after lake build
    python3 tools/arginf.py index > index.json

Each theorem lives in Theorems/<hash>/. The hash is the first 12 hex digits of
SHA-256 over the normalized `def statement : Prop := ...` body, with comments
and whitespace normalized and names excluded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import tomllib

try:  # `python3 tools/arginf.py` puts tools/ on the path; `python3 -m tools.x` the repo root.
    from tools import limits, safety
except ImportError:  # pragma: no cover
    import limits  # type: ignore[no-redef]
    import safety  # type: ignore[no-redef]

ROOT_DIR = "Theorems"
HASH_LEN = 12
HASH_RE = re.compile(rf"^[0-9a-f]{{{HASH_LEN}}}$")
# Theorem folders hold Lean only: no prose, no comments, no string literals. The statement is
# the statement. Curation (featured, examples) lives in CURATION_FILE, owned by maintainers.
ALLOWED_FILES = frozenset({"Statement.lean"})
ATTEMPT_DIR = "attempts"
ATTEMPT_FILES = frozenset({"Attempt.lean", "attempt.toml"})
CURATION_FILE = "curation.toml"
CURATION_KEYS = ("featured", "examples")
_STATEMENT_RE = re.compile(r"(?m)^def\s+statement\s*:\s*Prop\s*:=")
_COMMAND_RE = re.compile(
    r"(?m)^(?:end|theorem|lemma|def|abbrev|namespace|section|open|variable|instance|"
    r"example|structure|class|inductive|noncomputable|private|protected|@\[|#|import|set_option)\b"
)
# Building on another statement: import its Statement, or one of its proofs (an attempt).
_IMPORT_RE = re.compile(
    r"(?m)^import\s+Theorems\.«([0-9a-f]+)»\.(?:Statement|attempts\.«[0-9a-f]+»\.Attempt)\s*$"
)


class ToolError(Exception):
    """A problem the contributor needs to fix."""


def strip_comments(text: str) -> str:
    """Remove Lean `--` and nested `/- -/` comments, leaving string literals intact."""
    try:
        return safety.strip_comments(text)
    except safety.UnterminatedCommentError as err:
        raise ToolError(str(err)) from err


def extract_statement(text: str) -> str:
    """Return the normalized Prop from `def statement : Prop := ...`."""
    code = strip_comments(text)
    matches = list(_STATEMENT_RE.finditer(code))
    if len(matches) != 1:
        raise ToolError("Statement.lean must contain exactly one `def statement : Prop :=`")
    rest = code[matches[0].end() :]
    nxt = _COMMAND_RE.search(rest)
    body = " ".join((rest[: nxt.start()] if nxt else rest).split())
    if not body:
        raise ToolError("the statement is empty")
    return body


def statement_hash(text: str) -> str:
    return hashlib.sha256(extract_statement(text).encode("utf-8")).hexdigest()[:HASH_LEN]


# Scope checks (used by CI on every PR) -------------------------------------


def check_scope(changed: list[str], branch: str) -> list[str]:
    """A PR touches one theorem folder and at most one attempt inside it."""
    errors: list[str] = []
    folders: set[str] = set()
    attempts: set[str] = set()
    for raw in changed:
        p = PurePosixPath(raw)
        parts = p.parts
        if ".." in parts or p.is_absolute():
            errors.append(f"{raw}: path tricks are not allowed")
            continue
        if len(parts) < 3 or parts[0] != ROOT_DIR or not HASH_RE.match(parts[1]):
            errors.append(f"{raw}: outside a {ROOT_DIR}/<hash>/ folder")
            continue
        folders.add(parts[1])
        if len(parts) == 3:
            if parts[2] not in ALLOWED_FILES:
                errors.append(f"{raw}: only {', '.join(sorted(ALLOWED_FILES))} are allowed")
        elif len(parts) == 5 and parts[2] == ATTEMPT_DIR and HASH_RE.match(parts[3]):
            attempts.add(parts[3])
            if parts[4] not in ATTEMPT_FILES:
                errors.append(f"{raw}: only {', '.join(sorted(ATTEMPT_FILES))} are allowed")
        else:
            errors.append(f"{raw}: not an allowed file in {ROOT_DIR}/<hash>/")
    if len(folders) > 1:
        errors.append(f"a PR may change one folder only, found {sorted(folders)}")
    if len(attempts) > 1:
        errors.append(f"a PR may add one attempt only, found {sorted(attempts)}")
    if len(folders) == 1:
        (h,) = folders
        if not branch.startswith(f"{h}/"):
            errors.append(f"branch must start with '{h}/' (got '{branch}')")
    elif not changed:
        errors.append("no changes")
    if not folders and changed and not any("branch" in e for e in errors):
        errors.append("branch must start with '<hash>/' of the folder it changes")
    return errors


# Folders ---------------------------------------------------------------------


def prose_errors(path: Path | str, text: str) -> list[str]:
    """Lean files in theorem folders may not carry natural language."""
    errors: list[str] = []
    try:
        if strip_comments(text) != text:
            errors.append(f"{path}: comments are not allowed (statements are the statements)")
    except ToolError as err:
        errors.append(f"{path}: {err}")
    if '"' in text:
        errors.append(f"{path}: string literals are not allowed")
    return errors


def new_theorem(root: Path, prop: str, imports: tuple[str, ...] = ("Mathlib",)) -> str:
    body = f"def statement : Prop :=\n  {prop.strip()}\n"
    h = statement_hash(body)
    folder = root / ROOT_DIR / h
    if folder.exists():
        raise ToolError(f"{folder} already exists: this statement is already in the repo")
    for imp in imports:
        if not re.fullmatch(r"[A-Za-z_][\w.«»]*", imp):
            raise ToolError(f"bad import {imp!r}")
    text = (
        "".join(f"import {imp}\n" for imp in imports)
        + f"\nnamespace {ROOT_DIR}.«{h}»\n\n{body}\nend {ROOT_DIR}.«{h}»\n"
    )
    errors = prose_errors("Statement.lean", text)
    if errors:
        raise ToolError("; ".join(errors))
    folder.mkdir(parents=True)
    (folder / "Statement.lean").write_text(text, encoding="utf-8")
    return h


def check_folder(folder: Path) -> list[str]:
    errors: list[str] = []
    stmt = folder / "Statement.lean"
    if not stmt.is_file():
        return [f"{folder}: missing Statement.lean"]
    try:
        h = statement_hash(stmt.read_text(encoding="utf-8"))
        if h != folder.name:
            errors.append(f"{folder}: folder name does not match statement hash {h}")
    except ToolError as err:
        errors.append(f"{stmt}: {err}")
    errors += safety.statement_errors(stmt, stmt.read_text(encoding="utf-8"), folder.name)
    for f in folder.iterdir():
        if f.name == ATTEMPT_DIR and f.is_dir():
            continue
        if f.name not in ALLOWED_FILES:
            errors.append(f"{f}: only {', '.join(sorted(ALLOWED_FILES))} are allowed")
        elif f.suffix == ".lean":
            errors += prose_errors(f, f.read_text(encoding="utf-8"))
    return errors


def load_curation(root: Path) -> tuple[dict[str, list[str]], list[str]]:
    """Maintainer-owned lists of hashes: which statements are featured or worked examples."""
    path = root / CURATION_FILE
    empty: dict[str, list[str]] = {k: [] for k in CURATION_KEYS}
    if not path.is_file():
        return empty, []
    try:
        raw: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as err:
        return empty, [f"{CURATION_FILE}: {err}"]
    errors: list[str] = []
    out = dict(empty)
    for key, value in raw.items():
        if key not in CURATION_KEYS:
            errors.append(f"{CURATION_FILE}: unknown key {key!r}")
        elif not isinstance(value, list) or not all(
            isinstance(v, str) and HASH_RE.match(v) for v in value
        ):
            errors.append(f"{CURATION_FILE}: {key} must be a list of statement hashes")
        else:
            missing = [v for v in value if not (root / ROOT_DIR / v).is_dir()]
            if missing:
                errors.append(f"{CURATION_FILE}: {key} lists unknown statements {missing}")
            out[key] = list(value)
    return out, errors


# Attempts ("lines"): content-addressed, possibly partial, forming a tree -------

_NS_LINE_RE = re.compile(r"(?m)^\s*(?:namespace|end)\b.*$")
_SORRY_RE = re.compile(r"\bsorry\b")


def _attempt_body(text: str) -> str:
    """Attempt.lean without comments and namespace/end boilerplate, whitespace-normalized."""
    return " ".join(_NS_LINE_RE.sub("", strip_comments(text)).split())


def attempt_hash(text: str, parent: str | None) -> str:
    material = f"parent:{parent or ''}\n{_attempt_body(text)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:HASH_LEN]


def _attempt_ns(h: str, a: str) -> str:
    return f"{ROOT_DIR}.«{h}».Attempt_{a}"


def _read_attempt_meta(folder: Path) -> tuple[str | None, list[str]]:
    try:
        raw: dict[str, Any] = tomllib.loads((folder / "attempt.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as err:
        return None, [f"{folder}/attempt.toml: {err}"]
    errors: list[str] = []
    parent = raw.get("parent", "")
    if not isinstance(parent, str) or (parent and not HASH_RE.match(parent)):
        errors.append(f"{folder}/attempt.toml: parent must be empty or a hash")
        parent = ""
    if set(raw) - {"parent"}:
        errors.append(f"{folder}/attempt.toml: only `parent` is allowed")
    return (parent or None), errors


def _write_attempt(folder: Path, h: str, a: str, code: str, parent: str | None) -> None:
    ns = _attempt_ns(h, a)
    folder.mkdir(parents=True)
    (folder / "Attempt.lean").write_text(
        f"import {ROOT_DIR}.«{h}».Statement\n\nnamespace {ns}\n\n{code.strip()}\n\nend {ns}\n",
        encoding="utf-8",
    )
    (folder / "attempt.toml").write_text(f'parent = "{parent or ""}"\n', encoding="utf-8")


def _attempt_code(text: str) -> str:
    """The code between the namespace lines (what a continuation copies)."""
    lines = [ln for ln in text.splitlines() if not _NS_LINE_RE.match(ln)]
    return "\n".join(ln for ln in lines if not ln.startswith("import ")).strip()


def start_attempt(root: Path, h: str, parent: str | None) -> str:
    base = root / ROOT_DIR / h
    if not (base / "Statement.lean").is_file():
        raise ToolError(f"no statement {h}")
    if parent:
        ptext = (base / ATTEMPT_DIR / parent / "Attempt.lean").read_text(encoding="utf-8")
        imports = [ln for ln in ptext.splitlines() if ln.startswith("import ")]
        code = "\n".join(imports[1:] + ["", _attempt_code(ptext)]).strip()
    else:
        code = f"theorem proof : {ROOT_DIR}.«{h}».statement := by\n  sorry"
    a = attempt_hash(code, parent)
    if (base / ATTEMPT_DIR / a).exists():
        # Identical starting point already exists: work in a draft until it is sealed.
        a = f"draft_{secrets.token_hex(4)}"
    _write_attempt(base / ATTEMPT_DIR / a, h, a, code, parent)
    return a


def seal_attempt(folder: Path) -> str:
    """Re-hash an attempt after editing it, renaming its folder and namespace."""
    h = folder.parent.parent.name
    parent, errors = _read_attempt_meta(folder)
    if errors:
        raise ToolError("; ".join(errors))
    text = (folder / "Attempt.lean").read_text(encoding="utf-8")
    extra_imports = [
        ln for ln in text.splitlines() if ln.startswith("import ") and "Statement" not in ln
    ]
    code = "\n".join(extra_imports + ["", _attempt_code(text)]).strip()
    a = attempt_hash(code, parent)
    if a == folder.name:
        return a
    target = folder.parent / a
    if target.exists():
        raise ToolError(f"attempt {a} already exists")
    _write_attempt(target, h, a, code, parent)
    for f in folder.iterdir():
        f.unlink()
    folder.rmdir()
    return a


def check_attempt(folder: Path) -> list[str]:
    errors: list[str] = []
    parent, meta_errors = _read_attempt_meta(folder)
    errors += meta_errors
    f = folder / "Attempt.lean"
    if not f.is_file():
        return errors + [f"{folder}: missing Attempt.lean"]
    text = f.read_text(encoding="utf-8")
    errors += prose_errors(f, text)
    errors += safety.attempt_errors(f, text)
    extra = [ln for ln in text.splitlines() if ln.startswith("import ") and "Statement" not in ln]
    code = "\n".join(extra + ["", _attempt_code(text)]).strip()
    if attempt_hash(code, parent) != folder.name:
        errors.append(f"{folder}: folder name does not match attempt hash (run `arginf.py seal`)")
    if parent and not (folder.parent / parent).is_dir():
        errors.append(f"{folder}: parent attempt {parent} not found")
    for g in folder.iterdir():
        if g.name not in ATTEMPT_FILES:
            errors.append(f"{g}: only {', '.join(sorted(ATTEMPT_FILES))} are allowed")
    errors += _progress_errors(folder, code, parent)
    return errors


def _progress_errors(folder: Path, code: str, parent: str | None) -> list[str]:
    """A checkpoint must make progress: not the bare template, not an unchanged copy."""
    h = folder.parent.parent.name
    body = _attempt_body(code)
    template = _attempt_body(f"theorem proof : {ROOT_DIR}.«{h}».statement := by\n  sorry")
    if body == template:
        return [f"{folder}: no progress: this is the empty template (`by sorry`)"]
    if parent:
        pfile = folder.parent / parent / "Attempt.lean"
        if pfile.is_file():
            ptext = pfile.read_text(encoding="utf-8")
            extra = [
                ln
                for ln in ptext.splitlines()
                if ln.startswith("import ") and "Statement" not in ln
            ]
            pcode = "\n".join(extra + ["", _attempt_code(ptext)]).strip()
            if _attempt_body(pcode) == body:
                return [f"{folder}: no progress: identical to its parent {parent}"]
    return []


def attempt_info(folder: Path) -> dict[str, Any]:
    parent, _ = _read_attempt_meta(folder)
    code = strip_comments((folder / "Attempt.lean").read_text(encoding="utf-8"))
    goals = len(_SORRY_RE.findall(code))
    return {
        "hash": folder.name,
        "parent": parent,
        "openGoals": goals,
        "complete": goals == 0,
        # Proof length: non-whitespace characters of the code, so formatting doesn't count.
        "size": len("".join(_attempt_body(code).split())),
        "children": [],
        "lean": (folder / "Attempt.lean").read_text(encoding="utf-8"),
    }


# Axiom audit (CI runs this after `lake build`) ---------------------------------

ALLOWED_AXIOMS = frozenset({"propext", "Classical.choice", "Quot.sound"})
_AXIOMS_RE = re.compile(r"depends on axioms: \[(.*?)\]", re.DOTALL)


def audit_source(h: str, attempt: str) -> str:
    """A Lean file that fails unless the attempt proves exactly `statement`, then lists axioms."""
    ns = f"{ROOT_DIR}.«{h}»"
    module, proof = f"{ns}.{ATTEMPT_DIR}.«{attempt}».Attempt", f"{_attempt_ns(h, attempt)}.proof"
    return f"import {module}\nexample : {ns}.statement := {proof}\n#print axioms {proof}\n"


def complete_attempts(root: Path, h: str) -> list[str]:
    adir = root / ROOT_DIR / h / ATTEMPT_DIR
    if not adir.is_dir():
        return []
    return [
        a.name
        for a in sorted(adir.iterdir())
        if HASH_RE.match(a.name) and (a / "Attempt.lean").is_file() and attempt_info(a)["complete"]
    ]


def parse_axioms(output: str) -> list[str]:
    if "does not depend on any axioms" in output:
        return []
    m = _AXIOMS_RE.search(output)
    if not m:
        raise ToolError(f"could not read axioms from Lean output: {output.strip()[:300]}")
    return [a.strip() for a in m.group(1).split(",") if a.strip()]


def forbidden_axioms(axioms: list[str]) -> list[str]:
    return [a for a in axioms if a not in ALLOWED_AXIOMS]


def theorem_modules(root: Path, hashes: list[str] | None = None) -> list[str]:
    """Statement and attempt modules, statements first, for these folders (default: all)."""
    base = root / ROOT_DIR
    folders = (
        sorted(p for p in base.iterdir() if p.is_dir() and HASH_RE.match(p.name))
        if base.is_dir()
        else []
    )
    if hashes is not None:
        folders = [f for f in folders if f.name in set(hashes)]
    mods: list[str] = []
    for f in folders:
        if (f / "Statement.lean").is_file():
            mods.append(f"{ROOT_DIR}.«{f.name}».Statement")
        adir = f / ATTEMPT_DIR
        if adir.is_dir():
            mods += [
                f"{ROOT_DIR}.«{f.name}».{ATTEMPT_DIR}.«{a.name}».Attempt"
                for a in sorted(adir.iterdir())
                if HASH_RE.match(a.name) and (a / "Attempt.lean").is_file()
            ]
    return mods


def kernel_check(root: Path, modules: list[str], env: dict[str, str] | None = None) -> list[str]:
    """Replay every declaration of each module in the Lean kernel, one process per module.

    `leanchecker` ships with the toolchain. It loads .olean files without running their
    initializers, so it catches declarations that reached the environment without passing the
    kernel. Run it after building and before trusting `#print axioms`. Each run imports the
    module's dependencies (Mathlib) afresh, so modules are checked one at a time to bound
    memory."""
    if not modules:
        return ["kernel re-check: no modules to check"]
    errors: list[str] = []
    for module in modules:
        cmd = ["leanchecker", module]
        if env is None:
            cmd = ["lake", "env", *cmd]
        run = subprocess.run(
            cmd, cwd=root, env=env, capture_output=True, text=True, timeout=3600, check=False
        )
        if run.returncode != 0:
            out = (run.stdout + run.stderr).strip()[:2000] or "(no output)"
            errors.append(f"kernel re-check failed for {module} (exit {run.returncode}):\n{out}")
    return errors


def audit(root: Path, h: str, attempt: str) -> list[str]:
    if not HASH_RE.match(h) or not HASH_RE.match(attempt):
        return [f"bad hash {h!r}/{attempt!r}"]
    src = root / ".lake" / f"Audit_{h}_{attempt}.lean"
    src.parent.mkdir(exist_ok=True)
    src.write_text(audit_source(h, attempt), encoding="utf-8")
    run = subprocess.run(
        ["lake", "env", "lean", str(src)], cwd=root, capture_output=True, text=True, timeout=1800
    )
    output = run.stdout + run.stderr
    if run.returncode != 0:
        return [f"{h}: Lean rejected the proof:\n{output.strip()[:2000]}"]
    bad = forbidden_axioms(parse_axioms(output))
    return [f"{h}: forbidden axioms {bad}"] if bad else []


# Fast path: folders that only need Lean core compile in seconds with plain `lean` --

CORE_ROOTS = frozenset({"Init", "Std", "Lean"})
_ANY_IMPORT_RE = re.compile(r"(?m)^import\s+(\S+)\s*$")


def _module_file(root: Path, module: str) -> Path:
    parts = [p.strip("«»") for p in re.findall(r"«[^»]*»|[^.]+", module)]
    return root.joinpath(*parts).with_suffix(".lean")


def fast_plan(root: Path, h: str) -> list[tuple[str, Path]] | None:
    """Modules to compile, dependencies first, or None if anything needs Mathlib/CSLib."""
    targets = [f"{ROOT_DIR}.«{h}».Statement"]
    adir = root / ROOT_DIR / h / ATTEMPT_DIR
    if adir.is_dir():
        targets += [
            f"{ROOT_DIR}.«{h}».{ATTEMPT_DIR}.«{a.name}».Attempt"
            for a in sorted(adir.iterdir())
            if (a / "Attempt.lean").is_file()
        ]
    order: list[tuple[str, Path]] = []
    state: dict[str, int] = {}

    def visit(module: str) -> bool:
        if state.get(module) == 2:
            return True
        if state.get(module) == 1:
            raise ToolError(f"import cycle at {module}")
        root_name = module.split(".", 1)[0]
        if root_name in CORE_ROOTS:
            return True
        if root_name != ROOT_DIR:
            return False
        path = _module_file(root, module)
        if not path.is_file():
            raise ToolError(f"missing module {module}")
        state[module] = 1
        for dep in _ANY_IMPORT_RE.findall(strip_comments(path.read_text(encoding="utf-8"))):
            if not visit(dep):
                return False
        state[module] = 2
        order.append((module, path))
        return True

    for t in targets:
        if not visit(t):
            return None
    return order


def fast_check(root: Path, h: str, attempt: str | None = None) -> list[str] | None:
    """Compile and audit with core Lean only. None means the fast path does not apply."""
    plan = fast_plan(root, h)
    if plan is None:
        return None
    out = root / ".lake" / "fast"
    out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "LEAN_PATH": str(out)}
    for module, path in plan:
        olean = _module_file(out, module).with_suffix(".olean")
        olean.parent.mkdir(parents=True, exist_ok=True)
        run = subprocess.run(
            ["lean", "-R", str(root), "-o", str(olean), str(path)],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if run.returncode != 0:
            return [f"{module}: {(run.stdout + run.stderr).strip()[:2000]}"]
    kernel = kernel_check(root, [m for m, _ in plan if m.startswith(f"{ROOT_DIR}.«{h}».")], env)
    if kernel:
        return kernel
    # Every complete attempt in the folder is a proof, so all of them are audited.
    targets = sorted(set(complete_attempts(root, h)) | ({attempt} if attempt else set()))
    errors: list[str] = []
    for a in targets:
        src = out / f"Audit_{h}_{a}.lean"
        src.write_text(audit_source(h, a), encoding="utf-8")
        run = subprocess.run(
            ["lean", str(src)], cwd=root, env=env, capture_output=True, text=True, timeout=600
        )
        output = run.stdout + run.stderr
        if run.returncode != 0:
            errors.append(f"{h}: Lean rejected the proof:\n{output.strip()[:2000]}")
            continue
        bad = forbidden_axioms(parse_axioms(output))
        if bad:
            errors.append(f"{h}: forbidden axioms {bad}")
    return errors


# Index (rendered by arginf.com) ------------------------------------------------


def _attempt_tree(folder: Path) -> list[dict[str, Any]]:
    adir = folder / ATTEMPT_DIR
    if not adir.is_dir():
        return []
    nodes = {
        p.name: attempt_info(p)
        for p in sorted(adir.iterdir())
        if p.is_dir() and HASH_RE.match(p.name)
    }
    for n in nodes.values():
        if n["parent"] in nodes:
            nodes[n["parent"]]["children"].append(n["hash"])
    return list(nodes.values())


def build_index(root: Path) -> dict[str, Any]:
    curation, _ = load_curation(root)
    entries: dict[str, dict[str, Any]] = {}
    base = root / ROOT_DIR
    for folder in sorted(p for p in base.iterdir() if p.is_dir() and HASH_RE.match(p.name)):
        errors = check_folder(folder)
        stmt_text = (folder / "Statement.lean").read_text(encoding="utf-8")
        tree = _attempt_tree(folder)
        proofs = [a["hash"] for a in tree if a["complete"]]
        dep_text = stmt_text + "".join("\n" + a["lean"] for a in tree if a["complete"])
        deps = sorted({d for d in _IMPORT_RE.findall(dep_text) if d != folder.name})
        entries[folder.name] = {
            "hash": folder.name,
            "statement": extract_statement(stmt_text),
            "statementLean": stmt_text,
            "status": "proved" if proofs else "open",
            "proofs": proofs,
            "dependencies": deps,
            "dependents": [],
            "valid": not errors,
            "featured": folder.name in curation["featured"],
            "example": folder.name in curation["examples"],
            "attempts": tree,
        }
    for h, e in entries.items():
        for d in e["dependencies"]:
            if d in entries:
                entries[d]["dependents"].append(h)
    for e in entries.values():
        e["dependents"].sort()
    return {"version": 2, "theorems": list(entries.values())}


def _limits_errors(root: Path) -> list[str]:
    path = root / limits.LIMITS_FILE
    return limits.parse(path.read_text(encoding="utf-8"))[1] if path.is_file() else []


def check_all(root: Path) -> list[str]:
    """Every folder and attempt, as CI checks a PR. main runs this before building."""
    errors: list[str] = _limits_errors(root)
    base = root / ROOT_DIR
    if not base.is_dir():
        return errors
    for folder in sorted(base.iterdir()):
        if not (folder.is_dir() and HASH_RE.match(folder.name)):
            errors.append(f"{folder}: not a {ROOT_DIR}/<hash>/ folder")
            continue
        errors += check_folder(folder)
        adir = folder / ATTEMPT_DIR
        if adir.is_dir():
            for att in sorted(adir.iterdir()):
                if att.is_dir() and HASH_RE.match(att.name):
                    for e in check_attempt(att):
                        if "no progress" not in e:  # judged when the attempt was merged
                            errors.append(e)
                else:
                    errors.append(f"{att}: not an attempt folder")
    return errors


# CLI ---------------------------------------------------------------------------


def default_base(remotes: list[str]) -> str:
    """After `gh repo fork --clone`, origin is your fork and upstream is arginf/proofs."""
    return "upstream/main" if "upstream" in remotes else "origin/main"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arginf")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_hash = sub.add_parser("hash", help="print the hash of a Statement.lean")
    p_hash.add_argument("file", type=Path)
    p_new = sub.add_parser("new", help="create Theorems/<hash>/ for a new statement")
    p_new.add_argument("prop", help="the Lean Prop, e.g. '∀ n : Nat, n + 0 = n'")
    p_new.add_argument("--import", dest="imports", action="append", default=None)
    p_check = sub.add_parser("check", help="run the PR scope and folder checks locally")
    p_check.add_argument("--base", default=None, help="default: upstream/main if present")
    p_check.add_argument("--branch", default=None)
    sub.add_parser("index", help="print index.json for the site")
    p_att = sub.add_parser("attempt", help="start a new attempt, optionally continuing another")
    p_att.add_argument("theorem")
    p_att.add_argument("--from", dest="parent", default=None, help="attempt hash to continue")
    p_seal = sub.add_parser("seal", help="re-hash an edited attempt folder")
    p_seal.add_argument("folder", type=Path)
    p_fast = sub.add_parser("fast", help="compile + audit a core-only folder without Mathlib")
    p_fast.add_argument("theorem")
    p_fast.add_argument("--attempt", default=None)
    p_kernel = sub.add_parser("kernel", help="replay modules in the kernel (after `lake build`)")
    p_kernel.add_argument("hashes", nargs="*", help="statements to check (default: all)")
    sub.add_parser("check-all", help="folder, attempt and safety checks for every folder")
    p_audit = sub.add_parser("audit", help="check proofs and axioms (needs `lake build` first)")
    p_audit.add_argument("hashes", nargs="*", help="statements to audit (default: all)")
    args = parser.parse_args(argv)
    root = Path.cwd()
    try:
        if args.cmd == "hash":
            print(statement_hash(args.file.read_text(encoding="utf-8")))
        elif args.cmd == "new":
            h = new_theorem(root, args.prop, tuple(args.imports or ["Mathlib"]))
            print(h)
            print(f"next: git switch -c {h}/statement && git add {ROOT_DIR}/{h}", file=sys.stderr)
        elif args.cmd == "check":
            branch = args.branch or _git("rev-parse", "--abbrev-ref", "HEAD").strip()
            base = args.base or default_base(_git("remote").split())
            print(f"checking against {base}", file=sys.stderr)
            changed = _git("diff", "--name-only", f"{base}...HEAD").split()
            errors = check_scope(changed, branch)
            for c in changed:
                parts = PurePosixPath(c).parts
                if len(parts) >= 3 and parts[0] == ROOT_DIR and HASH_RE.match(parts[1]):
                    if (root / ROOT_DIR / parts[1]).is_dir():
                        errors += check_folder(root / ROOT_DIR / parts[1])
                    if len(parts) == 5 and parts[2] == ATTEMPT_DIR:
                        att = root / ROOT_DIR / parts[1] / ATTEMPT_DIR / parts[3]
                        if att.is_dir():
                            errors += check_attempt(att)
            if any(c == CURATION_FILE for c in changed):
                errors += load_curation(root)[1]
            if any(c == limits.LIMITS_FILE for c in changed):
                errors += _limits_errors(root)
            errors = list(dict.fromkeys(errors))
            for e in errors:
                print(f"error: {e}", file=sys.stderr)
            if errors:
                return 1
            print("ok")
        elif args.cmd == "attempt":
            a = start_attempt(root, args.theorem, args.parent)
            print(a)
            print(
                f"next: edit {ROOT_DIR}/{args.theorem}/{ATTEMPT_DIR}/{a}/Attempt.lean, then "
                f"`arginf.py seal` it",
                file=sys.stderr,
            )
        elif args.cmd == "seal":
            print(seal_attempt(args.folder))
        elif args.cmd == "fast":
            result = fast_check(root, args.theorem, args.attempt)
            if result is None:
                print("needs-full-build")
                return 3
            for e in result:
                print(f"error: {e}", file=sys.stderr)
            if result:
                return 1
            print("ok: compiled and audited with Lean core only")
        elif args.cmd == "audit":
            hashes = args.hashes or sorted(
                p.name for p in (root / ROOT_DIR).iterdir() if HASH_RE.match(p.name)
            )
            pairs = [(h, a) for h in hashes for a in complete_attempts(root, h)]
            errors = [e for h, a in pairs for e in audit(root, h, a)]
            for e in errors:
                print(f"error: {e}", file=sys.stderr)
            if errors:
                return 1
            print(f"ok: {len(pairs)} proof(s) use only {sorted(ALLOWED_AXIOMS)}")
        elif args.cmd == "kernel":
            errors = kernel_check(root, theorem_modules(root, args.hashes or None))
            for e in errors:
                print(f"error: {e}", file=sys.stderr)
            if errors:
                return 1
            print("ok: kernel re-check passed")
        elif args.cmd == "check-all":
            errors = check_all(root)
            for e in errors:
                print(f"error: {e}", file=sys.stderr)
            if errors:
                return 1
            print("ok")
        elif args.cmd == "index":
            json.dump(build_index(root), sys.stdout, ensure_ascii=False, indent=1)
            print()
    except ToolError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
