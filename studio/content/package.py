"""Script / storyboard / metadata generation with strict validation.

The package produced here is plain data. Downstream steps only ever:
  * pass narration text to the TTS engine's Python API (no shell),
  * draw caption / headline text with Pillow (FFmpeg never sees model text),
  * use `visual_query` as a URL-encoded search parameter,
  * send title / description / tags to the YouTube API as JSON fields.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from .. import ai, db, profile, settings
from ..ai import prompts
from ..research import dedup

DEVANAGARI = re.compile(r"[ऀ-ॿ]")
LATIN = re.compile(r"[A-Za-z]")
VISUAL_QUERY_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ,'&\-]{1,79}$")
WORDS_PER_SECOND = {"hi": 2.5, "en": 2.7}


# ------------------------------------------------------------------ schemas

class Scene(BaseModel):
    narration: str = Field(..., min_length=3, max_length=700, description="Exact words the narrator speaks")
    caption_text: str = Field(..., max_length=700, description="Subtitle text for this scene; empty string = same as narration")
    visual_description: str = Field(..., min_length=5, max_length=400,
                                    description="The shot like a film director: shot size, subject + action, setting, light")
    visual_query: str = Field(..., min_length=2, max_length=80, description="2-5 ENGLISH keywords for stock media search")
    visual_type: Literal["video", "photo", "illustration", "text_card"]
    on_screen_text: str = Field(..., max_length=60, description="Optional headline overlay (max 6 words) or empty string")


class FactCheck(BaseModel):
    claim: str = Field(..., max_length=400)
    status: Literal["supported", "general_knowledge", "unverified", "opinion"]
    evidence_id: int = Field(..., description="Supporting evidence id, or 0")
    note: str = Field(..., max_length=400)


class ThumbnailPlan(BaseModel):
    concept: str = Field(..., max_length=400, description="Visual idea: subject, composition, emotion")
    text: str = Field(..., min_length=2, max_length=40, description="2-5 punchy words, different from the title")


class Chapter(BaseModel):
    title: str = Field(..., min_length=2, max_length=60)
    first_scene: int = Field(..., ge=1, description="1-based index of the chapter's first scene")


class ScriptPackage(BaseModel):
    hook: str = Field(..., min_length=5, max_length=300)
    title: str = Field(..., min_length=10, max_length=100)
    description: str = Field(..., min_length=40, max_length=3500)
    tags: list[str] = Field(..., min_length=5, max_length=20)
    scenes: list[Scene] = Field(..., min_length=4, max_length=90)
    chapters: list[Chapter] = Field(..., max_length=20)
    thumbnail: ThumbnailPlan
    fact_checks: list[FactCheck] = Field(..., max_length=25)


class OutlineChapter(BaseModel):
    title: str = Field(..., min_length=2, max_length=60)
    key_points: list[str] = Field(..., min_length=2, max_length=6)
    evidence_ids: list[int] = Field(..., max_length=8)
    target_words: int = Field(..., ge=60, le=600)


class LongOutline(BaseModel):
    working_title: str = Field(..., min_length=10, max_length=100)
    hook: str = Field(..., min_length=5, max_length=400)
    chapters: list[OutlineChapter] = Field(..., min_length=4, max_length=10)
    closing: str = Field(..., max_length=400)


class ChapterScenes(BaseModel):
    scenes: list[Scene] = Field(..., min_length=2, max_length=14)
    fact_checks: list[FactCheck] = Field(..., max_length=8)


class LongMeta(BaseModel):
    title: str = Field(..., min_length=10, max_length=100)
    description: str = Field(..., min_length=40, max_length=3500)
    tags: list[str] = Field(..., min_length=5, max_length=20)
    thumbnail: ThumbnailPlan


class ReviewIssue(BaseModel):
    scene: int = Field(..., ge=0, description="1-based scene index, or 0 for the whole video")
    category: Literal["factual", "originality", "clarity", "policy", "language"]
    severity: Literal["low", "medium", "high"]
    detail: str = Field(..., max_length=400)
    suggestion: str = Field(..., max_length=400)


class Review(BaseModel):
    issues: list[ReviewIssue] = Field(..., max_length=15)
    overall: str = Field(..., max_length=600)
    ready: bool


# ------------------------------------------------------------------ helpers

def script_text(pkg: dict[str, Any]) -> str:
    return "\n".join(s["narration"] for s in pkg["scenes"])


# letters that are neither Latin nor Devanagari (Arabic, Cyrillic, Bengali, CJK …) — a model glitch in a Hindi script
OTHER_SCRIPT = re.compile(r"[\u0370-\u03ff\u0400-\u04ff\u0590-\u08ff\u0980-\u0dff\u0e00-\u0eff\u3040-\u9fff\uac00-\ud7af]+")


# number words the voice would read in Hindi; viewers say these numbers in English, so the writer uses digits
HINDI_NUMBER_WORDS = re.compile(r"(?<![ऀ-ॿ])(?:चौथाई|प्रतिशत|फ़ीसदी|फीसदी|दशमलव)(?![ऀ-ॿ])")


def _ratio(pattern: re.Pattern, text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    return sum(1 for c in letters if pattern.match(c)) / len(letters) if letters else 0.0


def word_count(text: str) -> int:
    return len(text.split())


def targets(fmt: str) -> dict[str, int]:
    wps = WORDS_PER_SECOND.get(profile.tts_language(), 2.6)
    if fmt == "short":
        secs = int(settings.get("production.short_seconds", 50))
        return {"seconds": secs, "words": int(secs * wps), "min_words": 60, "max_words": int(170 * wps),
                "min_scenes": 5, "max_scenes": 14, "scene_max_words": 45}
    from .. import channels
    own = (channels.current() or {}).get("long_minutes")      # the channel's own length (news: short and fresh)
    minutes = float(own or settings.get("production.long_minutes", 8))
    return {"seconds": int(minutes * 60), "words": int(minutes * 60 * wps), "min_words": int(minutes * 60 * wps * 0.6),
            "max_words": int(minutes * 60 * wps * 1.5), "min_scenes": 12, "max_scenes": 90, "scene_max_words": 80}


def check_display_language(text: str) -> str | None:
    """On-screen text (overlays, thumbnail) must match the channel's display script."""
    if not text.strip():
        return None
    lang = profile.language()
    if lang == "Hindi" and _ratio(DEVANAGARI, text) < 0.6:
        return "must be written in Hindi (Devanagari script)"
    if lang in ("Hinglish", "English") and _ratio(LATIN, text) < 0.6:
        return f"must be written in {'Roman-script Hinglish' if lang == 'Hinglish' else 'English'}"
    return None


def validate_scenes(scenes: list[Scene], fmt: str, *, check_totals: bool = True) -> None:
    t = targets(fmt)
    lang = profile.language()
    problems = []
    for i, s in enumerate(scenes, start=1):
        if word_count(s.narration) > t["scene_max_words"]:
            problems.append(f"scene {i}: narration has {word_count(s.narration)} words (max {t['scene_max_words']}) — split it")
        if not VISUAL_QUERY_OK.match(s.visual_query.strip()):
            problems.append(f"scene {i}: visual_query must be plain ENGLISH keywords (letters, digits, spaces)")
        elif s.visual_type != "text_card" and VISUALS_NO_PEOPLE.search(s.visual_query):
            problems.append(f"scene {i}: visual_query must not search for a specific/real person ('{s.visual_query}') — "
                            "show the subject or a generic person doing it, or use visual_type 'text_card'")
        if lang in ("Hindi", "Hinglish") and _ratio(DEVANAGARI, s.narration) < 0.6:
            problems.append(f"scene {i}: narration must be written in Devanagari script")
        if lang == "Hinglish" and (not s.caption_text.strip() or _ratio(LATIN, s.caption_text) < 0.6):
            problems.append(f"scene {i}: caption_text must be the same sentence in Roman-script Hinglish")
        if lang in ("Hindi", "Hinglish", "English") and OTHER_SCRIPT.search(s.narration + s.caption_text):
            problems.append(f"scene {i}: contains letters from another script "
                            f"('{OTHER_SCRIPT.search(s.narration + s.caption_text).group(0)}') — use only "
                            "Devanagari and Latin letters")
        if lang in ("Hindi", "Hinglish") and HINDI_NUMBER_WORDS.search(s.narration):
            problems.append(f"scene {i}: write numbers as digits, not Hindi number words "
                            f"('{HINDI_NUMBER_WORDS.search(s.narration).group(0)}' → e.g. 25%, 2.5%)")
        if lang == "English" and _ratio(LATIN, s.narration) < 0.8:
            problems.append(f"scene {i}: narration must be English")
        if len(s.on_screen_text.split()) > 7:
            problems.append(f"scene {i}: on_screen_text max 6 words")
    # Overlays are optional: a few in the wrong script are dropped by sanitize(); only force a
    # rewrite when most of them are wrong.
    overlays = [s.on_screen_text for s in scenes if s.on_screen_text.strip()]
    wrong = [o for o in overlays if check_display_language(o)]
    if overlays and len(wrong) > len(overlays) / 2:
        problems.append(f"on_screen_text {check_display_language(wrong[0])} ({len(wrong)} of {len(overlays)} overlays are not)")
    if check_totals:
        total = sum(word_count(s.narration) for s in scenes)
        if not (t["min_words"] <= total <= t["max_words"]):
            problems.append(f"total narration is {total} words; target about {t['words']} "
                            f"(allowed {t['min_words']}-{t['max_words']})")
        if not (t["min_scenes"] <= len(scenes) <= t["max_scenes"]):
            problems.append(f"{len(scenes)} scenes; need {t['min_scenes']}-{t['max_scenes']}")
    if problems:
        raise ValueError("; ".join(problems[:10]))


def validate_metadata(title: str, tags: list[str], competitor_titles: list[str]) -> None:
    problems = []
    if "<" in title or ">" in title:
        problems.append("title must not contain < or >")
    # str.isupper() is True for "आदित्य L1" (Devanagari is uncased), so judge Latin letters only
    latin = [c for c in title if LATIN.match(c)]
    if len(latin) >= 8 and sum(c.isupper() for c in latin) / len(latin) > 0.8:
        problems.append("title must not be ALL CAPS")
    if sum(len(t) + 3 for t in tags) > 480:
        problems.append("tags are too long in total (max ~450 characters)")
    for other in competitor_titles:
        if dedup.similarity(title, other) >= 0.8:
            problems.append(f"title is too similar to an existing video ({other[:60]!r}); write an original title")
            break
    if problems:
        raise ValueError("; ".join(problems))


def validate_facts(checks: list[FactCheck], valid_ids: set[int]) -> None:
    bad = [c.evidence_id for c in checks if c.evidence_id and c.evidence_id not in valid_ids]
    if bad:
        raise ValueError(f"fact_checks cite evidence ids that do not exist: {bad}; use 0 or a listed id")
    unsupported = [c.claim[:50] for c in checks if c.status == "supported" and not c.evidence_id]
    if unsupported:
        raise ValueError(f"status 'supported' requires an evidence_id: {unsupported}")


def _clean(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text.replace("<", "").replace(">", "")).strip()


# Emoji and pictographs: fine in titles, but TTS would read them aloud and caption fonts can't draw them.
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍⬀-⯿]")


def _spoken(text: str) -> str:
    return _clean(_EMOJI.sub("", text))


def sanitize(pkg: dict[str, Any]) -> dict[str, Any]:
    pkg["title"] = _clean(pkg["title"])[:100]
    pkg["description"] = _clean(pkg["description"])
    seen, tags, total = set(), [], 0
    for t in pkg["tags"]:
        t = _clean(t).lstrip("#")[:30]
        if t and t.lower() not in seen and total + len(t) + 3 <= 450:
            seen.add(t.lower())
            tags.append(t)
            total += len(t) + 3
    pkg["tags"] = tags
    for s in pkg["scenes"]:
        s["narration"] = _spoken(s["narration"])
        s["caption_text"] = _spoken(s["caption_text"]) or s["narration"]
        s["on_screen_text"] = _spoken(s["on_screen_text"])
        if check_display_language(s["on_screen_text"]):
            s["on_screen_text"] = ""          # wrong script for this channel: drop the optional overlay
        s["visual_query"] = re.sub(r"[^A-Za-z0-9 ,'&\-]", "", s["visual_query"]).strip()[:80]
    pkg["thumbnail"]["text"] = _spoken(pkg["thumbnail"]["text"])
    return pkg


# ------------------------------------------------------------------ generation

def _context(topic: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    requested = topic.get("idea_kind") == "request"
    return (f"CHANNEL PROFILE: {profile.summary_text()}\n"
            + ("SUBJECT RULE: the creator chose this video's subject (TOPIC and ANGLE below). Use the channel "
               "profile only for language, tone and audience — do NOT turn the video toward the channel's usual "
               "niche or mention it unless the topic itself is about it.\n" if requested else "")
            + f"TOPIC: {topic['title']}\nORIGINAL ANGLE: {topic.get('angle') or '-'}\n"
            f"HOOK IDEA: {topic.get('hook_idea') or '-'}\n\n"
            f"{prompts.evidence_block(evidence, max_chars=16000, content_chars=1400)}\n\n"
            f"{profile.language_rules()}\n"
            + _learned(topic))


def _learned(topic: dict[str, Any]) -> str:
    """This channel's lessons — only for videos made for the channel (not stand-alone 'no channel' videos)."""
    from .. import learning
    return learning.prompt_block(topic.get("channel_id"))


STYLE_NOTES = {
    "funny": "light humour and playful comparisons, never mocking real people",
    "educational": "clear explanations, one idea per scene, concrete examples",
    "documentary": "calm, cinematic narration with vivid scene-setting",
    "storytelling": "narrative arc with tension and a payoff",
    "tutorials": "step-by-step, practical, show the result first",
}


def _style() -> str:
    return "; ".join(STYLE_NOTES.get(s, s) for s in profile.styles())


VISUALS_NO_PEOPLE = re.compile(          # roles that point at one real, identifiable person
    r"\b(face|faces|portrait|selfie|leader|minister|president|politician|ceo|founder|actor|actress|celebrity|"
    r"chairman|prime minister|governor|mayor|king|queen)\b", re.I)

VISUAL_RULES = (
    "VISUALS: every scene needs a concrete, filmable visual that a viewer instantly connects to what is being said. "
    "visual_query = 2-5 plain ENGLISH keywords for stock footage search naming THIS scene's specific subject and "
    "action (e.g. 'glucose meter finger test', 'doctor examining diabetic foot', 'stock market candlestick chart') — "
    "never vague words alone like 'technology', 'business', 'success', 'abstract', 'concept'. Generic people doing "
    "the thing are fine (a doctor, a patient, a student, a family); NEVER a named or real, identifiable person, face "
    "portrait or public figure (a stock photo of a random person must never stand in for a real person). When a "
    "scene is about a specific person, use visual_type 'text_card' with their name/role as on_screen_text. "
    "visual_description = one sentence written like a film director's shot: shot size (wide / medium / close-up), "
    "the subject and what it is doing, the setting, and the light or time of day (e.g. 'Close-up of an elderly "
    "man's hands pricking a fingertip with a glucose meter, kitchen table, soft morning window light'). It is used "
    "to find or generate a real-looking picture, so make it concrete and photographable. Plan the scenes like a "
    "film: open with a strong establishing or hook shot and vary shot sizes. No brand names or text. This is the ONLY "
    "field that is always English. on_screen_text is shown to viewers, so it uses the channel's display language "
    "and script (see LANGUAGE). visual_type: 'video' for motion, 'photo' for a still, 'illustration' for abstract "
    "concepts, 'text_card' for a key number or statement shown as a designed card. Vary visuals from scene to scene.")


def generate_short(topic: dict[str, Any], evidence: list[dict[str, Any]], feedback: str = "") -> tuple[dict[str, Any], dict]:
    t = targets("short")
    valid_ids = {e["id"] for e in evidence}
    competitor_titles = [e["title"] for e in evidence if e["source_type"] == "youtube"]
    prompt = (_context(topic, evidence) +
              f"\nWrite a complete YouTube SHORT (vertical, ~{t['seconds']} seconds, about {t['words']} spoken words, "
              f"{t['min_scenes']}-10 scenes of 1-2 short sentences each). STYLE: {_style()}.\n"
              "STRUCTURE: scene 1 is the hook — the first sentence creates curiosity or tension in under 12 words; "
              "no greetings, no channel intro, no 'like and subscribe' at the start. Build quickly, deliver a clear "
              "payoff, end with one short line inviting a comment or follow.\n"
              f"{VISUAL_RULES}\n"
              "METADATA: title <= 70 characters, specific and honest; description 2-3 short paragraphs (no URLs — the "
              "app appends sources and credits); 8-15 tags; chapters = [] for Shorts. Thumbnail text 2-5 words.\n"
              "FACT CHECKS: list every factual claim in the narration with its status and the evidence id that "
              "supports it (0 if none). Prefer claims you can support.\n" + feedback)

    def check(p: ScriptPackage) -> None:
        validate_scenes(p.scenes, "short")
        validate_metadata(p.title, p.tags, competitor_titles)
        validate_facts(p.fact_checks, valid_ids)
        thumb_problem = check_display_language(p.thumbnail.text)
        if thumb_problem:
            raise ValueError(f"thumbnail.text {thumb_problem}")

    pkg, meta = ai.generate("script", prompts.system("an expert short-form scriptwriter"), prompt, ScriptPackage,
                            max_tokens=9000, validate=check)
    out = pkg.model_dump()
    out["chapters"] = []
    return sanitize(out), meta


def generate_long(topic: dict[str, Any], evidence: list[dict[str, Any]], feedback: str = "",
                  partial: dict[str, Any] | None = None,
                  checkpoint: Any = None) -> tuple[dict[str, Any], dict]:
    """Outline -> chapters -> metadata. `partial`/`checkpoint` let a retry resume from the last
    completed step instead of paying for the whole script again."""
    t = targets("long")
    valid_ids = {e["id"] for e in evidence}
    competitor_titles = [e["title"] for e in evidence if e["source_type"] == "youtube"]
    ctx = _context(topic, evidence)
    partial = partial if partial is not None else {}

    def save() -> None:
        if checkpoint:
            checkpoint(partial)
    outline_prompt = (ctx + f"\nPlan a LONG-FORM YouTube video (horizontal, about {t['seconds'] // 60} minutes, "
                      f"~{t['words']} spoken words). STYLE: {_style()}. Create 5-8 chapters with key points, the "
                      f"evidence ids each chapter relies on, and target word counts that add up to about {t['words']}. "
                      "Open with a strong hook (no greetings or channel intro), keep pacing varied, and close with a "
                      "satisfying conclusion.\n" + feedback)

    def check_outline(o: LongOutline) -> None:
        total = sum(c.target_words for c in o.chapters)
        if not (t["min_words"] <= total <= t["max_words"]):
            raise ValueError(f"chapter target_words add up to {total}; need about {t['words']}")

    if partial.get("outline"):
        outline, meta = LongOutline(**partial["outline"]), partial["outline_meta"]
    else:
        outline, meta = ai.generate("script", prompts.system("an expert documentary scriptwriter"), outline_prompt,
                                    LongOutline, max_tokens=5000, validate=check_outline)
        partial.update(outline=outline.model_dump(), outline_meta=meta, chapters={})
        save()
    scenes: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    chapters: list[dict[str, Any]] = []
    total_cost = meta.get("cost_usd", 0.0)
    for idx, ch in enumerate(outline.chapters, start=1):
        done = partial.setdefault("chapters", {}).get(str(idx))
        if done:
            chapters.append({"title": ch.title, "first_scene": len(scenes) + 1})
            scenes.extend(done["scenes"])
            facts.extend(done["fact_checks"])
            continue
        prev = " ".join(s["narration"] for s in scenes[-2:])
        ch_prompt = (ctx + f"\nWORKING TITLE: {outline.working_title}\nFULL OUTLINE: "
                     + " | ".join(f"{i}. {c.title}" for i, c in enumerate(outline.chapters, start=1))
                     + f"\n\nWrite chapter {idx}: '{ch.title}'. Key points: {'; '.join(ch.key_points)}. "
                     f"Relevant evidence ids: {ch.evidence_ids}. About {ch.target_words} spoken words in 3-10 scenes "
                     f"(1-3 sentences each). STYLE: {_style()}. "
                     + ("Begin with this hook: " + outline.hook + " " if idx == 1 else "")
                     + (f"Continue naturally from: \"{prev[-300:]}\" " if prev else "")
                     + ("End with this closing idea: " + outline.closing if idx == len(outline.chapters) else "")
                     + f"\n{VISUAL_RULES}\nList fact checks for the claims in this chapter.")

        def check_ch(c: ChapterScenes, want: int = ch.target_words) -> None:
            validate_scenes(c.scenes, "long", check_totals=False)
            validate_facts(c.fact_checks, valid_ids)
            got = sum(word_count(s.narration) for s in c.scenes)
            if got < want * 0.7:          # caught here, so the writer expands it now (not after the whole script)
                raise ValueError(f"this chapter has only {got} spoken words; write about {want} — add more detail, "
                                 "context and explanation from the evidence (no padding or repetition)")

        part, m = ai.generate("script", prompts.system("an expert documentary scriptwriter"), ch_prompt,
                              ChapterScenes, max_tokens=7000, validate=check_ch)
        total_cost += m.get("cost_usd", 0.0)
        chapters.append({"title": ch.title, "first_scene": len(scenes) + 1})
        scenes.extend(s.model_dump() for s in part.scenes)
        facts.extend(f.model_dump() for f in part.fact_checks)
        partial["chapters"][str(idx)] = {"scenes": [s.model_dump() for s in part.scenes],
                                         "fact_checks": [f.model_dump() for f in part.fact_checks]}
        save()

    validate_scenes([Scene(**s) for s in scenes], "long")
    meta_prompt = (ctx + "\nThe script is written. Chapters: " + " | ".join(c["title"] for c in chapters)
                   + "\nScript opening: " + " ".join(s["narration"] for s in scenes[:4])[:800]
                   + "\n\nWrite the final YouTube metadata: title <= 70 characters, specific and honest; description "
                   "2-4 short paragraphs (no URLs, no timestamps — the app adds chapters, sources and credits); 10-15 "
                   "tags; thumbnail concept and 2-5 word thumbnail text different from the title.")

    def check_meta(x: LongMeta) -> None:
        validate_metadata(x.title, x.tags, competitor_titles)
        thumb_problem = check_display_language(x.thumbnail.text)
        if thumb_problem:
            raise ValueError(f"thumbnail.text {thumb_problem}")

    md, m = ai.generate("script", prompts.system("a YouTube packaging expert"), meta_prompt, LongMeta,
                        max_tokens=3000, validate=check_meta)
    total_cost += m.get("cost_usd", 0.0)
    pkg = {"hook": outline.hook, "title": md.title, "description": md.description, "tags": md.tags,
           "scenes": scenes, "chapters": chapters, "thumbnail": md.thumbnail.model_dump(), "fact_checks": facts[:25]}
    meta = {**meta, "cost_usd": round(total_cost, 5)}
    return sanitize(pkg), meta


def originality(pkg: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    texts = [(e.get("content") or "") + " " + (e.get("snippet") or "") for e in evidence]
    ratio = dedup.overlap_ratio(script_text(pkg), texts)
    yt_titles = [e["title"] for e in evidence if e["source_type"] == "youtube"]
    closest = max((dedup.similarity(pkg["title"], t) for t in yt_titles), default=0.0)
    return {"copied_7gram_ratio": round(ratio, 3), "closest_competitor_title_similarity": round(closest, 2),
            "ok": ratio <= 0.08 and closest < 0.8}


def review(pkg: dict[str, Any], topic: dict[str, Any], evidence: list[dict[str, Any]]) -> tuple[dict[str, Any], dict]:
    numbered = "\n".join(f"{i}. {s['narration']}" for i, s in enumerate(pkg["scenes"], start=1))
    prompt = (f"CHANNEL PROFILE: {profile.summary_text()}\nTOPIC: {topic['title']}\n\n"
              f"{prompts.evidence_block(evidence, max_chars=12000, content_chars=1000)}\n\n"
              f"TITLE: {pkg['title']}\nSCRIPT (numbered scenes):\n{numbered}\n\n"
              f"FACT CHECKS CLAIMED BY THE WRITER: {db.dumps(pkg['fact_checks'])}\n\n"
              "Review this script as a strict editor. Flag: claims not supported by the evidence or likely false "
              "(factual); passages that look copied or too close to a source or competitor (originality); confusing "
              "or slow passages (clarity); anything against YouTube Community Guidelines or unsafe for the audience, "
              "and misleading title promises (policy); unnatural language or wrong script (language). Use severity "
              "'high' only for problems that must be fixed before publishing. Set ready=false if any high issue.")
    result, meta = ai.generate("review", prompts.system("a strict fact-checking editor"), prompt, Review, max_tokens=4000)
    return result.model_dump(), meta


class SceneFix(BaseModel):
    scene: int = Field(..., ge=1, description="1-based scene index being rewritten")
    narration: str = Field(..., min_length=3, max_length=700)
    caption_text: str = Field("", max_length=700, description="Subtitle for this scene (see LANGUAGE); empty = same as narration")
    on_screen_text: str = Field(..., max_length=60, description="Headline overlay (max 6 words) or empty string")


class SceneFixes(BaseModel):
    fixes: list[SceneFix] = Field(..., min_length=1, max_length=15)


def fix_scenes(pkg: dict[str, Any], issues: list[dict[str, Any]], topic: dict[str, Any],
               evidence: list[dict[str, Any]]) -> tuple[dict[str, Any], dict]:
    """Rewrite ONLY the scenes the editor flagged (a full rewrite tends to bring in new unsupported details).
    Everything else — other scenes, visuals, title — is kept exactly as it was."""
    flagged = sorted({i["scene"] for i in issues if 1 <= i["scene"] <= len(pkg["scenes"])})
    if not flagged:
        raise ValueError("no scene to fix")
    lines = "\n".join(f"{n}. {pkg['scenes'][n - 1]['narration']}  [overlay: {pkg['scenes'][n - 1]['on_screen_text']}]"
                      for n in flagged)
    notes = "\n".join(f"- scene {i['scene']}: {i['detail']} → {i['suggestion']}" for i in issues)
    prompt = (f"CHANNEL PROFILE: {profile.summary_text()}\nTOPIC: {topic['title']}\n\n"
              f"{prompts.evidence_block(evidence, max_chars=12000, content_chars=1000)}\n\n"
              f"SCENES TO FIX:\n{lines}\n\nEDITOR'S PROBLEMS:\n{notes}\n\n"
              f"{profile.language_rules()}\n"
              "Rewrite only these scenes so every problem is gone. Keep the same language, script, tone and about "
              "the same length. Use only facts, numbers and outlet names that appear in the EVIDENCE; when unsure, "
              "say it more generally instead of adding a detail. Return one fix per scene listed.")

    def check(r: SceneFixes) -> None:
        if {f.scene for f in r.fixes} != set(flagged):
            raise ValueError(f"return exactly one fix for each of scenes {flagged}")
        lang, problems = profile.language(), []
        for f in r.fixes:                       # same language checks as a full script (validate_scenes)
            if lang in ("Hindi", "Hinglish") and _ratio(DEVANAGARI, f.narration) < 0.6:
                problems.append(f"scene {f.scene}: narration must be written in Devanagari script")
            if lang == "Hinglish" and (not f.caption_text.strip() or _ratio(LATIN, f.caption_text) < 0.6):
                problems.append(f"scene {f.scene}: caption_text must be the same sentence in Roman-script Hinglish")
            if lang in ("Hindi", "Hinglish", "English") and OTHER_SCRIPT.search(f.narration + f.caption_text):
                problems.append(f"scene {f.scene}: use only Devanagari and Latin letters")
        if problems:
            raise ValueError("; ".join(problems))
    result, meta = ai.generate("script", prompts.system("a careful news script editor"), prompt, SceneFixes,
                               max_tokens=3000, validate=check)
    for f in result.fixes:
        s = pkg["scenes"][f.scene - 1]
        s["narration"] = _spoken(f.narration)
        s["caption_text"] = _spoken(f.caption_text) or s["narration"]
        overlay = _spoken(f.on_screen_text)
        s["on_screen_text"] = "" if check_display_language(overlay) else overlay
    return pkg, meta


def suggest_publish_time(fmt: str) -> dict[str, Any]:
    """Next free slot from the configured publishing slots (local time)."""
    slots = settings.get("publish.slots").get(fmt) or ["18:00"]
    taken = {r["publish_at"] for r in db.q("SELECT publish_at FROM productions WHERE publish_at IS NOT NULL")}
    now = datetime.now().astimezone()
    for day in range(0, 30):
        for slot in sorted(slots):
            hh, mm = (int(x) for x in slot.split(":"))
            local = (now + timedelta(days=day)).replace(hour=hh, minute=mm, second=0, microsecond=0)
            if local <= now + timedelta(hours=1):
                continue
            at = db.iso(local)
            if at not in taken:
                return {"local": local.strftime("%a %d %b %Y, %H:%M %Z"), "utc": at,
                        "local_input": local.strftime("%Y-%m-%dT%H:%M"),
                        "basis": "next free slot from your publishing schedule (Settings › YouTube). "
                                 "No performance data is used until your own analytics accumulate."}
    return {"local": "", "utc": None, "basis": "no free slot in the next 30 days"}
