"""Lexical allowlist for theorem folders (Python 3.11+, standard library only).

Lean can run code while it compiles a file (`#eval`, `run_cmd`, macros, elaborators, simprocs)
and while it imports one (`initialize`). CI compiles untrusted Lean on the same runner that
decides whether a proof is verified, so theorem folders may hold only plain declarations.
This module is the first layer. The second is CI's kernel re-check (`leanchecker`), which
replays every declaration in a fresh kernel without running initializers.

The rules are deliberately strict: a proof that needs something refused here can be merged
by a maintainer after review.
"""

from __future__ import annotations

import re
from pathlib import Path

try:  # `python3 tools/arginf.py` puts tools/ on the path; `python3 -m tools.x` the repo root.
    from tools import layout
except ImportError:  # pragma: no cover
    import layout  # type: ignore[no-redef]

MAX_BYTES = 512 * 1024
MAX_DIGITS = 20
ROOT_DIR = layout.ROOT_DIR
ALLOWED_IMPORT_ROOTS = frozenset({"Init", "Std", "Lean", "Mathlib", "Cslib", "CslibFrontier"})
# Theorems.«ab».«hash».Statement or …attempts.«cd».«attempt».Attempt: shard checked against the
# hash, HASH_LEN digits (tools/layout.py).
_THEOREM_IMPORT_RE = layout.THEOREM_MODULE_RE
_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_']*)*$")

# Commands and modifiers that run code, extend the parser, add axioms or skip checks.
FORBIDDEN_KEYWORDS = frozenset(
    {
        "axiom",
        "opaque",
        "unsafe",
        "partial",
        "extern",
        "export",
        "initialize",
        "builtin_initialize",
        "run_cmd",
        "run_elab",
        "run_meta",
        "run_tac",
        "by_elab",
        "elab",
        "elab_rules",
        "macro",
        "macro_rules",
        "syntax",
        "declare_syntax_cat",
        "notation",
        "notation3",
        "infix",
        "infixl",
        "infixr",
        "prefix",
        "postfix",
        "binder_predicate",
        "attribute",
        "native_decide",
        "simproc",
        "dsimproc",
        "simproc_decl",
        "dsimproc_decl",
        "builtin_simproc",
        "builtin_dsimproc",
        "register_option",
        "register_builtin_option",
        "register_simp_attr",
        "declare_config_elab",
        "add_decl_doc",
        "include_str",
        "implemented_by",
    }
)
# Name components that reach IO, the compiler, or the elaborator from ordinary terms.
FORBIDDEN_NAMES = frozenset(
    {
        "IO",
        "EIO",
        "BaseIO",
        "unsafeBaseIO",
        "unsafeIO",
        "unsafeEIO",
        "unsafeCast",
        "ptrAddrUnsafe",
        "Elab",
        "Meta",
        "MetaM",
        "CoreM",
        "TermElabM",
        "TacticM",
        "CommandElabM",
        "Compiler",
        "Parser",
        "Environment",
        "evalExpr",
        "evalConst",
        "addDecl",
        "ofReduceBool",
        "ofReduceNat",
        "reduceBool",
        "reduceNat",
        "trustCompiler",
        "native",
        "dbgTrace",
        "dbg_trace",
        "dbgTraceIfShared",
        "System",
        "Process",
        "FS",
    }
)
ALLOWED_ATTRIBUTES = frozenset({"simp", "local", "reducible", "irreducible", "instance"})
OPTION_CAPS = {
    "maxHeartbeats": 1_000_000,
    "maxRecDepth": 10_000,
    "synthInstance.maxHeartbeats": 200_000,
    "synthInstance.maxSize": 1_000,
}
# Keywords that start a new command: never allowed inside a statement's body.
_COMMAND_KEYWORDS = frozenset(
    {
        "theorem",
        "lemma",
        "def",
        "abbrev",
        "instance",
        "example",
        "structure",
        "class",
        "inductive",
        "namespace",
        "section",
        "end",
        "variable",
        "universe",
        "set_option",
        "mutual",
        "noncomputable",
        "private",
        "protected",
        "import",
    }
)
# Invisible or direction-changing characters (Trojan Source), and C0 controls but \t \n \r.
_BAD_CHARS_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff]"
)
# An identifier: «quoted» parts or runs of word characters, joined by dots. Lean does not allow
# λ, Π or Σ inside identifiers, so they are separators here (Python counts them as letters).
_IDENT_RE = re.compile(r"(?:«[^»\n]*»|[^\W\d]\w*[!?']*|\w+)(?:\.(?:«[^»\n]*»|\w+[!?']*))*")
_NOT_IDENT = str.maketrans({"λ": " ", "Π": " ", "Σ": " "})
_LONG_NUMERAL_RE = re.compile(
    rf"(?<![\w«])(?:0[xX][0-9a-fA-F_]{{{MAX_DIGITS + 1},}}|[0-9_]{{{MAX_DIGITS + 1},}})"
)
_HASH_CMD_RE = re.compile(r"#[^\s\[(]")
_SET_OPTION_RE = re.compile(r"\bset_option\s+([A-Za-z_][\w.]*)\s+(\S+)")
_ATTR_RE = re.compile(r"@\[([^\[\]]*)\]")


class UnterminatedCommentError(ValueError):
    """A `/-` block comment that never closes."""


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
        raise UnterminatedCommentError("unterminated block comment")
    return "".join(out)


def _identifiers(code: str) -> list[str]:
    return _IDENT_RE.findall(code.translate(_NOT_IDENT))


def _components(ident: str) -> list[str]:
    return [p.strip("«»") for p in re.findall(r"«[^»]*»|[^.]+", ident)]


def _split_imports(code: str) -> tuple[list[str], str, list[str]]:
    """Leading `import` lines, the rest of the file, and errors for misplaced imports."""
    lines = code.splitlines()
    imports: list[str] = []
    i = 0
    while i < len(lines) and (not lines[i].strip() or lines[i].startswith("import")):
        if lines[i].strip():
            imports.append(lines[i])
        i += 1
    rest = "\n".join(lines[i:])
    errors = [] if not re.search(r"(?m)^\s*import\b", rest) else ["imports must come first"]
    return imports, rest, errors


def _import_errors(imports: list[str], allow_theorems: bool = True) -> list[str]:
    errors: list[str] = []
    for line in imports:
        m = re.fullmatch(r"import\s+(\S+)\s*", line)
        if not m:
            errors.append(f"malformed import line: {line.strip()[:80]}")
            continue
        mod = m.group(1)
        if mod.startswith(f"{ROOT_DIR}."):
            if not (allow_theorems and _THEOREM_IMPORT_RE.match(mod)):
                errors.append(f"import {mod}: only Statement or Attempt modules of {ROOT_DIR}/")
        elif not _MODULE_RE.match(mod) or mod.split(".", 1)[0] not in ALLOWED_IMPORT_ROOTS:
            errors.append(
                f"import {mod} is outside the allowlist ({', '.join(sorted(ALLOWED_IMPORT_ROOTS))})"
            )
    return errors


def _body_errors(code: str) -> list[str]:
    """Rules for everything after the imports, shared by statements and attempts."""
    errors: list[str] = []
    if '"' in code:
        errors.append("string literals are not allowed")
    if "`" in code:
        errors.append("quotations and name literals (`) are not allowed")
    if m := _HASH_CMD_RE.search(code):
        errors.append(
            f"`{code[m.start() : m.start() + 12].split()[0]}`: commands starting with # are not "
            "allowed (write `# s` with a space for Finset.card)"
        )
    if m := _LONG_NUMERAL_RE.search(code):
        errors.append(f"numeral longer than {MAX_DIGITS} digits: {m.group(0)[:30]}…")
    seen_kw: set[str] = set()
    seen_names: set[str] = set()
    for ident in _identifiers(code):
        if ident in FORBIDDEN_KEYWORDS:
            seen_kw.add(ident)
        for part in _components(ident):
            if part in FORBIDDEN_NAMES:
                seen_names.add(part)
            if part in FORBIDDEN_KEYWORDS and not ident.startswith("«"):
                seen_kw.add(part)
    errors += [f"`{kw}` is not allowed in theorem folders" for kw in sorted(seen_kw)]
    errors += [f"names under `{n}` are not allowed in theorem folders" for n in sorted(seen_names)]
    # Attributes: only @[simp], @[local simp], @[reducible], ...
    if code.count("@[") != len(_ATTR_RE.findall(code)):
        errors.append("malformed attribute: only plain @[simp]-style attributes are allowed")
    for inner in _ATTR_RE.findall(code):
        words = [w for w in re.split(r"[\s,]+", inner.strip()) if w]
        bad = [w for w in words if w not in ALLOWED_ATTRIBUTES]
        if bad or not words:
            errors.append(
                f"attribute @[{inner.strip()}]: only {', '.join(sorted(ALLOWED_ATTRIBUTES))}"
            )
    # Options: named limits only, with caps.
    uses = len(re.findall(r"\bset_option\b", code))
    found = _SET_OPTION_RE.findall(code)
    if uses != len(found):
        errors.append("malformed set_option")
    for name, value in found:
        cap = OPTION_CAPS.get(name)
        if cap is None:
            errors.append(f"set_option {name}: only {', '.join(sorted(OPTION_CAPS))}")
        elif not value.isdigit() or not 0 < int(value) <= cap:
            errors.append(f"set_option {name} {value}: must be between 1 and {cap}")
    return errors


def _common_errors(text: str) -> tuple[str | None, list[str]]:
    if len(text.encode("utf-8")) > MAX_BYTES:
        return None, [f"file is larger than {MAX_BYTES // 1024} KiB"]
    if m := _BAD_CHARS_RE.search(text):
        return None, [f"invisible or control character U+{ord(m.group(0)):04X} is not allowed"]
    try:
        return strip_comments(text), []
    except UnterminatedCommentError as err:
        return None, [str(err)]


def attempt_errors(path: Path | str, text: str) -> list[str]:
    """Errors for an Attempt.lean, or [] if it is plain Lean."""
    code, errors = _common_errors(text)
    if code is None:
        return [f"{path}: {e}" for e in errors]
    imports, rest, errors = _split_imports(code)
    errors += _import_errors(imports)
    errors += _body_errors(rest)
    return [f"{path}: {e}" for e in errors]


def statement_errors(path: Path | str, text: str, h: str) -> list[str]:
    """Errors for a Statement.lean: imports, then only `def statement : Prop := ...`."""
    code, errors = _common_errors(text)
    if code is None:
        return [f"{path}: {e}" for e in errors]
    imports, rest, errors = _split_imports(code)
    errors += _import_errors(imports)
    errors += _body_errors(rest)
    ns = re.escape(f"{ROOT_DIR}.«{h}»")
    m = re.fullmatch(
        rf"namespace {ns} def statement : Prop := (.+) end {ns}", " ".join(rest.split())
    )
    if not m:
        errors.append(
            f"must be imports, then `namespace {ROOT_DIR}.«{h}»`, `def statement : Prop := ...` "
            f"and `end {ROOT_DIR}.«{h}»`, with nothing else"
        )
    else:
        body = m.group(1)
        if re.search(r"\bsorry\b", body):
            errors.append("a statement may not contain sorry")
        cmds = sorted({i for i in _identifiers(body) if i in _COMMAND_KEYWORDS})
        if cmds or "@[" in body:
            errors.append(
                f"the statement body may not start new commands ({', '.join(cmds) or '@['})"
            )
    return [f"{path}: {e}" for e in errors]
