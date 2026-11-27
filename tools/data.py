"""The published data tree that arginf.com reads (Python 3.11+, standard library only).

Small, content-addressed files instead of one index.json, so every file stays under GitHub's
1 MB recommendation and every directory under 3,000 entries at 100k statements, and a page
downloads only what it shows:

    manifest.json                          version, commit, counts, area list, file names, bytes
    summary/<ab>.<c>.json                  one row per statement in shard <ab> (cols + rows)
    statements/<ab>/<hash>.<c>.json        everything about one statement
    statements/<ab>/<hash>.lean-<n>.<c>.json   Lean bodies of a statement too big for one file
    search.<c>.json                        while small: one row per statement
    search/<xx>.<c>.json                   at scale: token -> statement hashes, xx = fnv1a % 256
    graph/areas.<c>.json                   statements per area and dependencies between areas
    reviews.<c>.json, ledger.<c>.json      open reviewed PRs; who merged what (from CI)

<c> is the first 10 hex digits of the file's SHA-256, so every name but manifest.json can be
cached forever. A summary row carries its detail file's <c>; the manifest lists every other
file. Everything here is data: the site renders it as text.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

VERSION = 3
SUMMARY_TEXT = 160  # characters of the statement shown in lists; the detail file has it all
SUMMARY_COLS = (
    "hash", "status", "statement", "attempts", "proofs", "areas", "deps", "dependents",
    "flags", "detail",
)  # fmt: skip
DETAIL_MAX = 512 * 1024  # beyond this, Lean bodies move to chunk files
CHUNK_MAX = 900 * 1024
SEARCH_SINGLE_MAX = 512 * 1024  # one search file up to this size, then sharded tokens
SEARCH_SHARDS = 256
MAX_POSTINGS = 5000  # a token in more statements than this is too common to help: stored as null
FLAG_FEATURED, FLAG_EXAMPLE = 1, 2

type Record = Mapping[str, Any]
type Files = dict[str, bytes]


def dump(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def content_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()[:10]


def detail_path(h: str, c: str) -> str:
    return f"statements/{h[:2]}/{h}.{c}.json"


def chunk_path(h: str, chunk: Mapping[str, Any]) -> str:
    return f"statements/{h[:2]}/{h}.lean-{chunk['n']}.{chunk['c']}.json"


def fnv1a(text: str) -> int:
    """32-bit FNV-1a over UTF-8; the site computes the same in TypeScript."""
    value = 0x811C9DC5
    for byte in text.encode():
        value = ((value ^ byte) * 0x01000193) & 0xFFFFFFFF
    return value


def token_shard(token: str) -> int:
    return fnv1a(token) % SEARCH_SHARDS


_WORD_RE = re.compile(r"[^\W_]+")


def tokens(
    statement: str, reading: str, codes: Iterable[str], names: Mapping[str, str]
) -> set[str]:
    """Lowercase search tokens: words of the statement and reading, area codes and names."""
    out = {w for w in _WORD_RE.findall(f"{statement} {reading}".lower()) if len(w) > 1}
    for code in codes:
        out.add(code.lower())
        out |= {w for w in _WORD_RE.findall(names.get(code, "").lower()) if len(w) > 1}
    return out


def first_reading(r: Record) -> str:
    readings = r.get("readings") or []
    return str(readings[0].get("text", "")) if readings else ""


def area_codes(r: Record) -> list[str]:
    areas = r.get("areas")
    return [str(c) for c in areas.get("codes", [])] if isinstance(areas, Mapping) else []


def dependents_of(records: Iterable[Record]) -> dict[str, list[str]]:
    known = {str(r["hash"]) for r in records if True}
    out: dict[str, set[str]] = defaultdict(set)
    for r in records:
        for d in r.get("dependencies", []):
            if d in known and d != r["hash"]:
                out[str(d)].add(str(r["hash"]))
    return {k: sorted(v) for k, v in out.items()}


# Detail files ------------------------------------------------------------------------------


def detail_files(r: Record, dependents: Sequence[str]) -> tuple[str, Files]:
    """The statement's detail file (and Lean chunk files if it is large). Returns its <c>."""
    h = str(r["hash"])
    rec = {**r, "dependents": list(dependents)}
    body = dump(rec)
    files: Files = {}
    if len(body) > DETAIL_MAX:
        rec, files = _split_lean(h, rec)
        body = dump(rec)
    c = content_hash(body)
    files[detail_path(h, c)] = body
    return c, files


def _split_lean(h: str, rec: dict[str, Any]) -> tuple[dict[str, Any], Files]:
    attempts = [dict(a) for a in rec.get("attempts", [])]
    chunks: list[dict[str, str]] = [{}]
    size = 0
    for a in attempts:
        lean = str(a.pop("lean", ""))
        if chunks[-1] and size + len(lean) > CHUNK_MAX:
            chunks.append({})
            size = 0
        chunks[-1][str(a["hash"])] = lean
        size += len(lean.encode())
        a["leanChunk"] = len(chunks)
    files: Files = {}
    refs: list[dict[str, Any]] = []
    for n, chunk in enumerate(chunks, 1):
        body = dump(chunk)
        ref = {"n": n, "c": content_hash(body)}
        files[chunk_path(h, ref)] = body
        refs.append(ref)
    return {**rec, "attempts": attempts, "leanChunks": refs}, files


# Summary shards ----------------------------------------------------------------------------


def summary_row(
    r: Record, detail_c: str, dependents: int, area_index: Mapping[str, int]
) -> list[Any]:
    text = str(r.get("statement", ""))
    if len(text) > SUMMARY_TEXT:
        text = text[: SUMMARY_TEXT - 1] + "…"
    flags = (FLAG_FEATURED if r.get("featured") else 0) | (FLAG_EXAMPLE if r.get("example") else 0)
    return [
        str(r["hash"]),
        str(r.get("status", "open")),
        text,
        len(r.get("attempts", [])),
        len(r.get("proofs", [])),
        [area_index[c] for c in area_codes(r) if c in area_index],
        sorted(str(d) for d in r.get("dependencies", [])),
        dependents,
        flags,
        detail_c,
    ]


def summary_shard(rows: Iterable[Sequence[Any]]) -> bytes:
    return dump({"cols": list(SUMMARY_COLS), "rows": sorted(rows, key=lambda row: row[0])})


def hashed(name: str, body: bytes) -> str:
    """`summary/ab` + body -> `summary/ab.<c>.json`."""
    return f"{name}.{content_hash(body)}.json"


# Search ------------------------------------------------------------------------------------


def search_single(records: Iterable[Record]) -> bytes:
    rows = [
        [str(r["hash"]), str(r.get("statement", "")), first_reading(r)[:200], area_codes(r)]
        for r in sorted(records, key=lambda r: str(r["hash"]))
    ]
    return dump({"cols": ["hash", "statement", "reading", "areas"], "rows": rows})


def search_tokens(
    records: Iterable[Record], names: Mapping[str, str]
) -> dict[int, dict[str, list[str] | None]]:
    postings: dict[str, list[str]] = defaultdict(list)
    for r in records:
        for t in tokens(str(r.get("statement", "")), first_reading(r), area_codes(r), names):
            postings[t].append(str(r["hash"]))
    shards: dict[int, dict[str, list[str] | None]] = defaultdict(dict)
    for t, hs in postings.items():
        shards[token_shard(t)][t] = sorted(hs) if len(hs) <= MAX_POSTINGS else None
    return shards


# Graph overview ----------------------------------------------------------------------------


def graph_areas(records: Sequence[Record]) -> bytes:
    codes = {str(r["hash"]): area_codes(r) for r in records}
    counts: Counter[str] = Counter(c for cs in codes.values() for c in cs)
    edges: Counter[tuple[str, str]] = Counter()
    for r in records:
        for d in r.get("dependencies", []):
            for a in codes.get(str(r["hash"]), []):
                for b in codes.get(str(d), []):
                    edges[(a, b)] += 1
    return dump(
        {
            "areas": [{"code": c, "count": n} for c, n in sorted(counts.items())],
            "edges": [[a, b, n] for (a, b), n in sorted(edges.items())],
        }
    )


# Manifest ----------------------------------------------------------------------------------


def manifest(
    commit: str,
    counts: Mapping[str, int],
    area_list: Sequence[Mapping[str, str]],
    files: Mapping[str, str],
    search_mode: str,
    total_bytes: int,
) -> bytes:
    return dump(
        {
            "version": VERSION,
            "commit": commit,
            "counts": dict(counts),
            "areaList": [dict(a) for a in area_list],
            "search": {"mode": search_mode, "shards": SEARCH_SHARDS}
            if search_mode == "tokens"
            else {"mode": search_mode},
            "files": dict(sorted(files.items())),
            "bytes": total_bytes,
        }
    )


def render(
    records: Iterable[Record],
    area_list: Sequence[Mapping[str, str]],
    commit: str,
    extra: Mapping[str, bytes] | None = None,
) -> Files:
    """The whole tree from scratch (first publish, tests). Publishing reuses the pieces."""
    recs = sorted(records, key=lambda r: str(r["hash"]))
    area_index = {str(a["code"]): i for i, a in enumerate(area_list)}
    names = {str(a["code"]): str(a["name"]) for a in area_list}
    deps_of = dependents_of(recs)
    out: Files = {}
    listed: dict[str, str] = {}
    shards: dict[str, list[list[Any]]] = defaultdict(list)
    for r in recs:
        h = str(r["hash"])
        dependents = deps_of.get(h, [])
        c, files = detail_files(r, dependents)
        out.update(files)
        shards[h[:2]].append(summary_row(r, c, len(dependents), area_index))
    for shard, rows in shards.items():
        body = summary_shard(rows)
        listed[f"summary/{shard}"] = path = hashed(f"summary/{shard}", body)
        out[path] = body
    single = search_single(recs)
    if len(single) <= SEARCH_SINGLE_MAX:
        mode = "single"
        listed["search"] = path = hashed("search", single)
        out[path] = single
    else:
        mode = "tokens"
        for n, shard_postings in search_tokens(recs, names).items():
            body = dump(shard_postings)
            listed[f"search/{n:02x}"] = path = hashed(f"search/{n:02x}", body)
            out[path] = body
    graph = graph_areas(recs)
    listed["graph/areas"] = path = hashed("graph/areas", graph)
    out[path] = graph
    for name, body in (extra or {}).items():
        listed[name] = path = hashed(name, body)
        out[path] = body
    counts = {
        "statements": len(recs),
        "attempts": sum(len(r.get("attempts", [])) for r in recs),
        "proofs": sum(len(r.get("proofs", [])) for r in recs),
    }
    total = sum(len(b) for b in out.values())
    out["manifest.json"] = manifest(commit, counts, area_list, listed, mode, total)
    return out
