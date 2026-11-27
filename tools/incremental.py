#!/usr/bin/env python3
"""Incremental CI: build, kernel-check and audit only what a change added (Python 3.11+, stdlib).

Merged files never change and the toolchain is pinned, so a proof checked once stays checked
until lean-toolchain or lake-manifest.json changes. Every check therefore works on the folders a
change added, and a merge costs the same at 10 statements as at 100,000:

    python3 -m tools.incremental plan --base <sha> [--head HEAD]   # JSON: statements, full, ...
    python3 -m tools.incremental targets <hash> ...                 # lake build arguments
    python3 -m tools.incremental lexical <hash> ...                 # folder + safety checks
    python3 -m tools.incremental audit --cache F <hash> ...         # axiom audit, cached
    python3 -m tools.incremental built --cache F <hash> ...         # record a build + kernel pass
    python3 -m tools.incremental matrix --cache F [--max-jobs N] [--max-modules M]
    python3 -m tools.incremental merge-cache --out F IN ...         # combine job caches

The audit cache (audit-cache.json, published next to the index) records, per environment key
(SHA-256 of lean-toolchain and lake-manifest.json), which attempts passed the audit and which
statements built and passed the kernel re-check. A toolchain or Mathlib bump changes the key, so
everything becomes pending and the full run (full-check.yml) works through it shard by shard,
a capped amount per run, resuming on the next run. Only passes are cached.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools import arginf, layout

# A change to any of these can change whether Lean accepts existing files: rebuild everything.
FULL_TRIGGERS = frozenset({"lean-toolchain", "lake-manifest.json", "lakefile.toml"})
# A change to the lexical rules re-runs the (cheap, Python-only) checks over every folder.
LEXICAL_TRIGGERS = frozenset({"tools/safety.py", "tools/arginf.py", "tools/layout.py"})
CACHE_VERSION = 1

type Auditor = Callable[[Path, str, str], list[str]]


@dataclass(frozen=True, slots=True)
class Plan:
    statements: list[str]
    full: bool
    lexical_full: bool
    reasons: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "statements": self.statements,
            "full": self.full,
            "lexicalFull": self.lexical_full,
            "reasons": self.reasons,
        }


def plan(changed: Iterable[str]) -> Plan:
    """What a diff (list of changed paths) needs checked."""
    statements: set[str] = set()
    reasons: list[str] = []
    full = lexical = False
    for raw in changed:
        parsed = layout.parse_path(raw)
        if not isinstance(parsed, str):
            statements.add(parsed.statement)
        elif raw in FULL_TRIGGERS:
            full = True
            reasons.append(f"{raw} changed: full rebuild")
        elif raw in LEXICAL_TRIGGERS:
            lexical = True
            reasons.append(f"{raw} changed: lexical checks over every folder")
    return Plan(sorted(statements), full, lexical or full, reasons)


def all_statements(root: Path) -> list[str]:
    return [d.name for d in layout.statement_dirs(root) if layout.HASH_RE.match(d.name)]


def _attempts(root: Path, h: str) -> list[str]:
    return [
        a.name
        for a in layout.attempt_dirs(layout.statement_dir(root, h))
        if layout.HASH_RE.match(a.name) and (a / "Attempt.lean").is_file()
    ]


def complete_attempts(root: Path, statements: Iterable[str]) -> list[tuple[str, str]]:
    return [(h, a) for h in statements for a in arginf.complete_attempts(root, h)]


def targets(root: Path, statements: Iterable[str]) -> list[str]:
    """`lake build` targets for these statements: their own modules; lake builds the imports."""
    out: list[str] = []
    for h in statements:
        if (layout.statement_dir(root, h) / "Statement.lean").is_file():
            out.append(f"+{layout.statement_module(h)}")
        out += [f"+{layout.attempt_module(h, a)}" for a in _attempts(root, h)]
    return out


def env_key(root: Path) -> str:
    """Changes whenever lean-toolchain or lake-manifest.json changes."""
    digest = hashlib.sha256()
    for name in ("lean-toolchain", "lake-manifest.json"):
        path = root / name
        data = path.read_bytes() if path.is_file() else b""
        digest.update(name.encode() + b"\0" + hashlib.sha256(data).digest())
    return digest.hexdigest()[:16]


class AuditCache:
    """Passes per environment key: audited attempts and built (kernel-checked) statements."""

    def __init__(self) -> None:
        self._audited: dict[str, set[str]] = {}
        self._built: dict[str, set[str]] = {}

    @classmethod
    def load(cls, path: Path) -> AuditCache:
        """A cache file, or a directory of shards (cache/audit/<ab>.json in the data tree)."""
        if path.is_dir():
            merged = cls()
            for shard in sorted(path.glob("*.json")):
                merged.merge(cls.load(shard))
            return merged
        cache = cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cache
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return cache
        for key, target in (("audited", cache._audited), ("built", cache._built)):
            section = data.get(key, {})
            if not isinstance(section, dict):
                return cls()
            for env, hashes in section.items():
                if not isinstance(hashes, list) or not all(
                    isinstance(x, str) and layout.HASH_RE.match(x) for x in hashes
                ):
                    return cls()  # anything malformed: start over rather than trust it
                target[str(env)] = set(hashes)
        return cache

    def save(self, path: Path) -> None:
        """To a file, or (if `path` is a directory) sharded by the first two hash digits."""
        if path.is_dir():
            shards: dict[str, AuditCache] = {}
            for env, hashes in self._audited.items():
                for x in hashes:
                    shards.setdefault(x[:2], AuditCache())._audited.setdefault(env, set()).add(x)
            for env, hashes in self._built.items():
                for x in hashes:
                    shards.setdefault(x[:2], AuditCache())._built.setdefault(env, set()).add(x)
            for shard, part in shards.items():
                part.save(path / f"{shard}.json")
            return
        data = {
            "version": CACHE_VERSION,
            "audited": {e: sorted(v) for e, v in sorted(self._audited.items())},
            "built": {e: sorted(v) for e, v in sorted(self._built.items())},
        }
        path.write_text(json.dumps(data, separators=(",", ":")) + "\n", encoding="utf-8")

    def merge(self, other: AuditCache) -> None:
        for mine, theirs in ((self._audited, other._audited), (self._built, other._built)):
            for env, hashes in theirs.items():
                mine.setdefault(env, set()).update(hashes)

    def prune(self, keep_env: str) -> None:
        """Drop entries for other environments (they can never be hit again)."""
        self._audited = {keep_env: self._audited.get(keep_env, set())}
        self._built = {keep_env: self._built.get(keep_env, set())}

    def add(self, h: str, a: str, env: str) -> None:
        del h  # attempt hashes are unique across statements
        self._audited.setdefault(env, set()).add(a)

    def done(self, a: str, env: str) -> bool:
        return a in self._audited.get(env, set())

    def add_built(self, h: str, env: str) -> None:
        self._built.setdefault(env, set()).add(h)

    def built(self, h: str, env: str) -> bool:
        return h in self._built.get(env, set())

    def size(self) -> int:
        return sum(len(v) for v in self._audited.values()) + sum(
            len(v) for v in self._built.values()
        )


def audit(
    root: Path, statements: Iterable[str], cache: AuditCache, env: str, auditor: Auditor
) -> list[str]:
    """Audit complete attempts not yet passed under `env`; cache each pass."""
    errors: list[str] = []
    for h, a in complete_attempts(root, statements):
        if cache.done(a, env):
            continue
        result = auditor(root, h, a)
        if result:
            errors += result
        else:
            cache.add(h, a, env)
    return errors


def mark_built(cache: AuditCache, root: Path, statements: Iterable[str], env: str) -> None:
    for h in statements:
        if layout.statement_dir(root, h).is_dir():
            cache.add_built(h, env)


def pending(root: Path, cache: AuditCache, env: str) -> list[str]:
    """Statements not yet built under `env`, or with a proof not yet audited under it."""
    return [
        h
        for h in all_statements(root)
        if not cache.built(h, env)
        or any(not cache.done(a, env) for a in arginf.complete_attempts(root, h))
    ]


def matrix(
    root: Path, cache: AuditCache, env: str, max_jobs: int, max_modules: int
) -> list[dict[str, str]]:
    """GitHub Actions matrix for the full run: pending statements in shard order, packed into
    at most `max_jobs` jobs of at most `max_modules` modules each. A statement is never split.
    Work beyond the caps waits for the next run, which starts from the cache."""
    jobs: list[list[str]] = []
    size = 0
    for h in pending(root, cache, env):
        n = len(targets(root, [h]))
        if jobs and size + n <= max_modules:
            jobs[-1].append(h)
            size += n
            continue
        if len(jobs) == max_jobs:
            break
        jobs.append([h])
        size = n
    return [
        {"name": f"{job[0][:2]}-{job[-1][:2]}", "statements": " ".join(job)} for job in jobs
    ]


def lexical(root: Path, statements: Iterable[str]) -> list[str]:
    """The PR checks' folder, attempt and safety checks for these statements only."""
    errors: list[str] = []
    for h in statements:
        folder = layout.statement_dir(root, h)
        if not folder.is_dir():
            errors.append(f"{h}: no folder at {folder}")
            continue
        errors += arginf.check_folder(folder)
        for att in layout.attempt_dirs(folder):
            errors += [e for e in arginf.check_attempt(att) if "no progress" not in e]
    return errors


def _changed(base: str, head: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", f"{base}...{head}"],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return [ln for ln in out.splitlines() if ln.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="incremental")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="what a diff needs checked (JSON)")
    p.add_argument("--base", required=True)
    p.add_argument("--head", default="HEAD")
    for name in ("targets", "lexical"):
        sub.add_parser(name).add_argument("hashes", nargs="*")
    for name in ("audit", "built"):
        q = sub.add_parser(name)
        q.add_argument("--cache", type=Path, required=True)
        q.add_argument("hashes", nargs="*")
    m = sub.add_parser("matrix", help="full-run jobs for pending work (JSON)")
    m.add_argument("--cache", type=Path, required=True)
    m.add_argument("--max-jobs", type=int, default=16)
    m.add_argument("--max-modules", type=int, default=1500)
    m.add_argument("--ignore-cache", action="store_true")
    mc = sub.add_parser("merge-cache", help="merge cache files, keeping the current environment")
    mc.add_argument("--out", type=Path, required=True)
    mc.add_argument("inputs", type=Path, nargs="*")
    args = parser.parse_args(argv)
    root = Path.cwd()
    for h in getattr(args, "hashes", []):
        if not layout.HASH_RE.match(h):
            print(f"error: {h!r} is not a statement hash", file=sys.stderr)
            return 2
    try:
        if args.cmd == "plan":
            print(json.dumps(plan(_changed(args.base, args.head)).as_json()))
        elif args.cmd == "targets":
            print(" ".join(targets(root, args.hashes)))
        elif args.cmd == "lexical":
            errors = lexical(root, args.hashes)
            for e in errors:
                print(f"error: {e}", file=sys.stderr)
            if errors:
                return 1
            print(f"ok: {len(args.hashes)} folder(s)")
        elif args.cmd in ("audit", "built"):
            env = env_key(root)
            cache = AuditCache.load(args.cache)
            if args.cmd == "built":
                mark_built(cache, root, args.hashes, env)
                cache.save(args.cache)
                print(f"recorded {len(args.hashes)} built statement(s)")
                return 0
            errors = audit(root, args.hashes, cache, env, arginf.audit)
            cache.save(args.cache)  # passes are kept even if another proof failed
            for e in errors:
                print(f"error: {e}", file=sys.stderr)
            if errors:
                return 1
            print(f"ok: audited under {env}")
        elif args.cmd == "merge-cache":
            env = env_key(root)
            merged = AuditCache()
            for path in args.inputs:
                merged.merge(AuditCache.load(path))  # malformed files count as empty
            merged.prune(env)
            merged.save(args.out)
            print(f"{merged.size()} cached pass(es) under {env}")
        elif args.cmd == "matrix":
            env = env_key(root)
            cache = AuditCache() if args.ignore_cache else AuditCache.load(args.cache)
            jobs = matrix(root, cache, env, args.max_jobs, args.max_modules)
            print(json.dumps({"include": jobs}))
            print(f"{len(pending(root, cache, env))} statement(s) pending", file=sys.stderr)
    except (arginf.ToolError, subprocess.CalledProcessError, OSError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
