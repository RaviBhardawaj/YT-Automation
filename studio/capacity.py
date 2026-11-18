"""Smart daily timing: when each channel's video is MADE, and when it goes PUBLIC — from this studio's own numbers.

The Mac makes one heavy video at a time (media lane), the internet (a phone hotspot) is often off at night, the free AI
writers run out when many jobs start together, and the free allowances reset at fixed times. So the planner:
  * measures each channel's real making time (median of its recent videos, plus a buffer);
  * measures, per hour, how often the internet was down (from the studio's own logs);
  * keeps fixed jobs fixed (the NIFTY episode needs the 15:30 market close);
  * places every other channel's making window so nothing overlaps, in reliable-internet hours, finishing before its
    publish time (with a margin), preferring a start just after a free-allowance reset;
  * sets each channel's publish time for its audience (kids after school, US + India news at the US morning / Indian
    evening, the market channel after the close).
It recomputes weekly and changes a channel's time only when the better slot is at least MIN_SHIFT away.
"""
from __future__ import annotations

import re
from statistics import median
from typing import Any

from . import channels, db, logs

STEP = 15                       # minutes
EARLIEST, LATEST = 6 * 60, 22 * 60
MARGIN = 45                     # ready this long before going public
BUFFER = 1.25                   # making time × this (retries, waiting for a writer)
MIN_SHIFT = 30                  # minutes; smaller changes aren't worth moving a schedule for
OFFLINE_OK = 0.30               # an hour is "reliable" when fewer than 30% of its events were offline errors
RESETS = (5 * 60 + 30, 13 * 60 + 30)       # Cloudflare (00:00 UTC) and Gemini / YouTube quota (midnight Pacific), IST
FINANCE_START = 15 * 60 + 40               # NIFTY daily episode: after the market close
DEFAULT_MAKE = {"story": 300, "general": 150, "nifty_pivot": 120}
# a KahaaniVerse film's making time grows with its length: storyboard + cast (fixed) and, per minute of film, ~12 shots
# (pictures, with the hotspot's retries) plus SadTalker (~50 s of Mac time per second of speech, ~0.6 min of speech
# per film minute) and the edit — measured on #199 / the 4 Nov dialogue test
FILM_FIXED, FILM_PER_MINUTE = 60, 70
FILM_MINUTES = (1.5, 10.0)               # shortest and longest film the planner may set
RELIABLE_DAY = 15 * 60 + 30              # minutes a day with dependable internet (measured hours below OFFLINE_OK)
AUDIENCE_PUBLISH = {           # minutes after midnight IST
    "kids": 18 * 60,           # after school in India
    "news_us_in": 18 * 60 + 30,   # 8:00 AM US Eastern and Indian evening prime time
    "news": 19 * 60,
    "finance": 18 * 60 + 30,   # after the close, once the episode is ready
    "film": 19 * 60,           # story films: Indian evening family time, after the kids' and news slots
    "default": 18 * 60,
}
OFFLINE_RX = re.compile(r"nodename nor servname|Network is unreachable|ConnectError|No internet")


def _hhmm(m: int) -> str:
    return f"{m // 60 % 24:02d}:{m % 60:02d}"


def _mins(t: str | None) -> int | None:
    if not t or ":" not in t:
        return None
    hh, mm = t.split(",")[0].strip().split(":")
    return int(hh) * 60 + int(mm)


def offline_rate(days: int = 14) -> dict[int, float]:
    """Share of each local hour's logged events that were 'no internet' errors (0..1)."""
    total, off = [0] * 24, [0] * 24
    for r in db.q("SELECT created_at, message FROM logs WHERE created_at >= ?", (db.iso_in(-days * 86400),)):
        h = db.parse_iso(r["created_at"]).astimezone().hour
        total[h] += 1
        off[h] += bool(OFFLINE_RX.search(r["message"] or ""))
    return {h: (off[h] / total[h] if total[h] >= 5 else 0.0) for h in range(24)}


def make_minutes(channel_id: int, kind: str, days: int = 21) -> int:
    """Median minutes from queued to ready for this channel's main daily video (a default until there's data)."""
    # from the first real work step (not the 'queued' line: time spent waiting behind other channels isn't making)
    rows = db.q("SELECT p.id, (SELECT MIN(created_at) FROM logs WHERE production_id=p.id AND message NOT LIKE "
                "'%queued%') AS a, "
                "(SELECT MAX(created_at) FROM logs WHERE production_id=p.id AND (message LIKE 'Ready for your review%' "
                "OR message LIKE 'Rendered%' OR message LIKE 'Uploaded %')) AS b FROM productions p "
                "WHERE p.channel_id=? AND p.kind=? AND p.format='long' AND p.created_at>=?",
                (channel_id, kind, db.iso_in(-days * 86400)))
    spans = [(db.parse_iso(r["b"]) - db.parse_iso(r["a"])).total_seconds() / 60 for r in rows if r["a"] and r["b"]]
    spans = [s for s in spans if 2 < s < 12 * 60]
    return int(median(spans)) if len(spans) >= 2 else DEFAULT_MAKE.get(kind, 150)


def _audience(ch: dict[str, Any]) -> str:
    from .creative import news
    if ch.get("workflow") == "nifty_daily":
        return "finance"
    if ch.get("workflow") == "cinematic_film":
        return "film"
    if ch.get("made_for_kids") or "kids" in channels.styles(ch):
        return "kids"
    if news.is_news(ch):
        return "news_us_in" if {"US", "IN"} <= set(news.regions(ch)) else "news"
    return "default"


def _kind(ch: dict[str, Any]) -> str:
    return {"nifty_daily": "nifty_pivot", "animated_story": "story", "cinematic_film": "film"}.get(ch.get("workflow"),
                                                                                                    "general")


def film_make(minutes: float) -> int:
    return int(FILM_FIXED + FILM_PER_MINUTE * minutes)


def _make(ch: dict[str, Any]) -> int:
    """Measured making time (with buffer); a film's is worked out from its length (lengths change; films are few)."""
    kind = _kind(ch)
    if kind == "film":
        return int(film_make(float(ch.get("long_minutes") or 3)) * BUFFER)
    return int(make_minutes(ch["id"], kind) * BUFFER)


def film_minutes_fit(others: int) -> float:
    """The longest film (in half minutes) whose making still fits the reliable-internet day next to the other
    channels' measured making times — a longer film runs into the next day and every channel falls behind."""
    left = RELIABLE_DAY - others
    m = ((left / BUFFER) - FILM_FIXED) / FILM_PER_MINUTE
    return max(FILM_MINUTES[0], min(FILM_MINUTES[1], int(m * 2) / 2))


def plan() -> dict[str, Any]:
    """The proposed timing for every active channel, with the reasons."""
    rate = offline_rate()
    blocks: list[tuple[int, int, int]] = []           # (start, end, channel id) already placed in the media lane
    out: list[dict[str, Any]] = []
    chans = [c for c in channels.all_channels() if c["status"] == "active"]
    fixed = [c for c in chans if c.get("workflow") == "nifty_daily"]
    flexible = [c for c in chans if c not in fixed and c.get("schedule_enabled")]
    for c in fixed:
        make = int(make_minutes(c["id"], "nifty_pivot") * BUFFER)
        blocks.append((FINANCE_START, FINANCE_START + make, c["id"]))
        pub = max(AUDIENCE_PUBLISH["finance"], FINANCE_START + make + MARGIN)
        out.append({"channel_id": c["id"], "name": c["name"], "make_at": _hhmm(FINANCE_START), "make_minutes": make,
                    "publish_at": _hhmm(pub), "why": "fixed: needs the 15:30 market close; publishes once ready"})

    def online(start: int, end: int) -> float:
        hours = range(start // 60, (end - 1) // 60 + 1)
        return sum(rate.get(h % 24, 0.0) for h in hours) / max(1, len(hours))

    def free(start: int, end: int) -> bool:
        return all(end <= a or start >= b for a, b, _ in blocks)
    films = [c for c in flexible if _kind(c) == "film"]
    others = sum(b - a for a, b, _ in blocks) + sum(_make(c) for c in flexible if c not in films)
    sized: dict[int, float] = {}
    for c in films:
        sized[c["id"]] = film_minutes_fit(others)
        c = {**c, "long_minutes": sized[c["id"]]}
    # films first: they need the free reference-picture allowance (Cloudflare, resets 05:30) the most, so they take
    # the first reliable hours of the allowance day; everything else is placed around them
    # then news (it must be made the same afternoon to be fresh), then the rest, longest first
    rest = sorted([c for c in flexible if c not in films], key=lambda c: (not _audience(c).startswith("news"), -_make(c)))
    order = sorted(films, key=lambda c: c["id"]) + rest
    for c in order:
        if c["id"] in sized:
            c = {**c, "long_minutes": sized[c["id"]]}
        make = _make(c)
        target = AUDIENCE_PUBLISH[_audience(c)]
        first_ok = next((h * 60 for h in range(RESETS[0] // 60 + 1, 24) if rate.get(h, 0.0) < OFFLINE_OK), EARLIEST)
        best, best_score = None, None
        for start in range(EARLIEST, LATEST, STEP):
            end = start + make
            if not free(start, end) or rate.get(start // 60, 0.0) >= OFFLINE_OK:
                continue
            late = max(0, end + MARGIN - target)                  # ready after the audience slot: goes out a day late
            fresh = min((start - r) % 1440 for r in RESETS)        # minutes since the last free-allowance reset
            score = (late > 0) * 10_000 + late + online(start, end) * 600 + min(fresh, 240) * 0.5 \
                + abs(target - MARGIN - end) * 0.2                # ready close to its slot (fresh news, fresh ideas)
            if c["id"] in sized:                                   # a film: as early in the allowance day as possible
                score = (late > 0) * 10_000 + online(start, end) * 600 + (start - first_ok) * 2
            if best_score is None or score < best_score:
                best, best_score = start, score
        if best is None:                                          # the lane is full: queue right after the last job
            best = max(b for _, b, _ in blocks) if blocks else 9 * 60
        blocks.append((best, best + make, c["id"]))
        ready = best + make + MARGIN
        pub = target                    # always the audience slot; ready late = made the evening before, out next day
        reasons = [f"makes in ~{make} min (measured, with buffer)", f"internet down {online(best, best + make):.0%} of "
                   f"the time in that window", f"audience slot {_hhmm(target)} ({_audience(c).replace('_', ' ')})"]
        if ready > target:
            reasons.append("made the evening before (no gap in the day's lane) and goes public at the audience slot "
                           "the next day")
        if c["id"] in sized:
            reasons.append(f"film length {sized[c['id']]:g} min — the longest that fits the day's reliable-internet "
                           f"hours next to the other channels; first in the day for the free reference-picture "
                           "allowance (Cloudflare resets 05:30)")
        out.append({"channel_id": c["id"], "name": c["name"], "make_at": _hhmm(best), "make_minutes": make,
                    "publish_at": _hhmm(pub), "why": "; ".join(reasons),
                    **({"film_minutes": sized[c["id"]]} if c["id"] in sized else {})})
    lane = sorted(blocks)
    return {"channels": out, "lane": [(_hhmm(a), _hhmm(b), cid) for a, b, cid in lane],
            "offline_by_hour": {h: round(v, 2) for h, v in rate.items()},
            "busy_minutes": sum(b - a for a, b, _ in blocks)}


def apply(p: dict[str, Any] | None = None, actor: str = "planner") -> list[str]:
    """Set each flexible channel's making time and every channel's publish time; skip changes under MIN_SHIFT."""
    from . import ops, scheduler
    p = p or plan()
    changed = []
    for row in p["channels"]:
        ch = channels.get(row["channel_id"])
        upd: dict[str, Any] = {}
        if ch.get("workflow") != "nifty_daily":
            cur = _mins(ch.get("schedule_time"))
            if cur is None or abs(cur - _mins(row["make_at"])) >= MIN_SHIFT:
                upd["schedule_time"] = row["make_at"]
        if "film_minutes" in row and abs(float(ch.get("long_minutes") or 0) - row["film_minutes"]) >= 0.5:
            upd["long_minutes"] = row["film_minutes"]
        cur_pub = _mins(ch.get("publish_time"))
        if cur_pub is None or abs(cur_pub - _mins(row["publish_at"])) >= MIN_SHIFT:
            upd["publish_time"] = row["publish_at"]
        if upd:
            before = {k: ch.get(k) for k in upd}
            channels.update(ch["id"], upd, actor=actor)
            ops.audit(actor, "smart_timing", channel_id=ch["id"], detail={"before": before, "after": upd,
                                                                            "why": row["why"]})
            changed.append(f"{ch['name']}: " + ", ".join(f"{k.replace('_', ' ')} {before[k]} → {v}"
                                                         for k, v in upd.items()))
    if changed:
        scheduler.sync_channels()
        logs.event("info", "planner", "Smart timing: " + "; ".join(changed))
    return changed


# ------------------------------------------------------------------ upload pacing
UPLOADS_PER_DAY = 4         # per channel per 24 h: YouTube refused the 5th upload in a day on 30 Oct (uploadLimitExceeded)
UPLOAD_GAP = 60             # minutes between two uploads to the same channel (no bursts)
QUIET = 0.5                 # an hour whose events are ≥ 50% 'no internet' is not used to start an upload


def upload_wait(channel_id: int | None, now: Any = None) -> tuple[int, str]:
    """Seconds an upload to this channel should wait, and why (0, '' = go now). Keeps every channel under YouTube's
    daily upload limit, spaces uploads out, and skips the hours the internet is usually down."""
    from datetime import datetime, timedelta
    now = now or datetime.now().astimezone()
    if not channel_id:
        return 0, ""
    waits: list[tuple[int, str]] = []
    rows = db.q("SELECT uploaded_at FROM productions WHERE channel_id=? AND youtube_video_id IS NOT NULL AND "
                "uploaded_at>=? ORDER BY uploaded_at", (channel_id, db.iso_in(-86400)))
    times = [db.parse_iso(r["uploaded_at"]) for r in rows if r["uploaded_at"]]
    if len(times) >= UPLOADS_PER_DAY:
        free_at = times[len(times) - UPLOADS_PER_DAY] + timedelta(days=1)
        waits.append((int((free_at - now).total_seconds()) + 60,
                      f"{len(times)} uploads to this channel in the last 24 h (YouTube's daily limit) — next slot "
                      f"{free_at.astimezone():%H:%M}"))
    if times:
        gap_at = times[-1] + timedelta(minutes=UPLOAD_GAP)
        if gap_at > now:
            waits.append((int((gap_at - now).total_seconds()) + 30,
                          f"spacing uploads {UPLOAD_GAP} min apart — next at {gap_at.astimezone():%H:%M}"))
    rate = offline_rate()
    if rate.get(now.hour, 0.0) >= QUIET:
        for k in range(1, 25):
            t = (now + timedelta(hours=k)).replace(minute=0, second=0, microsecond=0)
            if rate.get(t.hour, 0.0) < OFFLINE_OK:
                waits.append((int((t - now).total_seconds()) + 60,
                              f"the internet is usually down at {now:%H}:00 — uploading at {t:%H:%M}"))
                break
    wait = max(waits, default=(0, ""), key=lambda w: w[0])
    return (wait if wait[0] > 0 else (0, ""))
