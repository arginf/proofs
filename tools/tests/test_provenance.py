"""Provenance: when and by whom each attempt was merged, and its PR's 👍, from git + GitHub."""

import unittest
from pathlib import Path

from tools import layout, provenance

H = "0123456789abcdef0123"
A1, A2, A3 = "a" * 20, "b" * 20, "c" * 20


def index() -> dict[str, object]:
    att = lambda a, complete, size: {"hash": a, "complete": complete, "size": size}  # noqa: E731
    return {
        "version": 2,
        "theorems": [
            {
                "hash": H,
                "proofs": [A1, A2],
                "attempts": [att(A1, True, 90), att(A2, True, 40), att(A3, False, 10)],
            }
        ],
    }


class ProvenanceTests(unittest.TestCase):
    def test_collect_uses_first_adding_commit_and_its_pr(self) -> None:
        commits = {A1: ("s1", "2026-11-01T00:00:00Z"), A2: ("s2", "2026-11-02T00:00:00Z")}
        prs = {"s1": (1, "alice", 0), "s2": (2, "bob", 5)}
        out = provenance.collect(
            [(H, A1), (H, A2), (H, A3)],
            first_commit=lambda h, a: commits.get(a),
            pr_for=lambda sha: prs.get(sha),
        )
        self.assertEqual(
            out[A2],
            {
                "mergedAt": "2026-11-02T00:00:00Z",
                "commit": "s2",
                "pr": 2,
                "author": "bob",
                "upvotes": 5,
            },
        )
        self.assertNotIn(A3, out)  # never committed

    def test_direct_push_without_pr_has_no_author(self) -> None:
        out = provenance.collect(
            [(H, A1)], first_commit=lambda h, a: ("s1", "t"), pr_for=lambda s: None
        )
        self.assertEqual(
            out[A1], {"mergedAt": "t", "commit": "s1", "pr": None, "author": None, "upvotes": 0}
        )

    def test_embed_marks_first_shortest_and_most_upvoted(self) -> None:
        prov = {
            A1: {
                "mergedAt": "2026-11-01T00:00:00Z",
                "commit": "s1",
                "pr": 1,
                "author": "alice",
                "upvotes": 0,
            },
            A2: {
                "mergedAt": "2026-11-02T00:00:00Z",
                "commit": "s2",
                "pr": 2,
                "author": "bob",
                "upvotes": 5,
            },
        }
        t = provenance.embed(index(), prov)["theorems"][0]
        self.assertEqual(t["firstProof"], A1)
        self.assertEqual(t["shortestProof"], A2)
        self.assertEqual(t["topProof"], A2)
        by = {a["hash"]: a for a in t["attempts"]}
        self.assertEqual(by[A2]["author"], "bob")
        self.assertEqual(by[A3].get("author"), None)

    def test_no_top_proof_without_votes(self) -> None:
        prov = {A1: {"mergedAt": "1", "commit": "s", "pr": 1, "author": "a", "upvotes": 0}}
        t = provenance.embed(index(), prov)["theorems"][0]
        self.assertIsNone(t["topProof"])

    def test_attempt_paths(self) -> None:
        import tempfile

        root = Path(tempfile.mkdtemp())
        ((root / layout.attempt_path(H, A1)).parent).mkdir(parents=True)
        (root / layout.attempt_path(H, A1)).write_text("x")
        self.assertEqual(provenance.attempt_paths(root), [(H, A1)])


if __name__ == "__main__":
    unittest.main()
