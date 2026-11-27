#!/usr/bin/env python3
"""Publish the data tree (tools/data.py) to the `data` branch, incrementally (Python 3.11+).

    python3 -m tools.publish checkout --remote URL --dir data     # clone `data` (or start empty)
    python3 -m tools.publish wait --dir data                      # sleep until 6 min since last
    python3 -m tools.publish build --dir data --commit SHA [--also H ...] [--extra NAME=FILE ...]
    python3 -m tools.publish push --dir data --remote URL --message M   # one commit, one push
    python3 -m tools.publish squash --dir data --remote URL       # weekly: history -> 1 commit
    python3 -m tools.publish cache-join --dir data/cache/readings > readings.json
    python3 -m tools.publish cache-split --dir data/cache/readings readings.json

A build rebuilds only the statements that changed since the commit in the previous manifest,
plus the statements they import (whose dependents change); everything else is carried forward
by content hash. Files the new manifest no longer references are listed in meta/retired.json
and deleted by the next build, so a visitor holding the previous manifest can still fetch them.
Readings and areas come from caches in the tree (cache/readings/<ab>.json, cache/areas/<ab>.json).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools import arginf, areas, data, incremental, layout, provenance, readings

MIN_GAP_SECONDS = 360  # Pages builds from the branch: at most 10 builds an hour
RETIRED = "meta/retired.json"
SIZE_HISTORY = "meta/size-history.json"
HISTORY_KEEP_SECONDS = 120 * 24 * 3600

type Runner = Callable[[list[str], Path], str]
type RecordFn = Callable[..., dict[str, Any]]
type ProvenanceFn = Callable[[str, list[str]], dict[str, dict[str, Any]]]


@dataclass(slots=True)
class Result:
    full: bool
    affected: list[str]
    written: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    bytes: int = 0


# Caches (readings, areas): lists of {"hash": ..., "modelSha256": ...}, sharded by hash prefix ---


def read_cache(directory: Path, hashes: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """All entries, or only the shards holding `hashes`."""
    if not directory.is_dir():
        return []
    shards = (
        sorted(directory.glob("*.json"))
        if hashes is None
        else [directory / f"{s}.json" for s in sorted({h[:2] for h in hashes})]
    )
    out: list[dict[str, Any]] = []
    wanted = None if hashes is None else set(hashes)
    for path in shards:
        if not path.is_file():
            continue
        for entry in json.loads(path.read_text(encoding="utf-8")):
            if isinstance(entry, dict) and (wanted is None or entry.get("hash") in wanted):
                out.append(entry)
    return out


def write_cache(directory: Path, entries: Iterable[dict[str, Any]]) -> None:
    """Merge entries into their shards; an entry replaces one with the same (hash, model)."""
    by_shard: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in entries:
        if layout.HASH_RE.match(str(e.get("hash", ""))):
            by_shard[str(e["hash"])[:2]].append(e)
    directory.mkdir(parents=True, exist_ok=True)
    for shard, new in by_shard.items():
        path = directory / f"{shard}.json"
        old = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
        keyed = {(str(e.get("hash")), str(e.get("modelSha256"))): e for e in old}
        keyed.update({(str(e["hash"]), str(e.get("modelSha256"))): e for e in new})
        rows = [keyed[k] for k in sorted(keyed)]
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")


# The previous tree --------------------------------------------------------------------------


class Previous:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.manifest: dict[str, Any] | None = None
        path = out / "manifest.json"
        if path.is_file():
            try:
                m = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(m, dict) and m.get("version") == data.VERSION:
                    self.manifest = m
            except ValueError:
                pass
        self.files: dict[str, str] = dict(self.manifest["files"]) if self.manifest else {}
        self._rows: dict[str, dict[str, list[Any]]] = {}

    @property
    def commit(self) -> str | None:
        return str(self.manifest["commit"]) if self.manifest else None

    def rows(self, shard: str) -> dict[str, list[Any]]:
        if shard not in self._rows:
            path = self.files.get(f"summary/{shard}")
            rows: dict[str, list[Any]] = {}
            if path and (self.out / path).is_file():
                body = json.loads((self.out / path).read_text(encoding="utf-8"))
                rows = {str(r[0]): r for r in body["rows"]}
            self._rows[shard] = rows
        return self._rows[shard]

    def all_rows(self) -> dict[str, list[Any]]:
        out: dict[str, list[Any]] = {}
        for name in self.files:
            if name.startswith("summary/"):
                out.update(self.rows(name.split("/", 1)[1]))
        return out

    def detail(self, h: str) -> dict[str, Any] | None:
        row = self.rows(h[:2]).get(h)
        if row is None:
            return None
        path = self.out / data.detail_path(h, str(row[data.SUMMARY_COLS.index("detail")]))
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def detail_paths(h: str, c: str, detail: Mapping[str, Any] | None) -> set[str]:
    paths = {data.detail_path(h, c)}
    for chunk in (detail or {}).get("leanChunks", []):
        paths.add(data.chunk_path(h, chunk))
    return paths


# Build --------------------------------------------------------------------------------------


def _allowed(root: Path) -> set[str]:
    try:
        return {m.sha256 for m in readings.load_models(root / "models.toml").models}
    except (OSError, readings.ReadingError):
        return set()


def default_provenance(root: Path) -> ProvenanceFn:
    renames_file = root / "migrations" / "2026-11-hash20.json"
    renames = json.loads(renames_file.read_text()) if renames_file.is_file() else {}
    first = provenance._git_first_commit(root, provenance.legacy_paths(renames))
    pr_for = provenance._github_pr_for(
        os.environ.get("GITHUB_REPOSITORY", "arginf/proofs"),
        os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"),
    )

    def run(h: str, attempts: list[str]) -> dict[str, dict[str, Any]]:
        return provenance.collect([(h, a) for a in attempts], first, pr_for)

    return run


PROV_KEYS = ("mergedAt", "commit", "pr", "author", "upvotes")


def _write(out: Path, rel: str, body: bytes, written: list[str]) -> None:
    path = out / rel
    if path.is_file() and path.read_bytes() == body:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    written.append(rel)


def _tree_bytes(out: Path) -> int:
    return sum(p.stat().st_size for p in out.rglob("*") if p.is_file() and ".git" not in p.parts)


def build(
    root: Path,
    out: Path,
    commit: str,
    changed: list[str] | None,
    also: Iterable[str] = (),
    extra: Mapping[str, bytes] | None = None,
    record_fn: RecordFn = arginf.statement_record,
    provenance_fn: ProvenanceFn | None = None,
) -> Result:
    """Update the tree in `out` for `commit`. `changed` is the diff since the previous manifest's
    commit (None: rebuild everything)."""
    out.mkdir(parents=True, exist_ok=True)
    prev = Previous(out)
    for rel in _load_json(out / RETIRED, []):
        (out / str(rel)).unlink(missing_ok=True)
    existing = set(incremental.all_statements(root))
    full = (
        prev.manifest is None
        or changed is None
        or "curation.toml" in changed
        or bool(incremental.plan(changed).full)
    )
    curation, _ = arginf.load_curation(root)
    prov_fn = provenance_fn or default_provenance(root)

    # Which statements to rebuild, and their records.
    if full:
        targets = sorted(existing)
    else:
        assert changed is not None
        new = sorted((set(incremental.plan(changed).statements) | set(also)) & existing)
        targets = list(new)
    records = {h: record_fn(root, h, curation) for h in targets}
    if not full:
        lemmas = sorted({d for r in records.values() for d in r["dependencies"]} & existing - set(records))
        records.update({h: record_fn(root, h, curation) for h in lemmas})
    affected = sorted(records)
    result = Result(full=full, affected=affected)

    # Dependents: from everything (full) or previous ones plus new edges (incremental).
    if full:
        dependents = data.dependents_of(records.values())
    else:
        dependents = {}
        for h in affected:
            old = prev.detail(h)
            dependents[h] = set(old.get("dependents", []) if old else [])
        for h, r in records.items():
            for d in r["dependencies"]:
                if d in dependents:
                    dependents[d].add(h)
        dependents = {h: sorted(v) for h, v in dependents.items()}

    # Readings and areas from the caches; provenance carried forward, looked up only if new.
    allowed = _allowed(root)
    mini = {"theorems": [records[h] for h in affected]}
    readings.embed(mini, read_cache(out / "cache" / "readings", affected), allowed)
    areas.embed(mini, read_cache(out / "cache" / "areas", affected), allowed)
    for h in affected:
        r = records[h]
        old = prev.detail(h)
        carried = {
            str(a["hash"]): {k: a[k] for k in PROV_KEYS if k in a}
            for a in (old or {}).get("attempts", [])
            if a.get("mergedAt")
        }
        missing = [str(a["hash"]) for a in r["attempts"] if str(a["hash"]) not in carried]
        if missing:
            carried.update(prov_fn(h, missing))
        provenance.embed({"theorems": [r]}, carried)

    # Detail files and summary shards.
    area_list = areas.area_list()
    area_index = {a["code"]: i for i, a in enumerate(area_list)}
    files = dict(prev.files) if not full else {}
    retire: set[str] = set()
    shards: dict[str, dict[str, list[Any]]] = {}
    for h in affected:
        r = records[h]
        deps_of = dependents.get(h, [])
        c, dfiles = data.detail_files(r, deps_of)
        for rel, body in dfiles.items():
            _write(out, rel, body, result.written)
        old_row = prev.rows(h[:2]).get(h)
        if old_row is not None:
            old_c = str(old_row[data.SUMMARY_COLS.index("detail")])
            retire |= detail_paths(h, old_c, prev.detail(h)) - set(dfiles)
        shard = shards.setdefault(h[:2], {} if full else dict(prev.rows(h[:2])))
        shard[h] = data.summary_row(r, c, len(deps_of), area_index)
    for name, rows in shards.items():
        body = data.summary_shard(rows.values())
        files[f"summary/{name}"] = rel = data.hashed(f"summary/{name}", body)
        _write(out, rel, body, result.written)
    all_rows = {**prev.all_rows(), **{h: row for s in shards.values() for h, row in s.items()}}
    if full:
        all_rows = {h: row for s in shards.values() for h, row in s.items()}

    # Search: one file while small (rebuilt from every detail), then tokens updated in place.
    mode = "tokens" if prev.manifest and prev.manifest["search"]["mode"] == "tokens" else "single"
    names = {a["code"]: a["name"] for a in area_list}
    if mode == "single" or full:
        everything = [records.get(h) or _detail_at(out, h, row) for h, row in all_rows.items()]
        single = data.search_single(everything)
        for key in [k for k in files if k == "search" or k.startswith("search/")]:
            files.pop(key)
        if len(single) <= data.SEARCH_SINGLE_MAX:
            mode = "single"
            files["search"] = rel = data.hashed("search", single)
            _write(out, rel, single, result.written)
        else:
            mode = "tokens"
            for n, postings in data.search_tokens(everything, names).items():
                body = data.dump(postings)
                files[f"search/{n:02x}"] = rel = data.hashed(f"search/{n:02x}", body)
                _write(out, rel, body, result.written)
    else:
        _update_tokens(out, files, prev, records, names, result.written)

    # Graph overview and counts, from every summary row (Python only, no Lean).
    codes = {a["code"] for a in area_list}
    pseudo = [
        {
            "hash": h,
            "dependencies": row[data.SUMMARY_COLS.index("deps")],
            "areas": {"codes": [area_list[i]["code"] for i in row[data.SUMMARY_COLS.index("areas")] if i < len(area_list)]},
        }
        for h, row in all_rows.items()
    ]
    assert all(set(p["areas"]["codes"]) <= codes for p in pseudo)
    graph = data.graph_areas(pseudo)
    files["graph/areas"] = rel = data.hashed("graph/areas", graph)
    _write(out, rel, graph, result.written)
    for name, body in (extra or {}).items():
        files[name] = rel = data.hashed(name, body)
        _write(out, rel, body, result.written)
    counts = {
        "statements": len(all_rows),
        "attempts": sum(int(r[data.SUMMARY_COLS.index("attempts")]) for r in all_rows.values()),
        "proofs": sum(int(r[data.SUMMARY_COLS.index("proofs")]) for r in all_rows.values()),
    }

    # Retire what the new manifest no longer references (deleted on the next build).
    retire |= set(prev.files.values()) - set(files.values())
    retire -= set(result.written)
    result.retired = sorted(retire)
    _write(out, RETIRED, data.dump(result.retired), [])
    total = _tree_bytes(out)
    body = data.manifest(commit, counts, area_list, files, mode, total)
    _write(out, "manifest.json", body, result.written)
    result.bytes = _tree_bytes(out)
    _record_size(out, result.bytes)
    return result


def _detail_at(out: Path, h: str, row: list[Any]) -> dict[str, Any]:
    path = out / data.detail_path(h, str(row[data.SUMMARY_COLS.index("detail")]))
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _tokens_of(r: Mapping[str, Any] | None, names: Mapping[str, str]) -> set[str]:
    if not r:
        return set()
    return data.tokens(str(r.get("statement", "")), data.first_reading(r), data.area_codes(r), names)


def _update_tokens(
    out: Path,
    files: dict[str, str],
    prev: Previous,
    records: Mapping[str, Mapping[str, Any]],
    names: Mapping[str, str],
    written: list[str],
) -> None:
    edits: dict[int, list[tuple[str, str, bool]]] = defaultdict(list)  # shard -> (token, h, add)
    for h, r in records.items():
        old, new = _tokens_of(prev.detail(h), names), _tokens_of(r, names)
        for t in old - new:
            edits[data.token_shard(t)].append((t, h, False))
        for t in new - old:
            edits[data.token_shard(t)].append((t, h, True))
    for n, changes in edits.items():
        key = f"search/{n:02x}"
        path = files.get(key)
        postings: dict[str, list[str] | None] = (
            json.loads((out / path).read_text(encoding="utf-8")) if path and (out / path).is_file() else {}
        )
        for t, h, add in changes:
            current = postings.get(t, [])
            if current is None:
                continue  # too common to be useful
            hs = set(current)
            (hs.add if add else hs.discard)(h)
            postings[t] = sorted(hs) if len(hs) <= data.MAX_POSTINGS else None
        body = data.dump(postings)
        files[key] = rel = data.hashed(key, body)
        _write(out, rel, body, written)


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _record_size(out: Path, total: int) -> None:
    now = int(time.time())
    history = [e for e in _load_json(out / SIZE_HISTORY, []) if isinstance(e, dict)]
    history = [e for e in history if now - int(e.get("t", 0)) <= HISTORY_KEEP_SECONDS]
    if not history or now - int(history[-1]["t"]) >= 3600:
        history.append({"t": now, "bytes": total})
    else:
        history[-1] = {"t": history[-1]["t"], "bytes": total}
    (out / SIZE_HISTORY).parent.mkdir(parents=True, exist_ok=True)
    (out / SIZE_HISTORY).write_bytes(data.dump(history))


# Git: the data branch -----------------------------------------------------------------------


def _run(args: list[str], cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


BRANCH = "data"


def checkout(remote: str, work: Path, run: Runner = _run) -> None:
    """A shallow clone of the data branch, or an empty orphan branch if it doesn't exist yet."""
    work.parent.mkdir(parents=True, exist_ok=True)
    try:
        run(["git", "clone", "-q", "--depth=1", "--branch", BRANCH, remote, str(work)], work.parent)
    except subprocess.CalledProcessError:
        work.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "-q"], work)
        run(["git", "checkout", "-q", "--orphan", BRANCH], work)


def push(work: Path, remote: str, message: str, run: Runner = _run) -> bool:
    """One commit and one push of everything that changed; nothing if nothing changed."""
    run(["git", "add", "-A"], work)
    if not run(["git", "status", "--porcelain"], work).strip():
        return False
    run(["git", "commit", "-q", "-m", message], work)
    run(["git", "push", "-q", remote, f"HEAD:refs/heads/{BRANCH}"], work)
    return True


def squash(work: Path, remote: str, run: Runner = _run) -> None:
    """Replace the branch's history with one commit of the current tree (keeps the repo small)."""
    run(["git", "checkout", "-q", "--orphan", "squashed"], work)
    run(["git", "add", "-A"], work)
    run(["git", "commit", "-q", "-m", "data snapshot"], work)
    run(["git", "push", "-q", "--force", remote, f"HEAD:refs/heads/{BRANCH}"], work)


def last_publish(work: Path, run: Runner = _run) -> float | None:
    try:
        out = run(["git", "log", "-1", "--format=%ct"], work).strip()
    except subprocess.CalledProcessError:
        return None
    return float(out) if out else None


def wait_seconds(last: float | None, now: float, gap: int = MIN_GAP_SECONDS) -> float:
    return 0.0 if last is None else max(0.0, gap - (now - last))


# CLI ----------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="publish")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("checkout")
    c.add_argument("--remote", required=True)
    c.add_argument("--dir", type=Path, required=True)
    w = sub.add_parser("wait")
    w.add_argument("--dir", type=Path, required=True)
    b = sub.add_parser("build")
    b.add_argument("--dir", type=Path, required=True)
    b.add_argument("--commit", required=True)
    b.add_argument("--full", action="store_true")
    b.add_argument("--also", nargs="*", default=[])
    b.add_argument("--extra", nargs="*", default=[], help="NAME=FILE, e.g. reviews=reviews.json")
    p = sub.add_parser("push")
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--remote", required=True)
    p.add_argument("--message", required=True)
    s = sub.add_parser("squash")
    s.add_argument("--dir", type=Path, required=True)
    s.add_argument("--remote", required=True)
    j = sub.add_parser("cache-join")
    j.add_argument("--dir", type=Path, required=True)
    sp = sub.add_parser("cache-split")
    sp.add_argument("--dir", type=Path, required=True)
    sp.add_argument("file", type=Path)
    args = parser.parse_args(argv)
    root = Path.cwd()
    try:
        if args.cmd == "checkout":
            checkout(args.remote, args.dir)
        elif args.cmd == "wait":
            delay = wait_seconds(last_publish(args.dir), time.time())
            print(f"waiting {delay:.0f} s since the last publish")
            time.sleep(delay)
        elif args.cmd == "build":
            prev = Previous(args.dir)
            changed: list[str] | None = None
            if not args.full and prev.commit:
                try:
                    changed = incremental._changed(prev.commit, args.commit)
                except subprocess.CalledProcessError:
                    changed = None  # the previous commit isn't in history: rebuild everything
            extra: dict[str, bytes] = {}
            for item in args.extra:
                name, _, file = item.partition("=")
                if Path(file).is_file():
                    extra[name] = Path(file).read_bytes()
            r = build(root, args.dir, args.commit, changed, args.also, extra)
            print(
                f"{'full' if r.full else 'incremental'}: {len(r.affected)} statement(s), "
                f"{len(r.written)} file(s) written, {len(r.retired)} retired, {r.bytes} bytes"
            )
        elif args.cmd == "push":
            print("pushed" if push(args.dir, args.remote, args.message) else "nothing to publish")
        elif args.cmd == "squash":
            squash(args.dir, args.remote)
        elif args.cmd == "cache-join":
            json.dump(read_cache(args.dir), sys.stdout, ensure_ascii=False, indent=1)
            print()
        elif args.cmd == "cache-split":
            write_cache(args.dir, json.loads(args.file.read_text(encoding="utf-8")))
    except (subprocess.CalledProcessError, OSError, ValueError, arginf.ToolError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
