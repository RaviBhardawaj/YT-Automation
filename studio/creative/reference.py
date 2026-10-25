"""YouTube links in a request: "make a video like this one".

The linked video's public details (title, description, tags, category, language) are read through the
official YouTube Data API — or YouTube's public oEmbed endpoint when no API key is set — and become the brief:
the new video covers the same subject and genre, written and produced from scratch. The video itself is never
downloaded, re-uploaded or copied, and its wording is not reused.
"""
from __future__ import annotations

import re
from typing import Any

from .. import logs, net
from . import request as reqmod

VIDEO_URL = re.compile(r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com/(?:watch\?(?:[^\s#]*&)?v=|shorts/|live/|embed/)"
                       r"|youtu\.be/)([A-Za-z0-9_-]{11})[^\s]*", re.I)
# YouTube category id -> our content type (used only when the request and the video's text don't say)
CATEGORY_TYPES = {"1": "story", "2": "documentary", "10": "story", "15": "kids", "17": "documentary",
                  "19": "documentary", "20": "tech", "22": "documentary", "23": "comedy", "24": "story",
                  "25": "news", "26": "education", "27": "education", "28": "tech", "29": "documentary"}


def find(text: str) -> tuple[str, list[str]]:
    """(request text without the links, video ids in order)."""
    ids = [m.group(1) for m in VIDEO_URL.finditer(text or "")]
    return VIDEO_URL.sub(" ", text or "").strip(" ,.-\n"), list(dict.fromkeys(ids))


def fetch(video_id: str) -> dict[str, Any]:
    from ..research import sources
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        items = sources._yt("videos", {"part": "snippet,contentDetails", "id": video_id}, 1).get("items", [])
        if not items:
            raise ValueError("that YouTube video doesn't exist or is private")
        sn = items[0]["snippet"]
        return {"id": video_id, "url": url, "title": sn.get("title", ""), "channel": sn.get("channelTitle", ""),
                "description": (sn.get("description") or "")[:1500], "tags": (sn.get("tags") or [])[:20],
                "category_id": sn.get("categoryId"), "language": sn.get("defaultAudioLanguage") or
                sn.get("defaultLanguage") or "",
                "duration_s": sources._iso_duration_seconds(items[0].get("contentDetails", {}).get("duration", "")),
                "via": "YouTube Data API"}
    except ValueError:
        raise
    except Exception as e:                      # no API key / quota used up: public oEmbed (title + channel only)
        logs.event("info", "reference", f"YouTube API unavailable for {video_id} ({e}); using oEmbed")
        try:
            o = net.get_json("https://www.youtube.com/oembed", {"url": url, "format": "json"})
        except Exception as e2:
            raise ValueError(f"couldn't read that YouTube link ({e2})") from e2
        return {"id": video_id, "url": url, "title": o.get("title", ""), "channel": o.get("author_name", ""),
                "description": "", "tags": [], "category_id": None, "language": "", "duration_s": 0,
                "via": "YouTube oEmbed"}


def content_type(user_text: str, ref: dict[str, Any]) -> str:
    """What kind of video to make: the creator's own words first, then the linked video's subject."""
    for text in (user_text, " ".join([ref["title"], " ".join(ref["tags"]), ref["description"][:600]])):
        types = reqmod.detect_types(text)
        creative = [t for t in types if t in reqmod.CREATIVE_TYPES]
        if creative or types:
            return (creative or types)[0]
    return CATEGORY_TYPES.get(str(ref.get("category_id")), "documentary")


def brief(user_text: str, ref: dict[str, Any]) -> str:
    """The request the writers receive: the creator's instruction + what the reference video is about."""
    ask = user_text or "Make a new original video on the same subject as this reference video."
    parts = [ask, "", f"REFERENCE VIDEO (subject and genre only — never copy its script, title, footage or "
             f"wording; make it original): \"{ref['title']}\" by {ref['channel'] or 'unknown channel'}."]
    if ref["description"]:
        parts.append("Its description: " + re.sub(r"https?://\S+", "", ref["description"])[:900].strip())
    if ref["tags"]:
        parts.append("Its tags: " + ", ".join(ref["tags"][:15]))
    return "\n".join(parts)


def resolve(request: str) -> dict[str, Any] | None:
    """None when the request has no YouTube link; otherwise the reference and the derived brief."""
    user_text, ids = find(request)
    if not ids:
        return None
    ref = fetch(ids[0])
    ctype = content_type(user_text, ref)
    logs.event("info", "reference", f"Request links to “{ref['title']}” ({ref['via']}) → making a {ctype} video")
    return {"reference": ref, "user_text": user_text, "content_type": ctype, "brief": brief(user_text, ref)}
