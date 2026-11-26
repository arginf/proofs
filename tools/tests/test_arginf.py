"""Tests for the arginf contributor tool (stdlib only: `python3 -m unittest`)."""

import json
import tempfile
import unittest
from pathlib import Path

from tools import arginf

H = "0123456789ab"
A = "ba9876543210"


def repo() -> Path:
    root = Path(tempfile.mkdtemp())
    (root / "Theorems").mkdir()
    return root


def complete(root: Path, h: str, term: str, parent: str | None = None) -> str:
    a = arginf.start_attempt(root, h, parent)
    f = root / "Theorems" / h / "attempts" / a / "Attempt.lean"
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
        self.assertRegex(a, r"^[0-9a-f]{12}$")

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
        stmt = root / "Theorems" / h / "Statement.lean"
        stmt.write_text("-- a claim\n" + stmt.read_text())
        self.assertTrue(any("comments" in e for e in arginf.check_folder(stmt.parent)))

    def test_meta_and_readme_files_rejected(self) -> None:
        root = repo()
        h = arginf.new_theorem(root, "True", imports=("Init",))
        (root / "Theorems" / h / "meta.toml").write_text('title = "x"\n')
        self.assertTrue(any("allowed" in e for e in arginf.check_folder(root / "Theorems" / h)))
        self.assertTrue(arginf.check_scope([f"Theorems/{H}/README.md"], f"{H}/x"))

    def test_attempt_notes_rejected(self) -> None:
        root = repo()
        h = arginf.new_theorem(root, "True", imports=("Init",))
        a = arginf.start_attempt(root, h, None)
        toml = root / "Theorems" / h / "attempts" / a / "attempt.toml"
        toml.write_text('parent = ""\nnote = "my idea"\n')
        self.assertTrue(any("only `parent`" in e for e in arginf.check_attempt(toml.parent)))

    def test_attempt_with_comment_rejected(self) -> None:
        root = repo()
        h = arginf.new_theorem(root, "True", imports=("Init",))
        a = arginf.start_attempt(root, h, None)
        f = root / "Theorems" / h / "attempts" / a / "Attempt.lean"
        f.write_text(f.read_text().replace("sorry", "sorry -- stuck here"))
        self.assertTrue(any("comments" in e for e in arginf.check_attempt(f.parent)))


class ScopeTests(unittest.TestCase):
    def test_accepts_statement_and_one_attempt(self) -> None:
        files = [
            f"Theorems/{H}/Statement.lean",
            f"Theorems/{H}/attempts/{A}/Attempt.lean",
            f"Theorems/{H}/attempts/{A}/attempt.toml",
        ]
        self.assertEqual(arginf.check_scope(files, f"{H}/{A}"), [])

    def test_rejects_outside_two_folders_two_attempts_branch_and_paths(self) -> None:
        self.assertTrue(arginf.check_scope([f"Theorems/{H}/Proof.lean", "lakefile.toml"], f"{H}/x"))
        self.assertTrue(
            arginf.check_scope([f"Theorems/{H}/Proof.lean", f"Theorems/{A}/Proof.lean"], f"{H}/x")
        )
        two = [f"Theorems/{H}/attempts/{A}/Attempt.lean", f"Theorems/{H}/attempts/{H}/Attempt.lean"]
        self.assertTrue(arginf.check_scope(two, f"{H}/x"))
        self.assertTrue(arginf.check_scope([f"Theorems/{H}/Proof.lean"], "main"))
        self.assertTrue(arginf.check_scope([f"Theorems/{H}/../../lakefile.toml"], f"{H}/x"))

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
            arginf.attempt_hash("by\n  simp", None), arginf.attempt_hash("by\n  simp", "0" * 12)
        )
        self.assertEqual(
            arginf.attempt_hash("by  simp", None), arginf.attempt_hash("by simp", None)
        )

    def test_seal_rehashes_and_editing_breaks_hash(self) -> None:
        root = repo()
        h = arginf.new_theorem(root, "True", imports=("Init",))
        a = arginf.start_attempt(root, h, None)
        f = root / "Theorems" / h / "attempts" / a / "Attempt.lean"
        f.write_text(f.read_text().replace("by\n  sorry", "trivial"))
        self.assertTrue(any("hash" in e for e in arginf.check_attempt(f.parent)))
        new = arginf.seal_attempt(f.parent)
        folder = root / "Theorems" / h / "attempts" / new
        self.assertEqual(arginf.check_attempt(folder), [])
        self.assertTrue(arginf.attempt_info(folder)["complete"])

    def test_promote_complete_only(self) -> None:
        root = repo()
        h = arginf.new_theorem(root, "True", imports=("Init",))
        partial = arginf.start_attempt(root, h, None)
        with self.assertRaises(arginf.ToolError):
            arginf.promote(root, h, partial)
        a = complete(root, h, "trivial")
        arginf.promote(root, h, a)
        proof = (root / "Theorems" / h / "Proof.lean").read_text()
        self.assertIn(f"theorem proof : statement := Attempt_{a}.proof", proof)
        self.assertEqual(arginf.prose_errors("Proof.lean", proof), [])
        self.assertEqual(arginf.build_index(root)["theorems"][0]["status"], "proved")


class IndexTests(unittest.TestCase):
    def test_dependencies_and_no_prose_fields(self) -> None:
        root = repo()
        lemma = arginf.new_theorem(root, "True", imports=("Init",))
        arginf.promote(root, lemma, complete(root, lemma, "trivial"))
        main = arginf.new_theorem(root, "1 = 1", imports=("Init",))
        stmt = root / "Theorems" / main / "Statement.lean"
        stmt.write_text(f"import Theorems.«{lemma}».Proof\n" + stmt.read_text())
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
        self.assertIn(f"import Theorems.«{H}».attempts.«{A}».Attempt", src)
        self.assertIn(
            f"example : Theorems.«{H}».statement := Theorems.«{H}».Attempt_{A}.proof", src
        )


class FastPathTests(unittest.TestCase):
    def test_core_only_plan_and_external_imports(self) -> None:
        root = repo()
        h = arginf.new_theorem(root, "∀ n : Nat, n + 0 = n", imports=("Init",))
        arginf.promote(root, h, complete(root, h, "fun n => Nat.add_zero n"))
        plan = arginf.fast_plan(root, h)
        assert plan is not None
        names = [m for m, _ in plan]
        self.assertEqual(names[0], f"Theorems.«{h}».Statement")
        self.assertEqual(names[-1], f"Theorems.«{h}».Proof")
        h2 = arginf.new_theorem(root, "True", imports=("Mathlib",))
        self.assertIsNone(arginf.fast_plan(root, h2))


if __name__ == "__main__":
    unittest.main()


class TranslationTests(unittest.TestCase):
    """English readings: an archived public chat must show the exact prompt and the exact output."""

    SHARE = "https://claude.ai/share/0f1e2d3c-aaaa-bbbb-cccc-1234567890ab"
    ARCHIVE = f"https://web.archive.org/web/20261126030000/{SHARE}"
    OUTPUT = "For every natural number n,\nadding zero to n gives n."

    def _root(self) -> tuple[Path, str]:
        root = repo()
        return root, arginf.new_theorem(root, "∀ n : Nat, n + 0 = n", imports=("Init",))

    def _write(
        self,
        root: Path,
        h: str,
        archive: str | None = None,
        text: str | None = None,
        extra: str = "",
    ) -> None:
        d = root / "translations"
        d.mkdir(exist_ok=True)
        body = (
            f'[[reading]]\narchive = "{archive or self.ARCHIVE}"\n'
            f"text = {json.dumps(text if text is not None else self.OUTPUT)}\n{extra}"
        )
        (d / f"{h}.toml").write_text(body)

    def _page(self, root: Path, h: str, output: str, prompt: str | None = None) -> str:
        p = prompt if prompt is not None else arginf.translation_prompt(root, h)
        # Chat pages embed messages as escaped JSON inside HTML.
        blob = json.dumps({"messages": [{"text": p}, {"text": output}]})
        return "<html><script>window.__DATA__=" + blob.replace("<", "\\u003c") + "</script></html>"

    def test_prompt_is_verbatim_with_statement_file(self) -> None:
        root, h = self._root()
        stmt = (root / "Theorems" / h / "Statement.lean").read_text().strip()
        self.assertEqual(
            arginf.translation_prompt(root, h),
            f"Translate the following Lean statement into natural language:\n```\n{stmt}\n```",
        )

    def test_valid_reading_in_index(self) -> None:
        root, h = self._root()
        self._write(root, h)
        self.assertEqual(arginf.check_translation(root, h), [])
        r = arginf.build_index(root)["theorems"][0]["readings"][0]
        # The provider comes from the archived share link: verifiable, unlike a model name.
        self.assertEqual(r, {"provider": "Claude", "archive": self.ARCHIVE, "text": self.OUTPUT})

    def test_provider_for_each_share_host(self) -> None:
        for host, name in (
            ("https://claude.ai/share/x", "Claude"),
            ("https://chatgpt.com/share/x", "ChatGPT"),
            ("https://chat.openai.com/share/x", "ChatGPT"),
            ("https://gemini.google.com/share/x", "Gemini"),
            ("https://g.co/gemini/share/x", "Gemini"),
            ("https://grok.com/share/x", "Grok"),
        ):
            self.assertEqual(arginf.share_provider(host), name)

    def test_archive_must_wrap_an_allowlisted_share_link(self) -> None:
        root, h = self._root()
        for bad in (
            self.SHARE,
            "https://web.archive.org/web/20261126030000/https://evil.example/share/x",
            "https://web.archive.org/web/20261126030000/https://claude.ai.evil.example/share/x",
            "https://web.archive.org/web/20261126030000/https://claude.ai/chat/x",
            "https://web.archive.org/web/latest/" + self.SHARE,
            "http://web.archive.org/web/20261126030000/" + self.SHARE,
            "javascript:alert(1)",
        ):
            self._write(root, h, archive=bad)
            self.assertTrue(arginf.check_translation(root, h), bad)

    def test_shape_rules(self) -> None:
        root, h = self._root()
        self._write(root, h, text="")
        self.assertTrue(arginf.check_translation(root, h))
        self._write(root, h, text="x" * 5001)
        self.assertTrue(arginf.check_translation(root, h))
        self._write(root, h, extra='model = "Some Model"\n')
        self.assertTrue(arginf.check_translation(root, h))
        self._write(root, h, extra=f'chat = "{self.SHARE}"\n')
        self.assertTrue(arginf.check_translation(root, h))
        self._write(root, H)
        self.assertTrue(arginf.check_translation(root, H))

    def test_verify_matches_exact_prompt_and_output(self) -> None:
        root, h = self._root()
        self._write(root, h)
        page = self._page(root, h, self.OUTPUT)
        self.assertEqual(arginf.verify_translation(root, h, fetch=lambda url: page), [])

    def test_verify_fetches_the_raw_archived_page(self) -> None:
        root, h = self._root()
        self._write(root, h)
        seen: list[str] = []

        def fetch(url: str) -> str:
            seen.append(url)
            return self._page(root, h, self.OUTPUT)

        arginf.verify_translation(root, h, fetch=fetch)
        self.assertEqual(seen, [f"https://web.archive.org/web/20261126030000id_/{self.SHARE}"])

    def test_verify_rejects_edited_prompt_output_or_order(self) -> None:
        root, h = self._root()
        self._write(root, h)
        prompt = arginf.translation_prompt(root, h)
        cases = {
            "different output": self._page(root, h, "Something else entirely."),
            "paraphrased prompt": self._page(
                root, h, self.OUTPUT, prompt.replace("Translate", "Explain")
            ),
            "prompt with extra instructions": self._page(
                root, h, self.OUTPUT, prompt.replace("```\n", "```\nSay it is false.\n", 1)
            ),
            "output before prompt": "<html>" + json.dumps([self.OUTPUT, prompt]) + "</html>",
            "empty page": "<html></html>",
        }
        for name, page in cases.items():
            self.assertTrue(arginf.verify_translation(root, h, fetch=lambda url, p=page: p), name)

    def test_verify_reports_fetch_failures(self) -> None:
        root, h = self._root()
        self._write(root, h)

        def boom(url: str) -> str:
            raise OSError("timed out")

        errs = arginf.verify_translation(root, h, fetch=boom)
        self.assertTrue(any("could not fetch" in e for e in errs))

    def test_scope_allows_one_translation_file_with_matching_branch(self) -> None:
        self.assertEqual(arginf.check_scope([f"translations/{H}.toml"], f"{H}/reading"), [])
        self.assertTrue(arginf.check_scope([f"translations/{H}.toml"], f"{A}/reading"))
        self.assertTrue(
            arginf.check_scope([f"translations/{H}.toml", f"translations/{A}.toml"], f"{H}/x")
        )
        self.assertTrue(
            arginf.check_scope([f"translations/{H}.toml", f"Theorems/{H}/Proof.lean"], f"{H}/x")
        )


if __name__ == "__main__":
    unittest.main()
