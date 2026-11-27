"""Published data files (scale design: "Published data", test plan rows on limits and weight).

User stories:
- As GitHub, we stay under its limits: no published file over 1 MB and no directory over 3,000
  entries in a 100k-statement catalog.
- As a visitor, pages load little: a summary shard is under 20 KB compressed at 100k, and a
  statement page needs one detail file.
"""

import gzip
import hashlib
import json
import random
import unittest
from collections import Counter

from tools import data

MB = 1024 * 1024
AREAS = [
    {"code": c, "name": n}
    for c, n in (
        ("math.NT", "Number theory"),
        ("cs.CC", "Computational complexity"),
        ("math.LO", "Logic"),
        ("cs.CR", "Cryptography and security"),
    )
]
WORDS = (
    "Nat Int Real Finset Polynomial Prime Even Odd Function Injective Surjective Group Ring "
    "Field Matrix Graph Set List Bool OneWay SAT P NP"
).split()


def h(i: int, salt: str = "s") -> str:
    return hashlib.sha256(f"{salt}{i}".encode()).hexdigest()[:20]


def record(
    i: int, rng: random.Random, attempts: int = 1, lean_bytes: int = 200
) -> dict[str, object]:
    """A statement as arginf.build_index (plus embeds) produces it."""
    words = rng.sample(WORDS, 4)
    prop = f"∀ n : {words[0]}, {words[1]}.{words[2]} (n + {i}) → {words[3]} n"
    atts = []
    for j in range(attempts):
        a = h(i * 1000 + j, "a")
        atts.append(
            {
                "hash": a,
                "parent": None,
                "openGoals": 0 if j == 0 else 1,
                "complete": j == 0,
                "children": [],
                "size": lean_bytes,
                "lean": "x" * lean_bytes,
                "mergedAt": "2026-11-27T00:00:00Z",
                "pr": i,
                "author": "alice",
                "upvotes": 0,
            }
        )
    return {
        "hash": h(i),
        "statement": prop,
        "statementLean": (
            f"import Mathlib\n\nnamespace Theorems.«{h(i)}»\n\ndef statement : Prop :=\n"
            f"  {prop}\n\nend Theorems.«{h(i)}»\n"
        ),
        "status": "proved",
        "proofs": [atts[0]["hash"]] if atts else [],
        "dependencies": [h(rng.randrange(i))] if i and rng.random() < 0.3 else [],
        "dependents": [],
        "valid": True,
        "featured": False,
        "example": False,
        "attempts": atts,
        "firstProof": atts[0]["hash"] if atts else None,
        "shortestProof": atts[0]["hash"] if atts else None,
        "topProof": None,
        "readings": [
            {
                "text": f"For every n, {words[1]} holds for n plus {i}.",
                "model": "Qwen3-8B-Q4_K_M",
                "modelSha256": "d" * 64,
                "runtime": "llama.cpp b12292",
                "cpu": "AMD EPYC 7763",
                "promptSha256": "a" * 64,
                "truncated": False,
            }
        ],
        "areas": {
            "codes": rng.sample([a["code"] for a in AREAS], 2),
            "model": "Qwen3-8B-Q4_K_M",
            "modelSha256": "d" * 64,
            "runtime": "llama.cpp b12292",
            "cpu": "AMD EPYC 7763",
            "promptSha256": "b" * 64,
        },
    }


def catalog(n: int, seed: int = 1) -> list[dict[str, object]]:
    rng = random.Random(seed)
    return [record(i, rng) for i in range(n)]


class Big(unittest.TestCase):
    """One 100k-statement catalog rendered once (about 10 s)."""

    files: dict[str, bytes]

    @classmethod
    def setUpClass(cls) -> None:
        cls.files = data.render(catalog(100_000), AREAS, commit="c" * 40)

    def test_no_published_file_over_1_mb(self) -> None:
        sizes = {p: len(b) for p, b in self.files.items()}
        worst = max(sizes, key=sizes.__getitem__)
        self.assertLess(sizes[worst], MB, worst)

    def test_no_directory_over_3000_entries(self) -> None:
        width: Counter[str] = Counter()
        seen: set[tuple[str, str]] = set()
        for path in self.files:
            parts = path.split("/")
            for k in range(len(parts)):
                entry = ("/".join(parts[:k]), parts[k])
                if entry not in seen:
                    seen.add(entry)
                    width[entry[0]] += 1
        self.assertLessEqual(max(width.values()), 3000, width.most_common(3))

    def test_summary_shard_under_20_kb_compressed(self) -> None:
        shards = [b for p, b in self.files.items() if p.startswith("summary/")]
        self.assertEqual(len(shards), 256)
        worst = max(len(gzip.compress(b, 9)) for b in shards)
        self.assertLess(worst, 20 * 1024, worst)

    def test_manifest_is_small_and_lists_every_served_file_but_details(self) -> None:
        manifest = json.loads(self.files["manifest.json"])
        self.assertLess(len(self.files["manifest.json"]), 64 * 1024)
        self.assertEqual(manifest["counts"]["statements"], 100_000)
        listed = set(manifest["files"].values())
        for path in self.files:
            if path == "manifest.json" or path.startswith("statements/"):
                continue
            self.assertIn(path, listed)
        self.assertEqual(manifest["search"]["mode"], "tokens")

    def test_a_statement_page_needs_one_detail_file(self) -> None:
        manifest = json.loads(self.files["manifest.json"])
        target = h(4242)
        shard = json.loads(self.files[manifest["files"][f"summary/{target[:2]}"]])
        row = dict(
            zip(shard["cols"], next(r for r in shard["rows"] if r[0] == target), strict=True)
        )
        detail = json.loads(self.files[data.detail_path(target, row["detail"])])
        self.assertEqual(detail["hash"], target)
        self.assertEqual(detail["attempts"][0]["lean"], "x" * 200)

    def test_token_search_finds_a_statement_by_word_area_and_reading(self) -> None:
        manifest = json.loads(self.files["manifest.json"])
        target = catalog(5000)[4242]
        word = str(target["readings"][0]["text"]).split()[-1].rstrip(".").lower()  # "4242"
        shard = manifest["files"][f"search/{data.token_shard(word):02x}"]
        postings = json.loads(self.files[shard])
        self.assertIn(target["hash"], postings[word])


class SmallCatalogTests(unittest.TestCase):
    def test_single_search_file_while_small(self) -> None:
        files = data.render(catalog(50), AREAS, commit="c" * 40)
        manifest = json.loads(files["manifest.json"])
        self.assertEqual(manifest["search"]["mode"], "single")
        rows = json.loads(files[manifest["files"]["search"]])["rows"]
        self.assertEqual(len(rows), 50)

    def test_names_are_content_hashed_and_deterministic(self) -> None:
        a = data.render(catalog(20), AREAS, commit="c" * 40)
        b = data.render(list(reversed(catalog(20))), AREAS, commit="c" * 40)
        self.assertEqual(a, b)
        for path, body in a.items():
            if path == "manifest.json":
                continue
            stem = path.rsplit("/", 1)[-1]
            self.assertRegex(stem, r"\.[0-9a-f]{10}\.json$", path)
            self.assertEqual(stem.split(".")[-2], data.content_hash(body), path)

    def test_a_change_to_one_statement_changes_only_its_files(self) -> None:
        cat = catalog(300)
        before = data.render(cat, AREAS, commit="c" * 40)
        changed = dict(cat[7])
        changed["status"] = "open"
        after = data.render([*cat[:7], changed, *cat[8:]], AREAS, commit="c" * 40)
        diff = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
        shard = str(cat[7]["hash"])[:2]
        self.assertTrue(
            all(
                p.startswith(
                    (f"summary/{shard}", f"statements/{shard}/", "search", "graph/", "manifest")
                )
                for p in diff
            ),
            diff,
        )

    def test_graph_overview_counts_areas_and_cross_area_dependencies(self) -> None:
        files = data.render(catalog(200), AREAS, commit="c" * 40)
        manifest = json.loads(files["manifest.json"])
        graph = json.loads(files[manifest["files"]["graph/areas"]])
        self.assertEqual(sum(a["count"] for a in graph["areas"]), 400)  # 2 areas each
        self.assertTrue(all(len(e) == 3 and e[2] > 0 for e in graph["edges"]))

    def test_strings_are_data_only(self) -> None:
        cat = catalog(3)
        evil = dict(cat[0])
        evil["statement"] = '<img src=x onerror=alert(1)>" '
        files = data.render([evil, *cat[1:]], AREAS, commit="c" * 40)
        manifest = json.loads(files["manifest.json"])
        shard = json.loads(files[manifest["files"][f"summary/{str(evil['hash'])[:2]}"]])
        texts = [r[shard["cols"].index("statement")] for r in shard["rows"]]
        self.assertIn('<img src=x onerror=alert(1)>" ', texts)  # round-trips as text


class HugeStatementTests(unittest.TestCase):
    def test_a_statement_with_huge_proofs_is_split_under_1_mb(self) -> None:
        rng = random.Random(3)
        big = record(1, rng, attempts=40, lean_bytes=200_000)  # 8 MB of Lean
        files = data.render([big], AREAS, commit="c" * 40)
        details = [p for p in files if p.startswith("statements/")]
        self.assertGreater(len(details), 1)
        self.assertTrue(all(len(files[p]) < 1024 * 1024 for p in details))
        main = json.loads(files[next(p for p in details if ".lean-" not in p)])
        self.assertEqual(len(main["attempts"]), 40)
        restored = 0
        for chunk in main["leanChunks"]:
            body = json.loads(files[data.chunk_path(str(big["hash"]), chunk)])
            restored += len(body)
        self.assertEqual(restored, 40)


class TokenTests(unittest.TestCase):
    def test_tokens_are_lowercase_words_and_area_codes(self) -> None:
        toks = data.tokens(
            "∀ n : Nat, Nat.Prime (n + 1) → Even n",
            "For every n, it holds.",
            ["cs.CC"],
            {"cs.CC": "Computational complexity"},
        )
        for t in ("nat", "prime", "even", "every", "holds", "cs.cc", "computational", "complexity"):
            self.assertIn(t, toks)
        self.assertNotIn("n", toks)  # one-letter tokens are noise

    def test_shard_function_is_stable(self) -> None:
        # The site computes the same FNV-1a hash in TypeScript; these values pin it.
        self.assertEqual(data.token_shard("nat"), data.fnv1a("nat") % 256)
        self.assertEqual(data.fnv1a(""), 0x811C9DC5)
        self.assertEqual(data.fnv1a("a"), 0xE40C292C)


if __name__ == "__main__":
    unittest.main()
