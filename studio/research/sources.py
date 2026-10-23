"""Evidence collectors. Each returns a list of evidence dicts:

    {source_type, url, title, published_at, snippet, content, metrics}

Every number in `metrics` comes straight from the source API — nothing is estimated here.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import feedparser

from .. import db, net, settings

log = logging.getLogger(__name__)
Evidence = dict[str, Any]


def _iso_from_any(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, (int, float)):
        return db.iso(datetime.fromtimestamp(value, timezone.utc))
    if isinstance(value, time.struct_time):
        return db.iso(datetime(*value[:6], tzinfo=timezone.utc))
    s = str(value)
    try:
        return db.iso(parsedate_to_datetime(s))
    except (TypeError, ValueError):
        pass
    dt = db.parse_iso(s)
    return db.iso(dt) if dt else ""


def lang_code(language: str) -> str:
    return {"Hindi": "hi", "Hinglish": "hi", "English": "en"}.get(language, "en")


# ------------------------------------------------------------------ Google News RSS

def google_news(query: str, language: str, region: str, days: int = 7, limit: int = 12) -> list[Evidence]:
    hl = lang_code(language)
    url = "https://news.google.com/rss/search?" + urlencode(
        {"q": f"{query} when:{days}d", "hl": f"{hl}-{region}" if hl == "en" else hl, "gl": region, "ceid": f"{region}:{hl}"})
    _, text = net.get_text(url, respect_robots=False)
    feed = feedparser.parse(text)
    out = []
    for e in feed.entries[:limit]:
        source = (e.get("source") or {}).get("title", "")
        out.append({
            "source_type": "news", "url": e.get("link", ""), "title": e.get("title", ""),
            "published_at": _iso_from_any(e.get("published_parsed") or e.get("published")),
            "snippet": f"{source}".strip(), "content": "",
            "metrics": {"results_in_window": len(feed.entries), "window_days": days, "query": query},
        })
    return out


# ------------------------------------------------------------------ Wikipedia + pageviews

def wikipedia(query: str, language: str, limit: int = 2, with_text: bool = False) -> list[Evidence]:
    lc = lang_code(language)
    out = []
    for wl in dict.fromkeys([lc, "en"]):
        api = f"https://{wl}.wikipedia.org/w/api.php"
        data = net.get_json(api, {"action": "query", "list": "search", "srsearch": query, "srlimit": limit,
                                  "format": "json", "utf8": 1})
        for hit in data.get("query", {}).get("search", [])[:limit]:
            title = hit["title"]
            ev = {
                "source_type": "wikipedia",
                "url": f"https://{wl}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}",
                "title": f"Wikipedia ({wl}): {title}",
                "published_at": _iso_from_any(hit.get("timestamp")),
                "snippet": net.html_to_text(hit.get("snippet", ""), 400)[1] or _strip_tags(hit.get("snippet", "")),
                "content": "", "metrics": {},
            }
            try:
                ev["metrics"] = pageviews(wl, title)
            except net.FetchError as e:
                log.info("pageviews unavailable for %s: %s", title, e)
            if with_text:
                try:
                    ext = net.get_json(api, {"action": "query", "prop": "extracts", "explaintext": 1, "exchars": 6000,
                                             "titles": title, "format": "json", "redirects": 1})
                    pages = ext.get("query", {}).get("pages", {})
                    ev["content"] = next(iter(pages.values()), {}).get("extract", "")[:6000]
                except net.FetchError:
                    pass
            out.append(ev)
        if out:
            break
    return out


def _strip_tags(s: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", s)


def pageviews(wiki_lang: str, title: str) -> dict[str, Any]:
    end = datetime.now(timezone.utc).date() - timedelta(days=1)
    start = end - timedelta(days=59)
    url = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
           f"{wiki_lang}.wikipedia/all-access/user/{quote(title.replace(' ', '_'), safe='')}/daily/"
           f"{start:%Y%m%d}/{end:%Y%m%d}")
    data = net.get_json(url)
    views = [int(i.get("views", 0)) for i in data.get("items", [])]
    last30, prev30 = sum(views[-30:]), sum(views[:-30])
    return {
        "wiki_views_30d": last30, "wiki_views_prev_30d": prev30,
        "wiki_trend_pct": round((last30 - prev30) / prev30 * 100, 1) if prev30 else None,
        "pageviews_url": f"https://pageviews.wmcloud.org/?project={wiki_lang}.wikipedia.org&pages={quote(title.replace(' ', '_'))}",
    }


# ------------------------------------------------------------------ YouTube Data API v3

YT = "https://www.googleapis.com/youtube/v3"


def youtube_units_today() -> int:
    day = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")   # quota resets at midnight PT
    row = db.q1("SELECT COALESCE(SUM(units),0) AS u FROM api_quota WHERE api='youtube' AND day=?", (day,))
    return int(row["u"]) if row else 0


def _yt(path: str, params: dict[str, Any], units: int) -> dict[str, Any]:
    key = settings.secret("youtube_api_key")
    if not key:
        raise net.FetchError("YouTube Data API key not configured")
    if youtube_units_today() + units > int(settings.get("research.youtube_daily_unit_budget")):
        raise net.FetchError("daily YouTube API unit budget reached (Settings › Research)")
    day = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    db.insert("api_quota", {"api": "youtube", "units": units, "day": day, "created_at": db.iso()})
    return net.get_json(f"{YT}/{path}", {**params, "key": key})


def _iso_duration_seconds(d: str) -> int:
    import re
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
    if not m:
        return 0
    days, h, mi, s = (int(x or 0) for x in m.groups())
    return days * 86400 + h * 3600 + mi * 60 + s


def youtube_search(query: str, language: str, region: str, *, days: int = 365, limit: int = 15,
                   order: str = "relevance", duration: str | None = None) -> list[Evidence]:
    params: dict[str, Any] = {
        "part": "snippet", "q": query, "type": "video", "maxResults": min(limit, 50), "order": order,
        "regionCode": region, "relevanceLanguage": lang_code(language),
        "publishedAfter": db.iso(datetime.now(timezone.utc) - timedelta(days=days)),
    }
    if duration:
        params["videoDuration"] = duration
    found = _yt("search", params, 100)
    ids = [i["id"]["videoId"] for i in found.get("items", []) if i.get("id", {}).get("videoId")]
    if not ids:
        return []
    vids = _yt("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(ids)}, 1).get("items", [])
    channel_ids = sorted({v["snippet"]["channelId"] for v in vids})
    chans = {c["id"]: c for c in _yt("channels", {"part": "snippet,statistics", "id": ",".join(channel_ids[:50])}, 1).get("items", [])}
    out = []
    for v in vids:
        st, sn = v.get("statistics", {}), v["snippet"]
        ch = chans.get(sn["channelId"], {})
        subs = int(ch.get("statistics", {}).get("subscriberCount", 0) or 0)
        views = int(st.get("viewCount", 0) or 0)
        out.append({
            "source_type": "youtube", "url": f"https://www.youtube.com/watch?v={v['id']}",
            "title": sn.get("title", ""), "published_at": _iso_from_any(sn.get("publishedAt")),
            "snippet": f"Channel: {sn.get('channelTitle', '')}", "content": "",
            "metrics": {
                "views": views, "likes": int(st.get("likeCount", 0) or 0), "comments": int(st.get("commentCount", 0) or 0),
                "duration_s": _iso_duration_seconds(v.get("contentDetails", {}).get("duration", "")),
                "channel": sn.get("channelTitle", ""), "channel_id": sn["channelId"],
                "channel_url": f"https://www.youtube.com/channel/{sn['channelId']}",
                "channel_subscribers": subs if not ch.get("statistics", {}).get("hiddenSubscriberCount") else None,
                "views_per_subscriber": round(views / subs, 2) if subs else None,
            },
        })
    return out


# ------------------------------------------------------------------ Ollama web search

def ollama_web_search(query: str, limit: int = 8) -> list[Evidence]:
    key = settings.secret("ollama_api_key")
    if not key:
        raise net.FetchError("Ollama API key not configured")
    data = net.post_json("https://ollama.com/api/web_search", {"query": query, "max_results": min(limit, 10)},
                         headers={"Authorization": f"Bearer {key}"})
    out = []
    for r in data.get("results", []):
        if not str(r.get("url", "")).startswith(("http://", "https://")):
            continue
        out.append({"source_type": "web", "url": r["url"], "title": r.get("title", ""), "published_at": "",
                    "snippet": (r.get("content") or "")[:600], "content": (r.get("content") or "")[:4000],
                    "metrics": {}})
    return out


# ------------------------------------------------------------------ Hacker News (Algolia)

def hackernews(query: str, days: int = 30, limit: int = 8) -> list[Evidence]:
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    data = net.get_json("https://hn.algolia.com/api/v1/search",
                        {"query": query, "tags": "story", "numericFilters": f"created_at_i>{since}", "hitsPerPage": limit})
    return [{
        "source_type": "hackernews", "url": f"https://news.ycombinator.com/item?id={h['objectID']}",
        "title": h.get("title", ""), "published_at": _iso_from_any(h.get("created_at")),
        "snippet": h.get("url") or "", "content": "",
        "metrics": {"points": h.get("points", 0), "comments": h.get("num_comments", 0)},
    } for h in data.get("hits", [])]


# ------------------------------------------------------------------ user RSS feeds

def rss(keywords: list[str], limit: int = 10) -> list[Evidence]:
    words = [w.lower() for w in keywords if len(w) > 2]
    out: list[Evidence] = []
    for feed_row in db.q("SELECT url FROM feeds WHERE enabled=1"):
        try:
            _, text = net.get_text(feed_row["url"], respect_robots=False)
        except net.FetchError as e:
            log.info("feed %s failed: %s", feed_row["url"], e)
            continue
        for e in feedparser.parse(text).entries[:60]:
            blob = f"{e.get('title', '')} {e.get('summary', '')}".lower()
            if words and not any(w in blob for w in words):
                continue
            out.append({"source_type": "rss", "url": e.get("link", ""), "title": e.get("title", ""),
                        "published_at": _iso_from_any(e.get("published_parsed") or e.get("updated_parsed")),
                        "snippet": net.html_to_text(e.get("summary", ""), 500)[1][:500], "content": "", "metrics": {}})
            if len(out) >= limit:
                return out
    return out


# ------------------------------------------------------------------ full page text

def page_text(url: str) -> tuple[str, str]:
    """Fetch an article ourselves (robots.txt honoured). Returns (title, text)."""
    ctype, html = net.get_text(url, respect_robots=True)
    if "html" not in ctype and "text" not in ctype:
        raise net.FetchError(f"not a text page ({ctype})")
    return net.html_to_text(html, 8000)


def demand_points(metrics: dict[str, Any]) -> float | None:
    """A 0..1 demand signal from whatever real metric an evidence item carries."""
    if metrics.get("views"):
        return min(1.0, math.log10(metrics["views"] + 1) / 7)          # 10M views -> 1.0
    if metrics.get("wiki_views_30d"):
        return min(1.0, math.log10(metrics["wiki_views_30d"] + 1) / 6)  # 1M/month -> 1.0
    if metrics.get("points"):
        return min(1.0, math.log10(metrics["points"] + 1) / 3)          # 1000 points -> 1.0
    return None
