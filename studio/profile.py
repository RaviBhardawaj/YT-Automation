"""The channel profile captured by the first-run interview."""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Any, Iterator

from . import settings

QUESTIONS: list[dict[str, Any]] = [
    {"key": "niche_mode", "q": "Do you want me to research and recommend a niche, or do you already have one?",
     "type": "choice", "options": [["research", "Research and recommend niches for me"], ["have_one", "I already have a niche"]],
     "followup": {"when": "have_one", "key": "niche", "type": "text", "label": "Describe your niche",
                  "placeholder": "e.g. Space & astronomy facts for curious teens"}},
    {"key": "language", "q": "Which language should the channel use?",
     "type": "choice", "options": [["Hindi", "Hindi (Devanagari)"], ["Hinglish", "Hinglish"], ["English", "English"], ["Other", "Another language"]],
     "followup": {"when": "Other", "key": "language_other", "type": "text", "label": "Which language?", "placeholder": "e.g. Marathi"}},
    {"key": "formats", "q": "Do you want Shorts, long-form videos, or both?",
     "type": "choice", "options": [["shorts", "Shorts (vertical, ≤ 3 min)"], ["long", "Long-form (horizontal)"], ["both", "Both"]]},
    {"key": "cadence", "q": "How many videos should be produced?", "type": "cadence"},
    {"key": "audience", "q": "Who is the target audience?", "type": "audience"},
    {"key": "styles", "q": "Which style do you prefer?", "type": "multi",
     "options": [["funny", "Funny"], ["educational", "Educational"], ["documentary", "Documentary"],
                 ["storytelling", "Storytelling"], ["tutorials", "Tutorials"], ["other", "Other"]],
     "followup": {"when": "other", "key": "style_other", "type": "text", "label": "Describe the style"}},
    {"key": "has_channel", "q": "Do you already have a YouTube channel?", "type": "choice",
     "options": [["yes", "Yes"], ["no", "Not yet"]],
     "followup": {"when": "yes", "key": "channel_handle", "type": "text", "label": "Channel handle or URL (optional)",
                  "placeholder": "@yourchannel", "optional": True}},
    {"key": "publish_mode", "q": "Should videos be published automatically after validation, or should you approve them first?",
     "type": "choice", "options": [["approve_first", "I approve every video first (recommended)"],
                                   ["auto_after_validation", "Publish automatically after all checks pass"]]},
]


def answers() -> dict[str, Any]:
    return settings.get("interview") or {}


def complete() -> bool:
    return bool(settings.get("interview_complete"))


AUDIENCES = {"india": "India (Hindi)", "international": "Other countries (English)"}
_current_language: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_language", default=None)


def language_for(audience: str) -> str:
    return (settings.get("channel.audience_languages") or {}).get(audience) or "Hindi"


def _channel() -> dict[str, Any] | None:
    from . import channels
    return channels.current()


def language() -> str:
    """Language of the video being produced right now (set per production), else its channel's, else India's."""
    return _current_language.get() or (_channel() or {}).get("language") or language_for("india")


@contextmanager
def using_language(lang: str | None) -> Iterator[None]:
    token = _current_language.set(lang or None)
    try:
        yield
    finally:
        _current_language.reset(token)


def formats() -> list[str]:
    ch = _channel()
    if ch:
        from . import channels
        return channels.formats(ch)
    f = answers().get("formats", "shorts")
    return {"shorts": ["short"], "long": ["long"], "both": ["short", "long"]}.get(f, ["short"])


def styles() -> list[str]:
    ch = _channel()
    if ch and ch.get("styles"):
        return ch["styles"]
    a = answers()
    s = [x for x in a.get("styles", ["educational"]) if x != "other"]
    if a.get("style_other"):
        s.append(a["style_other"])
    return s or ["educational"]


def audience() -> str:
    ch = _channel()
    if ch and ch.get("audience"):
        return ch["audience"]
    a = answers()
    age = {"kids": "children under 13", "teens": "teenagers", "adults": "adults", "all": "a general audience"}.get(
        a.get("audience_age", "all"), "a general audience")
    return f"{a.get('audience', '').strip() or 'curious viewers'} ({age})"


def made_for_kids() -> bool:
    return answers().get("audience_age") == "kids"


def cadence() -> dict[str, dict[str, Any]]:
    c = answers().get("cadence") or {}
    out = {}
    for fmt in formats():
        key = "shorts" if fmt == "short" else "long"
        v = c.get(key) or {"count": 1, "period": "day" if fmt == "short" else "week"}
        out[fmt] = {"count": max(0, int(v.get("count", 1))), "period": v.get("period", "week")}
    return out


def per_day(fmt: str) -> float:
    c = cadence().get(fmt)
    if not c:
        return 0.0
    return c["count"] if c["period"] == "day" else c["count"] / 7.0


def summary_text() -> str:
    ch = _channel()
    if ch:
        return (f"Channel: {ch['name']}. Niche: {ch['niche']}" + (f" ({ch['sub_niche']})" if ch.get("sub_niche") else "")
                + f". Language: {language()}. Formats: {', '.join(formats())}. Audience: {audience()}. "
                f"Style: {', '.join(styles()) or 'per request'}. Tone: {ch.get('tone') or 'natural'}."
                + (f" Channel instructions: {ch['instructions']}" if ch.get("instructions") else "")
                + (f" Restrictions: {ch['restrictions']}" if ch.get("restrictions") else ""))
    a = answers()
    return (f"Language: {language()}. Formats: {', '.join(formats())}. Audience: {audience()}. "
            f"Style: {', '.join(styles())}. Niche: {a.get('niche') or '(to be researched)'}.")


def language_rules() -> str:
    lang = language()
    if lang == "Hindi":
        return ("LANGUAGE: Natural, conversational Hindi in Devanagari script for narration, caption_text, title, "
                "description, tags, on_screen_text and thumbnail text. Use everyday English loan words the way Indian "
                "viewers speak, but write them in Devanagari inside narration so the Hindi voice pronounces them.")
    if lang == "Hinglish":
        return ("LANGUAGE: Hinglish. `narration` MUST be written in DEVANAGARI script (English words transliterated, "
                "e.g. 'टेक्नोलॉजी') because a Hindi text-to-speech voice reads it. `caption_text` MUST contain the SAME "
                "sentences written in Roman-script Hinglish (e.g. 'Aaj hum technology ki baat karenge'). Title, "
                "description, tags, on_screen_text and thumbnail text use Roman-script Hinglish.")
    if lang == "English":
        return "LANGUAGE: Clear, conversational English everywhere."
    return f"LANGUAGE: Write narration, captions and metadata in {lang}."


def tts_language() -> str:
    """Language code for the TTS engine."""
    return {"Hindi": "hi", "Hinglish": "hi", "English": "en"}.get(language(), "en")
