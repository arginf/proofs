"""Incremental CI (scale design, test plan row "merge cost doesn't grow with the repo").

User story: as the project, a merge costs the same at 10 statements as at 100k. PR, merge-group
and main checks build and audit only the folders a change added; audits are cached by
(attempt, lean-toolchain, lake-manifest); a toolchain or manifest change triggers a full,
sharded, resumable run.
"""

import json
import tempfile
import unittest
from pathlib import Path

from tools import arginf, incremental, layout


def repo(n: int) -> Path:
    """n statements, each with one complete attempt (a proof)."""
    root = Path(tempfile.mkdtemp())
    (root / "Theorems").mkdir()
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.36.0-rc2\n")
    (root / "lake-manifest.json").write_text('{"packages": []}\n')
    for i in range(n):
        h = arginf.new_theorem(root, f"{i} = {i}", imports=("Init",))
        a = arginf.start_attempt(root, h, None)
        f = root / layout.attempt_path(h, a)
        f.write_text(f.read_text().replace("by\n  sorry", "rfl"))
        arginf.seal_attempt(f.parent)
    return root


def add_proof(root: Path, prop: str) -> tuple[str, str, list[str]]:
    """A new statement with a proof, and the paths a PR adding it would change."""
    h = arginf.new_theorem(root, prop, imports=("Init",))
    a = arginf.start_attempt(root, h, None)
    f = root / layout.attempt_path(h, a)
    f.write_text(f.read_text().replace("by\n  sorry", "rfl"))
    a = arginf.seal_attempt(f.parent)
    files = [layout.attempt_path(h, a), layout.attempt_path(h, a, "attempt.toml")]
    return h, a, [layout.statement_path(h), *files]


class Auditor:
    """Stands in for arginf.audit (which needs Lean) and counts the work."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, root: Path, h: str, a: str) -> list[str]:
        self.calls.append((h, a))
        return []


class SameWorkAtAnySize(unittest.TestCase):
    def work(self, n: int) -> tuple[int, int, int]:
        root = repo(n)
        env = incremental.env_key(root)
        cache = incremental.AuditCache()
        # Everything already merged was audited when it merged.
        for h, a in incremental.complete_attempts(root, incremental.all_statements(root)):
            cache.add(h, a, env)
        h, _a, changed = add_proof(root, "424242 = 424242")
        plan = incremental.plan(changed)
        self.assertFalse(plan.full)
        targets = incremental.targets(root, plan.statements)
        auditor = Auditor()
        errors = incremental.audit(root, plan.statements, cache, env, auditor)
        self.assertEqual(errors, [])
        self.assertEqual(plan.statements, [h])
        return len(plan.statements), len(targets), len(auditor.calls)

    def test_a_merge_does_the_same_work_with_1000_folders_as_with_10(self) -> None:
        self.assertEqual(self.work(10), self.work(1000))
        self.assertEqual(self.work(10), (1, 2, 1))  # one statement, its 2 modules, 1 audit


class PlanTests(unittest.TestCase):
    H = "0123456789abcdef0123"
    A = "fedcba98765432100123"

    def test_changed_statements_from_a_diff(self) -> None:
        changed = [
            layout.statement_path(self.H),
            layout.attempt_path(self.H, self.A),
            layout.attempt_path(self.H, self.A, "attempt.toml"),
            "README.md",
        ]
        plan = incremental.plan(changed)
        self.assertEqual(plan.statements, [self.H])
        self.assertFalse(plan.full)
        self.assertFalse(plan.lexical_full)

    def test_toolchain_manifest_or_lakefile_trigger_a_full_run(self) -> None:
        for f in ("lean-toolchain", "lake-manifest.json", "lakefile.toml"):
            plan = incremental.plan([f])
            self.assertTrue(plan.full, f)
            self.assertIn(f, " ".join(plan.reasons))

    def test_checking_tools_trigger_a_full_lexical_check_only(self) -> None:
        for f in ("tools/safety.py", "tools/arginf.py", "tools/layout.py"):
            plan = incremental.plan([f])
            self.assertTrue(plan.lexical_full, f)
            self.assertFalse(plan.full, f)
        self.assertFalse(incremental.plan(["tools/readings.py"]).lexical_full)

    def test_targets_are_lake_module_targets_for_those_statements_only(self) -> None:
        root = repo(3)
        h, a, _ = add_proof(root, "7 = 7")
        self.assertEqual(
            incremental.targets(root, [h]),
            [f"+{layout.statement_module(h)}", f"+{layout.attempt_module(h, a)}"],
        )
        self.assertEqual(incremental.targets(root, []), [])


class CacheKeyTests(unittest.TestCase):
    def test_key_changes_with_toolchain_or_manifest(self) -> None:
        root = repo(1)
        k0 = incremental.env_key(root)
        self.assertEqual(incremental.env_key(root), k0)
        (root / "lean-toolchain").write_text("leanprover/lean4:v4.37.0\n")
        k1 = incremental.env_key(root)
        (root / "lake-manifest.json").write_text('{"packages": [1]}\n')
        k2 = incremental.env_key(root)
        self.assertEqual(len({k0, k1, k2}), 3)

    def test_cached_audits_are_skipped_only_under_the_same_key(self) -> None:
        root = repo(2)
        env = incremental.env_key(root)
        stmts = incremental.all_statements(root)
        cache = incremental.AuditCache()
        first = Auditor()
        incremental.audit(root, stmts, cache, env, first)
        self.assertEqual(len(first.calls), 2)
        again = Auditor()
        incremental.audit(root, stmts, cache, env, again)
        self.assertEqual(again.calls, [])
        (root / "lean-toolchain").write_text("leanprover/lean4:v4.37.0\n")
        bumped = Auditor()
        incremental.audit(root, stmts, cache, incremental.env_key(root), bumped)
        self.assertEqual(len(bumped.calls), 2)

    def test_failures_are_not_cached(self) -> None:
        root = repo(1)
        env = incremental.env_key(root)
        cache = incremental.AuditCache()
        errors = incremental.audit(
            root, incremental.all_statements(root), cache, env, lambda r, h, a: ["bad"]
        )
        self.assertEqual(errors, ["bad"])
        self.assertEqual(cache.size(), 0)

    def test_cache_file_round_trip_and_small_shape(self) -> None:
        cache = incremental.AuditCache()
        cache.add("0" * 20, "1" * 20, "e1")
        cache.add("0" * 20, "2" * 20, "e1")
        cache.add("3" * 20, "4" * 20, "e2")
        path = Path(tempfile.mkdtemp()) / "audit-cache.json"
        cache.save(path)
        data = json.loads(path.read_text())
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["audited"]["e1"], ["1" * 20, "2" * 20])
        loaded = incremental.AuditCache.load(path)
        self.assertTrue(loaded.done("2" * 20, "e1"))
        self.assertFalse(loaded.done("2" * 20, "e2"))
        self.assertEqual(incremental.AuditCache.load(path.parent / "missing.json").size(), 0)

    def test_corrupt_cache_counts_as_empty(self) -> None:
        path = Path(tempfile.mkdtemp()) / "audit-cache.json"
        for bad in ("not json", '{"version": 9}', '{"version": 1, "audited": {"e": "x"}}'):
            path.write_text(bad)
            self.assertEqual(incremental.AuditCache.load(path).size(), 0, bad)


class FullRunTests(unittest.TestCase):
    """The full run is a matrix over shards, capped per run, and resumes where it stopped."""

    def test_matrix_covers_pending_work_by_shard_within_caps(self) -> None:
        root = repo(40)
        env = incremental.env_key(root)
        cache = incremental.AuditCache()
        jobs = incremental.matrix(root, cache, env, max_jobs=4, max_modules=6)
        self.assertLessEqual(len(jobs), 4)
        for job in jobs:
            stmts = job["statements"].split()
            self.assertLessEqual(len(incremental.targets(root, stmts)), 6)
            # A job never splits a statement, and its statements are in shard order.
            self.assertEqual(stmts, sorted(stmts))
        covered = [h for j in jobs for h in j["statements"].split()]
        self.assertEqual(len(covered), len(set(covered)))
        self.assertLess(len(covered), 40)  # capped: the rest waits for the next run

    def test_next_run_resumes_with_what_is_left(self) -> None:
        root = repo(12)
        env = incremental.env_key(root)
        cache = incremental.AuditCache()
        done: list[str] = []
        for _ in range(10):
            jobs = incremental.matrix(root, cache, env, max_jobs=2, max_modules=4)
            if not jobs:
                break
            for job in jobs:
                stmts = job["statements"].split()
                incremental.audit(root, stmts, cache, env, Auditor())
                incremental.mark_built(cache, root, stmts, env)
                done += stmts
        self.assertEqual(sorted(done), incremental.all_statements(root))
        self.assertEqual(incremental.matrix(root, cache, env, 2, 4), [])

    def test_a_toolchain_change_makes_everything_pending_again(self) -> None:
        root = repo(5)
        env = incremental.env_key(root)
        cache = incremental.AuditCache()
        stmts = incremental.all_statements(root)
        incremental.audit(root, stmts, cache, env, Auditor())
        incremental.mark_built(cache, root, stmts, env)
        self.assertEqual(incremental.matrix(root, cache, env, 8, 100), [])
        (root / "lean-toolchain").write_text("leanprover/lean4:v4.37.0\n")
        jobs = incremental.matrix(root, cache, incremental.env_key(root), 8, 100)
        self.assertEqual(sorted(h for j in jobs for h in j["statements"].split()), stmts)

    def test_statements_without_proofs_are_still_built_once_per_key(self) -> None:
        root = repo(0)
        h = arginf.new_theorem(root, "5 = 5", imports=("Init",))
        env = incremental.env_key(root)
        cache = incremental.AuditCache()
        jobs = incremental.matrix(root, cache, env, 1, 10)
        self.assertEqual([j["statements"] for j in jobs], [h])
        incremental.mark_built(cache, root, [h], env)
        self.assertEqual(incremental.matrix(root, cache, env, 1, 10), [])


class MergeCacheTests(unittest.TestCase):
    """Each full-run job writes its own cache part; the record job merges them."""

    def test_merge_keeps_every_pass_and_only_the_current_environment(self) -> None:
        root = repo(1)
        env = incremental.env_key(root)
        d = Path(tempfile.mkdtemp())
        a, b, old = incremental.AuditCache(), incremental.AuditCache(), incremental.AuditCache()
        a.add("0" * 20, "1" * 20, env)
        b.add("0" * 20, "2" * 20, env)
        b.add_built("0" * 20, env)
        old.add("0" * 20, "3" * 20, "stale-env")
        for name, c in (("a", a), ("b", b), ("old", old)):
            c.save(d / f"{name}.json")
        (d / "junk.json").write_text("not json")
        import contextlib
        import io
        import os

        cwd = os.getcwd()
        os.chdir(root)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = incremental.main(
                    ["merge-cache", "--out", str(d / "out.json")]
                    + [str(d / f"{n}.json") for n in ("a", "b", "old", "junk")]
                )
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0)
        out = incremental.AuditCache.load(d / "out.json")
        self.assertTrue(out.done("1" * 20, env) and out.done("2" * 20, env))
        self.assertTrue(out.built("0" * 20, env))
        self.assertFalse(out.done("3" * 20, "stale-env"))
        self.assertEqual(out.size(), 3)


if __name__ == "__main__":
    unittest.main()
