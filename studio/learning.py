"""Content gets better every day: each channel learns from its own results.

Once a day (and on demand) every channel is reviewed:
  * audience results — views per day since publishing, watch time, average % viewed and likes of its published
    videos (needs at least 3 videos that have been public for 2+ days; smaller samples are not trusted);
  * our own quality signals — how well the pictures matched their scenes, how often a scene fell back to a text
    card, quality-check warnings and failures.
A free AI model turns those numbers into at most 5 short, concrete lessons, each citing the videos it is based
on. Active lessons are added to the next scripts, story screenplays and idea generation for that channel.
The owner can switch any lesson off; a switched-off lesson is never proposed again.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import db, logs

log = logging.getLogger(__name__)
MIN_VIDEOS, MIN_AGE_DAYS, MAX_LESSONS = 3, 2.0, 5


class Lesson(BaseModel):
    lesson: str = Field(..., min_length=10, max_length=220,
                        description="one concrete instruction for future videos, e.g. 'Open with the payoff in the "
                                    "first 3 seconds — #69 (62% viewed) vs #68 (31%)'")
    kind: Literal["audience", "quality"]
    evidence: str = Field(..., max_length=300, description="the video numbers (#id) and figures it is based on")
    confidence: Literal["low", "medium", "high"]


class Lessons(BaseModel):
    lessons: list[Lesson] = Field(..., max_length=MAX_LESSONS)


def _age_days(iso: str | None) -> float:
    d = db.parse_iso(iso) if iso else None
    return (datetime.now(timezone.utc) - d).total_seconds() / 86400 if d else 0.0


def results(channel_id: int) -> list[dict[str, Any]]:
    """Published videos of a channel with their latest numbers and the features that might explain them."""
    rows = db.q("SELECT p.id, p.title, p.format, p.kind, p.language, p.package, p.timeline, p.request, p.publish_at, "
                "p.updated_at, a.metrics FROM productions p JOIN analytics a ON a.production_id=p.id WHERE "
                "p.channel_id=? AND p.status='uploaded' AND a.id IN (SELECT MAX(id) FROM analytics GROUP BY video_id) "
                "ORDER BY p.id DESC LIMIT 60", (channel_id,))
    out = []
    for r in rows:
        m = db.loads(r["metrics"], {}) or {}
        if m.get("privacy") in ("private",):
            continue                                          # not public yet: no audience signal
        age = max(_age_days(r["publish_at"] or r["updated_at"]), 0.01)
        pkg = db.loads(r["package"], {}) or {}
        tl = db.loads(r["timeline"], {}) or {}
        plan = db.loads(r["request"], {}) or {}
        scenes = pkg.get("scenes") or []
        first = (scenes[0].get("narration") or scenes[0].get("text") or "") if scenes else ""
        if not first and scenes and scenes[0].get("lines"):
            first = scenes[0]["lines"][0].get("text", "")
        out.append({"id": r["id"], "title": r["title"], "format": r["format"], "kind": r["kind"],
                    "type": plan.get("content_type") or r["kind"], "age_days": round(age, 1),
                    "views": int(m.get("views") or 0), "views_per_day": round(int(m.get("views") or 0) / age, 1),
                    "avg_viewed_pct": m.get("averageViewPercentage"), "minutes_watched": m.get("estimatedMinutesWatched"),
                    "likes": int(m.get("likes") or 0), "comments": int(m.get("comments") or 0),
                    "length_s": round(float(tl.get("total_duration") or 0)), "title_chars": len(r["title"] or ""),
                    "hook": first[:140], "publish_hour_utc": (r["publish_at"] or "")[11:13] or None})
    return out


def quality(channel_id: int, days: int = 14) -> dict[str, Any]:
    """Our own production signals for recent videos of this channel."""
    since = db.iso_in(-days * 86400)
    prods = [p for p in db.q("SELECT id, qa, status, request FROM productions WHERE channel_id=? AND created_at>=? "
                             "AND kind IN ('general','story','edit')", (channel_id, since))
             if not (db.loads(p["request"], {}) or {}).get("no_upload")]      # stand-alone videos aren't the channel's
    ids = [p["id"] for p in prods]
    scores, cards, stock = [], 0, 0
    if ids:
        marks = ",".join("?" for _ in ids)
        for a in db.q(f"SELECT provider, provider_id, relevance FROM assets WHERE production_id IN ({marks})", tuple(ids)):
            rel = db.loads(a.get("relevance"), {}) or {}
            if rel.get("score") is not None:
                scores.append(rel["score"])
            if a["provider"] == "procedural":
                cards += 1
            else:
                stock += 1
    warns: dict[str, int] = {}
    for p in prods:
        for c in (db.loads(p["qa"], {}) or {}).get("checks", []):
            if not c.get("ok"):
                warns[c["name"]] = warns.get(c["name"], 0) + 1
    return {"videos": len(prods), "failed": sum(1 for p in prods if p["status"] in ("failed", "needs_attention")),
            "visual_score_avg": round(sum(scores) / len(scores), 1) if scores else None,
            "card_share": round(cards / (cards + stock), 2) if cards + stock else None, "qa_problems": warns}


def review(channel_id: int) -> dict[str, Any]:
    """Re-learn this channel's lessons from its latest numbers. Returns what was learned."""
    from . import ai, channels
    from .ai import prompts
    ch = channels.get(channel_id)
    if not ch:
        return {"skipped": "channel not found"}
    res = [r for r in results(channel_id) if r["age_days"] >= MIN_AGE_DAYS]
    q = quality(channel_id)
    enough = len(res) >= MIN_VIDEOS
    if not enough and not q["videos"]:
        return {"skipped": f"not enough data yet ({len(res)} public video(s) older than 2 days, no recent videos)"}
    off = [r["lesson"] for r in db.q("SELECT lesson FROM lessons WHERE channel_id=? AND status='off'", (channel_id,))]
    table = "\n".join(f"#{r['id']} [{r['format']}/{r['type']}] '{r['title'][:70]}' — {r['views']} views in "
                      f"{r['age_days']} days ({r['views_per_day']}/day), avg viewed {r['avg_viewed_pct']}%, "
                      f"{r['likes']} likes, {r['comments']} comments, {r['length_s']}s, published "
                      f"{r['publish_hour_utc']}:00 UTC, opens with: \"{r['hook']}\"" for r in res[:40])
    prompt = (f"CHANNEL: {ch['name']} — niche: {ch.get('niche') or '-'}; audience: {ch.get('audience') or '-'}; "
              f"language: {ch.get('language')}\n\n"
              + (f"PUBLISHED VIDEOS AND THEIR REAL RESULTS (YouTube Analytics):\n{table}\n\n" if enough else
                 f"AUDIENCE DATA: only {len(res)} public video(s) older than 2 days — too few to judge what viewers "
                 "prefer. Give NO audience lessons.\n\n")
              + f"OUR OWN PRODUCTION QUALITY (last 14 days): {q}\n"
              "(visual_score_avg = how well pictures matched their scenes, 0-10, from a vision check; card_share = "
              "share of scenes that fell back to a text card because no fitting picture was found)\n\n"
              + (f"LESSONS THE OWNER SWITCHED OFF (never propose these or close variants): {off}\n\n" if off else "")
              + f"Write at most {MAX_LESSONS} lessons that will make this channel's NEXT videos better. Each must be a "
              "concrete instruction a scriptwriter or editor can follow (hook, title, length, pacing, topic choice, "
              "visual searches, format), based ONLY on the numbers above, citing the video numbers. Compare better "
              "and worse videos; with small samples say confidence 'low'. Don't invent numbers. No generic advice.")
    try:
        out, meta = ai.generate("learning", prompts.system("a YouTube growth analyst"), prompt, Lessons, max_tokens=2500)
    except Exception as e:  # no AI available today: keep yesterday's lessons
        logs.event("warning", "learning", f"'{ch['name']}': review skipped — {e}")
        return {"skipped": str(e)[:200]}
    lessons = [x for x in out.lessons if enough or x.kind == "quality"]
    with db.tx():
        db.ex("UPDATE lessons SET status='old', updated_at=? WHERE channel_id=? AND status='active'",
              (db.iso(), channel_id))
        for x in lessons:
            if any(x.lesson.strip().lower() == o.strip().lower() for o in off):
                continue
            db.insert("lessons", {"channel_id": channel_id, "workspace_id": ch["workspace_id"], "kind": x.kind,
                                  "lesson": x.lesson.strip(), "evidence": x.evidence.strip(), "confidence": x.confidence,
                                  "status": "active", "videos_used": len(res), "created_at": db.iso(),
                                  "updated_at": db.iso()})
    logs.event("info", "learning", f"'{ch['name']}': {len(lessons)} lesson(s) from {len(res)} video result(s) "
               f"(by {meta['provider']})")
    return {"lessons": [x.lesson for x in lessons], "videos": len(res), "quality": q}


def review_all() -> dict[str, Any]:
    out = {}
    for ch in db.q("SELECT id FROM channels WHERE status='active'"):
        out[ch["id"]] = review(ch["id"])
    return out


def active(channel_id: int | None) -> list[dict[str, Any]]:
    if not channel_id:
        return []
    return db.q("SELECT * FROM lessons WHERE channel_id=? AND status='active' ORDER BY kind, id", (channel_id,))


def prompt_block(channel_id: int | None) -> str:
    """What this channel has learned, for script, screenplay and idea prompts ('' when nothing yet)."""
    rows = active(channel_id)
    if not rows:
        return ""
    return ("WHAT WORKS ON THIS CHANNEL (learned from its own results — follow these unless the request says "
            "otherwise):\n" + "\n".join(f"- {r['lesson']}" for r in rows) + "\n")


# ------------------------------------------------------------------ weekly decision report (no AI, rule-based)

def _decide(r: dict[str, Any], median_vpd: float) -> tuple[str, str]:
    """One decision per video from its real numbers. CTR/impressions aren't collected, so 'repackage' is judged
    from views vs how much of the video viewers watch — stated as a hypothesis, never as a measured CTR problem."""
    if r["age_days"] < MIN_AGE_DAYS:
        return "too early", f"published {r['age_days']} days ago — wait for at least {MIN_AGE_DAYS} days of data"
    pct = r.get("avg_viewed_pct")
    if pct is None:
        return "no data", "YouTube Analytics hasn't returned watch-time numbers for this video yet"
    hot = median_vpd > 0 and r["views_per_day"] >= 1.5 * median_vpd
    cold = median_vpd > 0 and r["views_per_day"] <= 0.5 * median_vpd
    if hot and pct >= 40:
        return "continue", "more views than usual and viewers stay — make more on this topic/format"
    if pct < 25:
        return "improve", f"viewers watch only {pct:.0f}% on average — tighten the opening and remove slow parts"
    if cold and pct >= 40:
        return "repackage", ("few views but people who start it watch well — test a clearer title or thumbnail "
                             "(hypothesis: impressions/CTR are not collected)")
    if cold:
        return "test", "few views and average watching — try a different angle before making more like it"
    return "continue", "in line with the channel's usual results"


def weekly_report(channel_id: int, save_it: bool = True) -> dict[str, Any]:
    """This week's decision for every public video of a channel (continue / improve / repackage / test / too early),
    plus what to make next. Stored per ISO week, so past weeks can be compared."""
    from datetime import date
    from . import channels
    ch = channels.get(channel_id)
    if not ch:
        return {"skipped": "channel not found"}
    res = results(channel_id)
    vpds = sorted(r["views_per_day"] for r in res if r["age_days"] >= MIN_AGE_DAYS)
    median = vpds[len(vpds) // 2] if vpds else 0.0
    rows = []
    for r in res[:30]:
        decision, why = _decide(r, median)
        rows.append({"id": r["id"], "title": r["title"], "format": r["format"], "views": r["views"],
                     "views_per_day": r["views_per_day"], "avg_viewed_pct": r["avg_viewed_pct"],
                     "decision": decision, "why": why})
    counts: dict[str, int] = {}
    for x in rows:
        counts[x["decision"]] = counts.get(x["decision"], 0) + 1
    best = [x for x in rows if x["decision"] == "continue" and "more views" in x["why"]]
    nxt = ([f"Make a follow-up to #{best[0]['id']} '{best[0]['title'][:60]}' (it beat the channel's usual views)"]
           if best else []) + \
        (["Rework openings: several videos lose most viewers early"] if counts.get("improve", 0) >= 2 else []) + \
        (["Test new titles/thumbnails on the 'repackage' videos one at a time (change one thing per test)"]
         if counts.get("repackage") else []) + \
        (["Too little data to judge — keep the schedule steady and review next week"] if len(vpds) < MIN_VIDEOS else [])
    week = f"{date.today().isocalendar()[0]}-W{date.today().isocalendar()[1]:02d}"
    report = {"channel": ch["name"], "week": week, "videos": rows, "counts": counts, "median_views_per_day": median,
              "next": nxt, "not_collected": ["impressions", "click-through rate", "traffic sources",
                                             "returning viewers"],
              "made_at": db.iso()}
    if save_it:
        db.ex("INSERT INTO channel_reviews (channel_id, week, report, created_at) VALUES (?,?,?,?) "
              "ON CONFLICT(channel_id, week) DO UPDATE SET report=excluded.report, created_at=excluded.created_at",
              (channel_id, week, db.dumps(report), db.iso()))
    return report


def weekly_all() -> dict[str, Any]:
    from . import channels
    done = {}
    for ch in channels.all_channels():
        try:
            r = weekly_report(ch["id"])
            done[ch["id"]] = r.get("counts", r.get("skipped"))
        except Exception as e:  # one channel's problem never stops the others
            done[ch["id"]] = f"failed: {str(e)[:120]}"
    logs.event("info", "learning", f"Weekly channel review written for {len(done)} channel(s)")
    return done


def latest_weekly(channel_id: int) -> dict[str, Any] | None:
    r = db.q1("SELECT report FROM channel_reviews WHERE channel_id=? ORDER BY week DESC LIMIT 1", (channel_id,))
    return db.loads(r["report"], None) if r else None
