#!/usr/bin/env python3
"""Merge complete, verified proofs without a human (Python 3.11+, standard library only).

Runs from the default branch after "PR checks" succeed (workflow_run), so no PR code executes
with write access. It merges a PR that *adds* either
- one new Statement.lean (Lean only, no sorry, allowlisted imports, at most DAILY_STATEMENTS per
  author per 24 h), or
- one attempt folder (Attempt.lean + attempt.toml) with no `sorry` for a statement on main.
CI has already compiled it and audited any proof's axioms. Everything else, including partial
attempts, waits for a maintainer.

    python3 -m tools.automerge <head-sha>
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

from tools import arginf, moderation

_SORRY = re.compile(r"\bsorry\b")


# New statements merge on their own too (they're Lean only), within these guardrails:
ALLOWED_IMPORT_ROOTS = ("Init", "Std", "Lean", "Mathlib", "Cslib", "CslibFrontier")
DAILY_STATEMENTS = 5  # per author per 24 h; beyond that a maintainer merges
DAILY_CHECKPOINTS = 20  # partial attempts per author per 24 h (complete proofs are uncapped)


@dataclass(frozen=True, slots=True)
class Decision:
    merge: bool
    reason: str
    theorem: str | None = None
    attempt: str | None = None
    kind: str = "proof"


def _decide_statement(
    h: str,
    statement_exists: Callable[[str], bool],
    read: Callable[[str], str],
    recent_statements: Callable[[], int],
) -> Decision:
    if statement_exists(h):
        return Decision(False, f"statement {h} already exists", h, kind="statement")
    text = read(f"{arginf.ROOT_DIR}/{h}/Statement.lean")
    if _SORRY.search(arginf.strip_comments(text)):
        return Decision(False, "a statement may not contain sorry", h, kind="statement")
    for imp in re.findall(r"(?m)^import\s+(\S+)\s*$", text):
        if imp.split(".", 1)[0] not in ALLOWED_IMPORT_ROOTS:
            return Decision(False, f"import {imp} is outside the allowlist", h, kind="statement")
    if recent_statements() >= DAILY_STATEMENTS:
        return Decision(
            False, f"author reached {DAILY_STATEMENTS} new statements in 24 h", h, kind="statement"
        )
    return Decision(True, "new Lean-only statement, checks passed", h, kind="statement")


def decide(
    files: list[dict[str, Any]],
    statement_exists: Callable[[str], bool],
    attempt_lean: Callable[[str], str],
    recent_statements: Callable[[], int] = lambda: 0,
    recent_checkpoints: Callable[[], int] = lambda: 0,
) -> Decision:
    """Merge a new statement (Statement.lean only) or a new attempt (proof or checkpoint)."""
    expected = {"Attempt.lean", "attempt.toml"}
    folders: set[tuple[str, str]] = set()
    names: set[str] = set()
    statements: set[str] = set()
    for f in files:
        p = PurePosixPath(str(f.get("filename", "")))
        parts = p.parts
        if ".." in parts or p.is_absolute():
            return Decision(False, f"path trick: {p}")
        if f.get("status") != "added":
            return Decision(False, f"{p} is {f.get('status')}, not a new file")
        if (
            len(parts) == 3
            and parts[0] == arginf.ROOT_DIR
            and arginf.HASH_RE.match(parts[1])
            and parts[2] == "Statement.lean"
        ):
            statements.add(parts[1])
            continue
        if (
            len(parts) != 5
            or parts[0] != arginf.ROOT_DIR
            or not arginf.HASH_RE.match(parts[1])
            or parts[2] != arginf.ATTEMPT_DIR
            or not arginf.HASH_RE.match(parts[3])
            or parts[4] not in expected
        ):
            return Decision(False, f"{p} is not a new statement or attempt")
        folders.add((parts[1], parts[3]))
        names.add(parts[4])
    if statements and folders:
        return Decision(False, "a new statement and an attempt together: a maintainer merges")
    if statements:
        if len(statements) != 1:
            return Decision(False, "one new statement per PR")
        (h,) = statements
        return _decide_statement(h, statement_exists, attempt_lean, recent_statements)
    if len(folders) != 1 or names != expected:
        return Decision(False, "must add exactly one attempt (Attempt.lean and attempt.toml)")
    ((h, a),) = folders
    if not statement_exists(h):
        return Decision(False, f"statement {h} is not on main yet")
    code = arginf.strip_comments(
        attempt_lean(f"{arginf.ROOT_DIR}/{h}/{arginf.ATTEMPT_DIR}/{a}/Attempt.lean")
    )
    if _SORRY.search(code):
        # CI already enforced progress (no bare template, no unchanged copy of the parent).
        if recent_checkpoints() >= DAILY_CHECKPOINTS:
            return Decision(
                False, f"author reached {DAILY_CHECKPOINTS} checkpoints in 24 h", h, a, "checkpoint"
            )
        return Decision(True, "checkpoint (partial attempt), checks passed", h, a, "checkpoint")
    return Decision(True, "complete proof of an existing statement, checks passed", h, a)


def _no_model(texts: list[str]) -> list[float]:
    return [0.0 for _ in texts]


def _api(method: str, url: str, token: str, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310 - fixed https://api.github.com / raw hosts
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "arginf-automerge",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
        raw = r.read()
    return json.loads(raw) if raw else None


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1 or not re.fullmatch(r"[0-9a-f]{40}", args[0]):
        print("usage: automerge <head-sha>", file=sys.stderr)
        return 2
    (sha,) = args
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GH_TOKEN"]
    base = f"https://api.github.com/repos/{repo}"
    # workflow_run doesn't list fork PRs, so find the open PR whose head is exactly this commit.
    open_prs = _api("GET", f"{base}/pulls?state=open&base=main&per_page=100", token)
    matches = [p for p in open_prs if p["head"]["sha"] == sha]
    if len(matches) != 1:
        print("skip: no single open PR at this commit (closed, updated or ambiguous)")
        return 0
    number = str(matches[0]["number"])
    login = str(matches[0]["user"]["login"])
    files = _api("GET", f"{base}/pulls/{number}/files?per_page=100", token)

    def statement_exists(h: str) -> bool:
        try:
            _api("GET", f"{base}/contents/{arginf.ROOT_DIR}/{h}/Statement.lean?ref=main", token)
            return True
        except OSError:
            return False

    def attempt_lean(path: str) -> str:
        meta = _api("GET", f"{base}/contents/{path}?ref={sha}", token)
        return base64.b64decode(meta["content"]).decode("utf-8")

    def recent(kind: str) -> Callable[[], int]:
        """Merges of this kind for this author in the last 24 h (from our commit titles)."""

        def count() -> int:
            since = (datetime.now(UTC) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
            commits = _api("GET", f"{base}/commits?sha=main&since={since}&per_page=100", token)
            firsts = [c["commit"]["message"].split("\n", 1)[0] for c in commits]
            return sum(
                1
                for t in firsts
                if t.startswith("Merge #") and f": {kind} " in t and t.endswith(f"by @{login}")
            )

        return count

    d = decide(files, statement_exists, attempt_lean, recent("statement"), recent("checkpoint"))
    print(f"{'merge' if d.merge else 'skip'}: {d.reason}")
    if not d.merge:
        return 0
    # Moderation gate: anything suspicious waits for a maintainer instead of merging.
    lean_texts = [attempt_lean(f["filename"]) for f in files if f["filename"].endswith(".lean")]
    reasons = moderation.moderate(
        str(matches[0].get("title") or ""),
        str(matches[0].get("body") or ""),
        lean_texts,
        moderation.parse_word_list(os.environ.get("MODERATION_WORDS", "")),
        moderation.detoxify_scorer() if os.environ.get("MODERATION_MODEL") != "off" else _no_model,
    )
    if reasons:
        _api("POST", f"{base}/issues/{number}/labels", token, {"labels": ["needs-review"]})
        _api(
            "POST",
            f"{base}/issues/{number}/comments",
            token,
            {
                "body": "Not merged automatically; a maintainer will review it. Reason: "
                + "; ".join(reasons)
                + "."
            },
        )
        print(f"held for review: {'; '.join(reasons)}")
        return 0
    _api(
        "PUT",
        f"{base}/pulls/{number}/merge",
        token,
        {
            "sha": sha,
            "merge_method": "merge",
            "commit_title": (
                f"Merge #{number}: statement {d.theorem} by @{login}"
                if d.kind == "statement"
                else f"Merge #{number}: {d.kind} {d.attempt} by @{login}"
            ),
        },
    )
    # Merges made with GITHUB_TOKEN don't trigger push workflows, so publish explicitly.
    _api("POST", f"{base}/actions/workflows/main.yml/dispatches", token, {"ref": "main"})
    print(f"merged #{number} and started the main workflow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
