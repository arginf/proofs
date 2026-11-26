"""Daily automerge caps, with per-author overrides in limits.toml (Python 3.11+, stdlib only).

limits.toml lives outside Theorems/, so only a maintainer-reviewed PR can change it
(CODEOWNERS), and automerge reads it from main, never from the PR it is judging.

    [authors.some-login]   # GitHub login, any case
    proofs = 100           # complete proofs per 24 h (default 10)
    checkpoints = 50       # partial attempts per 24 h (default 20)
    statements = 10        # new statements per 24 h (default 5)

A key left out keeps its default. 0 means that kind always waits for a maintainer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

import tomllib

LIMITS_FILE = "limits.toml"
MAX_CAP = 1000
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


class LimitsError(Exception):
    """limits.toml is invalid: automerge stops rather than guess."""


@dataclass(frozen=True, slots=True)
class Caps:
    statements: int = 5
    checkpoints: int = 20
    proofs: int = 10


DEFAULT = Caps()
_KEYS = frozenset(f.name for f in fields(Caps))


def parse(text: str) -> tuple[dict[str, Caps], list[str]]:
    """Overrides keyed by lowercased login, and every problem found."""
    try:
        raw: dict[str, Any] = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        return {}, [f"{LIMITS_FILE}: {err}"]
    errors = [f"{LIMITS_FILE}: unknown table {k!r}" for k in raw if k != "authors"]
    authors = raw.get("authors", {})
    if not isinstance(authors, dict):
        return {}, errors + [f"{LIMITS_FILE}: authors must be a table"]
    table: dict[str, Caps] = {}
    for login, entry in authors.items():
        where = f"{LIMITS_FILE}: authors.{login}"
        if not _LOGIN_RE.match(login):
            errors.append(f"{where}: not a GitHub login")
            continue
        if login.lower() in table:
            errors.append(f"{where}: listed twice (logins ignore case)")
            continue
        if not isinstance(entry, dict):
            errors.append(f"{where}: must be a table")
            continue
        caps = DEFAULT
        for key, value in entry.items():
            if key not in _KEYS:
                errors.append(f"{where}: unknown key {key!r} (allowed: {', '.join(sorted(_KEYS))})")
            elif type(value) is not int or not 0 <= value <= MAX_CAP:
                errors.append(f"{where}.{key}: must be a whole number from 0 to {MAX_CAP}")
            else:
                caps = replace(caps, **{key: value})
        table[login.lower()] = caps
    return table, errors


def load_text(text: str) -> dict[str, Caps]:
    table, errors = parse(text)
    if errors:
        raise LimitsError("; ".join(errors))
    return table


def load(root: Path) -> dict[str, Caps]:
    path = root / LIMITS_FILE
    return load_text(path.read_text(encoding="utf-8")) if path.is_file() else {}


def caps_for(table: dict[str, Caps], login: str) -> Caps:
    return table.get(login.lower(), DEFAULT)
