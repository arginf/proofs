#!/usr/bin/env python3
"""Merge new statements, checkpoints and proofs without a human (Python 3.11+, stdlib only).

Runs from the default branch after our own "PR checks" workflow (.github/workflows/pr.yml)
succeeds (workflow_run), so no PR code executes with write access. It merges a PR that *adds*
either
- one new Statement.lean (plain Lean that passes tools/safety.py, no sorry, at most
  DAILY_STATEMENTS per author per 24 h), or
- one attempt folder (Attempt.lean + attempt.toml) for a statement on main: a checkpoint if it
  still has `sorry` (DAILY_CHECKPOINTS a day), otherwise a proof (DAILY_PROOFS a day).
CI has already compiled it, re-checked it in the kernel and audited any proof's axioms; this
script re-runs the lexical safety check itself rather than trusting CI. Authors whose GitHub
account is younger than MIN_ACCOUNT_AGE_DAYS, and everything else, wait for a maintainer.

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

from tools import arginf, moderation, safety

_SORRY = re.compile(r"\bsorry\b")


# Guardrails for automatic merges; beyond them a maintainer merges.
DAILY_STATEMENTS = 5  # new statements per author per 24 h
DAILY_CHECKPOINTS = 20  # partial attempts per author per 24 h
DAILY_PROOFS = 10  # complete proofs per author per 24 h
MIN_ACCOUNT_AGE_DAYS = 7  # newer GitHub accounts always wait for a maintainer
PR_WORKFLOW_PATH = ".github/workflows/pr.yml"
MAX_PAGES = 50


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
    path = f"{arginf.ROOT_DIR}/{h}/Statement.lean"
    text = read(path)
    errors = safety.statement_errors(path, text, h)
    if errors:
        return Decision(False, "; ".join(errors), h, kind="statement")
    if recent_statements() >= DAILY_STATEMENTS:
        return Decision(
            False, f"author reached {DAILY_STATEMENTS} new statements in 24 h", h, kind="statement"
        )
    return Decision(True, "new Lean-only statement, checks passed", h, kind="statement")


def trusted_run(path: str, event: str) -> bool:
    """Only a run of our own PR workflow, for a pull request, can lead to a merge.

    workflow_run matches workflows by name, and a PR can add a workflow with any name."""
    return event == "pull_request" and path == PR_WORKFLOW_PATH


def all_pages(fetch: Callable[[int], list[Any]], max_pages: int = MAX_PAGES) -> list[Any]:
    """Every item of a paged GitHub list (100 per page), failing loudly past max_pages."""
    out: list[Any] = []
    for page in range(1, max_pages + 1):
        items = fetch(page)
        out += items
        if len(items) < 100:
            return out
    raise RuntimeError(f"more than {max_pages} pages")


def decide(
    files: list[dict[str, Any]],
    statement_exists: Callable[[str], bool],
    attempt_lean: Callable[[str], str],
    recent_statements: Callable[[], int],
    recent_checkpoints: Callable[[], int],
    recent_proofs: Callable[[], int],
    changed_files: int,
    account_age_days: Callable[[], float],
) -> Decision:
    """Merge a new statement (Statement.lean only) or a new attempt (proof or checkpoint)."""
    if changed_files != len(files) or not files:
        return Decision(False, f"file list incomplete ({len(files)} of {changed_files})")
    d = _decide(
        files, statement_exists, attempt_lean, recent_statements, recent_checkpoints, recent_proofs
    )
    if d.merge and account_age_days() < MIN_ACCOUNT_AGE_DAYS:
        return Decision(
            False,
            f"account younger than {MIN_ACCOUNT_AGE_DAYS} days: a maintainer merges",
            d.theorem,
            d.attempt,
            d.kind,
        )
    return d


def _decide(
    files: list[dict[str, Any]],
    statement_exists: Callable[[str], bool],
    attempt_lean: Callable[[str], str],
    recent_statements: Callable[[], int],
    recent_checkpoints: Callable[[], int],
    recent_proofs: Callable[[], int],
) -> Decision:
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
    path = f"{arginf.ROOT_DIR}/{h}/{arginf.ATTEMPT_DIR}/{a}/Attempt.lean"
    text = attempt_lean(path)
    errors = safety.attempt_errors(path, text)
    if errors:
        return Decision(False, "; ".join(errors), h, a)
    code = arginf.strip_comments(text)
    if _SORRY.search(code):
        # CI already enforced progress (no bare template, no unchanged copy of the parent).
        if recent_checkpoints() >= DAILY_CHECKPOINTS:
            return Decision(
                False, f"author reached {DAILY_CHECKPOINTS} checkpoints in 24 h", h, a, "checkpoint"
            )
        return Decision(True, "checkpoint (partial attempt), checks passed", h, a, "checkpoint")
    if recent_proofs() >= DAILY_PROOFS:
        return Decision(False, f"author reached {DAILY_PROOFS} proofs in 24 h", h, a)
    return Decision(True, "complete proof of an existing statement, checks passed", h, a)


def _no_model(texts: list[str]) -> list[float]:
    return [0.0 for _ in texts]


def _api(method: str, url: str, token: str, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "arginf-automerge",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1 or not re.fullmatch(r"[0-9a-f]{40}", args[0]):
        print("usage: automerge <head-sha>", file=sys.stderr)
        return 2
    (sha,) = args
    if not trusted_run(os.environ.get("WORKFLOW_PATH", ""), os.environ.get("WORKFLOW_EVENT", "")):
        print("skip: not a pull_request run of " + PR_WORKFLOW_PATH)
        return 0
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GH_TOKEN"]
    base = f"https://api.github.com/repos/{repo}"
    # workflow_run doesn't list fork PRs, so find the open PR whose head is exactly this commit.
    open_prs = all_pages(
        lambda page: _api(
            "GET", f"{base}/pulls?state=open&base=main&per_page=100&page={page}", token
        )
    )
    matches = [p for p in open_prs if p["head"]["sha"] == sha]
    if len(matches) != 1:
        print("skip: no single open PR at this commit (closed, updated or ambiguous)")
        return 0
    number = str(matches[0]["number"])
    pr = _api("GET", f"{base}/pulls/{number}", token)
    if pr["head"]["sha"] != sha or pr["state"] != "open" or pr["base"]["ref"] != "main":
        print("skip: the PR moved on since this check ran")
        return 0
    login = str(pr["user"]["login"])
    files = all_pages(
        lambda page: _api("GET", f"{base}/pulls/{number}/files?per_page=100&page={page}", token)
    )

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
            commits = all_pages(
                lambda page: _api(
                    "GET", f"{base}/commits?sha=main&since={since}&per_page=100&page={page}", token
                )
            )
            firsts = [c["commit"]["message"].split("\n", 1)[0] for c in commits]
            return sum(
                1
                for t in firsts
                if t.startswith("Merge #") and f": {kind} " in t and t.endswith(f"by @{login}")
            )

        return count

    def account_age_days() -> float:
        user = _api("GET", f"https://api.github.com/users/{login}", token)
        created = datetime.strptime(str(user["created_at"]), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
        return (datetime.now(UTC) - created).total_seconds() / 86400

    d = decide(
        files,
        statement_exists,
        attempt_lean,
        recent("statement"),
        recent("checkpoint"),
        recent("proof"),
        int(pr["changed_files"]),
        account_age_days,
    )
    print(f"{'merge' if d.merge else 'skip'}: {d.reason}")
    if not d.merge:
        return 0
    # Moderation gate: anything suspicious waits for a maintainer instead of merging.
    lean_texts = [attempt_lean(f["filename"]) for f in files if f["filename"].endswith(".lean")]
    reasons = moderation.moderate(
        str(pr.get("title") or ""),
        str(pr.get("body") or ""),
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
