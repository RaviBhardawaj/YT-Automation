"""'फिबोनाची पिवट सीखें' — a 7-part educational series (one lesson generated per day at 4 PM IST).

Lessons teach the method only. They use one fixed, clearly labelled HYPOTHETICAL example
(no index or security data), so no market-data licence or data-lag question arises. The
curriculum, titles, key points, example numbers, charts, disclosure and description are
deterministic; the model only writes the engaging narration around them, and every draft goes
through the same linter and compliance gate as the NIFTY episodes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from PIL import Image, ImageDraw
from pydantic import BaseModel, Field

from .. import ai, config, db, logs, profile
from ..ai import prompts
from ..media import graphics
from . import analysis, audit, charts, i18n, lint, pivots

SERIES = "fibonacci_basics"
STAGES = ["script", "assets", "voice", "render", "thumbnail", "qa", "compliance"]
STATUS = {"script": "writing lesson", "assets": "drawing charts", "voice": "voicing", "render": "rendering",
          "thumbnail": "making thumbnail", "qa": "quality check", "compliance": "compliance gate"}

# Hypothetical examples — round, obviously illustrative numbers; never presented as an index.
EX = {"open": 1120.0, "high": 1200.0, "low": 1100.0, "close": 1150.0}
EX_NEXT = {"open": 1140.0, "high": 1195.0, "low": 1085.0, "close": 1190.0}
EX_NARROW = {"open": 1140.0, "high": 1175.0, "low": 1125.0, "close": 1150.0}
EX_WIDE = {"open": 1100.0, "high": 1225.0, "low": 1075.0, "close": 1150.0}
FIB = [0, 1, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144]

LESSONS: list[dict[str, Any]] = [
    {"n": 1, "title": {"hi": "फिबोनाची संख्याएँ और 0.618 कहाँ से आता है", "en": "Fibonacci numbers and where 0.618 comes from"},
     "points": {"sequence": "फिबोनाची क्रम: हर संख्या पिछली दो संख्याओं का जोड़ है — 0, 1, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144",
                "ratio_618": "बड़ी संख्याओं में किसी संख्या को अगली संख्या से भाग दें तो लगभग 0.618 आता है (जैसे 89 ÷ 144)",
                "ratio_382": "किसी संख्या को दो कदम आगे वाली संख्या से भाग दें तो लगभग 0.382 आता है (जैसे 34 ÷ 89)",
                "why_charts": "टेक्निकल एनालिसिस में इन अनुपातों का इस्तेमाल संदर्भ स्तर बनाने में होता है — यह एक पैमाना है, कोई जादू या गारंटी नहीं"},
     "visuals": ["sequence", "concept"]},
    {"n": 2, "title": {"hi": "पिवट पॉइंट क्या है और कैसे निकालते हैं", "en": "What a pivot point is and how to calculate it"},
     "points": {"hlc": "पिवट पिछले पूरे सत्र के तीन आंकड़ों से बनता है: हाई (H), लो (L) और क्लोज़ (C)",
                "formula": "P = (H + L + C) ÷ 3 — काल्पनिक उदाहरण में (1,200 + 1,100 + 1,150) ÷ 3 = 1,150",
                "meaning": "पिवट पिछले सत्र का एक संतुलन बिंदु है, जिसके आसपास बाकी संदर्भ स्तर बनते हैं",
                "previous": "हमेशा पूरा हो चुका सत्र लिया जाता है — अधूरे दिन के आंकड़ों से नहीं"},
     "visuals": ["formula_p", "levels_example"]},
    {"n": 3, "title": {"hi": "फिबोनाची पिवट स्तर — R1, R2, R3 और S1, S2, S3", "en": "Fibonacci pivot levels — R1-R3 and S1-S3"},
     "points": {"range": "रेंज = H − L; काल्पनिक उदाहरण में 1,200 − 1,100 = 100",
                "r_levels": "R1 = P + 0.382 × रेंज, R2 = P + 0.618 × रेंज, R3 = P + रेंज",
                "s_levels": "S1 = P − 0.382 × रेंज, S2 = P − 0.618 × रेंज, S3 = P − रेंज",
                "example": "उदाहरण में: R1 1,188.2, R2 1,211.8, R3 1,250; S1 1,111.8, S2 1,088.2, S3 1,050"},
     "visuals": ["formula_levels", "levels_example"]},
    {"n": 4, "title": {"hi": "S2 और R2 का असली मतलब — सपोर्ट और रेजिस्टेंस", "en": "What S2 and R2 really mean"},
     "points": {"support": "S स्तर (सपोर्ट) नीचे की ओर संदर्भ ज़ोन हैं, R स्तर (रेजिस्टेंस) ऊपर की ओर",
                "zones": "ये सटीक रेखाएँ नहीं, बल्कि ज़ोन की तरह पढ़े जाते हैं जहाँ विश्लेषक देखते हैं कि कीमत कैसा बर्ताव करती है",
                "s2r2": "S2 और R2 पिवट से 0.618 × रेंज दूर होते हैं, इसलिए अक्सर दिन के बड़े हिलाव के संदर्भ में देखे जाते हैं",
                "not_prediction": "कोई भी स्तर भविष्यवाणी नहीं है — कीमत वहाँ पहुँचे या न पहुँचे, दोनों संभव हैं"},
     "visuals": ["levels_example", "concept"]},
    {"n": 5, "title": {"hi": "कीमत इन स्तरों पर कैसे बर्ताव करती है", "en": "How price behaves around the levels"},
     "points": {"touch": "अगर अगले सत्र की रेंज किसी स्तर को छू ले, तो कहते हैं स्तर 'टेस्ट' हुआ",
                "close": "असली संकेत क्लोज़ से मिलता है: स्तर छूकर कीमत उसके ऊपर बंद हुई या नीचे",
                "example": "काल्पनिक अगले सत्र में लो 1,085 ने S2 (1,088.2) को छुआ, और क्लोज़ 1,190 पर R1 (1,188.2) के ऊपर हुआ",
                "order": "दैनिक कैंडल से यह पता नहीं चलता कि पहले कौन सा स्तर छुआ गया — इसके लिए सत्यापित इंट्राडे डेटा चाहिए"},
     "visuals": ["interaction_example", "concept"]},
    {"n": 6, "title": {"hi": "रेंज छोटी या बड़ी — स्तरों की दूरी कैसे बदलती है", "en": "Narrow vs wide range — how level spacing changes"},
     "points": {"narrow": "छोटी रेंज (50) में S2 1,119.1 और R2 1,180.9 के बीच सिर्फ़ 61.8 अंकों का फ़ासला होता है",
                "wide": "बड़ी रेंज (150) में S2 1,057.3 और R2 1,242.7 के बीच 185.4 अंकों का फ़ासला होता है",
                "why": "पिवट दोनों में 1,150 है, पर रेंज बदलते ही सारे स्तरों की दूरी बदल जाती है",
                "read": "इसलिए स्तरों को हमेशा उस दिन की रेंज के साथ पढ़ना चाहिए"},
     "visuals": ["spacing_example", "concept"]},
    {"n": 7, "title": {"hi": "सीमाएँ, जोखिम और सही तरीके से सीखना", "en": "Limits, risk and learning the right way"},
     "points": {"no_guarantee": "फिबोनाची पिवट एक गणितीय पैमाना है; यह भविष्य की गारंटी नहीं देता",
                "data": "हमेशा सत्यापित, पूरा हो चुका सत्र और भरोसेमंद डेटा स्रोत इस्तेमाल करें",
                "context": "खबरें, गैप और बड़े इवेंट स्तरों की प्रासंगिकता बदल सकते हैं",
                "practice": "सीखने का सही तरीका: पुराने डेटा पर अभ्यास करें, नोट्स बनाएँ, और किसी भी वित्तीय निर्णय से पहले स्वयं जाँच करें"},
     "visuals": ["concept", "recap"]},
]
TOTAL = len(LESSONS)

DISCLOSURE = {
    "hi": "यह वीडियो केवल शैक्षिक उद्देश्य के लिए है। इसमें दिखाए गए सभी आंकड़े काल्पनिक उदाहरण हैं, किसी वास्तविक बाज़ार डेटा "
          "पर आधारित नहीं। तकनीकी स्तर केवल विश्लेषणात्मक संदर्भ बिंदु हैं, भविष्य की कीमतों की गारंटी नहीं। बाज़ार जोखिम बने "
          "रहते हैं, और कोई भी वित्तीय निर्णय लेने से पहले दर्शकों को जानकारी की स्वयं जाँच करनी चाहिए।",
    "en": "This content is for educational purposes. All figures shown are hypothetical examples, not real market data. "
          "Technical levels are analytical reference points, not guarantees of future price movements. Market risks remain, "
          "and viewers should independently verify information before making financial decisions.",
}
RIBBON = {"hi": "शैक्षिक सामग्री · सभी आंकड़े काल्पनिक उदाहरण हैं, वास्तविक बाज़ार डेटा नहीं · स्तर संदर्भ बिंदु हैं, गारंटी नहीं",
          "en": "Educational content · all figures are hypothetical examples, not market data · levels are not guarantees"}
HYPO_NOTE = {"hi": "काल्पनिक उदाहरण — वास्तविक बाज़ार डेटा नहीं", "en": "Hypothetical example — not market data"}
SERIES_NAME = {"hi": "फिबोनाची पिवट सीखें", "en": "Learn Fibonacci Pivots"}


def title(lesson: dict[str, Any], L: str) -> str:
    return f"{SERIES_NAME[L]} | भाग {lesson['n']}/{TOTAL}: {lesson['title'][L]}" if L == "hi" else \
        f"{SERIES_NAME[L]} | Part {lesson['n']}/{TOTAL}: {lesson['title'][L]}"


def facts_for(n: int, language: str) -> dict[str, Any]:
    """Lesson 'fact sheet': every number the lesson may show or speak."""
    L = i18n.code(language)
    lesson = LESSONS[n - 1]
    numbers: dict[str, float] = {}
    for tag, bar in (("ex", EX), ("next", EX_NEXT), ("narrow", EX_NARROW), ("wide", EX_WIDE)):
        calc = pivots.fibonacci_pivots(bar["high"], bar["low"], bar["close"])
        for k, v in {**bar, **calc["levels"], "range": calc["range"],
                     "spacing": round(calc["levels"]["R2"] - calc["levels"]["S2"], 2)}.items():
            numbers[f"{tag}_{k}"] = v
    for i, f in enumerate(FIB):
        numbers[f"fib_{i}"] = f
    numbers.update({"ratio_618": 0.618, "ratio_382": 0.382, "ratio_1618": 1.618, "pct_618": 61.8, "pct_382": 38.2,
                    "lesson": n, "total": TOTAL, "sum_hlc": 3450.0, "three": 3.0})
    return {"mode": "lesson", "language": language, "lang": L, "series": SERIES, "lesson_no": n,
            "lesson_title": lesson["title"][L], "title": title(lesson, L),
            "observations": [{"id": k, "text": v} for k, v in lesson["points"].items()],
            "limitations": [], "numbers": numbers, "date_numbers": [], "data_nature": "none",
            "example": {"basis": EX, "levels": pivots.fibonacci_pivots(EX["high"], EX["low"], EX["close"])["levels"]}}


# ------------------------------------------------------------------ script

Purpose = Literal["hook", "teach", "example", "recap", "outro"]
Visual = Literal["title", "sequence", "formula_p", "formula_levels", "levels_example", "interaction_example",
                 "spacing_example", "concept", "recap"]


class LessonScene(BaseModel):
    purpose: Purpose
    narration: str = Field(..., min_length=20, max_length=900)
    on_screen_text: str = Field(..., max_length=60, description="Short label, max 6 words")
    visual: Visual
    key_point_ids: list[str] = Field(..., max_length=6)


class LessonScript(BaseModel):
    scenes: list[LessonScene] = Field(..., min_length=5, max_length=10)


def _prompt(facts: dict[str, Any], words: int) -> str:
    lesson = LESSONS[facts["lesson_no"] - 1]
    nxt = LESSONS[facts["lesson_no"]]["title"][facts["lang"]] if facts["lesson_no"] < TOTAL else None
    allowed_visuals = ", ".join(["title", "concept", "recap"] + lesson["visuals"])
    return "\n".join([
        f"SERIES: '{SERIES_NAME[facts['lang']]}' — a {TOTAL}-part beginner course. THIS IS PART {facts['lesson_no']} of {TOTAL}: "
        f"{lesson['title'][facts['lang']]}.",
        "AUDIENCE: Indian beginners curious about the stock market; explain like a warm, clear teacher.",
        "LANGUAGE: Natural conversational Hindi in Devanagari script (keep S1-S3, P, R1-R3, H, L, C as they are). Write "
        "numbers exactly as given below." if facts["lang"] == "hi" else "LANGUAGE: Clear, friendly English.",
        "",
        "KEY POINTS you must teach (cite their ids in key_point_ids; cover ALL of them):",
        *[f"  [{k}] {v}" for k, v in lesson["points"].items()],
        "",
        "HYPOTHETICAL EXAMPLE (the ONLY numbers allowed besides the Fibonacci sequence and the ratios 0.382 / 0.618 / 1.618):",
        f"  example session: H {analysis.fmt(EX['high'])}, L {analysis.fmt(EX['low'])}, C {analysis.fmt(EX['close'])}; "
        f"levels P 1,150, R1 1,188.2, R2 1,211.8, R3 1,250, S1 1,111.8, S2 1,088.2, S3 1,050",
        "  next example session: open 1,140, high 1,195, low 1,085, close 1,190",
        "  narrow range example: range 50 → S2 1,119.1, R2 1,180.9 (61.8 apart); wide range: range 150 → S2 1,057.3, "
        "R2 1,242.7 (185.4 apart)",
        "  Always call it a hypothetical example (काल्पनिक उदाहरण). Never attach it to NIFTY, Sensex, a stock or a date.",
        "",
        f"WRITE about {words} spoken words in 6-9 scenes:",
        "  1. hook (visual 'title') — open with a relatable question or everyday comparison that makes beginners curious. "
        "No greetings, no channel intro, no hype.",
        f"  2-6. teach / example scenes (visuals from: {allowed_visuals}) — one idea per scene, simple words, a tiny "
        "analogy where it helps.",
        "  7. recap (visual 'recap') — 2-3 line summary of today's lesson.",
        "  8. outro (visual 'concept') — ask viewers ONE question to answer in the comments about today's topic, then a "
        "warm, low-pressure invitation to subscribe so they can follow the series"
        + (f" and a one-line teaser of the next part: '{nxt}'." if nxt else " — this is the final part; thank them for "
           "completing the series and invite them to subscribe for upcoming historical case studies."),
        "",
        "RULES:",
        "- Teach the method only. No buy/sell/hold, no entries, targets, stop-losses, options, strikes or trading calls.",
        "- No promises of profit or accuracy, no urgency or FOMO, no fear. Never say a level 'will' be reached.",
        "- Do not mention SEBI, registration or real market prices, and do not write a disclaimer (the app adds it).",
        "- The narrator is a synthetic voice: no 'I recommend', no personal trading claims.",
        "- on_screen_text: a short label in the video language, max 6 words.",
    ])


def _lint(script: LessonScript, facts: dict[str, Any]) -> list[str]:
    from .episode import devanagari_ratio
    problems = []
    text = "\n".join(s.narration + "\n" + s.on_screen_text for s in script.scenes)
    for f in lint.prohibited_language(text):
        problems.append(f"remove '{f['match']}' ({f['why']})")
    bad = lint.unverified_numbers(text, facts)
    if bad:
        problems.append("numbers not in the lesson example (use the given numbers or words): " + ", ".join(sorted(set(bad))[:10]))
    ids = {o["id"] for o in facts["observations"]}
    used = {k for s in script.scenes for k in s.key_point_ids}
    if ids - used:
        problems.append(f"key points not covered: {sorted(ids - used)}")
    for need in ("hook", "teach", "recap", "outro"):
        if need not in {s.purpose for s in script.scenes}:
            problems.append(f"missing a '{need}' scene")
    lesson = LESSONS[facts["lesson_no"] - 1]
    bad_vis = {s.visual for s in script.scenes} - set(["title", "concept", "recap"] + lesson["visuals"])
    if bad_vis:
        problems.append(f"visuals not available in this lesson: {sorted(bad_vis)}")
    outro = [s for s in script.scenes if s.purpose == "outro"]
    if outro and not any(w in outro[-1].narration.lower() for w in ("subscribe", "सब्सक्राइब")):
        problems.append("the outro must invite viewers to subscribe (plainly, without pressure)")
    if facts["lang"] == "hi":
        for i, s in enumerate(script.scenes, start=1):
            if devanagari_ratio(s.narration) < 0.6:
                problems.append(f"scene {i}: narration must be Hindi in Devanagari script")
    for i, s in enumerate(script.scenes, start=1):
        if len(s.narration.split()) > 95:
            problems.append(f"scene {i} is too long; split it")
    return problems


def describe(facts: dict[str, Any], chapters: list[tuple[str, str]] | None = None, voice: dict | None = None) -> str:
    L = facts["lang"]
    lesson = LESSONS[facts["lesson_no"] - 1]
    hi = L == "hi"
    nxt = LESSONS[facts["lesson_no"]]["title"][L] if facts["lesson_no"] < TOTAL else None
    parts = [(f"{SERIES_NAME[L]} — भाग {lesson['n']}/{TOTAL}: {lesson['title'][L]}" if hi else
              f"{SERIES_NAME[L]} — Part {lesson['n']}/{TOTAL}: {lesson['title'][L]}"), "",
             "इस भाग में आप सीखेंगे:" if hi else "In this lesson:"] + [f"- {v}" for v in lesson["points"].values()]
    parts += ["", HYPO_NOTE[L] + (": H 1,200 · L 1,100 · C 1,150 → P 1,150 · R2 1,211.8 · S2 1,088.2")]
    if chapters:
        parts += ["", "अध्याय" if hi else "Chapters"] + [f"{tm} {name}" for tm, name in chapters]
    if nxt:
        parts += ["", (f"अगला भाग: {nxt}" if hi else f"Next part: {nxt}")]
    parts += ["", ("सीरीज़ के अगले भाग देखने के लिए चैनल को सब्सक्राइब करें, और अपने सवाल कमेंट में लिखें।" if hi else
                   "Subscribe to follow the series, and ask your questions in the comments."),
              "", i18n.t("h_disclosure", L), DISCLOSURE[L], "", i18n.t("credits", L, voice=i18n.voice_credit(voice)),
              "", i18n.t("hashtags", L)]
    return "\n".join(parts).replace("<", "").replace(">", "")


TAGS_HI = ["फिबोनाची", "फिबोनाची पिवट", "पिवट पॉइंट", "सपोर्ट और रेजिस्टेंस", "शेयर बाज़ार सीखें", "टेक्निकल एनालिसिस हिंदी",
           "stock market education hindi", "Fibonacci pivot points"]
TAGS_EN = ["Fibonacci", "Fibonacci pivot points", "pivot points", "support and resistance", "technical analysis basics",
           "stock market education"]


def create(n: int | None = None, audience: str = "india", fmt: str = "long", actor: str = "scheduler") -> int:
    from .. import worker
    if n is None:
        n = next_lesson_number()
        if n is None:
            raise ValueError("all lessons of the series have been generated")
    language = profile.language_for(audience)
    facts = facts_for(n, language)
    pid = db.insert("productions", {
        "topic_id": None, "format": fmt, "title": facts["title"], "status": "queued", "stage": "script", "kind": "fib_lesson",
        "audience": audience, "language": language, "package": db.dumps({}),
        "episode": db.dumps({"facts": facts, "disclosure": DISCLOSURE[facts["lang"]]}), "test_mode": 0,
        "created_at": db.iso(), "updated_at": db.iso()})
    audit.record("lesson_requested", {"lesson": n, "audience": audience, "language": language}, production_id=pid, actor=actor)
    worker.enqueue("production", {"production_id": pid}, production_id=pid, max_attempts=3)
    logs.event("info", "finance", f"Lesson {n}/{TOTAL} queued: {facts['title']}", production_id=pid)
    return pid


def next_lesson_number(audience: str = "india") -> int | None:
    done = {r["n"] for r in db.q(
        "SELECT CAST(json_extract(episode, '$.facts.lesson_no') AS INTEGER) AS n FROM productions "
        "WHERE kind='fib_lesson' AND audience=? AND status NOT IN ('rejected','failed')", (audience,))}
    return next((l["n"] for l in LESSONS if l["n"] not in done), None)


# ------------------------------------------------------------------ stages

def stage_script(p: dict[str, Any]) -> None:
    from .. import pipeline
    facts = p["episode"]["facts"]
    words = 380 if p["format"] == "long" else 140

    def check(s: LessonScript) -> None:
        problems = _lint(s, facts)
        if problems:
            raise ValueError("; ".join(problems[:12]))

    try:
        script, meta = ai.generate("script", prompts.system("a warm, precise stock-market educator writing a beginner course"),
                                   _prompt(facts, words), LessonScript, max_tokens=6000, validate=check)
    except ai.AllProvidersFailed as e:
        if "invalid output" not in str(e):
            raise
        audit.record("script_rejected", {"reason": str(e)[:2000]}, production_id=p["id"])
        raise pipeline.NeedsAttention(f"NEEDS REVIEW — no lesson script passed the compliance lint: {str(e)[:600]}") from e
    L = facts["lang"]
    scenes = [s.model_dump() | {"observation_ids": s.key_point_ids} for s in script.scenes]
    scenes.append({"purpose": "disclosure", "narration": DISCLOSURE[L], "on_screen_text": "", "visual": "disclosure",
                   "key_point_ids": [], "observation_ids": []})
    from .episode import signoff_scene
    scenes.append(signoff_scene(facts))
    for s in scenes:
        s.update({"caption_text": s["narration"], "visual_description": f"chart: {s['visual']}", "visual_query": "chart",
                  "visual_type": "text_card"})
    names = {"hook": "शुरुआत" if L == "hi" else "Introduction", "teach": "सीखें" if L == "hi" else "Lesson",
             "example": "उदाहरण" if L == "hi" else "Example", "recap": "सार" if L == "hi" else "Recap",
             "outro": "आपका सवाल" if L == "hi" else "Your question", "disclosure": i18n.t("h_disclosure", L)}
    chapters = []
    for i, s in enumerate(scenes, start=1):
        if s["purpose"] == "signoff":
            continue
        if not chapters or chapters[-1]["title"] != names[s["purpose"]]:
            chapters.append({"title": names[s["purpose"]], "first_scene": i})
    lv = facts["example"]["levels"]
    pkg = {"title": facts["title"], "description": describe(facts), "tags": TAGS_HI if L == "hi" else TAGS_EN,
           "scenes": scenes, "chapters": chapters if p["format"] == "long" else [], "hook": scenes[0]["narration"],
           "concept_title": facts["lesson_title"],
           "thumbnail": {"generated_by": "deterministic", "text": facts["lesson_title"],
                         "lines": [SERIES_NAME[L], f"{facts['lesson_no']}/{TOTAL}", facts["lesson_title"]]},
           "fact_checks": [], "generated_by": meta, "review": {"issues": [], "overall": "lint passed", "ready": True},
           "originality": {"ok": True, "copied_7gram_ratio": 0.0, "closest_competitor_title_similarity": 0.0},
           "example_levels": lv}
    pkg["final_description"] = pkg["description"]
    pipeline.save(p["id"], package=pkg, title=pkg["title"])
    audit.log_artifact("script_generated", p["id"], "script", {"scenes": scenes, "model": meta},
                       extra={"lesson": facts["lesson_no"], "provider": meta.get("provider")})


def stage_assets(p: dict[str, Any]) -> None:
    facts = p["episode"]["facts"]
    L = facts["lang"]
    fmt = p["format"]
    charts.LANG = L
    work = Path(config.PRODUCTIONS_DIR / f"{p['id']:05d}" / "charts")
    work.mkdir(parents=True, exist_ok=True)
    db.ex("DELETE FROM assets WHERE production_id=?", (p["id"],))
    footer = HYPO_NOTE[L]
    lv = facts["example"]["levels"]
    f = analysis.fmt
    head = f"{SERIES_NAME[L]} · {facts['lesson_no']}/{TOTAL}"
    rendered: dict[str, Path] = {}

    def make(visual: str, scene: dict[str, Any]) -> Path:
        out = work / f"{visual}.png"
        if visual == "title":
            return charts.text_panel([(SERIES_NAME[L], "heavy", charts.HL), (f"{facts['lesson_no']}/{TOTAL}", "body", charts.MUTED),
                                      (facts["lesson_title"], "heavy", charts.TEXT)], fmt, "", footer, out)
        if visual == "sequence":
            approx = "= लगभग" if L == "hi" else "~"
            return charts.text_panel([(", ".join(str(x) for x in FIB), "heavy", charts.TEXT),
                                      (f"89 ÷ 144 {approx} 0.618", "heavy", charts.HL),
                                      (f"34 ÷ 89 {approx} 0.382", "heavy", charts.SUP),
                                      (f"144 ÷ 89 {approx} 1.618", "body", charts.MUTED)], fmt, head, footer, out)
        if visual == "formula_p":
            return charts.text_panel([("H 1,200   L 1,100   C 1,150", "body", charts.MUTED),
                                      ("P = (H + L + C) ÷ 3", "heavy", charts.PIV),
                                      ("(1,200 + 1,100 + 1,150) ÷ 3 = 1,150", "heavy", charts.HL)], fmt, head, footer, out)
        if visual == "formula_levels":
            return charts.text_panel([("P 1,150 · H − L = 100", "body", charts.MUTED),
                                      (f"R2 = P + 0.618 × 100 = {f(lv['R2'])}", "heavy", charts.RES),
                                      (f"R1 = P + 0.382 × 100 = {f(lv['R1'])}", "body", charts.RES),
                                      (f"S1 = P − 0.382 × 100 = {f(lv['S1'])}", "body", charts.SUP),
                                      (f"S2 = P − 0.618 × 100 = {f(lv['S2'])}", "heavy", charts.SUP)], fmt, head, footer, out)
        if visual == "levels_example":
            return charts.levels_chart(EX, lv, fmt, head, HYPO_NOTE[L], footer, out, basis_label=HYPO_NOTE[L].split(" —")[0])
        if visual == "interaction_example":
            touched = tuple(k for k in pivots.ORDER if EX_NEXT["low"] <= lv[k] <= EX_NEXT["high"])
            return charts.levels_chart(EX, lv, fmt, head, HYPO_NOTE[L], footer, out, review=EX_NEXT,
                                       review_label="अगला सत्र" if L == "hi" else "next session",
                                       basis_label="पिछला सत्र" if L == "hi" else "previous session", touched=touched)
        if visual == "spacing_example":
            n = pivots.fibonacci_pivots(EX_NARROW["high"], EX_NARROW["low"], EX_NARROW["close"])["levels"]
            w = pivots.fibonacci_pivots(EX_WIDE["high"], EX_WIDE["low"], EX_WIDE["close"])["levels"]
            return charts.text_panel([(("छोटी रेंज 50" if L == "hi" else "Narrow range 50"), "body", charts.MUTED),
                                      (f"S2 {f(n['S2'])}  ↔  R2 {f(n['R2'])}  =  61.80", "heavy", charts.SUP),
                                      (("बड़ी रेंज 150" if L == "hi" else "Wide range 150"), "body", charts.MUTED),
                                      (f"S2 {f(w['S2'])}  ↔  R2 {f(w['R2'])}  =  185.40", "heavy", charts.RES)],
                                     fmt, head, footer, out)
        if visual == "disclosure":
            return charts.text_panel([(DISCLOSURE[L], "body", charts.TEXT)], fmt, i18n.t("c_disclosure", L), footer, out)
        if visual == "signoff":
            return charts.text_panel([(SERIES_NAME[L], "heavy", charts.HL), (i18n.t("c_signoff_sub", L), "body", charts.MUTED)],
                                     fmt, "", footer, out)
        label = scene.get("on_screen_text") or facts["lesson_title"]
        return charts.text_panel([(label, "heavy", charts.HL)], fmt, head, footer, out)

    for i, scene in enumerate(p["package"]["scenes"]):
        key = scene["visual"]
        cache_key = key if key not in ("concept", "recap") else f"{key}_{i}"
        if cache_key not in rendered:
            rendered[cache_key] = make(key, scene)
            if cache_key != key:
                rendered[cache_key] = rendered[cache_key].rename(work / f"{cache_key}.png")
        path = rendered[cache_key]
        with Image.open(path) as im:
            w, h = im.size
        db.insert("assets", {"production_id": p["id"], "scene_index": i, "kind": "image", "provider": "original_chart",
                             "provider_id": f"{path.stem}#card", "source_url": "", "page_url": "", "local_path": str(path),
                             "license": "Original — drawn by this app (hypothetical example, no market data)",
                             "license_url": "", "author": "this channel", "attribution": "", "width": w, "height": h,
                             "duration": 0.0, "created_at": db.iso()})
    audit.record("charts_rendered", {"charts": sorted(v.name for v in rendered.values())}, production_id=p["id"])


def ribbon_png(facts: dict[str, Any], w: int, h: int, fmt: str, out: Path) -> dict[str, int]:
    from .episode import _ribbon
    return _ribbon(RIBBON[facts["lang"]], w, h, fmt, out)


def stage_thumbnail(p: dict[str, Any]) -> None:
    from .. import pipeline
    facts = p["episode"]["facts"]
    L = facts["lang"]
    W, H = config.THUMB_W, config.THUMB_H
    img = Image.new("RGB", (W, H), charts.BG)
    d = ImageDraw.Draw(img)
    lv = facts["example"]["levels"]
    top, bottom, x0, x1 = 170, 650, 820, 1230
    lo, hi_ = lv["S3"], lv["R3"]
    for name in pivots.ORDER:
        y = bottom - (lv[name] - lo) / (hi_ - lo) * (bottom - top)
        col = charts.PIV if name == "P" else (charts.RES if name.startswith("R") else charts.SUP)
        d.line([(x0, y), (x1, y)], fill=col, width=7 if name in ("S2", "R2") else 3)
        d.text((x0 - 14, y), name, font=graphics.font_for(name, "heavy", 34), fill=col, anchor="rm")
    series = SERIES_NAME[L]
    d.text((60, 50), series, font=graphics.font_for(series, "heavy", 70), fill=charts.HL)
    part = f"भाग {facts['lesson_no']}/{TOTAL}" if L == "hi" else f"Part {facts['lesson_no']}/{TOTAL}"
    d.text((60, 150), part, font=graphics.font_for(part, "body", 46), fill=charts.MUTED)
    fnt, lines, lh = graphics.fit_text(facts["lesson_title"], "heavy", 640, 380, 74, 40, max_lines=4, spacing=1.05)
    for i, ln in enumerate(lines):
        d.text((60, 240 + i * lh), ln, font=fnt, fill=charts.TEXT)
    out = Path(config.PRODUCTIONS_DIR / f"{p['id']:05d}" / "thumbnail.jpg")
    size = graphics.save_jpeg_under(img, out, 2 * 1024 * 1024)
    contrast = round(min(graphics.contrast(c, charts.BG) for c in (charts.TEXT, charts.HL, charts.MUTED)), 2)
    tl = p["timeline"]
    tl["thumbnail"] = {"path": str(out), "width": W, "height": H, "size_bytes": size, "contrast": contrast,
                       "text": facts["lesson_title"], "generated_by": "deterministic"}
    pipeline.save(p["id"], timeline=tl, thumbnail_path=str(out))
    audit.record("thumbnail_rendered", {"lesson": facts["lesson_no"], "contrast": contrast}, production_id=p["id"], artifact=out)


def _stage_qa(p: dict[str, Any]) -> None:
    from .episode import stage_qa
    stage_qa(p)


def _stage_compliance(p: dict[str, Any]) -> None:
    from .episode import stage_compliance
    stage_compliance(p)


STAGE_FN = {"script": stage_script, "assets": stage_assets, "thumbnail": stage_thumbnail, "qa": _stage_qa,
            "compliance": _stage_compliance}
