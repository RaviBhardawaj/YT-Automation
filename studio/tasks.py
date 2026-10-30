"""Task handlers wired into the queue, plus the daily planner."""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from . import db, logs, pipeline, profile, settings, worker
from .media import tts
from .research import engine
from .youtube import analytics

ACTIVE = ("queued", "researching", "scripting", "reviewing", "sourcing visuals", "voicing", "rendering",
          "making thumbnail", "quality check")


@worker.handler("niche_research")
def _niche(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    return engine.run_niche_research(payload)


@worker.handler("ideation")
def _ideation(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    return engine.run_ideation(payload)


@worker.handler("whisper_download")
def _whisper_download(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    from . import system_setup
    return system_setup.install_whisper()


@worker.handler("local_flux_setup")
def _local_flux_setup(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    from . import system_setup
    return system_setup.install_local_flux()


@worker.handler("voiceclone_setup")
def _voiceclone_setup(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    from . import system_setup
    return system_setup.install_voiceclone()


@worker.handler("backup")
def _backup(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    from . import backup
    res = backup.make("auto")
    if datetime.now().weekday() == 6:                 # Sundays: prove the newest copy can be restored
        res["drill"] = backup.restore_drill()
    return {k: res[k] for k in ("file", "bytes", "removed", "drill") if k in res}


@worker.handler("comments_daily")
def _comments_daily(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    from .youtube import comments
    return comments.refresh_recent()


@worker.handler("weekly_review")
def _weekly_review(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    from . import learning
    return learning.weekly_all()


@worker.handler("learning_review")
def _learning_review(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    from . import learning
    if payload.get("channel_id"):
        return learning.review(int(payload["channel_id"]))
    return learning.review_all()


@worker.handler("film_ref")
def _film_ref(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    """One new version of a character or location picture (Cast review → Regenerate)."""
    from .creative import film
    from .film_studio import casting
    r = casting.generate(int(payload["ref_id"]), int(payload["workspace_id"]),
                         film.workdir(int(payload["production_id"])) / "cast", film._gen)
    return {"ref": r["id"], "file": r["file_path"]}


@worker.handler("film_short_make")
def _film_short_make(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    from . import pipeline
    from .film_studio import shorts
    return {"short": shorts.make(pipeline.load(int(payload["production_id"])), int(payload["index"]))}


@worker.handler("film_shots_run")
def _film_shots_run(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    """Regenerate / upgrade single shots of a finished film, then re-edit and re-check it."""
    from . import pipeline
    pid = int(payload["production_id"])
    pipeline.save(pid, stage="shots", status="queued", error=None)
    return pipeline.run_production({"production_id": pid}, task)


@worker.handler("voice_preview")
def _voice_preview(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    from .media import voiceclone
    v = voiceclone.get(payload["voice_id"])
    if not v:
        return {"skipped": "voice deleted"}
    return {"preview": str(voiceclone.preview(v))}


@worker.handler("production")
def _production(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    res = pipeline.run_production(payload, task)
    from .social import publisher                  # finished video: Facebook/Instagram if the channel is automatic
    try:
        publisher.maybe_auto(int(payload["production_id"]))
    except Exception:
        logs.event("warning", "social", "Couldn't queue automatic Facebook/Instagram publishing",
                   production_id=int(payload["production_id"]))
    return res


@worker.handler("social_upload")
def _social_upload(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    from .social import publisher
    return publisher.run(int(payload["production_id"]), payload["platform"])


@worker.handler("upload")
def _upload(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    return pipeline.run_upload(payload, task)


@worker.handler("analytics_sync")
def _analytics(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    return analytics.sync()


@worker.handler("kokoro_download")
def _kokoro(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    tts.kokoro_download(payload.get("variant", "int8"))
    logs.event("info", "voice", f"Kokoro model ({payload.get('variant', 'int8')}) installed")
    return {"ok": True}


@worker.handler("ollama_pull")
def _ollama_pull(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    import httpx
    host = settings.get("ai.ollama_host").rstrip("/")
    r = httpx.post(host + "/api/pull", json={"model": payload["model"], "stream": False},
                   timeout=httpx.Timeout(7200, connect=10))
    r.raise_for_status()
    logs.event("info", "ai", f"Ollama model pulled: {payload['model']}")
    return r.json()


@worker.handler("piper_download")
def _piper(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    card = tts.piper_download(payload["voice"])
    logs.event("info", "voice", f"Piper voice {payload['voice']} installed (licence: {card['license']})")
    return {"license": card["license"], "commercial": card["commercial"]}


def _available_ideas(fmt: str) -> list[dict[str, Any]]:
    niche = db.q1("SELECT id FROM niches WHERE status='approved' ORDER BY approved_at DESC LIMIT 1")
    if not niche:
        return []
    min_score = float(settings.get("production.min_topic_score", 35))
    approved = db.q("SELECT * FROM topics WHERE niche_id=? AND channel_id IS NOT NULL AND format=? AND status='approved' ORDER BY score DESC",
                    (niche["id"], fmt))
    auto = db.q("SELECT * FROM topics WHERE niche_id=? AND channel_id IS NOT NULL AND format=? AND status='idea' AND score>=? ORDER BY score DESC",
                (niche["id"], fmt, min_score)) if settings.get("production.auto_pick_topics", True) else []
    return approved + auto


@worker.handler("ideation_refill")
def _refill(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    niche = db.q1("SELECT id FROM niches WHERE status='approved' ORDER BY approved_at DESC LIMIT 1")
    if not niche or not profile.complete():
        return {"skipped": "no approved niche"}
    need = [f for f in profile.formats() if len(_available_ideas(f)) < max(4, math.ceil(profile.per_day(f) * 3))]
    if need:
        worker.enqueue("ideation", {"niche_id": niche["id"], "count": 10, "formats": need}, dedupe=True)
    return {"queued_for": need}


@worker.handler("daily_plan")
def _daily_plan(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    if not profile.complete():
        return {"skipped": "interview not completed"}
    if not db.q1("SELECT id FROM niches WHERE status='approved'"):
        return {"skipped": "no approved niche"}
    active = db.q1(f"SELECT COUNT(*) AS n FROM productions WHERE status IN ({','.join('?' for _ in ACTIVE)})", ACTIVE)["n"]
    room = max(0, int(settings.get("production.max_active")) - active)
    created, waiting = [], []
    for fmt in profile.formats():
        per_day = profile.per_day(fmt)
        if per_day <= 0:
            continue
        made_7d = db.q1("SELECT COUNT(*) AS n FROM productions WHERE format=? AND test_mode=0 AND created_at>=?",
                        (fmt, db.iso_in(-7 * 86400)))["n"]
        today = min(math.ceil(per_day), max(0, round(per_day * 7) - made_7d))
        ideas = _available_ideas(fmt)
        for idea in ideas[:min(today, room)]:
            created.append(pipeline.create(idea["id"]))
            room -= 1
        if today > len(ideas):
            waiting.append(fmt)
    if waiting and not payload.get("followup"):
        niche = db.q1("SELECT id FROM niches WHERE status='approved' ORDER BY approved_at DESC LIMIT 1")
        worker.enqueue("ideation", {"niche_id": niche["id"], "count": 10, "formats": waiting}, dedupe=True)
        worker.enqueue("daily_plan", {"followup": True}, delay=45 * 60, dedupe=True)
    logs.event("info", "planner", f"Daily plan: started {len(created)} production(s)"
               + (f"; researching more ideas for {', '.join(waiting)}" if waiting else ""))
    return {"created": created, "waiting_for_ideas": waiting}


@worker.handler("channel_ideas")
def _channel_ideas(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    """Ideas for one studio channel: story concepts for creative channels, evidence-backed topics for factual ones."""
    from . import channels, jobs
    ch = channels.get(int(payload["channel_id"]))
    if not ch:
        return {"skipped": "channel deleted"}
    count = max(3, min(20, int(payload.get("count", 10))))
    fmts = [f for f in payload.get("formats") or [] if f in ("long", "short")] or channels.formats(ch)
    creative = ch["workflow"] == "animated_story" or (
        ch["workflow"] == "auto" and jobs.plan_for(ch["id"], payload.get("focus") or "")["workflow"] == "animated_story")
    with channels.using(ch["id"]), profile.using_language(ch["language"] or "Hindi"):
        if creative:
            from .creative import ideas
            return ideas.generate(ch["id"], count, fmts, focus=payload.get("focus") or "")
        return engine.run_ideation({"channel_id": ch["id"], "count": count, "formats": fmts,
                                    "focus": payload.get("focus")})


@worker.handler("trend_research")
def _trend_research(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    """What is popular in each story channel's niche (official API, metadata only) → original episode ideas."""
    from .creative import trends
    if payload.get("channel_id"):
        row = trends.scan(int(payload["channel_id"]))
        return trends.ideas_from(row) if row["videos"] else {"skipped": "no popular videos found"}
    return trends.run_daily()


@worker.handler("channel_stats")
def _channel_stats(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    from .youtube import growth
    if payload.get("channel_id"):
        return growth.refresh(int(payload["channel_id"]))
    return growth.refresh_all()


@worker.handler("channel_daily")
def _channel_daily(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    """A channel's scheduled video: its best saved/approved idea, else a fresh idea, else its own brief."""
    from . import channels, jobs
    ch = channels.get(int(payload["channel_id"]))
    if not ch or ch["status"] != "active":
        return {"skipped": "channel paused or deleted"}
    from . import ops
    if ops.stopped():
        return {"skipped": "emergency stop is on"}
    from . import plans
    if not plans.can(ch["workspace_id"], "schedules"):
        return {"skipped": "automatic daily videos aren't enabled for this account"}
    from datetime import datetime
    slot = int(payload.get("slot") or 0)
    day = datetime.now().astimezone().date().isoformat() + (f"#{slot + 1}" if slot else "")   # one video per slot
    done = db.q1("SELECT id FROM productions WHERE channel_id=? AND json_extract(request, '$.scheduled_for')=? "
                 "AND status<>'rejected'", (ch["id"], day))
    if done:
        return {"skipped": f"today's video already exists (#{done['id']})"}
    from .creative import news
    if news.is_news(ch):                     # news: today's trending story — never an idea saved days ago
        story = news.pick(ch)
        if not story:
            logs.event("warning", "news", f"'{ch['name']}': no new trending story reported by 2+ outlets right now")
            return {"skipped": "no new trending story right now"}
        res = jobs.create(ch["id"], "", idea_id=news.create_topic(ch, story), actor="schedule", scheduled_for=day)
        logs.event("info", "news", f"'{ch['name']}' news video started on “{story['keyword']}”: {res.get('production_ids')}")
        return {**res, "story": story["keyword"]}
    # what you approved first; then an idea from the last two days' trend scan (themes getting the most views in
    # this niche right now — original ideas, never copies); then saved and best-scored ideas
    idea = db.q1("SELECT id FROM topics WHERE channel_id=? AND status IN ('approved','idea') "
                 "ORDER BY (status='approved') DESC, (why_now LIKE 'this week''s popular theme%' AND created_at>=?) DESC, "
                 "saved DESC, score DESC, id DESC LIMIT 1", (ch["id"], db.iso_in(-2 * 86400)))
    if idea:
        res = jobs.create(ch["id"], "", idea_id=idea["id"], actor="schedule", scheduled_for=day)
    else:
        brief = f"A new {', '.join(channels.styles(ch)[:2]) or ''} episode for this channel: {ch['niche']}. " \
                f"{ch['instructions'] or ''}".strip()
        res = jobs.create(ch["id"], brief, actor="schedule", scheduled_for=day)
    # keep the idea list topped up for the coming days
    if db.q1("SELECT COUNT(*) AS n FROM topics WHERE channel_id=? AND status IN ('approved','idea')",
             (ch["id"],))["n"] < 4:
        worker.enqueue("channel_ideas", {"channel_id": ch["id"], "count": 8}, dedupe=True, max_attempts=2)
    logs.event("info", "planner", f"'{ch['name']}' daily video started: {res.get('production_ids')}")
    return res


@worker.handler("fib_lesson_daily")
def _fib_lesson_daily(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    """Generate the next lesson of the series (approval still required before any upload)."""
    from .finance import lessons
    audience = payload.get("audience", "india")
    n = lessons.next_lesson_number(audience)
    if n is None:
        db.ex("UPDATE schedules SET enabled=0 WHERE task_type='fib_lesson_daily'")
        logs.event("info", "planner", f"Fibonacci series complete ({lessons.TOTAL} lessons) — daily lesson schedule switched off")
        return {"complete": True}
    pid = lessons.create(n, audience=audience, fmt=payload.get("format", "long"))
    return {"production_id": pid, "lesson": n}


@worker.handler("nifty_daily_episode")
def _nifty_daily(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    """Daily show (job at 15:40): how today's levels worked + the next session's reference levels — from today's verified data only.

    Freshness rules (old data must never be presented as today's):
      * today must be an NSE trading day, and it must be after the 15:30 close;
      * a valid daily dataset must contain today's bar, retrieved after today's close;
      * today's 15-minute / 1-hour bars must be complete sessions (25 / 7 bars) to be used.
    Missing data -> nothing is generated; the check repeats every 10 minutes until 22:30.
    """
    from .finance import calendar, episode, marketdata
    if not settings.get("nifty.daily_episode", True):
        return {"skipped": "daily episode switched off"}
    now = marketdata.ist_now()
    today = now.date()
    if not calendar.is_trading_day(today):
        logs.event("info", "finance", f"No daily episode: {today} is not an NSE trading day ({calendar.why_closed(today)})")
        return {"skipped": calendar.why_closed(today)}
    if (now.hour, now.minute) < marketdata.SESSION_CLOSE:
        return {"skipped": "market still open"}
    review = today.isoformat()
    basis = calendar.previous_session(today).isoformat()
    if db.q1("SELECT id FROM productions WHERE kind='nifty_pivot' AND json_extract(episode,'$.request.mode')='daily_review' "
             "AND json_extract(episode,'$.request.review_date')=? AND status<>'rejected'", (review,)):
        return {"skipped": f"episode for {review} already exists"}
    close_utc = db.iso(now.replace(hour=15, minute=30, second=0, microsecond=0))
    ds = db.q1("SELECT id FROM market_datasets WHERE timeframe='1d' AND status!='invalid' AND last_date=? "
               "AND retrieved_at>=? ORDER BY id DESC LIMIT 1", (review, close_utc))
    from_project = False
    if not ds and settings.get("nifty.auto_import_pivot_project", True):
        # Fully automatic: take today's high / low / close and levels from the owner's Pivot Reversal app
        from .finance import pivot_project
        try:
            ds = {"id": pivot_project.import_today(today)}
            from_project = True
            logs.event("info", "finance", f"Imported today's NIFTY data from the Pivot Reversal project (dataset #{ds['id']})")
        except pivot_project.ProjectDataError as e:
            logs.event("warning", "finance", f"Pivot Reversal project data not usable yet: {e}")
    missing = [] if ds else [f"today's ({review}) daily bar"]
    need_intraday = settings.get("nifty.require_intraday", True) and not from_project   # that source has no candles
    late = (now.hour, now.minute) >= (22, 0)
    if need_intraday and not late:
        for tf in ("15m", "1h"):
            bars, ids = marketdata.intraday_bars(review, tf)
            if not (ids and marketdata.session_complete(bars, tf)):
                missing.append(f"today's complete {tf} bars")
    if missing and (not ds or not late):
        msg = (f"Daily NIFTY episode for {review} is waiting for verified data: {', '.join(missing)}. Import it under "
               "Compliance › Import market data (retrieved after 15:30). Nothing is generated from older data.")
        logs.event("warning", "finance", msg)
        if (now.hour, now.minute) < (22, 30):
            # De-duplicate against *waiting* checks only: worker dedupe also matches this running task and would drop it
            if not db.q1("SELECT id FROM tasks WHERE type='nifty_daily_episode' AND status='pending'"):
                worker.enqueue("nifty_daily_episode", {"recheck": True}, delay=600)
        return {"waiting_for_data": missing}
    pid = episode.create(ds["id"], basis, "daily_review", fmt="long", actor="scheduler",
                         audience=settings.get("nifty.daily_audience", "india"), review_date=review)
    return {"production_id": pid, "review_date": review, "basis_date": basis}


@worker.handler("housekeeping")
def _housekeeping(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    logs.prune()
    db.ex("DELETE FROM tasks WHERE status IN ('done','cancelled') AND updated_at < ?", (db.iso_in(-30 * 86400),))
    from . import storage
    freed, deleted = 0, 0
    days = int(settings.get("storage.auto_clean_days") or 0)
    if days:                          # working files of finished videos; the final video/thumbnail/subtitles stay
        for p in db.q("SELECT id FROM productions WHERE status IN ('uploaded','rejected','failed') AND updated_at < ?",
                      (db.iso_in(-days * 86400),)):
            freed += storage.clear_working_files(p["id"])
    rdays = int(settings.get("storage.auto_delete_rejected_days") or 0)
    if rdays:
        for p in db.q("SELECT id FROM productions WHERE status='rejected' AND parent_id IS NULL AND updated_at < ?",
                      (db.iso_in(-rdays * 86400),)):
            try:
                freed += storage.delete_video(p["id"])
                deleted += 1
            except ValueError:
                pass
    if freed:
        logs.event("info", "storage", f"Housekeeping freed {freed / 1e6:.0f} MB ({deleted} rejected video(s) deleted)")
    return {"freed_mb": round(freed / 1e6, 1), "deleted": deleted}
