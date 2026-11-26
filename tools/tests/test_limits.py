"""Per-author overrides of automerge's daily caps (limits.toml, maintainer-only)."""

import tempfile
import unittest
from pathlib import Path

from tools import arginf, automerge, layout, limits

REPO = Path(__file__).resolve().parents[2]


class ParseTests(unittest.TestCase):
    def test_defaults_without_a_file_or_entry(self) -> None:
        table, errors = limits.parse("")
        self.assertEqual(errors, [])
        self.assertEqual(limits.caps_for(table, "anyone"), limits.DEFAULT)
        self.assertEqual(limits.DEFAULT, limits.Caps(statements=5, checkpoints=20, proofs=10))

    def test_proof_cap_is_earned(self) -> None:
        self.assertEqual(limits.caps_for({}, "x", earned=0).proofs, limits.BASE_PROOFS)
        self.assertEqual(
            limits.caps_for({}, "x", earned=3).proofs,
            limits.BASE_PROOFS + 3 * limits.PROOFS_PER_EARNED,
        )
        self.assertEqual(limits.caps_for({}, "x", earned=10_000).proofs, limits.MAX_EARNED_PROOFS)
        self.assertEqual(limits.caps_for({}, "x", earned=-5).proofs, limits.BASE_PROOFS)

    def test_an_explicit_override_wins_over_earning(self) -> None:
        table, _ = limits.parse("[authors.x]\nproofs = 0\n[authors.y]\ncheckpoints = 99\n")
        self.assertEqual(limits.caps_for(table, "x", earned=50).proofs, 0)
        y = limits.caps_for(table, "y", earned=2)
        self.assertEqual(
            (y.checkpoints, y.proofs), (99, limits.BASE_PROOFS + 2 * limits.PROOFS_PER_EARNED)
        )

    def test_override_merges_with_defaults_and_ignores_login_case(self) -> None:
        table, errors = limits.parse("[authors.AdamPDavis]\nproofs = 200\n")
        self.assertEqual(errors, [])
        caps = limits.caps_for(table, "adampdavis", earned=7)
        self.assertEqual(caps, limits.Caps(statements=5, checkpoints=20, proofs=200))
        self.assertEqual(limits.caps_for(table, "someone-else"), limits.DEFAULT)

    def test_zero_means_always_wait_for_a_maintainer(self) -> None:
        table, _ = limits.parse("[authors.x]\nstatements = 0\n")
        self.assertEqual(limits.caps_for(table, "x").statements, 0)

    def test_invalid_files_are_rejected(self) -> None:
        for text in (
            "[authors.x]\nproofs = -1\n",
            f"[authors.x]\nproofs = {limits.MAX_CAP + 1}\n",
            '[authors.x]\nproofs = "10"\n',
            "[authors.x]\nproofs = true\n",
            "[authors.x]\nproof = 10\n",
            "[authors.x]\nmin_account_age_days = 0\n",
            "[authors.x]\nproofs = 1\n[authors.X]\nproofs = 2\n",
            "[other]\nx = 1\n",
            "[authors.'bad login!']\nproofs = 1\n",
            "authors = 3\n",
            "not toml [",
        ):
            with self.subTest(text=text):
                _, errors = limits.parse(text)
                self.assertTrue(errors, text)

    def test_load_fails_closed_on_errors(self) -> None:
        with self.assertRaises(limits.LimitsError):
            limits.load_text("[authors.x]\nproofs = -1\n")

    def test_repo_file_is_valid(self) -> None:
        path = REPO / limits.LIMITS_FILE
        if not path.is_file():
            self.skipTest("no limits.toml")
        self.assertEqual(limits.parse(path.read_text(encoding="utf-8"))[1], [])

    def test_check_all_reports_an_invalid_file(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "Theorems").mkdir()
        (root / limits.LIMITS_FILE).write_text("[authors.x]\nproofs = -1\n")
        self.assertTrue(arginf.check_all(root))
        (root / limits.LIMITS_FILE).write_text("[authors.x]\nproofs = 50\n")
        self.assertEqual(arginf.check_all(root), [])


class DecideUsesCaps(unittest.TestCase):
    H = "0123456789abcdef0123"
    A = "ba98765432100123abcd"

    def decide(self, lean: str, caps: limits.Caps, recent: int) -> automerge.Decision:
        fs = [
            {"filename": layout.attempt_path(self.H, self.A, "Attempt.lean"), "status": "added"},
            {"filename": layout.attempt_path(self.H, self.A, "attempt.toml"), "status": "added"},
        ]
        return automerge.decide(
            fs,
            statement_exists=lambda h: True,
            attempt_lean=lambda p: lean,
            recent_statements=lambda: recent,
            recent_checkpoints=lambda: recent,
            recent_proofs=lambda: recent,
            changed_files=2,
            account_age_days=lambda: 365.0,
            caps=caps,
        )

    def test_raised_and_lowered_caps(self) -> None:
        proof = (
            f"import {layout.statement_module(self.H)}\n\nnamespace X\n\n"
            f"theorem proof : Theorems.«{self.H}».statement :=\n  trivial\n\nend X\n"
        )
        partial = proof.replace("trivial", "by\n  sorry")
        high = limits.Caps(statements=5, checkpoints=100, proofs=100)
        self.assertFalse(self.decide(proof, limits.DEFAULT, 10).merge)
        self.assertTrue(self.decide(proof, high, 10).merge)
        self.assertFalse(self.decide(proof, high, 100).merge)
        self.assertTrue(self.decide(partial, high, 50).merge)
        self.assertFalse(self.decide(proof, limits.Caps(5, 20, 0), 0).merge)


if __name__ == "__main__":
    unittest.main()
