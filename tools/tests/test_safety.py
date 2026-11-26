"""Theorem folders hold plain Lean: nothing that runs code while Lean compiles or imports it.

These tests pin the lexical allowlist in tools/safety.py. It is the first of two layers: the
second is the kernel re-check (`leanchecker`) that CI runs after building.
"""

import unittest
from pathlib import Path

from tools import arginf, safety

H = "0123456789ab"
A = "ba9876543210"
REPO = Path(__file__).resolve().parents[2]


def statement(body: str = "∀ n : Nat, n + 0 = n", imports: str = "import Init") -> str:
    return (
        f"{imports}\n\nnamespace Theorems.«{H}»\n\n"
        f"def statement : Prop :=\n  {body}\n\nend Theorems.«{H}»\n"
    )


def attempt(code: str, imports: str = "") -> str:
    extra = f"{imports}\n" if imports else ""
    return (
        f"import Theorems.«{H}».Statement\n{extra}\nnamespace Theorems.«{H}».Attempt_{A}\n\n"
        f"{code}\n\nend Theorems.«{H}».Attempt_{A}\n"
    )


PROOF = f"theorem proof : Theorems.«{H}».statement := by\n  intro n\n  simp"


class RepoContentTests(unittest.TestCase):
    def test_every_merged_file_passes(self) -> None:
        theorems = REPO / "Theorems"
        if not theorems.is_dir():
            self.skipTest("no Theorems/ folder")
        for stmt in sorted(theorems.glob("*/Statement.lean")):
            errs = safety.statement_errors(stmt, stmt.read_text(encoding="utf-8"), stmt.parent.name)
            self.assertEqual(errs, [], stmt)
        for att in sorted(theorems.glob("*/attempts/*/Attempt.lean")):
            self.assertEqual(safety.attempt_errors(att, att.read_text(encoding="utf-8")), [], att)


class OrdinaryProofsPass(unittest.TestCase):
    def test_typical_attempts(self) -> None:
        cases = [
            PROOF,
            f"theorem proof : Theorems.«{H}».statement :=\n  fun n => Nat.add_zero n",
            f"theorem proof : Theorems.«{H}».statement := λ n => Nat.add_zero n",
            f"theorem proof : Theorems.«{H}».statement := by\n  sorry",
            f"@[simp] lemma helper (n : Nat) : n + 0 = n := rfl\n\n{PROOF}",
            f"@[simp, reducible] def two : Nat := 2\n\n{PROOF}",
            f"def x : Nat := 1\n\n{PROOF}",
            f"set_option maxHeartbeats 400000 in\n{PROOF}",
            f"open Nat in\n{PROOF}",
            f"theorem arr : #[1, 2].size = 2 := rfl\n\n{PROOF}",
            f"theorem card (s : List Nat) : True := by\n  have := # s\n  trivial\n\n{PROOF}",
            f"private theorem x₁ (α : Type) (a : α) : a = a := rfl\n\n{PROOF}",
            f"noncomputable def pick : Nat := Classical.choice ⟨0⟩\n\n{PROOF}",
            f"theorem big : 12345678901234567890 = 12345678901234567890 := rfl\n\n{PROOF}",
            f"theorem c : 'a' = 'a' := rfl\n\n{PROOF}",
        ]
        for code in cases:
            with self.subTest(code=code):
                self.assertEqual(safety.attempt_errors("A", attempt(code)), [])

    def test_mathlib_imports_allowed_in_attempts(self) -> None:
        for imp in (
            "import Mathlib",
            "import Mathlib.Data.Nat.Basic",
            "import Cslib.Crypto",
            "import CslibFrontier.X",
            f"import Theorems.«{A}».Statement",
            f"import Theorems.«{A}».attempts.«{H}».Attempt",
        ):
            with self.subTest(imp=imp):
                self.assertEqual(safety.attempt_errors("A", attempt(PROOF, imp)), [])


class CodeThatRunsIsRejected(unittest.TestCase):
    """Anything that can run code at compile or import time, or extend the parser, is refused."""

    def assertRejected(self, code: str) -> None:
        self.assertTrue(safety.attempt_errors("A", attempt(code)), code)

    def test_forbidden_keywords_anywhere(self) -> None:
        for kw in sorted(safety.FORBIDDEN_KEYWORDS):
            for code in (
                f"{kw} x\n{PROOF}",  # at the start of a line
                f"{PROOF}\n  {kw} x",  # indented after a proof
                f"theorem t : True := trivial {kw} x\n{PROOF}",  # mid-line
            ):
                with self.subTest(kw=kw, code=code):
                    self.assertRejected(code)

    def test_forbidden_name_components(self) -> None:
        for name in sorted(safety.FORBIDDEN_NAMES):
            for spelled in (name, f"Foo.{name}.bar", f"«{name}».x", f"_root_.{name}"):
                with self.subTest(spelled=spelled):
                    self.assertRejected(f"def x := {spelled}\n{PROOF}")

    def test_lambda_glued_to_a_name_is_still_seen(self) -> None:
        self.assertRejected(f"def x := λIO => 0\n{PROOF}")
        self.assertRejected(f"def x := ΠIO, 0\n{PROOF}")

    def test_hash_commands(self) -> None:
        for cmd in ("#eval", "#exit", "#print", "#check", "#guard", "#reduce", "#find", "#x"):
            with self.subTest(cmd=cmd):
                self.assertRejected(f"{cmd} 1\n{PROOF}")
                self.assertRejected(f"{PROOF}\n  {cmd} 1")

    def test_quotations_and_strings(self) -> None:
        self.assertRejected(f"def n := `foo\n{PROOF}")
        self.assertRejected(f"def n := ``Nat\n{PROOF}")
        self.assertRejected('def s := "x"\n' + PROOF)

    def test_attributes_are_allowlisted(self) -> None:
        for attr in (
            "implemented_by f",
            "init",
            "tactic x",
            "csimp",
            "extern",
            "command_elab x",
            "simp, init",
            "norm_num x",
            "aesop",
            "env",
            "macro x",
            "simp [x]",
        ):
            with self.subTest(attr=attr):
                self.assertRejected(f"@[{attr}] def y : Nat := 1\n{PROOF}")
        self.assertRejected(f"@[simp\n{PROOF}")  # unterminated

    def test_options_are_allowlisted_and_capped(self) -> None:
        for opt in (
            "debug.skipKernelTC true",
            "pp.all true",
            "maxHeartbeats 0",
            f"maxHeartbeats {safety.OPTION_CAPS['maxHeartbeats'] + 1}",
            "maxHeartbeats x",
            "trace.Meta.Tactic.simp true",
            "compiler.extract_closed false",
        ):
            with self.subTest(opt=opt):
                self.assertRejected(f"set_option {opt} in\n{PROOF}")
        self.assertRejected(f"set_option\n{PROOF}")

    def test_long_numerals(self) -> None:
        self.assertRejected(f"theorem t : {'1' * 21} = {'1' * 21} := rfl\n{PROOF}")
        self.assertRejected(f"theorem t : 0x{'f' * 21} = 0 := rfl\n{PROOF}")

    def test_size_limit(self) -> None:
        pad = "\n".join(f"theorem t{i} : True := trivial" for i in range(20000))
        self.assertRejected(f"{pad}\n{PROOF}")

    def test_invisible_and_bidi_characters(self) -> None:
        for ch in ("\u202e", "\u2066", "\u200b", "\u200d", "\ufeff", "\u2028", "\x00", "\x1b"):
            with self.subTest(ch=repr(ch)):
                self.assertRejected(f"theorem t{ch} : True := trivial\n{PROOF}")

    def test_imports_are_allowlisted(self) -> None:
        for imp in (
            "import Foo",
            "import Lake",
            "import Theorems.«abc».Other",
            "import Theorems.Something",
            "import  Mathlib.X extra",
        ):
            with self.subTest(imp=imp):
                self.assertTrue(safety.attempt_errors("A", attempt(PROOF, imp)))


class StatementShape(unittest.TestCase):
    """A Statement.lean is imports plus exactly `def statement : Prop := ...` in its namespace."""

    def test_good_statements(self) -> None:
        for body in (
            "∀ n : Nat, n + 0 = n",
            "True",
            "open Nat in ∀ n : Nat, n = n",
            "∀ f : List Bool → List Bool, f = f",
        ):
            with self.subTest(body=body):
                self.assertEqual(safety.statement_errors("S", statement(body), H), [])
        for imp in (
            "import Mathlib",
            "import Init",
            "import Std",
            "import Lean",
            f"import Theorems.«{A}».Statement",
            "import Mathlib\nimport CslibFrontier.A",
        ):
            with self.subTest(imp=imp):
                self.assertEqual(safety.statement_errors("S", statement(imports=imp), H), [])

    def test_helpers_are_not_allowed(self) -> None:
        # The hash covers only the body, so a helper would change the meaning without the hash.
        before = statement().replace("def statement", "def helper : Prop := False\n\ndef statement")
        after = statement().replace(
            "\nend Theorems", "\ntheorem t : True := trivial\n\nend Theorems"
        )
        for text in (before, after):
            with self.subTest(text=text):
                self.assertTrue(safety.statement_errors("S", text, H))

    def test_namespace_must_match_hash(self) -> None:
        self.assertTrue(safety.statement_errors("S", statement(), A))
        self.assertTrue(
            safety.statement_errors(
                "S", statement().replace(f"namespace Theorems.«{H}»", "namespace X"), H
            )
        )

    def test_body_rules(self) -> None:
        for body in (
            "sorry",
            "True\n\ndef x : Nat := 1",
            "@[simp] True",
            "IO.Unit = IO.Unit",
            "True\nset_option maxHeartbeats 1 in True",
        ):
            with self.subTest(body=body):
                self.assertTrue(safety.statement_errors("S", statement(body), H))

    def test_statement_imports(self) -> None:
        for imp in ("import Foo", f"import Theorems.«{A}».Other"):
            with self.subTest(imp=imp):
                self.assertTrue(safety.statement_errors("S", statement(imports=imp), H))


class WiredIntoChecks(unittest.TestCase):
    """`arginf.py check` (every PR) and `check-all` (every main build) run the allowlist."""

    def repo(self) -> Path:
        import tempfile

        root = Path(tempfile.mkdtemp())
        (root / "Theorems").mkdir()
        return root

    def test_check_attempt_and_check_all_report_unsafe_lean(self) -> None:
        root = self.repo()
        h = arginf.new_theorem(root, "∀ n : Nat, n + 0 = n", ("Init",))
        a = arginf.start_attempt(root, h, None)
        f = root / "Theorems" / h / "attempts" / a / "Attempt.lean"
        f.write_text(f.read_text().replace("by\n  sorry", "by\n  run_tac pure ()\n  sorry"))
        a = arginf.seal_attempt(f.parent)
        att = root / "Theorems" / h / "attempts" / a
        self.assertTrue(any("run_tac" in e for e in arginf.check_attempt(att)))
        self.assertTrue(any("run_tac" in e for e in arginf.check_all(root)))

    def test_check_folder_reports_a_helper_in_the_statement(self) -> None:
        root = self.repo()
        h = arginf.new_theorem(root, "True", ("Init",))
        stmt = root / "Theorems" / h / "Statement.lean"
        stmt.write_text(
            stmt.read_text().replace("def statement", "def t : Prop := True\ndef statement")
        )
        self.assertTrue(arginf.check_folder(stmt.parent))

    def test_new_theorem_output_passes(self) -> None:
        root = self.repo()
        h = arginf.new_theorem(root, "∀ n : Nat, n + 0 = n", ("Mathlib",))
        self.assertEqual(arginf.check_all(root), [])
        self.assertEqual(arginf.check_folder(root / "Theorems" / h), [])

    def test_fork_clones_check_against_upstream(self) -> None:
        self.assertEqual(arginf.default_base(["origin", "upstream"]), "upstream/main")
        self.assertEqual(arginf.default_base(["origin"]), "origin/main")

    def test_kernel_check_runs_one_module_at_a_time(self) -> None:
        # One process per module: leanchecker imports Mathlib once per module, and doing them
        # all in one process at once ran out of memory on CI.
        import subprocess
        from unittest import mock

        done = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(arginf.subprocess, "run", return_value=done) as run:
            self.assertEqual(arginf.kernel_check(Path("."), ["M.A", "M.B"]), [])
            self.assertEqual(
                [c.args[0] for c in run.call_args_list],
                [["lake", "env", "leanchecker", "M.A"], ["lake", "env", "leanchecker", "M.B"]],
            )
            run.reset_mock()
            arginf.kernel_check(Path("."), ["M.A"], env={})
            self.assertEqual(run.call_args.args[0], ["leanchecker", "M.A"])
        killed = subprocess.CompletedProcess([], -9, "", "")
        with mock.patch.object(arginf.subprocess, "run", return_value=killed):
            errs = arginf.kernel_check(Path("."), ["M.A"])
            self.assertTrue(errs)
            self.assertIn("-9", errs[0])
        self.assertTrue(arginf.kernel_check(Path("."), []))  # nothing to check is an error

    def test_theorem_modules(self) -> None:
        root = self.repo()
        h = arginf.new_theorem(root, "True", ("Init",))
        a = arginf.start_attempt(root, h, None)
        mods = arginf.theorem_modules(root)
        self.assertEqual(
            mods, [f"Theorems.«{h}».Statement", f"Theorems.«{h}».attempts.«{a}».Attempt"]
        )
        self.assertEqual(arginf.theorem_modules(root, [h]), mods)
        self.assertEqual(arginf.theorem_modules(root, ["ffffffffffff"]), [])


if __name__ == "__main__":
    unittest.main()
