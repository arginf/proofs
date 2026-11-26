#!/usr/bin/env python3
"""One-time move to the sharded layout with 20-digit hashes (Python 3.11+, stdlib only).

    python3 -m tools.migrate_layout migrations/2026-11-hash20.json

Reads the legacy tree (Theorems/<12-digit hash>/attempts/<12-digit hash>/), checks every folder
against its legacy hash, and refuses to write anything if one doesn't match. Then it recomputes
every hash at HASH_LEN digits, rewrites module paths, namespaces and hash references, writes the
new tree to Theorems/<ab>/<hash>/attempts/<cd>/<attempt>/, rewrites curation.toml, and writes
an old-to-new map (statements, attempts, and each attempt's statement) for the site's redirects,
provenance and automerge's earned credit.

A statement hash covers only its Prop, so a statement that names no other statement keeps the
same SHA-256 and its new hash extends the old one. An attempt hash covers its parent's hash and
its code, so every attempt hash changes. Items are renamed in dependency order (a statement or
attempt after everything it references), so references are rewritten before hashing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools import arginf, layout

LEGACY = layout.LEGACY_HASH_LEN
_LEGACY_RE = re.compile(rf"^[0-9a-f]{{{LEGACY}}}$")
_QUOTED_RE = re.compile(rf"«([0-9a-f]{{{LEGACY}}})»")
_ATTEMPT_NAME_RE = re.compile(rf"Attempt_([0-9a-f]{{{LEGACY}}})(?![0-9a-f])")
_LEGACY_MODULE_RE = re.compile(
    rf"Theorems\.«([0-9a-f]{{{LEGACY}}})»\.(?:Statement|attempts\.«([0-9a-f]{{{LEGACY}}})»\.Attempt)"
    r"(?![\w.])"
)

type Renames = dict[str, Any]


class MigrationError(Exception):
    """The tree is not a clean legacy tree; nothing was written."""


@dataclass(slots=True)
class _Attempt:
    statement: str
    name: str
    text: str
    parent: str | None


def _legacy_statement_hash(text: str) -> str:
    return hashlib.sha256(arginf.extract_statement(text).encode()).hexdigest()[:LEGACY]


def _legacy_parent(folder: Path) -> str | None:
    try:
        raw = tomllib.loads((folder / "attempt.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise MigrationError(f"{folder}/attempt.toml: {err}") from err
    parent = raw.get("parent", "")
    if (
        set(raw) - {"parent"}
        or not isinstance(parent, str)
        or (parent and not _LEGACY_RE.match(parent))
    ):
        raise MigrationError(f"{folder}/attempt.toml: only `parent`, empty or a legacy hash")
    return parent or None


def _attempt_code(text: str) -> str:
    """What seal hashes: imports other than the statement's own, then the code."""
    extra = [ln for ln in text.splitlines() if ln.startswith("import ") and "Statement" not in ln]
    return "\n".join(extra + ["", arginf._attempt_code(text)]).strip()


def _legacy_attempt_hash(text: str, parent: str | None) -> str:
    material = f"parent:{parent or ''}\n{arginf._attempt_body(_attempt_code(text))}"
    return hashlib.sha256(material.encode()).hexdigest()[:LEGACY]


def _read_legacy(root: Path) -> tuple[dict[str, str], dict[str, _Attempt]]:
    base = root / layout.ROOT_DIR
    if not base.is_dir():
        raise MigrationError(f"no {layout.ROOT_DIR}/ folder")
    entries = sorted(base.iterdir())
    if not entries or not all(e.is_dir() and _LEGACY_RE.match(e.name) for e in entries):
        raise MigrationError(
            f"{layout.ROOT_DIR}/ must hold only legacy <{LEGACY}-digit hash>/ folders "
            "(already migrated, or mixed)"
        )
    statements: dict[str, str] = {}
    attempts: dict[str, _Attempt] = {}
    for folder in entries:
        h = folder.name
        text = (folder / "Statement.lean").read_text(encoding="utf-8")
        if _legacy_statement_hash(text) != h:
            raise MigrationError(f"{folder}: name does not match its legacy statement hash")
        statements[h] = text
        adir = folder / layout.ATTEMPT_DIR
        for att in sorted(adir.iterdir()) if adir.is_dir() else []:
            if not _LEGACY_RE.match(att.name):
                raise MigrationError(f"{att}: not a legacy attempt folder")
            parent = _legacy_parent(att)
            atext = (att / "Attempt.lean").read_text(encoding="utf-8")
            if _legacy_attempt_hash(atext, parent) != att.name:
                raise MigrationError(f"{att}: name does not match its legacy attempt hash")
            if att.name in attempts:
                raise MigrationError(f"attempt {att.name} appears under two statements")
            attempts[att.name] = _Attempt(h, att.name, atext, parent)
    for a in attempts.values():
        if a.parent and (a.parent not in attempts or attempts[a.parent].statement != a.statement):
            raise MigrationError(f"attempt {a.name}: parent {a.parent} not found on {a.statement}")
    return statements, attempts


def _refs(text: str) -> set[str]:
    return set(_QUOTED_RE.findall(text)) | set(_ATTEMPT_NAME_RE.findall(text))


def _rewrite(text: str, new: dict[str, str]) -> str:
    """Module paths, «hash» references and Attempt_<hash> names, for every renamed item."""

    def module(m: re.Match[str]) -> str:
        h, a = m.group(1), m.group(2)
        if h not in new or (a is not None and a not in new):
            return m.group(0)
        return (
            layout.statement_module(new[h]) if a is None else layout.attempt_module(new[h], new[a])
        )

    text = _LEGACY_MODULE_RE.sub(module, text)
    text = _QUOTED_RE.sub(lambda m: f"«{new.get(m.group(1), m.group(1))}»", text)
    return _ATTEMPT_NAME_RE.sub(lambda m: f"Attempt_{new.get(m.group(1), m.group(1))}", text)


def plan(root: Path) -> tuple[Renames, dict[str, str]]:
    """The renames and the new files (path -> text), without writing anything."""
    statements, attempts = _read_legacy(root)
    known = set(statements) | set(attempts)
    deps: dict[str, set[str]] = {}
    for h, text in statements.items():
        deps[h] = (_refs(text) & known) - {h}
    for a, att in attempts.items():
        d = (_refs(att.text) & known) | {att.statement} | ({att.parent} if att.parent else set())
        deps[a] = d - {a}
    new: dict[str, str] = {}
    files: dict[str, str] = {}
    pending = sorted(known)
    while pending:
        ready = [x for x in pending if deps[x] <= set(new)]
        if not ready:
            raise MigrationError(f"reference cycle among {pending[:5]}")
        for x in ready:
            if x in statements:
                body = _rewrite(statements[x], new)
                nh = arginf.statement_hash(body)
                new[x] = nh
                files[layout.statement_path(nh)] = body.replace(f"«{x}»", f"«{nh}»")
            else:
                att = attempts[x]
                code = _attempt_code(_rewrite(att.text, new))
                parent = new[att.parent] if att.parent else None
                na = arginf.attempt_hash(code, parent)
                new[x] = na
                nh = new[att.statement]
                files[layout.attempt_path(nh, na)] = arginf.attempt_text(nh, na, code)
                files[layout.attempt_path(nh, na, "attempt.toml")] = f'parent = "{parent or ""}"\n'
        pending = [x for x in pending if x not in new]
    renames: Renames = {
        "statements": {h: new[h] for h in sorted(statements)},
        "attempts": {a: new[a] for a in sorted(attempts)},
        "attemptStatement": {new[a]: new[att.statement] for a, att in sorted(attempts.items())},
    }
    if len(set(new.values())) != len(new):
        raise MigrationError("two items map to the same new hash")
    return renames, files


def _rewrite_curation(root: Path, statements: dict[str, str]) -> str | None:
    path = root / arginf.CURATION_FILE
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    return re.sub(
        rf'"([0-9a-f]{{{LEGACY}}})"', lambda m: f'"{statements.get(m.group(1), m.group(1))}"', text
    )


def migrate(root: Path) -> Renames:
    """Move the legacy tree to the new layout in place; return the renames."""
    renames, files = plan(root)
    curation = _rewrite_curation(root, renames["statements"])
    # Build the new tree beside the repo and check it with the new rules before swapping.
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp)
        for rel, text in files.items():
            (staged / rel).parent.mkdir(parents=True, exist_ok=True)
            (staged / rel).write_text(text, encoding="utf-8")
        if curation is not None:
            (staged / arginf.CURATION_FILE).write_text(curation, encoding="utf-8")
        errors = arginf.check_all(staged)
        if errors:
            raise MigrationError("the migrated tree fails its checks:\n" + "\n".join(errors[:20]))
        shutil.rmtree(root / layout.ROOT_DIR)
        shutil.move(str(staged / layout.ROOT_DIR), str(root / layout.ROOT_DIR))
        if curation is not None:
            (root / arginf.CURATION_FILE).write_text(curation, encoding="utf-8")
    return renames


def attempt_pairs(renames: Renames) -> list[tuple[tuple[str, str], tuple[str, str]]]:
    """((old statement, old attempt), (new statement, new attempt)) for every attempt."""
    old_of = {new: old for old, new in renames["statements"].items()}
    return [
        (
            (old_of[renames["attemptStatement"][a_new]], a_old),
            (renames["attemptStatement"][a_new], a_new),
        )
        for a_old, a_new in renames["attempts"].items()
    ]


def write_map(renames: Renames, path: Path) -> None:
    data = {
        "hashLength": {"from": LEGACY, "to": layout.HASH_LEN},
        "statements": renames["statements"],
        "attempts": renames["attempts"],
        "attemptStatement": renames["attemptStatement"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="migrate_layout")
    parser.add_argument("map", type=Path, help="where to write the old-to-new map (JSON)")
    args = parser.parse_args(argv)
    try:
        renames = migrate(Path.cwd())
    except (MigrationError, arginf.ToolError, OSError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    write_map(renames, args.map)
    print(
        f"moved {len(renames['statements'])} statement(s) and {len(renames['attempts'])} "
        f"attempt(s); map in {args.map}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
