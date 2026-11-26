"""Auto-merge: only a PR that adds one complete attempt to an existing statement qualifies."""

import unittest

from tools import automerge

H = "0123456789ab"
A = "ba9876543210"
COMPLETE = (
    f"import Theorems.«{H}».Statement\n\nnamespace X\n\n"
    f"theorem proof : Theorems.«{H}».statement :=\n  trivial\n\nend X\n"
)
PARTIAL = COMPLETE.replace("trivial", "by\n  sorry")


def files(*paths: str, status: str = "added") -> list[dict[str, str]]:
    return [{"filename": p, "status": status} for p in paths]


ATTEMPT = files(
    f"Theorems/{H}/attempts/{A}/Attempt.lean", f"Theorems/{H}/attempts/{A}/attempt.toml"
)


STATEMENT = (
    f"import Mathlib\n\nnamespace Theorems.«{H}»\n\n"
    f"def statement : Prop :=\n  True\n\nend Theorems.«{H}»\n"
)


class DecideTests(unittest.TestCase):
    def ok(
        self,
        fs: list[dict[str, str]],
        lean: str = COMPLETE,
        exists: bool = True,
        recent: int = 0,
        recent_checkpoints: int = 0,
    ) -> automerge.Decision:
        return automerge.decide(
            fs,
            statement_exists=lambda h: exists,
            attempt_lean=lambda path: lean,
            recent_statements=lambda: recent,
            recent_checkpoints=lambda: recent_checkpoints,
        )

    def test_complete_attempt_on_existing_statement_merges(self) -> None:
        d = self.ok(ATTEMPT)
        self.assertTrue(d.merge, d.reason)
        self.assertEqual((d.theorem, d.attempt), (H, A))

    def test_partial_attempt_merges_as_a_checkpoint(self) -> None:
        d = self.ok(ATTEMPT, PARTIAL)
        self.assertTrue(d.merge, d.reason)
        self.assertEqual(d.kind, "checkpoint")

    def test_checkpoint_daily_cap(self) -> None:
        d = self.ok(ATTEMPT, PARTIAL, recent_checkpoints=automerge.DAILY_CHECKPOINTS)
        self.assertFalse(d.merge)
        self.assertTrue(self.ok(ATTEMPT, COMPLETE, recent_checkpoints=99).merge)  # proofs uncapped

    def test_sorry_in_comments_is_not_a_checkpoint_but_real_sorry_is(self) -> None:
        self.assertEqual(self.ok(ATTEMPT, COMPLETE + "-- sorry\n").kind, "proof")
        self.assertEqual(self.ok(ATTEMPT, COMPLETE.replace("trivial", "sorry")).kind, "checkpoint")

    def test_other_files_never_auto_merge(self) -> None:
        self.assertFalse(self.ok(ATTEMPT + files(".github/workflows/pr.yml")).merge)
        self.assertFalse(self.ok(ATTEMPT + files("curation.toml")).merge)
        self.assertFalse(self.ok(ATTEMPT + files(f"Theorems/{H}/Statement.lean")).merge)

    def test_only_additions_of_one_attempt(self) -> None:
        self.assertFalse(self.ok(files(*[f["filename"] for f in ATTEMPT], status="modified")).merge)
        other = files(
            f"Theorems/{H}/attempts/{H}/Attempt.lean", f"Theorems/{H}/attempts/{H}/attempt.toml"
        )
        self.assertFalse(self.ok(ATTEMPT + other).merge)
        self.assertFalse(self.ok(files(f"Theorems/{H}/attempts/{A}/Attempt.lean")).merge)  # no toml

    def test_statement_must_already_exist_on_main(self) -> None:
        self.assertFalse(self.ok(ATTEMPT, exists=False).merge)

    def test_new_statement_merges_on_its_own(self) -> None:
        d = self.ok(files(f"Theorems/{H}/Statement.lean"), lean=STATEMENT, exists=False)
        self.assertTrue(d.merge, d.reason)
        self.assertEqual(d.kind, "statement")

    def test_statement_guardrails(self) -> None:
        stmt = files(f"Theorems/{H}/Statement.lean")
        self.assertFalse(self.ok(stmt, lean=STATEMENT.replace("True", "sorry"), exists=False).merge)
        bad_import = STATEMENT.replace("import Mathlib", "import Mathlib\nimport SomeOther.Lib")
        self.assertFalse(self.ok(stmt, lean=bad_import, exists=False).merge)
        for ok_import in (
            "Init",
            "Std",
            "Lean",
            "Mathlib.Data.Nat.Basic",
            "Cslib.Crypto",
            "CslibFrontier.X",
        ):
            lean = STATEMENT.replace("import Mathlib", f"import {ok_import}")
            self.assertTrue(self.ok(stmt, lean=lean, exists=False).merge, ok_import)
        self.assertFalse(
            self.ok(stmt, lean=STATEMENT, exists=False, recent=automerge.DAILY_STATEMENTS).merge
        )
        self.assertFalse(self.ok(stmt, lean=STATEMENT, exists=True).merge)  # already on main

    def test_statement_and_attempt_together_wait_for_a_maintainer(self) -> None:
        both = files(f"Theorems/{H}/Statement.lean") + ATTEMPT
        self.assertFalse(self.ok(both, exists=False).merge)

    def test_path_tricks(self) -> None:
        bad = files(
            f"Theorems/{H}/attempts/{A}/../../../lakefile.toml",
            f"Theorems/{H}/attempts/{A}/attempt.toml",
        )
        self.assertFalse(self.ok(bad).merge)


if __name__ == "__main__":
    unittest.main()
