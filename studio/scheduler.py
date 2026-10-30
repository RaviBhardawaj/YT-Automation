"""Persistent schedules stored in SQLite. Missed runs (Mac asleep / app closed) run once on wake."""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta
from typing import Any

from . import db, logs, worker

log = logging.getLogger(__name__)
_stop = threading.Event()
_thread: threading.Thread | None = None

DEFAULT_SCHEDULES = [
    {"name": "Topic research refill", "task_type": "ideation_refill", "kind": "daily", "time_of_day": "07:30"},
    {"name": "Daily production plan", "task_type": "daily_plan", "kind": "daily", "time_of_day": "08:00"},
    {"name": "YouTube analytics sync", "task_type": "analytics_sync", "kind": "interval", "interval_minutes": 360},
    {"name": "Housekeeping", "task_type": "housekeeping", "kind": "daily", "time_of_day": "03:30"},
    {"name": "Channel growth & monetization progress", "task_type": "channel_stats", "kind": "daily",
     "time_of_day": "06:00"},
    {"name": "Learn from results (better content every day)", "task_type": "learning_review", "kind": "daily",
     "time_of_day": "06:30"},
    {"name": "Trend research for every channel (original ideas, never copies)", "task_type": "trend_research",
     "kind": "daily", "time_of_day": "07:00"},
    # 7-part 'फिबोनाची पिवट सीखें' series: one lesson generated per day; switches itself off after part 7.
    {"name": "Fibonacci lesson series (daily 4 PM, 7 parts)", "task_type": "fib_lesson_daily", "kind": "daily",
     "time_of_day": "16:00"},
    # NIFTY 50 daily episode after the close on NSE trading days — only when that session's data has been imported.
    {"name": "NIFTY daily episode (3:40 PM): today's review + next-session levels", "task_type": "nifty_daily_episode",
     "kind": "daily", "time_of_day": "15:40", "days": [0, 1, 2, 3, 4]},
]


def next_run(s: dict[str, Any], after: datetime | None = None) -> str:
    after = (after or datetime.now().astimezone())
    if s["kind"] == "interval":
        return db.iso(after + timedelta(minutes=max(5, int(s.get("interval_minutes") or 60))))
    hh, mm = (int(x) for x in (s.get("time_of_day") or "09:00").split(":"))
    days = db.loads(s.get("days"), None)
    cand = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if cand <= after:
        cand += timedelta(days=1)
    for _ in range(8):
        if not days or cand.weekday() in days:
            break
        cand += timedelta(days=1)
    return db.iso(cand)


def ensure_defaults() -> None:
    for s in DEFAULT_SCHEDULES:
        if not db.q1("SELECT id FROM schedules WHERE task_type=?", (s["task_type"],)):
            row = {"name": s["name"], "task_type": s["task_type"], "payload": "{}", "kind": s["kind"],
                   "time_of_day": s.get("time_of_day"), "interval_minutes": s.get("interval_minutes"),
                   "days": db.dumps(s["days"]) if s.get("days") else None, "enabled": 1, "created_at": db.iso()}
            row["next_run_at"] = next_run(row)
            db.insert("schedules", row)


def sync_channels() -> None:
    """One daily slot per video time of each studio channel ("09:00, 18:00" = two videos a day); the NIFTY channel
    keeps its own data-driven 3:40 PM schedule."""
    from . import channels
    for ch in channels.all_channels():
        if ch["workflow"] == "nifty_daily":
            continue
        times = channels.schedule_times(ch)
        rows = {(db.loads(r["payload"], {}) or {}).get("slot", 0): r for r in db.q(
            "SELECT id, payload, time_of_day, days, next_run_at, enabled FROM schedules "
            "WHERE task_type='channel_daily' AND channel_id=? ORDER BY id", (ch["id"],))}
        for slot, at in enumerate(times):
            payload = {"channel_id": ch["id"], **({"slot": slot} if slot else {})}
            row = {"name": f"{ch['name']}: daily video" + (f" {slot + 1}" if len(times) > 1 else ""),
                   "task_type": "channel_daily", "payload": db.dumps(payload), "kind": "daily", "time_of_day": at,
                   "days": db.dumps(ch.get("schedule_days")) if ch.get("schedule_days") else None,
                   "enabled": int(bool(ch.get("schedule_enabled")) and ch.get("status") == "active"),
                   "channel_id": ch["id"]}
            row["next_run_at"] = next_run(row)
            cur = rows.pop(slot, None)
            if not cur:
                db.insert("schedules", {**row, "created_at": db.iso()})
            elif (cur["time_of_day"], cur["days"], cur["enabled"]) != (row["time_of_day"], row["days"], row["enabled"]) \
                    or not cur["next_run_at"]:
                db.update("schedules", cur["id"], row)
            else:
                db.update("schedules", cur["id"], {"name": row["name"]})
        for extra in rows.values():                        # fewer times than before: drop the extra slots
            db.ex("DELETE FROM schedules WHERE id=?", (extra["id"],))


def tick() -> int:
    if os.environ.get("STUDIO_SCHEDULES", "on") == "off":      # test / staging instances: no automatic jobs
        return 0
    now = db.iso()
    fired = 0
    for s in db.q("SELECT * FROM schedules WHERE enabled=1 AND next_run_at IS NOT NULL AND next_run_at <= ?", (now,)):
        worker.enqueue(s["task_type"], db.loads(s["payload"], {}), schedule_id=s["id"], dedupe=True)
        db.update("schedules", s["id"], {"last_run_at": now, "next_run_at": next_run(s)})
        logs.event("info", "scheduler", f"Started scheduled job: {s['name']}")
        fired += 1
    # new month -> resume tasks that were paused by the spending limit
    month = datetime.now().strftime("%Y-%m")
    from . import settings
    if settings.get("scheduler.last_month") != month:
        settings.set("scheduler.last_month", month)
        n = worker.resume_paused("%budget%")
        if n:
            logs.event("info", "scheduler", f"New month: resumed {n} task(s) paused by the AI budget")
    return fired


def _loop() -> None:
    while not _stop.wait(20):
        try:
            tick()
        except Exception:
            log.exception("scheduler tick failed")


def start() -> None:
    global _thread
    if _thread:
        return
    try:
        tick()
    except Exception:
        log.exception("initial scheduler tick failed")
    _thread = threading.Thread(target=_loop, name="scheduler", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
