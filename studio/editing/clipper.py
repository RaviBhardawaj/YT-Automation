"""AI Shorts clipper: one long video → its best vertical moments for YouTube Shorts, Instagram Reels and Facebook Reels.

The transcript (local Whisper, with word timings) is read in windows. For each window the AI proposes
self-contained moments with a strong first line and rates them; every proposal is then snapped to whole
lines of the real transcript, kept inside the chosen length, de-duplicated and ranked. The best N become
outputs of the normal editing engine (face-centred 9:16 crop, burned-in captions, a hook headline, a cover).
Each clip also gets ready-to-paste titles, captions and hashtags per platform. Without the AI (or without
speech) a rule-based picker still finds moments, so a job never fails only because a model is unavailable.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from .. import ai
from .planner import EditPlan, Output, Segment

LENGTHS = {"short": (15.0, 30.0), "medium": (30.0, 60.0), "long": (60.0, 90.0)}
LENGTH_LABELS = {"short": "15–30 seconds", "medium": "30–60 seconds", "long": "60–90 seconds"}
MAX_CLIPS = 15
WINDOW_CHARS = 9000
IG_HASHTAGS = 5                     # Instagram allows at most 5 hashtags per post
FB_HASHTAGS = 3
YT_HASHTAGS = 5
WEIGHTS = {"hook": 0.35, "value": 0.25, "emotion": 0.2, "standalone": 0.2}
SCRIPTS = (("Hindi", r"[ऀ-ॿ]"), ("Bengali", r"[ঀ-৿]"), ("Gujarati", r"[઀-૿]"),
           ("Tamil", r"[஀-௿]"), ("Telugu", r"[ఀ-౿]"))


def auto_count(duration: float, length: str = "medium") -> int:
    """About one clip per 2.5 minutes of source, 1–10, never more clips than the video can hold."""
    lo, _ = LENGTHS.get(length, LENGTHS["medium"])
    fit = max(1, int(duration // lo))
    return max(1, min(10, fit, round(duration / 150) or 1))


def spoken_language(transcript: list[dict[str, Any]]) -> str:
    text = " ".join(s["text"] for s in transcript)[:4000]
    letters = len(re.findall(r"\w", text)) or 1
    for name, pat in SCRIPTS:
        if len(re.findall(pat, text)) / letters > 0.3:
            return name
    return "English"


# ------------------------------------------------------------------ AI moment finder

class Scores(BaseModel):
    hook: float = Field(ge=0, le=10, description="how strongly the first line stops a scrolling viewer")
    value: float = Field(ge=0, le=10, description="insight, payoff, humour or story")
    emotion: float = Field(ge=0, le=10)
    standalone: float = Field(ge=0, le=10, description="understandable without the rest of the video")


class Moment(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    title: str = Field(description="catchy but honest title, at most 70 characters")
    hook: str = Field(description="3–8 word on-screen headline for the first seconds")
    reason: str = Field(description="one sentence: why this moment works as a Short")
    keywords: list[str] = Field(default_factory=list, description="2–5 spoken words to highlight in the captions")
    scores: Scores


class Moments(BaseModel):
    moments: list[Moment] = Field(default_factory=list)


SYSTEM = ("You are a top short-form video editor for YouTube Shorts, Instagram Reels and Facebook Reels. You find "
          "the moments of a long video that work on their own as vertical clips. Use only timestamps that appear in "
          "the transcript. Output one JSON object matching the schema, no commentary.")


def windows(tr: list[dict[str, Any]], max_chars: int = WINDOW_CHARS, overlap: float = 45.0) -> list[list[dict[str, Any]]]:
    """Consecutive transcript chunks the AI can read at once; each starts ~`overlap` s before the previous one ended."""
    out, i = [], 0
    while i < len(tr):
        j, size = i, 0
        while j < len(tr) and (j == i or size + len(tr[j]["text"]) + 16 <= max_chars):
            size += len(tr[j]["text"]) + 16
            j += 1
        out.append(tr[i:j])
        if j >= len(tr):
            break
        back = j
        while back - 1 > i + 1 and tr[j - 1]["end"] - tr[back - 1]["start"] < overlap:
            back -= 1
        i = max(i + 1, back)
    return out


def _ask(win: list[dict[str, Any]], k: int, lo: float, hi: float, *, name: str, duration: float, language: str,
         notes: str) -> list[Moment]:
    a, b = win[0]["start"], win[-1]["end"]
    lines = "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in win)
    prompt = (f"VIDEO: '{name}', {duration / 60:.1f} minutes long. This part of the transcript covers {a:.0f}–{b:.0f} s.\n"
              f"CREATOR'S NOTES: {notes or 'none'}\n\n"
              f"Find up to {k} moments, each {lo:.0f}–{hi:.0f} seconds long, that work as standalone Shorts/Reels:\n"
              "- start exactly where a transcript line begins with something that grabs attention in the first 3 "
              "seconds (a bold claim, a question, a surprising fact, a strong emotion, a joke's set-up) — never "
              "mid-sentence, never on filler like 'so', 'and', 'um';\n"
              "- end where a line ends, after the payoff (the answer, the punchline, the conclusion);\n"
              "- self-contained: someone who never saw the full video understands it;\n"
              "- prefer practical tips, strong opinions, stories, humour and emotional peaks; skip greetings, "
              "intros, sponsor reads and outros.\n"
              f"Write title, hook and reason in {language}. Score honestly — most moments are not 10s.\n\n"
              f"TRANSCRIPT ([start-end] seconds):\n{lines}")

    def check(r: Moments) -> None:
        bad = [f"{m.start:.1f}-{m.end:.1f}" for m in r.moments
               if m.start < a - 1 or m.end > b + 1 or not (lo * 0.6 <= m.end - m.start <= hi * 1.5)]
        if bad:
            raise ValueError(f"moments must lie inside {a:.1f}-{b:.1f} s and last {lo:.0f}-{hi:.0f} s; fix: "
                             + ", ".join(bad[:6]))
    res, _ = ai.generate("script", SYSTEM, prompt, Moments, max_tokens=3000, validate=check)
    return res.moments


# ------------------------------------------------------------------ snapping, scoring, picking

def snap(start: float, end: float, tr: list[dict[str, Any]], lo: float, hi: float,
         duration: float) -> tuple[float, float] | None:
    """Whole transcript lines only: begin where a line begins, end where one ends, then fit [lo, hi] by whole lines."""
    if not tr:
        s = max(0.0, min(start, duration - lo))
        return round(s, 2), round(min(duration, s + min(hi, max(lo, end - start))), 2)
    si = max((i for i, x in enumerate(tr) if x["start"] <= start + 0.75), default=0)
    ei = min((i for i, x in enumerate(tr) if x["end"] >= end - 0.75), default=len(tr) - 1)
    ei = max(ei, si)
    while tr[ei]["end"] - tr[si]["start"] < lo and ei + 1 < len(tr):
        ei += 1
    while tr[ei]["end"] - tr[si]["start"] > hi and ei > si:
        ei -= 1
    s, e = tr[si]["start"], tr[ei]["end"]
    if e - s > hi:                                 # one very long line: end on a word inside the limit
        words = [w for w in (tr[si].get("words") or []) if w["end"] <= s + hi]
        e = words[-1]["end"] if words else s + hi
    if e - s < lo * 0.75:
        return None                                # too short to stand alone (end of the video)
    return round(max(0.0, s - 0.12), 2), round(min(duration, e + 0.25), 2)


def score_of(sc: dict[str, float]) -> int:
    return int(round(10 * sum(WEIGHTS[k] * float(sc.get(k, 0)) for k in WEIGHTS)))


def _overlap(a: dict[str, Any], b: dict[str, Any]) -> float:
    inter = max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
    return inter / max(0.1, min(a["end"] - a["start"], b["end"] - b["start"]))


def pick(cands: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """Best first, no two clips sharing more than a quarter of their length."""
    kept: list[dict[str, Any]] = []
    for c in sorted(cands, key=lambda c: (-c["score"], c["start"])):
        if all(_overlap(c, k) < 0.25 for k in kept):
            kept.append(c)
        if len(kept) >= count:
            break
    return kept


HOOKISH = re.compile(r"[?!]|\b(why|how|what|secret|mistake|never|always|truth|best|worst|stop|imagine|biggest|nobody|"
                     r"most people|here's|the reason)\b|क्यों|कैसे|क्या|सच|गलती|कभी|सबसे|राज़|ज़रूर", re.I)
FILLER_START = re.compile(r"^(and|so|but|um+|uh+|also|then|like|okay|और|तो|लेकिन|फिर)\b", re.I)


def heuristic(tr: list[dict[str, Any]], duration: float, lo: float, hi: float,
              cuts: list[float] | None = None) -> list[dict[str, Any]]:
    """Rule-based candidates: clean, hook-like first lines with dense speech; evenly spaced windows when silent."""
    target = (lo + hi) / 2
    out = []
    if not tr:
        n = max(1, int(duration // target))
        for i in range(n):
            s = duration * i / n
            if cuts:                                    # start on a shot change near the even spot
                near = min(cuts, key=lambda c: abs(c - s))
                s = near if abs(near - s) < target / 2 else s
            s = max(0.0, min(s, duration - lo))
            out.append({"start": round(s, 2), "end": round(min(duration, s + target), 2), "score": 50,
                        "title": "", "hook": "", "reason": "evenly spaced moment (no speech found)", "keywords": [],
                        "picked_by": "rules"})
        return out
    for i, line in enumerate(tr):
        j = i
        while j + 1 < len(tr) and tr[j]["end"] - line["start"] < target:
            j += 1
        span = snap(line["start"], tr[j]["end"], tr, lo, hi, duration)
        if not span:
            continue
        text = " ".join(x["text"] for x in tr[i:j + 1])
        rate = len(text.split()) / max(1.0, span[1] - span[0])
        first = line["text"].strip()
        score = 35 + min(20, rate * 7) + (20 if HOOKISH.search(first) else 0) \
            + (10 if not FILLER_START.match(first) else 0) + (5 if re.search(r"[.!?।]$", tr[j]["text"].strip()) else 0)
        out.append({"start": span[0], "end": span[1], "score": int(min(score, 85)), "title": first[:70],
                    "hook": " ".join(first.split()[:7]), "reason": "strong opening line with dense speech",
                    "keywords": [], "picked_by": "rules"})
    return out


def find_moments(media: dict[str, Any], scenes: list[float], *, count: int, length: str, language: str,
                 notes: str = "") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The `count` best moments of one video, best first, each with title, hook, reason and score."""
    lo, hi = LENGTHS.get(length, LENGTHS["medium"])
    duration = float(media.get("duration") or 0)
    tr = media.get("transcript") or []
    meta: dict[str, Any] = {"windows": 0, "ai_windows": 0, "ai_errors": []}
    cands: list[dict[str, Any]] = []
    if tr:
        wins = windows(tr)
        meta["windows"] = len(wins)
        per = max(2, -(-count * 2 // len(wins)))       # ask for about twice what we need
        for win in wins:
            try:
                found = _ask(win, per, lo, hi, name=media["original_name"], duration=duration, language=language,
                             notes=notes)
                meta["ai_windows"] += 1
            except Exception as e:                      # this window falls back to the rules
                meta["ai_errors"].append(str(e)[:200])
                continue
            for m in found:
                span = snap(m.start, m.end, tr, lo, hi, duration)
                if span:
                    cands.append({"start": span[0], "end": span[1], "score": score_of(m.scores.model_dump()),
                                  "title": m.title.strip()[:90], "hook": m.hook.strip()[:60],
                                  "reason": m.reason.strip()[:240],
                                  "keywords": [k.strip() for k in m.keywords if k.strip()][:5], "picked_by": "ai"})
    chosen = pick(cands, count)
    if len(chosen) < count:                             # top up with rule-based moments that don't overlap
        chosen = pick(chosen + [dict(c, score=min(c["score"], min((k["score"] for k in chosen), default=100) - 1))
                                for c in heuristic(tr, duration, lo, hi, scenes)], count)
    meta["picked_by"] = "ai" if chosen and all(c["picked_by"] == "ai" for c in chosen) else \
        ("rules" if all(c["picked_by"] == "rules" for c in chosen) else "ai+rules")
    return chosen, meta


# ------------------------------------------------------------------ captions and hashtags per platform

class PlatformCopy(BaseModel):
    youtube_title: str = Field(description="YouTube Shorts title, max 90 characters, no hashtags")
    youtube_description: str = Field(description="2–3 sentences for YouTube, no hashtags")
    instagram_caption: str = Field(description="Instagram Reels caption: hook line, 1–2 short sentences, a question "
                                               "or call to action; no hashtags")
    facebook_caption: str = Field(description="Facebook Reels caption: 1–2 friendly sentences; no hashtags")
    hashtags: list[str] = Field(description="6–10 relevant hashtags without '#', most specific first")
    tags: list[str] = Field(default_factory=list, description="5–12 YouTube search tags")


class Copies(BaseModel):
    clips: list[PlatformCopy]


def clip_text(tr: list[dict[str, Any]], start: float, end: float, limit: int = 700) -> str:
    text = " ".join(s["text"] for s in tr if s["end"] > start and s["start"] < end)
    return text if len(text) <= limit else text[:limit] + " …"


def _hashtag(t: str) -> str:
    return re.sub(r"[^\w]", "", t.lstrip("#"), flags=re.UNICODE)


def _final(c: dict[str, Any], cp: PlatformCopy | None) -> dict[str, Any]:
    """Ready-to-paste text per platform within each platform's limits."""
    tags = [h for h in (_hashtag(t) for t in ((cp.hashtags if cp else []) + c.get("keywords", []))) if h]
    tags = list(dict.fromkeys(tags))
    title = ((cp.youtube_title if cp else "") or c.get("title") or c.get("hook") or "Short").strip()
    hook = c.get("hook") or title
    yt_desc = (cp.youtube_description if cp else "") or c.get("reason") or hook
    ig = (cp.instagram_caption if cp else "") or f"{hook}\n\n{c.get('reason') or ''}".strip()
    fb = (cp.facebook_caption if cp else "") or hook
    hs = lambda n: " ".join("#" + t for t in tags[:n])          # noqa: E731
    yt_tags, used = [], 0
    for t in [x.strip() for x in ((cp.tags if cp else []) or tags) if x.strip()]:
        if used + len(t) + 1 > 450:
            break
        yt_tags.append(t[:30])
        used += len(t) + 1
    return {
        "youtube": {"title": title[:100], "description": f"{yt_desc.strip()}\n\n{hs(YT_HASHTAGS)} #Shorts".strip()[:4900],
                    "tags": yt_tags},
        "instagram": {"caption": f"{ig.strip()}\n\n{hs(IG_HASHTAGS)}".strip()[:2200]},
        "facebook": {"caption": f"{fb.strip()}\n\n{hs(FB_HASHTAGS)}".strip()[:5000]},
        "hashtags": tags[:10],
    }


def platform_copy(clips: list[dict[str, Any]], tr: list[dict[str, Any]], *, language: str, name: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(0, len(clips), 5):
        batch = clips[i:i + 5]
        listing = "\n\n".join(f"CLIP {n + 1}: working title '{c['title']}', hook '{c['hook']}'\nSAYS: "
                              f"{clip_text(tr, c['start'], c['end'])}" for n, c in enumerate(batch))
        copies: list[PlatformCopy | None] = [None] * len(batch)
        try:
            def check(r: Copies, n: int = len(batch)) -> None:
                if len(r.clips) != n:
                    raise ValueError(f"return exactly {n} clips, in the same order")
            res, _ = ai.generate(
                "script", "You write short-form video captions that get views without misleading anyone. Output one "
                "JSON object matching the schema.",
                f"These vertical clips were cut from the video '{name}'. For each clip write the YouTube Shorts "
                f"title and description, the Instagram Reels caption, the Facebook Reels caption, hashtags and "
                f"YouTube tags — all in {language}. Honest, specific, no fake claims, no all-caps.\n\n{listing}",
                Copies, max_tokens=4000, validate=check)
            copies = list(res.clips)
        except Exception:                                 # the deterministic text below still works
            pass
        out += [_final(c, cp) for c, cp in zip(batch, copies)]
    return out


# ------------------------------------------------------------------ the plan the editing engine renders

def plan(media: dict[str, Any], scenes: list[float], opts: dict[str, Any], *, language: str,
         notes: str = "") -> tuple[EditPlan, dict[str, Any], list[dict[str, Any]]]:
    length = opts.get("length") if opts.get("length") in LENGTHS else "medium"
    duration = float(media.get("duration") or 0)
    count = int(opts.get("clips") or 0) or auto_count(duration, length)
    count = max(1, min(MAX_CLIPS, count))
    moments, meta = find_moments(media, scenes, count=count, length=length, language=language, notes=notes)
    if not moments:
        raise ValueError("the video is too short for the chosen clip length — pick a shorter length")
    tr = media.get("transcript") or []
    copies = platform_copy(moments, tr, language=language, name=media["original_name"])
    outputs, clips = [], []
    for n, (m, cp) in enumerate(zip(moments, copies), 1):
        outputs.append(Output(
            name=f"Short {n}", aspect="9:16", reframe="face" if opts.get("face_crop", True) else "fit_blur",
            segments=[Segment(media_id=media["id"], start=m["start"], end=m["end"], why=m["reason"][:200])],
            remove_silences=bool(opts.get("remove_pauses", True)), subtitles=bool(opts.get("captions", True)),
            highlight_words=m.get("keywords") or [], title=cp["youtube"]["title"],
            headline=(m["hook"] or m["title"])[:60] if opts.get("headline", True) else "",
            description=cp["youtube"]["description"], tags=cp["youtube"]["tags"]))
        clips.append({"rank": n, "start": m["start"], "end": m["end"], "seconds": round(m["end"] - m["start"], 1),
                      "score": m["score"], "hook": m["hook"], "reason": m["reason"], "picked_by": m["picked_by"],
                      "platforms": cp})
    summary = (f"{len(outputs)} vertical clip(s) of {LENGTH_LABELS[length]} from '{media['original_name']}' "
               f"({meta['picked_by']} picks)")
    return EditPlan(summary=summary, outputs=outputs, unsupported=[]), {**meta, "planner": "clipper",
                                                                        "length": length, "count": count}, clips


def details_text(title: str, clip: dict[str, Any], source: str) -> str:
    """The .txt that ships next to each clip: everything needed to post it by hand."""
    p = clip["platforms"]
    mm = lambda t: f"{int(t // 60)}:{int(t % 60):02d}"          # noqa: E731
    return "\n".join([
        f"{title}", "=" * min(60, max(10, len(title))), "",
        f"From: {source}  ({mm(clip['start'])}–{mm(clip['end'])}, {clip['seconds']:.0f} s)",
        f"Score: {clip['score']}/100 — {clip['reason']}", "",
        "YOUTUBE SHORTS", "--------------", f"Title: {p['youtube']['title']}", "", "Description:",
        p["youtube"]["description"], "", f"Tags: {', '.join(p['youtube']['tags'])}", "",
        "INSTAGRAM REELS", "---------------", p["instagram"]["caption"], "",
        "FACEBOOK REELS", "--------------", p["facebook"]["caption"], "",
        "Format: 1080×1920 (9:16) MP4, H.264 + AAC — fits YouTube Shorts, Instagram Reels and Facebook Reels.", ""])
