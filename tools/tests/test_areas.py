"""Area labels: the pinned reading model picks all applicable fields from a fixed list.

User stories:
- As a reader, I can filter and colour the statement graph by field (arXiv-style areas).
- As a maintainer, I know the site only ever shows labels from the fixed list, whatever the
  model replies, so a statement cannot inject text through its labels.
- As anyone, I can reproduce a statement's labels from the pinned model, prompt and runtime.
- As the project, CI spends model time only on statements that are new, or whose prompt or
  area list changed.
"""

import tempfile
import unittest
from pathlib import Path

from tools import areas, arginf, readings

SHA = "a" * 64
H1, H2 = "a" * 12, "b" * 12


class AreaListTests(unittest.TestCase):
    def test_codes_are_unique_arxiv_style_and_named(self) -> None:
        codes = [a.code for a in areas.AREAS]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertGreaterEqual(len(codes), 20)
        for a in areas.AREAS:
            self.assertRegex(a.code, r"^(math|cs)\.[A-Z]{2}$")
            self.assertTrue(a.name and a.name[0].isupper())

    def test_list_covers_what_arginf_hosts_today(self) -> None:
        codes = {a.code for a in areas.AREAS}
        for c in ("cs.CC", "cs.CR", "math.NT", "math.LO", "math.CO", "cs.FL"):
            self.assertIn(c, codes)

    def test_published_list_is_plain_data(self) -> None:
        listed = areas.area_list()
        self.assertEqual(listed[0], {"code": areas.AREAS[0].code, "name": areas.AREAS[0].name})
        self.assertEqual(len(listed), len(areas.AREAS))


class PromptTests(unittest.TestCase):
    def _root(self) -> tuple[Path, str]:
        root = Path(tempfile.mkdtemp())
        (root / "Theorems").mkdir()
        return root, arginf.new_theorem(root, "∀ n : Nat, n + 0 = n", imports=("Init",))

    def test_prompt_lists_every_area_and_the_statement_without_its_hash(self) -> None:
        root, h = self._root()
        msg = areas.user_message(root, h)
        for a in areas.AREAS:
            self.assertIn(f"{a.code}: {a.name}", msg)
        self.assertTrue(msg.endswith("def statement : Prop :=\n  ∀ n : Nat, n + 0 = n\n```"))
        self.assertIn("import Init", msg)
        self.assertNotIn(h, msg)

    def test_prompt_shares_the_statement_text_with_readings(self) -> None:
        root, h = self._root()
        lean = readings.statement_lean(root, h)
        self.assertIn(lean, areas.user_message(root, h))
        self.assertIn(lean, readings.user_message(root, h))

    def test_prompt_digest_changes_when_the_list_changes(self) -> None:
        root, h = self._root()
        before = readings.digest(areas.user_message(root, h))
        extra = (*areas.AREAS, areas.Area("math.XX", "Invented area"))
        self.assertNotEqual(before, readings.digest(areas.user_message(root, h, extra)))


class ParseTests(unittest.TestCase):
    def test_keeps_known_codes_in_reply_order(self) -> None:
        self.assertEqual(areas.parse_codes("cs.CR, cs.CC"), ("cs.CR", "cs.CC"))

    def test_tolerates_formatting_noise(self) -> None:
        self.assertEqual(areas.parse_codes("Areas: `cs.CC`;\n- math.LO."), ("cs.CC", "math.LO"))

    def test_drops_unknown_codes_prose_and_duplicates(self) -> None:
        self.assertEqual(
            areas.parse_codes("math.XX, cs.CC, cs.CC, <script>alert(1)</script> math.zz"),
            ("cs.CC",),
        )

    def test_codes_must_be_whole_tokens(self) -> None:
        self.assertEqual(areas.parse_codes("cs.CCX math.NTA xmath.LO"), ())

    def test_empty_or_unparseable_reply_gives_no_areas(self) -> None:
        self.assertEqual(areas.parse_codes(""), ())
        self.assertEqual(areas.parse_codes("I am not sure."), ())

    def test_caps_the_number_of_areas(self) -> None:
        reply = ", ".join(a.code for a in areas.AREAS)
        got = areas.parse_codes(reply)
        self.assertEqual(len(got), areas.MAX_AREAS)
        self.assertEqual(got, tuple(a.code for a in areas.AREAS[: areas.MAX_AREAS]))


class CacheTests(unittest.TestCase):
    P = {H1: "p" * 64, H2: "q" * 64}

    def _tag(self, h: str, prompt: str, sha: str = SHA) -> dict[str, object]:
        return {"hash": h, "modelSha256": sha, "promptSha256": prompt, "areas": ["cs.CC"]}

    def test_only_new_or_changed_statements_are_tagged(self) -> None:
        old = [self._tag(H1, "p" * 64)]
        self.assertEqual(areas.missing([H1, H2], old, SHA, self.P), [H2])
        self.assertEqual(areas.missing([H1], [self._tag(H1, "z" * 64)], SHA, self.P), [H1])
        self.assertEqual(areas.missing([H1], old, "c" * 64, self.P), [H1])

    def test_merge_replaces_by_hash_and_model(self) -> None:
        old = [self._tag(H1, "p" * 64), self._tag(H2, "q" * 64)]
        new = [{**self._tag(H1, "p" * 64), "areas": ["math.LO"]}]
        merged = areas.merge(old, new)
        self.assertEqual([t["hash"] for t in merged], [H1, H2])
        self.assertEqual(merged[0]["areas"], ["math.LO"])


class EmbedTests(unittest.TestCase):
    def test_embed_attaches_labels_list_and_reproduction_details(self) -> None:
        index = {"version": 2, "theorems": [{"hash": H1}, {"hash": H2}]}
        tags = [
            {
                "hash": H1,
                "areas": ["cs.CC", "cs.CR"],
                "raw": "cs.CC, cs.CR",
                "model": "M",
                "modelSha256": SHA,
                "runtime": "r",
                "cpu": "c",
                "promptSha256": "p" * 64,
                "seconds": 2.0,
            }
        ]
        out = areas.embed(index, tags, {SHA})
        self.assertEqual(out["areaList"], areas.area_list())
        self.assertEqual(
            out["theorems"][0]["areas"],
            {
                "codes": ["cs.CC", "cs.CR"],
                "model": "M",
                "modelSha256": SHA,
                "runtime": "r",
                "cpu": "c",
                "promptSha256": "p" * 64,
            },
        )
        self.assertIsNone(out["theorems"][1]["areas"])

    def test_embed_revalidates_codes_and_drops_unallowlisted_models(self) -> None:
        index = {"version": 2, "theorems": [{"hash": H1}]}
        forged = [{"hash": H1, "areas": ["cs.CC", "evil"], "modelSha256": SHA}]
        out = areas.embed(index, forged, {SHA})
        self.assertEqual(out["theorems"][0]["areas"]["codes"], ["cs.CC"])
        self.assertIsNone(areas.embed(index, forged, {"d" * 64})["theorems"][0]["areas"])


class DecodingTests(unittest.TestCase):
    def test_labels_use_the_deterministic_decoding_with_a_short_cap(self) -> None:
        args = readings.generation_args(Path("b"), Path("m"), Path("p"), areas.MAX_TOKENS)
        joined = " ".join(args)
        for flag in ("--temp 0", "--top-k 1", "--seed 0", f"-n {areas.MAX_TOKENS}"):
            self.assertIn(flag, joined)
        self.assertLess(areas.MAX_TOKENS, readings.MAX_TOKENS)

    def test_readings_default_cap_is_unchanged(self) -> None:
        args = readings.generation_args(Path("b"), Path("m"), Path("p"))
        self.assertEqual(args[args.index("-n") + 1], str(readings.MAX_TOKENS))


if __name__ == "__main__":
    unittest.main()
