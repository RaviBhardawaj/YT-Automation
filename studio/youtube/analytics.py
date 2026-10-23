"""Pull public statistics (Data API) and owner analytics (Analytics API) for uploaded videos."""
from __future__ import annotations

from datetime import date
from typing import Any

from .. import db, logs
from . import oauth

ANALYTICS_METRICS = "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,subscribersGained"


def sync() -> dict[str, Any]:
    if not oauth.connected():
        return {"synced": 0, "note": "YouTube not connected"}
    rows = db.q("SELECT id, youtube_video_id, uploaded_at FROM productions WHERE youtube_video_id IS NOT NULL")
    if not rows:
        return {"synced": 0, "note": "no uploaded videos yet"}
    by_vid = {r["youtube_video_id"]: r for r in rows}
    stats: dict[str, dict[str, Any]] = {}
    ids = list(by_vid)
    for i in range(0, len(ids), 50):
        r = oauth.request("GET", "https://www.googleapis.com/youtube/v3/videos",
                          params={"part": "statistics,status", "id": ",".join(ids[i:i + 50])})
        for item in r.json().get("items", []) if r.status_code == 200 else []:
            s = item.get("statistics", {})
            stats[item["id"]] = {"views": int(s.get("viewCount", 0)), "likes": int(s.get("likeCount", 0)),
                                 "comments": int(s.get("commentCount", 0)),
                                 "privacy": item.get("status", {}).get("privacyStatus")}
    start = min((r["uploaded_at"] or db.iso())[:10] for r in rows)
    note = ""
    for i in range(0, len(ids), 200):
        r = oauth.request("GET", "https://youtubeanalytics.googleapis.com/v2/reports", params={
            "ids": "channel==MINE", "startDate": start, "endDate": date.today().isoformat(),
            "metrics": ANALYTICS_METRICS, "dimensions": "video", "filters": "video==" + ",".join(ids[i:i + 200]),
            "maxResults": 200})
        if r.status_code != 200:
            note = f"Analytics API: HTTP {r.status_code} (owner metrics unavailable; public stats saved)"
            continue
        data = r.json()
        cols = [h["name"] for h in data.get("columnHeaders", [])]
        for row in data.get("rows", []):
            rec = dict(zip(cols, row))
            stats.setdefault(rec["video"], {}).update({k: v for k, v in rec.items() if k != "video"})
    now = db.iso()
    for vid, m in stats.items():
        db.insert("analytics", {"production_id": by_vid[vid]["id"], "video_id": vid, "metrics": db.dumps(m), "fetched_at": now})
    logs.event("info", "youtube", f"Analytics synced for {len(stats)} video(s). {note}".strip())
    return {"synced": len(stats), "note": note}


def latest() -> list[dict[str, Any]]:
    rows = db.q("""SELECT a.video_id, a.metrics, a.fetched_at, p.id AS production_id, p.title, p.format, p.uploaded_at
                   FROM analytics a JOIN productions p ON p.id = a.production_id
                   WHERE a.id IN (SELECT MAX(id) FROM analytics GROUP BY video_id) ORDER BY p.uploaded_at DESC""")
    for r in rows:
        r["metrics"] = db.loads(r["metrics"], {})
    return rows
