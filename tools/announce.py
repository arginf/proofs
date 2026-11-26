"""One status comment per automatically merged PR (Python 3.11+, stdlib only).

When automerge queues a PR it posts "accepted, queued". When main's ledger job sees the merge it
updates the same comment with the outcome and badges. Badges are stated only after the merge,
because "first proof" can change hands while PRs wait in the queue. Comments are written with
the workflow's own token, quote nothing from the PR, and are found again by MARKER plus the
github-actions bot as author, so a contributor's copy of the marker is never edited.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from tools import ledger, limits

MARKER = "<!-- arginf-status -->"
BOT_LOGIN = "github-actions[bot]"
SITE = "https://arginf.com"
FOOTER = (
    "\n\n---\n_arginf bot · checked by Lean's kernel and an axiom audit "
    "([how](https://github.com/arginf/proofs/blob/main/SECURITY.md#how-a-proof-is-trusted))_"
)
FIRST = "💡 First proof of this statement"
SHORTEST = "🎯 Shortest proof so far"
CREDIT = f"+{limits.PROOFS_PER_EARNED} to your daily automatic-merge cap for proofs"

_WHAT = {"statement": "statement", "checkpoint": "checkpoint", "proof": "proof"}


def statement_url(h: str) -> str:
    return f"{SITE}/theorems/{h}"


def queued_body(kind: str) -> str:
    what = _WHAT[kind]
    return (
        f"{MARKER}\n✅ **Lean accepted this {what}.** It's queued to merge, and the merge queue "
        "checks it once more together with the latest `main` (usually within a few minutes)."
        + FOOTER
    )


def _badges(
    en: ledger.Entry, entries: list[ledger.Entry], info: dict[str, tuple[int, int]]
) -> list[str]:
    proofs = [x for x in entries if x.kind == "proof" and x.statement == en.statement]
    proofs.sort(key=lambda x: (x.merged_at, x.pr))
    out: list[str] = []
    if proofs and proofs[0].pr == en.pr:
        out.append(FIRST)
        creator = next(
            (x.author for x in entries if x.kind == "statement" and x.statement == en.statement),
            None,
        )
        if creator != en.author:
            out.append(CREDIT)
    sized = [x for x in proofs if x.attempt in info]
    if sized:
        best = min(sized, key=lambda x: (info[str(x.attempt)][0], x.merged_at, x.pr))
        if best.pr == en.pr:
            out.append(SHORTEST)
    return out


def merged_body(
    en: ledger.Entry, entries: list[ledger.Entry], info: dict[str, tuple[int, int]]
) -> str:
    """The comment once merged. `info` maps attempt hash -> (size, open goals) on main."""
    url = statement_url(en.statement)
    if en.kind == "statement":
        text = f"📌 **Merged: new statement.** It's on [its page]({url}), open for proofs."
    elif en.kind == "checkpoint":
        goals = info.get(str(en.attempt), (0, 0))[1]
        text = (
            f"🧩 **Merged: checkpoint saved** with {goals} open goals. Anyone can continue it "
            f"from [the statement's page]({url})."
        )
    else:
        badges = _badges(en, entries, info)
        text = f"🏁 **Verified and merged.** See it on [the statement's page]({url})."
        if badges:
            text += "\n\n" + "\n".join(f"- {b}" for b in badges)
    return f"{MARKER}\n{text}{FOOTER}"


def to_announce(
    previous: list[ledger.Entry], entries: list[ledger.Entry], now: str, window_hours: int = 2
) -> list[ledger.Entry]:
    """Entries new since `previous` and merged within the window (never all history)."""
    seen = {x.pr for x in previous}
    t = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    since = (t - timedelta(hours=window_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [x for x in entries if x.pr not in seen and x.merged_at >= since]


def find_comment(comments: list[dict[str, Any]]) -> int | None:
    for c in comments:
        user = c.get("user") or {}
        if (
            user.get("login") == BOT_LOGIN
            and user.get("type") == "Bot"
            and MARKER in str(c.get("body", ""))
        ):
            return int(c["id"])
    return None


def upsert(api: Callable[..., Any], base: str, number: int, body: str) -> None:
    """Update our status comment on the PR, or post it if there is none yet."""
    comments: list[dict[str, Any]] = []
    for page in range(1, 11):
        batch = list(api("GET", f"{base}/issues/{number}/comments?per_page=100&page={page}"))
        comments += batch
        if len(batch) < 100:
            break
    cid = find_comment(comments)
    if cid is None:
        api("POST", f"{base}/issues/{number}/comments", {"body": body})
    else:
        api("PATCH", f"{base}/issues/comments/{cid}", {"body": body})
