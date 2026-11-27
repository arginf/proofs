#!/usr/bin/env python3
"""Size and runway guards for the published data (Python 3.11+, standard library only).

GitHub Pages refuses to publish a site over 1 GB. Each publish records the tree's size in
meta/size-history.json; this reads it and says how close we are, so the move to S3 + CloudFront
(infra/data-host in arginf/arginf, see its runbook) starts long before the cap:

    plan     at 40% of the cap, or under 180 days of runway at the last 30 days' growth
    urgent   at 60%, or under 60 days

    python3 -m tools.guard check --dir data [--output $GITHUB_OUTPUT]

It only reports: the publish workflow opens or updates an issue and turns a `data-size` check
red, but publishing never blocks on it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CAP_BYTES = 1_000_000_000
PLAN_FRACTION, URGENT_FRACTION = 0.40, 0.60
PLAN_RUNWAY_DAYS, URGENT_RUNWAY_DAYS = 180, 60
WINDOW_DAYS = 30
DAY = 86400


@dataclass(frozen=True, slots=True)
class Verdict:
    level: str  # ok | plan | urgent
    bytes: int
    fraction: float
    runway_days: float | None
    reasons: list[str] = field(default_factory=list)


def evaluate(history: Sequence[dict[str, Any]], now: float, cap: int = CAP_BYTES) -> Verdict:
    points = sorted((int(e["t"]), int(e["bytes"])) for e in history if "t" in e and "bytes" in e)
    if not points:
        return Verdict("ok", 0, 0.0, None)
    latest_t, latest = points[-1]
    window = [p for p in points if p[0] >= now - WINDOW_DAYS * DAY] or points
    first_t, first = window[0]
    runway: float | None = None
    span_days = (latest_t - first_t) / DAY
    if span_days >= 1 and latest > first:
        rate = (latest - first) / span_days
        runway = max(0.0, (cap - latest) / rate)
    fraction = latest / cap
    reasons: list[str] = []
    level = "ok"
    if fraction >= URGENT_FRACTION or (runway is not None and runway < URGENT_RUNWAY_DAYS):
        level = "urgent"
    elif fraction >= PLAN_FRACTION or (runway is not None and runway < PLAN_RUNWAY_DAYS):
        level = "plan"
    reasons.append(f"{latest / 1e6:.0f} MB published, {fraction:.0%} of the 1 GB Pages cap")
    if runway is not None:
        reasons.append(
            f"about {runway:.0f} days until the cap at the last {WINDOW_DAYS} days' growth"
        )
    return Verdict(level, latest, fraction, runway, reasons)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="guard")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--dir", type=Path, required=True)
    c.add_argument("--output", type=Path, default=None, help="append key=value lines here")
    c.add_argument("--now", type=float, default=None)
    args = parser.parse_args(argv)
    try:
        history = json.loads((args.dir / "meta" / "size-history.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        history = []
    v = evaluate(history if isinstance(history, list) else [], args.now or time.time())
    summary = "; ".join(v.reasons)
    print(f"{v.level}: {summary}")
    if args.output:
        with args.output.open("a", encoding="utf-8") as f:
            f.write(f"level={v.level}\npercent={round(v.fraction * 100)}\n")
            f.write(f"runway={'' if v.runway_days is None else round(v.runway_days)}\n")
            f.write(f"summary={summary}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
