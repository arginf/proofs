#!/usr/bin/env python3
"""Queue new statements, checkpoints and proofs for merging without a human (Python 3.11+).

Accepted PRs get an `arginf:<kind>` label (the ledger's record, see tools/ledger.py) and go into
GitHub's merge queue, which re-runs PR checks on the queued commit with main before merging.

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
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tools import announce, arginf, layout, ledger, limits, moderation, safety

_SORRY = re.compile(r"\bsorry\b")


# Guardrails for automatic merges; beyond them a maintainer merges. Daily caps per author per
# 24 h; a maintainer can raise or lower them for one author in limits.toml (tools/limits.py).
DAILY_STATEMENTS = limits.DEFAULT.statements
DAILY_CHECKPOINTS = limits.DEFAULT.checkpoints
DAILY_PROOFS = limits.DEFAULT.proofs
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
    caps: limits.Caps,
) -> Decision:
    if statement_exists(h):
        return Decision(False, f"statement {h} already exists", h, kind="statement")
    path = layout.statement_path(h)
    text = read(path)
    errors = safety.statement_errors(path, text, h)
    if errors:
        return Decision(False, "; ".join(errors), h, kind="statement")
    if recent_statements() >= caps.statements:
        return Decision(
            False, f"author reached {caps.statements} new statements in 24 h", h, kind="statement"
        )
    return Decision(True, "new Lean-only statement, checks passed", h, kind="statement")


_ENQUEUE = (
    "mutation($id: ID!, $sha: GitObjectID!) { enqueuePullRequest(input: "
    "{pullRequestId: $id, expectedHeadOid: $sha}) { mergeQueueEntry { position } } }"
)


def enqueue(
    api: Callable[..., Any], base: str, number: int, node_id: str, sha: str, kind: str
) -> None:
    """Label the PR with its kind (the ledger's record), then add it to the merge queue.

    expectedHeadOid makes GitHub refuse if the PR moved past the commit CI checked."""
    api("POST", f"{base}/issues/{number}/labels", {"labels": [ledger.label_for(kind)]})
    out = api(
        "POST",
        "https://api.github.com/graphql",
        {"query": _ENQUEUE, "variables": {"id": node_id, "sha": sha}},
    )
    if not isinstance(out, dict) or out.get("errors") or not out.get("data"):
        raise RuntimeError(f"could not enqueue #{number}: {out!r}")


def recent_count(
    entries: list[ledger.Entry],
    gap: list[ledger.Entry],
    open_prs: list[dict[str, Any]],
    login: str,
    kind: str,
    since: str,
) -> int:
    """Merges of `kind` by `login` since `since` (ledger plus merges after it), plus PRs of that
    kind still waiting in the queue, so filling the queue cannot get past a cap."""
    merged = ledger.recent(ledger.merge(entries, gap), login, kind, since)
    label = ledger.label_for(kind)
    me = login.lower()
    queued = sum(
        1
        for p in open_prs
        if str(p["user"]["login"]).lower() == me
        and label in [str(lab["name"]) for lab in p.get("labels", [])]
    )
    return merged + queued


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
    caps: limits.Caps = limits.DEFAULT,
) -> Decision:
    """Merge a new statement (Statement.lean only) or a new attempt (proof or checkpoint)."""
    if changed_files != len(files) or not files:
        return Decision(False, f"file list incomplete ({len(files)} of {changed_files})")
    d = _decide(
        files,
        statement_exists,
        attempt_lean,
        recent_statements,
        recent_checkpoints,
        recent_proofs,
        caps,
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
    caps: limits.Caps,
) -> Decision:
    expected = {"Attempt.lean", "attempt.toml"}
    folders: set[tuple[str, str]] = set()
    names: set[str] = set()
    statements: set[str] = set()
    for f in files:
        name = str(f.get("filename", ""))
        if f.get("status") != "added":
            return Decision(False, f"{name} is {f.get('status')}, not a new file")
        # Exact sharded shape: right shard, fixed depth, 20-digit hashes (tools/layout.py).
        parsed = layout.parse_path(name)
        if isinstance(parsed, str):
            return Decision(False, f"{name} is not a new statement or attempt ({parsed})")
        if parsed.kind == "statement":
            statements.add(parsed.statement)
            continue
        assert parsed.attempt is not None
        folders.add((parsed.statement, parsed.attempt))
        names.add(parsed.name)
    if statements and folders:
        return Decision(False, "a new statement and an attempt together: a maintainer merges")
    if statements:
        if len(statements) != 1:
            return Decision(False, "one new statement per PR")
        (h,) = statements
        return _decide_statement(h, statement_exists, attempt_lean, recent_statements, caps)
    if len(folders) != 1 or names != expected:
        return Decision(False, "must add exactly one attempt (Attempt.lean and attempt.toml)")
    ((h, a),) = folders
    if not statement_exists(h):
        return Decision(False, f"statement {h} is not on main yet")
    path = layout.attempt_path(h, a)
    text = attempt_lean(path)
    errors = safety.attempt_errors(path, text)
    if errors:
        return Decision(False, "; ".join(errors), h, a)
    code = arginf.strip_comments(text)
    if _SORRY.search(code):
        # CI already enforced progress (no bare template, no unchanged copy of the parent).
        if recent_checkpoints() >= caps.checkpoints:
            return Decision(
                False, f"author reached {caps.checkpoints} checkpoints in 24 h", h, a, "checkpoint"
            )
        return Decision(True, "checkpoint (partial attempt), checks passed", h, a, "checkpoint")
    if recent_proofs() >= caps.proofs:
        return Decision(False, f"author reached {caps.proofs} proofs in 24 h", h, a)
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
            _api("GET", f"{base}/contents/{layout.statement_path(h)}?ref=main", token)
            return True
        except OSError:
            return False

    def attempt_lean(path: str) -> str:
        meta = _api("GET", f"{base}/contents/{path}?ref={sha}", token)
        return base64.b64decode(meta["content"]).decode("utf-8")

    # Who merged what comes from the ledger (built by main from GitHub's records), plus PRs
    # merged since that run and PRs still queued. No ledger yet: count the last 24 h directly.
    now = datetime.now(UTC)
    since = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        entries, started = ledger.read_ledger(
            lambda url: _api("GET", url, token),
            lambda url: ledger.download(url, token),
            base,
            ledger.LEDGER_WORKFLOW,
        )
    except (ledger.LedgerError, OSError, KeyError) as err:
        print(f"no ledger ({err}); counting the last 24 h from the API, no earned credit")
        entries, started = [], None
    gap_since = max(since, started or since)
    q = f"repo:{repo} is:pr is:merged author:{login} merged:>={gap_since}"
    gap: list[ledger.Entry] = []
    for item in all_pages(
        lambda page: _api(
            "GET",
            "https://api.github.com/search/issues?"
            + urllib.parse.urlencode({"q": q, "per_page": 100, "page": page}),
            token,
        )["items"],
        max_pages=10,
    ):
        labels = [str(lab["name"]) for lab in item.get("labels", [])]
        if ledger.kind_from_labels(labels) is None:
            continue
        n = int(item["number"])
        merged_pr = _api("GET", f"{base}/pulls/{n}", token)

        def files_page(page: int, n: int = n) -> list[Any]:
            return list(_api("GET", f"{base}/pulls/{n}/files?per_page=100&page={page}", token))

        names = [str(f["filename"]) for f in all_pages(files_page)]
        if int(merged_pr.get("changed_files", -1)) != len(names):
            continue
        en = ledger.entry_from_pr(n, login, labels, merged_pr.get("merged_at"), names)
        if en is not None:
            gap.append(en)

    def recent(kind: str) -> Callable[[], int]:
        return lambda: recent_count(entries, gap, open_prs, login, kind, since)

    def account_age_days() -> float:
        user = _api("GET", f"https://api.github.com/users/{login}", token)
        created = datetime.strptime(str(user["created_at"]), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
        return (datetime.now(UTC) - created).total_seconds() / 86400

    # Read from main (this job's checkout), never from the PR. An invalid file stops here.
    root = Path.cwd()
    caps = limits.caps_for(limits.load(root), login, ledger.earned(entries, login))
    print(f"caps for @{login}: {caps}")
    d = decide(
        files,
        statement_exists,
        attempt_lean,
        recent("statement"),
        recent("checkpoint"),
        recent("proof"),
        int(pr["changed_files"]),
        account_age_days,
        caps,
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
    enqueue(
        lambda method, url, body=None: _api(method, url, token, body),
        base,
        int(number),
        str(pr["node_id"]),
        sha,
        d.kind,
    )
    # Status comment with the workflow's own token, quoting nothing from the PR (tools/announce.py).
    comment_token = os.environ.get("COMMENT_TOKEN") or token
    announce.upsert(
        lambda method, url, body=None: _api(method, url, comment_token, body),
        base,
        int(number),
        announce.queued_body(d.kind, int(number)),
    )
    print(f"queued #{number} ({d.kind}); the merge queue re-checks it with main before merging")
    return 0


if __name__ == "__main__":
    sys.exit(main())
