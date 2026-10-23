"""Turn a creative request into a production plan.

Priority (highest first):
  1. the user's explicit request for this job   (content type, animation, format, language, duration, subject)
  2. the selected channel's configuration       (niche, workflow, language, tone, formats, durations)
  3. the selected idea and its metadata
  4. the channel's established styles
  5. system defaults
A one-off request never changes the channel's stored configuration.
"""
from __future__ import annotations

import re
from typing import Any

# content type -> words that signal it (English, Hinglish and Hindi)
TYPE_WORDS: dict[str, list[str]] = {
    "horror": ["horror", "scary", "ghost", "haunted", "spooky", "creepy", "bhoot", "bhutiya", "chudail", "darawani",
               "डरावनी", "डरावना", "भूत", "हॉरर", "चुड़ैल", "भूतिया"],
    "comedy": ["funny", "comedy", "hilarious", "comic", "joke", "jokes", "humor", "humour", "parody", "mazedaar",
               "मज़ेदार", "मजेदार", "कॉमेडी", "हंसी", "हास्य", "फनी"],
    "thriller": ["thriller", "suspense", "chase", "heist", "रोमांचक", "सस्पेंस"],
    "mystery": ["mystery", "detective", "whodunit", "रहस्य", "जासूस"],
    "romance": ["romance", "romantic", "love story", "प्रेम कहानी", "रोमांस"],
    "kids": ["kids", "children", "bedtime", "nursery", "बच्चों", "बाल कहानी"],
    "motivation": ["motivation", "motivational", "inspiring", "inspirational", "प्रेरणा", "प्रेरक", "मोटिवेशन"],
    "drama": ["drama", "emotional", "family story", "ड्रामा", "भावुक"],
    "finance": ["finance", "stock", "stocks", "share market", "stock market", "nifty", "sensex", "invest", "investing",
                "mutual fund", "trading", "शेयर", "मार्केट", "निवेश", "निफ्टी", "फाइनेंस"],
    "tech": ["technology", "tech", " ai ", "artificial intelligence", "gadget", "smartphone", "software", "टेक्नोलॉजी",
             "तकनीक"],
    "documentary": ["documentary", "history of", "true story", "डॉक्यूमेंट्री", "इतिहास"],
    "news": ["news", "latest update", "breaking", "समाचार", "खबर"],
    "education": ["explain", "explaining", "explainer", "how does", "how do", "what is", "tutorial", "guide",
                  "lesson", "teach", "learn", "समझाएं", "समझाइए", "सीखें", "क्या है", "कैसे काम"],
}
STORY_WORDS = ["story", "kahani", "कहानी", "episode", "tale", "plot", "किस्सा"]
ANIMATION_WORDS = ["animated", "animation", "cartoon", "toon", "anime", "कार्टून", "एनिमेटेड", "एनीमेशन"]
CREATIVE_TYPES = {"horror", "comedy", "thriller", "mystery", "romance", "kids", "motivation", "drama", "story"}
FACTUAL_TYPES = {"finance", "tech", "documentary", "news", "education"}
# when several types match, the more specific creative genre wins over generic words
PRIORITY = ["horror", "comedy", "thriller", "mystery", "romance", "kids", "motivation", "drama", "finance", "tech",
            "documentary", "news", "education"]


def _has(text: str, word: str) -> bool:
    if re.search(r"[ऀ-ॿ]", word):
        return word in text
    return re.search(rf"(?<![a-z]){re.escape(word.strip())}(?![a-z])", text) is not None


def detect_types(text: str) -> list[str]:
    t = f" {text.lower()} "
    return [k for k in PRIORITY if any(_has(t, w) for w in TYPE_WORDS[k])]


def plan(request: str, channel: dict[str, Any] | None = None, idea: dict[str, Any] | None = None) -> dict[str, Any]:
    """Decide content type, workflow, format, language and duration for one job."""
    ch = channel or {}
    req = (request or "").strip()
    text = f"{req} {(idea or {}).get('title', '') if not req else ''}"
    low = f" {text.lower()} "
    explicit = detect_types(text)
    creative = [x for x in explicit if x in CREATIVE_TYPES]
    factual = [x for x in explicit if x in FACTUAL_TYPES]
    animated = any(_has(low, w) for w in ANIMATION_WORDS)
    storyish = any(_has(low, w) for w in STORY_WORDS)
    source = "request"
    if creative:
        content_type = creative[0]
    elif factual and not (animated and not factual[0] in ("finance", "tech")):
        content_type = factual[0]
    elif animated or storyish:
        content_type = "story"
    else:                                       # nothing explicit: idea, then channel styles, then default
        source = "idea" if idea and idea.get("style") else "channel"
        hint = (idea or {}).get("style") or next(iter(ch.get("styles") or []), "") or ""
        content_type = hint if hint in CREATIVE_TYPES | FACTUAL_TYPES else (
            "story" if ch.get("workflow") == "animated_story" else "education")
    # workflow: explicit animation/story wins; finance on the NIFTY channel without a request stays daily
    if content_type in CREATIVE_TYPES or animated:
        workflow = "animated_story"
    else:
        workflow = "factual"
    # format
    if re.search(r"\b(shorts?|reels?|vertical)\b|शॉर्ट", low):
        fmt = "short" if not re.search(r"\b(long|full episode)\b", low) else "both"
    elif re.search(r"\b(long|full[- ]length|episode|\d+\s*(min|minute)s?)\b|लंबा|मिनट", low):
        fmt = "long"
    else:
        f = ch.get("formats", "both")
        fmt = {"short": "short", "long": "long"}.get(f, "both")
    # language
    if re.search(r"\bhinglish\b", low):
        language = "Hinglish"
    elif re.search(r"\bhindi\b|हिंदी|हिन्दी", low):
        language = "Hindi"
    elif re.search(r"\benglish\b|अंग्रेज़ी", low):
        language = "English"
    elif re.search(r"[ऀ-ॿ]", req) and not ch.get("language"):
        language = "Hindi"
    else:
        language = ch.get("language") or "Hindi"
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:min|minute|मिनट)", low)
    minutes = float(m.group(1)) if m else float(ch.get("long_minutes") or (8 if workflow == "animated_story" else 8))
    s = re.search(r"(\d+)\s*(?:sec|second|सेकंड)", low)
    short_seconds = int(s.group(1)) if s else int(ch.get("short_seconds") or 50)
    return {"request": req, "content_type": content_type, "workflow": workflow, "animated": workflow == "animated_story",
            "format": fmt, "language": language, "long_minutes": max(1.0, min(20.0, minutes)),
            "short_seconds": max(15, min(120, short_seconds)),
            # the channel's tone belongs to the channel's own genres; a request for another genre sets its own tone
            "tone": (ch.get("tone") or "") if source != "request" or content_type in (ch.get("styles") or [])
            or content_type == "story" else "",
            "decided_by": source, "matched_types": explicit, "idea_id": (idea or {}).get("id")}
