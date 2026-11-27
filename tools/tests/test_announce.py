"""The one status comment per PR: "accepted, queued" when queued, then badges once merged."""

import unittest

from tools import announce, layout, ledger

S = "1" * layout.HASH_LEN
S2 = "2" * layout.HASH_LEN
A1 = "a1" + "0" * (layout.HASH_LEN - 2)
A2 = "a2" + "0" * (layout.HASH_LEN - 2)
A3 = "a3" + "0" * (layout.HASH_LEN - 2)


def e(pr: int, who: str, kind: str, h: str, a: str | None, t: str) -> ledger.Entry:
    return ledger.Entry(pr, who, kind, h, a, t)


ENTRIES = [
    e(1, "alice", "statement", S, None, "2026-11-27T00:00:00Z"),
    e(2, "bob", "proof", S, A1, "2026-11-27T01:00:00Z"),
    e(3, "carol", "proof", S, A2, "2026-11-27T02:00:00Z"),
    e(4, "dan", "checkpoint", S, A3, "2026-11-27T03:00:00Z"),
    e(5, "alice", "statement", S2, None, "2026-11-27T04:00:00Z"),
]
INFO = {A1: (40, 0), A2: (25, 0), A3: (30, 2)}  # attempt -> (size, open goals)


class BodyTests(unittest.TestCase):
    def test_queued_body_says_accepted_and_nothing_about_badges(self) -> None:
        for kind in ledger.KINDS:
            body = announce.queued_body(kind, 7, S, A1 if kind != "statement" else None)
            self.assertIn(announce.pr_url(7), body)
            self.assertIn(announce.MARKER, body)
            self.assertIn("queued", body.lower())
            for word in ("first", "shortest", "badge"):
                self.assertNotIn(word, body.lower())
            self.assertTrue(body.endswith(announce.FOOTER))

    def test_merged_bodies_and_badges(self) -> None:
        first = announce.merged_body(ENTRIES[1], ENTRIES, INFO)
        self.assertIn("Verified", first)
        self.assertIn(announce.pr_url(2), first)
        self.assertIn(announce.FIRST, first)
        self.assertNotIn(announce.SHORTEST, first)  # A2 is shorter
        self.assertIn(announce.CREDIT, first)  # first proof of someone else's statement
        self.assertIn(announce.statement_url(S), first)
        second = announce.merged_body(ENTRIES[2], ENTRIES, INFO)
        self.assertIn(announce.SHORTEST, second)
        self.assertNotIn(announce.FIRST, second)
        self.assertNotIn(announce.CREDIT, second)
        cp = announce.merged_body(ENTRIES[3], ENTRIES, INFO)
        self.assertIn("2 open goals", cp)
        self.assertNotIn("Verified", cp)  # a checkpoint is not a proof
        st = announce.merged_body(ENTRIES[4], ENTRIES, INFO)
        self.assertIn(announce.statement_url(S2), st)
        for body in (first, second, cp, st):
            self.assertIn(announce.MARKER, body)
            self.assertTrue(body.endswith(announce.FOOTER))

    def test_own_statement_earns_no_credit(self) -> None:
        own = [
            e(1, "bob", "statement", S, None, "2026-11-27T00:00:00Z"),
            e(2, "bob", "proof", S, A1, "2026-11-27T01:00:00Z"),
        ]
        body = announce.merged_body(own[1], own, INFO)
        self.assertIn(announce.FIRST, body)
        self.assertNotIn(announce.CREDIT, body)

    def test_shortest_ties_go_to_the_earlier_merge(self) -> None:
        info = {A1: (25, 0), A2: (25, 0)}
        self.assertIn(announce.SHORTEST, announce.merged_body(ENTRIES[1], ENTRIES, info))
        self.assertNotIn(announce.SHORTEST, announce.merged_body(ENTRIES[2], ENTRIES, info))


class SelectionTests(unittest.TestCase):
    def test_only_new_recent_entries_are_announced(self) -> None:
        previous = ENTRIES[:2]
        now = "2026-11-27T04:30:00Z"
        got = announce.to_announce(previous, ENTRIES, now, window_hours=2)
        self.assertEqual([x.pr for x in got], [4, 5])  # 3 is new but older than 2 h
        # A rebuilt ledger (no previous) announces only the window, never all history.
        self.assertEqual([x.pr for x in announce.to_announce([], ENTRIES, now, 2)], [4, 5])


class UpsertTests(unittest.TestCase):
    def comments(self) -> list[dict[str, object]]:
        return [
            {"id": 1, "body": f"x {announce.MARKER}", "user": {"login": "mallory", "type": "User"}},
            {
                "id": 2,
                "body": f"old {announce.MARKER}",
                "user": {"login": "github-actions[bot]", "type": "Bot"},
            },
        ]

    def test_updates_our_own_comment_only(self) -> None:
        self.assertEqual(announce.find_comment(self.comments()), 2)
        self.assertIsNone(announce.find_comment(self.comments()[:1]))  # a user's copy of the marker

    def test_upsert_patches_or_posts(self) -> None:
        calls: list[tuple[str, str, object]] = []

        def api(method: str, url: str, body: object = None) -> object:
            calls.append((method, url, body))
            return self.comments() if method == "GET" else {}

        base = "https://api.github.com/repos/o/r"
        announce.upsert(api, base, 7, "hello")
        self.assertEqual(calls[-1], ("PATCH", f"{base}/issues/comments/2", {"body": "hello"}))
        calls.clear()

        def api2(method: str, url: str, body: object = None) -> object:
            calls.append((method, url, body))
            return [] if method == "GET" else {}

        announce.upsert(api2, base, 7, "hello")
        self.assertEqual(calls[-1], ("POST", f"{base}/issues/7/comments", {"body": "hello"}))


class PublishTests(unittest.TestCase):
    base = "https://api.github.com/repos/o/r"
    h = "ab" * 10

    def run_publish(self, body: str, login: str = "github-actions[bot]") -> list[tuple[str, str, object]]:
        calls: list[tuple[str, str, object]] = []
        comments = [{"id": 5, "body": body, "user": {"login": login, "type": "Bot"}}]

        def api(method: str, url: str, b: object = None) -> object:
            calls.append((method, url, b))
            return comments if method == "GET" else {}

        announce.publish(api, self.base, 7)
        return calls

    def test_merged_becomes_published_with_text_unchanged(self) -> None:
        body = announce.queued_body("statement", 7, self.h, None, stage="merged")
        patches = [c for c in self.run_publish(body) if c[0] == "PATCH"]
        self.assertEqual(len(patches), 1)
        new = str(patches[0][2]["body"])  # type: ignore[index]
        stage = announce.parse_stage(new)
        assert stage is not None
        self.assertEqual((stage["stage"], stage["pr"], stage["hash"]), ("published", 7, self.h))
        strip = announce._STAGE_RE.sub("", new)
        self.assertEqual(strip, announce._STAGE_RE.sub("", body))

    def test_only_moves_forward_from_merged(self) -> None:
        for stage in ("checks", "failed", "held", "accepted", "queued", "published"):
            body = announce.queued_body("statement", 7, self.h, None, stage=stage)
            self.assertFalse([c for c in self.run_publish(body) if c[0] != "GET"], stage)

    def test_never_touches_a_users_comment_or_posts_one(self) -> None:
        body = announce.queued_body("statement", 7, self.h, None, stage="merged")
        self.assertFalse([c for c in self.run_publish(body, "mallory") if c[0] != "GET"])

    def test_cli_rejects_bad_pr_numbers(self) -> None:
        for bad in (["published"], ["published", "0"], ["published", "7x"], ["nope", "7"]):
            self.assertEqual(announce.main(bad), 2, bad)


class LedgerGlueTests(unittest.TestCase):
    def test_build_announce_updates_only_new_merges(self) -> None:
        import tempfile
        from datetime import UTC, datetime
        from pathlib import Path
        from unittest import mock

        root = Path(tempfile.mkdtemp())
        (root / "Theorems").mkdir()
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        old = e(1, "alice", "statement", S, None, now)
        new = e(2, "bob", "proof", S, A1, now)
        calls: list[tuple[str, str]] = []

        def call(method: str, url: str, token: str, body: object = None) -> object:
            calls.append((method, url))
            return [] if method == "GET" else {}

        with mock.patch.object(ledger, "_call", side_effect=call):
            ledger._announce(root, "o/r", "t", [old], [old, new])
        self.assertEqual(
            calls,
            [
                ("GET", "https://api.github.com/repos/o/r/issues/2/comments?per_page=100&page=1"),
                ("POST", "https://api.github.com/repos/o/r/issues/2/comments"),
            ],
        )


class StageTests(unittest.TestCase):
    """A hidden machine-readable line the live /pr/<n> page polls."""

    def test_every_body_carries_one_stage_line(self) -> None:
        bodies = {
            "checks": announce.checks_body(7),
            "failed": announce.failed_body(7),
            "held": announce.held_body(7),
            "queued": announce.queued_body("proof", 7, S, A1),
            "merged": announce.merged_body(ENTRIES[1], ENTRIES, INFO),
        }
        for stage, body in bodies.items():
            with self.subTest(stage=stage):
                got = announce.parse_stage(body)
                assert got is not None
                self.assertEqual(got["v"], 1)
                self.assertEqual(got["stage"], stage)
                self.assertRegex(str(got["at"]), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
                self.assertEqual(body.count("arginf-stage:"), 1)
                self.assertTrue(body.startswith(announce.MARKER))
        merged = announce.parse_stage(bodies["merged"])
        assert merged is not None
        self.assertEqual((merged["pr"], merged["hash"], merged["attempt"]), (2, S, A1))
        queued = announce.parse_stage(bodies["queued"])
        assert queued is not None
        self.assertEqual((queued["hash"], queued["attempt"]), (S, A1))

    def test_stage_line_is_strict(self) -> None:
        with self.assertRaises(ValueError):
            announce.stage_line("other", 1)
        with self.assertRaises(ValueError):
            announce.stage_line("queued", 1, h="not hex")
        with self.assertRaises(ValueError):
            announce.stage_line("queued", 0)
        self.assertIsNone(announce.parse_stage("no stage here"))
        self.assertIsNone(announce.parse_stage("<!-- arginf-stage: {bad json} -->"))

    def test_with_stage_rewrites_only_the_stage(self) -> None:
        body = announce.merged_body(ENTRIES[1], ENTRIES, INFO)
        pub = announce.with_stage(body, "published")
        got = announce.parse_stage(pub)
        assert got is not None
        self.assertEqual((got["stage"], got["pr"], got["hash"]), ("published", 2, S))
        self.assertEqual(pub.split("-->", 2)[2], body.split("-->", 2)[2])  # visible text unchanged

    def test_no_pr_text_in_stage_or_bodies(self) -> None:
        # Only fixed strings, numbers and validated hashes ever reach a comment.
        for body in (announce.checks_body(7), announce.held_body(7), announce.failed_body(7)):
            self.assertNotIn("Theorems/", body)


if __name__ == "__main__":
    unittest.main()
