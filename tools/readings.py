#!/usr/bin/env python3
"""Machine-generated English readings of statements (Python 3.11+, standard library only).

A reading is the output of an allowlisted open-weights model (models.toml), run by a pinned
llama.cpp build with deterministic decoding, on a fixed prompt containing the statement's Lean.
There is no system prompt, memory or tool use, so anyone can rerun it and check the text:

    python3 -m tools.readings generate --model <name> [<hash> ...]   # writes readings.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools import arginf

PROMPT = (
    "Translate the following Lean statement into the language of a mathematics research paper. "
    "Reply with only the translation of what the statement says: no preamble, and do not "
    "describe the imports, the definition or the Lean syntax. Write it the way theorem "
    "statements appear in papers, as prose with inline notation in LaTeX between $ signs, "
    "for example: For every $x \\in \\mathbb{R}$, we have $x^2 \\ge 0$. Use standard notation "
    "for Lean types, consistently: Nat as $\\mathbb{N}$, Int as $\\mathbb{Z}$, Rat as "
    "$\\mathbb{Q}$, Real as $\\mathbb{R}$, Complex as $\\mathbb{C}$, Bool as $\\{0,1\\}$, "
    "List Bool as $\\{0,1\\}^*$, Fin n as $[n]$, Set α as $\\mathcal{P}(\\alpha)$."
    "\n```\n{lean}\n```"
)
TEMPLATES = ("chatml", "qwen3-nothink")
ALLOWED_HOSTS = ("https://huggingface.co/", "https://github.com/ggml-org/llama.cpp/releases/")
THREADS = 4
MAX_TOKENS = 512
CONTEXT = 8192
_SHA = re.compile(r"[0-9a-f]{64}")


class ReadingError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Runtime:
    name: str
    version: str
    url: str
    sha256: str


@dataclass(frozen=True, slots=True)
class Model:
    name: str
    license: str
    url: str
    sha256: str
    template: str


@dataclass(frozen=True, slots=True)
class Allowlist:
    runtime: Runtime
    models: tuple[Model, ...]

    def model(self, name: str) -> Model:
        for m in self.models:
            if m.name == name:
                return m
        raise ReadingError(f"model {name!r} is not in models.toml")


def _pinned(url: object, sha: object, where: str) -> tuple[str, str]:
    if not isinstance(url, str) or not url.startswith(ALLOWED_HOSTS):
        raise ReadingError(f"{where}: url must be https on {', '.join(ALLOWED_HOSTS)}")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise ReadingError(f"{where}: sha256 must be 64 lowercase hex digits")
    return url, sha


def load_models(path: Path) -> Allowlist:
    raw: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    rt = raw.get("runtime")
    if not isinstance(rt, dict):
        raise ReadingError("models.toml: missing [runtime]")
    url, sha = _pinned(rt.get("url"), rt.get("sha256"), "runtime")
    runtime = Runtime(str(rt.get("name")), str(rt.get("version")), url, sha)
    models: list[Model] = []
    for i, m in enumerate(raw.get("model", [])):
        where = f"model {i + 1}"
        if not isinstance(m, dict) or not isinstance(m.get("name"), str):
            raise ReadingError(f"{where}: needs a name")
        url, sha = _pinned(m.get("url"), m.get("sha256"), where)
        if m.get("template") not in TEMPLATES:
            raise ReadingError(f"{where}: template must be one of {TEMPLATES}")
        models.append(Model(m["name"], str(m.get("license", "")), url, sha, m["template"]))
    if not models:
        raise ReadingError("models.toml: no [[model]] entries")
    return Allowlist(runtime, tuple(models))


def user_message(root: Path, h: str) -> str:
    """The fixed prompt around Statement.lean, keeping imports (they give names their meaning)
    and dropping the namespace/end lines, which only carry our hash."""
    text = (root / arginf.ROOT_DIR / h / "Statement.lean").read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if not re.match(r"\s*(namespace|end)\b", ln)]
    lean = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return PROMPT.replace("{lean}", lean)  # not str.format: the example contains TeX braces


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def missing(
    hashes: list[str], existing: list[dict[str, Any]], model_sha: str, prompts: dict[str, str]
) -> list[str]:
    """Statements needing a (re)reading: new, or read with another model, cap or prompt."""
    done = {
        r["hash"]
        for r in existing
        if r.get("modelSha256") == model_sha
        and r.get("maxTokens") == MAX_TOKENS
        and r.get("promptSha256") == prompts.get(r["hash"])
    }
    return [h for h in hashes if h not in done]


def merge(existing: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    key = {(r["hash"], r["modelSha256"]): r for r in existing}
    for r in new:
        key[(r["hash"], r["modelSha256"])] = r
    return sorted(key.values(), key=lambda r: (r["hash"], r["modelSha256"]))


PUBLISHED = (
    "text", "model", "modelSha256", "runtime", "cpu", "promptSha256", "maxTokens", "truncated",
)  # fmt: skip


def prune(rs: list[dict[str, Any]], allowed: set[str]) -> list[dict[str, Any]]:
    """Drop readings from models no longer in models.toml."""
    return [r for r in rs if r.get("modelSha256") in allowed]


def embed(
    index: dict[str, Any], rs: list[dict[str, Any]], allowed: set[str] | None = None
) -> dict[str, Any]:
    """Attach each statement's readings to the published index (reproduction details only)."""
    by_hash: dict[str, list[dict[str, Any]]] = {}
    for r in prune(rs, allowed) if allowed is not None else rs:
        by_hash.setdefault(r["hash"], []).append({k: r[k] for k in PUBLISHED if k in r})
    for t in index["theorems"]:
        t["readings"] = by_hash.get(t["hash"], [])
    return index


def _cpu() -> str:
    try:
        for ln in Path("/proc/cpuinfo").read_text().splitlines():
            if ln.startswith("model name"):
                return ln.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


def chat_prompt(template: str, user: str) -> str:
    """The raw model input: one user turn, no system message, then the assistant turn."""
    if template == "chatml":
        return f"<|im_start|>user\n{user}<|im_end|>\n<|im_start|>assistant\n"
    if template == "qwen3-nothink":
        # Qwen3's documented non-thinking turn: an empty think block opens the reply.
        return f"<|im_start|>user\n{user}<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
    raise ReadingError(f"unknown template {template}")


def generation_args(binary: Path, model: Path, prompt_file: Path) -> list[str]:
    return [
        str(binary),
        "-m", str(model),
        "-f", str(prompt_file),
        "-c", str(CONTEXT),
        "-n", str(MAX_TOKENS),
        "--temp", "0",
        "--top-k", "1",
        "--seed", "0",
        "--threads", str(THREADS),
        "--no-display-prompt",
    ]  # fmt: skip


_EVAL_RE = re.compile(r"\beval time\s*=\s*[\d.]+\s*ms\s*/\s*(\d+)\s*(?:runs|tokens)")


def generated_tokens(stderr: str) -> int | None:
    """Tokens generated, from llama.cpp's performance summary (the non-prompt eval line)."""
    counts = [int(m.group(1)) for m in _EVAL_RE.finditer(stderr)]
    return counts[-1] if counts else None


def is_truncated(stderr: str) -> bool:
    n = generated_tokens(stderr)
    return n is not None and n >= MAX_TOKENS - 1


def clean_output(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    for marker in ("<|im_end|>", "[end of text]", "> EOF by user"):
        text = text.split(marker, 1)[0]
    return text.strip()


def check_sha256(path: Path, expected: str) -> None:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != expected:
        raise ReadingError(f"{path.name}: sha256 {h.hexdigest()} != pinned {expected}")


def _download(url: str, dest: Path, sha: str) -> Path:
    if dest.is_file():
        try:
            check_sha256(dest, sha)
            return dest
        except ReadingError:
            dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "arginf-ci"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=600) as r, dest.open("wb") as f:  # noqa: S310
        shutil.copyfileobj(r, f, 1 << 20)
    check_sha256(dest, sha)
    return dest


def _binary(runtime_dir: Path) -> Path:
    for name in ("llama-completion", "llama-cli"):
        found = sorted(runtime_dir.rglob(name))
        if found:
            found[0].chmod(0o755)
            return found[0]
    raise ReadingError("no llama-completion or llama-cli in the runtime archive")


def generate(
    root: Path, allow: Allowlist, model: Model, hashes: list[str], cache: Path
) -> list[dict[str, Any]]:
    tar = _download(
        allow.runtime.url, cache / f"{allow.runtime.version}.tar.gz", allow.runtime.sha256
    )
    rdir = cache / f"runtime-{allow.runtime.version}"
    if not rdir.is_dir():
        with tarfile.open(tar) as t:
            t.extractall(rdir, filter="data")
    binary = _binary(rdir)
    weights = _download(model.url, cache / f"{model.sha256}.gguf", model.sha256)
    out: list[dict[str, Any]] = []
    for h in hashes:
        prompt_file = cache / f"prompt-{h}.txt"
        message = user_message(root, h)
        prompt_file.write_text(chat_prompt(model.template, message), encoding="utf-8")
        start = time.monotonic()
        run = subprocess.run(
            generation_args(binary, weights, prompt_file),
            capture_output=True, text=True, timeout=1800,
            env={"LD_LIBRARY_PATH": str(binary.parent), "PATH": "/usr/bin:/bin"},
        )  # fmt: skip
        seconds = round(time.monotonic() - start, 1)
        if run.returncode != 0:
            raise ReadingError(f"{h}: generation failed:\n{run.stderr[-3000:]}")
        out.append(
            {
                "hash": h,
                "text": clean_output(run.stdout),
                "model": model.name,
                "modelSha256": model.sha256,
                "runtime": f"{allow.runtime.name} {allow.runtime.version}",
                "cpu": _cpu(),
                "maxTokens": MAX_TOKENS,
                "truncated": is_truncated(run.stderr),
                "promptSha256": digest(message),
                "seconds": seconds,
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="readings")
    sub = parser.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--model", required=True)
    g.add_argument("--cache", type=Path, default=Path(".cache/readings"))
    g.add_argument("--out", type=Path, default=Path("readings.json"))
    g.add_argument("--existing", type=Path, default=None, help="readings.json to extend")
    g.add_argument("hashes", nargs="*")
    e = sub.add_parser("embed", help="attach readings.json to index.json in place")
    e.add_argument("index", type=Path)
    e.add_argument("readings", type=Path)
    args = parser.parse_args(argv)
    root = Path.cwd()
    if args.cmd == "embed":
        idx = json.loads(args.index.read_text(encoding="utf-8"))
        rs = (
            json.loads(args.readings.read_text(encoding="utf-8")) if args.readings.is_file() else []
        )
        allowed = {m.sha256 for m in load_models(root / "models.toml").models}
        rs = prune(rs, allowed)
        args.readings.write_text(json.dumps(rs, ensure_ascii=False, indent=1) + "\n")
        args.index.write_text(
            json.dumps(embed(idx, rs, allowed), ensure_ascii=False, indent=1) + "\n"
        )
        return 0
    try:
        allow = load_models(root / "models.toml")
        model = allow.model(args.model)
        hashes = args.hashes or sorted(
            p.name for p in (root / arginf.ROOT_DIR).iterdir() if arginf.HASH_RE.match(p.name)
        )
        existing: list[dict[str, Any]] = []
        if args.existing and args.existing.is_file():
            existing = json.loads(args.existing.read_text(encoding="utf-8"))
        prompts = {h: digest(user_message(root, h)) for h in hashes}
        todo = missing(hashes, existing, model.sha256, prompts)
        existing = prune(existing, {m.sha256 for m in allow.models})
        result = merge(existing, generate(root, allow, model, todo, args.cache) if todo else [])
    except (ReadingError, OSError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(todo)} new reading(s), {len(result)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
