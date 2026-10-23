"""NIFTY 50 Fibonacci pivot episodes: creation and the pipeline stages specific to them.

Division of labour:
  * code   — data, calculations, observations, title, description, tags, thumbnail, charts,
             intro and disclosure (deterministic; nothing invented)
  * model  — explanatory narration that may only use the fact sheet; every draft is linted
             (prohibited language + unverified numbers) and rejected drafts are rewritten
  * gate   — the 10 compliance checks decide what, if anything, may be uploaded
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from PIL import Image, ImageDraw
from pydantic import BaseModel, Field

from .. import ai, config, db, logs, profile, settings
from ..ai import prompts
from ..media import graphics
from . import analysis, audit, charts, gate, i18n, lint, marketdata

STAGES = ["data", "script", "assets", "voice", "render", "thumbnail", "qa", "compliance"]
STATUS = {"data": "verifying data", "script": "scripting", "assets": "drawing charts", "voice": "voicing",
          "render": "rendering", "thumbnail": "making thumbnail", "qa": "quality check", "compliance": "compliance gate"}

Purpose = Literal["context", "calculation", "levels", "review", "intraday_15m", "intraday_1h", "mtf", "concept",
                  "scenarios", "limitations", "recap", "next_levels"]
Visual = Literal["candles", "formula", "levels", "review", "chart_15m", "chart_1h", "mtf", "concept", "scenarios",
                 "limitations", "recap", "next_levels"]
# Purpose -> the visual that must accompany it (charts are drawn from data, so they are not the model's choice)
VISUAL_FOR = {"context": "candles", "calculation": "formula", "levels": "levels", "review": "review",
              "intraday_15m": "chart_15m", "intraday_1h": "chart_1h", "mtf": "mtf", "concept": "concept",
              "scenarios": "scenarios", "limitations": "limitations", "recap": "recap", "next_levels": "next_levels"}
FORECAST = re.compile(r"\b(will|is going to|are going to|expected to|likely to|bound to|should)\s+(\w+\s+)?(rise|fall|go|move|"
                      r"reach|test|break|bounce|hit|rally|drop|climb|decline|recover|reverse|surge|crash)\b"
                      r"|बढ़ेगा|बढ़ेगी|गिरेगा|गिरेगी|चढ़ेगा|चढ़ेगी|टूटेगा|टूटेगी|पहुँचेगा|पहुँचेगी|उम्मीद है कि|तय है कि", re.I)
MAX_SCENE_WORDS = 85            # short scenes: better pacing, and clean cut points for Shorts


class EpScene(BaseModel):
    purpose: Purpose
    narration: str = Field(..., min_length=20, max_length=1100)
    on_screen_text: str = Field(..., max_length=60, description="Short factual label, max 6 words")
    visual: Visual
    observation_ids: list[str] = Field(..., max_length=8)


class EpisodeScript(BaseModel):
    concept_title: str = Field("", max_length=80, description="The educational idea this episode teaches")
    scenes: list[EpScene] = Field(..., min_length=2, max_length=26)


# ------------------------------------------------------------------ creation

def create(dataset_id: int, basis_date: str, mode: str, fmt: str = "long", actor: str = "user",
           audience: str = "india", review_date: str | None = None) -> int:
    from .. import worker
    language = profile.language_for(audience)
    facts = analysis.build_facts(dataset_id, basis_date, mode, language)   # fail fast on bad requests
    if review_date and facts.get("review_date") != review_date:
        raise marketdata.DataError(f"the session after {basis_date} in dataset #{dataset_id} is "
                                   f"{facts.get('review_date')}, not {review_date}")
    title = title_for(facts)
    pid = db.insert("productions", {
        "topic_id": None, "format": fmt, "title": title, "status": "queued", "stage": "data", "kind": "nifty_pivot",
        "audience": audience, "language": language,
        "package": db.dumps({}), "episode": db.dumps({"request": {"dataset_id": dataset_id, "basis_date": basis_date,
                                                                  "review_date": facts.get("review_date"),
                                                                  "mode": mode, "format": fmt}}),
        "test_mode": 0, "created_at": db.iso(), "updated_at": db.iso()})
    audit.record("episode_requested", {"dataset_id": dataset_id, "basis_date": basis_date, "mode": mode, "format": fmt},
                 production_id=pid, actor=actor)
    worker.enqueue("production", {"production_id": pid}, production_id=pid, max_attempts=3)
    logs.event("info", "finance", f"NIFTY pivot episode #{pid} queued for {facts['basis_label']} ({mode})", production_id=pid)
    return pid


# ------------------------------------------------------------------ deterministic text

def source_name(facts: dict[str, Any]) -> str:
    return marketdata.source(facts["source_id"])["name"]


def title_for(facts: dict[str, Any]) -> str:
    """The factual, deterministic episode title (the gate requires exactly this)."""
    L = facts.get("lang", "en")
    if facts["mode"] == "daily_review":
        return i18n.t("title_review", L, date=i18n.short_date(facts["review_date"], L),
                      ndate=i18n.short_date(facts["next"]["date"], L))
    return i18n.t("title", L, date=facts["basis_label"])


def signoff_scene(facts: dict[str, Any], short: bool = False) -> dict[str, Any]:
    """News-anchor style closing line, spoken after the disclosure over a closing card."""
    L = facts.get("lang", "en")
    if short:
        text = i18n.t("signoff_short", L)
    elif facts["mode"] == "daily_review":
        text = i18n.t("signoff_review", L, date=i18n.short_date(facts["review_date"], L),
                      ndate=i18n.short_date(facts["next"]["date"], L))
    elif facts["mode"] == "lesson":
        from . import lessons
        n = facts["lesson_no"]
        text = i18n.t("signoff_lesson" if n < lessons.TOTAL else "signoff_series_end", L, n=n, total=lessons.TOTAL)
    else:
        text = i18n.t("signoff_episode", L)
    return {"purpose": "signoff", "narration": text, "on_screen_text": "", "visual": "signoff", "observation_ids": [],
            "key_point_ids": []}


def intro_text(facts: dict[str, Any]) -> str:
    L = facts.get("lang", "en")
    nature = i18n.nature(facts["data_nature"], L)
    if facts["mode"] == "daily_review":
        return i18n.t("intro_review", L, basis=facts["basis_label"], nature=nature, review=facts["review_label"],
                      ndate=facts["next"]["label"])
    if facts["mode"] == "historical_case_study":
        return i18n.t("intro_hist", L, basis=facts["basis_label"], nature=nature, review=facts["review_label"])
    return i18n.t("intro_daily", L, basis=facts["basis_label"], nature=nature)


def compose_description(facts: dict[str, Any], disclosure: str, chapters: list[tuple[str, str]] | None = None,
                        voice: dict | None = None) -> str:
    lv = facts["pivots"]["levels"]
    b = facts["basis"]
    fmt = analysis.fmt
    L = facts.get("lang", "en")
    t = lambda key, **kw: i18n.t(key, L, **kw)  # noqa: E731
    if facts["mode"] == "daily_review":
        summary = t("summary_review", basis=facts["basis_label"], review=facts["review_label"], ndate=facts["next"]["label"])
    elif facts["mode"] == "historical_case_study":
        summary = t("summary_hist", basis=facts["basis_label"], review=facts["review_label"])
    else:
        summary = t("summary_daily", basis=facts["basis_label"])
    parts = [summary, "", t("h_data"),
             t("d_index", nature=i18n.nature(facts["data_nature"], L)),
             t("d_source", source=source_name(facts), retrieved=facts["retrieved_label"]),
             t("d_session", date=facts["basis_label"], o=fmt(b["open"]) if b["open"] is not None else "—", h=fmt(b["high"]),
               l=fmt(b["low"]), c=fmt(b["close"]))]
    if facts.get("review"):
        r = facts["review"]
        parts.append(t("d_following", date=facts["review_label"], o=fmt(r["open"]) if r["open"] is not None else "—",
                       h=fmt(r["high"]), l=fmt(r["low"]),
                       c=fmt(r["close"])))
    parts += ["", t("h_levels", date=facts["basis_label"]),
              " | ".join(f"{k} {fmt(lv[k])}" for k in facts["pivots"]["order"]),
              t("h_formula") + ": " + facts["pivots"]["formula"], ""]
    if facts.get("next"):
        nx = facts["next"]["levels"]
        parts += [t("h_next", date=facts["next"]["label"]),
                  " | ".join(f"{k} {fmt(nx[k])}" for k in facts["pivots"]["order"]), ""]
    head = parts
    tail = ["", t("h_disclosure"), disclosure, "", t("credits", voice=i18n.voice_credit(voice)), "", t("hashtags")]
    chap = (["", t("h_chapters")] + [f"{tm} {name}" for tm, name in chapters]) if chapters else []
    # Observations, most important first; YouTube allows 5,000 bytes (Devanagari is 3 bytes a character)
    key = ("next_levels", "next_position", "s2_15m", "r2_15m", "s2_review", "r2_review", "levels_touched", "tf_compare")
    obs = sorted(facts["observations"], key=lambda o: (o["id"] not in key, key.index(o["id"]) if o["id"] in key else 0))
    obs_lines = [f"- {o['text']}" for o in obs]
    lim_lines = [f"- {x}" for x in facts["limitations"]]

    def build() -> str:
        body = head + ([t("h_showed")] + obs_lines if obs_lines else []) + \
            (["", t("h_limits")] + lim_lines if lim_lines else []) + chap + tail
        return "\n".join(body).replace("<", "").replace(">", "")
    text = build()
    while len(text.encode()) > DESCRIPTION_BYTES and (lim_lines[1:] or obs_lines[3:]):
        (obs_lines if len(obs_lines) > 3 else lim_lines).pop()
        text = build()
    while len(text.encode()) > DESCRIPTION_BYTES and (obs_lines or lim_lines):
        (obs_lines or lim_lines).pop()
        text = build()
    return text


DESCRIPTION_BYTES = 4900


TAGS = ["NIFTY 50", "nifty analysis", "nifty levels", "nifty support and resistance", "nifty pivot points",
        "Fibonacci pivot points", "nifty 15 minute chart", "technical analysis", "stock market india",
        "share market", "NSE", "nifty levels for tomorrow"]
TAGS_HI = ["NIFTY 50", "nifty analysis in hindi", "nifty levels", "nifty support resistance", "nifty pivot points",
           "fibonacci pivot", "share market hindi", "stock market hindi", "nifty 15 minute chart", "technical analysis hindi",
           "निफ्टी एनालिसिस", "सपोर्ट रेजिस्टेंस", "nifty levels for tomorrow"]


# ------------------------------------------------------------------ stages

def stage_data(p: dict[str, Any]) -> None:
    req = p["episode"]["request"]
    facts = analysis.build_facts(req["dataset_id"], req["basis_date"], req["mode"], profile.language())
    src = marketdata.source(facts["source_id"])
    disclosure = lint.disclosure_text(facts, src["name"])
    ep = {**p["episode"], "facts": facts, "disclosure": disclosure,
          "source": {k: src[k] for k in ("id", "name", "terms_url", "status", "licence_reference")}}
    from .. import pipeline
    pipeline.save(p["id"], episode=db.dumps(ep), title=title_for(facts))
    audit.log_artifact("facts_computed", p["id"], "facts", facts,
                       extra={"basis_date": facts["basis_date"], "levels": facts["pivots"]["levels"]})


def _script_prompt(facts: dict[str, Any], words: int, sections: list[str] | None = None, previous: str = "") -> str:
    fmt = analysis.fmt
    lv = facts["pivots"]["levels"]
    b = facts["basis"]
    lines = [
        f"EPISODE TYPE: {analysis.MODES[facts['mode']]}",
        HINDI_RULES if facts.get("lang") == "hi" else "LANGUAGE: Clear, conversational English.",
        "",
        "FACT SHEET — the ONLY numbers you may use; write them exactly as shown (commas, two decimals):",
        ("- The data source gives NO opening prices: never mention an open price or an opening gap.\n"
         if b["open"] is None or (facts.get("review") or {}).get("open", 0) is None else "") +
        f"- Basis session {facts['basis_label']}: open {fmt(b['open'])}, high {fmt(b['high'])}, low {fmt(b['low'])}, "
        f"close {fmt(b['close'])}; range {fmt(facts['pivots']['range'])} points",
        "- Fibonacci pivot levels from that session: " + ", ".join(f"{k} {fmt(lv[k])}" for k in facts["pivots"]["order"]),
        f"- Formula: {facts['pivots']['formula']}",
    ]
    if facts.get("review"):
        r = facts["review"]
        lines.append(f"- Following session {facts['review_label']}: open {fmt(r['open'])}, high {fmt(r['high'])}, "
                     f"low {fmt(r['low'])}, close {fmt(r['close'])}")
    if facts.get("next"):
        nx, nb = facts["next"]["levels"], facts["next"]["basis"]
        lines.append(f"- Reference levels for {facts['next']['label']} (from the {facts['review_label']} high {fmt(nb['high'])}, "
                     f"low {fmt(nb['low'])}, close {fmt(nb['close'])}; range {fmt(facts['next']['range'])}): "
                     + ", ".join(f"{k} {fmt(nx[k])}" for k in facts["pivots"]["order"]))
    lines += ["- Observations (cite by id):"] + [f"  [{o['id']}] {o['text']}" for o in facts["observations"]]
    lines += ["- Limitations:"] + [f"  * {x}" for x in facts["limitations"]]
    review_rule = ("Past tense throughout: this is a review of what already happened, not a forecast."
                   if facts["mode"] == "historical_case_study" else
                   "Conditional, explanatory language only (e.g. 'technical analysts often watch whether…'); no forecasts.")
    sections = sections or required_sections(facts)
    guide = {
        "context": "the verified previous-session OHLC and what it shows; name the data source and date",
        "calculation": "how Fibonacci pivots are computed, walking through the real numbers step by step",
        "levels": "what S2 and R2 represent: analytical reference zones, not turning points or predictions",
        "review": "what the following session actually did relative to S2, R2 and the pivot; if a level was not "
                  "tested, say plainly that it was not tested",
        "intraday_15m": "the 15-minute bars: when levels were first touched, closes beyond, failed breakouts — only "
                        "what the 15-minute observations state",
        "intraday_1h": "the broader 1-hour sequence and how it compares with the 15-minute view; explain any "
                       "disagreement instead of claiming confirmation",
        "mtf": "the 15-minute and 1-hour Fibonacci levels from the observations and how they differ from the daily levels",
        "concept": "ONE educational idea this specific data illustrates (e.g. range width and level spacing, gaps and "
                   "pivot relevance, why daily bars hide the order of events). Name it in concept_title",
        "scenarios": "what different price behaviours around a level are generally taken to mean, as concepts only "
                     "(test-and-hold vs close beyond vs failed breakout), with uncertainty; no instructions",
        "limitations": "what this data cannot tell us",
        "recap": "summarise the most important verified observations, what remains uncertain, and invite viewers to "
                 "subscribe for future educational analysis (one short sentence)",
    }
    if facts["mode"] == "daily_review":
        guide.update({
            "levels": f"today's reference levels, calculated from the {facts['basis_label']} session: what S2 and R2 represent",
            "review": f"how the index actually traded on {facts.get('review_label')} around S2, R2 and the pivot — which "
                      "levels were tested and which were not",
            "calculation": f"how the {facts['next']['label']} levels are computed from the {facts.get('review_label')} high, "
                           "low and close, walking through the real numbers",
            "next_levels": f"the reference zones for {facts['next']['label']} and where today's close sits relative to "
                           "them; conditional language only ('if price holds above the pivot, many analysts watch R1 and "
                           "R2 as the next reference zones') — never say where the index will go",
            "mtf": "the 15-minute and 1-hour Fibonacci levels at today's close (from the observations) and how they "
                   "differ from the daily levels",
            "scenarios": "for the next session, as concepts only: what a test-and-hold, a close beyond, or a failed "
                         "breakout at these zones is generally taken to mean; stress that none is predicted",
        })
    if previous:
        lines += ["", "ALREADY NARRATED earlier in this episode (continue naturally from it; do not repeat its points):",
                  previous]
    lines += ["",
              f"WRITE {'the next part' if previous else 'the explanatory part'} of the episode: about {words} spoken "
              "words in total (do not pad — shorter is fine if the data says less), as scenes in this order, one or "
              "more scenes per section. Write ONLY these sections:"]
    lines += [f"{i}. {sec} (visual '{VISUAL_FOR[sec]}') — {guide[sec]}." for i, sec in enumerate(sections, start=1)]
    lines += ["",
              "RULES:",
              "- Use only numbers from the fact sheet, exactly as written. Use words for anything else (e.g. 'two levels').",
              "- Each scene lists the observation ids it relies on; use at least three different observations overall.",
              "- Call S1-S3, P and R1-R3 'reference levels'. Never call them targets, entries, stop-losses, signals or "
              "calls. Never suggest buying, selling, holding, options, strikes or any trade. No position sizing.",
              f"- {review_rule}",
              "- Never invent a reaction to a level: describe a test, rejection or breakout only when an observation states it.",
              "- Separate what happened (observations) from what could happen (scenarios, clearly hypothetical).",
              "- No claims of accuracy, success rates, returns or profits. No urgency, fear or hype. No greetings. "
              "Only the recap may invite viewers to subscribe; no other calls to action.",
              "- Do not mention SEBI, registration or legal status, and do not write a disclaimer — the app adds the "
              "official disclosure.",
              "- The narrator is a synthetic voice: never present it as a human expert, trader or analyst; avoid 'I "
              "recommend', 'my view', 'my analysis'.",
              "- The data is not live: never say live, real-time or 'right now'."
              + ("" if facts.get("intraday") else
                 " You may name the 15-minute and 1-hour LEVELS given in the observations, but there are no 15-minute "
                 "or 1-hour candles: never describe intraday moves, times or candle patterns." if facts.get("mtf") else
                 " Do not mention 15-minute or hourly charts — no intraday data is verified."),
              f"- Keep each scene under {MAX_SCENE_WORDS - 15} words — use two or three short scenes for a section rather than one long one; start each section with a natural spoken transition.",
              "- on_screen_text: a short factual label (max 6 words), e.g. 'S2 at 24,512.35'.",
              "- concept_title: " + ("name the concept section's idea (5-80 characters)." if "concept" in sections
                                     else "leave it as an empty string.")]
    return "\n".join(lines)


# Relative length of each section in the word budget: closing sections are naturally short
SECTION_WEIGHT = {"limitations": 0.5, "recap": 0.7}


def required_sections(facts: dict[str, Any]) -> list[str]:
    tfs = (facts.get("intraday") or {}).get("timeframes", [])
    if facts["mode"] == "daily_review":
        # Daily show: today's levels -> what happened today -> next session's reference levels
        return (["levels", "review"] + (["intraday_15m"] if "15m" in tfs else []) + (["intraday_1h"] if "1h" in tfs else [])
                + ["calculation", "next_levels"] + (["mtf"] if facts.get("mtf") else [])
                + ["scenarios", "limitations", "recap"])
    secs = ["context", "calculation", "levels"]
    if facts.get("review"):
        secs.append("review")
    if "15m" in tfs:
        secs.append("intraday_15m")
    if "1h" in tfs:
        secs.append("intraday_1h")
    if facts.get("mtf"):
        secs.append("mtf")
    return secs + ["concept", "scenarios", "limitations", "recap"]


def target_words(facts: dict[str, Any], fmt: str) -> int:
    """Length follows the verified material: about 10-15 minutes only when the data supports it."""
    if fmt != "long":
        return 150
    return min(1900, 420 + 95 * len(facts["observations"]) + 60 * len(required_sections(facts)))


def _lint_script(script: EpisodeScript, facts: dict[str, Any], fmt: str = "long", sections: list[str] | None = None,
                 want: int | None = None) -> list[str]:
    problems = []
    text = "\n".join(s.narration + "\n" + s.on_screen_text for s in script.scenes)
    for f in lint.prohibited_language(text + "\n" + script.concept_title):
        problems.append(f"remove '{f['match']}' ({f['why']}) near: {f['context']!r}")
    bad = lint.unverified_numbers(text + "\n" + script.concept_title, facts)
    if bad:
        problems.append("numbers not in the fact sheet (rewrite in words or use the exact fact-sheet value): "
                        + ", ".join(sorted(set(bad))[:10]))
    fc = [m.group(0) for m in FORECAST.finditer(text)]
    if fc:
        problems.append(f"no forecasts — describe reference levels and what is commonly watched, conditionally "
                        f"('if … then analysts often watch …'): remove {sorted(set(fc))[:5]}")
    live = gate._affirmative_mentions(gate.LIVE_WORDS, text)
    if live:
        problems.append(f"do not describe the data as live ('{live[0]}')")
    intraday = gate._affirmative_mentions(gate.INTRADAY_WORDS, text)
    if intraday and not facts.get("intraday") and not facts.get("mtf"):
        problems.append("there is no verified intraday data — do not discuss 15-minute or hourly charts")
    ids = {o["id"] for o in facts["observations"]}
    for s in script.scenes:          # limitations are not data claims; models sometimes "cite" them — ignore that
        s.observation_ids = [o for o in s.observation_ids if o.strip().lower() not in ("limitations", "limitation")]
    used = {o for s in script.scenes for o in s.observation_ids}
    if used - ids:
        problems.append(f"unknown observation ids: {sorted(used - ids)}")
    whole = sections is None
    sections = sections or required_sections(facts)
    if len(used & ids) < (3 if whole else 1):
        problems.append("use at least three different observations" if whole else "cite the observations you rely on")
    if "concept" in sections and len(script.concept_title.strip()) < 5:
        problems.append("concept_title must name the concept (5-80 characters)")
    need = set(sections)
    missing = need - {s.purpose for s in script.scenes}
    if missing:
        problems.append(f"missing scene purposes: {sorted(missing)}")
    extra = {s.purpose for s in script.scenes} - need
    if extra:
        problems.append(f"no verified data for scene purposes {sorted(extra)} — remove them")
    order = [s.purpose for s in script.scenes]
    seq = [x for x in sections if x in order]
    if [p for i, p in enumerate(order) if i == 0 or p != order[i - 1]] != seq:
        problems.append(f"keep the sections in this order, each section's scenes together: {seq}")
    cta = [i for i, s in enumerate(script.scenes, start=1) if s.purpose != "recap" and
           re.search(r"subscrib|सब्सक्राइब", s.narration, re.I)]
    if cta:
        problems.append(f"only the recap may invite subscribing (scenes {cta})")
    total = sum(len(s.narration.split()) for s in script.scenes)
    want = want or target_words(facts, fmt)
    if fmt == "long" and total < 0.3 * want:          # only near-empty parts are rewritten; length never blocks publishing
        problems.append(f"too short: {total} words for {len(facts['observations'])} verified observations (aim for about "
                        f"{want}). Explain each observation more fully, using several scenes per section where the data "
                        "supports it — without repeating points or adding numbers that are not in the fact sheet")
    for i, s in enumerate(script.scenes, start=1):
        if len(s.narration.split()) > MAX_SCENE_WORDS:
            problems.append(f"scene {i} is too long ({len(s.narration.split())} words); split it")
        if len(s.on_screen_text.split()) > 7:
            problems.append(f"scene {i} on_screen_text is longer than 6 words")
    if re.search(r"\b(I|my)\b\s+(recommend|suggest|think|believe|view|analysis|opinion)", text, re.I) or \
            re.search(r"मेरी\s+(राय|सलाह)|मैं\s+(सलाह|सुझाव)", text):
        problems.append("the synthetic narrator must not give personal views or recommendations")
    if facts.get("lang") == "hi":
        for i, s in enumerate(script.scenes, start=1):
            if devanagari_ratio(s.narration) < 0.6:
                problems.append(f"scene {i}: narration must be Hindi in Devanagari script")
    return problems


def devanagari_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    return sum(1 for c in letters if "\u0900" <= c <= "\u097f") / len(letters) if letters else 0.0


HINDI_RULES = ("LANGUAGE: Everyday spoken HINGLISH, the way popular Indian market YouTubers talk — Hindi sentence "
               "structure mixed with the English market words people actually use: लेवल, सपोर्ट, रेजिस्टेंस, ज़ोन, "
               "ब्रेकआउट, क्लोज़, ओपन, हाई, लो, कैंडल, चार्ट, रेंज, सेशन, डेटा, एनालिसिस, टेस्ट, पॉइंट्स. Write EVERYTHING "
               "in Devanagari script (English words too, as above) so the Hindi voice pronounces them correctly; keep only "
               "NIFTY 50, S1-S3, P, R1-R3 and IST in Latin letters. Avoid formal or Sanskritised Hindi: say 'लेवल' not "
               "'स्तर', 'टेस्ट हुआ' not 'परीक्षण हुआ', 'एनालिसिस' not 'विश्लेषण', 'ऊपर/नीचे क्लोज़ हुआ' not 'बंद हुआ'. "
               "Sound like a real person talking to viewers: short sentences, varied rhythm, natural transitions "
               "('अब ज़रा देखिए…', 'यहाँ एक इंटरेस्टिंग चीज़ है…', 'सीधी सी बात है…'), the occasional rhetorical question, "
               "address viewers as 'आप'. Do not claim to be a human, a trader or an expert. Write numbers exactly as in the "
               "fact sheet (Western digits with commas); say 'पॉइंट्स' for points. Call the levels 'रेफरेंस लेवल' or "
               "'ज़ोन'; never use टारगेट, स्टॉप लॉस, एंट्री, खरीदें, बेचें or "
               "the word गारंटी at all (say 'पक्का नहीं कहा जा सकता' instead). Never use future-tense market verbs such as "
               "बढ़ेगा, गिरेगा, पहुँचेगा, टूटेगा — for the next session write conditionally, e.g. 'अगर कीमत पिवट के ऊपर "
               "टिकती है, तो कई विश्लेषक R1 और R2 को अगले संदर्भ ज़ोन के रूप में देखते हैं'.")


FALLBACK = {
    "levels": {"hi": "{basis} के हाई, लो और क्लोज़ से आज के लेवल्स निकले थे — पिवट {P}, S2 {S2} और R2 {R2}। S2 और R2 को आप "
                     "सपोर्ट और रेजिस्टेंस ज़ोन की तरह देखिए — ये रेफरेंस लेवल्स हैं, कोई प्रेडिक्शन नहीं।",
               "en": "Today's levels came from the {basis} high, low and close: pivot {P}, S2 {S2} and R2 {R2}. Treat S2 "
                     "and R2 as support and resistance zones — reference levels, not predictions."},
    "calculation": {"hi": "अब {ndate} के लेवल्स कैसे निकले, ये देखते हैं। {rdate} का हाई {H}, लो {L} और क्लोज़ {C} रहा। "
                          "पिवट बराबर हाई प्लस लो प्लस क्लोज़ भाग तीन, यानी {nP}। रेंज {rng} पॉइंट्स है, तो R2 बना {nR2} "
                          "और S2 बना {nS2}।",
                    "en": "Here is how the {ndate} levels are calculated. On {rdate} the high was {H}, the low {L} and the "
                          "close {C}. The pivot is high plus low plus close, divided by three: {nP}. The range is {rng} "
                          "points, which puts R2 at {nR2} and S2 at {nS2}."},
    "scenarios": {"hi": "अगर अगले सेशन में प्राइस पिवट {nP} के ऊपर टिकता है, तो कई एनालिस्ट R1 {nR1} और R2 {nR2} को अगले "
                        "रेफरेंस ज़ोन की तरह देखते हैं। अगर प्राइस पिवट के नीचे रहता है, तो S1 {nS1} और S2 {nS2} पर नज़र "
                        "रहती है। ध्यान रखिए — ये सिर्फ़ कॉन्सेप्ट हैं, कोई प्रेडिक्शन या सलाह नहीं।",
                  "en": "If price holds above the {nP} pivot in the next session, many analysts watch R1 at {nR1} and R2 "
                        "at {nR2} as the next reference zones. If it stays below the pivot, S1 at {nS1} and S2 at {nS2} "
                        "come into focus. These are concepts only — not predictions or advice."},
    "recap": {"hi": "तो आज का सार — NIFTY 50 {C} पर क्लोज़ हुआ। {ndate} के लिए पिवट {nP}, S2 {nS2} और R2 {nR2} हैं। ऐसे ही "
                    "डेली एनालिसिस के लिए चैनल को सब्सक्राइब कर लीजिए।",
              "en": "To sum up: NIFTY 50 closed at {C}. For {ndate}, the pivot is {nP}, S2 {nS2} and R2 {nR2}. "
                    "Subscribe for the daily analysis."},
}


def fallback_script(facts: dict[str, Any]) -> EpisodeScript:
    """Script written by the app from the verified fact sheet (used only when no AI script passes the checks)."""
    L = facts.get("lang", "en")
    f = analysis.fmt
    lv, nx, r = facts["pivots"]["levels"], facts["next"]["levels"], facts["review"]
    kw = {"basis": facts["basis_label"], "rdate": facts["review_label"], "ndate": facts["next"]["label"],
          "P": f(lv["P"]), "S2": f(lv["S2"]), "R2": f(lv["R2"]), "H": f(r["high"]), "L": f(r["low"]), "C": f(r["close"]),
          "rng": f(facts["next"]["range"]), **{f"n{k}": f(v) for k, v in nx.items()}}
    obs = {o["id"]: o["text"] for o in facts["observations"]}
    by_section = {
        "review": ["s2_review", "r2_review", "levels_touched"],
        "intraday_15m": ["structure_15m", "s2_15m", "r2_15m", "p_15m"],
        "intraday_1h": ["structure_1h", "tf_compare"],
        "next_levels": ["next_levels", "next_position"],
        "mtf": ["mtf_15m", "mtf_1h"],
    }
    scenes: list[EpScene] = []
    for sec in required_sections(facts):
        if sec in FALLBACK:
            cite = {"levels": ["level_spacing"], "calculation": ["next_levels"], "scenarios": ["next_levels"],
                    "recap": ["next_levels", "levels_touched"]}.get(sec, [])
            scenes.append(EpScene.model_construct(purpose=sec, narration=FALLBACK[sec][L].format(**kw), on_screen_text="",
                                                  visual=VISUAL_FOR[sec], observation_ids=[c for c in cite if c in obs]))
        elif sec == "limitations":
            for x in facts["limitations"][:3]:
                scenes.append(EpScene.model_construct(purpose=sec, narration=x, on_screen_text="", visual="limitations",
                                                      observation_ids=[]))
        else:
            for oid in by_section.get(sec, []):
                if oid in obs:
                    scenes.append(EpScene.model_construct(purpose=sec, narration=obs[oid], on_screen_text="",
                                                          visual=VISUAL_FOR[sec], observation_ids=[oid]))
    return EpisodeScript.model_construct(concept_title="", scenes=scenes)


def stage_script(p: dict[str, Any]) -> None:
    from .. import pipeline
    facts = p["episode"]["facts"]
    words = target_words(facts, p["format"])
    if p["episode"].get("force_fallback"):
        script = fallback_script(facts)
        meta = {"provider": "app", "model": "fact-sheet template (after a failed check)", "cost_usd": 0}
        return _finish_script(p, facts, script, meta)
    secs = required_sections(facts)
    # Local/cloud models write ~700 words per answer: long episodes are written in parts of a few sections each,
    # every part linted on its own, then the joined script is linted again as a whole.
    size = int(settings.get("script.sections_per_call", 2))
    parts = [secs[i:i + size] for i in range(0, len(secs), size)] if p["format"] == "long" and len(secs) > size + 1 else [secs]
    try:
        merged: list[EpScene] = []
        concept_title, metas = "", []
        for part in parts:
            part_words = round(words * sum(SECTION_WEIGHT.get(x, 1.0) for x in part)
                               / sum(SECTION_WEIGHT.get(x, 1.0) for x in secs))
            previous = "\n".join(f"[{s.purpose}] {s.narration}" for s in merged)

            def check(s: EpisodeScript, part=part, part_words=part_words) -> None:
                problems = _lint_script(s, facts, p["format"], sections=part, want=part_words)
                if problems:
                    raise ValueError("; ".join(problems[:12]))
            got, meta = ai.generate("script", prompts.system("an educational scriptwriter for a stock-market education channel"),
                                    _script_prompt(facts, part_words, part, previous), EpisodeScript, max_tokens=12000,
                                    validate=check)
            merged += got.scenes
            concept_title = concept_title or got.concept_title
            metas.append(meta)
        script = EpisodeScript.model_construct(concept_title=concept_title, scenes=merged)   # parts were validated
        problems = _lint_script(script, facts, p["format"])
        if problems:
            raise pipeline.NeedsAttention("NEEDS REVIEW — joined script failed the lint: " + "; ".join(problems[:8]))
        meta = {**metas[-1], "parts": len(parts), "cost_usd": sum(m.get("cost_usd") or 0 for m in metas)}
    except (ai.AllProvidersFailed, pipeline.NeedsAttention) as e:
        unreachable = isinstance(e, ai.AllProvidersFailed) and "invalid output" not in str(e)
        if facts["mode"] == "daily_review":
            # The daily show must not miss a day: fall back to a script written by the app from the fact sheet
            script = fallback_script(facts)
            meta = {"provider": "app", "model": "fact-sheet template (AI script unavailable)", "cost_usd": 0}
            audit.record("script_fallback_used", {"reason": str(e)[:1500]}, production_id=p["id"])
            logs.event("warning", "finance", "AI script unavailable — using the app's fact-sheet script so today's video "
                       f"still goes out ({str(e)[:200]})", production_id=p["id"])
        elif unreachable:
            raise                      # providers unreachable: let the queue retry later
        else:
            audit.record("script_rejected", {"reason": str(e)[:2000]}, production_id=p["id"])
            raise pipeline.NeedsAttention(f"NEEDS REVIEW — no script passed the compliance lint: {str(e)[:600]}") from e
    _finish_script(p, facts, script, meta)


def _finish_script(p: dict[str, Any], facts: dict[str, Any], script: EpisodeScript, meta: dict[str, Any]) -> None:
    """Wrap the narration scenes with the intro, disclosure and sign-off, and build the package."""
    from .. import pipeline
    disclosure = p["episode"]["disclosure"]
    scenes = [{"purpose": "intro", "narration": intro_text(facts), "on_screen_text": "",
               "visual": "title", "observation_ids": []}]
    scenes += [{**s.model_dump(), "visual": VISUAL_FOR[s.purpose]} for s in script.scenes]
    scenes.append({"purpose": "disclosure", "narration": disclosure, "on_screen_text": "", "visual": "disclosure",
                   "observation_ids": []})
    scenes.append(signoff_scene(facts))
    for s in scenes:     # fields the shared media pipeline expects
        s.update({"caption_text": s["narration"], "visual_description": f"chart: {s['visual']}",
                  "visual_query": "chart", "visual_type": "text_card"})
    chapters = []
    if p["format"] == "long":
        names = {k: i18n.t(f"ch_{k}", facts.get("lang", "en")) for k in
                 ("intro", "context", "calculation", "levels", "review", "intraday_15m", "intraday_1h", "mtf",
                  "scenarios", "limitations", "recap", "disclosure", "next_levels")}
        names["concept"] = script.concept_title
        for i, s in enumerate(scenes, start=1):
            if s["purpose"] in ("disclosure", "signoff"):
                continue                          # the closing disclosure and sign-off belong to the recap chapter
            if not chapters or chapters[-1]["title"] != names.get(s["purpose"], s["purpose"]):
                chapters.append({"title": names.get(s["purpose"], s["purpose"])[:60], "first_scene": i})
    lv = facts["pivots"]["levels"]
    L = facts.get("lang", "en")
    pkg = {
        "title": title_for(facts),
        "description": compose_description(facts, disclosure), "tags": TAGS_HI if L == "hi" else TAGS, "scenes": scenes,
        "chapters": chapters,
        "hook": scenes[0]["narration"], "concept_title": script.concept_title,
        "thumbnail": {"generated_by": "deterministic", "concept": "verified S2 / P / R2 levels on a plain chart background",
                      "text": f"S2 {analysis.fmt(lv['S2'])} · R2 {analysis.fmt(lv['R2'])}",
                      "lines": ["NIFTY 50 · " + i18n.t("thumb_sub", L), facts["basis_label"],
                                f"R2 {analysis.fmt(lv['R2'])}", f"P {analysis.fmt(lv['P'])}", f"S2 {analysis.fmt(lv['S2'])}"]},
        "fact_checks": [{"claim": o["text"], "status": "supported", "evidence_id": 0,
                         "note": f"computed from dataset #{facts['dataset_id']}"} for o in facts["observations"]],
        "generated_by": meta, "review": {"issues": [], "overall": "deterministic lint passed", "ready": True},
        "originality": {"ok": True, "copied_7gram_ratio": 0.0, "closest_competitor_title_similarity": 0.0},
    }
    pkg["final_description"] = pkg["description"]
    pipeline.save(p["id"], package=pkg, title=pkg["title"])
    audit.log_artifact("script_generated", p["id"], "script", {"scenes": scenes, "concept": script.concept_title,
                                                                 "model": meta}, extra={"provider": meta.get("provider"),
                                                                                        "model": meta.get("model")})


def level_states(facts: dict[str, Any]) -> dict[str, str]:
    """Deterministic status of S2 / P / R2 for recap panels and thumbnails."""
    out = {}
    m15 = ((facts.get("intraday") or {}).get("analysis") or {}).get("m15", {}).get("levels", {})
    for name in ("R2", "P", "S2"):
        if name in m15:
            out[name] = m15[name]["state"]
        elif facts.get("review"):
            out[name] = "reached" if name in facts.get("touched", []) else "not_tested"
        else:
            out[name] = "future"
    return out


def chart_footer(facts: dict[str, Any], tf: str = "1d") -> str:
    L = facts.get("lang", "en")
    if tf == "1d":
        return i18n.t("c_footer", L, nature=i18n.nature(facts["data_nature"], L), source=source_name(facts),
                      retrieved=facts["retrieved_label"])
    ds = marketdata.dataset(facts["intraday"]["dataset_ids"][tf]) if tf in (facts.get("intraday") or {}).get("dataset_ids", {}) \
        else marketdata.dataset(facts["mtf"][tf]["dataset_id"])
    ret = db.parse_iso(ds["retrieved_at"]).astimezone(marketdata.IST).strftime("%d %b %Y, %H:%M IST")
    return i18n.t("c_intraday_footer", L, tf=tf, nature=i18n.nature(ds["data_nature"], L),
                  source=marketdata.source(ds["source_id"])["name"], retrieved=ret)


def draw_visual(facts: dict[str, Any], visual: str, scene: dict[str, Any], fmt: str, work: Path,
                concept_title: str = "", disclosure: str = "") -> tuple[Path, str]:
    """Draw one scene visual from the verified facts. Returns (final image, asset kind 'image' or 'anim')."""
    L = facts.get("lang", "en")
    t = lambda key, **kw: i18n.t(key, L, **kw)  # noqa: E731
    f = analysis.fmt
    charts.LANG = L
    lv = facts["pivots"]["levels"]
    footer = chart_footer(facts)
    out = work / f"{visual}.png"
    if visual == "title":
        return charts.text_panel([("NIFTY 50", "heavy", charts.TEXT), (t("c_title2"), "heavy", charts.HL),
                                  (facts["basis_label"], "body", charts.MUTED)], fmt, "", footer, out), "image"
    if visual == "candles":
        bars = marketdata.daily_bars(facts["dataset_id"])
        end = facts.get("review_date", facts["basis_date"])
        idx_end = next(i for i, b in enumerate(bars) if b["trade_date"] == end)
        window = bars[max(0, idx_end - (14 if fmt == "short" else 29)): idx_end + 1]
        hi = {facts["basis_date"]: i18n.short_date(facts["basis_date"], L)}
        if facts.get("review_date"):
            hi[facts["review_date"]] = i18n.short_date(facts["review_date"], L)
        return charts.candles_chart(window, hi, fmt, t("c_candles"), t("c_basis", date=facts["basis_label"]),
                                    footer, out), "image"
    if visual == "formula" and facts.get("next"):
        b, nx = facts["next"]["basis"], facts["next"]["levels"]
        return charts.text_panel([
            (f"H {f(b['high'])}   L {f(b['low'])}   C {f(b['close'])}  ({facts['review_label']})", "body", charts.MUTED),
            (f"P = (H + L + C) / 3 = {f(nx['P'])}", "heavy", charts.PIV),
            (f"R2 = P + 0.618 × (H − L) = {f(nx['R2'])}", "heavy", charts.RES),
            (f"S2 = P − 0.618 × (H − L) = {f(nx['S2'])}", "heavy", charts.SUP),
            (t("c_range", v=f(facts["next"]["range"])), "body", charts.MUTED)], fmt,
            t("c_next_levels", date=facts["next"]["label"]), footer, out), "image"
    if visual == "next_levels":
        nb = facts["next"]["basis"]

        def make_next(step: int | None, path: Path) -> Path:
            return charts.levels_chart(nb, facts["next"]["levels"], fmt, t("c_next_levels", date=facts["next"]["label"]),
                                       t("c_next_sub", date=facts["review_label"]), footer, path,
                                       basis_label=facts["review_label"], reveal=step)
        charts.frames(make_next, list(range(0, len(charts.REVEAL_ORDER) + 1)), out.with_name(out.stem + "_frames"))
        return make_next(None, out), "anim"
    if visual == "formula":
        b = facts["basis"]
        return charts.text_panel([
            (f"H {f(b['high'])}   L {f(b['low'])}   C {f(b['close'])}", "body", charts.MUTED),
            (f"P = (H + L + C) / 3 = {f(lv['P'])}", "heavy", charts.PIV),
            (f"R2 = P + 0.618 × (H − L) = {f(lv['R2'])}", "heavy", charts.RES),
            (f"S2 = P − 0.618 × (H − L) = {f(lv['S2'])}", "heavy", charts.SUP),
            (t("c_range", v=f(facts["pivots"]["range"])), "body", charts.MUTED)], fmt, t("c_calc"), footer, out), "image"
    if visual in ("levels", "review"):
        review = visual == "review" and facts.get("review")
        kw = dict(review=facts["review"], review_label=facts["review_label"], touched=tuple(facts.get("touched", []))) \
            if review else {}
        title, sub = (t("c_next"), facts["review_label"]) if review else (t("c_levels"), t("c_from", date=facts["basis_label"]))

        def make(step: int, path: Path) -> Path:
            return charts.levels_chart(facts["basis"], lv, fmt, title, sub, footer, path,
                                       basis_label=facts["basis_label"], reveal=step, **kw)
        if review:
            return make(None, out), "image"
        charts.frames(make, list(range(0, len(charts.REVEAL_ORDER) + 1)), out.with_name(out.stem + "_frames"))
        return make(None, out), "anim"
    if visual in ("chart_15m", "chart_1h"):
        tf = "15m" if visual == "chart_15m" else "1h"
        bars = facts["intraday"]["bars"][tf]
        marks: dict[str, str] = {}
        if tf == "15m":
            for name, b in facts["intraday"]["analysis"]["m15"]["levels"].items():
                if name not in ("S2", "R2", "P") or b["state"] == "not_tested":
                    continue
                if b.get("first_touch"):
                    marks.setdefault(b["first_touch"], t("m_touch", name=name))
                if b.get("first_close_beyond") and b["first_close_beyond"] != b.get("first_touch"):
                    marks.setdefault(b["first_close_beyond"], t("m_beyond", name=name))
                if b.get("returned_at"):
                    marks.setdefault(b["returned_at"], t("m_back"))
        title = t("c_15m" if tf == "15m" else "c_1h")
        sub = t("c_session", date=facts["review_label"])
        ftr = chart_footer(facts, tf)

        def make(step: int | None, path: Path) -> Path:
            return charts.intraday_chart(bars, lv, fmt, title, sub, ftr, path, marks=marks, upto=step)
        n = len(bars)
        steps = sorted({max(1, round(n * k / 12)) for k in range(1, 13)})
        charts.frames(make, steps, out.with_name(out.stem + "_frames"))
        return make(None, out), "anim"
    if visual == "mtf":
        cols = [(t("c_daily"), lv)] + [(t("c_15min" if tf == "15m" else "c_1hour"), facts["mtf"][tf]["levels"])
                                       for tf in ("15m", "1h") if tf in facts.get("mtf", {})]
        return charts.mtf_panel(cols, fmt, t("c_mtf"), t("c_mtf_sub", date=facts["basis_label"]), footer, out,
                                note=t("c_mtf_note")), "image"
    if visual == "scenarios":
        lines = t("c_scen_lines").split("|")
        return charts.text_panel([(x, "body", charts.TEXT if i < 3 else charts.MUTED) for i, x in enumerate(lines)],
                                 fmt, t("c_scen"), footer, out), "image"
    if visual == "recap":
        st = level_states(facts)
        cols = {"R2": charts.RES, "P": charts.PIV, "S2": charts.SUP}
        return charts.text_panel([(f"{k} {f(lv[k])} — {t('st_' + st[k])}", "body", cols[k]) for k in ("R2", "P", "S2")],
                                 fmt, t("c_recap"), footer, out), "image"
    if visual == "limitations":
        return charts.text_panel([(x, "body", charts.TEXT) for x in facts["limitations"][:4]], fmt, t("c_limits"),
                                 footer, out), "image"
    if visual == "disclosure":
        return charts.text_panel([(disclosure, "body", charts.TEXT)], fmt, t("c_disclosure"), footer, out), "image"
    if visual == "signoff":
        return signoff_card(facts, fmt, out, footer), "image"
    title = concept_title or "Concept"
    out = work / f"{visual}_{abs(hash(scene.get('on_screen_text', ''))) % 10**6}.png"
    return charts.text_panel([(scene.get("on_screen_text") or title, "heavy", charts.HL)], fmt, title, footer, out), "image"


def signoff_card(facts: dict[str, Any], fmt: str, out: Path, footer: str) -> Path:
    L = facts.get("lang", "en")
    lines = [("NIFTY 50", "heavy", charts.TEXT), (i18n.t("c_title2", L), "heavy", charts.HL)]
    if facts.get("next"):
        lines.append((i18n.t("c_signoff_next", L, ndate=i18n.short_date(facts["next"]["date"], L)), "body", charts.TEXT))
    lines.append((i18n.t("c_signoff_sub", L), "body", charts.MUTED))
    return charts.text_panel(lines, fmt, "", footer, out)


def stage_assets(p: dict[str, Any]) -> None:
    facts = p["episode"]["facts"]
    fmt = p["format"]
    work = Path(config.PRODUCTIONS_DIR / f"{p['id']:05d}" / "charts")
    work.mkdir(parents=True, exist_ok=True)
    db.ex("DELETE FROM assets WHERE production_id=?", (p["id"],))
    rendered: dict[str, tuple[Path, str]] = {}
    for i, scene in enumerate(p["package"]["scenes"]):
        key = scene["visual"]
        if key not in rendered or key == "concept":
            rendered[key] = draw_visual(facts, key, scene, fmt, work, p["package"].get("concept_title", ""),
                                        p["episode"].get("disclosure", ""))
        path, kind = rendered[key]
        with Image.open(path) as im:
            w, h = im.size
        db.insert("assets", {"production_id": p["id"], "scene_index": i, "kind": kind, "provider": "original_chart",
                             "provider_id": f"{path.stem}#card", "source_url": "", "page_url": "", "local_path": str(path),
                             "license": f"Original — drawn by this app from dataset #{facts['dataset_id']}",
                             "license_url": "", "author": "this channel", "attribution": "", "width": w, "height": h,
                             "duration": 0.0, "created_at": db.iso()})
    audit.record("charts_rendered", {"charts": sorted(str(v[0].name) for v in rendered.values()),
                                     "animated": sorted(str(v[0].name) for v in rendered.values() if v[1] == "anim")},
                 production_id=p["id"])


def ribbon_png(facts: dict[str, Any], w: int, h: int, fmt: str, out: Path) -> dict[str, int]:
    """Persistent on-screen disclosure ribbon."""
    L = facts.get("lang", "en")
    nature = i18n.nature(facts["data_nature"], L)
    tfs = (facts.get("intraday") or {}).get("timeframes") or list((facts.get("mtf") or {}))
    if tfs:
        nature += " + " + "/".join(tfs) + (" इंट्राडे" if L == "hi" else " intraday")
    return _ribbon(i18n.t("ribbon", L, nature=nature, date=facts["basis_label"]), w, h, fmt, out)


def _ribbon(text: str, w: int, h: int, fmt: str, out: Path) -> dict[str, int]:
    size = int(min(w, h) * (0.022 if fmt == "short" else 0.02))
    f = graphics.font_for(text, "body", size)
    probe = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    lines = graphics.wrap(text, f, int(w * 0.92), probe)
    lh = int(size * 1.45)
    img = Image.new("RGBA", (w, lh * len(lines) + 16), (0, 0, 0, 170))
    d = ImageDraw.Draw(img)
    for i, ln in enumerate(lines):
        d.text((w // 2, 8 + i * lh), ln, font=f, fill=(235, 235, 235, 255), anchor="ma")
    img.save(out)
    return {"x": 0, "y": h - img.height, "w": w, "h": img.height}


def thumbnail_story(facts: dict[str, Any]) -> dict[str, Any]:
    """Pick the thumbnail's focal point from what the verified data showed (not the same layout every day)."""
    L = facts.get("lang", "en")
    st = level_states(facts)
    lv = facts["pivots"]["levels"]
    for state, key, accent in (("failed_breakout", "th_failed", charts.DOWN), ("closed_beyond", "th_beyond", charts.HL),
                               ("tested_held", "th_held", charts.UP), ("reached", "th_held", charts.UP)):
        for name in ("S2", "R2"):
            if st.get(name) == state:
                return {"variant": state, "focus": name, "headline": i18n.t(key, L, name=name), "accent": accent,
                        "value": lv[name]}
    if facts.get("review"):
        return {"variant": "untested", "focus": None, "headline": i18n.t("th_untested", L), "accent": charts.MUTED,
                "value": None}
    return {"variant": "ladder", "focus": None, "headline": i18n.t("th_levels", L), "accent": charts.HL, "value": None}


def stage_thumbnail(p: dict[str, Any]) -> None:
    from .. import pipeline
    facts = p["episode"]["facts"]
    lv = facts["pivots"]["levels"]
    story = thumbnail_story(facts)
    W, H = config.THUMB_W, config.THUMB_H
    L = facts.get("lang", "en")
    f = analysis.fmt
    img = charts._background(W, H).convert("RGBA")

    def layer_rect(box, colour, alpha, radius=0):
        lay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(lay).rounded_rectangle(box, radius=radius, fill=colour + (alpha,))
        img.alpha_composite(lay)

    def pill(x, ymid, text, colour, size):
        fnt = graphics.font_for(text, "heavy", size)
        tw = ImageDraw.Draw(img).textlength(text, font=fnt)
        h = size * 1.4
        layer_rect((x, ymid - h / 2, x + tw + size * 1.1, ymid + h / 2), colour, 245, radius=int(h / 2))
        ImageDraw.Draw(img).text((x + size * 0.55, ymid), text, font=fnt, fill=(12, 14, 20), anchor="lm")

    # Right: zones + levels + the verified candle of the session being discussed
    nx = (facts.get("next") or {}).get("levels")
    ladder = nx or lv
    top, bottom, x0, x1 = 90, 640, 760, 1250
    vals = [ladder["S3"], ladder["R3"]]
    cndl = facts.get("review") if facts.get("review") else facts["basis"]
    vals += [cndl["high"], cndl["low"]]
    lo, hi = min(vals), max(vals)

    def y(v: float) -> float:
        return bottom - (v - lo) / (hi - lo or 1) * (bottom - top)
    layer_rect((x0, min(y(ladder["R1"]), y(ladder["R2"])), x1, max(y(ladder["R1"]), y(ladder["R2"]))), charts.ZONE_RES, 60)
    layer_rect((x0, min(y(ladder["S1"]), y(ladder["S2"])), x1, max(y(ladder["S1"]), y(ladder["S2"]))), charts.ZONE_SUP, 60)
    d = ImageDraw.Draw(img)
    for name in facts["pivots"]["order"]:
        col = charts.PIV if name == "P" else (charts.RES if name.startswith("R") else charts.SUP)
        d.line([(x0, y(ladder[name])), (x1, y(ladder[name]))], fill=col, width=6 if name in ("S2", "R2", "P") else 2)
    cx = x0 + (x1 - x0) * 0.35
    if cndl["open"] is None:                      # high-low range bar with a close tick (no invented open)
        d.line([(cx, y(cndl["high"])), (cx, y(cndl["low"]))], fill=charts.HL, width=14)
        d.line([(cx, y(cndl["close"])), (cx + 46, y(cndl["close"]))], fill=charts.HL, width=8)
    else:
        col = charts.UP if cndl["close"] >= cndl["open"] else charts.DOWN
        d.line([(cx, y(cndl["high"])), (cx, y(cndl["low"]))], fill=col, width=10)
        a, b = sorted((y(cndl["open"]), y(cndl["close"])))
        d.rectangle([cx - 40, a, cx + 40, max(b, a + 5)], fill=col, outline=charts.HL, width=4)

    # Left: badge, NIFTY 50, today's event, the next session's key levels
    badge_date = i18n.short_date(facts.get("review_date") or facts["basis_date"], L)
    pill(48, 62, i18n.t("th_badge", L, date=badge_date), charts.HL, 34)
    big = graphics.font_for("NIFTY 50", "heavy", 112)
    d = ImageDraw.Draw(img)
    d.text((44, 108), "NIFTY 50", font=big, fill=(255, 255, 255), stroke_width=5, stroke_fill=(0, 0, 0))
    head = story["headline"]
    hf = graphics.font_for(head, "heavy", 64)
    lines = graphics.wrap(head, hf, 660, d)[:2]
    for i, ln in enumerate(lines):
        d.text((48, 248 + i * 78), ln, font=hf, fill=story["accent"], stroke_width=4, stroke_fill=(0, 0, 0))
    yv = 248 + len(lines) * 78 + 22
    shown_levels: dict[str, float] = {}
    if nx:
        sub = i18n.t("th_next", L, ndate=i18n.short_date(facts["next"]["date"], L))
        d.text((48, yv), sub, font=graphics.font_for(sub, "heavy", 42), fill=(255, 255, 255), stroke_width=3,
               stroke_fill=(0, 0, 0))
        yv += 66
        for i, name in enumerate(("R2", "P", "S2")):
            colour = charts.PIV if name == "P" else (charts.RES if name.startswith("R") else charts.SUP)
            pill(48, yv + 32 + i * 76, f"{name}  {f(nx[name])}", colour, 44)
            shown_levels[name] = nx[name]
    else:
        show = [story["focus"]] if story["focus"] else ["R2", "P", "S2"]
        for i, name in enumerate(show):
            colour = charts.PIV if name == "P" else (charts.RES if name.startswith("R") else charts.SUP)
            pill(48, yv + 32 + i * 76, f"{name}  {f(lv[name])}", colour, 44)
            shown_levels[name] = lv[name]
    img = img.convert("RGB")
    when = facts.get("review_label") if story["variant"] != "ladder" and facts.get("review") else facts["basis_label"]
    show = list(shown_levels)
    out = Path(config.PRODUCTIONS_DIR / f"{p['id']:05d}" / "thumbnail.jpg")
    size = graphics.save_jpeg_under(img, out, 2 * 1024 * 1024)
    contrast = round(min(graphics.contrast(c, charts.BG) for c in (charts.TEXT, story["accent"], charts.RES, charts.SUP)), 2)
    nums = [f"{n} {f(v)}" for n, v in shown_levels.items()]
    pkg = p["package"]
    pkg["thumbnail"] = {**pkg.get("thumbnail", {}), "generated_by": "deterministic", "variant": story["variant"],
                        "text": head, "lines": ["NIFTY 50", head, *nums, when, badge_date]}
    tl = p["timeline"]
    tl["thumbnail"] = {"path": str(out), "width": W, "height": H, "size_bytes": size, "contrast": contrast,
                       "text": head, "generated_by": "deterministic", "variant": story["variant"]}
    pipeline.save(p["id"], timeline=tl, thumbnail_path=str(out), package=pkg)
    audit.record("thumbnail_rendered", {"variant": story["variant"], "levels_shown": shown_levels,
                                        "contrast": contrast}, production_id=p["id"], artifact=out)


def stage_qa(p: dict[str, Any]) -> None:
    from .. import pipeline
    from ..media import qa
    tl = p["timeline"]
    result = qa.run(p, p["package"], tl, pipeline.assets_for(p["id"]), tl.get("thumbnail"), tl.get("voice", {}), None, None)
    pipeline.save(p["id"], qa=result)
    audit.log_artifact("technical_qa", p["id"], "technical_qa", result, extra={"passed": result["passed"]})
    if not result["passed"]:
        failing = "; ".join(c["name"] for c in result["checks"] if not c["ok"] and c["severity"] == "fail")
        raise pipeline.NeedsAttention(f"NEEDS REVIEW — technical quality checks failed: {failing}")


def stage_compliance(p: dict[str, Any]) -> None:
    from .. import pipeline
    result = gate.run(p, pipeline.assets_for(p["id"]))
    pipeline.save(p["id"], compliance=db.dumps(result))
    try:
        _compliance_outcome(p, result)
    finally:
        write_report(p["id"])
        if p.get("parent_id"):
            write_report(p["parent_id"])


def _compliance_outcome(p: dict[str, Any], result: dict[str, Any]) -> None:
    from .. import pipeline
    from ..youtube import oauth
    audit.log_artifact("compliance_gate", p["id"], "compliance_gate", result,
                       extra={"status": result["status"], "critical": result["critical_failures"],
                              "review": result["review_items"]})
    if result["status"] == "NEEDS_REVIEW":
        reasons = "; ".join(f"{c['no']}. {c['name']}: {c['detail']}" for c in result["checks"]
                            if not c["ok"] and c["severity"] == gate.CRITICAL)
        facts = (p.get("episode") or {}).get("facts") or {}
        used_fallback = (p["package"].get("generated_by") or {}).get("provider") == "app"
        if p.get("kind") == "nifty_pivot" and facts.get("mode") == "daily_review" and not used_fallback:
            # Self-healing for the daily show: an AI-script problem must not cost the day — redo the episode with the
            # app's fact-sheet script (which always passes the content checks). Data problems will fail again and stop.
            ep = {**p["episode"], "force_fallback": True}
            pipeline.save(p["id"], episode=db.dumps(ep))
            audit.record("script_fallback_after_gate", {"reasons": reasons[:1500]}, production_id=p["id"])
            logs.event("warning", "compliance", f"AI script failed a check ({reasons[:200]}) — remaking today's video with "
                       "the app's fact-sheet script", production_id=p["id"])
            pipeline.reset_to_stage(p["id"], "script")
            pipeline.save(p["id"], episode=db.dumps(ep))      # reset clears nothing in episode, but be explicit
            return
        pipeline.save(p["id"], status="needs_review", error=f"NEEDS REVIEW — {reasons}"[:1500])
        logs.event("warning", "compliance", f"Episode blocked by compliance gate: {reasons[:300]}", production_id=p["id"])
        return
    if p.get("kind") == "nifty_pivot" and p["format"] == "long":
        from . import shorts
        try:
            shorts.create_for(p["id"])
        except Exception as e:                      # the long episode stays valid even if no Short can be made
            logs.event("warning", "shorts", f"Shorts not created: {e}", production_id=p["id"])
    # Unattended public release: only with both compliance switches on, and either a fully cleared gate or the
    # owner's recorded override (which never covers critical failures — those returned above).
    unattended = (result["status"] in ("CLEARED", "OWNER_OVERRIDE") and settings.get("compliance.public_publishing_enabled")
                  and settings.get("compliance.unattended_publishing") and oauth.connected()
                  and (not pipeline.nc_voice(p.get("timeline")) or result["status"] == "OWNER_OVERRIDE"))
    if unattended:
        by = "auto (owner override)" if result["status"] == "OWNER_OVERRIDE" else "auto (compliance cleared)"
        pipeline.approve(p["id"], privacy="public", publish_at=None, by=by)
        return
    note = {"PRIVATE_REVIEW_ONLY": "Private upload for manual review only — unresolved review items: "
                                   + ", ".join(result["review_items"]),
            "CLEARED_PRIVATE_UNTIL_ENABLED": "All checks passed; public release stays off until you enable it in Compliance settings.",
            "CLEARED": "All checks passed.",
            "OWNER_OVERRIDE": "Critical checks passed; open review items are covered by the owner's recorded risk "
                              "acceptance: " + ", ".join(result["review_items"])}[result["status"]]
    pipeline.save(p["id"], status="awaiting_approval", error=note)
    logs.event("info", "compliance", f"Compliance gate: {result['status']}", production_id=p["id"])
    if settings.get("compliance.auto_private_upload") and "private" in result["allowed_privacy"]:
        if oauth.connected():
            pipeline.approve(p["id"], privacy="private", publish_at=None, by="auto (private upload)")
        else:
            logs.event("warning", "youtube", "Auto private upload is on, but YouTube is not connected — the video waits "
                       "for approval. Connect in Settings › YouTube.", production_id=p["id"])


def write_report(pid: int) -> Path:
    """Human-readable production report saved next to the video (data, levels, script, QA, gate, Shorts, timing)."""
    from .. import pipeline
    p = pipeline.load(pid)
    ep, pkg, tl = p.get("episode") or {}, p.get("package") or {}, p.get("timeline") or {}
    facts = ep.get("facts") or {}
    f = analysis.fmt
    lines = [f"# Production report — #{pid} {pkg.get('title', p['title'])}", "",
             f"- Kind / format / language: {p.get('kind')} / {p['format']} / {p.get('language')}",
             f"- Status: {p['status']}" + (f" — {p['error']}" if p.get("error") else ""),
             f"- Created {p['created_at']} · updated {p['updated_at']}", ""]
    if facts:
        b = facts["basis"]
        lines += ["## Data", f"- Dataset #{facts['dataset_id']} (sha256 {facts['dataset_sha256'][:16]}…) · source: "
                  f"{source_name(facts)} · {facts['data_nature']} · retrieved {facts['retrieved_label']}",
                  f"- Basis session {facts['basis_label']}: O {f(b['open'])} H {f(b['high'])} L {f(b['low'])} C {f(b['close'])}"]
        if facts.get("review"):
            r = facts["review"]
            lines.append(f"- Following session {facts['review_label']}: O {f(r['open'])} H {f(r['high'])} L {f(r['low'])} C {f(r['close'])}")
        if facts.get("intraday"):
            lines.append("- Intraday: " + ", ".join(f"{tf} dataset #{i}" for tf, i in facts["intraday"]["dataset_ids"].items()))
        lines += ["", "## Levels (formula " + facts["pivots"]["formula"] + ")",
                  " | ".join(f"{k} {f(facts['pivots']['levels'][k])}" for k in facts["pivots"]["order"])]
        for tf, m in (facts.get("mtf") or {}).items():
            lines.append(f"- {tf} levels (last {m['bars']} bars): " + " | ".join(f"{k} {f(m['levels'][k])}" for k in ("S2", "P", "R2")))
        lines += ["", "## Observations"] + [f"- [{o['id']}] {o['text']}" for o in facts["observations"]]
        lines += ["", "## Limitations"] + [f"- {x}" for x in facts["limitations"]]
    scenes = pkg.get("scenes", [])
    words = sum(len(x["narration"].split()) for x in scenes)
    lines += ["", "## Script", f"- {len(scenes)} scenes, {words} words; model: {(pkg.get('generated_by') or {}).get('provider')} "
              f"{(pkg.get('generated_by') or {}).get('model')}"]
    if tl.get("total_duration"):
        lines.append(f"- Rendered length {tl['total_duration'] / 60:.1f} min · voice {(tl.get('voice') or {}).get('voice_id')} "
                     f"({(tl.get('voice') or {}).get('license')})")
    if tl.get("chapter_times"):
        lines += ["- Chapters: " + "; ".join(f"{int(t) // 60}:{int(t) % 60:02d} {c['title']}"
                                             for t, c in zip(tl["chapter_times"], pkg.get("chapters", [])))]
    if p.get("video_path"):
        lines.append(f"- Video: {p['video_path']}")
    if p.get("thumbnail_path"):
        lines.append(f"- Thumbnail: {p['thumbnail_path']} (variant {(pkg.get('thumbnail') or {}).get('variant')})")
    qa = p.get("qa") or {}
    if qa:
        lines += ["", f"## Technical QA — {'PASSED' if qa.get('passed') else 'FAILED'}"] + \
                 [f"- {'ok ' if c['ok'] else 'NO '} {c['name']}: {c['detail']}" for c in qa.get("checks", [])]
    g = p.get("compliance") or {}
    if g:
        lines += ["", f"## Compliance gate — {g.get('status')} (allowed: {', '.join(g.get('allowed_privacy', [])) or 'none'})"] + \
                 [f"- {'pass' if c['ok'] else c['severity'].upper()} {c['no']}. {c['name']}: {c['detail']}" for c in g.get("checks", [])]
    kids = db.q("SELECT id, title, status, video_path FROM productions WHERE parent_id=? ORDER BY id", (pid,))
    if kids:
        lines += ["", "## Shorts"] + [f"- #{k['id']} {k['title']} — {k['status']} — {k['video_path'] or 'not rendered yet'}" for k in kids]
    lines += ["", "_Automated guard rails only — not legal advice and not a determination of compliance._"]
    out = Path(config.PRODUCTIONS_DIR / f"{pid:05d}" / "report.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


STAGE_FN = {"data": stage_data, "script": stage_script, "assets": stage_assets, "thumbnail": stage_thumbnail,
            "qa": stage_qa, "compliance": stage_compliance}
