"""Size and runway guards; reviews.json; the audit cache as shards in the data tree.

User stories:
- As the owner, I hear about Pages' 1 GB cap early: at 40% or under 180 days of runway an issue
  opens, at 60% or under 60 days it is urgent and a check turns red; publishing never blocks.
- As a visitor, "Under review" costs me no GitHub API calls: CI lists open PRs whose scope and
  lean checks passed, as "#n by @login", never with titles.
"""

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tools import guard, incremental, reviews

DAY = 86400
GB = 1_000_000_000


class GuardTests(unittest.TestCase):
    def history(self, points: list[tuple[float, float]], now: float) -> list[dict[str, int]]:
        """(days ago, GB) -> size-history entries."""
        return [{"t": int(now - d * DAY), "bytes": int(gb * GB)} for d, gb in points]

    def test_small_and_slow_is_ok(self) -> None:
        now = 1_800_000_000.0
        v = guard.evaluate(self.history([(30, 0.10), (0, 0.11)], now), now)
        self.assertEqual(v.level, "ok")
        self.assertGreater(v.runway_days or 0, 180)

    def test_40_percent_or_180_days_plans_the_move(self) -> None:
        now = 1_800_000_000.0
        self.assertEqual(guard.evaluate(self.history([(0, 0.41)], now), now).level, "plan")
        # 0.30 GB, growing 0.1 GB / 30 days -> 210 days to 1 GB: ok; 0.15 GB / 30 days -> 140: plan.
        self.assertEqual(guard.evaluate(self.history([(30, 0.20), (0, 0.30)], now), now).level, "ok")
        self.assertEqual(guard.evaluate(self.history([(30, 0.15), (0, 0.30)], now), now).level, "plan")

    def test_60_percent_or_60_days_is_urgent(self) -> None:
        now = 1_800_000_000.0
        self.assertEqual(guard.evaluate(self.history([(0, 0.61)], now), now).level, "urgent")
        self.assertEqual(guard.evaluate(self.history([(30, 0.10), (0, 0.50)], now), now).level, "urgent")

    def test_runway_needs_a_day_of_history_and_growth(self) -> None:
        now = 1_800_000_000.0
        self.assertIsNone(guard.evaluate(self.history([(0, 0.1)], now), now).runway_days)
        self.assertIsNone(guard.evaluate(self.history([(10, 0.3), (0, 0.2)], now), now).runway_days)

    def test_check_command_reports_but_never_fails_the_publish(self) -> None:
        d = Path(tempfile.mkdtemp())
        (d / "meta").mkdir()
        now = 1_800_000_000
        (d / "meta" / "size-history.json").write_text(json.dumps([{"t": now, "bytes": int(0.7 * GB)}]))
        out = d / "github_output"
        code = guard.main(["check", "--dir", str(d), "--output", str(out), "--now", str(now)])
        self.assertEqual(code, 0)
        text = out.read_text()
        self.assertIn("level=urgent", text)
        self.assertIn("percent=70", text)


def pull(n: int, ref: str, sha: str, login: str = "alice", **extra: Any) -> dict[str, Any]:
    return {
        "number": n,
        "title": "IGNORED <script>",
        "html_url": f"https://github.com/arginf/proofs/pull/{n}",
        "head": {"ref": ref, "sha": sha},
        "user": {"login": login},
        "draft": False,
        **extra,
    }


def runs(scope: str, lean: str) -> dict[str, Any]:
    return {
        "check_runs": [
            {"name": "scope", "status": "completed", "conclusion": scope},
            {"name": "lean", "status": "completed", "conclusion": lean},
        ]
    }


H = "0123456789abcdef0123"
A = "fedcba98765432100123"


class ReviewsTests(unittest.TestCase):
    def fetch(self, pulls: list[dict[str, Any]], checks: dict[str, Any]) -> Any:
        def get(path: str) -> Any:
            if path.startswith("/pulls"):
                return pulls if path.endswith("page=1") else []
            sha = path.split("/commits/")[1].split("/")[0]
            return checks.get(sha, {"check_runs": []})

        return get

    def test_lists_only_reviewed_theorem_prs_without_titles(self) -> None:
        pulls = [
            pull(7, f"{H}/{A}", "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"),
            pull(8, f"{H}/x", "b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2"),  # scope ok, lean failed
            pull(9, "infra/thing", "c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3"),  # not a theorem branch
            pull(10, f"{H}/y", "d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4", draft=True),
            pull(11, "0123456789ab/old", "e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5"),  # 12-digit branch: not a current hash
        ]
        checks = {"a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1": runs("success", "success"), "b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2": runs("success", "failure"), "d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4": runs("success", "success"), "e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5": runs("success", "success")}
        out = reviews.build(self.fetch(pulls, checks))
        self.assertEqual(
            out["pulls"],
            [
                {"number": 7, "author": "alice", "theorem": H, "attempt": A, "draft": False},
                {"number": 10, "author": "alice", "theorem": H, "attempt": None, "draft": True},
            ],
        )
        self.assertNotIn("IGNORED", json.dumps(out))

    def test_rejects_hostile_values(self) -> None:
        bad = [
            pull(1, f"{H}/x", "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1", login="<b>x</b>"),
            {**pull(2, f"{H}/x", "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"), "html_url": "javascript:alert(1)"},
            {**pull(3, f"{H}/x", "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"), "number": "3"},
        ]
        out = reviews.build(self.fetch(bad, {"a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1": runs("success", "success")}))
        self.assertEqual(out["pulls"], [])


class AuditCacheDirTests(unittest.TestCase):
    def test_cache_saves_and_loads_as_shards_in_the_data_tree(self) -> None:
        d = Path(tempfile.mkdtemp()) / "cache" / "audit"
        d.mkdir(parents=True)
        c = incremental.AuditCache()
        c.add("ab" + "0" * 18, "cd" + "1" * 18, "e1")
        c.add("ab" + "0" * 18, "ef" + "2" * 18, "e1")
        c.add_built("ab" + "0" * 18, "e1")
        c.save(d)
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["ab.json", "cd.json", "ef.json"])
        back = incremental.AuditCache.load(d)
        self.assertTrue(back.done("cd" + "1" * 18, "e1") and back.built("ab" + "0" * 18, "e1"))
        self.assertEqual(back.size(), 3)


if __name__ == "__main__":
    unittest.main()
