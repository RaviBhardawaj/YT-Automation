"""Per-channel growth and YouTube Partner Program (monetization) progress.

Numbers come from the channel's own authorization: subscribers from the YouTube Data API, watch time and
Shorts views from the YouTube Analytics API. They approximate YouTube's own eligibility counters (which use
public views only), so they are labelled as estimates. Joining the Partner Program is done by the channel
owner in YouTube Studio → Earn; no API can switch monetization on.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .. import channels, db, logs
from . import oauth

# YouTube's published eligibility thresholds (support.google.com/youtube/answer/72851). They can change; the
# Studio "Earn" page is the authority.
TIERS = {
    "early": {"label": "Early access (fan funding: memberships, Super Thanks)", "subscribers": 500,
              "public_uploads_90d": 3, "watch_hours_12m": 3000, "shorts_views_90d": 3_000_000},
    "full": {"label": "Full Partner Program (ad revenue)", "subscribers": 1000, "watch_hours_12m": 4000,
             "shorts_views_90d": 10_000_000},
}
DATA = "https://www.googleapis.com/youtube/v3"
ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"


def _get(cid: int, url: str, params: dict[str, Any]) -> dict[str, Any]:
    r = oauth.request("GET", url, channel_id=cid, params=params)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
    return r.json()


def _by_type(cid: int, start: date) -> tuple[dict[str, dict[str, float]], str]:
    """{content type: {minutes, views}} — split into Shorts / long videos / live when the API supports it."""
    params = {"ids": "channel==MINE", "startDate": start.isoformat(), "endDate": date.today().isoformat(),
              "metrics": "estimatedMinutesWatched,views"}
    try:
        data = _get(cid, ANALYTICS, {**params, "dimensions": "creatorContentType"})
        out = {}
        for row in data.get("rows", []):
            out[str(row[0])] = {"minutes": float(row[1] or 0), "views": float(row[2] or 0)}
        return out, ""
    except RuntimeError:
        data = _get(cid, ANALYTICS, params)          # older accounts: totals only (Shorts included)
        row = (data.get("rows") or [[0, 0]])[0]
        return {"ALL": {"minutes": float(row[0] or 0), "views": float(row[1] or 0)}}, \
            "Shorts and long videos could not be separated; watch hours include Shorts."


def refresh(channel_id: int) -> dict[str, Any]:
    ch = channels.get(channel_id)
    if not ch or not oauth.connected(channel_id):
        raise RuntimeError("connect this channel to YouTube first")
    info = _get(channel_id, f"{DATA}/channels", {"part": "statistics,contentDetails,snippet", "mine": "true"})
    item = (info.get("items") or [None])[0]
    if not item:
        raise RuntimeError("this Google account has no YouTube channel")
    st = item.get("statistics", {})
    uploads_pl = item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
    since90 = date.today() - timedelta(days=90)
    public_90 = 0
    if uploads_pl:
        pl = _get(channel_id, f"{DATA}/playlistItems", {"part": "status,contentDetails", "playlistId": uploads_pl,
                                                        "maxResults": 50})
        for it in pl.get("items", []):
            published = (it.get("contentDetails", {}).get("videoPublishedAt") or "")[:10]
            if published >= since90.isoformat() and it.get("status", {}).get("privacyStatus") == "public":
                public_90 += 1
    year, note = _by_type(channel_id, date.today() - timedelta(days=365))
    quarter, _ = _by_type(channel_id, since90)
    long_minutes = sum(v["minutes"] for k, v in year.items() if k != "SHORTS")
    shorts_views = quarter.get("SHORTS", {}).get("views", 0.0) if "ALL" not in quarter else 0.0
    m = {
        "fetched_at": db.iso(), "youtube_title": item["snippet"]["title"],
        "subscribers": None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0)),
        "total_views": int(st.get("viewCount", 0)), "videos": int(st.get("videoCount", 0)),
        "watch_hours_12m": round(long_minutes / 60, 1), "shorts_views_90d": int(shorts_views),
        "public_uploads_90d": public_90, "note": note,
    }
    m["tiers"] = {k: progress(m, t) for k, t in TIERS.items()}
    db.update("channels", channel_id, {"monetization": db.dumps(m), "updated_at": db.iso()})
    logs.event("info", "youtube", f"'{ch['name']}': {m['subscribers']} subscribers, {m['watch_hours_12m']} watch hours "
               f"(12 months), {m['shorts_views_90d']:,} Shorts views (90 days)")
    return m


def progress(m: dict[str, Any], tier: dict[str, Any]) -> dict[str, Any]:
    subs = m.get("subscribers") or 0
    subs_ok = subs >= tier["subscribers"]
    uploads_ok = m.get("public_uploads_90d", 0) >= tier.get("public_uploads_90d", 0)
    hours_pct = min(100, m.get("watch_hours_12m", 0) / tier["watch_hours_12m"] * 100)
    shorts_pct = min(100, m.get("shorts_views_90d", 0) / tier["shorts_views_90d"] * 100)
    return {"subscribers_pct": round(min(100, subs / tier["subscribers"] * 100), 1), "subscribers_ok": subs_ok,
            "uploads_ok": uploads_ok, "watch_pct": round(hours_pct, 1), "shorts_pct": round(shorts_pct, 1),
            "eligible": subs_ok and uploads_ok and (hours_pct >= 100 or shorts_pct >= 100)}


def refresh_all() -> dict[str, Any]:
    done, failed = [], {}
    for ch in channels.all_channels():
        if oauth.connected(ch["id"]):
            try:
                refresh(ch["id"])
                done.append(ch["id"])
            except Exception as e:                  # one channel's problem never stops the others
                failed[ch["id"]] = str(e)[:200]
    return {"refreshed": done, "failed": failed}
