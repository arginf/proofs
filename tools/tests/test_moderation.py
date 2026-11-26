"""Moderation gate for auto-merge. Uses placeholder words only (zzbad, qqworse)."""

import unittest

from tools import moderation

WORDS = ["zzbad", "qqworse"]
LEAN = "theorem proof : Theorems.«0123456789ab».statement := by\n  intro myCleanVar\n  simp\n"


def clean_scorer(texts: list[str]) -> list[float]:
    return [0.01 for _ in texts]


class WordTests(unittest.TestCase):
    def test_identifier_words_split_camel_and_snake(self) -> None:
        words = moderation.identifier_words("def fooBar_bazQux : Nat := hεLemma")
        for w in ("foo", "bar", "baz", "qux", "lemma"):
            self.assertIn(w, words)

    def test_normalization_catches_leetspeak_and_repeats(self) -> None:
        self.assertEqual(moderation.normalize("ZZB4AAD"), moderation.normalize("zzbad"))
        self.assertEqual(moderation.normalize("qqw0rs3"), moderation.normalize("qqworse"))

    def test_blocked_words_in_identifiers_and_split_across_parts(self) -> None:
        self.assertTrue(moderation.word_hits(["zz", "bad"], WORDS))
        self.assertTrue(moderation.word_hits(["zzb4d"], WORDS))
        self.assertFalse(moderation.word_hits(["assume", "classify"], ["ass"]))  # no substring hits


class ModerateTests(unittest.TestCase):
    def test_clean_pr_passes(self) -> None:
        self.assertEqual(
            moderation.moderate("Prove it", "By simp.", [LEAN], WORDS, clean_scorer), []
        )

    def test_blocked_identifier_flags(self) -> None:
        lean = LEAN.replace("myCleanVar", "zzBad_x")
        reasons = moderation.moderate("Prove it", "", [lean], WORDS, clean_scorer)
        self.assertTrue(any("word list" in r for r in reasons))
        self.assertFalse(any("zzbad" in r for r in reasons))  # never echo the word

    def test_toxic_title_flags(self) -> None:
        def scorer(texts: list[str]) -> list[float]:
            return [0.97 if "nasty" in t else 0.01 for t in texts]

        reasons = moderation.moderate("nasty title", "", [LEAN], [], scorer)
        self.assertTrue(any("title" in r for r in reasons))

    def test_scorer_failure_fails_closed(self) -> None:
        def broken(texts: list[str]) -> list[float]:
            raise RuntimeError("model unavailable")

        self.assertTrue(moderation.moderate("x", "", [LEAN], [], broken))

    def test_word_list_parsing_ignores_blank_and_comment_lines(self) -> None:
        self.assertEqual(
            moderation.parse_word_list("# c\nZZBad\n\n qqworse \n"),
            [moderation.normalize("zzbad"), moderation.normalize("qqworse")],
        )


if __name__ == "__main__":
    unittest.main()
