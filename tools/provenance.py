#!/usr/bin/env python3
"""Who merged each attempt, when, through which PR, and its 👍 count (Python 3.11+, stdlib).

Runs in CI on main (needs full git history and a read-only GITHUB_TOKEN):
    python3 -m tools.provenance embed index.json

Authors are the GitHub login of the PR that added the attempt: verified by GitHub, unlike free-text
git author names. Each statement's proofs get three markers: firstProof (earliest merged),
shortestProof (fewest non-whitespace characters) and topProof (most 👍 on its PR, if any).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tools import arginf, layout

type Commit = tuple[str, str]  # (sha, ISO date)
type PR = tuple[int, str, int]  # (number, author login, 👍)


def attempt_paths(root: Path) -> list[tuple[str, str]]:
    return [
        (t.name, a.name)
        for t in layout.statement_dirs(root)
        if arginf.HASH_RE.match(t.name)
        for a in layout.attempt_dirs(t)
        if arginf.HASH_RE.match(a.name) and (a / "Attempt.lean").is_file()
    ]


def legacy_paths(renames: dict[str, Any]) -> dict[tuple[str, str], str]:
    """(statement, attempt) -> the attempt's path before the 2026-11 layout migration.

    Moving an attempt makes git see a new file, so its first commit would be the migration's.
    Provenance asks git about the old path instead, keeping the original merge, PR and author."""
    new_of = renames.get("statements", {})
    old_of_attempt = {new: old for old, new in renames.get("attempts", {}).items()}
    old_of_statement = {new: old for old, new in new_of.items()}
    stmt_of = renames.get("attemptStatement", {})
    out: dict[tuple[str, str], str] = {}
    for a_new, a_old in old_of_attempt.items():
        h_new = stmt_of.get(a_new)
        h_old = old_of_statement.get(h_new) if h_new else None
        if h_new and h_old:
            out[(h_new, a_new)] = (
                f"{arginf.ROOT_DIR}/{h_old}/{arginf.ATTEMPT_DIR}/{a_old}/Attempt.lean"
            )
    return out


def collect(
    attempts: list[tuple[str, str]],
    first_commit: Callable[[str, str], Commit | None],
    pr_for: Callable[[str], PR | None],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for h, a in attempts:
        c = first_commit(h, a)
        if c is None:
            continue
        sha, date = c
        pr = pr_for(sha)
        out[a] = {
            "mergedAt": date,
            "commit": sha,
            "pr": pr[0] if pr else None,
            "author": pr[1] if pr else None,
            "upvotes": pr[2] if pr else 0,
        }
    return out


def embed(index: dict[str, Any], prov: dict[str, dict[str, Any]]) -> dict[str, Any]:
    for t in index["theorems"]:
        for a in t["attempts"]:
            a.update(prov.get(a["hash"], {}))
        proofs = [a for a in t["attempts"] if a["hash"] in set(t.get("proofs", []))]
        dated = [p for p in proofs if p.get("mergedAt")]
        t["firstProof"] = min(dated, key=lambda p: p["mergedAt"])["hash"] if dated else None
        t["shortestProof"] = (
            min(proofs, key=lambda p: (p["size"], p.get("mergedAt") or ""))["hash"]
            if proofs
            else None
        )
        voted = [p for p in proofs if p.get("upvotes", 0) > 0]
        t["topProof"] = (
            max(voted, key=lambda p: (p["upvotes"], -_order(dated, p)))["hash"] if voted else None
        )
    return index


def _order(dated: list[dict[str, Any]], p: dict[str, Any]) -> int:
    ordered = sorted(dated, key=lambda x: x["mergedAt"])
    return next((i for i, x in enumerate(ordered) if x["hash"] == p["hash"]), len(ordered))


def _git_first_commit(
    root: Path, legacy: dict[tuple[str, str], str] | None = None
) -> Callable[[str, str], Commit | None]:
    legacy = legacy or {}

    def run(h: str, a: str) -> Commit | None:
        path = legacy.get((h, a)) or layout.attempt_path(h, a)
        out = subprocess.run(
            ["git", "log", "--diff-filter=A", "--format=%H %cI", "--", path],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout.split("\n")  # fmt: skip
        lines = [ln for ln in out if ln.strip()]
        if not lines:
            return None
        sha, date = lines[-1].split(" ", 1)
        return sha, date

    return run


def _github_pr_for(repo: str, token: str | None) -> Callable[[str], PR | None]:
    def get(url: str) -> Any:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "arginf-ci"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=headers)  # noqa: S310 - fixed https host
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
            return json.loads(r.read())

    def run(sha: str) -> PR | None:
        pulls = get(f"https://api.github.com/repos/{repo}/commits/{sha}/pulls")
        merged = [p for p in pulls if p.get("merged_at")]
        if not merged:
            return None
        n = int(merged[0]["number"])
        issue = get(f"https://api.github.com/repos/{repo}/issues/{n}")
        return n, str(merged[0]["user"]["login"]), int(issue.get("reactions", {}).get("+1", 0))

    return run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="provenance")
    sub = parser.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("embed", help="add provenance and proof markers to index.json in place")
    e.add_argument("index", type=Path)
    args = parser.parse_args(argv)
    root = Path.cwd()
    repo = os.environ.get("GITHUB_REPOSITORY", "arginf/proofs")
    try:
        renames_file = root / "migrations" / "2026-11-hash20.json"
        renames = json.loads(renames_file.read_text()) if renames_file.is_file() else {}
        prov = collect(
            attempt_paths(root),
            _git_first_commit(root, legacy_paths(renames)),
            _github_pr_for(repo, os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")),
        )
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError) as err:
        print(f"warning: provenance unavailable ({err}); publishing without it", file=sys.stderr)
        prov = {}
    idx = json.loads(args.index.read_text(encoding="utf-8"))
    args.index.write_text(json.dumps(embed(idx, prov), ensure_ascii=False, indent=1) + "\n")
    print(f"provenance for {len(prov)} attempt(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
