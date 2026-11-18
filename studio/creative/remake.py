"""Trending-story remakes: each day, retell the story idea behind today's most-viewed video with this channel's own
characters (or new characters made for that one script) — one long episode and one Short a day.

How it stays original (copyright and YouTube's "reused / unoriginal content" rule):
- Only public metadata is read through the official YouTube Data API: the "most popular" chart (1 unit per
  category) and the niche's daily trend scan. No video, audio, thumbnail or transcript is downloaded or re-used.
- The writer takes the PREMISE (the situation and emotion that make it work, in general words) and builds a new
  story on it: new characters or our cast, a new setting, new scenes, jokes, dialogue and ending. It must list at
  least three concrete differences from the source.
- A title too close to the source title is rejected; on kids channels clickbait words are rejected too.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from .. import ai, channels, db, logs, net, settings
from ..research import dedup, sources
from . import characters, trends

# YouTube video categories whose popular chart holds retell-able stories (Film & Animation, Comedy, Entertainment)
CATEGORIES = {"1": "Film & Animation", "23": "Comedy", "24": "Entertainment"}
FRESH_DAYS = 3                    # a video older than this is not "today's" trend
MAX_CANDIDATES = 25
DEFAULT_TIMES = "09:00, 18:00"    # long video in the morning, its Short in the evening
KIND = "trend_remake"
WHY = "today's trending story, retold originally"


def enabled(ch: dict[str, Any] | None) -> bool:
    """Trend remakes are on for this channel (a story channel that switched them on)."""
    from . import news
    return (bool(ch) and bool((ch.get("meta") or {}).get("trend_remake")) and trends.eligible(ch)
            and ch["workflow"] in ("animated_story", "auto") and not news.is_news(ch))


def regions(ch: dict[str, Any]) -> list[str]:
    r = (ch.get("meta") or {}).get("remake_regions")
    if r:
        return [str(x).upper()[:2] for x in r][:3]
    return ["IN"] if (ch.get("language") or "Hindi") != "English" else ["IN", "US"]


def enable(channel_id: int, on: bool = True, times: str | None = None, actor: str = "user") -> dict[str, Any]:
    """Switch the channel to (or off) the daily trending-story remake: exactly one long video and one Short a day."""
    from .. import scheduler
    ch = channels.get(channel_id)
    if not ch:
        raise ValueError("channel not found")
    if on and (ch["workflow"] not in ("animated_story", "auto") or not trends.eligible(ch)):
        raise ValueError("trending-story remakes are for animated story channels (kids, comedy …)")
    db.update("channels", channel_id, {"meta": db.dumps({**(ch.get("meta") or {}), "trend_remake": bool(on)}),
                                       "updated_at": db.iso()})
    if on:
        slots = [t.strip() for t in (times or DEFAULT_TIMES).split(",") if t.strip()][:2]
        if len(slots) != 2:
            raise ValueError("give two times, e.g. 09:00, 18:00 (long video, then its Short)")
        channels.update(channel_id, {"formats": "both", "schedule_enabled": True, "schedule_time": ", ".join(slots),
                                     "status": "active"}, actor=actor)
        scheduler.sync_channels()
    logs.event("info", "planner", f"'{ch['name']}': trending-story remakes switched {'on' if on else 'off'}")
    return {"trend_remake": bool(on), "schedule_time": channels.get(channel_id)["schedule_time"]}


# ------------------------------------------------------------------ discovery

def _used_sources(channel_id: int) -> set[str]:
    out = set()
    for r in db.q("SELECT gap FROM topics WHERE channel_id=? AND idea_kind=?", (channel_id, KIND)):
        src = ((db.loads(r["gap"], {}) or {}).get("remake") or {}).get("source") or {}
        if src.get("url"):
            out.add(src["url"])
    return out


def _views_per_day(views: int, published_at: str | None) -> tuple[int, float]:
    when = db.parse_iso(published_at)
    age = max(0.25, (datetime.now(timezone.utc) - when).total_seconds() / 86400) if when else 30.0
    return round(views / age), age


def candidates(ch: dict[str, Any]) -> list[dict[str, Any]]:
    """Today's most-viewed videos that could carry a story for this channel, fastest-growing first."""
    found: dict[str, dict[str, Any]] = {}
    for region in regions(ch):
        for cat in CATEGORIES:
            try:
                rows = sources.youtube_most_popular(region, cat)
            except net.FetchError as e:               # some regions have no chart for a category
                logs.event("info", "ideas", f"popular chart {region}/{CATEGORIES[cat]} skipped: {str(e)[:120]}")
                continue
            for v in rows:
                found.setdefault(v["url"], {**v, "from": f"popular {region} {CATEGORIES[cat]}"})
    scan = trends.latest(ch["id"])                  # the niche's own trend scan, when it ran in the last day
    if scan and (db.utcnow() - db.parse_iso(scan["created_at"])).total_seconds() < 26 * 3600:
        for v in scan["videos"]:
            found.setdefault(v["url"], {"id": v["url"].rsplit("=", 1)[-1], "url": v["url"], "title": v["title"],
                                        "description": "", "channel": v.get("channel", ""),
                                        "published_at": v.get("published_at"), "views": v.get("views", 0),
                                        "seconds": v.get("seconds", 0), "from": "niche trend scan"})
    used = _used_sources(ch["id"])
    kids = trends.kids(ch)
    out = []
    for v in found.values():
        vpd, age = _views_per_day(v["views"], v.get("published_at"))
        if v["url"] in used or age > FRESH_DAYS:
            continue
        if kids and trends.CLICKBAIT.search(v["title"]):
            continue
        out.append({**v, "views_per_day": vpd})
    return sorted(out, key=lambda v: -v["views_per_day"])[:MAX_CANDIDATES]


# ------------------------------------------------------------------ adaptation

class Episode(BaseModel):
    title: str = Field(description="our own title in the channel language, under 60 characters, clearly different "
                                   "from the source title")
    logline: str = Field(description="two or three sentences: who wants what, what goes wrong, the twist, the payoff")
    hook: str = Field(description="what is seen and heard in the first 3 seconds")


class RemakePlan(BaseModel):
    source_index: int = Field(description="index (from the list) of the trending video whose story idea you retell")
    premise: str = Field(description="the general story idea in one sentence — a situation and emotion anyone could "
                                     "write about, with no names, catchphrases or specific scenes from the source")
    why_it_trends: str = Field(description="why this idea is getting views today, one sentence")
    use_cast: bool = Field(description="true = the channel's recurring characters fit this story; false = the "
                                       "story needs its own new characters (made only for this video)")
    new_characters: list[str] = Field(default_factory=list,
                                      description="if use_cast is false: 1-4 short descriptions of the new characters")
    changes: list[str] = Field(description="at least 3 concrete ways our story differs from the source (setting, "
                                           "characters, middle, twist, ending, jokes)")
    long: Episode = Field(description="the full-length episode")
    short: Episode = Field(description="a complete under-60-second Short on the same premise: the funniest or most "
                                       "surprising moment as its own mini-story, ending so viewers want the full "
                                       "episode — never a cut-out piece")


SYSTEM = ("You are the head writer of an animated YouTube series. Each day you take the story IDEA behind one of "
          "today's most popular videos and retell it as a brand-new episode for your own channel. Ideas and premises "
          "are free to use; expression is not: never copy or closely paraphrase another video's title, characters, "
          "names, dialogue, catchphrases, scene order, thumbnail or ending. Skip news, music videos, trailers, real "
          "people, sports and anything unsafe for the audience. Output exactly one JSON object matching the schema.")


# YouTube's made-for-kids rules on top of trends.KIDS_RULES (COPPA: comments, notifications and personalised ads are
# off for these videos; YouTube's quality principles for kids content)
KIDS_EXTRA = ("KIDS CHANNEL RULES: ages 2-8; warm, safe, kind; a gentle lesson or a happy ending is welcome. Never ask "
              "viewers to comment, like, subscribe or press the bell; never ask for or show personal information; no "
              "dangerous acts children could copy, no scary, violent or mean-spirited scenes, no brands, real people, "
              "toys-unboxing or ads; no fake urgency. Plain titles a parent would search (cartoon, kahani, rhyme, "
              "song) with the character's name. Only pick a source story that can be retold this way.")
KIDS_BANNED = re.compile(r"(?i)subscribe|comment|like karo|bell|सब्सक्राइब|कमेंट")


def adapt(ch: dict[str, Any], cands: list[dict[str, Any]]) -> RemakePlan:
    cast = characters.for_channel(ch["id"], include_draft=False)
    listing = "\n".join(
        f"[{i}] {v['title']} — {v['views_per_day']:,} views/day, {v['seconds'] // 60}:{v['seconds'] % 60:02d}"
        + (f"\n    about: {' '.join(v['description'].split())[:300]}" if v.get("description") else "")
        for i, v in enumerate(cands))
    kids = trends.kids(ch)
    prompt = (f"CHANNEL: {ch['name']} — {ch['niche']} ({ch.get('sub_niche') or '-'}); audience: "
              f"{ch.get('audience') or ('children' if kids else 'general')}; language: {ch['language']}; "
              f"styles: {', '.join(channels.styles(ch)) or 'comedy'}; tone: {ch.get('tone') or '-'}\n"
              f"CHANNEL INSTRUCTIONS: {ch.get('instructions') or '-'}\nRESTRICTIONS: {ch.get('restrictions') or '-'}\n"
              f"RECURRING CHARACTERS:\n{characters.bible_text(cast)}\n\n"
              f"TODAY'S MOST-VIEWED VIDEOS (titles and descriptions only):\n{listing}\n\n"
              "Pick the ONE video whose story idea will get this channel the most views and that can be retold "
              "well as an animated story for this audience (prefer the highest views/day among the good fits). "
              "Write its premise in general words, then our own long episode and our own Short on that premise. "
              "Use the recurring characters when they fit; if they don't, set use_cast to false and describe new "
              f"characters. Titles and loglines in {ch['language']}."
              + (f"\n\n{trends.KIDS_RULES}\n{KIDS_EXTRA}" if kids else ""))

    def check(out: RemakePlan) -> None:
        if not 0 <= out.source_index < len(cands):
            raise ValueError(f"source_index must be between 0 and {len(cands) - 1}")
        src = cands[out.source_index]["title"]
        for ep in (out.long, out.short):
            if dedup.similarity(ep.title, src) >= trends.TOO_CLOSE:
                raise ValueError(f"title '{ep.title}' is too close to the source title — write our own")
            if kids and (trends.CLICKBAIT.search(ep.title) or KIDS_BANNED.search(ep.title + " " + ep.hook)):
                raise ValueError(f"title '{ep.title}' uses a clickbait word, not allowed on a kids channel")
        if dedup.similarity(out.long.title, out.short.title) >= dedup.DUPLICATE:
            raise ValueError("the Short needs its own title")
        if len([c for c in out.changes if c.strip()]) < 3:
            raise ValueError("list at least 3 concrete differences from the source")
        if not out.use_cast and not out.new_characters:
            raise ValueError("describe the new characters (or use the recurring cast)")

    out, _ = ai.generate("ideation", SYSTEM, prompt, RemakePlan, max_tokens=3000, validate=check)
    return out


def _topic(ch: dict[str, Any], plan: RemakePlan, ep: Episode, fmt: str, src: dict[str, Any]) -> int:
    style = next((s for s in channels.styles(ch) if s in channels.STYLES), "comedy")
    chars = [c["name"] for c in characters.for_channel(ch["id"], include_draft=False)] if plan.use_cast else []
    summary = (f"{ep.logline} PREMISE (retold originally, nothing copied): {plan.premise}. "
               + (f"NEW CHARACTERS for this video only: {'; '.join(plan.new_characters)}. " if not plan.use_cast else "")
               + f"MUST DIFFER FROM THE SOURCE: {'; '.join(plan.changes)}.")
    return db.insert("topics", {
        "channel_id": ch["id"], "title": ep.title.strip()[:200], "angle": ep.logline, "format": fmt,
        "hook_idea": ep.hook, "why_now": WHY, "status": "approved", "score": 80,
        "score_breakdown": db.dumps({"trend": {"points": 80, "max": 100, "kind": "measured",
                                               "basis": f"source getting {src['views_per_day']:,} views/day"}}),
        "claims": "[]", "evidence_ids": "[]", "search_query": ep.title[:200], "norm_title": dedup.normalize(ep.title),
        "gap": db.dumps({"characters": chars or plan.new_characters,
                         "remake": {"source": {k: src.get(k) for k in ("url", "title", "channel", "views_per_day",
                                                                       "from")},
                                    "premise": plan.premise, "why_it_trends": plan.why_it_trends,
                                    "changes": plan.changes, "use_cast": plan.use_cast}}),
        "idea_kind": KIND, "style": style, "language": ch["language"], "summary": summary[:2000],
        "workspace_id": ch["workspace_id"], "created_at": db.iso(), "updated_at": db.iso()})


def _today(channel_id: int) -> list[dict[str, Any]]:
    start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return db.q("SELECT id, format, status, gap FROM topics WHERE channel_id=? AND idea_kind=? AND created_at>=? "
                "ORDER BY id", (channel_id, KIND, db.iso(start)))


def make_today(channel_id: int) -> dict[str, Any]:
    """Today's remake (created once per day): {'long': topic id, 'short': topic id, 'use_cast', 'source', 'premise'}."""
    ch = channels.get(channel_id)
    rows = _today(channel_id)
    if rows:
        by = {r["format"]: r for r in rows}
        rm = (db.loads(rows[0]["gap"], {}) or {}).get("remake") or {}
        return {"long": by.get("long", {}).get("id"), "short": by.get("short", {}).get("id"),
                "use_cast": rm.get("use_cast", True), "source": rm.get("source"), "premise": rm.get("premise")}
    if not settings.has_secret("youtube_api_key"):
        raise ValueError("Add a free YouTube Data API key under Settings → Credentials to find today's trends.")
    cands = candidates(ch)
    if not cands:
        raise ValueError("no fresh trending video found today")
    plan = adapt(ch, cands)
    src = cands[plan.source_index]
    long_id = _topic(ch, plan, plan.long, "long", src)
    short_id = _topic(ch, plan, plan.short, "short", src)
    logs.event("info", "ideas", f"'{ch['name']}': today's trend remake — premise “{plan.premise[:120]}” "
                                f"(from a video at {src['views_per_day']:,} views/day; "
                                f"{'our cast' if plan.use_cast else 'new characters'})")
    return {"long": long_id, "short": short_id, "use_cast": plan.use_cast, "source": src, "premise": plan.premise}


def topic_for(channel_id: int, fmt: str) -> tuple[int | None, bool]:
    """(topic id, use_cast) for today's long or Short; (None, True) when no remake could be made."""
    try:
        today = make_today(channel_id)
    except (ValueError, net.FetchError, ai.AllProvidersFailed) as e:
        logs.event("warning", "ideas", f"trend remake skipped today: {str(e)[:200]} — using the idea list instead")
        return None, True
    tid = today.get(fmt)
    if tid and db.q1("SELECT status FROM topics WHERE id=?", (tid,))["status"] == "producing":
        return None, True                                   # already made today
    return tid, bool(today.get("use_cast", True))
