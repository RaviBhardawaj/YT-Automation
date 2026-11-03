"""Growth doctor: why a channel isn't growing, from its OWN YouTube numbers, and what to change.

Only real, synced numbers are used (views, average % watched, average seconds watched, subscribers gained, traffic
sources, Shorts vs long). Each finding names its evidence. Some actions are applied automatically because they only
change how OUR next videos are made (never anything on YouTube, never engagement):
  * winners    — the daily video prefers ideas close to the channel's best-performing videos (tasks._channel_daily);
  * hook       — a hook lesson goes into the writer's instructions when viewers leave in the first seconds;
  * shorts     — every long video gets Shorts when Shorts bring most of the views.
Nothing here can promise subscribers or views. Buying subscribers/views, sub-for-sub, bots or fake engagement are
against YouTube's rules and get channels removed — the studio never does them.
"""
from __future__ import annotations

from typing import Any

from .. import channels, db, logs

MIN_VIEWS = 5                    # a video needs this many views before its watch % says anything
HOOK_PCT = 30                    # long videos: below this average % watched, the opening loses people
WINNER_MIN_VIEWS = 20


def _videos(channel_id: int) -> list[dict[str, Any]]:
    rows = db.q("SELECT p.id, p.title, p.format, p.uploaded_at, a.metrics FROM productions p JOIN analytics a "
                "ON a.production_id=p.id WHERE p.channel_id=? AND a.id IN (SELECT MAX(id) FROM analytics "
                "GROUP BY video_id)", (channel_id,))
    out = []
    for r in rows:
        m = db.loads(r["metrics"], {}) or {}
        age = max(1.0, (db.utcnow() - (db.parse_iso(r["uploaded_at"]) or db.utcnow())).total_seconds() / 86400)
        out.append({"id": r["id"], "title": r["title"] or "", "format": r["format"], "views": int(m.get("views") or 0),
                    "views_per_day": round(int(m.get("views") or 0) / age, 1),
                    "pct": m.get("averageViewPercentage"), "secs": m.get("averageViewDuration"),
                    "subs": m.get("subscribersGained"), "sources": m.get("traffic_sources") or {}})
    return out


def winners(channel_id: int, n: int = 3) -> list[dict[str, Any]]:
    """The channel's best videos so far (views per day, at least WINNER_MIN_VIEWS views)."""
    vids = [v for v in _videos(channel_id) if v["views"] >= WINNER_MIN_VIEWS]
    return sorted(vids, key=lambda v: -v["views_per_day"])[:n]


def diagnose(channel_id: int) -> dict[str, Any]:
    ch = channels.get(channel_id) or {}
    vids = _videos(channel_id)
    m = ch.get("monetization") or {}
    if isinstance(m, str):
        m = db.loads(m, {}) or {}
    findings: list[dict[str, Any]] = []

    def add(key: str, severity: str, title: str, evidence: str, action: str, auto: str = "") -> None:
        findings.append({"key": key, "severity": severity, "title": title, "evidence": evidence, "action": action,
                         "auto": auto})
    if not vids:
        add("no_data", "info", "Not enough data yet", "no synced video numbers for this channel",
            "Upload a few videos and refresh the numbers; the doctor needs real results to work from.")
        return {"channel": ch.get("name"), "findings": findings, "winners": [], "numbers": {}}
    views = sum(v["views"] for v in vids)
    zero = [v for v in vids if v["views"] <= 2]
    longs = [v for v in vids if v["format"] == "long" and v["views"] >= MIN_VIEWS and v["pct"] is not None]
    shorts_views = sum(v["views"] for v in vids if v["format"] == "short")
    subs = sum(int(v["subs"] or 0) for v in vids)
    src: dict[str, int] = {}
    for v in vids:
        for k, s in v["sources"].items():
            src[k] = src.get(k, 0) + int(s.get("views") or 0)
    numbers = {"videos": len(vids), "views": views, "zero_view_videos": len(zero), "shorts_share":
               round(shorts_views / views * 100) if views else None, "subs_gained": subs,
               "subscribers": m.get("subscribers"), "sources": src}

    weak = [v for v in longs if v["pct"] < HOOK_PCT]
    if weak:
        worst = sorted(weak, key=lambda v: v["pct"])[:3]
        add("hook", "high", "Viewers leave long videos in the first seconds",
            "; ".join(f"#{v['id']} watched {v['pct']:.0f}% ({v['secs']} s)" for v in worst),
            "Start with the payoff: the most surprising fact or moment in the first 3 seconds, no greeting or intro, "
            "a new visual every few seconds, and keep long videos short until people stay.",
            auto="hook lesson added to the writer's instructions")
    if views and shorts_views / views >= 0.6:
        add("shorts", "high", "Shorts bring most of your viewers",
            f"{round(shorts_views / views * 100)}% of views come from Shorts",
            "Make 2–3 Shorts from every long video and post at least one Short a day; end each Short pointing to "
            "the full video.", auto="every long video now gets Shorts")
    win = winners(channel_id)
    if win:
        add("winners", "high", "Some topics clearly work — make more of them",
            "; ".join(f"#{v['id']} '{v['title'][:45]}' {v['views']} views" for v in win),
            "Follow up on these topics and formats (part 2, next update, same style of title).",
            auto="daily videos now prefer ideas close to these")
    if len(vids) >= 8 and len(zero) / len(vids) >= 0.6:
        add("volume", "medium", "Many videos get almost no views",
            f"{len(zero)} of {len(vids)} videos have 0–2 views",
            "Fewer, better videos: one strong video a day beats several weak ones; YouTube shows a channel less when "
            "most of its videos get no reaction. Consider lowering the daily count in the channel's schedule.")
    if views >= 100 and subs == 0:
        add("subs", "medium", "Viewers watch but don't subscribe",
            f"{views} views, {subs} subscribers gained from them",
            "Give a reason to subscribe: a recurring series ('daily gold rate', 'Pappu ki kahani part 3'), a clear "
            "channel promise in the first seconds and at the end, and a consistent topic so viewers know what comes "
            "next.")
    suggested = src.get("RELATED_VIDEO", 0) + src.get("SUBSCRIBER", 0)
    if sum(src.values()) >= 30 and suggested / max(1, sum(src.values())) < 0.15:
        add("suggested", "medium", "YouTube isn't recommending the videos yet",
            f"suggested/subscriber views {suggested} of {sum(src.values())}; most come from Shorts feed or search",
            "Recommendations follow watch time: fix the hook first. Use search words people type in titles "
            "(e.g. 'Gold rate today') so search keeps bringing viewers meanwhile.")
    if ch.get("made_for_kids"):
        add("kids", "info", "Made for kids limits growth features",
            "this channel's videos are marked 'made for kids'",
            "YouTube turns off comments and notifications and limits personalised recommendations for kids' content. "
            "This label is required by law (COPPA) when videos are aimed at children — only change it if you "
            "genuinely change the audience to all ages (family comedy), never to work around the rule.")
    return {"channel": ch.get("name"), "findings": findings, "winners": win, "numbers": numbers}


DOCTOR_KIND = "doctor"
HOOK_LESSON = ("Open with the payoff: say the most surprising fact or show the funniest/most dramatic moment in the "
               "very first sentence — no greeting, no channel intro, no 'aaj hum baat karenge'. Change the visual "
               "every few seconds.")


def apply(channel_id: int) -> dict[str, Any]:
    """Apply the automatic actions for this channel's findings (only how the next videos are made)."""
    d = diagnose(channel_id)
    keys = {f["key"] for f in d["findings"]}
    ch = channels.get(channel_id) or {}
    done = []
    db.ex("UPDATE lessons SET status='old', updated_at=? WHERE channel_id=? AND kind=? AND status='active'",
          (db.iso(), channel_id, DOCTOR_KIND))
    if "hook" in keys:
        ev = next(f["evidence"] for f in d["findings"] if f["key"] == "hook")
        db.insert("lessons", {"channel_id": channel_id, "workspace_id": ch.get("workspace_id") or 1, "kind": DOCTOR_KIND,
                              "lesson": HOOK_LESSON, "evidence": ev, "confidence": "medium", "status": "active",
                              "videos_used": len(d["winners"]), "created_at": db.iso(), "updated_at": db.iso()})
        done.append("hook lesson")
    if "shorts" in keys and ch.get("formats") in ("long", "both") and int(ch.get("shorts_per_episode") or 0) < 2:
        channels.update(channel_id, {"shorts_per_episode": 3, "formats": "both"}, actor="growth doctor")
        done.append("3 Shorts per long video")
    if d["winners"]:
        done.append(f"{len(d['winners'])} winning topic(s) preferred for daily videos")
    if done:
        logs.event("info", "growth", f"'{ch.get('name')}': growth doctor applied — {', '.join(done)}")
    return {**d, "applied": done}


def apply_all() -> dict[str, Any]:
    out = {}
    for ch in channels.all_channels():
        try:
            out[ch["id"]] = apply(ch["id"])["applied"]
        except Exception as e:  # one channel's problem never stops the others
            out[ch["id"]] = f"failed: {str(e)[:120]}"
    return out
