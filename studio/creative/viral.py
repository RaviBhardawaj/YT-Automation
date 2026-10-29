"""The viral engine: what makes people click, stay and share — applied to every video, checked before publishing.

1. Hooks: the first seconds show the most surprising / funniest moment; no greetings, intros or title cards.
2. Titles: several honest variants are written and scored; the best one is used and the rest are kept, so the owner
   can run YouTube's own A/B "Test & compare" with them.
3. A viral check before publishing scores the opening, length, title and thumbnail text, and fixes what it can by
   itself (title, thumbnail words). It never blocks a video — it improves it and says what it changed.
Nothing here invents facts or promises: a title must describe what really happens in the video.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from .. import ai, logs

HOOK_RULES = ("HOOK (most important): the very first line starts in the middle of the action with the most surprising, "
              "funny or tense moment — a reaction, a shout, a problem — in under 3 seconds of speech. No greetings, no "
              "'once upon a time', no introducing characters or describing the place first. Every scene ends on a "
              "small surprise that makes the viewer want the next one; the last line is the biggest payoff.")
SHORT_HOOK_RULES = ("SHORT FORMAT: line 1 is the hook (a shout, a surprise, a question) within 2 seconds; one joke or one "
                    "idea only; something new happens every 2-3 seconds; the final line is the payoff and leads naturally "
                    "back to the first line so the Short loops.")

CLICKBAIT_KIDS = re.compile(r"(?i)shock|danger|kidnap|scary|horror|खतरनाक|खौफनाक|भयानक|किडनैप|अपहरण|डरावन")
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


class TitleOption(BaseModel):
    title: str = Field(description="the title, max 60 characters, in the video's language")
    curiosity: int = Field(ge=1, le=10, description="how much it makes a viewer want to know what happens")
    clarity: int = Field(ge=1, le=10, description="how clearly it says what the video is")
    emotion: int = Field(ge=1, le=10, description="emotional pull: fun, surprise, warmth, tension")
    honest: bool = Field(description="true only if it describes what really happens in the video")


class TitleSet(BaseModel):
    options: list[TitleOption] = Field(description="5 different titles")


def score(o: TitleOption, kids: bool) -> float:
    s = o.curiosity * 0.4 + o.emotion * 0.35 + o.clarity * 0.25
    if not o.honest:
        s -= 10
    n = len(o.title)
    if n > 60:
        s -= 3
    elif 25 <= n <= 55:
        s += 0.5
    if kids and CLICKBAIT_KIDS.search(o.title):
        s -= 10
    if len(EMOJI.findall(o.title)) > 2:
        s -= 1
    return round(s, 2)


def titles(summary: str, current: str, language: str, *, kids: bool = False, fmt: str = "long") -> dict[str, Any]:
    """5 honest title variants, scored; {'title': best, 'variants': [...], 'by': model} (current title kept on failure)."""
    rules = ("For a children's channel: no fear, violence or clickbait words; warm, silly, curious. " if kids else "")
    try:
        out, meta = ai.generate(
            "ideation", "You write YouTube titles that get clicked AND keep their promise. Output one JSON object.",
            f"LANGUAGE: {language}\nFORMAT: {'YouTube Short' if fmt == 'short' else 'video'}\n"
            f"WHAT HAPPENS IN THE VIDEO: {summary[:1500]}\nCURRENT TITLE: {current}\n\n"
            "Write 5 clearly different titles (max 60 characters each): name the most surprising or funny thing that "
            "happens, use a concrete detail, at most 2 emoji, no ALL CAPS, no 'Episode'/'Part', no false promises. "
            f"{rules}Score each one honestly.", TitleSet, max_tokens=1200)
    except Exception as e:  # titles must never stop a video
        logs.event("warning", "creative", f"title variants skipped: {e}"[:200])
        return {"title": current, "variants": [], "by": None}
    ranked = sorted(({"title": o.title.strip().strip('"')[:70], "score": score(o, kids)} for o in out.options
                     if o.title.strip()), key=lambda x: -x["score"])
    if not ranked or ranked[0]["score"] <= 0:
        return {"title": current, "variants": ranked, "by": meta.get("provider")}
    return {"title": ranked[0]["title"], "variants": ranked[:5], "by": meta.get("provider")}


def check(*, kind: str, fmt: str, seconds: float, title: str, thumb_text: str = "", opening_delay: float = 0.0,
          kids: bool = False) -> list[dict[str, Any]]:
    """Viral checks (warnings, never blocking) — each {'name', 'ok', 'severity': 'warn', 'detail'}."""
    out = []

    def add(name: str, ok: bool, detail: str) -> None:
        out.append({"name": f"viral: {name}", "ok": ok, "severity": "warn", "detail": detail})
    add("opens fast", opening_delay <= 1.0,
        f"first words after {opening_delay:.1f} s" + ("" if opening_delay <= 1.0 else " — cut the intro so it starts at once"))
    if fmt == "short":
        add("short length", 15 <= seconds <= 50, f"{seconds:.0f} s (best 15–50 s for Shorts)")
    else:
        add("video length", 150 <= seconds <= 480 if kind in ("story", "kids") else 120 <= seconds <= 900,
            f"{seconds / 60:.1f} min" + (" (kids episodes hold attention best at 3–6 min)" if kind in ("story", "kids") else ""))
    add("title", 15 <= len(title) <= 60 and not (kids and CLICKBAIT_KIDS.search(title)),
        f"{len(title)} characters" + (" — too long, gets cut off on phones" if len(title) > 60 else ""))
    if thumb_text:
        words = len(thumb_text.split())
        add("thumbnail words", words <= 4, f"{words} words (2–4 big words read best on a phone)")
    return out
