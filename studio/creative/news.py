"""Daily trending-news channel: picks today's most-searched story that real news outlets are reporting, and an
original, photo-real AI news presenter for the channel.

Where the stories come from: Google Trends' public daily feed for India (what people are searching for right now,
with the news articles that explain each search). A story is used only when at least two different outlets report
it; those articles are attached to the video as its sources, and the script is written from them in the channel's
own words. No one's video is downloaded, re-uploaded or re-cut — YouTube treats re-used clips as "reused content"
(no monetization) and news footage belongs to the broadcaster.

The presenter is a fictional person (not modelled on any real anchor), drawn once as a photo-real studio picture.
Videos that show the presenter are marked as altered/synthetic content when uploaded, as YouTube requires for
realistic AI people.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from .. import channels, config, db, logs, net
from ..research import dedup, engine, sources

TRENDS_RSS = "https://trends.google.com/trending/rss?geo={geo}"
HT = "{https://trends.google.com/trending/rss}"
MIN_OUTLETS = 2               # a story at least two different outlets report
RECENT_DAYS = 3               # a story this channel covered in the last 3 days is not repeated
SAME_STORY = 0.45
READABLE = re.compile(r"[\x00-\x7f\u0900-\u097f\u2010-\u203a\u20b9]+")   # Latin + Devanagari headlines
PRESENTER_ROLE = "news presenter"
PRESENTER_PROVIDER = "ai_presenter"

PRESENTER = {
    "name": "Aarav Mehra",
    "look": ("an Indian man in his early thirties, short neat black hair, light stubble, warm brown eyes, navy blue "
             "suit, white shirt, no tie, small lapel microphone"),
    "personality": "calm, clear and trustworthy; explains what happened, why it matters and what comes next",
}
STUDIO = ("modern TV news studio, blue and white lighting, large blurred city skyline screens behind, glossy anchor "
          "desk, photorealistic, real skin texture, 35mm broadcast camera, sharp focus on the presenter, no text, "
          "no logos, no watermark")


def is_news(ch: dict[str, Any] | None) -> bool:
    return bool(ch) and ch.get("workflow") == "factual" and "news" in channels.styles(ch)


# ------------------------------------------------------------------ today's stories

def _traffic(s: str) -> int:
    m = re.match(r"([\d,]+)\s*([KkMm]?)", s or "")
    if not m:
        return 0
    n = int(m.group(1).replace(",", ""))
    return n * {"k": 1000, "m": 1_000_000}.get(m.group(2).lower(), 1)


def parse_trends(xml_text: str) -> list[dict[str, Any]]:
    """Google Trends daily RSS -> [{keyword, traffic, published_at, articles: [{title, url, source}]}]."""
    root = ET.fromstring(xml_text)
    out = []
    for it in root.iter("item"):
        when = it.findtext("pubDate") or ""
        try:
            published = db.iso(parsedate_to_datetime(when).astimezone(timezone.utc))
        except (TypeError, ValueError):
            published = ""
        arts = [{"title": (n.findtext(f"{HT}news_item_title") or "").strip(),
                 "url": (n.findtext(f"{HT}news_item_url") or "").strip(),
                 "source": (n.findtext(f"{HT}news_item_source") or "").strip()} for n in it.iter(f"{HT}news_item")]
        out.append({"keyword": (it.findtext("title") or "").strip(),
                    "traffic": _traffic(it.findtext(f"{HT}approx_traffic") or ""), "published_at": published,
                    "articles": [a for a in arts if a["title"] and a["url"].startswith("http")]})
    return [t for t in out if t["keyword"]]


def trending(geo: str = "IN") -> list[dict[str, Any]]:
    _, text = net.get_text(TRENDS_RSS.format(geo=geo), respect_robots=False)
    return parse_trends(text)


def _recent_titles(channel_id: int) -> list[tuple[int, str]]:
    since = db.iso(datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS))
    rows = db.q("SELECT id, title, search_query FROM topics WHERE channel_id=? AND created_at>=?", (channel_id, since))
    return [(r["id"], f"{r['title']} {r['search_query'] or ''}") for r in rows]


def pick(ch: dict[str, Any], stories: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """The most-searched story today that ≥2 outlets report and this channel hasn't covered in the last days."""
    stories = trending() if stories is None else stories
    done = _recent_titles(ch["id"])
    for s in sorted(stories, key=lambda s: -s["traffic"]):
        s = {**s, "articles": [a for a in s["articles"] if READABLE.fullmatch(a["title"])]}   # English/Hindi reports
        outlets = {a["source"] for a in s["articles"] if a["source"]}
        if len(outlets) < MIN_OUTLETS:
            continue
        text = f"{s['keyword']} {s['articles'][0]['title']}"
        if done and dedup.best_match(text, done)[1] >= SAME_STORY:
            continue
        if any(s["keyword"].lower() in t.lower() for _, t in done):
            continue
        return s
    return None


def create_topic(ch: dict[str, Any], story: dict[str, Any]) -> int:
    """A topic for the story with today's articles attached as its sources."""
    lead = story["articles"][0]["title"]
    outlets = sorted({a["source"] for a in story["articles"] if a["source"]})
    today = datetime.now().astimezone().strftime("%d %B %Y")
    tid = db.insert("topics", {
        "channel_id": ch["id"], "title": lead[:110], "format": "long", "hook_idea": "", "status": "approved",
        "angle": (f"Today's trending news ({today}): {story['keyword']}. Report only what these outlets confirm: "
                  f"{', '.join(outlets)}. Say what happened, why people are talking about it, and what happens next. "
                  "Neutral and factual; name the outlets; never guess, never add numbers the sources don't give."),
        "why_now": f"trending in India today (~{story['traffic']:,}+ searches)", "score": min(100, story["traffic"] // 1000),
        "idea_kind": "news", "style": "news", "language": ch.get("language") or "Hinglish",
        "summary": " | ".join(f"{a['source']}: {a['title']}" for a in story["articles"][:6]),
        "search_query": story["keyword"][:200], "norm_title": dedup.normalize(lead),
        "workspace_id": ch["workspace_id"], "created_at": db.iso(), "updated_at": db.iso()})
    run = engine.start_run("topic", f"news: {story['keyword']}")
    items = []
    for a in story["articles"][:6]:
        e = {"source_type": "news", "url": a["url"], "title": a["title"], "published_at": story["published_at"],
             "snippet": a["source"], "content": "", "metrics": {"trend": story["keyword"], "traffic": story["traffic"]}}
        try:
            title, body = sources.page_text(a["url"])
            if len(body) > 400:
                e["content"] = body
        except net.FetchError as err:
            e["snippet"] += f" [full text not fetched: {err}]"[:200]
        items.append(e)
    saved = engine.save_evidence(run, items, topic_id=tid)
    engine.finish_run(run, "trending news story", {"google_trends": len(saved)})
    db.update("topics", tid, {"evidence_ids": db.dumps([e["id"] for e in saved])})
    return tid


# ------------------------------------------------------------------ presenter

def presenter(channel_id: int) -> dict[str, Any] | None:
    r = db.q1("SELECT * FROM characters WHERE channel_id=? AND role=? AND status='approved' ORDER BY id LIMIT 1",
              (channel_id, PRESENTER_ROLE))
    if not r:
        return None
    r["design"] = db.loads(r.get("design"), {}) or {}
    return r


def presenter_pictures(channel_id: int) -> dict[str, Path]:
    """{'long': 16:9 studio picture, 'short': 9:16} — whichever exist."""
    p = presenter(channel_id)
    if not p:
        return {}
    d = p["design"]
    out = {k: Path(d[k]) for k in ("picture_long", "picture_short") if d.get(k) and Path(d[k]).exists()}
    return {("long" if k == "picture_long" else "short"): v for k, v in out.items()}


def ensure_presenter(channel_id: int, gen=None) -> dict[str, Any]:
    """Create the channel's presenter once (fictional person, photo-real studio pictures from the free image chain)."""
    from . import characters
    have = presenter(channel_id)
    folder = config.DATA_DIR / "channels" / f"{channel_id:04d}" / "characters"
    folder.mkdir(parents=True, exist_ok=True)
    if gen is None:
        from .film import _gen as gen
    if have and presenter_pictures(channel_id).keys() == {"long", "short"}:
        return have
    base = f"Photorealistic TV news presenter: {PRESENTER['look']}"
    long_ = folder / "presenter_long.png"
    short = folder / "presenter_short.png"
    gen(f"{base}, seated at the anchor desk, medium shot from the waist up, looking into the camera with a calm "
        f"confident expression, {STUDIO}", long_, size=(1024, 576))
    gen(f"{base}, standing, vertical medium shot from the waist up, centred, looking into the camera, hands relaxed, "
        f"{STUDIO}", short, size=(576, 1024))
    design = {"look": PRESENTER["look"], "style": "photoreal", "picture_long": str(long_), "picture_short": str(short)}
    if have:
        db.update("characters", have["id"], {"design": db.dumps(design), "reference_path": str(long_),
                                             "updated_at": db.iso()})
        return presenter(channel_id)
    cid = db.insert("characters", {
        "channel_id": channel_id, "name": PRESENTER["name"], "role": PRESENTER_ROLE, "design": db.dumps(design),
        "personality": PRESENTER["personality"], "voice": db.dumps(characters._pick_voice(channel_id, characters.normalise_design({"gender": "male", "age": "adult"}))),
        "catchphrases": "[]", "relationships": "", "status": "approved", "reference_path": str(long_),
        "rules": "A fictional AI presenter. Never claim to be a real journalist; videos showing him are disclosed "
                 "as altered/synthetic content.", "created_at": db.iso(), "updated_at": db.iso()})
    logs.event("info", "news", f"News presenter '{PRESENTER['name']}' created for channel #{channel_id} (#{cid})")
    return presenter(channel_id)


def presenter_asset(p: dict[str, Any], idx: int, work: Path) -> dict[str, Any] | None:
    """The presenter picture as the opening (and closing) visual of a news video on a channel that has one."""
    ch = channels.get(p.get("channel_id"))
    if not is_news(ch):
        return None
    last = len((p.get("package") or {}).get("scenes") or []) - 1
    if idx not in (0, last):
        return None
    pic = presenter_pictures(ch["id"]).get("short" if p["format"] == "short" else "long")
    if not pic:
        return None
    from PIL import Image
    with Image.open(pic) as im:
        w, h = im.size
    target = work / f"presenter_{idx:03d}{pic.suffix}"
    if not target.exists():
        target.write_bytes(pic.read_bytes())
    return {"scene_index": idx, "kind": "image", "provider": PRESENTER_PROVIDER, "provider_id": f"presenter{idx}",
            "source_url": "", "page_url": "", "local_path": str(target), "license": "AI-generated (own presenter)",
            "license_url": "", "author": "AI presenter (fictional)", "attribution": "", "width": w, "height": h,
            "duration": None}
