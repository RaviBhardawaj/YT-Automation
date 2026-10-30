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

# YouTube Partner Program rules as read on the official pages — with where and when, never assumed current.
# Re-check them (docs/CHANNEL_AUTOMATION_AND_MONETIZATION.md says how) — YouTube Studio → Earn is the authority.
RULES = {
    "checked": "2026-10-30",
    "sources": {"ypp_overview": "https://support.google.com/youtube/answer/72851",
                "monetization_policies": "https://support.google.com/youtube/answer/1311392",
                "api_terms": "https://developers.google.com/youtube/terms/api-services-terms-of-service"},
    "notes": ["Full tier verified on the overview page on 2026-10-30: 1,000 subscribers AND either 4,000 qualified "
              "watch hours from public long videos in 12 months, or 10 million qualified Shorts views in 90 days.",
              "The overview page announces YPP updates from 1 Feb 2027; the updated terms must be accepted in "
              "YouTube Studio by 31 Jan 2027.",
              "The earlier-access (fan funding) thresholds are NOT stated on the overview page as checked — the "
              "numbers below are from earlier YouTube announcements and are shown as unverified.",
              "Monetization policies (checked 2026-10-30): reused content, inauthentic (mass-produced/repetitive) "
              "content, and 'AI personas related to sensitive topics' (AI hosts presenting as experts on health, "
              "legal, financial or political advice) are not monetizable."],
}
TIERS = {
    "early": {"label": "Earlier access (fan funding: memberships, Super Thanks)", "subscribers": 500,
              "public_uploads_90d": 3, "watch_hours_12m": 3000, "shorts_views_90d": 3_000_000, "verified": False},
    "full": {"label": "Full Partner Program (ad revenue)", "subscribers": 1000, "watch_hours_12m": 4000,
             "shorts_views_90d": 10_000_000, "verified": True},
}
OWNER_STATUSES = ("applied", "accepted", "rejected")    # only the owner can know these (no API reports them)
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
    met = subs_ok and uploads_ok and (hours_pct >= 100 or shorts_pct >= 100)
    # "eligible" kept for older pages: it means the measured numbers reach the thresholds — NOT that YouTube
    # accepted the channel (YouTube reviews it after the owner applies).
    return {"subscribers_pct": round(min(100, subs / tier["subscribers"] * 100), 1), "subscribers_ok": subs_ok,
            "uploads_ok": uploads_ok, "watch_pct": round(hours_pct, 1), "shorts_pct": round(shorts_pct, 1),
            "eligible": met, "requirements_met": met, "verified_thresholds": bool(tier.get("verified"))}


def status(ch: dict[str, Any]) -> dict[str, str]:
    """Honest monetization status: unknown → in progress → requirements met (YouTube must still review) →
    applied / accepted / rejected only when the owner records it (no API reports a YPP decision)."""
    m = ch.get("monetization") or {}
    if isinstance(m, str):
        m = db.loads(m, {}) or {}
    own = (m.get("owner_status") or {})
    if own.get("status") in OWNER_STATUSES:
        return {"key": own["status"], "label": {"applied": "applied — waiting for YouTube's review",
                                                "accepted": "accepted by YouTube (recorded by you)",
                                                "rejected": "not accepted by YouTube (recorded by you)"}[own["status"]],
                "since": own.get("at", "")}
    if not m.get("fetched_at"):
        return {"key": "unknown", "label": "unknown — numbers not loaded yet", "since": ""}
    full = (m.get("tiers") or {}).get("full") or {}
    if full.get("requirements_met"):
        return {"key": "requirements_met", "label": "requirements met — apply in YouTube Studio → Earn; YouTube "
                "reviews the channel (eligibility is not acceptance)", "since": m["fetched_at"]}
    return {"key": "in_progress", "label": "in progress", "since": m["fetched_at"]}


def set_owner_status(channel_id: int, value: str, actor: str) -> None:
    if value not in (*OWNER_STATUSES, ""):
        raise ValueError("status must be applied, accepted, rejected or empty")
    ch = channels.get(channel_id)
    m = (ch or {}).get("monetization") or {}
    if isinstance(m, str):
        m = db.loads(m, {}) or {}
    m["owner_status"] = {"status": value, "at": db.iso(), "by": actor} if value else {}
    db.update("channels", channel_id, {"monetization": db.dumps(m), "updated_at": db.iso()})
    from .. import ops
    ops.audit(actor, "monetization_status", channel_id=channel_id, detail={"status": value or "cleared"})


SENSITIVE = ("finance", "financ", "stock", "nifty", "market", "trading", "invest", "politic", "election", "health",
             "medical", "legal", "law", "news")


def policy_readiness(channel_id: int, days: int = 60) -> list[dict[str, Any]]:
    """Checks against YouTube's monetization policies, from this channel's own recent uploads. Each is pass / warn /
    fail / not_checked with the evidence — a pass here is our check, not YouTube's decision."""
    from ..research import dedup
    ch = channels.get(channel_id) or {}
    ups = db.q("SELECT id, title, kind, qa, package FROM productions WHERE channel_id=? AND youtube_video_id IS NOT "
               "NULL AND uploaded_at>=? ORDER BY id DESC LIMIT 30", (channel_id, db.iso_in(-days * 86400)))
    out: list[dict[str, Any]] = []

    def add(name: str, state: str, detail: str, policy: str = "") -> None:
        out.append({"name": name, "state": state, "detail": detail, "policy": policy})
    if not ups:
        add("recent uploads", "not_checked", f"no uploads in the last {days} days to check")
        return out
    ids = [u["id"] for u in ups]
    marks = ",".join("?" * len(ids))
    presenter = db.q1(f"SELECT COUNT(DISTINCT production_id) AS n FROM assets WHERE production_id IN ({marks}) AND "
                      "provider IN ('ai_presenter')", tuple(ids))["n"]
    topic = " ".join([ch.get("niche") or "", ch.get("sub_niche") or "", " ".join(ch.get("styles") or []),
                      ch.get("workflow") or ""]).lower()
    sensitive = [w for w in SENSITIVE if w in topic]
    if presenter and sensitive:
        add("AI persona on a sensitive topic", "warn",
            f"{presenter} of {len(ups)} recent uploads use a realistic AI presenter, and the channel covers "
            f"'{sensitive[0]}' topics. YouTube does not monetize AI personas presenting as experts giving health, "
            "legal, financial or political advice. Keep the presenter strictly reporting (no advice, no tips), "
            "consider a human voice/face, and decide before applying.", RULES["sources"]["monetization_policies"])
    elif presenter:
        add("AI persona on a sensitive topic", "pass", f"AI presenter used on {presenter} upload(s); channel topics "
            "are not health/legal/financial/political", RULES["sources"]["monetization_policies"])
    else:
        add("AI persona on a sensitive topic", "pass", "no realistic AI presenter in recent uploads",
            RULES["sources"]["monetization_policies"])
    synth = db.q1(f"SELECT COUNT(DISTINCT production_id) AS n FROM assets WHERE production_id IN ({marks}) AND "
                  "provider IN ('ai_presenter','hf_zerogpu','hf_ltx2','sadtalker_local')", tuple(ids))["n"]
    add("altered or synthetic content disclosed", "pass",
        f"{synth} upload(s) with realistic AI people/motion — each is sent to YouTube with 'altered or synthetic "
        "content' set automatically" if synth else "no realistic synthetic people or motion in recent uploads")
    pairs, close = 0, []
    for i, a in enumerate(ups[:15]):
        for b in ups[i + 1:15]:
            pairs += 1
            sim = dedup.similarity(a["title"] or "", b["title"] or "")
            if sim >= 0.8:
                close.append((a["id"], b["id"], round(sim, 2)))
    finance_daily = ch.get("workflow") == "nifty_daily"
    add("inauthentic / repetitive content", "pass" if not close else ("warn" if not finance_daily else "pass"),
        (f"{len(close)} of {pairs} pairs of recent titles are near-identical: "
         + ", ".join(f"#{a}~#{b}" for a, b, _ in close[:5]) if close else
         f"no near-identical titles among {min(15, len(ups))} recent uploads")
        + (" (daily dated market episodes share a title pattern by design; their content differs daily)"
           if close and finance_daily else ""), RULES["sources"]["monetization_policies"])
    copied = [u["id"] for u in ups if ((db.loads(u["package"], {}) or {}).get("originality") or {}).get("ok") is False]
    add("reused content", "pass" if not copied else "warn",
        f"{len(copied)} upload(s) failed the copied-wording check: {copied[:5]}" if copied else
        "every checked upload is written in its own words (7-word phrase check against its sources)",
        RULES["sources"]["monetization_policies"])
    flagged = [u["id"] for u in ups if any(c.get("name") == "editorial review" and not c.get("ok")
                                           for c in (db.loads(u["qa"], {}) or {}).get("checks", []))]
    add("metadata and facts", "pass" if not flagged else "warn",
        f"{len(flagged)} upload(s) went out with an open fact-check issue (approved by you): {flagged[:5]}" if flagged
        else "no upload went out with an open fact-check issue")
    if any(w in topic for w in ("kid", "child")):
        add("made for kids set", "pass" if ch.get("made_for_kids") else "warn",
            "set — every upload is marked made for kids" if ch.get("made_for_kids") else
            "this looks like a children's channel but 'made for kids' is off (Channel → Uploading & publishing)")
    return out


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
