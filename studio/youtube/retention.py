"""Where viewers leave: YouTube's audience-retention curve for one video, mapped onto that video's own scenes.

The curve comes from the YouTube Analytics API (dimension elapsedVideoTimeRatio; metrics audienceWatchRatio,
relativeRetentionPerformance, startedWatching, stoppedWatching, totalSegmentImpressions — all on the official
metric list, checked 2026-10-31). Each scene gets: watch ratio at its start and end, the largest drop inside it,
and a signal — drop_off / rewatch / strong_hold / steady / insufficient. Nothing is estimated: a scene with fewer
than two curve points is 'insufficient', and a video with fewer than MIN_VIEWS views gets no recommendation.

Scene mapping and thresholds adapted from youtube-automation-agent (MIT) utils/scene-retention-engine.js.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .. import db
from . import oauth

REPORTS = "https://youtubeanalytics.googleapis.com/v2/reports"
RETENTION_METRICS = ("audienceWatchRatio,relativeRetentionPerformance,startedWatching,stoppedWatching,"
                     "totalSegmentImpressions")
MIN_VIEWS = 20
MIN_POINTS = 20


def _report(channel_id: int, params: dict[str, Any]) -> list[dict[str, Any]]:
    r = oauth.request("GET", REPORTS, channel_id=channel_id, params=params)
    if r.status_code != 200:
        raise RuntimeError(f"Analytics API HTTP {r.status_code}: {r.text[:160]}")
    data = r.json()
    cols = [h["name"] for h in data.get("columnHeaders", [])]
    return [dict(zip(cols, row)) for row in data.get("rows", [])]


def fetch(channel_id: int, video_id: str, since: str) -> dict[str, Any]:
    """The retention curve and traffic sources of one video (owner analytics, read with the channel's own login)."""
    base = {"ids": "channel==MINE", "startDate": since[:10], "endDate": date.today().isoformat(),
            "filters": f"video=={video_id}"}
    points = _report(channel_id, {**base, "metrics": RETENTION_METRICS, "dimensions": "elapsedVideoTimeRatio"})
    sources = _report(channel_id, {**base, "metrics": "views,estimatedMinutesWatched",
                                   "dimensions": "insightTrafficSourceType", "sort": "-views"})
    return {"retention_points": [[round(float(p["elapsedVideoTimeRatio"]), 4),
                                  round(float(p.get("audienceWatchRatio") or 0), 4),
                                  round(float(p.get("relativeRetentionPerformance") or 0), 4),
                                  int(p.get("startedWatching") or 0), int(p.get("stoppedWatching") or 0),
                                  int(p.get("totalSegmentImpressions") or 0)] for p in points],
            "traffic_sources": {s["insightTrafficSourceType"]: {"views": int(s.get("views") or 0),
                                                                "minutes": float(s.get("estimatedMinutesWatched") or 0)}
                                for s in sources},
            "retention_fetched_at": db.iso()}


def _points(raw: list[list[float]]) -> list[dict[str, float]]:
    out, seen = [], set()
    for r in sorted(raw or [], key=lambda x: x[0]):
        if r[0] <= 0 or r[0] in seen:
            continue
        seen.add(r[0])
        out.append({"t": min(1.0, r[0]), "watch": max(0.0, r[1]), "relative": max(0.0, min(1.0, r[2])),
                    "started": max(0, r[3]), "stopped": max(0, r[4]), "impressions": max(0, r[5])})
    return out


def scene_times(p: dict[str, Any]) -> list[dict[str, Any]]:
    """[{index, label, start, end}] from the video's own timeline (narrated videos and films)."""
    tl = p.get("timeline") or {}
    if isinstance(tl, str):
        tl = db.loads(tl, {}) or {}
    scenes = tl.get("scenes") or []
    pad = float(tl.get("pad") or 0)
    pkg = p.get("package") or {}
    if isinstance(pkg, str):
        pkg = db.loads(pkg, {}) or {}
    labels = [(s.get("on_screen_text") or s.get("narration") or "")[:60] for s in pkg.get("scenes") or []]
    out, t = [], 0.0
    for i, s in enumerate(scenes):
        d = float(s.get("duration") or 0) + pad
        if d <= 0:
            continue
        out.append({"index": i, "label": labels[i] if i < len(labels) and labels[i] else f"Scene {i + 1}",
                    "start": round(t, 2), "end": round(t + d, 2)})
        t += d
    return out


def classify(m: dict[str, Any]) -> str:
    if m["points"] < 2:
        return "insufficient"
    if m["change"] <= -8 or m["largest_drop"] >= 10 or (m["stop_rate"] or 0) >= 12:
        return "drop_off"
    if m["peak"] >= 105 or m["largest_lift"] >= 8:
        return "rewatch"
    if m["change"] >= -4 and m["avg_relative"] >= 0.6:
        return "strong_hold"
    return "steady"


def map_to_scenes(raw: list[list[float]], scenes: list[dict[str, Any]], duration: float) -> list[dict[str, Any]]:
    """Each scene's share of the retention curve, in watch-ratio points (100 = everyone who started is watching)."""
    pts = _points(raw)
    if not pts or not scenes or duration <= 0:
        return []
    scale = duration / max(scenes[-1]["end"], 0.01)        # timeline length vs the real video length
    out = []
    for k, sc in enumerate(scenes):
        a, b = sc["start"] * scale, sc["end"] * scale
        sel = [p for p in pts if a < p["t"] * duration <= b or (k == len(scenes) - 1 and p["t"] * duration > a)]
        if not sel:
            continue
        i0 = pts.index(sel[0])
        prev = pts[i0 - 1] if i0 > 0 else sel[0]
        drop = max(0.0, prev["watch"] - sel[0]["watch"])
        lift = 0.0
        for x, y in zip(sel, sel[1:]):
            drop, lift = max(drop, x["watch"] - y["watch"]), max(lift, y["watch"] - x["watch"])
        imps = sum(p["impressions"] for p in sel)
        m = {**sc, "points": len(sel), "start_watch": round(sel[0]["watch"] * 100, 1),
             "end_watch": round(sel[-1]["watch"] * 100, 1), "change": round((sel[-1]["watch"] - sel[0]["watch"]) * 100, 1),
             "largest_drop": round(drop * 100, 1), "largest_lift": round(lift * 100, 1),
             "avg_relative": round(sum(p["relative"] for p in sel) / len(sel), 3),
             "stop_rate": round(sum(p["stopped"] for p in sel) / imps * 100, 1) if imps else None,
             "peak": round(max(p["watch"] for p in sel) * 100, 1)}
        m["signal"] = classify(m)
        out.append(m)
    return out


def report(production_id: int) -> dict[str, Any]:
    """The latest synced retention of one uploaded video, per scene, with a recommendation when the data allows."""
    p = db.q1("SELECT id, timeline, package FROM productions WHERE id=?", (production_id,))
    row = db.q1("SELECT metrics FROM analytics WHERE production_id=? ORDER BY id DESC LIMIT 1", (production_id,))
    m = db.loads((row or {}).get("metrics"), {}) or {}
    raw = m.get("retention_points") or []
    if not p or not raw:
        return {"available": False, "why": "no retention data synced yet (needs YouTube Analytics and a public "
                                           "video a few days old)"}
    tl = db.loads(p["timeline"], {}) or {}
    duration = float(tl.get("total_duration") or 0)
    scenes = map_to_scenes(raw, scene_times(p), duration)
    views = int(m.get("views") or 0)
    drops = sorted((s for s in scenes if s["signal"] == "drop_off"), key=lambda s: -(s["largest_drop"] + abs(s["change"])))
    holds = [s for s in scenes if s["signal"] in ("strong_hold", "rewatch")]
    rec = None
    if views >= MIN_VIEWS and len(raw) >= MIN_POINTS and (drops or holds):
        if drops:
            d = drops[0]
            rec = (f"Most viewers leave in scene {d['index'] + 1} ({d['start']:.0f}–{d['end']:.0f} s, "
                   f"'{d['label']}'): watch ratio falls {abs(d['change']):.0f} points. Shorten or sharpen that part "
                   "in the next video on this topic.")
        else:
            h = max(holds, key=lambda s: s["avg_relative"])
            rec = f"Scene {h['index'] + 1} ('{h['label']}') holds viewers best — open with that kind of moment."
    return {"available": True, "views": views, "points": len(raw), "scenes": scenes, "recommendation": rec,
            "confidence": "low" if views < 100 else ("medium" if views < 1000 else "high"),
            "traffic_sources": m.get("traffic_sources") or {}, "fetched_at": m.get("retention_fetched_at"),
            "note": None if rec or views >= MIN_VIEWS else f"only {views} views — too few to judge scenes"}
