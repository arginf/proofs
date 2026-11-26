"""Machine readings: pinned model + runtime, fixed prompt, deterministic decoding."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from tools import arginf, readings

MODELS = """
[runtime]
name = "llama.cpp"
version = "b1"
url = "https://github.com/ggml-org/llama.cpp/releases/download/b1/llama-b1-bin-ubuntu-x64.tar.gz"
sha256 = "{sha}"

[[model]]
name = "Tiny-Model"
license = "Apache-2.0"
url = "https://huggingface.co/x/y/resolve/0123/tiny.gguf"
sha256 = "{sha}"
template = "chatml"
"""
SHA = "a" * 64


class ModelsTests(unittest.TestCase):
    def _load(self, text: str) -> readings.Allowlist:
        f = Path(tempfile.mkdtemp()) / "models.toml"
        f.write_text(text)
        return readings.load_models(f)

    def test_loads_pinned_runtime_and_models(self) -> None:
        allow = self._load(MODELS.format(sha=SHA))
        self.assertEqual(allow.runtime.version, "b1")
        self.assertEqual(allow.model("Tiny-Model").template, "chatml")
        with self.assertRaises(readings.ReadingError):
            allow.model("Not-Allowlisted")

    def test_rejects_unpinned_or_insecure_entries(self) -> None:
        for bad in (
            MODELS.format(sha="abc"),
            MODELS.format(sha=SHA).replace("https://huggingface.co", "http://huggingface.co"),
            MODELS.format(sha=SHA).replace("https://huggingface.co", "https://evil.example"),
            MODELS.format(sha=SHA).replace('template = "chatml"', 'template = "raw"'),
        ):
            with self.assertRaises(readings.ReadingError):
                self._load(bad)

    def test_repo_models_toml_is_valid(self) -> None:
        allow = readings.load_models(Path(__file__).resolve().parents[2] / "models.toml")
        self.assertTrue(allow.models)


class PromptTests(unittest.TestCase):
    def test_chatml_has_no_system_message(self) -> None:
        p = readings.chat_prompt("chatml", "Hello")
        self.assertEqual(p, "<|im_start|>user\nHello<|im_end|>\n<|im_start|>assistant\n")
        self.assertNotIn("system", p)

    def test_user_message_is_the_fixed_prompt_with_imports_and_statement(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "Theorems").mkdir()
        h = arginf.new_theorem(root, "∀ n : Nat, n + 0 = n", imports=("Init",))
        self.assertEqual(
            readings.user_message(root, h),
            "Translate the following Lean statement into the language of a mathematics research "
            "paper. Reply with only the translation of what the statement says: no preamble, and "
            "do not describe the imports, the definition or the Lean syntax. Write it the way "
            "theorem statements appear in papers, as prose with inline notation in LaTeX between "
            "$ signs, for example: For every $x \\in \\mathbb{R}$, we have $x^2 \\ge 0$. Use "
            "standard notation for Lean types, consistently: Nat as $\\mathbb{N}$, Int as "
            "$\\mathbb{Z}$, Rat as $\\mathbb{Q}$, Real as $\\mathbb{R}$, Complex as $\\mathbb{C}$, "
            "Bool as $\\{0,1\\}$, List Bool as $\\{0,1\\}^*$, Fin n as $[n]$, Set α as "
            "$\\mathcal{P}(\\alpha)$.\n```\n"
            "import Init\n\ndef statement : Prop :=\n  ∀ n : Nat, n + 0 = n\n```",
        )
        self.assertNotIn(h, readings.user_message(root, h))


class DecodingTests(unittest.TestCase):
    def test_args_are_deterministic(self) -> None:
        args = readings.generation_args(Path("bin/llama-completion"), Path("m.gguf"), Path("p.txt"))
        joined = " ".join(args)
        for flag in (
            "--temp 0",
            "--top-k 1",
            "--seed 0",
            "--threads 4",
            "-n 512",
            "-f p.txt",
            "-m m.gguf",
        ):
            self.assertIn(flag, joined)

    def test_clean_output_strips_chat_tokens(self) -> None:
        self.assertEqual(readings.clean_output("  Every n.<|im_end|>\n"), "Every n.")
        self.assertEqual(readings.clean_output("Every n. [end of text]\n"), "Every n.")
        self.assertEqual(readings.clean_output("Every n.\n\n> EOF by user\n"), "Every n.")

    def test_sha256_check(self) -> None:
        f = Path(tempfile.mkdtemp()) / "blob"
        f.write_bytes(b"data")
        readings.check_sha256(f, hashlib.sha256(b"data").hexdigest())
        with self.assertRaises(readings.ReadingError):
            readings.check_sha256(f, SHA)


if __name__ == "__main__":
    unittest.main()


class CacheTests(unittest.TestCase):
    """Each statement is read once per model; results carry everything needed to reproduce."""

    P = {"a" * 20: "p" * 64, "b" * 20: "q" * 64}

    def test_missing_only_and_merge(self) -> None:
        old = [{"hash": "a" * 20, "modelSha256": SHA, "maxTokens": 512, "promptSha256": "p" * 64}]
        self.assertEqual(readings.missing(["a" * 20, "b" * 20], old, SHA, self.P), ["b" * 20])
        self.assertEqual(readings.missing(["a" * 20], old, "c" * 64, self.P), ["a" * 20])

    def test_changing_the_prompt_regenerates(self) -> None:
        old = [{"hash": "a" * 20, "modelSha256": SHA, "maxTokens": 512, "promptSha256": "z" * 64}]
        self.assertEqual(readings.missing(["a" * 20], old, SHA, self.P), ["a" * 20])

    def test_changing_the_cap_regenerates(self) -> None:
        old = [{"hash": "a" * 20, "modelSha256": SHA, "maxTokens": 256, "promptSha256": "p" * 64}]
        self.assertEqual(readings.missing(["a" * 20], old, SHA, self.P), ["a" * 20])
        legacy = [{"hash": "a" * 20, "modelSha256": SHA, "promptSha256": "p" * 64}]
        self.assertEqual(readings.missing(["a" * 20], legacy, SHA, self.P), ["a" * 20])
        merged = readings.merge(old, [{"hash": "b" * 20, "modelSha256": SHA, "text": "y"}])
        self.assertEqual([r["hash"] for r in merged], ["a" * 20, "b" * 20])

    def test_prompt_digest_changes_with_statement(self) -> None:
        self.assertNotEqual(readings.digest("p1"), readings.digest("p2"))


class EmbedTests(unittest.TestCase):
    def test_embed_attaches_readings_by_hash_without_timing(self) -> None:
        index = {"version": 2, "theorems": [{"hash": "a" * 20}, {"hash": "b" * 20}]}
        rs = [
            {
                "hash": "a" * 20,
                "text": "t",
                "model": "M",
                "modelSha256": SHA,
                "runtime": "r",
                "cpu": "c",
                "promptSha256": "p" * 64,
                "seconds": 3.0,
            }
        ]
        out = readings.embed(index, rs)
        self.assertEqual(
            out["theorems"][0]["readings"],
            [
                {
                    "text": "t",
                    "model": "M",
                    "modelSha256": SHA,
                    "runtime": "r",
                    "cpu": "c",
                    "promptSha256": "p" * 64,
                }
            ],
        )
        self.assertEqual(out["theorems"][1]["readings"], [])


class LimitsTests(unittest.TestCase):
    def test_context_is_explicit(self) -> None:
        args = readings.generation_args(Path("b"), Path("m"), Path("p"))
        self.assertIn("-c", args)
        self.assertEqual(args[args.index("-c") + 1], str(readings.CONTEXT))

    def test_truncation_detected_from_llama_perf_line(self) -> None:
        capped = (
            "llama_perf_context_print:        eval time =   61234.56 ms /   511 runs   "
            "(  119.83 ms per token,     8.34 tokens per second)\n"
        )
        done = capped.replace("511 runs", "87 runs")
        self.assertEqual(readings.generated_tokens(capped), 511)
        self.assertTrue(readings.is_truncated(capped))
        self.assertFalse(readings.is_truncated(done))
        self.assertFalse(readings.is_truncated("no perf line"))
        self.assertIsNone(readings.generated_tokens("no perf line"))


class PromptBracesTests(unittest.TestCase):
    def test_tex_braces_in_the_prompt_are_literal(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "Theorems").mkdir()
        h = arginf.new_theorem(root, "True", imports=("Init",))
        msg = readings.user_message(root, h)
        self.assertIn("\\mathbb{R}", msg)
        self.assertTrue(msg.endswith("def statement : Prop :=\n  True\n```"))


class ThinkTests(unittest.TestCase):
    def test_empty_think_block_is_stripped(self) -> None:
        self.assertEqual(
            readings.clean_output("<think>\n\n</think>\n\nFor every $n$."), "For every $n$."
        )


class PruneTests(unittest.TestCase):
    """Only readings from models still allowlisted in models.toml are kept and published."""

    def test_embed_and_prune_keep_only_allowlisted_models(self) -> None:
        old, new = "1" * 64, "2" * 64
        rs = [
            {"hash": "a" * 20, "text": "old", "model": "Old", "modelSha256": old},
            {"hash": "a" * 20, "text": "new", "model": "New", "modelSha256": new},
        ]
        kept = readings.prune(rs, {new})
        self.assertEqual([r["text"] for r in kept], ["new"])
        index = {"version": 2, "theorems": [{"hash": "a" * 20}]}
        out = readings.embed(index, rs, {new})
        self.assertEqual([r["text"] for r in out["theorems"][0]["readings"]], ["new"])
