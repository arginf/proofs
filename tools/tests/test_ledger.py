"""The merge ledger: who merged what, from GitHub's own records, never from commit authors."""

import json
import unittest

from tools import layout, ledger

S1 = "1" * layout.HASH_LEN
S2 = "2" * layout.HASH_LEN
S3 = "3" * layout.HASH_LEN
A1 = "a1" + "0" * (layout.HASH_LEN - 2)
A2 = "a2" + "0" * (layout.HASH_LEN - 2)
A3 = "a3" + "0" * (layout.HASH_LEN - 2)


def attempt_files(h: str, a: str) -> list[str]:
    return [layout.attempt_path(h, a), layout.attempt_path(h, a, "attempt.toml")]


def e(pr: int, who: str, kind: str, h: str, a: str | None, t: str) -> ledger.Entry:
    return ledger.Entry(pr=pr, author=who, kind=kind, statement=h, attempt=a, merged_at=t)


class LabelTests(unittest.TestCase):
    def test_one_kind_label(self) -> None:
        self.assertEqual(ledger.label_for("proof"), "arginf:proof")
        self.assertEqual(ledger.kind_from_labels(["arginf:proof", "needs-review"]), "proof")
        self.assertIsNone(ledger.kind_from_labels([]))
        self.assertIsNone(ledger.kind_from_labels(["arginf:proof", "arginf:checkpoint"]))
        self.assertIsNone(ledger.kind_from_labels(["arginf:other"]))
        with self.assertRaises(ValueError):
            ledger.label_for("other")


class EntryFromPrTests(unittest.TestCase):
    def test_attempt_and_statement_prs(self) -> None:
        got = ledger.entry_from_pr(
            7, "Bob", ["arginf:proof"], "2026-11-27T01:00:00Z", attempt_files(S1, A1)
        )
        self.assertEqual(got, e(7, "bob", "proof", S1, A1, "2026-11-27T01:00:00Z"))
        got = ledger.entry_from_pr(
            8, "al", ["arginf:statement"], "2026-11-27T01:00:00Z", [layout.statement_path(S2)]
        )
        self.assertEqual(got, e(8, "al", "statement", S2, None, "2026-11-27T01:00:00Z"))

    def test_mismatches_are_ignored(self) -> None:
        t = "2026-11-27T01:00:00Z"
        self.assertIsNone(ledger.entry_from_pr(1, "x", [], t, attempt_files(S1, A1)))
        self.assertIsNone(
            ledger.entry_from_pr(1, "x", ["arginf:statement"], t, attempt_files(S1, A1))
        )
        self.assertIsNone(
            ledger.entry_from_pr(1, "x", ["arginf:proof"], t, [layout.statement_path(S1)])
        )
        self.assertIsNone(
            ledger.entry_from_pr(
                1, "x", ["arginf:proof"], t, attempt_files(S1, A1) + attempt_files(S1, A2)
            )
        )
        self.assertIsNone(
            ledger.entry_from_pr(1, "x", ["arginf:proof"], t, attempt_files(S1, A1) + ["README.md"])
        )
        self.assertIsNone(
            ledger.entry_from_pr(1, "x", ["arginf:proof"], None, attempt_files(S1, A1))
        )


class LegacyTitleTests(unittest.TestCase):
    def test_titles_from_before_the_queue(self) -> None:
        old_s, old_a = "b" * layout.LEGACY_HASH_LEN, "c" * layout.LEGACY_HASH_LEN
        renames = {"statements": {old_s: S1}, "attempts": {old_a: A1}, "attemptStatement": {A1: S1}}
        rows = [
            (f"Merge #3: statement {old_s} by @Alice", "2026-11-20T00:00:00Z"),
            (f"Merge #4: proof {old_a} by @bob", "2026-11-21T00:00:00Z"),
            ("Merge pull request #5 from x/y", "2026-11-22T00:00:00Z"),
            (f"Merge #6: proof {'d' * 12} by @bob", "2026-11-23T00:00:00Z"),  # unknown attempt
        ]
        self.assertEqual(
            ledger.from_titles(rows, renames),
            [
                e(3, "alice", "statement", S1, None, "2026-11-20T00:00:00Z"),
                e(4, "bob", "proof", S1, A1, "2026-11-21T00:00:00Z"),
            ],
        )


class FileTests(unittest.TestCase):
    def test_round_trip_dedup_and_order(self) -> None:
        a = e(2, "bob", "proof", S1, A1, "2026-11-27T02:00:00Z")
        b = e(1, "al", "statement", S1, None, "2026-11-27T01:00:00Z")
        merged = ledger.merge([a], [b, a])
        self.assertEqual(merged, [b, a])
        self.assertEqual(ledger.loads(ledger.dumps(merged)), merged)

    def test_invalid_files_raise(self) -> None:
        good = json.loads(ledger.dumps([e(1, "al", "statement", S1, None, "2026-11-27T01:00:00Z")]))
        bad_variants = [
            "not json",
            json.dumps({"version": 99, "entries": []}),
            json.dumps({"version": 1}),
            json.dumps({**good, "entries": [{**good["entries"][0], "kind": "other"}]}),
            json.dumps({**good, "entries": [{**good["entries"][0], "statement": "zz"}]}),
            json.dumps({**good, "entries": [{**good["entries"][0], "pr": "1"}]}),
            json.dumps({**good, "entries": [{**good["entries"][0], "author": "bad login!"}]}),
            json.dumps({**good, "entries": [{**good["entries"][0], "extra": 1}]}),
            json.dumps({**good, "entries": [{**good["entries"][0], "attempt": A1}]}),
        ]
        for text in bad_variants:
            with self.subTest(text=text), self.assertRaises(ledger.LedgerError):
                ledger.loads(text)


class EarnedAndRecentTests(unittest.TestCase):
    ENTRIES = (
        e(1, "alice", "statement", S1, None, "2026-11-26T00:00:00Z"),
        e(2, "alice", "statement", S2, None, "2026-11-26T00:00:00Z"),
        e(3, "mallory", "statement", S3, None, "2026-11-26T00:00:00Z"),
        e(4, "bob", "proof", S1, A1, "2026-11-26T01:00:00Z"),
        e(5, "carol", "proof", S1, A2, "2026-11-26T02:00:00Z"),  # second proof of S1
        e(6, "bob", "proof", S2, A3, "2026-11-27T01:00:00Z"),
        e(7, "mallory", "proof", S3, A1, "2026-11-27T01:00:00Z"),  # own statement
        e(8, "bob", "checkpoint", S3, A2, "2026-11-27T02:00:00Z"),
    )

    def test_earned(self) -> None:
        self.assertEqual(ledger.earned(list(self.ENTRIES), "Bob"), 2)
        self.assertEqual(ledger.earned(list(self.ENTRIES), "carol"), 0)
        self.assertEqual(ledger.earned(list(self.ENTRIES), "mallory"), 0)

    def test_recent(self) -> None:
        since = "2026-11-27T00:00:00Z"
        self.assertEqual(ledger.recent(list(self.ENTRIES), "bob", "proof", since), 1)
        self.assertEqual(ledger.recent(list(self.ENTRIES), "BOB", "checkpoint", since), 1)
        self.assertEqual(ledger.recent(list(self.ENTRIES), "bob", "statement", since), 0)
        self.assertEqual(
            ledger.recent(list(self.ENTRIES), "alice", "statement", "2026-11-25T00:00:00Z"), 2
        )


if __name__ == "__main__":
    unittest.main()
