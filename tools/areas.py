#!/usr/bin/env python3
"""Machine-assigned areas of statements (Python 3.11+, standard library only).

The pinned reading model (models.toml) picks every applicable field for a statement from the
fixed list AREAS (arXiv subject classes), with the same deterministic decoding as readings.
Replies are filtered against AREAS, so only those fixed labels are ever published:

    python3 -m tools.areas generate --model <name> [<hash> ...]   # writes areas.json
    python3 -m tools.areas embed index.json areas.json            # attaches them to the index
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools import arginf, layout, readings


@dataclass(frozen=True, slots=True)
class Area:
    code: str
    name: str


# arXiv subject classes for the fields arginf statements come from. Changing this list changes
# the prompt digest, so CI relabels every statement on the next merge.
AREAS: tuple[Area, ...] = (
    Area("math.LO", "Logic"),
    Area("math.NT", "Number theory"),
    Area("math.CO", "Combinatorics"),
    Area("math.GR", "Group theory"),
    Area("math.RA", "Rings and algebras"),
    Area("math.AC", "Commutative algebra"),
    Area("math.AG", "Algebraic geometry"),
    Area("math.CT", "Category theory"),
    Area("math.GN", "General topology"),
    Area("math.AT", "Algebraic topology"),
    Area("math.GT", "Geometric topology"),
    Area("math.DG", "Differential geometry"),
    Area("math.MG", "Metric geometry"),
    Area("math.CA", "Classical analysis and ODEs"),
    Area("math.FA", "Functional analysis"),
    Area("math.CV", "Complex variables"),
    Area("math.AP", "Analysis of PDEs"),
    Area("math.DS", "Dynamical systems"),
    Area("math.PR", "Probability"),
    Area("math.ST", "Statistics theory"),
    Area("math.OC", "Optimization and control"),
    Area("math.NA", "Numerical analysis"),
    Area("math.IT", "Information theory"),
    Area("cs.CC", "Computational complexity"),
    Area("cs.CR", "Cryptography and security"),
    Area("cs.DS", "Data structures and algorithms"),
    Area("cs.LO", "Logic in computer science"),
    Area("cs.FL", "Formal languages and automata theory"),
    Area("cs.PL", "Programming languages"),
    Area("cs.DM", "Discrete mathematics"),
)
MAX_AREAS = 4
MAX_TOKENS = 48  # a handful of codes; anything longer is noise
PROMPT = (
    "Classify the following Lean statement by mathematical area. From the list below, choose "
    f"every area the statement belongs to, at most {MAX_AREAS}, most relevant first. Reply "
    "with only the chosen codes, separated by commas, and nothing else.\n\n{areas}\n```\n{lean}\n```"
)
_CODE_RE = re.compile(r"(?<![\w.])(?:math|cs)\.[A-Za-z]{2}(?![\w])")
PUBLISHED = ("model", "modelSha256", "runtime", "cpu", "promptSha256")


def area_list(areas: tuple[Area, ...] = AREAS) -> list[dict[str, str]]:
    return [{"code": a.code, "name": a.name} for a in areas]


def user_message(root: Path, h: str, areas: tuple[Area, ...] = AREAS) -> str:
    listing = "\n".join(f"{a.code}: {a.name}" for a in areas)
    # Not str.format: Lean may contain braces.
    return PROMPT.replace("{areas}", listing).replace("{lean}", readings.statement_lean(root, h))


def parse_codes(reply: str, areas: tuple[Area, ...] = AREAS) -> tuple[str, ...]:
    """Known codes from a model reply, in reply order, deduplicated, capped at MAX_AREAS."""
    known = {a.code for a in areas}
    out: list[str] = []
    for m in _CODE_RE.finditer(reply):
        code = m.group(0)
        if code in known and code not in out:
            out.append(code)
    return tuple(out[:MAX_AREAS])


def missing(
    hashes: list[str], existing: list[dict[str, Any]], model_sha: str, prompts: dict[str, str]
) -> list[str]:
    """Statements needing labels: new, or labelled with another model, prompt or area list."""
    done = {
        t["hash"]
        for t in existing
        if t.get("modelSha256") == model_sha and t.get("promptSha256") == prompts.get(t["hash"])
    }
    return [h for h in hashes if h not in done]


def merge(existing: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    key = {(t["hash"], t["modelSha256"]): t for t in existing}
    for t in new:
        key[(t["hash"], t["modelSha256"])] = t
    return sorted(key.values(), key=lambda t: (t["hash"], t["modelSha256"]))


def embed(index: dict[str, Any], tags: list[dict[str, Any]], allowed: set[str]) -> dict[str, Any]:
    """Attach the area list and each statement's labels (first allowlisted model wins).

    Codes are re-checked against AREAS here too, so a hand-edited areas.json cannot publish
    anything but the fixed labels.
    """
    known = {a.code for a in AREAS}
    by_hash: dict[str, dict[str, Any]] = {}
    for t in tags:
        if t.get("modelSha256") not in allowed or t["hash"] in by_hash:
            continue
        codes = [c for c in t.get("areas", []) if isinstance(c, str) and c in known]
        by_hash[t["hash"]] = {"codes": codes[:MAX_AREAS]} | {k: t[k] for k in PUBLISHED if k in t}
    index["areaList"] = area_list()
    for th in index["theorems"]:
        th["areas"] = by_hash.get(th["hash"])
    return index


def generate(
    root: Path,
    allow: readings.Allowlist,
    model: readings.Model,
    hashes: list[str],
    cache: Path,
) -> list[dict[str, Any]]:
    engine = readings.prepare(allow, model, cache)
    out: list[dict[str, Any]] = []
    for h in hashes:
        message = user_message(root, h)
        run = readings.complete(engine, f"areas-{h}", message, MAX_TOKENS)
        raw = readings.clean_output(run.stdout)
        out.append(
            {
                "hash": h,
                "areas": list(parse_codes(raw)),
                "raw": raw,
                "model": model.name,
                "modelSha256": model.sha256,
                "runtime": f"{allow.runtime.name} {allow.runtime.version}",
                "cpu": readings._cpu(),
                "promptSha256": readings.digest(message),
                "seconds": run.seconds,
            }
        )
    return out


def _load_json_list(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="areas")
    sub = parser.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--model", required=True)
    g.add_argument("--cache", type=Path, default=Path(".cache/readings"))
    g.add_argument("--out", type=Path, default=Path("areas.json"))
    g.add_argument("--existing", type=Path, default=None, help="areas.json to extend")
    g.add_argument("hashes", nargs="*")
    e = sub.add_parser("embed", help="attach areas.json to index.json in place")
    e.add_argument("index", type=Path)
    e.add_argument("areas", type=Path)
    args = parser.parse_args(argv)
    root = Path.cwd()
    try:
        allow = readings.load_models(root / "models.toml")
        allowed = {m.sha256 for m in allow.models}
        if args.cmd == "embed":
            tags = readings.prune(_load_json_list(args.areas), allowed)
            args.areas.write_text(json.dumps(tags, ensure_ascii=False, indent=1) + "\n")
            idx = json.loads(args.index.read_text(encoding="utf-8"))
            args.index.write_text(
                json.dumps(embed(idx, tags, allowed), ensure_ascii=False, indent=1) + "\n"
            )
            return 0
        model = allow.model(args.model)
        hashes = args.hashes or sorted(
            p.name for p in layout.statement_dirs(root) if arginf.HASH_RE.match(p.name)
        )
        existing = _load_json_list(args.existing)
        prompts = {h: readings.digest(user_message(root, h)) for h in hashes}
        todo = missing(hashes, existing, model.sha256, prompts)
        existing = readings.prune(existing, allowed)
        result = merge(existing, generate(root, allow, model, todo, args.cache) if todo else [])
    except (readings.ReadingError, OSError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(todo)} new label set(s), {len(result)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
