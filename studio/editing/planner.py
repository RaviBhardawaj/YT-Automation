"""Turn the creator's editing instructions into a concrete, validated edit plan.

The AI sees the instructions, each source's real properties (duration, size, audio), its timestamped
transcript and detected scene cuts, and returns which segments go into which output and what to apply.
Anything the engine can't do is listed in `unsupported` and reported to the creator — never faked.
If the AI is unavailable, a rule-based plan is built from the instructions instead.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from .. import ai

ENGINE_CAN = ("cut and reorder segments; remove silences/pauses; vertical 9:16, square 1:1 or 16:9 reframing "
              "(blurred-background fit, centre crop, or a crop centred on the speaker's face per shot); a short "
              "headline over the first seconds; burned-in subtitles (as spoken, or translated into another language) with "
              "highlighted keywords, plus an .srt file; intro and outro title cards; original background music (mood-based) "
              "mixed under the speech; loudness normalisation; speed change 0.5–2x; titles, descriptions, tags "
              "and a thumbnail for each output")
ENGINE_CANNOT = ("replacing or dubbing the voiceover, AI-generated video or images, object removal, colour "
                 "grading beyond basic, adding stock footage, lip-sync, a crop that follows movement within one shot")


class Segment(BaseModel):
    media_id: int
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    why: str = ""


class Output(BaseModel):
    name: str = Field(description="short label, e.g. 'Main edit' or 'Short 2'")
    aspect: Literal["16:9", "9:16", "1:1", "original"] = "original"
    reframe: Literal["fit_blur", "crop", "face"] = "fit_blur"
    segments: list[Segment] = Field(default_factory=list, description="in output order; empty = whole source")
    remove_silences: bool = False
    subtitles: bool = False
    subtitle_language: str = Field("original", description="'original' = as spoken, or a language to translate "
                                   "captions into (e.g. 'Hindi') when the creator asks for it")
    highlight_words: list[str] = Field(default_factory=list, description="keywords to colour in subtitles")
    headline: str = Field("", description="optional 3–8 word headline shown over the first seconds")
    intro_text: str = ""
    outro_text: str = ""
    music_mood: Literal["none", "funny", "calm", "happy", "tense", "scary", "sad", "inspiring"] = "none"
    speed: float = Field(1.0, ge=0.5, le=2.0)
    title: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)


class EditPlan(BaseModel):
    summary: str = Field(description="one sentence: what will be produced")
    outputs: list[Output] = Field(min_length=1, max_length=12)
    unsupported: list[str] = Field(default_factory=list, description="requested things the engine cannot do")


SYSTEM = ("You are a senior video editor planning an edit for an automated FFmpeg editing engine. Follow the "
          "creator's instructions exactly; do not add a fixed template. Use only timestamps that exist in the "
          "sources. Output one JSON object matching the schema, no commentary.")


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + " …"


def describe_sources(media: list[dict[str, Any]], scenes: dict[int, list[float]]) -> str:
    parts = []
    for m in media:
        parts.append(f"- media_id {m['id']}: {m['kind']} '{m['original_name']}', duration "
                     f"{(m.get('duration') or 0):.1f}s, {m.get('width') or '-'}x{m.get('height') or '-'}, "
                     f"audio {'yes' if m.get('has_audio') else 'no'}")
        if scenes.get(m["id"]):
            parts.append(f"  scene cuts at: {', '.join(f'{t:.1f}' for t in scenes[m['id']][:80])}")
        tr = m.get("transcript") or []
        if tr:
            lines = "\n".join(f"  [{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in tr)
            parts.append("  transcript:\n" + _clip(lines, 24000))
    return "\n".join(parts)


def plan(instructions: str, media: list[dict[str, Any]], scenes: dict[int, list[float]], *, language: str,
         fmt: str | None) -> tuple[EditPlan, dict[str, Any]]:
    videos = [m for m in media if m["kind"] in ("video", "audio")]
    durations = {m["id"]: float(m.get("duration") or 0) for m in videos}
    prompt = (f"CREATOR'S INSTRUCTIONS (highest priority): {instructions or 'Make this a clean, engaging video.'}\n"
              f"REQUESTED FORMAT: {fmt or 'decide from the instructions'}\n"
              f"LANGUAGE FOR TITLES/DESCRIPTIONS: {language}\n\nSOURCES:\n{describe_sources(media, scenes)}\n\n"
              f"THE ENGINE CAN: {ENGINE_CAN}.\nTHE ENGINE CANNOT: {ENGINE_CANNOT} — list any such request in "
              f"'unsupported' and still do everything else.\n"
              f"Rules: when the instructions change the video itself (pauses, subtitles, music, intro, pacing …) "
              f"and ALSO ask for Shorts or clips, the FIRST output is the full edited video and the Shorts follow. "
              f"Captions show the words as spoken unless the creator asks for captions in another language "
              f"(set subtitle_language). "
              f"Shorts are 9:16 and 15–60 seconds, each a self-contained moment with a strong first line. "
              f"Segments must lie inside the source duration; prefer cutting at transcript sentence ends or scene "
              f"cuts. Keep the original meaning. Titles in {language}, honest, no clickbait lies.")

    def check(p: EditPlan) -> None:
        problems = []
        for o in p.outputs:
            for s in o.segments:
                if s.media_id not in durations:
                    problems.append(f"{o.name}: media_id {s.media_id} is not a video/audio source")
                elif s.end > durations[s.media_id] + 0.5 or s.start >= s.end:
                    problems.append(f"{o.name}: segment {s.start}-{s.end} is outside 0-{durations[s.media_id]:.1f}s")
            if o.aspect == "9:16":
                total = sum(s.end - s.start for s in o.segments) / o.speed if o.segments else \
                    sum(durations.values()) / o.speed
                if total > 180:
                    problems.append(f"{o.name}: a Short must be under 3 minutes (is {total:.0f}s)")
                if total < 15 and sum(durations.values()) >= 30:
                    problems.append(f"{o.name}: a Short must be at least 15 seconds (is {total:.0f}s) — extend it to "
                                    f"a complete moment, joining neighbouring sentences if needed")
        if problems:
            raise ValueError("; ".join(problems[:8]))

    try:
        out, meta = ai.generate("script", SYSTEM, prompt, EditPlan, max_tokens=6000, validate=check)
        return out, {**meta, "planner": "ai"}
    except Exception as e:                        # AI unavailable: rule-based plan, still follows the instructions
        return fallback(instructions, media, scenes, fmt=fmt), {"planner": "rules", "ai_error": str(e)[:300]}


# ------------------------------------------------------------------ rule-based fallback

NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
             "ten": 10, "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "पाँच": 5}


def _count_shorts(text: str) -> int:
    m = re.search(r"(\d+|" + "|".join(NUM_WORDS) + r")\s*(?:engaging\s+|vertical\s+|short\s+)?(?:shorts?|clips?|reels?|शॉर्ट)",
                  text, re.I)
    if not m:
        return 0
    v = m.group(1).lower()
    return int(v) if v.isdigit() else NUM_WORDS.get(v, 0)


def fallback(instructions: str, media: list[dict[str, Any]], scenes: dict[int, list[float]],
             fmt: str | None = None) -> EditPlan:
    low = (instructions or "").lower()
    videos = [m for m in media if m["kind"] in ("video", "audio") and m.get("duration")]
    if not videos:
        raise ValueError("upload at least one video or audio file to edit")
    src = max(videos, key=lambda m: m["duration"])
    n_shorts = _count_shorts(low) or (3 if fmt == "short" or re.search(r"\bshorts?\b|reels?|vertical", low) else 0)
    subs = bool(re.search(r"subtitle|caption|सबटाइटल", low))
    silences = bool(re.search(r"pause|silence|dead air|tighten|unnecessary", low))
    music = "happy" if re.search(r"music|bgm", low) else "none"
    intro = "Intro" if re.search(r"\bintro|title sequence", low) else ""
    outro = "Thanks for watching" if re.search(r"\boutro|ending", low) else ""
    unsupported = [w for w, pat in (("voiceover replacement", r"voice ?over|dub"), ("AI-generated footage", r"generate (video|footage|image)"))
                   if re.search(pat, low)]
    outputs: list[Output] = []
    if fmt != "short" or not n_shorts:
        outputs.append(Output(name="Main edit", aspect="9:16" if fmt == "short" else "original", segments=[],
                              remove_silences=silences, subtitles=subs, intro_text=intro, outro_text=outro,
                              music_mood=music, title=src["original_name"].rsplit(".", 1)[0][:90]))
    if n_shorts:
        # spread Shorts over the video, preferring transcript sentence boundaries
        tr = src.get("transcript") or []
        length = min(45.0, src["duration"] / max(1, n_shorts))
        for i in range(n_shorts):
            start = src["duration"] * (i + 0.5) / n_shorts - length / 2
            if tr:
                start = min((s["start"] for s in tr), key=lambda t: abs(t - start))
            start = max(0.0, min(start, src["duration"] - length))
            outputs.append(Output(name=f"Short {i + 1}", aspect="9:16",
                                  segments=[Segment(media_id=src["id"], start=round(start, 2),
                                                    end=round(start + length, 2), why="evenly spaced moment")],
                                  subtitles=True, title=f"{src['original_name'].rsplit('.', 1)[0][:70]} — part {i + 1}"))
    return EditPlan(summary=f"{len(outputs)} output(s) from '{src['original_name']}' (rule-based plan)",
                    outputs=outputs, unsupported=unsupported)
