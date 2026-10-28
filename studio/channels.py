"""Studio channels: one studio, many YouTube channels with different niches.

Every channel has its own niche, language, style, workflow, schedule, publishing rules, character bible,
ideas, library and YouTube authorization. Code that produces or uploads for a channel runs inside
`using(channel_id)`, so settings, voices and credentials always come from that channel.
"""
from __future__ import annotations

import contextvars
import re
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
JSON_FIELDS = ("styles", "branding", "voice", "schedule_days", "monetization", "youtube_status", "analytics", "meta")

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
          "publish_privacy", "status", "uploads_enabled", "made_for_kids", "publish_time")


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
            if isinstance(v, dict):                       # checkboxes: {"comedy": true, ...}
                v = [s for s, on in v.items() if on]
            v = [str(s).strip().lower() for s in (v if isinstance(v, list) else str(v).split(",")) if str(s).strip()]
        elif k in ("long_minutes",):
            v = max(1.0, min(30.0, float(v)))
        elif k == "short_seconds":
            v = max(15, min(180, int(v)))
        elif k == "shorts_per_episode":
            v = max(0, min(8, int(v)))
        elif k in ("research_required", "schedule_enabled", "auto_publish", "uploads_enabled", "made_for_kids"):
            v = 1 if v in (True, 1, "1", "true", "on", "yes") else 0
        elif k == "schedule_time":
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(v)):
                raise ValueError("schedule time must be HH:MM")
        elif k == "schedule_days":
            if isinstance(v, dict):                       # checkboxes: {"0": true, ...}
                v = [d for d, on in v.items() if on]
            v = sorted({int(x) for x in (v if isinstance(v, list) else str(v).split(",")) if str(x).strip() != ""
                        and 0 <= int(x) <= 6})
        elif k == "publish_time":
            v = str(v or "").strip() or None
            if v and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
                raise ValueError("publish time must be HH:MM (or empty to publish right away)")
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


FINANCE_KINDS = ("nifty_pivot", "nifty_short", "fib_lesson")


def finance_channel() -> dict[str, Any] | None:
    """The studio channel that carries the NIFTY episodes, their Shorts and the lesson series."""
    cur = get(current_id())
    if cur and cur["workflow"] == "nifty_daily":
        return cur
    return _row(db.q1("SELECT * FROM channels WHERE workflow='nifty_daily' ORDER BY (status='active') DESC, id LIMIT 1"))


def adopt_finance_orphans() -> int:
    """NIFTY videos saved without a channel belong to the NIFTY channel (its library, analytics and upload switch)."""
    ch = finance_channel()
    if not ch:
        return 0
    n = db.q1("SELECT COUNT(*) AS n FROM productions WHERE channel_id IS NULL AND kind IN "
              f"{FINANCE_KINDS!r}")["n"]
    if n:
        db.ex(f"UPDATE productions SET channel_id=?, workspace_id=? WHERE channel_id IS NULL AND kind IN {FINANCE_KINDS!r}",
              (ch["id"], ch["workspace_id"]))
    return n


# ------------------------------------------------------------------ helpers used by the pipelines

HANDLE_URL = re.compile(r"(?:youtube\.com/)?(@[\w.\-]{3,100})|youtube\.com/channel/(UC[\w\-]{22})|^(UC[\w\-]{22})$")


def lookup_youtube(text: str) -> dict[str, Any]:
    """Public details of a YouTube channel from an @handle, a channel URL or a UC… id (YouTube Data API)."""
    from .research import sources
    m = HANDLE_URL.search((text or "").strip())
    if not m:
        raise ValueError("enter the channel's @handle (like @KidsandComedy) or its youtube.com link")
    handle, cid = m.group(1), m.group(2) or m.group(3)
    params = {"part": "snippet,statistics,status", **({"forHandle": handle} if handle else {"id": cid})}
    items = sources._yt("channels", params, 1).get("items", [])
    if not items:
        raise ValueError(f"no YouTube channel found for {handle or cid}")
    c = items[0]
    sn, st = c["snippet"], c.get("statistics", {})
    return {"id": c["id"], "title": sn.get("title", ""), "handle": sn.get("customUrl") or handle or "",
            "created": (sn.get("publishedAt") or "")[:10], "subscribers": st.get("subscriberCount"),
            "videos": st.get("videoCount"), "thumbnail": (sn.get("thumbnails", {}).get("default") or {}).get("url"),
            "privacy": c.get("status", {}).get("privacyStatus")}


def link_youtube(channel_id: int, text: str) -> dict[str, Any]:
    """Lock a studio channel to one YouTube channel: connecting any other account is refused."""
    info = lookup_youtube(text)
    # duplicates only within the same workspace: another tenant can never block (or learn about) this link;
    # who really owns the YouTube channel is proven by Google's sign-in when connecting
    other = db.q1("SELECT id, name FROM channels WHERE (youtube_expected_id=? OR youtube_channel_id=?) AND id<>? "
                  "AND workspace_id=(SELECT workspace_id FROM channels WHERE id=?)",
                  (info["id"], info["id"], channel_id, channel_id))
    if other:
        raise ValueError(f"that YouTube channel is already linked to your channel '{other['name']}'")
    db.update("channels", channel_id, {"youtube_expected_id": info["id"], "youtube_handle": info["handle"],
                                       "youtube_title": info["title"], "updated_at": db.iso()})
    return info


def remove(channel_id: int) -> dict[str, int]:
    """Delete a studio channel. Its videos stay in the library (unassigned); its schedule and characters go."""
    if db.q1("SELECT 1 FROM tasks t JOIN productions p ON p.id=t.production_id WHERE p.channel_id=? "
             "AND t.status IN ('running')", (channel_id,)):
        raise ValueError("a video for this channel is being made right now — wait for it or cancel it first")
    from .youtube import oauth
    if oauth.connected(channel_id):
        oauth.disconnect(channel_id)
    db.ex("UPDATE tasks SET status='cancelled', updated_at=? WHERE status IN ('pending','paused') AND production_id IN "
          "(SELECT id FROM productions WHERE channel_id=?)", (db.iso(), channel_id))
    kept = db.ex("UPDATE productions SET channel_id=NULL WHERE channel_id=?", (channel_id,))
    db.ex("UPDATE topics SET channel_id=NULL, status=CASE WHEN status IN ('idea','approved') THEN 'rejected' "
          "ELSE status END WHERE channel_id=?", (channel_id,))
    db.ex("DELETE FROM schedules WHERE channel_id=?", (channel_id,))
    db.ex("DELETE FROM characters WHERE channel_id=?", (channel_id,))
    db.ex("DELETE FROM channels WHERE id=?", (channel_id,))
    return {"videos_kept": kept}


SHORT_KINDS = ("story_short", "nifty_short", "edit_clip")


def next_publish_slot(ch: dict[str, Any], main: bool = True) -> str | None:
    """Next occurrence of the channel's publish time (local), at least 20 minutes ahead; None = publish now."""
    from datetime import datetime, timedelta
    if not ch or not ch.get("publish_time"):
        return None
    hh, mm = (int(x) for x in ch["publish_time"].split(":"))
    now = datetime.now().astimezone()
    slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if slot <= now + timedelta(minutes=20):
        slot += timedelta(days=1)
    # one main video per slot: a backlog (after a pause or YouTube's upload limit) goes out on the following days
    # instead of all at once. Shorts may share their day.
    taken = {r["publish_at"] for r in db.q(
        f"SELECT publish_at FROM productions WHERE channel_id=? AND publish_at>=? AND kind NOT IN "
        f"{SHORT_KINDS!r} AND status IN ('approved','uploading','uploaded','paused')",
        (ch["id"], db.iso(slot)))}
    for _ in range(60 if main else 0):
        if db.iso(slot) not in taken:
            break
        slot += timedelta(days=1)
    return db.iso(slot)


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
