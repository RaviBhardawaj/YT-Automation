"""Vertical Shorts cut from a finished long NIFTY episode.

A Short is a self-contained segment of the long video's script — whole scenes, never a random
time window — re-laid out for 9:16 rather than cropped, so no price label is cut off. It keeps
the parent's fact sheet (same dataset, same levels), so every number matches the full episode.
Around the segment the app adds, deterministically: a truthful hook, a pointer to the full
episode and a short spoken disclosure. Each Short is its own production with its own
compliance gate, approval and upload record, linked to the parent.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PIL import Image

from .. import config, db, logs, settings
from . import audit, episode, i18n

STAGES = ["voice", "assets", "render", "thumbnail", "qa", "compliance"]
STATUS = {"voice": "voicing", "assets": "drawing charts", "render": "rendering", "thumbnail": "making cover",
          "qa": "quality check", "compliance": "compliance gate"}
MIN_WORDS, MAX_WORDS = 40, 140
MAX_SEGMENT_SECONDS = 34.0        # + hook, pointer, short disclosure and sign-off (≈ 22-25 s) ≤ 60 s

# Segment types in priority order: (purpose in the long script, eligibility test)
SEGMENTS = [
    ("next_levels", lambda f: bool(f.get("next"))),
    ("review", lambda f: bool(f.get("review")) and bool(f.get("touched"))),
    ("intraday_15m", lambda f: any(b["state"] in ("failed_breakout", "closed_beyond", "tested_held")
                                   for k, b in (((f.get("intraday") or {}).get("analysis") or {}).get("m15", {})
                                                .get("levels", {})).items() if k in ("S2", "R2", "P"))),
    ("calculation", lambda f: True),
    ("levels", lambda f: True),
    ("intraday_1h", lambda f: bool(((f.get("intraday") or {}).get("analysis") or {}).get("comparison"))),
    ("mtf", lambda f: bool(f.get("mtf"))),
]

KEY_OBSERVATIONS = {"next_levels", "next_position", "s2_review", "r2_review", "s2_15m", "r2_15m", "p_15m", "tf_compare", "levels_touched"}

TEXT = {
    "hook_review": {"en": "Did NIFTY 50 actually reach its Fibonacci pivot levels on {rdate}? Here is what the verified data shows.",
                    "hi": "क्या {rdate} को NIFTY 50 अपने पिवट लेवल्स तक सच में पहुँचा? चलिए डेटा से देखते हैं।"},
    "hook_intraday_15m": {"en": "What happened around S2 and R2 on the 15-minute chart of {rdate}? Bar by bar, from verified data.",
                          "hi": "{rdate} के 15-मिनट चार्ट पर S2 और R2 के पास क्या हुआ? कैंडल बाय कैंडल देखिए।"},
    "hook_calculation": {"en": "Where do the NIFTY 50 Fibonacci pivot levels from the {basis} session come from? Here is the calculation.",
                         "hi": "{basis} के NIFTY 50 फिबोनाची पिवट लेवल्स आते कहाँ से हैं? पूरी कैलकुलेशन एक मिनट में।"},
    "hook_levels": {"en": "What do S2 and R2 actually mean on a NIFTY 50 chart? A quick explanation with the {basis} levels.",
                    "hi": "NIFTY 50 चार्ट पर S2 और R2 का असली मतलब क्या है? {basis} के लेवल्स के साथ समझिए।"},
    "hook_intraday_1h": {"en": "Do the 15-minute and 1-hour charts of {rdate} tell the same story? Here is the comparison.",
                         "hi": "क्या {rdate} के 15-मिनट और 1-घंटे के चार्ट एक ही कहानी बताते हैं? चलिए कंपेयर करते हैं।"},
    "hook_mtf": {"en": "Daily, 15-minute and 1-hour Fibonacci levels from the {basis} close: how do they differ?",
                 "hi": "{basis} के क्लोज़ से डेली, 15-मिनट और 1-घंटे के फिबोनाची लेवल्स — इनमें फ़र्क क्या है?"},
    "zones": {"en": "The main zones for {ndate}: S2 at {s2}, the pivot at {p} and R2 at {r2}. The {rdate} close of {close} "
                    "is {d} points {side} that pivot.",
              "hi": "{ndate} के मेन ज़ोन: S2 {s2}, पिवट {p} और R2 {r2}। {rdate} का क्लोज़ {close} है — पिवट से {d} पॉइंट्स {side}।"},
    "hook_next_levels": {"en":"NIFTY 50 Fibonacci reference levels for {ndate}, calculated from the {rdate} session.",
                         "hi": "{ndate} के NIFTY 50 फिबोनाची लेवल्स — {rdate} के सेशन से निकले हुए। ध्यान से देखिए।"},
    "title_next_levels": {"en": "NIFTY 50 Levels for {ndate} | Fibonacci Pivots #Shorts",
                          "hi": "{ndate} के NIFTY 50 लेवल्स | फिबोनाची पिवट #Shorts"},
    "title_review": {"en": "Did NIFTY 50 Reach S2 or R2? | {rdate} #Shorts", "hi": "क्या NIFTY 50 S2 या R2 तक पहुँचा? | {rdate} #Shorts"},
    "title_intraday_15m": {"en": "NIFTY 50 15-Minute Chart at Pivot Levels | {rdate} #Shorts",
                           "hi": "पिवट लेवल्स पर NIFTY 50 का 15-मिनट चार्ट | {rdate} #Shorts"},
    "title_calculation": {"en": "How Fibonacci Pivot Levels Are Calculated | NIFTY 50 {basis} #Shorts",
                          "hi": "फिबोनाची पिवट लेवल कैसे निकालते हैं | NIFTY 50 {basis} #Shorts"},
    "title_levels": {"en": "What S2 and R2 Mean | NIFTY 50 {basis} #Shorts", "hi": "S2 और R2 का मतलब | NIFTY 50 {basis} #Shorts"},
    "title_intraday_1h": {"en": "15-Minute vs 1-Hour: NIFTY 50 on {rdate} #Shorts",
                          "hi": "15-मिनट vs 1-घंटा: {rdate} को NIFTY 50 #Shorts"},
    "title_mtf": {"en": "Daily vs 15-Min vs 1-Hour Fibonacci Levels | NIFTY 50 {basis} #Shorts",
                  "hi": "डेली vs 15-मिनट vs 1-घंटे के फिबोनाची लेवल्स | NIFTY 50 {basis} #Shorts"},
    "pointer": {"en": "The full analysis is on the channel — subscribe for daily updates.",
                "hi": "पूरा एनालिसिस चैनल पर है — डेली अपडेट्स के लिए सब्सक्राइब कर लीजिए।"},
    "disclosure": {"en": "Educational analysis of {date} data. Levels are reference points, not guarantees of future prices. Markets involve risk.",
                   "hi": "सिर्फ़ एजुकेशनल एनालिसिस, {date} के डेटा से। लेवल्स रेफरेंस हैं, गारंटी नहीं। मार्केट में रिस्क है।"},
    "full_episode": {"en": "Full episode", "hi": "फुल एपिसोड"},
    "full_on_channel": {"en": "\"{title}\" on this channel", "hi": "इस चैनल पर \"{title}\""},
    "segment_note": {"en": "This Short is an excerpt ({start}–{end}) of the full episode.",
                     "hi": "ये Short फुल एपिसोड का एक हिस्सा ({start}–{end}) है।"},
    "cta_card": {"en": "Full episode on the channel", "hi": "फुल एपिसोड चैनल पर"},
}


def tx(key: str, L: str, **kw: Any) -> str:
    return TEXT[key][L].format(**kw)


def _mmss(t: float) -> str:
    t = int(round(t))
    return f"{t // 60}:{t % 60:02d}"


# ------------------------------------------------------------------ selection

def candidates(parent: dict[str, Any]) -> list[dict[str, Any]]:
    """Self-contained segments of the long script, located on the rendered timeline."""
    facts = parent["episode"]["facts"]
    scenes = parent["package"]["scenes"]
    timing = (parent.get("timeline") or {}).get("scenes") or []
    if len(timing) != len(scenes):
        raise ValueError("the long video has not been voiced/rendered yet")
    starts = [0.0]
    for t_ in timing:
        starts.append(starts[-1] + t_["duration"])
    out = []
    for purpose, eligible in SEGMENTS:
        idxs = [i for i, s in enumerate(scenes) if s["purpose"] == purpose]
        if not idxs or not eligible(facts):
            continue
        section = [idxs[0]]                               # first contiguous run of that section
        for i in idxs[1:]:
            if i == section[-1] + 1:
                section.append(i)
        # Whole scenes only: the best contiguous window that fits, favouring the key verified observations
        best, best_score = None, None
        for a in range(len(section)):
            for b in range(a + 1, len(section) + 1):
                run = section[a:b]
                words = sum(len(scenes[i]["narration"].split()) for i in run)
                secs = sum(timing[i]["duration"] for i in run)
                if words < MIN_WORDS or words > MAX_WORDS or secs > MAX_SEGMENT_SECONDS:
                    continue
                cited = {o for i in run for o in scenes[i].get("observation_ids", [])}
                score = (sum(3 if o in KEY_OBSERVATIONS else 1 for o in cited), words)
                if best_score is None or score > best_score:
                    best, best_score = run, score
        if not best:
            continue
        run = best
        words = sum(len(scenes[i]["narration"].split()) for i in run)
        out.append({"purpose": purpose, "scenes": run, "words": words,
                    "start": round(starts[run[0]], 2), "end": round(starts[run[-1] + 1], 2),
                    "observations": sorted({o for i in run for o in scenes[i].get("observation_ids", [])})})
    if any(s["purpose"] == "next_levels" for s in scenes) and facts.get("next") and \
            not any(c["purpose"] == "next_levels" for c in out):
        out.insert(0, {"purpose": "next_levels", "scenes": [], "words": 0, "start": None, "end": None,
                       "observations": ["next_levels", "next_position"], "source": "fact_sheet"})
    return out


def _fact_sheet_scene(facts: dict[str, Any], purpose: str) -> dict[str, Any]:
    """A short segment written from verified observations only (used when no long-video scene fits a Short)."""
    L = facts.get("lang", "en")
    nx, f = facts["next"]["levels"], episode.analysis.fmt
    close = facts["review"]["close"]
    side = i18n.t("above" if close > nx["P"] else "below", L)
    # Only the three main zones: long prices read slowly, and a Short has about 40 seconds for its segment
    text = tx("zones", L, ndate=facts["next"]["label"], s2=f(nx["S2"]), p=f(nx["P"]), r2=f(nx["R2"]),
              rdate=facts["review_label"], close=f(close), d=f(abs(round(close - nx["P"], 2))), side=side)
    return {"purpose": purpose, "narration": text, "on_screen_text": "", "visual": "next_levels",
            "observation_ids": ["next_levels", "next_position"]}


def _more(parent: dict[str, Any], purpose: str) -> str:
    L = parent["episode"]["facts"].get("lang", "en")
    names = [c["title"] for c in parent["package"].get("chapters", [])
             if c["title"] not in (i18n.t(f"ch_{purpose}", L), i18n.t("ch_intro", L), i18n.t("ch_recap", L))]
    sep = " और " if L == "hi" else " and "
    names = names[:2]
    return ", ".join(names[:-1]) + (sep + names[-1] if len(names) > 1 else (names[0] if names else ""))


def build(parent: dict[str, Any], seg: dict[str, Any]) -> dict[str, Any]:
    """Deterministic package + episode record for one Short."""
    facts = parent["episode"]["facts"]
    L = facts.get("lang", "en")
    kw = {"basis": facts["basis_label"], "rdate": facts.get("review_label", facts["basis_label"]),
          "ndate": (facts.get("next") or {}).get("label", "")}
    disclosure = tx("disclosure", L, date=facts.get("review_label") if facts["mode"] == "daily_review" else facts["basis_label"])
    src = parent["package"]["scenes"]
    body = [_fact_sheet_scene(facts, seg["purpose"])] if seg.get("source") == "fact_sheet" else \
        [{k: src[i][k] for k in ("purpose", "narration", "on_screen_text", "visual", "observation_ids")} for i in seg["scenes"]]
    visual = body[0]["visual"]
    scenes = [{"purpose": "hook", "narration": tx(f"hook_{seg['purpose']}", L, **kw), "on_screen_text": "",
               "visual": visual, "observation_ids": []}]
    scenes += body
    scenes.append({"purpose": "outro", "narration": tx("pointer", L, more=_more(parent, seg["purpose"]) or kw["basis"]),
                   "on_screen_text": "", "visual": "cta", "observation_ids": []})
    scenes.append({"purpose": "disclosure", "narration": disclosure, "on_screen_text": "", "visual": "disclosure",
                   "observation_ids": []})
    scenes.append(episode.signoff_scene(facts, short=True))
    for s in scenes:
        s.update({"caption_text": s["narration"], "visual_description": f"chart: {s['visual']}", "visual_query": "chart",
                  "visual_type": "text_card"})
    title = tx(f"title_{seg['purpose']}", L, **kw)
    pkg = {"title": title, "description": "", "tags": (episode.TAGS_HI if L == "hi" else episode.TAGS)[:6] + ["Shorts"],
           "scenes": scenes, "chapters": [], "hook": scenes[0]["narration"],
           "thumbnail": {"generated_by": "deterministic", "text": title, "lines": [title]},
           "fact_checks": parent["package"].get("fact_checks", []),
           "review": {"issues": [], "overall": "deterministic segment of a gated episode", "ready": True},
           "originality": {"ok": True, "copied_7gram_ratio": 0.0, "closest_competitor_title_similarity": 0.0}}
    ep = {"facts": facts, "disclosure": disclosure, "full_disclosure": parent["episode"]["disclosure"],
          "source": parent["episode"].get("source"), "expected_title": title,
          "segment": {**seg, "parent_id": parent["id"], "parent_title": parent["package"]["title"]}}
    pkg["description"] = describe(parent, ep, None)
    pkg["final_description"] = pkg["description"]
    return {"package": pkg, "episode": ep}


def describe(parent: dict[str, Any], ep: dict[str, Any], parent_video_id: str | None, voice: dict | None = None) -> str:
    facts = ep["facts"]
    L = facts.get("lang", "en")
    seg = ep["segment"]
    lv = facts["pivots"]["levels"]
    f = episode.analysis.fmt
    link = (f"https://youtu.be/{parent_video_id}" if parent_video_id else
            tx("full_on_channel", L, title=seg["parent_title"]))
    note = tx("segment_note", L, start=_mmss(seg["start"]), end=_mmss(seg["end"])) if seg.get("start") is not None else ""
    parts = [ep["expected_title"].replace(" #Shorts", ""), "",
             f"{tx('full_episode', L)}: {link}", note, "",
             i18n.t("h_data", L),
             i18n.t("d_index", L, nature=i18n.nature(facts["data_nature"], L)),
             i18n.t("d_source", L, source=episode.source_name(facts), retrieved=facts["retrieved_label"]),
             i18n.t("h_levels", L, date=facts["basis_label"]),
             " | ".join(f"{k} {f(lv[k])}" for k in ("R2", "P", "S2")), "",
             i18n.t("h_disclosure", L), ep["full_disclosure"], "",
             i18n.t("credits", L, voice=i18n.voice_credit(voice)), "", i18n.t("hashtags_short", L)]
    return "\n".join(parts).replace("<", "").replace(">", "")


def create_for(parent_id: int, actor: str = "system") -> list[int]:
    """Create the Shorts for a finished long episode (idempotent: existing Shorts are kept)."""
    from .. import pipeline, worker
    parent = pipeline.load(parent_id)
    if parent.get("kind") != "nifty_pivot" or parent["format"] != "long":
        raise ValueError("Shorts are made from long NIFTY pivot episodes")
    if not settings.get("shorts.enabled", True):
        return []
    existing = {db.loads(r["episode"], {}).get("segment", {}).get("purpose")
                for r in db.q("SELECT episode FROM productions WHERE parent_id=? AND status<>'rejected'", (parent_id,))}
    limit = int(settings.get("shorts.per_episode", 4))
    made = []
    segs = candidates(parent)
    for seg in segs:
        if len(existing) + len(made) >= limit:
            break
        if seg["purpose"] in existing:
            continue
        built = build(parent, seg)
        pid = db.insert("productions", {
            "topic_id": None, "format": "short", "title": built["package"]["title"], "status": "queued", "stage": "voice",
            "kind": "nifty_short", "parent_id": parent_id, "audience": parent.get("audience") or "india",
            "channel_id": parent.get("channel_id"), "workspace_id": parent.get("workspace_id") or 1,
            "language": parent.get("language"), "package": db.dumps(built["package"]),
            "episode": db.dumps(built["episode"]), "test_mode": 0, "created_at": db.iso(), "updated_at": db.iso()})
        audit.record("short_created", {"parent_id": parent_id, "segment": seg}, production_id=pid, actor=actor)
        worker.enqueue("production", {"production_id": pid}, production_id=pid, max_attempts=3)
        made.append(pid)
    write_manifest(parent_id)
    skipped = [s for s, _ in SEGMENTS if s not in {c["purpose"] for c in segs}]
    logs.event("info", "shorts", f"{len(made)} Short(s) queued from episode #{parent_id}"
               + (f"; no self-contained verified segment for: {', '.join(skipped)}" if skipped else ""),
               production_id=parent_id)
    return made


# ------------------------------------------------------------------ stages

def stage_assets(p: dict[str, Any]) -> None:
    facts = p["episode"]["facts"]
    work = Path(config.PRODUCTIONS_DIR / f"{p['id']:05d}" / "charts")
    work.mkdir(parents=True, exist_ok=True)
    db.ex("DELETE FROM assets WHERE production_id=?", (p["id"],))
    L = facts.get("lang", "en")
    drawn: dict[str, tuple[Path, str]] = {}
    for i, scene in enumerate(p["package"]["scenes"]):
        key = scene["visual"]
        if key == "cta":
            if key not in drawn:
                out = work / "cta.png"
                from . import charts
                drawn[key] = (charts.text_panel([(tx("cta_card", L), "heavy", charts.HL),
                                                 (p["episode"]["segment"]["parent_title"], "body", charts.TEXT)],
                                                "short", "", episode.chart_footer(facts), out), "image")
        elif key not in drawn or key == "concept":
            drawn[key] = episode.draw_visual(facts, key, scene, "short", work, "", p["episode"]["disclosure"])
        path, kind = drawn[key]
        if i == 0 and kind == "anim":
            kind = "image"            # the hook shows the finished chart at once; the segment replays the build-up
        with Image.open(path) as im:
            w, h = im.size
        db.insert("assets", {"production_id": p["id"], "scene_index": i, "kind": kind, "provider": "original_chart",
                             "provider_id": f"{path.stem}#card", "source_url": "", "page_url": "", "local_path": str(path),
                             "license": f"Original — drawn by this app from dataset #{facts['dataset_id']}",
                             "license_url": "", "author": "this channel", "attribution": "", "width": w, "height": h,
                             "duration": 0.0, "created_at": db.iso()})
    audit.record("short_charts_rendered", {"charts": sorted(v[0].name for v in drawn.values())}, production_id=p["id"])


def stage_cover(p: dict[str, Any]) -> None:
    """Shorts show a frame of the video; keep a 9:16 cover image for the dashboard, no custom thumbnail upload."""
    from .. import pipeline
    first = db.q1("SELECT local_path FROM assets WHERE production_id=? ORDER BY scene_index LIMIT 1", (p["id"],))
    out = Path(config.PRODUCTIONS_DIR / f"{p['id']:05d}" / "cover.jpg")
    with Image.open(first["local_path"]) as im:
        im.convert("RGB").save(out, "JPEG", quality=90)
    tl = p["timeline"]
    tl["thumbnail"] = None
    tl["cover"] = {"path": str(out), "generated_by": "deterministic"}
    pipeline.save(p["id"], timeline=tl, thumbnail_path=None)


def stage_compliance(p: dict[str, Any]) -> None:
    episode.stage_compliance(p)
    write_manifest(p["episode"]["segment"]["parent_id"])


STAGE_FN = {"assets": stage_assets, "thumbnail": stage_cover, "qa": episode.stage_qa, "compliance": stage_compliance}


# ------------------------------------------------------------------ manifest

def manifest(parent_id: int) -> dict[str, Any]:
    from .. import pipeline
    parent = pipeline.load(parent_id)
    rows = []
    for r in db.q("SELECT * FROM productions WHERE parent_id=? ORDER BY id", (parent_id,)):
        ep = db.loads(r["episode"], {}) or {}
        pkg = db.loads(r["package"], {}) or {}
        gate_ = db.loads(r["compliance"], {}) or {}
        qa_ = db.loads(r["qa"], {}) or {}
        seg = ep.get("segment", {})
        rows.append({"short_id": r["id"], "segment": seg.get("purpose"), "source_scenes": seg.get("scenes"),
                     "source_start": seg.get("start"), "source_end": seg.get("end"), "title": pkg.get("title"),
                     "description": pkg.get("final_description") or pkg.get("description"), "output": r["video_path"],
                     "validation": {"qa_passed": qa_.get("passed"), "gate_status": gate_.get("status")},
                     "status": r["status"], "youtube_video_id": r["youtube_video_id"], "privacy": r["privacy"]})
    return {"episode_id": parent_id, "episode_title": parent["package"].get("title"),
            "episode_video": parent.get("video_path"), "episode_youtube_id": parent.get("youtube_video_id"),
            "generated_at": db.iso(), "shorts": rows}


def write_manifest(parent_id: int) -> Path:
    m = manifest(parent_id)
    out = Path(config.PRODUCTIONS_DIR / f"{parent_id:05d}" / "shorts_manifest.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
