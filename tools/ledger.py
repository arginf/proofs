"""The merge ledger: which automatic merges happened, by whom (Python 3.11+, stdlib only).

With the merge queue, commit titles and commit authors no longer say who contributed what, and
commit authors are set by contributors anyway. The ledger is rebuilt from GitHub's own records:
each automatically merged PR carries exactly one `arginf:<kind>` label, which only automerge
(or a maintainer) can add, and its author and merge time come from the API. Merges from before
the queue are read once from automerge's old commit titles.

Main's publish job writes ledger.json as an artifact (and a public copy for transparency).
Automerge reads the artifact of the latest successful run, never the public copy.

    python3 -m tools.ledger build --previous old.json --out ledger.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from tools import layout

VERSION = 1
KINDS = ("statement", "checkpoint", "proof")
LABEL_PREFIX = "arginf:"
RENAME_FILE = "migrations/2026-11-hash20.json"
_LOGIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38})$")
_TIME_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
_HEX_RE = re.compile(rf"^[0-9a-f]{{{layout.HASH_LEN}}}$")
_OLD_TITLE_RE = re.compile(
    rf"^Merge #(\d+): (statement|proof|checkpoint) "
    rf"([0-9a-f]{{{layout.HASH_LEN}}}|[0-9a-f]{{{layout.LEGACY_HASH_LEN}}}) by @([A-Za-z0-9-]+)$"
)


LEDGER_WORKFLOW = "main.yml"  # its successful runs on main upload the `ledger` artifact
LEDGER_ARTIFACT = "ledger"


class LedgerError(Exception):
    """ledger.json is missing or malformed: callers fail closed."""


@dataclass(frozen=True, slots=True)
class Entry:
    pr: int
    author: str  # GitHub login, lowercase
    kind: str  # one of KINDS
    statement: str
    attempt: str | None  # None for statements
    merged_at: str  # UTC, YYYY-MM-DDTHH:MM:SSZ


def label_for(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    return LABEL_PREFIX + kind


def kind_from_labels(labels: Iterable[str]) -> str | None:
    """The kind if exactly one `arginf:<kind>` label is present, else None."""
    kinds = [lab[len(LABEL_PREFIX) :] for lab in labels if lab.startswith(LABEL_PREFIX)]
    return kinds[0] if len(kinds) == 1 and kinds[0] in KINDS else None


def entry_from_pr(
    number: int, login: str, labels: Iterable[str], merged_at: str | None, filenames: list[str]
) -> Entry | None:
    """An entry for a merged PR whose label and files agree, else None (never guessed)."""
    kind = kind_from_labels(labels)
    if kind is None or merged_at is None or not filenames:
        return None
    parsed = [layout.parse_path(f) for f in filenames]
    if any(isinstance(p, str) for p in parsed):
        return None
    ps = [p for p in parsed if not isinstance(p, str)]
    targets = {(p.kind, p.statement, p.attempt) for p in ps}
    if len(targets) != 1:
        return None
    ((pkind, h, a),) = targets
    if (kind == "statement") != (pkind == "statement"):
        return None
    return Entry(number, login.lower(), kind, h, a, merged_at)


def from_titles(rows: list[tuple[str, str]], renames: dict[str, Any]) -> list[Entry]:
    """Entries from automerge's commit titles before the merge queue (title, commit date)."""
    statements: dict[str, str] = renames.get("statements", {})
    attempts: dict[str, str] = renames.get("attempts", {})
    attempt_statement: dict[str, str] = renames.get("attemptStatement", {})
    out: list[Entry] = []
    for title, when in rows:
        m = _OLD_TITLE_RE.match(title)
        if not m:
            continue
        pr, kind, ref, who = int(m.group(1)), m.group(2), m.group(3), m.group(4).lower()
        if kind == "statement":
            h = statements.get(ref, ref)
            if _HEX_RE.match(h):
                out.append(Entry(pr, who, kind, h, None, when))
        else:
            a = attempts.get(ref, ref)
            h2 = attempt_statement.get(a)
            if h2 and _HEX_RE.match(a):
                out.append(Entry(pr, who, kind, h2, a, when))
    return out


def merge(*lists: Iterable[Entry]) -> list[Entry]:
    """All entries, one per PR (the first seen wins), oldest first."""
    seen: dict[int, Entry] = {}
    for entries in lists:
        for en in entries:
            seen.setdefault(en.pr, en)
    return sorted(seen.values(), key=lambda en: (en.merged_at, en.pr))


def dumps(entries: list[Entry]) -> str:
    return json.dumps({"version": VERSION, "entries": [asdict(en) for en in entries]}, indent=1)


def _entry(raw: Any) -> Entry:
    if not isinstance(raw, dict) or set(raw) != {f for f in Entry.__slots__}:
        raise LedgerError(f"bad entry {raw!r}")
    pr, author, kind, h, a, t = (raw[k] for k in Entry.__slots__)
    ok = (
        type(pr) is int
        and pr > 0
        and isinstance(author, str)
        and _LOGIN_RE.match(author)
        and kind in KINDS
        and isinstance(h, str)
        and _HEX_RE.match(h)
        and (a is None if kind == "statement" else isinstance(a, str) and bool(_HEX_RE.match(a)))
        and isinstance(t, str)
        and _TIME_RE.match(t)
    )
    if not ok:
        raise LedgerError(f"bad entry {raw!r}")
    return Entry(pr, author, kind, h, a, t)


def loads(text: str) -> list[Entry]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        raise LedgerError(f"ledger is not JSON: {err}") from err
    if not isinstance(data, dict) or data.get("version") != VERSION:
        raise LedgerError("unknown ledger version")
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise LedgerError("ledger has no entries list")
    return merge([_entry(r) for r in entries])


def earned(entries: list[Entry], login: str) -> int:
    """Statements added by someone else where `login`'s proof merged first.

    Proving your own statement, or one that is already proved, earns nothing."""
    creator: dict[str, str] = {}
    first: dict[str, str] = {}
    for en in sorted(entries, key=lambda x: (x.merged_at, x.pr)):
        if en.kind == "statement":
            creator.setdefault(en.statement, en.author)
        elif en.kind == "proof":
            first.setdefault(en.statement, en.author)
    me = login.lower()
    return sum(1 for h, who in first.items() if who == me and creator.get(h) != me)


def recent(entries: list[Entry], login: str, kind: str, since: str) -> int:
    """Merges of `kind` by `login` at or after `since` (same timestamp format)."""
    me = login.lower()
    return sum(1 for en in entries if en.author == me and en.kind == kind and en.merged_at >= since)


# Reading and building through the API ----------------------------------------------------------


def read_ledger(
    get_json: Callable[[str], Any], get_bytes: Callable[[str], bytes], base: str, workflow: str
) -> tuple[list[Entry], str | None]:
    """The ledger from the latest successful run of `workflow` on main, and when that run started.

    Read through the Actions API, never from the public copy (a CDN may serve it stale)."""
    import io
    import zipfile

    runs = get_json(
        f"{base}/actions/workflows/{workflow}/runs?branch=main&status=success&per_page=1"
    ).get("workflow_runs", [])
    if not runs:
        raise LedgerError(f"no successful {workflow} run on main")
    run = runs[0]
    arts = get_json(f"{base}/actions/runs/{run['id']}/artifacts?per_page=100").get("artifacts", [])
    art = [a for a in arts if a.get("name") == LEDGER_ARTIFACT and not a.get("expired")]
    if len(art) != 1:
        raise LedgerError(f"run {run['id']} has no {LEDGER_ARTIFACT} artifact")
    with zipfile.ZipFile(io.BytesIO(get_bytes(str(art[0]["archive_download_url"])))) as z:
        text = z.read("ledger.json").decode("utf-8")
    started = run.get("run_started_at") or run.get("created_at")
    return loads(text), (str(started) if started else None)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def download(url: str, token: str) -> bytes:
    """An artifact zip: GitHub answers with a redirect to storage, which must not get our token."""
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "User-Agent": "arginf-automerge"}
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=30) as r:
            return bytes(r.read())
    except urllib.error.HTTPError as err:
        location = err.headers.get("Location") if err.code in (301, 302, 303, 307, 308) else None
        if not location or not location.startswith("https://"):
            raise
    with urllib.request.urlopen(location, timeout=60) as r:
        return bytes(r.read())


def _api(url: str, token: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "arginf-ledger",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def merged_since(repo: str, token: str, since: str) -> list[Entry]:
    """Labelled PRs merged at or after `since`, with their files (paginated, capped)."""
    out: list[Entry] = []
    for kind in KINDS:
        q = f"repo:{repo} is:pr is:merged label:{label_for(kind)} merged:>={since}"
        for page in range(1, 11):  # search returns at most 1,000 results
            url = "https://api.github.com/search/issues?" + urllib.parse.urlencode(
                {"q": q, "per_page": 100, "page": page, "sort": "updated", "order": "asc"}
            )
            items = _api(url, token)["items"]
            for it in items:
                number = int(it["number"])
                pr = _api(f"https://api.github.com/repos/{repo}/pulls/{number}", token)
                files = _api(
                    f"https://api.github.com/repos/{repo}/pulls/{number}/files?per_page=100", token
                )
                if int(pr.get("changed_files", -1)) != len(files):
                    continue  # never attribute from a truncated file list
                en = entry_from_pr(
                    number,
                    str(pr["user"]["login"]),
                    [str(lab["name"]) for lab in pr.get("labels", [])],
                    pr.get("merged_at"),
                    [str(f["filename"]) for f in files],
                )
                if en is not None:
                    out.append(en)
            if len(items) < 100:
                break
    return out


def legacy_entries(root: Path) -> list[Entry]:
    log = subprocess.run(
        ["git", "log", "--first-parent", "--reverse", "--format=%s%x09%cI", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    rows: list[tuple[str, str]] = []
    for line in log.splitlines():
        title, _, when = line.rpartition("\t")
        rows.append((title, _utc(when)))
    path = root / RENAME_FILE
    renames = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return from_titles(rows, renames)


def _utc(iso: str) -> str:
    from datetime import UTC, datetime

    return datetime.fromisoformat(iso).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ledger")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="previous ledger + legacy titles + PRs merged since")
    b.add_argument("--previous", type=Path, default=None, help="default: the latest artifact")
    b.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    repo, token = os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"]
    previous: list[Entry] = []
    if args.previous is not None:
        previous = loads(args.previous.read_text(encoding="utf-8"))
    else:
        try:
            previous, _ = read_ledger(
                lambda url: _api(url, token),
                lambda url: download(url, token),
                f"https://api.github.com/repos/{repo}",
                LEDGER_WORKFLOW,
            )
        except (LedgerError, OSError) as err:
            print(f"no previous ledger ({err}): rebuilding from the API")
    legacy = legacy_entries(Path.cwd())
    since = previous[-1].merged_at if previous else "2026-11-01T00:00:00Z"
    fresh = merged_since(repo, token, since)
    entries = merge(previous, legacy, fresh)
    args.out.write_text(dumps(entries) + "\n", encoding="utf-8")
    print(f"ledger: {len(entries)} entries ({len(fresh)} fetched since {since})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
