"""Moderation gate for auto-merge (Python 3.11+; detoxify is only needed in CI).

A PR that trips this gate is not rejected: it simply isn't auto-merged, and gets a
`needs-review` label so a maintainer decides. Two independent signals:
- a word list (kept private in a repo secret, so it can't be studied), matched after
  normalizing leetspeak and repeated letters, on whole words and adjacent word pairs only
  (no substring matching, which would flag "assume" and "classify");
- detoxify's toxicity scores on the PR title, description and identifier words.
Reasons never repeat the offending text.
"""

from __future__ import annotations

import re
from collections.abc import Callable

type Scorer = Callable[[list[str]], list[float]]

THRESHOLD = 0.5
_LEET = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}
)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_']*")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def normalize(word: str) -> str:
    w = word.lower().translate(_LEET)
    w = re.sub(r"[^a-z]", "", w)
    return re.sub(r"(.)\1+", r"\1", w)


def identifier_words(lean: str) -> list[str]:
    words: list[str] = []
    for ident in _IDENT.findall(lean):
        for part in ident.replace("'", "_").split("_"):
            words += [m.lower() for m in _CAMEL.findall(part)]
    return [w for w in words if len(w) >= 2]


def parse_word_list(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(normalize(s))
    return [w for w in out if w]


def word_hits(words: list[str], blocked: list[str]) -> bool:
    bad = {normalize(b) for b in blocked if normalize(b)}
    norm = [normalize(w) for w in words]
    candidates = set(norm) | {a + b for a, b in zip(norm, norm[1:], strict=False)}
    return bool(candidates & bad)


def moderate(
    title: str, body: str, lean_texts: list[str], blocked: list[str], scorer: Scorer
) -> list[str]:
    reasons: list[str] = []
    words = [w for t in lean_texts for w in identifier_words(t)]
    if blocked and (
        word_hits(words, blocked) or word_hits(re.findall(r"\w+", title + " " + body), blocked)
    ):
        reasons.append("matched the moderation word list")
    texts = {"title": title, "description": body, "identifiers": " ".join(words)}
    labels = [k for k, v in texts.items() if v.strip()]
    try:
        scores = scorer([texts[k] for k in labels])
    except Exception as err:  # noqa: BLE001 - fail closed: a human reviews instead
        return reasons + [f"toxicity model unavailable ({type(err).__name__})"]
    for label, score in zip(labels, scores, strict=True):
        if score >= THRESHOLD:
            reasons.append(f"{label} scored as likely abusive ({score:.2f})")
    return reasons


def detoxify_scorer() -> Scorer:
    """Max over detoxify's categories (toxicity, insult, threat, identity attack, ...)."""
    from detoxify import Detoxify  # type: ignore[import-not-found]  # only in the auto-merge job

    model = Detoxify("original")

    def score(texts: list[str]) -> list[float]:
        if not texts:
            return []
        result = model.predict(texts)
        return [max(float(result[k][i]) for k in result) for i in range(len(texts))]

    return score
