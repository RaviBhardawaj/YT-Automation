"""Screenplay generation for animated / story channels.

The user's explicit request is the first thing the writer reads and overrides channel defaults.
Long episodes are written as an outline, then dialogue a few scenes per call (local and cloud
models stop around 300-700 words per answer); every part is validated before it is accepted.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from .. import ai, db
from . import characters as charmod, viral
from .cartoon import LOCATIONS

Emotion = Literal["neutral", "happy", "laugh", "angry", "sad", "scared", "surprised", "shout", "whisper"]
Mood = Literal["funny", "tense", "scary", "sad", "happy", "calm"]
Time = Literal["day", "evening", "night"]

GENRE_NOTES = {
    "comedy": "a genuinely funny comedy: escalating situation, misunderstandings, physical comedy, a running gag and "
              "a big punchline; relatable Indian everyday life; never mean-spirited",
    "horror": "an original horror story: building dread, eerie sounds described in action, a twist, a chilling ending; "
              "scary but no gore",
    "thriller": "a tense thriller with stakes, a ticking clock and a twist",
    "mystery": "a mystery with clues, red herrings and a satisfying reveal",
    "romance": "a sweet romantic story with charm and a warm ending",
    "kids": "a cheerful children's story, simple words, a gentle lesson shown not told",
    "motivation": "an inspiring story of struggle and comeback with an uplifting ending (story, not a lecture)",
    "drama": "an emotional family drama with conflict and resolution",
    "story": "an original, entertaining story with a clear beginning, middle and end",
}


class NewCharacter(BaseModel):
    key: str = Field(..., description="N1, N2, ... for characters not in the bible")
    name: str = Field(..., min_length=1, max_length=40)
    role: str = Field("", max_length=120)
    personality: str = Field("", max_length=300)
    catchphrase: str = Field("", max_length=80)
    gender: Literal["male", "female"]
    age: Literal["child", "young", "adult", "old"]
    body: Literal["thin", "average", "round", "tall"]
    skin: Literal["light", "medium", "wheat", "dark", "pale", "green"]
    outfit: str = Field(..., description="main outfit colour, e.g. blue")
    outfit2: str = Field(..., description="secondary colour, e.g. red")
    hair: Literal["short", "long", "curly", "bun", "bald", "spiky", "ponytail"]
    accessory: Literal["none", "cape", "glasses", "mustache", "turban", "cap", "mask", "tie", "hat", "bindi", "beard",
                       "cape_mask"]


class Beat(BaseModel):
    location: str = Field(..., description=f"one of: {', '.join(LOCATIONS)}")
    time: Time
    mood: Mood
    characters: list[str] = Field(..., min_length=1, max_length=4, description="character keys in this scene")
    summary: str = Field(..., min_length=10, max_length=400)


class Outline(BaseModel):
    title: str = Field(..., min_length=3, max_length=90)
    logline: str = Field(..., min_length=10, max_length=300)
    cast: list[str] = Field(..., min_length=1, max_length=6, description="keys of bible characters used")
    new_characters: list[NewCharacter] = Field(default_factory=list, max_length=4)
    beats: list[Beat] = Field(..., min_length=1, max_length=16)
    thumbnail_text: str = Field(..., min_length=2, max_length=40)
    description: str = Field(..., min_length=20, max_length=1500)
    tags: list[str] = Field(..., min_length=3, max_length=15)


class Line(BaseModel):
    speaker: str = Field(..., description="character key, or NARRATOR")
    text: str = Field(..., min_length=1, max_length=260)
    emotion: Emotion
    action: str = Field("", max_length=120)


class SceneDialogue(BaseModel):
    lines: list[Line] = Field(..., min_length=2, max_length=16)


class Dialogue(BaseModel):
    scenes: list[SceneDialogue] = Field(..., min_length=1, max_length=6)



def _learned(channel: dict[str, Any], plan: dict[str, Any] | None = None) -> str:
    from .. import learning
    return "" if (plan or {}).get("no_upload") else learning.prompt_block(channel.get("id"))

def _lang_rules(language: str) -> str:
    if language in ("Hindi", "Hinglish"):
        return ("LANGUAGE: everyday spoken Hindi/Hinglish the way Indian families and friends talk — write ALL dialogue "
                "in DEVANAGARI script (English words transliterated, e.g. 'बिजली का बिल', 'सुपरहीरो', 'मोबाइल') so the Hindi "
                "voices pronounce it. Numbers are written as digits (5, 100, 2.5) — the voices say them in English. "
                "Title, description and tags may mix Devanagari and Roman Hinglish.")
    return f"LANGUAGE: natural, conversational {language} for all dialogue and metadata."


def _devanagari_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    return sum(1 for c in letters if "ऀ" <= c <= "ॿ") / len(letters) if letters else 0.0


def recent_episodes(channel_id: int, limit: int = 12) -> list[str]:
    rows = db.q("SELECT title, package FROM productions WHERE channel_id=? AND kind='story' AND status<>'rejected' "
                "ORDER BY id DESC LIMIT ?", (channel_id, limit))
    out = []
    for r in rows:
        pkg = db.loads(r["package"], {}) or {}
        out.append(f"{r['title']} — {pkg.get('logline', '')}"[:200])
    return out


def write(plan: dict[str, Any], channel: dict[str, Any], idea: dict[str, Any] | None = None) -> dict[str, Any]:
    """Outline + dialogue. Returns the screenplay dict (not yet saved)."""
    fresh = plan.get("use_cast") is False             # one-off video: invent its own characters, no recurring cast
    bible = [] if fresh else charmod.for_channel(channel["id"])
    keys = {charmod.key_of(c) for c in bible}
    words = int(plan["long_minutes"] * 135) if plan["format"] != "short" else int(plan["short_seconds"] * 2.3)
    n_beats = max(2, min(14, round(words / 110))) if plan["format"] != "short" else 2
    genre = GENRE_NOTES.get(plan["content_type"], GENRE_NOTES["story"])
    recent = recent_episodes(channel["id"])
    head = (
        f"USER REQUEST (highest priority — follow it exactly; it overrides channel defaults): "
        f"{plan['request'] or '(none — use the selected idea and channel style)'}\n"
        + (f"SELECTED IDEA: {idea['title']} — {idea.get('summary') or idea.get('angle') or ''}\n" if idea else "")
        + f"CHANNEL: {channel['name']} — niche: {channel['niche']}. Audience: {channel['audience'] or 'general'}. "
        f"Tone: {plan['tone'] or 'set by the requested genre below (not the channel’s usual tone)'}.\n"
        + (f"CHANNEL INSTRUCTIONS: {channel['instructions']}\n" if channel.get("instructions") else "")
        + (f"CHANNEL RESTRICTIONS: {channel['restrictions']}\n" if channel.get("restrictions") else "")
        + _learned(channel, plan)
        + f"GENRE FOR THIS EPISODE: {plan['content_type']} — {genre}.\n"
        + (f"The channel usually makes {', '.join(channel.get('styles') or [])}; THIS episode is "
           f"{plan['content_type']} because the user asked for it. Keep the recurring characters if they fit, "
           f"but the genre, mood, music and ending must be {plan['content_type']}.\n"
           if plan.get("decided_by") == "request" and plan["content_type"] not in (channel.get("styles") or [])
           and plan["content_type"] != "story" else "")
        + "This is ENTERTAINMENT, not a lesson: no explanations, tutorials or 'today we will learn'. Show, don't teach.\n"
        + ("CHARACTERS: this is a stand-alone video. Do NOT use the channel's recurring characters; create fresh, "
           "original characters that fit the request (keys N1, N2 …).\n" if fresh else
           f"CHARACTER BIBLE (recurring characters — keep their names, personalities and catchphrases exactly):\n"
           f"{charmod.bible_text(bible)}\n")
        + f"RECENT EPISODES (do not repeat these plots): {'; '.join(recent) or 'none yet'}\n"
        f"{_lang_rules(plan['language'])}\n")
    outline_prompt = (head +
        f"\nPlan an original {'YouTube Short (vertical)' if plan['format'] == 'short' else 'animated episode'} of about "
        f"{words} spoken words: exactly {n_beats} scenes (beats). Use bible characters by key where they fit; invent new "
        "characters only when the story needs them (keys N1, N2 ... with a full visual design). Locations must be from the allowed list. Moods must fit the genre. thumbnail_text = 2-4 punchy "
        "words. Description: 2-3 lines, no URLs. Tags: 6-12.\n"
        + viral.HOOK_RULES + (" " + viral.SHORT_HOOK_RULES if plan["format"] == "short" else "")
        + " Beat 1 must already be inside the funniest or most surprising situation of the story.")

    def check_outline(o: Outline) -> None:
        problems = []
        new_keys = {n.key for n in o.new_characters}
        for k in o.cast:
            if k not in keys and k not in new_keys:
                problems.append(f"cast key {k} is neither in the bible nor in new_characters")
        for i, b in enumerate(o.beats, 1):
            if b.location not in LOCATIONS:
                problems.append(f"beat {i}: location must be one of {', '.join(LOCATIONS)}")
            for k in b.characters:
                if k not in keys and k not in new_keys:
                    problems.append(f"beat {i}: unknown character key {k}")
        if abs(len(o.beats) - n_beats) > 1:
            problems.append(f"write exactly {n_beats} beats")
        if plan["content_type"] == "horror" and not any(b.mood in ("scary", "tense") for b in o.beats):
            problems.append("a horror story needs scary/tense scenes")
        if plan["content_type"] == "comedy" and not any(b.mood == "funny" for b in o.beats):
            problems.append("a comedy needs funny scenes")
        if problems:
            raise ValueError("; ".join(problems[:8]))

    outline, meta = ai.generate("script", "You are the head writer of an animated YouTube series. Output one JSON object "
                                "matching the schema. No markdown.", outline_prompt, Outline, max_tokens=6000,
                                validate=check_outline)
    cast_keys = set(outline.cast) | {n.key for n in outline.new_characters}
    names = {charmod.key_of(c): c["name"] for c in bible} | {n.key: n.name for n in outline.new_characters}
    scenes_out: list[dict[str, Any]] = []
    per_call = 2 if plan["format"] != "short" else 2
    per_scene_words = max(40, words // max(1, len(outline.beats)))
    for start in range(0, len(outline.beats), per_call):
        chunk = outline.beats[start:start + per_call]
        story_so_far = " ".join(f"[{i + 1}] {b.summary}" for i, b in enumerate(outline.beats))
        prev = scenes_out[-1]["lines"][-3:] if scenes_out else []
        prompt = (head + f"\nEPISODE: {outline.title} — {outline.logline}\nFULL OUTLINE: {story_so_far}\n"
                  f"CAST KEYS AND NAMES: {', '.join(f'{k}={v}' for k, v in names.items() if k in cast_keys)}; "
                  "NARRATOR may speak short scene-setting lines.\n"
                  + (f"LAST LINES SO FAR: {' / '.join(p['text'] for p in prev)}\n" if prev else "")
                  + f"\nWrite the dialogue for scenes {start + 1}-{start + len(chunk)} only:\n"
                  + "\n".join(f"Scene {start + i + 1}: {b.location}, {b.time}, mood {b.mood}, characters "
                              f"{', '.join(b.characters)} — {b.summary}" for i, b in enumerate(chunk))
                  + f"\nAbout {per_scene_words} spoken words per scene (that is roughly 8-14 lines per scene) — build "
                  "each scene properly: setup, escalation, reaction, button. Short punchy lines (max ~25 words). Every line: speaker key, text, emotion, a short action. Only cast keys or NARRATOR may speak. "
                  + ("End the final scene with the punchline / payoff." if start + per_call >= len(outline.beats) else "")
                  + (" " + viral.HOOK_RULES + (" " + viral.SHORT_HOOK_RULES if plan["format"] == "short" else "")
                     if start == 0 else ""))

        def check_dialogue(dlg: Dialogue, chunk=chunk, strict_length: bool = True) -> None:
            problems = []
            if len(dlg.scenes) != len(chunk):
                problems.append(f"return exactly {len(chunk)} scenes")
            for si, sc in enumerate(dlg.scenes, 1):
                for ln in sc.lines:
                    if ln.speaker != charmod.NARRATOR_KEY and ln.speaker not in cast_keys:
                        problems.append(f"scene {si}: unknown speaker {ln.speaker}")
                        break
                text = " ".join(ln.text for ln in sc.lines)
                if plan["language"] in ("Hindi", "Hinglish") and _devanagari_ratio(text) < 0.6:
                    problems.append(f"scene {si}: dialogue must be in Devanagari script")
            spoken = sum(len(ln.text.split()) for sc in dlg.scenes for ln in sc.lines)
            if strict_length and spoken < 0.55 * per_scene_words * len(chunk):   # only far-too-short parts
                problems.append(f"too short: {spoken} words for {len(chunk)} scenes — write about "
                                f"{per_scene_words} words per scene with more back-and-forth and reactions")
            if problems:
                raise ValueError("; ".join(problems[:8]))

        system = ("You are a dialogue writer for an animated YouTube series. Output one JSON object matching the "
                  "schema. No markdown.")
        try:
            dlg, _ = ai.generate("script", system, prompt, Dialogue, max_tokens=6000, validate=check_dialogue)
        except ai.AllProvidersFailed as e:
            if "too short" not in str(e):
                raise
            # length must never cost the episode: accept a shorter but otherwise valid part
            dlg, _ = ai.generate("script", system, prompt, Dialogue, max_tokens=6000,
                                 validate=lambda d, chunk=chunk: check_dialogue(d, chunk, strict_length=False))
        for b, sc in zip(chunk, dlg.scenes):
            scenes_out.append({**b.model_dump(), "lines": [ln.model_dump() for ln in sc.lines]})
    return {"title": outline.title, "logline": outline.logline, "cast": sorted(cast_keys),
            "new_characters": [n.model_dump() for n in outline.new_characters], "scenes": scenes_out,
            "thumbnail_text": outline.thumbnail_text, "description": outline.description, "tags": outline.tags,
            "generated_by": meta, "words": sum(len(ln["text"].split()) for s in scenes_out for ln in s["lines"])}


def save_new_characters(channel_id: int, sp: dict[str, Any], episode_id: int, oneoff: bool = False) -> dict[str, str]:
    """New characters become recurring bible entries — or, for a stand-alone video, characters of that video only
    (status 'oneoff': never offered to later episodes). Returns {N-key: C-key} and rewrites the screenplay keys."""
    mapping: dict[str, str] = {}
    for n in sp.get("new_characters", []):
        existing = None if oneoff else db.q1(
            "SELECT id FROM characters WHERE channel_id=? AND lower(name)=lower(?) AND status NOT IN ('retired','oneoff')",
            (channel_id, n["name"]))
        if existing:
            mapping[n["key"]] = f"C{existing['id']}"
            continue
        cid = charmod.create(channel_id, n["name"], n, role=n.get("role", ""), personality=n.get("personality", ""),
                             catchphrases=[n["catchphrase"]] if n.get("catchphrase") else [],
                             rules=f"Introduced in episode #{episode_id}; keep this design and voice.",
                             status="oneoff" if oneoff else "approved")
        mapping[n["key"]] = f"C{cid}"
    if mapping:
        def m(k: str) -> str:
            return mapping.get(k, k)
        sp["cast"] = sorted({m(k) for k in sp["cast"]})
        for s in sp["scenes"]:
            s["characters"] = [m(k) for k in s["characters"]]
            for ln in s["lines"]:
                ln["speaker"] = m(ln["speaker"])
    return mapping


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("*", "").replace("#", "")).strip()
