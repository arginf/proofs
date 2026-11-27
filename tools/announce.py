"""One status comment per automatically merged PR (Python 3.11+, stdlib only).

When automerge queues a PR it posts "accepted, queued". When main's ledger job sees the merge it
updates the same comment with the outcome and badges. Badges are stated only after the merge,
because "first proof" can change hands while PRs wait in the queue. Comments are written with
the workflow's own token, quote nothing from the PR, and are found again by MARKER plus the
github-actions bot as author, so a contributor's copy of the marker is never edited.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from tools import layout, ledger, limits

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


STAGES = ("checks", "failed", "held", "accepted", "queued", "merged", "published")
_STAGE_RE = re.compile(r"<!-- arginf-stage: (\{[^\n]*?\}) -->")
_HEX = re.compile(rf"^[0-9a-f]{{{layout.HASH_LEN}}}$")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def stage_line(
    stage: str, pr: int, h: str | None = None, a: str | None = None, at: str | None = None
) -> str:
    """The hidden line the live /pr/<n> page reads: fixed keys, validated values only."""
    if stage not in STAGES or type(pr) is not int or pr <= 0:
        raise ValueError(f"bad stage {stage!r} for #{pr}")
    for v in (h, a):
        if v is not None and not _HEX.match(v):
            raise ValueError(f"bad hash {v!r}")
    data = {"v": 1, "stage": stage, "pr": pr, "hash": h, "attempt": a, "at": at or _now()}
    return f"<!-- arginf-stage: {json.dumps(data, separators=(',', ':'))} -->"


def parse_stage(body: str) -> dict[str, Any] | None:
    m = _STAGE_RE.search(body)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def with_stage(body: str, stage: str) -> str:
    """The same comment at a later stage (e.g. published), visible text unchanged."""
    old = parse_stage(body)
    if old is None:
        raise ValueError("no stage line")
    line = stage_line(stage, int(old["pr"]), old.get("hash"), old.get("attempt"))
    return _STAGE_RE.sub(lambda m: line, body, count=1)


def _compose(line: str, text: str, number: int) -> str:
    return f"{MARKER}\n{line}\n{text}{_track(number)}{FOOTER}"


def checks_body(number: int) -> str:
    return _compose(
        stage_line("checks", number),
        "⏳ **Checking.** Lean is building this PR and auditing any proof.",
        number,
    )


def failed_body(number: int) -> str:
    return _compose(
        stage_line("failed", number),
        "❌ **Checks failed.** Open the failed check above for Lean's message, fix it and push "
        "again.",
        number,
    )


def held_body(number: int) -> str:
    return _compose(
        stage_line("held", number),
        "✋ **Checks passed, waiting for a maintainer.** This PR is outside what merges "
        "automatically (see CONTRIBUTING: what merges how), so a person will review it.",
        number,
    )


def statement_url(h: str) -> str:
    return f"{SITE}/theorems/{h}"


def pr_url(number: int) -> str:
    return f"{SITE}/pr/{number}"


def _track(number: int) -> str:
    return f"\n\n[Track this PR live]({pr_url(number)})"


def queued_body(kind: str, number: int, h: str, a: str | None, stage: str = "queued") -> str:
    what = _WHAT[kind]
    return _compose(
        stage_line(stage, number, h, a),
        f"✅ **Lean accepted this {what}.** It's queued to merge, and the merge queue checks it "
        "once more together with the latest `main` (usually within a few minutes).",
        number,
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
    return _compose(stage_line("merged", en.pr, en.statement, en.attempt), text, en.pr)


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


def _comments(api: Callable[..., Any], base: str, number: int) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    for page in range(1, 11):
        batch = list(api("GET", f"{base}/issues/{number}/comments?per_page=100&page={page}"))
        comments += batch
        if len(batch) < 100:
            break
    return comments


def upsert(api: Callable[..., Any], base: str, number: int, body: str) -> None:
    """Update our status comment on the PR, or post it if there is none yet."""
    cid = find_comment(_comments(api, base, number))
    if cid is None:
        api("POST", f"{base}/issues/{number}/comments", {"body": body})
    else:
        api("PATCH", f"{base}/issues/comments/{cid}", {"body": body})


def publish(api: Callable[..., Any], base: str, number: int) -> bool:
    """Mark our comment "published" once the site has the merge. Only moves on from "merged";
    never posts, and never edits a comment that is not the bot's own."""
    comments = _comments(api, base, number)
    cid = find_comment(comments)
    if cid is None:
        return False
    body = str(next(c for c in comments if int(c["id"]) == cid)["body"])
    stage = parse_stage(body)
    if stage is None or stage.get("stage") != "merged":
        return False
    api("PATCH", f"{base}/issues/comments/{cid}", {"body": with_stage(body, "published")})
    return True


def main(argv: list[str] | None = None) -> int:
    """`announce published <pr>...`: run by the site's publish job after a deploy succeeds."""
    args = argv if argv is not None else sys.argv[1:]
    if len(args) < 2 or args[0] != "published" or not all(
        re.fullmatch(r"[1-9][0-9]{0,8}", n) for n in args[1:]
    ):
        print("usage: announce published <pr-number>...", file=sys.stderr)
        return 2
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["COMMENT_TOKEN"]
    base = f"https://api.github.com/repos/{repo}"
    for n in args[1:]:
        done = publish(lambda m, u, b=None: ledger._call(m, u, token, b), base, int(n))
        print(f"#{n}: {'published' if done else 'unchanged'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
