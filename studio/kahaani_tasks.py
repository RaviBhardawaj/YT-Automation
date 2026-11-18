"""KahaaniVerse background tasks: the daily film (research → story → film queued) and the character library.
Kept apart from tasks.py so the channel can be switched off as one unit."""
from __future__ import annotations

from typing import Any

from . import channels, db, ops, worker

SCHEDULE = {"name": "KahaaniVerse daily film (research, story, storyboard, film + Short)", "task_type": "kahaani_daily",
            "kind": "daily", "time_of_day": "09:00"}


@worker.handler("kahaani_daily")
def _kahaani_daily(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    from .creative import director
    if ops.stopped():
        return {"skipped": "emergency stop is on"}
    out = {}
    for ch in channels.all_channels():
        if ch["workflow"] != "cinematic_film" or ch["status"] != "active":
            continue
        if ch.get("schedule_enabled"):          # the channel's own planned daily slot makes the film (smart timing)
            out[ch["id"]] = "made by the channel's daily slot"
            continue
        if db.q1("SELECT id FROM productions WHERE channel_id=? AND kind='film' AND created_at>=? AND status NOT IN "
                 "('rejected','failed')", (ch["id"], db.iso_in(-20 * 3600))):
            out[ch["id"]] = "today's film is already in production"
            continue
        out[ch["id"]] = director.start_film(ch["id"], minutes=float(ch.get("long_minutes") or 10),
                                            youtube=bool(payload.get("youtube", True)))
    return out


@worker.handler("kahaani_library")
def _kahaani_library(payload: dict[str, Any], task: dict[str, Any]) -> Any:
    from .creative import film, kahaani
    return kahaani.build_sheets(int(payload["channel_id"]), film._gen)


def ensure_schedule(enabled: bool = False) -> int:
    """The daily-film schedule (created off: it is switched on once a film has passed the owner's look)."""
    row = db.q1("SELECT id FROM schedules WHERE task_type='kahaani_daily'")
    if row:
        return row["id"]
    from . import scheduler
    s = {**SCHEDULE, "payload": "{}", "interval_minutes": None, "days": None, "enabled": int(enabled),
         "created_at": db.iso()}
    s["next_run_at"] = scheduler.next_run(s)
    return db.insert("schedules", s)
