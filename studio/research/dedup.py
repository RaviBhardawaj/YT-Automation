"""Duplicate / near-duplicate detection for topic titles, and copy detection for scripts."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

_STOP = set("""
a an the of to in on for and or is are was were be been being this that these those with without
what why how who whom which when where will would can could should do does did your you we our my
i it its from by at as about into than then vs versus ever most more best top new
क्या है हैं था थे की के का को में से पर और या यह वह ये वो एक कैसे क्यों कौन कब कहाँ भी ही तो
kya hai hain ka ki ke ko me mein se par aur ya yeh woh ek kaise kyun kaun kab kahan bhi hi toh
""".split())


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = re.sub(r"[^\w\sऀ-ॿ]", " ", text)      # drop punctuation & emoji, keep Devanagari
    return re.sub(r"\s+", " ", text).strip()


def tokens(text: str) -> set[str]:
    return {t for t in normalize(text).split() if t not in _STOP and len(t) > 1}


def _trigrams(text: str) -> set[str]:
    s = normalize(text).replace(" ", "")
    return {s[i:i + 3] for i in range(max(0, len(s) - 2))}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    return max(
        _jaccard(ta, tb),
        _jaccard(_trigrams(a), _trigrams(b)) * 0.95,
        SequenceMatcher(None, normalize(a), normalize(b)).ratio() * 0.9,
    )


DUPLICATE = 0.72
NEAR = 0.55


def best_match(title: str, existing: list[tuple[int, str]]) -> tuple[int | None, float]:
    best_id, best = None, 0.0
    for tid, other in existing:
        s = similarity(title, other)
        if s > best:
            best_id, best = tid, s
    return best_id, best


# ------------------------------------------------------------------ copy detection

def shingles(text: str, n: int = 7) -> set[tuple[str, ...]]:
    words = normalize(text).split()
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def overlap_ratio(script: str, sources: list[str], n: int = 7) -> float:
    """Share of the script's n-word sequences that also appear verbatim in any source text."""
    mine = shingles(script, n)
    if not mine:
        return 0.0
    theirs: set[tuple[str, ...]] = set()
    for s in sources:
        theirs |= shingles(s, n)
    return len(mine & theirs) / len(mine)
