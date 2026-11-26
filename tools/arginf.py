#!/usr/bin/env python3
"""arginf contributor tool (Python 3.11+, standard library only).

    python3 tools/arginf.py hash Theorems/<hash>/Statement.lean
    python3 tools/arginf.py new [--import Mathlib] "<Lean Prop>"
    python3 tools/arginf.py check [--base origin/main]   # same checks as CI
    python3 tools/arginf.py index > index.json

Each theorem lives in Theorems/<hash>/. The hash is the first 12 hex digits of
SHA-256 over the normalized `def statement : Prop := ...` body, with comments
and whitespace normalized and names excluded.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import secrets
import subprocess
import sys
import tomllib
import urllib.request
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

ROOT_DIR = "Theorems"
HASH_LEN = 12
HASH_RE = re.compile(rf"^[0-9a-f]{{{HASH_LEN}}}$")
# Theorem folders hold Lean only: no prose, no comments, no string literals. The statement is
# the statement. Curation (featured, examples) lives in CURATION_FILE, owned by maintainers.
ALLOWED_FILES = frozenset({"Statement.lean", "Proof.lean"})
ATTEMPT_DIR = "attempts"
ATTEMPT_FILES = frozenset({"Attempt.lean", "attempt.toml"})
CURATION_FILE = "curation.toml"
# English readings of statements, each labeled with the model that wrote it and a public chat
# showing the full context. Prose, so they live outside Theorems/ and need maintainer review.
TRANSLATION_DIR = "translations"
SHARE_PREFIXES = {
    "claude.ai": "/share/",
    "chatgpt.com": "/share/",
    "chat.openai.com": "/share/",
    "gemini.google.com": "/share/",
    "g.co": "/gemini/share/",
    "grok.com": "/share/",
}
ARCHIVE_RE = re.compile(r"https://web\.archive\.org/web/(\d{14})/(https://\S+)")
READING_KEYS = frozenset({"model", "archive", "text"})
READINGS_MAX = 10
READING_TEXT_MAX = 5000
PROMPT_TEMPLATE = "Translate the following Lean statement into natural language:\n```\n{lean}\n```"
FETCH_MAX_BYTES = 20_000_000
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:/()+-]{0,79}")
CURATION_KEYS = ("featured", "examples")
_STATEMENT_RE = re.compile(r"(?m)^def\s+statement\s*:\s*Prop\s*:=")
_COMMAND_RE = re.compile(
    r"(?m)^(?:end|theorem|lemma|def|abbrev|namespace|section|open|variable|instance|"
    r"example|structure|class|inductive|noncomputable|private|protected|@\[|#|import|set_option)\b"
)
_IMPORT_RE = re.compile(r"(?m)^import\s+Theorems\.«([0-9a-f]+)»\.(?:Proof|Statement)\s*$")
_ATTEMPT_IMPORT_RE = re.compile(
    r"(?m)^import\s+Theorems\.«([0-9a-f]+)»\.attempts\.«([0-9a-f]+)»\.Attempt\s*$"
)


class ToolError(Exception):
    """A problem the contributor needs to fix."""


def strip_comments(text: str) -> str:
    """Remove Lean `--` and nested `/- -/` comments, leaving string literals intact."""
    out: list[str] = []
    i, n, depth = 0, len(text), 0
    while i < n:
        two = text[i : i + 2]
        if depth:
            if two == "/-":
                depth, i = depth + 1, i + 2
            elif two == "-/":
                depth, i = depth - 1, i + 2
                if depth == 0:
                    out.append(" ")
            else:
                i += 1
            continue
        if two == "/-":
            depth, i = 1, i + 2
        elif two == "--":
            while i < n and text[i] != "\n":
                i += 1
        elif text[i] == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        else:
            out.append(text[i])
            i += 1
    if depth:
        raise ToolError("unterminated block comment")
    return "".join(out)


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
    translations: set[str] = set()
    for raw in changed:
        p = PurePosixPath(raw)
        parts = p.parts
        if ".." in parts or p.is_absolute():
            errors.append(f"{raw}: path tricks are not allowed")
            continue
        if len(parts) == 2 and parts[0] == TRANSLATION_DIR and parts[1].endswith(".toml"):
            th = parts[1][: -len(".toml")]
            if not HASH_RE.match(th):
                errors.append(f"{raw}: translation files are named <hash>.toml")
            else:
                translations.add(th)
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
    if translations and folders:
        errors.append("translations go in their own PR, separate from Lean changes")
    if len(translations) > 1:
        errors.append(f"a PR may add one translation file only, found {sorted(translations)}")
    if len(translations) == 1 and not folders:
        (th,) = translations
        if not branch.startswith(f"{th}/"):
            errors.append(f"branch must start with '{th}/' (got '{branch}')")
        return errors
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


def _share_link_ok(url: str) -> bool:
    u = urlsplit(url)
    prefix = SHARE_PREFIXES.get(u.hostname or "")
    return (
        u.scheme == "https"
        and prefix is not None
        and u.path.startswith(prefix)
        and len(u.path) > len(prefix)
        and not u.username
        and not u.port
    )


def _archive_parts(url: str) -> tuple[str, str] | None:
    """(timestamp, share link) for a Wayback snapshot of an allowlisted share link."""
    m = ARCHIVE_RE.fullmatch(url)
    if not m or not _share_link_ok(m.group(2)):
        return None
    return m.group(1), m.group(2)


def _reading_errors(where: str, r: object) -> list[str]:
    if not isinstance(r, dict):
        return [f"{where}: each [[reading]] must be a table"]
    errors: list[str] = []
    unknown = set(r) - READING_KEYS
    if unknown:
        errors.append(f"{where}: unknown keys {sorted(unknown)} (allowed: model, archive, text)")
    model, archive, text = r.get("model"), r.get("archive"), r.get("text")
    if not isinstance(model, str) or not _MODEL_RE.fullmatch(model):
        errors.append(f"{where}: model is required (the model that wrote the reading)")
    if not isinstance(archive, str) or _archive_parts(archive) is None:
        errors.append(
            f"{where}: archive must be https://web.archive.org/web/<14-digit timestamp>/"
            f"<public share link on {', '.join(SHARE_PREFIXES)}>"
        )
    if not isinstance(text, str) or not text.strip() or len(text) > READING_TEXT_MAX:
        errors.append(f"{where}: text must be the model's exact output, 1-{READING_TEXT_MAX} chars")
    return errors


def translation_prompt(root: Path, h: str) -> str:
    """The exact prompt a reading's chat must contain: the whole Statement.lean, verbatim."""
    lean = (root / ROOT_DIR / h / "Statement.lean").read_text(encoding="utf-8").strip()
    return PROMPT_TEMPLATE.format(lean=lean)


def _page_text(page: str) -> str:
    """Archived chat pages embed messages as (possibly nested) escaped JSON inside HTML."""
    text = html.unescape(page)
    for _ in range(2):
        text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)
        text = text.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "")
        text = text.replace('\\"', '"').replace("\\/", "/").replace("\\\\", "\\")
    return " ".join(text.split())


def _fetch(url: str) -> str:
    if not url.startswith("https://web.archive.org/web/"):
        raise OSError("refusing to fetch outside web.archive.org")
    req = urllib.request.Request(  # noqa: S310 - https://web.archive.org only, checked above
        url, headers={"User-Agent": "arginf-ci (+https://arginf.com)"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - fixed https host above
        data: bytes = resp.read(FETCH_MAX_BYTES + 1)
    if len(data) > FETCH_MAX_BYTES:
        raise OSError("archived page too large")
    return data.decode("utf-8", errors="replace")


def verify_translation(root: Path, h: str, fetch: Callable[[str], str] | None = None) -> list[str]:
    """Check each reading's archived chat: the exact prompt, then the exact output after it."""
    errors = check_translation(root, h)
    if errors:
        return errors
    get = fetch or _fetch
    raw: dict[str, Any] = tomllib.loads(
        (root / TRANSLATION_DIR / f"{h}.toml").read_text(encoding="utf-8")
    )
    prompt = " ".join(translation_prompt(root, h).split())
    for i, r in enumerate(raw["reading"]):
        where = f"{TRANSLATION_DIR}/{h}.toml reading {i + 1}"
        parts = _archive_parts(r["archive"])
        assert parts is not None
        ts, share = parts
        url = f"https://web.archive.org/web/{ts}id_/{share}"
        try:
            page = _page_text(get(url))
        except (OSError, ValueError) as err:
            errors.append(f"{where}: could not fetch {url}: {err}")
            continue
        at = page.find(prompt)
        if at < 0:
            errors.append(f"{where}: archived chat does not contain the exact prompt")
            continue
        if page.find(" ".join(str(r["text"]).split()), at + len(prompt)) < 0:
            errors.append(f"{where}: text does not match the model's output after the prompt")
    return errors


def check_translation(root: Path, h: str) -> list[str]:
    path = root / TRANSLATION_DIR / f"{h}.toml"
    if not HASH_RE.match(h):
        return [f"{path}: bad hash"]
    if not (root / ROOT_DIR / h / "Statement.lean").is_file():
        return [f"{path}: no statement {h}"]
    try:
        raw: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as err:
        return [f"{path}: {err}"]
    errors: list[str] = []
    if set(raw) - {"reading"}:
        errors.append(f"{path}: only [[reading]] tables are allowed")
    readings = raw.get("reading")
    if not isinstance(readings, list) or not 1 <= len(readings) <= READINGS_MAX:
        return errors + [f"{path}: needs 1-{READINGS_MAX} [[reading]] tables"]
    for i, r in enumerate(readings):
        errors += _reading_errors(f"{path} reading {i + 1}", r)
    return errors


def load_readings(root: Path, h: str) -> list[dict[str, Any]]:
    path = root / TRANSLATION_DIR / f"{h}.toml"
    if not path.is_file() or check_translation(root, h):
        return []
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return [
        {"model": r["model"], "archive": r["archive"], "text": r["text"]} for r in raw["reading"]
    ]


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
    extra = [ln for ln in text.splitlines() if ln.startswith("import ") and "Statement" not in ln]
    code = "\n".join(extra + ["", _attempt_code(text)]).strip()
    if attempt_hash(code, parent) != folder.name:
        errors.append(f"{folder}: folder name does not match attempt hash (run `arginf.py seal`)")
    if parent and not (folder.parent / parent).is_dir():
        errors.append(f"{folder}: parent attempt {parent} not found")
    for g in folder.iterdir():
        if g.name not in ATTEMPT_FILES:
            errors.append(f"{g}: only {', '.join(sorted(ATTEMPT_FILES))} are allowed")
    return errors


def attempt_info(folder: Path) -> dict[str, Any]:
    parent, _ = _read_attempt_meta(folder)
    code = strip_comments((folder / "Attempt.lean").read_text(encoding="utf-8"))
    goals = len(_SORRY_RE.findall(code))
    return {
        "hash": folder.name,
        "parent": parent,
        "openGoals": goals,
        "complete": goals == 0,
        "children": [],
    }


def promote(root: Path, h: str, a: str) -> None:
    """Make a complete attempt the theorem's Proof.lean."""
    folder = root / ROOT_DIR / h / ATTEMPT_DIR / a
    errors = check_attempt(folder)
    if errors:
        raise ToolError("; ".join(errors))
    if not attempt_info(folder)["complete"]:
        raise ToolError(f"attempt {a} still has open goals (sorry)")
    proof = root / ROOT_DIR / h / "Proof.lean"
    if proof.exists():
        raise ToolError(f"{proof} already exists: this theorem is already proved")
    proof.write_text(
        f"import {ROOT_DIR}.«{h}».{ATTEMPT_DIR}.«{a}».Attempt\n\n"
        f"namespace {ROOT_DIR}.«{h}»\n\n"
        f"theorem proof : statement := Attempt_{a}.proof\n\n"
        f"end {ROOT_DIR}.«{h}»\n",
        encoding="utf-8",
    )


# Axiom audit (CI runs this after `lake build`) ---------------------------------

ALLOWED_AXIOMS = frozenset({"propext", "Classical.choice", "Quot.sound"})
_AXIOMS_RE = re.compile(r"depends on axioms: \[(.*?)\]", re.S)


def audit_source(h: str, attempt: str | None = None) -> str:
    """A Lean file that fails unless the proof proves exactly `statement`, then lists axioms."""
    ns = f"{ROOT_DIR}.«{h}»"
    if attempt:
        module, proof = (
            f"{ns}.{ATTEMPT_DIR}.«{attempt}».Attempt",
            f"{_attempt_ns(h, attempt)}.proof",
        )
    else:
        module, proof = f"{ns}.Proof", f"{ns}.proof"
    return f"import {module}\nexample : {ns}.statement := {proof}\n#print axioms {proof}\n"


def parse_axioms(output: str) -> list[str]:
    if "does not depend on any axioms" in output:
        return []
    m = _AXIOMS_RE.search(output)
    if not m:
        raise ToolError(f"could not read axioms from Lean output: {output.strip()[:300]}")
    return [a.strip() for a in m.group(1).split(",") if a.strip()]


def forbidden_axioms(axioms: list[str]) -> list[str]:
    return [a for a in axioms if a not in ALLOWED_AXIOMS]


def audit(root: Path, h: str, attempt: str | None = None) -> list[str]:
    if not HASH_RE.match(h) or (attempt is not None and not HASH_RE.match(attempt)):
        return [f"bad hash {h!r}/{attempt!r}"]
    src = root / ".lake" / f"Audit_{h}_{attempt or 'proof'}.lean"
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
    if (root / ROOT_DIR / h / "Proof.lean").is_file():
        targets.append(f"{ROOT_DIR}.«{h}».Proof")
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
    targets: list[str | None] = [attempt] if attempt else []
    if (root / ROOT_DIR / h / "Proof.lean").is_file():
        targets.append(None)
    errors: list[str] = []
    for a in targets:
        src = out / f"Audit_{h}_{a or 'proof'}.lean"
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
        proof = folder / "Proof.lean"
        proof_text = proof.read_text(encoding="utf-8") if proof.is_file() else ""
        for ah, aa in _ATTEMPT_IMPORT_RE.findall(proof_text):
            att = root / ROOT_DIR / ah / ATTEMPT_DIR / aa / "Attempt.lean"
            if ah == folder.name and att.is_file():
                proof_text += "\n" + att.read_text(encoding="utf-8")
        deps = sorted(
            {d for d in _IMPORT_RE.findall(stmt_text + "\n" + proof_text) if d != folder.name}
        )
        entries[folder.name] = {
            "hash": folder.name,
            "statement": extract_statement(stmt_text),
            "statementLean": stmt_text,
            "proofLean": proof_text or None,
            "status": "proved" if proof.is_file() else "open",
            "dependencies": deps,
            "dependents": [],
            "valid": not errors,
            "featured": folder.name in curation["featured"],
            "example": folder.name in curation["examples"],
            "attempts": _attempt_tree(folder),
            "readings": load_readings(root, folder.name),
        }
    for h, e in entries.items():
        for d in e["dependencies"]:
            if d in entries:
                entries[d]["dependents"].append(h)
    for e in entries.values():
        e["dependents"].sort()
    return {"version": 2, "theorems": list(entries.values())}


# CLI ---------------------------------------------------------------------------


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
    p_check.add_argument("--base", default="origin/main")
    p_check.add_argument("--branch", default=None)
    sub.add_parser("index", help="print index.json for the site")
    p_att = sub.add_parser("attempt", help="start a new attempt, optionally continuing another")
    p_att.add_argument("theorem")
    p_att.add_argument("--from", dest="parent", default=None, help="attempt hash to continue")
    p_seal = sub.add_parser("seal", help="re-hash an edited attempt folder")
    p_seal.add_argument("folder", type=Path)
    p_prom = sub.add_parser("promote", help="make a complete attempt the theorem's proof")
    p_prom.add_argument("theorem")
    p_prom.add_argument("attempt")
    p_fast = sub.add_parser("fast", help="compile + audit a core-only folder without Mathlib")
    p_fast.add_argument("theorem")
    p_fast.add_argument("--attempt", default=None)
    p_prompt = sub.add_parser("prompt", help="print the exact translation prompt for a statement")
    p_prompt.add_argument("theorem")
    p_verify = sub.add_parser("verify-translation", help="check readings against archived chats")
    p_verify.add_argument("theorem")
    p_audit = sub.add_parser("audit", help="check proofs and axioms (needs `lake build` first)")
    p_audit.add_argument("hashes", nargs="*", help="default: every folder with a Proof.lean")
    p_audit.add_argument("--attempt", default=None, help="audit this complete attempt instead")
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
            changed = _git("diff", "--name-only", f"{args.base}...HEAD").split()
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
            for c in changed:
                parts = PurePosixPath(c).parts
                if len(parts) == 2 and parts[0] == TRANSLATION_DIR and parts[1].endswith(".toml"):
                    errors += check_translation(root, parts[1][: -len(".toml")])
            if any(c == CURATION_FILE for c in changed):
                errors += load_curation(root)[1]
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
        elif args.cmd == "promote":
            promote(root, args.theorem, args.attempt)
            print(f"wrote {ROOT_DIR}/{args.theorem}/Proof.lean")
        elif args.cmd == "prompt":
            print(translation_prompt(root, args.theorem))
        elif args.cmd == "verify-translation":
            errors = verify_translation(root, args.theorem)
            for e in errors:
                print(f"error: {e}", file=sys.stderr)
            if errors:
                return 1
            print("ok: archived chats show the exact prompt and output")
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
                p.parent.name for p in (root / ROOT_DIR).glob("*/Proof.lean")
            )
            errors = [e for h in hashes for e in audit(root, h, args.attempt)]
            for e in errors:
                print(f"error: {e}", file=sys.stderr)
            if errors:
                return 1
            print(f"ok: {len(hashes)} proof(s) use only {sorted(ALLOWED_AXIOMS)}")
        elif args.cmd == "index":
            json.dump(build_index(root), sys.stdout, ensure_ascii=False, indent=1)
            print()
    except ToolError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
