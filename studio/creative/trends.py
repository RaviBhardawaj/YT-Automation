"""Daily trend research for creative channels (kids, comedy …): learn what is working, then make ORIGINAL episodes.

Reads the most-viewed recent videos in the channel's niche through the official YouTube Data API — titles, view
counts, lengths and channels only; no video is ever downloaded or re-used. It measures what those videos have in
common (themes, length, title and hook style) and asks the writer for new episodes with this channel's own
characters on those themes. Ideas whose titles come too close to a trending video's title are dropped, so the
channel copies ideas-level patterns, never anyone's video, characters, title or plot.
"""
from __future__ import annotations

import re
import statistics
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from .. import ai, channels, db, logs, net, profile, settings
from ..research import dedup, sources

REGION = "IN"
QUERIES = {
    "Hindi": {"long": ["hindi cartoon kahani", "hindi comedy cartoon", "bachon ki kahani cartoon"],
              "short": ["hindi cartoon shorts", "funny cartoon hindi"]},
    "English": {"long": ["kids cartoon story", "funny cartoon for kids"], "short": ["kids cartoon shorts"]},
}
TOO_CLOSE = 0.5                 # an idea this similar to a trending title is dropped (closer than "same theme")
UNITS_PER_SEARCH = 102          # search (100) + videos (1) + channels (1)


class Theme(BaseModel):
    theme: str = Field(description="a general theme or situation type, e.g. 'a pet causing chaos at home' — never a "
                                   "specific video's title, character or plot")
    why_it_works: str = Field(description="why children enjoy this, in one sentence")
    videos: int = Field(ge=1, description="how many of the listed videos share this theme")


class TrendPatterns(BaseModel):
    themes: list[Theme] = Field(description="3 to 6 themes, most common first")
    title_style: str = Field(description="how the top titles are written (length, words, emotion, emoji) — general")
    hook_style: str = Field(description="what the strongest openings seem to promise in the first seconds")
    what_we_are_missing: list[str] = Field(description="2 to 5 concrete differences between the trending videos and "
                                                       "this channel's own recent videos")
    avoid: list[str] = Field(default_factory=list, description="things to stay away from (copying, unsafe content)")


KIDS_RULES = ("This is a MADE-FOR-KIDS channel (ages 4–12). YouTube ranks children's videos by quality: no clickbait or "
              "sensational words (shocking, danger, secret, kidnapping, scary truth), no fear, violence, crime against "
              "children, or adults' relationship fights; at most 2 emoji; titles under 70 characters that honestly say "
              "what happens. Prefer warmth, curiosity, silly surprises, kindness and a clear funny payoff. Learn the "
              "themes and energy of popular videos, not their sensational packaging.")


# sensational words that make a children's video look low-quality to YouTube (English, Hindi and Hinglish)
CLICKBAIT = re.compile(r"(?i)shock|danger|secret|kidnap|scary|horror|खतरनाक|खौफनाक|भयानक|किडनैप|अपहरण|सच्चाई|डरावन|"
                       r"\bshort\b|शॉर्ट")


def kids(ch: dict[str, Any]) -> bool:
    return bool(ch.get("made_for_kids")) or "kids" in channels.styles(ch)


SYSTEM = ("You are a children's YouTube content strategist. You study what is popular to learn patterns and themes "
          "only. Never suggest copying, re-uploading, re-dubbing or closely imitating anyone's video, characters, "
          "titles, catchphrases or thumbnails. Output exactly one JSON object matching the schema, no commentary.")


STORY = "animated_story"


def eligible(ch: dict[str, Any] | None) -> bool:
    """Every active channel learns from what is popular in its niche — except the NIFTY channel, whose videos come
    from verified market data only, and the hidden holder for stand-alone videos."""
    return bool(ch) and ch["status"] == "active" and ch["workflow"] != "nifty_daily"


def latest(channel_id: int) -> dict[str, Any] | None:
    row = db.q1("SELECT * FROM trend_scans WHERE channel_id=? ORDER BY id DESC LIMIT 1", (channel_id,))
    if row:
        for k in ("videos", "stats", "patterns", "ideas"):
            row[k] = db.loads(row[k], {} if k in ("stats", "patterns") else [])
    return row


def _queries(ch: dict[str, Any]) -> dict[str, list[str]]:
    if ch["workflow"] == STORY and kids(ch):
        q = {k: list(v) for k, v in QUERIES.get(ch["language"] or "Hindi", QUERIES["English"]).items()}
        if ch.get("sub_niche"):
            q["long"].append(f"{ch['sub_niche'].split(',')[0].strip()} cartoon")
        return q
    niche = (ch.get("niche") or ch["name"]).strip()
    sub = (ch.get("sub_niche") or "").split(",")[0].strip()
    hindi = (ch.get("language") or "Hindi") != "English"
    long = [niche] + ([sub] if sub else []) + ([f"{niche} hindi"] if hindi else [])
    return {"long": long[:3], "short": [f"{niche} shorts"]}


def _search(ch: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    found: dict[str, dict[str, Any]] = {}
    units = 0
    for fmt, queries in _queries(ch).items():
        for q in queries:
            for days in (7, 30):                         # widen only when the last week is too thin
                rows = sources.youtube_search(q, ch["language"] or "Hindi", REGION, days=days, limit=15,
                                              order="viewCount", duration="short" if fmt == "short" else "medium")
                units += UNITS_PER_SEARCH
                if len(rows) >= 8 or days == 30:
                    break
            for r in rows:
                m = r["metrics"]
                when = db.parse_iso(r.get("published_at"))
                age = max(1.0, (datetime.now(timezone.utc) - when).total_seconds() / 86400) if when else 30.0
                found.setdefault(r["url"], {
                    "title": r["title"], "url": r["url"], "channel": m["channel"], "channel_url": m["channel_url"],
                    "views": m["views"], "views_per_day": round(m["views"] / age), "seconds": m["duration_s"],
                    "format": "short" if m["duration_s"] <= 60 else "long", "published_at": r["published_at"],
                    "subscribers": m.get("channel_subscribers")})
    top = sorted(found.values(), key=lambda v: -v["views_per_day"])[:30]
    return top, units


def _stats(videos: list[dict[str, Any]]) -> dict[str, Any]:
    longs = [v for v in videos if v["format"] == "long"]
    shorts = [v for v in videos if v["format"] == "short"]
    med = lambda xs: round(statistics.median(xs)) if xs else None
    return {"videos": len(videos), "long": len(longs), "short": len(shorts),
            "median_long_seconds": med([v["seconds"] for v in longs]),
            "median_short_seconds": med([v["seconds"] for v in shorts]),
            "median_views_per_day": med([v["views_per_day"] for v in videos]),
            "median_title_chars": med([len(v["title"]) for v in videos]),
            "channels": len({v["channel"] for v in videos})}


def _own_recent(channel_id: int) -> str:
    rows = db.q("SELECT p.title, p.format, a.metrics FROM productions p JOIN analytics a ON a.production_id=p.id "
                "WHERE p.channel_id=? AND a.id IN (SELECT MAX(id) FROM analytics GROUP BY production_id) "
                "ORDER BY p.id DESC LIMIT 15", (channel_id,))
    out = []
    for r in rows:
        m = db.loads(r["metrics"], {}) or {}
        out.append(f"- [{r['format']}] {r['title']} — {m.get('views', 0)} views, "
                   f"{round(m.get('averageViewPercentage') or 0)}% watched on average")
    return "\n".join(out) or "- no published results yet"


def analyse(ch: dict[str, Any], videos: list[dict[str, Any]], stats: dict[str, Any]) -> TrendPatterns:
    listing = "\n".join(f"- [{v['format']}, {v['seconds'] // 60}:{v['seconds'] % 60:02d}, {v['views_per_day']:,} views/day] "
                        f"{v['title']}" for v in videos[:30])
    prompt = (f"CHANNEL: {ch['name']} — {ch['niche']} ({ch.get('sub_niche') or '-'}); audience: "
              f"{ch.get('audience') or 'children'}; language: {ch['language']}\n\n"
              f"MOST-VIEWED RECENT VIDEOS IN THIS NICHE ({REGION}):\n{listing}\n\n"
              f"MEASURED: median long video {stats.get('median_long_seconds')} s, median Short "
              f"{stats.get('median_short_seconds')} s, median title {stats.get('median_title_chars')} characters.\n\n"
              f"THIS CHANNEL'S OWN RECENT VIDEOS AND RESULTS:\n{_own_recent(ch['id'])}\n\n"
              "Describe the general themes and styles that make these videos popular, and what this channel is "
              "missing compared with them. Themes must be general situation types an original story could use — "
              "not any listed video's plot, title or characters."
              + (f"\n\n{KIDS_RULES} Leave out themes and 'missing' points that break these rules." if kids(ch) else ""))
    out, _ = ai.generate("ideation", SYSTEM, prompt, TrendPatterns, max_tokens=3000)
    return out


def scan(channel_id: int) -> dict[str, Any]:
    """One trend scan for a channel (about 500–1,000 of the 10,000 free daily YouTube API units)."""
    ch = channels.get(channel_id)
    if not eligible(ch):
        raise ValueError("trend research is for active channels (not the NIFTY data channel)")
    if not settings.has_secret("youtube_api_key"):
        raise ValueError("Add a free YouTube Data API key under Settings → Credentials first.")
    videos, units = _search(ch)
    stats = _stats(videos)
    patterns = analyse(ch, videos, stats).model_dump() if videos else {}
    db.insert("trend_scans", {"channel_id": ch["id"], "workspace_id": ch["workspace_id"],
                                    "videos": db.dumps(videos), "stats": db.dumps(stats),
                                    "patterns": db.dumps(patterns), "ideas": "[]", "units": units,
                                    "created_at": db.iso()})
    logs.event("info", "ideas", f"'{ch['name']}': trend scan of {len(videos)} popular videos "
                                f"({len(patterns.get('themes', []))} themes, ~{units} API units)")
    return latest(ch["id"])


def focus_text(scan_row: dict[str, Any]) -> str:
    pt, st = scan_row["patterns"], scan_row["stats"]
    themes = "; ".join(f"{t['theme']} ({t['why_it_works']})" for t in pt.get("themes", []))
    return (f"THIS WEEK'S POPULAR THEMES in this niche: {themes}. Title style that works: {pt.get('title_style')}. "
            f"Opening style that works: {pt.get('hook_style')}. Typical length: long about "
            f"{round((st.get('median_long_seconds') or 240) / 60)} min, Shorts about {st.get('median_short_seconds') or 40} s. "
            f"What this channel has been missing: {'; '.join(pt.get('what_we_are_missing', []))}. "
            + ("Write ORIGINAL episodes with THIS channel's own recurring characters on these themes. "
               if scan_row.get("story", True) else "Propose ORIGINAL videos for THIS channel on these themes, with a "
               "fresh angle and real facts. ")
            + "Never reuse or closely paraphrase any popular video's title, characters, plot, catchphrases or "
            "thumbnails. Each Short must be complete on its own (hook and payoff in under a minute), never a cut-out "
            "piece. "
            "Do not put the word 'Short' in titles." + (f" {KIDS_RULES}" if scan_row.get("kids") else ""))


def ideas_from(scan_row: dict[str, Any], count: int = 6) -> dict[str, Any]:
    """Original ideas on this week's themes; any idea too close to a trending title is dropped."""
    from . import ideas
    ch = channels.get(scan_row["channel_id"])
    scan_row = {**scan_row, "kids": kids(ch), "story": ch["workflow"] == STORY}
    with channels.using(ch["id"]), profile.using_language(ch["language"] or "Hindi"):
        if ch["workflow"] == STORY:
            res = ideas.generate(ch["id"], count, channels.formats(ch), focus=focus_text(scan_row))
        else:                                    # factual channels: researched, source-checked topics on the themes
            from ..research import engine
            res = engine.run_ideation({"channel_id": ch["id"], "count": count, "formats": channels.formats(ch),
                                       "focus": focus_text(scan_row)[:600]})
    trending = [(i, v["title"]) for i, v in enumerate(scan_row["videos"])]
    kept, dropped = [], []
    for tid in res["created"]:
        t = db.q1("SELECT title FROM topics WHERE id=?", (tid,))
        _, sim = dedup.best_match(t["title"], trending) if trending else (None, 0.0)
        if sim >= TOO_CLOSE or (scan_row["kids"] and CLICKBAIT.search(t["title"])):
            db.ex("DELETE FROM topics WHERE id=?", (tid,))
            dropped.append(t["title"])
            continue
        db.ex("UPDATE topics SET why_now=? WHERE id=?", ("this week's popular theme (original story)", tid))
        kept.append(tid)
    db.ex("UPDATE trend_scans SET ideas=? WHERE id=?", (db.dumps(kept), scan_row["id"]))
    if dropped:
        logs.event("info", "ideas", f"'{ch['name']}': {len(dropped)} idea(s) dropped (too close to a popular "
                                    f"video's title, or clickbait on a kids' channel)")
    return {"created": kept, "dropped_too_close": dropped, "duplicates_skipped": res.get("duplicates_skipped", 0)}


def run_daily() -> dict[str, Any]:
    """Scan every active channel once a day and add original trend-based ideas to its list."""
    if not settings.get("trends.enabled"):
        return {"skipped": "trend research switched off"}
    done = {}
    for ch in db.q("SELECT id FROM channels WHERE status='active' AND workflow<>'nifty_daily'"):
        last = latest(ch["id"])
        if last and (db.utcnow() - db.parse_iso(last["created_at"])).total_seconds() < 20 * 3600:
            done[ch["id"]] = "already scanned today"
            continue
        try:
            row = scan(ch["id"])
            done[ch["id"]] = ideas_from(row) if row["videos"] else "no popular videos found"
        except (ValueError, net.FetchError) as e:
            logs.event("warning", "ideas", f"trend research skipped: {e}")
            done[ch["id"]] = str(e)
    return done
