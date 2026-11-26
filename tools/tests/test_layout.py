"""Sharded layout and 20-digit hashes (scale design, test plan row 1).

User story: as a contributor, `arginf.py new` and `attempt` put my files in the right shard,
and CI rejects a wrong shard, a wrong depth or a wrong hash length wherever paths are checked.
"""

import tempfile
import unittest
from pathlib import Path

from tools import arginf, automerge, layout, safety

H = "0123456789abcdef0123"  # 20 hex digits
A = "fedcba98765432100123"
OLD = "0123456789ab"  # 12 digits: the legacy length


def st(h: str = H) -> str:
    return f"Theorems/{h[:2]}/{h}/Statement.lean"


def at(h: str = H, a: str = A, name: str = "Attempt.lean") -> str:
    return f"Theorems/{h[:2]}/{h}/attempts/{a[:2]}/{a}/{name}"


class ConstantsTests(unittest.TestCase):
    def test_hash_length_is_20_everywhere(self) -> None:
        self.assertEqual(layout.HASH_LEN, 20)
        self.assertEqual(arginf.HASH_LEN, 20)
        self.assertRegex(arginf.statement_hash("def statement : Prop := True"), r"^[0-9a-f]{20}$")
        self.assertRegex(arginf.attempt_hash("trivial", None), r"^[0-9a-f]{20}$")
        self.assertTrue(layout.HASH_RE.match(H))
        for bad in (OLD, H + "0", H.upper(), "g" * 20):
            self.assertFalse(layout.HASH_RE.match(bad), bad)

    def test_new_hash_extends_the_old_one(self) -> None:
        # Same SHA-256, longer prefix: a statement's 12-digit hash is the start of its new one.
        import hashlib

        full = hashlib.sha256(b"True").hexdigest()
        self.assertEqual(arginf.statement_hash("def statement : Prop := True"), full[:20])


class PathTests(unittest.TestCase):
    def test_paths_and_modules_come_from_the_hash(self) -> None:
        self.assertEqual(layout.statement_path(H), st())
        self.assertEqual(layout.attempt_path(H, A), at())
        self.assertEqual(layout.attempt_path(H, A, "attempt.toml"), at(name="attempt.toml"))
        self.assertEqual(layout.statement_module(H), f"Theorems.«01».«{H}».Statement")
        self.assertEqual(
            layout.attempt_module(H, A), f"Theorems.«01».«{H}».attempts.«fe».«{A}».Attempt"
        )

    def test_fixed_depth(self) -> None:
        self.assertEqual(len(Path(st()).parts), 4)
        self.assertEqual(len(Path(at()).parts), 7)

    def test_parse_accepts_exactly_the_two_shapes(self) -> None:
        self.assertEqual(
            layout.parse_path(st()), layout.Parsed("statement", H, None, "Statement.lean")
        )
        self.assertEqual(layout.parse_path(at()), layout.Parsed("attempt", H, A, "Attempt.lean"))

    def test_parse_rejects_wrong_shard_depth_and_length(self) -> None:
        bad = [
            f"Theorems/{H}/Statement.lean",  # legacy flat
            f"Theorems/ff/{H}/Statement.lean",  # wrong shard
            f"Theorems/01/{H}/attempts/{A}/Attempt.lean",  # attempt not sharded
            f"Theorems/01/{H}/attempts/00/{A}/Attempt.lean",  # wrong attempt shard
            f"Theorems/01/{H}/x/Statement.lean",  # too deep
            f"Theorems/01/{H}",  # too shallow
            f"Theorems/{OLD[:2]}/{OLD}/Statement.lean",  # 12 digits
            f"Theorems/01/{H}/attempts/fe/{A}/extra/Attempt.lean",
            f"Theorems/01/{H}/../../x/Statement.lean",
            f"/Theorems/01/{H}/Statement.lean",
            f"Other/01/{H}/Statement.lean",
        ]
        for raw in bad:
            self.assertIsInstance(layout.parse_path(raw), str, raw)


class ScopeTests(unittest.TestCase):
    def test_accepts_statement_or_one_attempt(self) -> None:
        self.assertEqual(arginf.check_scope([st()], f"{H}/x"), [])
        self.assertEqual(arginf.check_scope([at(), at(name="attempt.toml")], f"{H}/x"), [])

    def test_rejects_wrong_shard_depth_length_and_files(self) -> None:
        for raw in (
            f"Theorems/ff/{H}/Statement.lean",
            f"Theorems/01/{H}/attempts/{A}/Attempt.lean",
            f"Theorems/{OLD}/Statement.lean",
            f"Theorems/01/{H}/README.md",
            at(name="notes.md"),
        ):
            self.assertTrue(arginf.check_scope([raw], f"{H}/x"), raw)

    def test_one_folder_one_attempt_and_branch(self) -> None:
        other = "ab" + H[2:]
        self.assertTrue(arginf.check_scope([st(), st(other)], f"{H}/x"))
        self.assertTrue(arginf.check_scope([at(), at(a="00" + A[2:])], f"{H}/x"))
        self.assertTrue(arginf.check_scope([st()], "main"))


class AutomergeTests(unittest.TestCase):
    STATEMENT = (
        f"import Init\n\nnamespace Theorems.«{H}»\n\ndef statement : Prop :=\n  True\n\n"
        f"end Theorems.«{H}»\n"
    )

    def decide(self, names: list[str], lean: str, exists: bool) -> automerge.Decision:
        files = [{"filename": n, "status": "added"} for n in names]
        return automerge.decide(
            files,
            statement_exists=lambda h: exists,
            attempt_lean=lambda p: lean,
            recent_statements=lambda: 0,
            recent_checkpoints=lambda: 0,
            recent_proofs=lambda: 0,
            changed_files=len(files),
            account_age_days=lambda: 365.0,
        )

    def test_reads_sharded_paths(self) -> None:
        d = self.decide([st()], self.STATEMENT, exists=False)
        self.assertTrue(d.merge, d.reason)
        self.assertEqual(d.theorem, H)

    def test_rejects_wrong_shard_depth_and_length(self) -> None:
        for raw in (
            f"Theorems/ff/{H}/Statement.lean",
            f"Theorems/{H}/Statement.lean",
            f"Theorems/{OLD[:2]}/{OLD}/Statement.lean",
        ):
            self.assertFalse(self.decide([raw], self.STATEMENT, exists=False).merge, raw)
        flat = [
            f"Theorems/01/{H}/attempts/{A}/Attempt.lean",
            f"Theorems/01/{H}/attempts/{A}/attempt.toml",
        ]
        self.assertFalse(self.decide(flat, "", exists=True).merge)

    def test_statement_exists_is_asked_about_the_sharded_path(self) -> None:
        asked: list[str] = []
        files = [{"filename": st(), "status": "added"}]
        automerge.decide(
            files,
            statement_exists=lambda h: asked.append(h) or False,
            attempt_lean=lambda p: asked.append(p) or self.STATEMENT,
            recent_statements=lambda: 0,
            recent_checkpoints=lambda: 0,
            recent_proofs=lambda: 0,
            changed_files=1,
            account_age_days=lambda: 365.0,
        )
        self.assertIn(st(), asked)


class SafetyTests(unittest.TestCase):
    def test_theorem_imports_must_be_sharded_and_20_digits(self) -> None:
        ok = [
            f"import {layout.statement_module(H)}",
            f"import {layout.attempt_module(H, A)}",
        ]
        bad = [
            f"import Theorems.«{H}».Statement",  # legacy unsharded
            f"import Theorems.«ff».«{H}».Statement",  # wrong shard
            f"import Theorems.«01».«{H}».attempts.«00».«{A}».Attempt",  # wrong attempt shard
            f"import Theorems.«{OLD[:2]}».«{OLD}».Statement",  # 12 digits
        ]
        proof = f"theorem proof : Theorems.«{H}».statement := trivial"
        ns = f"Theorems.«{H}».Attempt_{A}"
        for imp in ok:
            text = f"{imp}\n\nnamespace {ns}\n\n{proof}\n\nend {ns}\n"
            self.assertFalse([e for e in safety.attempt_errors("A", text) if "import" in e], imp)
        for imp in bad:
            text = f"{imp}\n\nnamespace {ns}\n\n{proof}\n\nend {ns}\n"
            self.assertTrue([e for e in safety.attempt_errors("A", text) if "import" in e], imp)


class ToolTests(unittest.TestCase):
    def repo(self) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / "Theorems").mkdir()
        return root

    def test_new_and_attempt_write_into_the_right_shard(self) -> None:
        root = self.repo()
        h = arginf.new_theorem(root, "True", imports=("Init",))
        self.assertTrue((root / layout.statement_path(h)).is_file())
        text = (root / layout.statement_path(h)).read_text()
        self.assertIn(f"namespace Theorems.«{h}»", text)  # namespaces are not sharded
        a = arginf.start_attempt(root, h, None)
        f = root / layout.attempt_path(h, a)
        f.write_text(f.read_text().replace("by\n  sorry", "trivial"))
        new = arginf.seal_attempt(f.parent)
        folder = root / layout.attempt_path(h, new)
        self.assertTrue(folder.is_file())
        lean = folder.read_text()
        self.assertTrue(lean.startswith(f"import {layout.statement_module(h)}\n"))
        self.assertIn(f"namespace Theorems.«{h}».Attempt_{new}", lean)
        self.assertEqual(arginf.check_attempt(folder.parent), [])
        self.assertEqual(arginf.check_all(root), [])

    def test_continuation_finds_its_parent_in_another_shard(self) -> None:
        root = self.repo()
        h = arginf.new_theorem(root, "∀ n : Nat, n + 0 = n", imports=("Init",))
        p = arginf.start_attempt(root, h, None)
        f = root / layout.attempt_path(h, p)
        f.write_text(f.read_text().replace("by\n  sorry", "by\n  intro n\n  sorry"))
        p = arginf.seal_attempt(f.parent)
        c = arginf.start_attempt(root, h, p)
        cf = root / layout.attempt_path(h, c)
        cf.write_text(cf.read_text().replace("intro n\n  sorry", "intro n\n  rfl"))
        c = arginf.seal_attempt(cf.parent)
        self.assertEqual(arginf.check_attempt((root / layout.attempt_path(h, c)).parent), [])
        tree = {x["hash"]: x for x in arginf.build_index(root)["theorems"][0]["attempts"]}
        self.assertEqual(tree[c]["parent"], p)
        self.assertEqual(tree[p]["children"], [c])

    def test_check_all_rejects_a_folder_in_the_wrong_shard(self) -> None:
        root = self.repo()
        h = arginf.new_theorem(root, "True", imports=("Init",))
        src = root / layout.statement_path(h)
        wrong = root / "Theorems" / ("ff" if h[:2] != "ff" else "00") / h
        wrong.mkdir(parents=True)
        src.rename(wrong / "Statement.lean")
        self.assertTrue(any("shard" in e for e in arginf.check_all(root)), arginf.check_all(root))

    def test_check_all_rejects_a_legacy_flat_folder(self) -> None:
        root = self.repo()
        (root / "Theorems" / OLD).mkdir()
        self.assertTrue(arginf.check_all(root))

    def test_dependencies_follow_sharded_imports(self) -> None:
        root = self.repo()
        lemma = arginf.new_theorem(root, "True", imports=("Init",))
        a = arginf.start_attempt(root, lemma, None)
        f = root / layout.attempt_path(lemma, a)
        f.write_text(f.read_text().replace("by\n  sorry", "trivial"))
        la = arginf.seal_attempt(f.parent)
        main = arginf.new_theorem(root, "1 = 1", imports=("Init",))
        stmt = root / layout.statement_path(main)
        stmt.write_text(f"import {layout.attempt_module(lemma, la)}\n" + stmt.read_text())
        by = {t["hash"]: t for t in arginf.build_index(root)["theorems"]}
        self.assertEqual(by[main]["dependencies"], [lemma])
        self.assertEqual(
            arginf.theorem_modules(root, [lemma]),
            [layout.statement_module(lemma), layout.attempt_module(lemma, la)],
        )
        plan = arginf.fast_plan(root, main)
        assert plan is not None
        self.assertIn(layout.attempt_module(lemma, la), [m for m, _ in plan])


class EarnedCreditTests(unittest.TestCase):
    def test_old_12_digit_titles_still_count_through_the_rename_map(self) -> None:
        stmt_new, att_new = H, A
        renamed = {OLD: stmt_new, "a" * 12: att_new}
        titles = [
            f"Merge #1: statement {OLD} by @alice",
            f"Merge #2: proof {'a' * 12} by @bob",
        ]
        where = {att_new: stmt_new}
        self.assertEqual(automerge.earned_proofs(titles, where.get, "bob", renamed), 1)
        # New 20-digit titles parse too.
        titles20 = [f"Merge #3: statement {H} by @alice", f"Merge #4: proof {A} by @bob"]
        self.assertEqual(automerge.earned_proofs(titles20, where.get, "bob"), 1)


class DirectoryWidthTests(unittest.TestCase):
    """GitHub recommends at most 3,000 entries per directory (scale design: Constraints)."""

    def test_no_directory_over_3000_entries_in_a_synthetic_catalog(self) -> None:
        import hashlib
        from collections import Counter

        # 100k statements and 1M attempts are too many files for a unit test, so count the
        # directory entries their paths would create instead.
        width: Counter[str] = Counter()
        seen: set[str] = set()
        for i in range(100_000):
            h = hashlib.sha256(f"s{i}".encode()).hexdigest()[:20]
            for d, child in (("Theorems", h[:2]), (f"Theorems/{h[:2]}", h)):
                if (d, child) not in seen:
                    seen.add((d, child))  # type: ignore[arg-type]
                    width[d] += 1
        # One very popular statement with 600k attempts. Shard fill is random: the fullest of
        # 256 shards passes 3,000 at about 700k entries (average ~2,730 plus 4 sigma), not 768k.
        h = H
        for i in range(600_000):
            a = hashlib.sha256(f"a{i}".encode()).hexdigest()[:20]
            d1 = f"Theorems/01/{h}/attempts"
            for d, child in ((d1, a[:2]), (f"{d1}/{a[:2]}", a)):
                if (d, child) not in seen:
                    seen.add((d, child))  # type: ignore[arg-type]
                    width[d] += 1
        self.assertLessEqual(max(width.values()), 3000, width.most_common(3))
        self.assertEqual(width["Theorems"], 256)


if __name__ == "__main__":
    unittest.main()


class LemmaImportTests(unittest.TestCase):
    """An attempt that imports another statement's proof keeps every import first when sealed."""

    def test_seal_keeps_lemma_imports_above_the_namespace(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "Theorems").mkdir()
        lemma = arginf.new_theorem(root, "True", imports=("Init",))
        a = arginf.start_attempt(root, lemma, None)
        f = root / layout.attempt_path(lemma, a)
        f.write_text(f.read_text().replace("by\n  sorry", "trivial"))
        la = arginf.seal_attempt(f.parent)
        h = arginf.new_theorem(root, "1 = 1", imports=("Init",))
        b = arginf.start_attempt(root, h, None)
        g = root / layout.attempt_path(h, b)
        g.write_text(
            f"import {layout.attempt_module(lemma, la)}\n"
            + g.read_text().replace(
                "by\n  sorry", f"by\n  have := Theorems.«{lemma}».Attempt_{la}.proof\n  rfl"
            )
        )
        sealed = arginf.seal_attempt(g.parent)
        text = (root / layout.attempt_path(h, sealed)).read_text()
        head = text.split("\nnamespace ", 1)[0]
        self.assertIn(f"import {layout.attempt_module(lemma, la)}", head)
        self.assertNotIn("import", text.split("\nnamespace ", 1)[1])
        self.assertEqual(arginf.check_attempt((root / layout.attempt_path(h, sealed)).parent), [])
        self.assertEqual(arginf.check_all(root), [])


class PathCommandTests(unittest.TestCase):
    """`arginf.py path <hash> [<attempt>]` prints the folder, so contributors never type a shard."""

    def run_cli(self, *args: str) -> tuple[int, str]:
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = arginf.main(["path", *args])
        return code, out.getvalue().strip()

    def test_prints_statement_and_attempt_folders(self) -> None:
        self.assertEqual(self.run_cli(H), (0, f"Theorems/01/{H}"))
        self.assertEqual(self.run_cli(H, A), (0, f"Theorems/01/{H}/attempts/fe/{A}"))

    def test_rejects_anything_but_hashes(self) -> None:
        for bad in (OLD, "../x", H.upper()):
            self.assertEqual(self.run_cli(bad)[0], 1, bad)
        self.assertEqual(self.run_cli(H, "draft_x")[0], 1)


class RenameMapTests(unittest.TestCase):
    """The committed old-to-new map keeps history attached to migrated attempts."""

    MAP = {
        "statements": {OLD: H},
        "attempts": {"a" * 12: A},
        "attemptStatement": {A: H},
    }

    def test_provenance_asks_git_about_the_old_path(self) -> None:
        from tools import provenance

        self.assertEqual(
            provenance.legacy_paths(self.MAP),
            {(H, A): f"Theorems/{OLD}/attempts/{'a' * 12}/Attempt.lean"},
        )
        first = provenance._git_first_commit(Path("."), provenance.legacy_paths(self.MAP))
        import subprocess
        from unittest import mock

        with mock.patch.object(subprocess, "run") as run:
            run.return_value.stdout = "s1 2026-11-26T00:00:00Z\n"
            self.assertEqual(first(H, A), ("s1", "2026-11-26T00:00:00Z"))
            self.assertEqual(
                run.call_args.args[0][-1], f"Theorems/{OLD}/attempts/{'a' * 12}/Attempt.lean"
            )
            first(H, "b" * 20)  # not migrated: its own sharded path
            self.assertEqual(run.call_args.args[0][-1], layout.attempt_path(H, "b" * 20))

    def test_automerge_loads_the_committed_map(self) -> None:
        import json

        root = Path(tempfile.mkdtemp())
        self.assertEqual(automerge.load_renames(root), {})
        (root / "migrations").mkdir()
        (root / automerge.RENAME_FILE).write_text(json.dumps(self.MAP))
        self.assertEqual(automerge.load_renames(root), {OLD: H, "a" * 12: A})

    def test_the_repo_map_matches_the_tree(self) -> None:
        import json

        repo = Path(__file__).resolve().parents[2]
        path = repo / automerge.RENAME_FILE
        if not path.is_file():
            self.skipTest("no rename map")
        data = json.loads(path.read_text())
        for new in data["statements"].values():
            self.assertTrue(layout.statement_dir(repo, new).is_dir(), new)
        for a, h in data["attemptStatement"].items():
            self.assertTrue(layout.attempt_dir(repo, h, a).is_dir(), a)
