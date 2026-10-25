"""Studio channels: one studio, many YouTube channels with different niches.

Every channel has its own niche, language, style, workflow, schedule, publishing rules, character bible,
ideas, library and YouTube authorization. Code that produces or uploads for a channel runs inside
`using(channel_id)`, so settings, voices and credentials always come from that channel.
"""
from __future__ import annotations

import contextvars
import json
from contextlib import contextmanager
from typing import Any, Iterator

from . import db, settings

WORKFLOWS = {
    "auto": "Decide per request (story/animation for creative requests, research for factual ones)",
    "animated_story": "Animated stories: comedy, horror, drama, kids, motivation (characters, dialogue, animation)",
    "factual": "Researched videos: explainers, tech, documentary, news, finance concepts (sources, visuals, narration)",
    "nifty_daily": "NIFTY 50 daily levels (verified market data, levels, disclosures)",
}
STYLES = ["comedy", "drama", "horror", "storytelling", "documentary", "education", "satire", "news", "motivation",
          "kids", "thriller", "romance", "mystery", "tech", "finance"]
LANGUAGES = ["Hindi", "Hinglish", "English", "Marathi", "Tamil", "Telugu", "Bengali", "Gujarati"]
JSON_FIELDS = ("styles", "branding", "voice", "schedule_days", "monetization")

_current: contextvars.ContextVar[int | None] = contextvars.ContextVar("current_channel", default=None)


def _row(r: dict[str, Any] | None) -> dict[str, Any] | None:
    if not r:
        return None
    for k in JSON_FIELDS:
        r[k] = db.loads(r.get(k), [] if k in ("styles", "schedule_days") else {}) or \
            ([] if k in ("styles", "schedule_days") else {})
    return r


def get(channel_id: int | None) -> dict[str, Any] | None:
    return _row(db.q1("SELECT * FROM channels WHERE id=?", (channel_id,))) if channel_id else None


def all_channels(workspace_id: int | None = None) -> list[dict[str, Any]]:
    """Every channel (scheduler/system use), or one workspace's channels (anything shown to a user)."""
    if workspace_id is None:
        return [_row(r) for r in db.q("SELECT * FROM channels ORDER BY id")]
    return [_row(r) for r in db.q("SELECT * FROM channels WHERE workspace_id=? ORDER BY id", (workspace_id,))]


def current_id() -> int | None:
    return _current.get()


def current() -> dict[str, Any] | None:
    return get(_current.get())


@contextmanager
def using(channel_id: int | None) -> Iterator[None]:
    token = _current.set(channel_id)
    try:
        yield
    finally:
        _current.reset(token)


FIELDS = ("name", "niche", "sub_niche", "audience", "language", "tone", "styles", "workflow", "formats", "long_minutes",
          "short_seconds", "shorts_per_episode", "instructions", "restrictions", "research_required", "branding",
          "voice", "schedule_enabled", "schedule_time", "schedule_days", "timezone", "auto_publish",
          "publish_privacy", "status", "uploads_enabled")


def clean(data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in FIELDS:
        if k not in data:
            continue
        v = data[k]
        if k in ("name", "niche", "sub_niche", "audience", "tone", "instructions", "restrictions"):
            v = str(v or "").strip()[:4000 if k in ("instructions", "restrictions") else 300]
        elif k == "language":
            v = str(v or "Hindi").strip()[:40] or "Hindi"
        elif k == "workflow":
            if v not in WORKFLOWS:
                raise ValueError(f"workflow must be one of {', '.join(WORKFLOWS)}")
        elif k == "formats":
            if v not in ("long", "short", "both"):
                raise ValueError("formats must be long, short or both")
        elif k == "styles":
            v = [str(s).strip().lower() for s in (v if isinstance(v, list) else str(v).split(",")) if str(s).strip()]
        elif k in ("long_minutes",):
            v = max(1.0, min(30.0, float(v)))
        elif k == "short_seconds":
            v = max(15, min(180, int(v)))
        elif k == "shorts_per_episode":
            v = max(0, min(8, int(v)))
        elif k in ("research_required", "schedule_enabled", "auto_publish", "uploads_enabled"):
            v = 1 if v in (True, 1, "1", "true", "on", "yes") else 0
        elif k == "schedule_time":
            import re
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(v)):
                raise ValueError("schedule time must be HH:MM")
        elif k == "schedule_days":
            v = sorted({int(x) for x in (v if isinstance(v, list) else str(v).split(",")) if str(x).strip() != ""
                        and 0 <= int(x) <= 6})
        elif k == "publish_privacy":
            if v not in ("private", "unlisted", "public"):
                raise ValueError("privacy must be private, unlisted or public")
        elif k == "status":
            if v not in ("active", "paused"):
                raise ValueError("status must be active or paused")
        elif k in ("branding", "voice"):
            v = v if isinstance(v, dict) else db.loads(v, {})
        out[k] = json.dumps(v, ensure_ascii=False) if k in JSON_FIELDS else v
    if not partial and not out.get("name"):
        raise ValueError("channel name is required")
    return out


def create(data: dict[str, Any], workspace_id: int | None = None) -> int:
    from . import plans
    row = clean(data)
    row.setdefault("styles", "[]")
    ws = workspace_id or plans.current_id() or 1
    return db.insert("channels", {**row, "workspace_id": ws, "created_at": db.iso(), "updated_at": db.iso()})


def update(channel_id: int, data: dict[str, Any]) -> None:
    row = clean(data, partial=True)
    if row:
        db.update("channels", channel_id, {**row, "updated_at": db.iso()})


def ensure_default() -> None:
    """First run of multi-channel: the existing setup becomes channel #1 (NIFTY), with all its history."""
    if db.q1("SELECT id FROM channels LIMIT 1"):
        return
    yt = settings.get("youtube.channel") or {}
    cid = db.insert("channels", {
        "name": "NIFTY 50 Fibonacci Levels", "niche": "NIFTY 50 Fibonacci pivot levels — daily analysis",
        "sub_niche": "support and resistance zones, next-session levels", "audience": "Indian stock-market learners",
        "language": "Hindi", "tone": "conversational Hinglish, news-anchor style",
        "styles": json.dumps(["finance", "news"]), "workflow": "nifty_daily", "formats": "both",
        "long_minutes": 8, "short_seconds": 50, "shorts_per_episode": 4,
        "instructions": "Daily 3:40 PM review of today's NIFTY session at the Fibonacci levels and the next "
                        "session's reference levels. Never give buy/sell calls, targets or stop-losses.",
        "restrictions": "No trade recommendations, no predictions, verified data only.", "research_required": 1,
        "branding": json.dumps({}), "voice": json.dumps({}), "schedule_enabled": 0, "schedule_time": "15:40",
        "schedule_days": json.dumps([0, 1, 2, 3, 4]), "timezone": "Asia/Kolkata", "auto_publish": 1,
        "publish_privacy": "public", "youtube_channel_id": yt.get("id"), "youtube_title": yt.get("title"),
        "status": "active", "created_at": db.iso(), "updated_at": db.iso()})
    db.ex("UPDATE productions SET channel_id=? WHERE channel_id IS NULL AND kind IN "
          "('nifty_pivot','nifty_short','fib_lesson')", (cid,))
    db.ex("UPDATE schedules SET channel_id=? WHERE channel_id IS NULL AND task_type IN "
          "('nifty_daily_episode','fib_lesson_daily')", (cid,))
    # earlier general videos went to the same YouTube channel: keep them in its library
    db.ex("UPDATE productions SET channel_id=? WHERE channel_id IS NULL", (cid,))


# ------------------------------------------------------------------ helpers used by the pipelines

def uploads_allowed(ch: dict[str, Any] | None) -> bool:
    """The channel's YouTube upload switch (off = videos are made and kept, never uploaded)."""
    return bool(ch) and bool(ch.get("uploads_enabled", 1))


def styles(ch: dict[str, Any] | None) -> list[str]:
    return (ch or {}).get("styles") or []


def formats(ch: dict[str, Any] | None) -> list[str]:
    f = (ch or {}).get("formats", "both")
    return {"long": ["long"], "short": ["short"], "both": ["long", "short"]}[f]


def refresh_token_secret(channel_id: int | None) -> str:
    """Each channel's YouTube authorization is its own Keychain entry (channel #1 keeps the original one)."""
    return "google_refresh_token" if not channel_id or channel_id == 1 else f"google_refresh_token__ch{channel_id}"


def counts(channel_id: int | None = None, workspace_id: int | None = None) -> dict[str, int]:
    where, args = ("WHERE channel_id=?", (channel_id,)) if channel_id else \
        ("WHERE workspace_id=?", (workspace_id,)) if workspace_id else ("", ())
    rows = db.q(f"SELECT status, created_at, format, kind FROM productions {where}", args)
    from datetime import datetime
    today = datetime.now().astimezone().date()

    def local_day(v: str):
        dt = db.parse_iso(v)
        return dt.astimezone().date() if dt else None
    active = ("queued", "analysing media", "transcribing", "planning edit", "rendering", "voicing", "scripting", "drawing charts", "quality check", "compliance gate",
              "generating script", "generating assets", "animating", "editing", "quality review", "uploading")
    return {
        "today": sum(1 for r in rows if local_day(r["created_at"]) == today),
        "in_production": sum(1 for r in rows if r["status"] in active),
        "review": sum(1 for r in rows if r["status"] in ("awaiting_approval", "needs_review")),
        "ready": sum(1 for r in rows if r["status"] == "approved"),
        "published": sum(1 for r in rows if r["status"] == "uploaded"),
        "failed": sum(1 for r in rows if r["status"] in ("failed", "needs_attention")),
        "shorts": sum(1 for r in rows if r["format"] == "short"),
        "total": len(rows),
    }
