"""Incremental publishing to the data branch (scale design: Published data, CI and merge rate).

User stories:
- As the project, a publish costs the same at 10 statements as at 1,000: only changed
  statements (and the statements they import, whose dependents change) are rebuilt; everything
  else is carried forward by content hash.
- As GitHub, publishing makes at most one push per run and runs at most every 6 minutes.
- As a visitor, a file I may still be fetching isn't deleted until the next publish.
"""

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from tools import arginf, data, layout, publish


def repo(n: int) -> Path:
    root = Path(tempfile.mkdtemp())
    (root / "Theorems").mkdir()
    for i in range(n):
        h = arginf.new_theorem(root, f"{i} = {i}", imports=("Init",))
        a = arginf.start_attempt(root, h, None)
        f = root / layout.attempt_path(h, a)
        f.write_text(f.read_text().replace("by\n  sorry", "rfl"))
        arginf.seal_attempt(f.parent)
    return root


def new_statement(
    root: Path, prop: str, imports: tuple[str, ...] = ("Init",)
) -> tuple[str, list[str]]:
    h = arginf.new_theorem(root, prop, imports=imports)
    return h, [layout.statement_path(h)]


class Counting:
    def __init__(self) -> None:
        self.records: list[str] = []
        self.provenance: list[str] = []

    def record(self, root: Path, h: str, curation: Any = None) -> dict[str, Any]:
        self.records.append(h)
        return arginf.statement_record(root, h, curation)

    def prov(self, h: str, attempts: list[str]) -> dict[str, dict[str, Any]]:
        self.provenance += attempts
        return {
            a: {
                "mergedAt": "2026-11-27T00:00:00Z",
                "commit": "s",
                "pr": 1,
                "author": "alice",
                "upvotes": 0,
            }
            for a in attempts
        }


def build(
    root: Path, out: Path, changed: list[str] | None, spy: Counting, **kw: Any
) -> publish.Result:
    return publish.build(
        root,
        out,
        commit="c" * 40,
        changed=changed,
        record_fn=spy.record,
        provenance_fn=spy.prov,
        **kw,
    )


def manifest(out: Path) -> dict[str, Any]:
    return json.loads((out / "manifest.json").read_text())


def summary_rows(out: Path) -> dict[str, dict[str, Any]]:
    m = manifest(out)
    rows: dict[str, dict[str, Any]] = {}
    for name, path in m["files"].items():
        if name.startswith("summary/"):
            shard = json.loads((out / path).read_text())
            rows.update({r[0]: dict(zip(shard["cols"], r, strict=True)) for r in shard["rows"]})
    return rows


def detail(out: Path, h: str) -> dict[str, Any]:
    return json.loads((out / data.detail_path(h, summary_rows(out)[h]["detail"])).read_text())


class FirstPublishTests(unittest.TestCase):
    def test_builds_everything_once(self) -> None:
        root, out, spy = repo(5), Path(tempfile.mkdtemp()), Counting()
        result = build(root, out, None, spy)
        self.assertTrue(result.full)
        self.assertEqual(len(spy.records), 5)
        self.assertEqual(manifest(out)["counts"]["statements"], 5)
        self.assertEqual(len(summary_rows(out)), 5)
        self.assertEqual(len(spy.provenance), 5)
        h = sorted(summary_rows(out))[0]
        self.assertEqual(detail(out, h)["attempts"][0]["author"], "alice")


class IncrementalTests(unittest.TestCase):
    def work(self, n: int) -> tuple[int, int]:
        root, out = repo(n), Path(tempfile.mkdtemp())
        build(root, out, None, Counting())
        _h, changed = new_statement(root, "999999 = 999999")
        spy = Counting()
        result = build(root, out, changed, spy)
        self.assertFalse(result.full)
        return len(spy.records), len(spy.provenance)

    def test_a_publish_rebuilds_the_same_amount_at_10_and_at_200(self) -> None:
        self.assertEqual(self.work(10), self.work(200))
        self.assertEqual(self.work(10), (1, 0))  # one statement, no attempts yet

    def test_unchanged_shards_are_carried_forward_byte_for_byte(self) -> None:
        root, out = repo(40), Path(tempfile.mkdtemp())
        build(root, out, None, Counting())
        before = {
            p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()
        }
        h, changed = new_statement(root, "777 = 777")
        result = build(root, out, changed, Counting())
        after = {
            p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()
        }
        touched = {p for p in after if before.get(p) != after[p]}
        for p in touched:
            self.assertTrue(
                p.startswith(
                    (
                        f"summary/{h[:2]}",
                        f"statements/{h[:2]}/",
                        "search",
                        "graph/",
                        "manifest",
                        "meta/",
                    )
                ),
                p,
            )
        self.assertEqual(
            set(result.written) | {"manifest.json"},
            {p for p in touched if not p.startswith("meta/")} | {"manifest.json"},
        )
        self.assertEqual(manifest(out)["counts"]["statements"], 41)

    def test_importing_a_lemma_updates_the_lemmas_dependents(self) -> None:
        root, out = repo(3), Path(tempfile.mkdtemp())
        build(root, out, None, Counting())
        lemma = sorted(summary_rows(out))[0]
        a = detail(out, lemma)["attempts"][0]["hash"]
        h, changed = new_statement(root, "4 = 4")
        stmt = root / layout.statement_path(h)
        stmt.write_text(f"import {layout.attempt_module(lemma, a)}\n" + stmt.read_text())
        spy = Counting()
        build(root, out, changed, spy)
        self.assertEqual(sorted(spy.records), sorted([h, lemma]))
        self.assertEqual(detail(out, lemma)["dependents"], [h])
        self.assertEqual(summary_rows(out)[lemma]["dependents"], 1)
        self.assertEqual(summary_rows(out)[h]["deps"], [lemma])

    def test_provenance_is_carried_forward_and_only_new_attempts_are_looked_up(self) -> None:
        root, out = repo(2), Path(tempfile.mkdtemp())
        build(root, out, None, Counting())
        h = sorted(summary_rows(out))[0]
        a = arginf.start_attempt(root, h, None)
        f = root / layout.attempt_path(h, a)
        f.write_text(f.read_text().replace("by\n  sorry", "by\n  rfl"))
        a = arginf.seal_attempt(f.parent)
        spy = Counting()
        build(root, out, [layout.attempt_path(h, a)], spy)
        self.assertEqual(spy.provenance, [a])
        self.assertEqual(len(detail(out, h)["attempts"]), 2)
        self.assertTrue(all(x.get("author") == "alice" for x in detail(out, h)["attempts"]))

    def test_curation_change_rebuilds_everything(self) -> None:
        root, out = repo(3), Path(tempfile.mkdtemp())
        build(root, out, None, Counting())
        h = sorted(summary_rows(out))[1]
        (root / "curation.toml").write_text(f'featured = ["{h}"]\n')
        spy = Counting()
        self.assertTrue(build(root, out, ["curation.toml"], spy).full)
        self.assertEqual(summary_rows(out)[h]["flags"], data.FLAG_FEATURED)

    def test_superseded_files_are_retired_on_the_next_publish(self) -> None:
        root, out = repo(3), Path(tempfile.mkdtemp())
        build(root, out, None, Counting())
        h = sorted(summary_rows(out))[0]
        old_detail = data.detail_path(h, summary_rows(out)[h]["detail"])
        old_summary = manifest(out)["files"][f"summary/{h[:2]}"]
        # A new attempt on an existing statement replaces its detail file and summary shard.
        a = arginf.start_attempt(root, h, None)
        f = root / layout.attempt_path(h, a)
        f.write_text(f.read_text().replace("by\n  sorry", "by\n  rfl"))
        a = arginf.seal_attempt(f.parent)
        first = build(root, out, [layout.attempt_path(h, a)], Counting())
        superseded = {old_detail, old_summary}
        self.assertTrue(superseded <= set(first.retired))
        for p in superseded:  # still there: someone may hold the old manifest
            self.assertTrue((out / p).is_file(), p)
        build(root, out, [], Counting())
        for p in superseded:
            self.assertFalse((out / p).exists(), p)

    def test_readings_and_areas_come_from_the_sharded_caches(self) -> None:
        root, out = repo(2), Path(tempfile.mkdtemp())
        h = sorted(p.name for p in layout.statement_dirs(root))[0]
        sha = "d" * 64
        (root / "models.toml").write_text(
            '[runtime]\nname = "llama.cpp"\nversion = "b1"\nurl = "https://github.com/ggml-org/llama.cpp/releases/download/b1/x.tgz"\n'
            f'sha256 = "{"e" * 64}"\n\n[[model]]\nname = "M"\nlicense = "Apache-2.0"\n'
            'url = "https://huggingface.co/x/y/resolve/abc/m.gguf"\n'
            f'sha256 = "{sha}"\ntemplate = "chatml"\n'
        )
        reading = {
            "hash": h,
            "text": "For all n, n = n.",
            "model": "M",
            "modelSha256": sha,
            "runtime": "r",
            "cpu": "c",
            "promptSha256": "a" * 64,
            "truncated": False,
        }
        area = {
            "hash": h,
            "areas": ["math.LO"],
            "model": "M",
            "modelSha256": sha,
            "runtime": "r",
            "cpu": "c",
            "promptSha256": "b" * 64,
        }
        publish.write_cache(out / "cache" / "readings", [reading])
        publish.write_cache(out / "cache" / "areas", [area])
        build(root, out, None, Counting())
        d = detail(out, h)
        self.assertEqual(d["readings"][0]["text"], "For all n, n = n.")
        self.assertEqual(d["areas"]["codes"], ["math.LO"])
        self.assertIn("math.LO", [a["code"] for a in manifest(out)["areaList"]])
        # Enrichment of one statement later: only that statement is rebuilt.
        reading2 = {**reading, "text": "Every n equals itself."}
        publish.write_cache(out / "cache" / "readings", [reading2])
        spy = Counting()
        build(root, out, [], spy, also=[h])
        self.assertEqual(spy.records, [h])
        self.assertEqual(detail(out, h)["readings"][0]["text"], "Every n equals itself.")

    def test_extra_files_are_listed_in_the_manifest(self) -> None:
        root, out = repo(1), Path(tempfile.mkdtemp())
        build(root, out, None, Counting(), extra={"reviews": b'{"pulls":[]}', "ledger": b"[]"})
        files = manifest(out)["files"]
        self.assertEqual((out / files["reviews"]).read_bytes(), b'{"pulls":[]}')
        self.assertEqual((out / files["ledger"]).read_bytes(), b"[]")


class TokenSearchIncrementalTests(unittest.TestCase):
    def test_switches_to_tokens_and_updates_only_touched_shards(self) -> None:
        old = data.SEARCH_SINGLE_MAX
        data.SEARCH_SINGLE_MAX = 10  # force token mode on a tiny catalog
        try:
            root, out = repo(4), Path(tempfile.mkdtemp())
            build(root, out, None, Counting())
            self.assertEqual(manifest(out)["search"]["mode"], "tokens")
            before = dict(manifest(out)["files"])
            h, changed = new_statement(root, "Nat.succ 31415 = 31416")
            build(root, out, changed, Counting())
            files = manifest(out)["files"]
            shard = files[f"search/{data.token_shard('31415'):02x}"]
            self.assertIn(h, json.loads((out / shard).read_text())["31415"])
            moved = {k for k in files if k.startswith("search/") and files[k] != before.get(k)}
            toks = data.tokens("Nat.succ 31415 = 31416", "", [], {})
            self.assertLessEqual(moved, {f"search/{data.token_shard(t):02x}" for t in toks})
        finally:
            data.SEARCH_SINGLE_MAX = old


class GitTests(unittest.TestCase):
    """At most one push per run; nothing pushed when nothing changed; weekly squash."""

    def setUp(self) -> None:
        self.remote = Path(tempfile.mkdtemp()) / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        self.work = Path(tempfile.mkdtemp()) / "data"
        self.pushes = 0

        def runner(args: list[str], cwd: Path) -> str:
            if args[:2] == ["git", "push"]:
                self.pushes += 1
            env = {
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
                "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            }
            return subprocess.run(
                args, cwd=cwd, check=True, capture_output=True, text=True, env=env
            ).stdout

        self.run_git = runner

    def commits(self) -> int:
        out = subprocess.run(
            ["git", "--git-dir", str(self.remote), "rev-list", "--count", "data"],
            capture_output=True,
            text=True,
        )
        return int(out.stdout.strip() or 0)

    def test_one_push_per_publish_and_none_without_changes(self) -> None:
        publish.checkout(self.remote.as_uri(), self.work, run=self.run_git)
        (self.work / "a.json").write_text("1")
        self.assertTrue(publish.push(self.work, self.remote.as_uri(), "first", run=self.run_git))
        self.assertEqual((self.pushes, self.commits()), (1, 1))
        (self.work / "a.json").write_text("2")
        (self.work / "b.json").write_text("3")
        self.assertTrue(publish.push(self.work, self.remote.as_uri(), "second", run=self.run_git))
        self.assertEqual((self.pushes, self.commits()), (2, 2))
        self.assertFalse(publish.push(self.work, self.remote.as_uri(), "nothing", run=self.run_git))
        self.assertEqual((self.pushes, self.commits()), (2, 2))

    def test_squash_keeps_the_tree_and_one_commit(self) -> None:
        publish.checkout(self.remote.as_uri(), self.work, run=self.run_git)
        for i in range(3):
            (self.work / f"f{i}.json").write_text(str(i))
            publish.push(self.work, self.remote.as_uri(), f"c{i}", run=self.run_git)
        self.assertEqual(self.commits(), 3)
        publish.squash(self.work, self.remote.as_uri(), run=self.run_git)
        self.assertEqual(self.commits(), 1)
        fresh = Path(tempfile.mkdtemp()) / "again"
        publish.checkout(self.remote.as_uri(), fresh, run=self.run_git)
        self.assertEqual(
            sorted(p.name for p in fresh.glob("*.json")), ["f0.json", "f1.json", "f2.json"]
        )

    def test_checkout_of_a_missing_branch_starts_empty(self) -> None:
        publish.checkout(self.remote.as_uri(), self.work, run=self.run_git)
        self.assertEqual([p.name for p in self.work.iterdir() if p.name != ".git"], [])


class RateTests(unittest.TestCase):
    def test_publishes_at_least_6_minutes_apart(self) -> None:
        now = time.time()
        self.assertEqual(publish.wait_seconds(None, now), 0)
        self.assertEqual(publish.wait_seconds(now - 400, now), 0)
        self.assertAlmostEqual(publish.wait_seconds(now - 60, now), 300, delta=1)
        self.assertEqual(publish.MIN_GAP_SECONDS, 360)


if __name__ == "__main__":
    unittest.main()


class PublishedPrsTests(unittest.TestCase):
    """After a deploy, the PRs whose statements this publish rebuilt move to "published"."""

    def test_prs_for_the_affected_statements_only(self) -> None:
        from tools import ledger

        h1, h2 = "a" * 20, "b" * 20
        entries = [
            ledger.Entry(5, "alice", "proof", h1, "c" * 20, "2026-11-27T00:00:00Z"),
            ledger.Entry(6, "bob", "statement", h2, None, "2026-11-27T00:01:00Z"),
            ledger.Entry(4, "carol", "checkpoint", h1, "d" * 20, "2026-11-26T00:00:00Z"),
        ]
        text = ledger.dumps(entries)
        self.assertEqual(publish.published_prs(text, [h1]), [4, 5])
        self.assertEqual(publish.published_prs(text, []), [])
        self.assertEqual(publish.published_prs("not json", [h1]), [])


class CacheWriteTests(unittest.TestCase):
    def test_write_cache_reports_which_statements_changed(self) -> None:
        d = Path(tempfile.mkdtemp())
        e = {"hash": "a" * 20, "modelSha256": "m", "text": "x"}
        self.assertEqual(publish.write_cache(d, [e]), {"a" * 20})
        self.assertEqual(publish.write_cache(d, [e]), set())  # same entry again: no change
        self.assertEqual(
            publish.write_cache(d, [{**e, "text": "y"}, {"hash": "b" * 20, "modelSha256": "m"}]),
            {"a" * 20, "b" * 20},
        )
        self.assertEqual(len(publish.read_cache(d)), 2)
        self.assertEqual([x["hash"] for x in publish.read_cache(d, ["b" * 20])], ["b" * 20])


class NoOpTests(unittest.TestCase):
    def test_a_publish_with_nothing_new_changes_no_file(self) -> None:
        root, out = repo(3), Path(tempfile.mkdtemp())
        build(root, out, None, Counting())
        before = {
            p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()
        }
        result = build(root, out, [], Counting())
        after = {
            p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()
        }
        self.assertEqual(result.written, [])
        self.assertEqual(before, after)


class PagesFilesTests(unittest.TestCase):
    def test_tree_has_nojekyll_and_the_custom_domain(self) -> None:
        root, out = repo(1), Path(tempfile.mkdtemp())
        build(root, out, None, Counting())
        self.assertTrue((out / ".nojekyll").is_file())  # serve files as they are, no Jekyll
        self.assertEqual((out / "CNAME").read_text(), "data.arginf.com\n")
        self.assertEqual(build(root, out, [], Counting()).written, [])  # still a no-op after
