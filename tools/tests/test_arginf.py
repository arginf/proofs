"""Tests for the arginf contributor tool (stdlib only: `python3 -m unittest`)."""

import json
import tempfile
import unittest
from pathlib import Path

from tools import arginf, layout

H = "0123456789abcdef0123"
A = "ba98765432100123abcd"


def repo() -> Path:
    root = Path(tempfile.mkdtemp())
    (root / "Theorems").mkdir()
    return root


def complete(root: Path, h: str, term: str, parent: str | None = None) -> str:
    a = arginf.start_attempt(root, h, parent)
    f = root / layout.attempt_path(h, a)
    f.write_text(f.read_text().replace("by\n  sorry", term))
    return arginf.seal_attempt(f.parent)


class HashTests(unittest.TestCase):
    def test_extracts_and_normalizes_prop(self) -> None:
        text = (
            "import Init\nnamespace X\ndef statement : Prop :=\n  ∀ n : Nat,   n + 0 = n\nend X\n"
        )
        self.assertEqual(arginf.extract_statement(text), "∀ n : Nat, n + 0 = n")

    def test_hash_ignores_whitespace_namespace_and_imports(self) -> None:
        a = arginf.statement_hash(
            "import Init\nnamespace Y\ndef statement : Prop :=\n  1 = 1\nend Y"
        )
        b = arginf.statement_hash("def statement : Prop := 1   =   1")
        self.assertEqual(a, b)
        self.assertRegex(a, r"^[0-9a-f]{20}$")

    def test_hash_changes_with_statement(self) -> None:
        self.assertNotEqual(
            arginf.statement_hash("def statement : Prop := 1 = 1"),
            arginf.statement_hash("def statement : Prop := 2 = 2"),
        )

    def test_rejects_missing_or_duplicate_statement(self) -> None:
        with self.assertRaises(arginf.ToolError):
            arginf.extract_statement("theorem x : True := trivial")
        with self.assertRaises(arginf.ToolError):
            arginf.extract_statement("def statement : Prop := True\ndef statement : Prop := False")


class NoProseTests(unittest.TestCase):
    """Theorem folders are Lean only: the statements are the statements."""

    def test_comments_and_strings_rejected(self) -> None:
        self.assertTrue(arginf.prose_errors("f", "-- why\ndef statement : Prop := True"))
        self.assertTrue(arginf.prose_errors("f", "/-! doc -/\ndef statement : Prop := True"))
        self.assertTrue(arginf.prose_errors("f", 'def statement : Prop := "hi" = "hi"'))
        self.assertEqual(arginf.prose_errors("f", "def statement : Prop := ∀ n : Nat, n = n"), [])

    def test_folder_with_prose_fails(self) -> None:
        root = repo()
        h = arginf.new_theorem(root, "True", imports=("Init",))
        stmt = root / layout.statement_path(h)
        stmt.write_text("-- a claim\n" + stmt.read_text())
        self.assertTrue(any("comments" in e for e in arginf.check_folder(stmt.parent)))

    def test_meta_and_readme_files_rejected(self) -> None:
        root = repo()
        h = arginf.new_theorem(root, "True", imports=("Init",))
        (layout.statement_dir(root, h) / "meta.toml").write_text('title = "x"\n')
        errs = arginf.check_folder(layout.statement_dir(root, h))
        self.assertTrue(any("allowed" in e for e in errs))
        self.assertTrue(arginf.check_scope([f"Theorems/{H[:2]}/{H}/README.md"], f"{H}/x"))

    def test_attempt_notes_rejected(self) -> None:
        root = repo()
        h = arginf.new_theorem(root, "True", imports=("Init",))
        a = arginf.start_attempt(root, h, None)
        toml = layout.attempt_dir(root, h, a) / "attempt.toml"
        toml.write_text('parent = ""\nnote = "my idea"\n')
        self.assertTrue(any("only `parent`" in e for e in arginf.check_attempt(toml.parent)))

    def test_attempt_with_comment_rejected(self) -> None:
        root = repo()
        h = arginf.new_theorem(root, "True", imports=("Init",))
        a = arginf.start_attempt(root, h, None)
        f = root / layout.attempt_path(h, a)
        f.write_text(f.read_text().replace("sorry", "sorry -- stuck here"))
        self.assertTrue(any("comments" in e for e in arginf.check_attempt(f.parent)))


class ScopeTests(unittest.TestCase):
    def test_accepts_statement_and_one_attempt(self) -> None:
        files = [
            layout.statement_path(H),
            layout.attempt_path(H, A, "Attempt.lean"),
            layout.attempt_path(H, A, "attempt.toml"),
        ]
        self.assertEqual(arginf.check_scope(files, f"{H}/{A}"), [])

    def test_rejects_outside_two_folders_two_attempts_branch_and_paths(self) -> None:
        self.assertTrue(
            arginf.check_scope([layout.statement_path(H), "lakefile.toml"], f"{H}/x")
        )
        self.assertTrue(
            arginf.check_scope(
                [layout.statement_path(H), layout.statement_path(A)], f"{H}/x"
            )
        )
        two = [layout.attempt_path(H, A, "Attempt.lean"), layout.attempt_path(H, H, "Attempt.lean")]
        self.assertTrue(arginf.check_scope(two, f"{H}/x"))
        self.assertTrue(arginf.check_scope([layout.statement_path(H)], "main"))
        # Proof.lean no longer exists: every complete attempt is a proof.
        self.assertTrue(arginf.check_scope([f"Theorems/{H[:2]}/{H}/Proof.lean"], f"{H}/x"))
        trick = f"Theorems/{H[:2]}/{H}/../../../lakefile.toml"
        self.assertTrue(arginf.check_scope([trick], f"{H}/x"))

    def test_curation_is_maintainer_only(self) -> None:
        errs = arginf.check_scope(["curation.toml"], f"{H}/x")
        self.assertTrue(any("outside" in e for e in errs))


class CurationTests(unittest.TestCase):
    def test_featured_and_examples_from_curation_file(self) -> None:
        root = repo()
        h1 = arginf.new_theorem(root, "True", imports=("Init",))
        h2 = arginf.new_theorem(root, "1 = 1", imports=("Init",))
        (root / "curation.toml").write_text(f'featured = ["{h1}"]\nexamples = ["{h2}"]\n')
        by = {t["hash"]: t for t in arginf.build_index(root)["theorems"]}
        self.assertTrue(by[h1]["featured"] and not by[h1]["example"])
        self.assertTrue(by[h2]["example"] and not by[h2]["featured"])

    def test_curation_validates_hashes_and_keys(self) -> None:
        root = repo()
        (root / "curation.toml").write_text('featured = ["nothere00000"]\nhot = []\n')
        errs = arginf.load_curation(root)[1]
        self.assertTrue(any("unknown key" in e for e in errs))
        self.assertTrue(any("hashes" in e or "unknown statements" in e for e in errs))


class AttemptTests(unittest.TestCase):
    def test_start_continue_and_tree(self) -> None:
        root = repo()
        h = arginf.new_theorem(root, "∀ n : Nat, n + 0 = n", imports=("Init",))
        a = arginf.start_attempt(root, h, None)
        b = arginf.start_attempt(root, h, a)
        entry = arginf.build_index(root)["theorems"][0]
        tree = {x["hash"]: x for x in entry["attempts"]}
        self.assertEqual(tree[b]["parent"], a)
        self.assertEqual(tree[a]["children"], [b])
        self.assertEqual(tree[a]["openGoals"], 1)
        self.assertNotIn("note", tree[a])
        self.assertEqual(entry["status"], "open")

    def test_attempt_hash_covers_code_and_parent(self) -> None:
        self.assertNotEqual(
            arginf.attempt_hash("by\n  simp", None), arginf.attempt_hash("by\n  simp", "0" * 20)
        )
        self.assertEqual(
            arginf.attempt_hash("by  simp", None), arginf.attempt_hash("by simp", None)
        )

    def test_seal_rehashes_and_editing_breaks_hash(self) -> None:
        root = repo()
        h = arginf.new_theorem(root, "True", imports=("Init",))
        a = arginf.start_attempt(root, h, None)
        f = root / layout.attempt_path(h, a)
        f.write_text(f.read_text().replace("by\n  sorry", "trivial"))
        self.assertTrue(any("hash" in e for e in arginf.check_attempt(f.parent)))
        new = arginf.seal_attempt(f.parent)
        folder = (root / layout.attempt_path(h, new)).parent
        self.assertEqual(arginf.check_attempt(folder), [])
        self.assertTrue(arginf.attempt_info(folder)["complete"])

    def test_every_complete_attempt_is_a_proof(self) -> None:
        root = repo()
        h = arginf.new_theorem(root, "True", imports=("Init",))
        arginf.start_attempt(root, h, None)  # partial: sorry
        self.assertEqual(arginf.build_index(root)["theorems"][0]["status"], "open")
        long_a = complete(root, h, "by\n  exact trivial")
        short_a = complete(root, h, "trivial")
        t = arginf.build_index(root)["theorems"][0]
        self.assertEqual(t["status"], "proved")
        self.assertEqual(sorted(t["proofs"]), sorted([long_a, short_a]))
        sizes = {x["hash"]: x["size"] for x in t["attempts"]}
        self.assertLess(sizes[short_a], sizes[long_a])


class IndexTests(unittest.TestCase):
    def test_dependencies_and_no_prose_fields(self) -> None:
        root = repo()
        lemma = arginf.new_theorem(root, "True", imports=("Init",))
        la = complete(root, lemma, "trivial")
        main = arginf.new_theorem(root, "1 = 1", imports=("Init",))
        stmt = root / layout.statement_path(main)
        stmt.write_text(f"import {layout.attempt_module(lemma, la)}\n" + stmt.read_text())
        index = arginf.build_index(root)
        self.assertEqual(index["version"], 2)
        by = {t["hash"]: t for t in index["theorems"]}
        self.assertEqual(by[main]["dependencies"], [lemma])
        self.assertEqual(by[lemma]["dependents"], [main])
        for t in index["theorems"]:
            self.assertFalse({"title", "description", "kind", "tags"} & set(t))
        json.dumps(index)


class AxiomTests(unittest.TestCase):
    def test_parse_and_forbid(self) -> None:
        ok = "'x' depends on axioms: [propext, Classical.choice, Quot.sound]"
        self.assertEqual(arginf.forbidden_axioms(arginf.parse_axioms(ok)), [])
        self.assertEqual(arginf.parse_axioms("'x' does not depend on any axioms"), [])
        bad = "'x' depends on axioms: [propext, sorryAx, Lean.ofReduceBool, Theorems.«a».cheat]"
        self.assertEqual(
            arginf.forbidden_axioms(arginf.parse_axioms(bad)),
            ["sorryAx", "Lean.ofReduceBool", "Theorems.«a».cheat"],
        )
        with self.assertRaises(arginf.ToolError):
            arginf.parse_axioms("error: unknown identifier")

    def test_audit_source_pins_statement(self) -> None:
        src = arginf.audit_source(H, A)
        self.assertIn(f"import {layout.attempt_module(H, A)}", src)
        self.assertIn(
            f"example : Theorems.«{H}».statement := Theorems.«{H}».Attempt_{A}.proof", src
        )


class FastPathTests(unittest.TestCase):
    def test_core_only_plan_and_external_imports(self) -> None:
        root = repo()
        h = arginf.new_theorem(root, "∀ n : Nat, n + 0 = n", imports=("Init",))
        a = complete(root, h, "fun n => Nat.add_zero n")
        plan = arginf.fast_plan(root, h)
        assert plan is not None
        names = [m for m, _ in plan]
        self.assertEqual(names[0], layout.statement_module(h))
        self.assertEqual(names[-1], layout.attempt_module(h, a))
        h2 = arginf.new_theorem(root, "True", imports=("Mathlib",))
        self.assertIsNone(arginf.fast_plan(root, h2))


if __name__ == "__main__":
    unittest.main()


class IndexFilesTests(unittest.TestCase):
    def test_each_attempt_carries_its_own_lean(self) -> None:
        root = repo()
        h = arginf.new_theorem(root, "True", imports=("Init",))
        a = complete(root, h, "trivial")
        t = arginf.build_index(root)["theorems"][0]
        self.assertNotIn("proofLean", t)
        att = {x["hash"]: x for x in t["attempts"]}[a]
        self.assertIn("trivial", att["lean"])
        self.assertTrue(att["complete"])


class JunkCheckpointTests(unittest.TestCase):
    """A checkpoint must make progress: no bare template, no unchanged copy of its parent."""

    def test_bare_template_is_rejected(self) -> None:
        root = repo()
        h = arginf.new_theorem(root, "True", imports=("Init",))
        a = arginf.start_attempt(root, h, None)  # just `by sorry`
        errs = arginf.check_attempt((root / layout.attempt_path(h, a)).parent)
        self.assertTrue(any("no progress" in e for e in errs), errs)

    def test_unchanged_copy_of_parent_is_rejected(self) -> None:
        root = repo()
        h = arginf.new_theorem(root, "∀ n : Nat, n + 0 = n", imports=("Init",))
        p = arginf.start_attempt(root, h, None)
        f = root / layout.attempt_path(h, p)
        f.write_text(f.read_text().replace("by\n  sorry", "by\n  intro n\n  sorry"))
        p = arginf.seal_attempt(f.parent)
        child = arginf.start_attempt(root, h, p)  # copy, not yet edited
        cf = root / layout.attempt_path(h, child)
        sealed = arginf.seal_attempt(cf.parent)
        errs = arginf.check_attempt((root / layout.attempt_path(h, sealed)).parent)
        self.assertTrue(any("no progress" in e for e in errs), errs)

    def test_real_progress_passes(self) -> None:
        root = repo()
        h = arginf.new_theorem(root, "∀ n : Nat, n + 0 = n", imports=("Init",))
        p = arginf.start_attempt(root, h, None)
        f = root / layout.attempt_path(h, p)
        f.write_text(f.read_text().replace("by\n  sorry", "by\n  intro n\n  sorry"))
        p = arginf.seal_attempt(f.parent)
        self.assertEqual(arginf.check_attempt((root / layout.attempt_path(h, p)).parent), [])
        child = arginf.start_attempt(root, h, p)
        cf = root / layout.attempt_path(h, child)
        cf.write_text(cf.read_text().replace("intro n\n  sorry", "intro n\n  rfl"))
        c = arginf.seal_attempt(cf.parent)
        self.assertEqual(arginf.check_attempt((root / layout.attempt_path(h, c)).parent), [])
