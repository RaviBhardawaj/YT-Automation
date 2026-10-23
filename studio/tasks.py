"""Task handlers wired into the queue, plus the daily planner."""
from __future__ import annotations

import math
import shutil
from typing import Any

from . import config, db, logs, pipeline, profile, settings, worker
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


@worker.handler("production")
def _production(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    return pipeline.run_production(payload, task)


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
    approved = db.q("SELECT * FROM topics WHERE niche_id=? AND format=? AND status='approved' ORDER BY score DESC",
                    (niche["id"], fmt))
    auto = db.q("SELECT * FROM topics WHERE niche_id=? AND format=? AND status='idea' AND score>=? ORDER BY score DESC",
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
    freed = 0
    # Intermediate render files of finished productions can be regenerated; keep final outputs.
    for p in db.q("SELECT id FROM productions WHERE status IN ('uploaded','rejected') AND updated_at < ?",
                  (db.iso_in(-3 * 86400),)):
        d = config.PRODUCTIONS_DIR / f"{p['id']:05d}"
        for sub in ("clips", "overlays"):
            target = d / sub
            if target.exists():
                freed += sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
                shutil.rmtree(target, ignore_errors=True)
        (d / "video_noaudio.mp4").unlink(missing_ok=True)
    return {"freed_mb": round(freed / 1e6, 1)}
