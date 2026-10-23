"""Caption chunking and timing, synchronised to per-sentence TTS timings."""
from __future__ import annotations

from typing import Any

from . import tts


def chunk(text: str, max_words: int, max_chars: int) -> list[str]:
    words, out, cur = text.split(), [], []
    for w in words:
        trial = " ".join(cur + [w])
        if cur and (len(cur) >= max_words or len(trial) > max_chars):
            out.append(" ".join(cur))
            cur = [w]
        else:
            cur.append(w)
    if cur:
        out.append(" ".join(cur))
    # avoid a dangling one-word final chunk
    if len(out) > 1 and len(out[-1].split()) == 1 and len(out[-2].split()) < max_words + 1:
        last = out.pop()
        out[-1] = out[-1] + " " + last
    return out


def _weight(s: str) -> float:
    return len(s) + 2.0


def _spread(chunks: list[str], start: float, end: float) -> list[dict[str, Any]]:
    total = sum(_weight(c) for c in chunks) or 1.0
    t, out = start, []
    for c in chunks:
        dur = (end - start) * _weight(c) / total
        out.append({"start": t, "end": t + dur, "text": c})
        t += dur
    return out


def scene_captions(scene: dict[str, Any], timing: dict[str, Any], style: str, fmt: str) -> list[dict[str, Any]]:
    """Caption cues for one scene, in seconds relative to the scene start."""
    max_words, max_chars = (3, 22) if style == "bold" and fmt == "short" else (5, 30) if style == "bold" else (9, 44)
    sentences = timing["sentences"]
    cap = (scene.get("caption_text") or scene["narration"]).strip()
    cap_sentences = tts.split_sentences(cap)
    cues: list[dict[str, Any]] = []
    if cap == scene["narration"].strip() or len(cap_sentences) == len(sentences):
        pairs = zip(cap_sentences if len(cap_sentences) == len(sentences) else [s["text"] for s in sentences], sentences)
        for text, s in pairs:
            cues += _spread(chunk(text, max_words, max_chars), s["start"], s["end"])
    else:
        start = sentences[0]["start"] if sentences else 0.0
        end = sentences[-1]["end"] if sentences else timing["duration"]
        cues = _spread(chunk(cap, max_words, max_chars), start, end)
    # hold each cue until the next one starts so captions never flicker off mid-scene
    for a, b in zip(cues, cues[1:]):
        if b["start"] - a["end"] < 0.35:
            a["end"] = b["start"]
    return cues


def _ts(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(cues: list[dict[str, Any]]) -> str:
    lines = []
    for i, c in enumerate(cues, start=1):
        lines += [str(i), f"{_ts(c['start'])} --> {_ts(c['end'])}", c["text"], ""]
    return "\n".join(lines)
