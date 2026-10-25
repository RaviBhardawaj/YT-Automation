"""Pull public statistics (Data API) and owner analytics (Analytics API) for uploaded videos."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .. import db, logs
from . import oauth

ANALYTICS_METRICS = "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,subscribersGained"


def sync(channel_id: int | None = None) -> dict[str, Any]:
    """Per-video statistics for the studio's uploads to one channel, read with that channel's own login."""
    if channel_id is None:
        return sync_all()
    if not oauth.connected(channel_id):
        return {"synced": 0, "note": "YouTube not connected"}
    rows = db.q("SELECT id, youtube_video_id, uploaded_at FROM productions WHERE youtube_video_id IS NOT NULL "
                "AND channel_id=?", (channel_id,))
    if not rows:
        return {"synced": 0, "note": "no uploaded videos yet"}
    by_vid = {r["youtube_video_id"]: r for r in rows}
    stats: dict[str, dict[str, Any]] = {}
    ids = list(by_vid)
    for i in range(0, len(ids), 50):
        r = oauth.request("GET", "https://www.googleapis.com/youtube/v3/videos", channel_id=channel_id,
                          params={"part": "statistics,status", "id": ",".join(ids[i:i + 50])})
        for item in r.json().get("items", []) if r.status_code == 200 else []:
            s = item.get("statistics", {})
            stats[item["id"]] = {"views": int(s.get("viewCount", 0)), "likes": int(s.get("likeCount", 0)),
                                 "comments": int(s.get("commentCount", 0)),
                                 "privacy": item.get("status", {}).get("privacyStatus")}
    start = min((r["uploaded_at"] or db.iso())[:10] for r in rows)
    note = ""
    for i in range(0, len(ids), 200):
        r = oauth.request("GET", "https://youtubeanalytics.googleapis.com/v2/reports", channel_id=channel_id, params={
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


DATA = "https://www.googleapis.com/youtube/v3"
REPORTS = "https://youtubeanalytics.googleapis.com/v2/reports"
TOTALS = ("views,estimatedMinutesWatched,averageViewDuration,subscribersGained,subscribersLost,likes,comments,shares")


def _get(cid: int, url: str, params: dict[str, Any]) -> dict[str, Any]:
    r = oauth.request("GET", url, channel_id=cid, params=params)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
    return r.json()


def channel_report(cid: int, days: int = 28) -> dict[str, Any]:
    """The channel's last-N-day totals and daily trend, plus every recent video on the YouTube channel."""
    from .. import channels
    end, start = date.today(), date.today() - timedelta(days=days - 1)
    rep: dict[str, Any] = {"fetched_at": db.iso(), "days": days, "notes": []}
    try:
        t = _get(cid, REPORTS, {"ids": "channel==MINE", "startDate": start.isoformat(), "endDate": end.isoformat(),
                                "metrics": TOTALS})
        cols = [h["name"] for h in t.get("columnHeaders", [])]
        rep["totals"] = dict(zip(cols, (t.get("rows") or [[0] * len(cols)])[0]))
        d = _get(cid, REPORTS, {"ids": "channel==MINE", "startDate": start.isoformat(), "endDate": end.isoformat(),
                                "metrics": "views,estimatedMinutesWatched,subscribersGained", "dimensions": "day",
                                "sort": "day"})
        got = {row[0]: row for row in d.get("rows", [])}
        rep["daily"] = [{"day": (start + timedelta(days=i)).isoformat(),
                         "views": int((got.get((start + timedelta(days=i)).isoformat()) or [0, 0])[1]),
                         "minutes": float((got.get((start + timedelta(days=i)).isoformat()) or [0, 0, 0])[2]),
                         "subs": int((got.get((start + timedelta(days=i)).isoformat()) or [0, 0, 0, 0])[3])}
                        for i in range(days)]
    except RuntimeError as e:                       # e.g. a brand-new channel has no Analytics data yet
        rep["notes"].append(f"YouTube Analytics: {e}")
        rep.setdefault("totals", {})
        rep.setdefault("daily", [])
    # every video on the channel (latest 50), whether or not the studio made it
    info = _get(cid, f"{DATA}/channels", {"part": "contentDetails,statistics,snippet", "mine": "true"})
    item = (info.get("items") or [{}])[0]
    rep["channel"] = {"title": item.get("snippet", {}).get("title"), **{k: int(v) for k, v in
                      item.get("statistics", {}).items() if str(v).isdigit()}}
    uploads = item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
    videos = []
    if uploads:
        pl = _get(cid, f"{DATA}/playlistItems", {"part": "contentDetails", "playlistId": uploads, "maxResults": 50})
        ids = [i["contentDetails"]["videoId"] for i in pl.get("items", [])]
        if ids:
            vs = _get(cid, f"{DATA}/videos", {"part": "snippet,statistics,contentDetails,status", "id": ",".join(ids)})
            made = {r["youtube_video_id"]: r["id"] for r in db.q(
                "SELECT id, youtube_video_id FROM productions WHERE youtube_video_id IS NOT NULL AND channel_id=?",
                (cid,))}
            from ..research.sources import _iso_duration_seconds
            for v in vs.get("items", []):
                st, sn = v.get("statistics", {}), v["snippet"]
                dur = _iso_duration_seconds(v.get("contentDetails", {}).get("duration", ""))
                videos.append({"id": v["id"], "title": sn.get("title", ""), "published": sn.get("publishedAt", "")[:10],
                               "views": int(st.get("viewCount", 0) or 0), "likes": int(st.get("likeCount", 0) or 0),
                               "comments": int(st.get("commentCount", 0) or 0), "seconds": dur,
                               "short": 0 < dur <= 180, "privacy": v.get("status", {}).get("privacyStatus"),
                               "production_id": made.get(v["id"])})
    videos.sort(key=lambda v: v["published"], reverse=True)
    rep["videos"] = videos
    db.update("channels", cid, {"analytics": db.dumps(rep), "updated_at": db.iso()})
    ch = channels.get(cid)
    logs.event("info", "youtube", f"Analytics for '{ch['name']}': {len(videos)} videos, "
               f"{int(rep['totals'].get('views', 0) or 0):,} views in {days} days")
    return rep


def sync_all() -> dict[str, Any]:
    """Every connected channel: its report, growth progress and per-video stats. One failure never stops the rest."""
    from .. import channels
    from . import growth
    done, failed = [], {}
    for ch in channels.all_channels():
        if not oauth.connected(ch["id"]):
            continue
        try:
            channel_report(ch["id"])
            growth.refresh(ch["id"])
            sync(ch["id"])
            done.append(ch["id"])
        except Exception as e:
            failed[ch["id"]] = str(e)[:200]
            logs.event("warning", "youtube", f"Analytics for '{ch['name']}' failed: {e}")
    return {"channels": done, "failed": failed}
