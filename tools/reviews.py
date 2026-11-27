#!/usr/bin/env python3
"""reviews.json: open PRs whose `scope` and `lean` checks passed (Python 3.11+, stdlib only).

Built by the publish workflow with its own read-only token, so visitors never call the GitHub
API (anonymous calls are capped at 60 an hour per IP, which one visit used up at ~60 open PRs).
Only "#n by @login" facts are kept: never titles or descriptions.

    GH_TOKEN=... python3 -m tools.reviews > reviews.json
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from collections.abc import Callable
from typing import Any

from tools import layout

LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REQUIRED = ("scope", "lean")
MAX_PAGES = 30

type Fetch = Callable[[str], Any]


def _passed(runs: Any) -> bool:
    items = runs.get("check_runs", []) if isinstance(runs, dict) else []
    ok = {
        r.get("name")
        for r in items
        if isinstance(r, dict)
        and r.get("status") == "completed"
        and r.get("conclusion") == "success"
    }
    return all(name in ok for name in REQUIRED)


def build(fetch: Fetch, repo: str = "arginf/proofs") -> dict[str, Any]:
    pulls: list[Any] = []
    for page in range(1, MAX_PAGES + 1):
        batch = fetch(f"/pulls?state=open&per_page=100&page={page}")
        if not isinstance(batch, list) or not batch:
            break
        pulls += batch
    out: list[dict[str, Any]] = []
    url_re = re.compile(rf"^https://github\.com/{re.escape(repo)}/pull/\d+$")
    for p in pulls:
        if not isinstance(p, dict):
            continue
        number, head, user = p.get("number"), p.get("head") or {}, p.get("user") or {}
        login, ref, sha = user.get("login"), head.get("ref"), head.get("sha")
        if not (
            isinstance(number, int) and number > 0 and isinstance(ref, str) and isinstance(sha, str)
        ):
            continue
        if not (isinstance(login, str) and LOGIN_RE.match(login)):
            continue
        if not url_re.match(str(p.get("html_url", ""))) or not re.fullmatch(r"[0-9a-f]{7,64}", sha):
            continue
        theorem, _, rest = ref.partition("/")
        if not layout.HASH_RE.match(theorem):
            continue
        if not _passed(fetch(f"/commits/{sha}/check-runs?per_page=100")):
            continue
        out.append(
            {
                "number": number,
                "author": login,
                "theorem": theorem,
                "attempt": rest if layout.HASH_RE.match(rest) else None,
                "draft": bool(p.get("draft", False)),
            }
        )
    return {"version": 1, "pulls": sorted(out, key=lambda x: x["number"])}


def _github(repo: str, token: str | None) -> Fetch:
    def fetch(path: str) -> Any:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "arginf-ci"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}{path}", headers=headers)  # noqa: S310
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - fixed https host
            return json.loads(r.read())

    return fetch


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "arginf/proofs")
    try:
        result = build(_github(repo, os.environ.get("GH_TOKEN")), repo)
    except (OSError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, separators=(",", ":"))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
