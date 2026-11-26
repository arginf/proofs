"""Where statements and attempts live (Python 3.11+, standard library only).

Every level is sharded by the first two hex digits of its own hash, at a fixed depth, so no
directory grows past GitHub's recommended 3,000 entries at catalog scale:

    Theorems/<ab>/<hash>/Statement.lean                                  ab = hash[:2]
    Theorems/<ab>/<hash>/attempts/<cd>/<attempt>/{Attempt.lean,attempt.toml}   cd = attempt[:2]

Module paths follow the folders (Theorems.«ab».«hash».Statement). Namespaces do not: they stay
Theorems.«hash» and Theorems.«hash».Attempt_<attempt>, so Lean code names a statement by its
hash alone. Every path and module name is built here, and every path from a PR is read by
`parse_path`, which accepts exactly the two shapes above.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

ROOT_DIR = "Theorems"
ATTEMPT_DIR = "attempts"
STATEMENT_FILE = "Statement.lean"
ATTEMPT_FILES = frozenset({"Attempt.lean", "attempt.toml"})
# 80 bits: finding a junk statement that collides with a known one is out of reach (12 digits,
# 48 bits, took about a GPU-day). Hashes before 2026-11-27 had 12 digits; see migrations/.
HASH_LEN = 20
LEGACY_HASH_LEN = 12
SHARD_LEN = 2
HASH_RE = re.compile(rf"^[0-9a-f]{{{HASH_LEN}}}$")
STATEMENT_PARTS = 4  # Theorems/ab/hash/Statement.lean
ATTEMPT_PARTS = 7  # Theorems/ab/hash/attempts/cd/attempt/<file>

_H = rf"[0-9a-f]{{{HASH_LEN}}}"
# A theorem module, with the shard checked against the hash by backreference.
THEOREM_MODULE_RE = re.compile(
    rf"^{ROOT_DIR}\.«([0-9a-f]{{{SHARD_LEN}}})»\.«(\1[0-9a-f]{{{HASH_LEN - SHARD_LEN}}})»\."
    rf"(?:Statement|{ATTEMPT_DIR}\.«([0-9a-f]{{{SHARD_LEN}}})»\."
    rf"«(\3[0-9a-f]{{{HASH_LEN - SHARD_LEN}}})»\.Attempt)$"
)


def shard(h: str) -> str:
    return h[:SHARD_LEN]


def statement_dir(root: Path, h: str) -> Path:
    return root / ROOT_DIR / shard(h) / h


def attempt_dir(root: Path, h: str, a: str) -> Path:
    return statement_dir(root, h) / ATTEMPT_DIR / shard(a) / a


def statement_path(h: str) -> str:
    return f"{ROOT_DIR}/{shard(h)}/{h}/{STATEMENT_FILE}"


def attempt_path(h: str, a: str, name: str = "Attempt.lean") -> str:
    return f"{ROOT_DIR}/{shard(h)}/{h}/{ATTEMPT_DIR}/{shard(a)}/{a}/{name}"


def statement_module(h: str) -> str:
    return f"{ROOT_DIR}.«{shard(h)}».«{h}».Statement"


def attempt_module(h: str, a: str) -> str:
    return f"{ROOT_DIR}.«{shard(h)}».«{h}».{ATTEMPT_DIR}.«{shard(a)}».«{a}».Attempt"


def statement_namespace(h: str) -> str:
    return f"{ROOT_DIR}.«{h}»"


def attempt_namespace(h: str, a: str) -> str:
    return f"{ROOT_DIR}.«{h}».Attempt_{a}"


def module_statement(module: str) -> str | None:
    """The statement hash a theorem module belongs to, or None if it isn't one."""
    m = THEOREM_MODULE_RE.match(module)
    return m.group(2) if m else None


@dataclass(frozen=True, slots=True)
class Parsed:
    kind: Literal["statement", "attempt"]
    statement: str
    attempt: str | None
    name: str


def parse_path(raw: str) -> Parsed | str:
    """A repository path as a statement or attempt file, or the reason it is neither."""
    p = PurePosixPath(raw)
    parts = p.parts
    if p.is_absolute() or ".." in parts or "." in parts:
        return f"{raw}: path tricks are not allowed"
    if len(parts) < 3 or parts[0] != ROOT_DIR:
        return f"{raw}: outside {ROOT_DIR}/<ab>/<hash>/"
    sh, h = parts[1], parts[2]
    if not HASH_RE.match(h):
        return f"{raw}: {h!r} is not a {HASH_LEN}-digit statement hash"
    if sh != shard(h):
        return f"{raw}: statement {h} belongs in shard {shard(h)}/, not {sh}/"
    if len(parts) == STATEMENT_PARTS:
        if parts[3] != STATEMENT_FILE:
            return f"{raw}: only {STATEMENT_FILE} is allowed in a statement folder"
        return Parsed("statement", h, None, STATEMENT_FILE)
    if len(parts) == ATTEMPT_PARTS and parts[3] == ATTEMPT_DIR:
        ash, a, name = parts[4], parts[5], parts[6]
        if not HASH_RE.match(a):
            return f"{raw}: {a!r} is not a {HASH_LEN}-digit attempt hash"
        if ash != shard(a):
            return f"{raw}: attempt {a} belongs in shard {shard(a)}/, not {ash}/"
        if name not in ATTEMPT_FILES:
            return f"{raw}: only {', '.join(sorted(ATTEMPT_FILES))} are allowed in an attempt"
        return Parsed("attempt", h, a, name)
    return f"{raw}: not {ROOT_DIR}/<ab>/<hash>/{STATEMENT_FILE} or an attempt file"


def statement_dirs(root: Path) -> list[Path]:
    """Every statement folder in the sharded layout, sorted by hash."""
    base = root / ROOT_DIR
    if not base.is_dir():
        return []
    return sorted(
        (d for s in base.iterdir() if s.is_dir() for d in s.iterdir() if d.is_dir()),
        key=lambda d: d.name,
    )


def attempt_dirs(folder: Path) -> list[Path]:
    """Every attempt folder of a statement folder, sorted by hash."""
    base = folder / ATTEMPT_DIR
    if not base.is_dir():
        return []
    return sorted(
        (d for s in base.iterdir() if s.is_dir() for d in s.iterdir() if d.is_dir()),
        key=lambda d: d.name,
    )


def statement_of_attempt_dir(folder: Path) -> str:
    """Theorems/<ab>/<hash>/attempts/<cd>/<attempt> -> <hash>."""
    return folder.parent.parent.parent.name


def shape_errors(root: Path) -> list[str]:
    """Anything under Theorems/ that is not in its shard at the fixed depth."""
    base = root / ROOT_DIR
    if not base.is_dir():
        return []
    errors: list[str] = []
    for s in sorted(base.iterdir()):
        if not s.is_dir() or not re.fullmatch(rf"[0-9a-f]{{{SHARD_LEN}}}", s.name):
            errors.append(f"{s}: not a {ROOT_DIR}/<ab>/ shard")
            continue
        for d in sorted(s.iterdir()):
            if not d.is_dir() or not HASH_RE.match(d.name):
                errors.append(f"{d}: not a {ROOT_DIR}/<ab>/<{HASH_LEN}-digit hash>/ folder")
            elif shard(d.name) != s.name:
                errors.append(f"{d}: statement {d.name} belongs in shard {shard(d.name)}/")
            else:
                errors += _attempt_shape_errors(d)
    return errors


def _attempt_shape_errors(folder: Path) -> list[str]:
    base = folder / ATTEMPT_DIR
    if not base.is_dir():
        return []
    errors: list[str] = []
    for s in sorted(base.iterdir()):
        if not s.is_dir() or not re.fullmatch(rf"[0-9a-f]{{{SHARD_LEN}}}", s.name):
            errors.append(f"{s}: not an {ATTEMPT_DIR}/<cd>/ shard")
            continue
        for d in sorted(s.iterdir()):
            if not d.is_dir() or not HASH_RE.match(d.name):
                errors.append(f"{d}: not an attempt folder")
            elif shard(d.name) != s.name:
                errors.append(f"{d}: attempt {d.name} belongs in shard {shard(d.name)}/")
    return errors
