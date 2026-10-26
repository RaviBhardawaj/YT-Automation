"""Publishing finished videos to Facebook and Instagram, one tracked post per platform.

Each (video, platform) pair has one social_posts row, so asking twice — or a retry after a crash — never posts
twice: a published row is final, and an Instagram container that was already uploaded is resumed, not re-made.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import channels, db, logs, plans
from . import meta

PLATFORMS = ("facebook", "instagram")
LABEL = {"facebook": "Facebook", "instagram": "Instagram"}


def _video(p: dict[str, Any]) -> tuple[Path | None, dict[str, Any]]:
    path = Path(p["video_path"]) if p.get("video_path") else None
    if not path or not path.is_file():
        return None, {}
    from ..uploads import probe
    return path, probe(path)


def route(p: dict[str, Any], platform: str) -> tuple[str | None, str]:
    """('reel'|'video', '') when this video can go to the platform, else (None, reason)."""
    ch = channels.get(p.get("channel_id")) if p.get("channel_id") else None
    if not ch:
        return None, "make it for a channel to publish it"
    if not channels.uploads_allowed(ch):
        return None, "uploads are switched off for this channel"
    if not meta.connected(ch["id"]):
        return None, "connect Facebook & Instagram for this channel first"
    m = ch.get("meta") or {}
    path, info = _video(p)
    if not path:
        return None, "the video file isn't ready"
    secs, w, h = info.get("duration") or 0, info.get("width") or 0, info.get("height") or 0
    vertical = h > w
    if platform == "instagram":
        if not m.get("ig_user_id"):
            return None, "no Instagram professional account is linked to this Facebook Page"
        if secs > meta.IG_MAX_SECONDS or path.stat().st_size > meta.IG_MAX_BYTES:
            return None, "too long or too large for an Instagram Reel (15 min / 300 MB)"
        return "reel", ""
    if vertical and 3 <= secs <= meta.FB_REEL_MAX_SECONDS:
        return "reel", ""
    return "video", ""


def caption(p: dict[str, Any], platform: str) -> str:
    pkg = db.loads(p.get("package"), {}) if isinstance(p.get("package"), str) else (p.get("package") or {})
    title = (pkg.get("title") or p.get("title") or "").replace("#Shorts", "").strip()
    desc = (pkg.get("final_description") or pkg.get("description") or "")
    desc = re.sub(r"https?://(www\.)?(youtube\.com|youtu\.be)/\S+", "", desc)          # YouTube-only links
    desc = desc.replace("#Shorts", "").strip()
    text = f"{title}\n\n{desc}".strip()
    if platform == "instagram":                                # Instagram: 2,200 characters, 30 hashtags
        seen = 0

        def keep(m: re.Match) -> str:
            nonlocal seen
            seen += 1
            return m.group(0) if seen <= 30 else ""
        text = re.sub(r"#\w+", keep, text)[:2200]
    return text


def status(production_id: int) -> dict[str, dict[str, Any]]:
    return {r["platform"]: r for r in db.q("SELECT * FROM social_posts WHERE production_id=?", (production_id,))}


def request(production_id: int, platforms: list[str], by: str = "user") -> dict[str, str]:
    """Queue publishing; returns {platform: what happened}."""
    from .. import pipeline, worker
    p = pipeline.load(production_id)
    out = {}
    if not plans.can(p.get("workspace_id") or 1, "publish"):
        return {pl: "publishing isn't enabled for this account" for pl in platforms}
    for pl in platforms:
        if pl not in PLATFORMS:
            continue
        row = db.q1("SELECT * FROM social_posts WHERE production_id=? AND platform=?", (production_id, pl))
        if row and row["status"] in ("published", "uploading", "processing", "pending"):
            out[pl] = "already " + row["status"]
            continue
        kind, why = route(p, pl)
        if not kind:
            out[pl] = why
            continue
        if row:
            db.ex("UPDATE social_posts SET status='pending', kind=?, error=NULL, updated_at=? WHERE id=?",
                  (kind, db.iso(), row["id"]))
        else:
            db.insert("social_posts", {"production_id": production_id, "channel_id": p.get("channel_id"),
                                       "platform": pl, "kind": kind, "status": "pending", "created_at": db.iso(),
                                       "updated_at": db.iso()})
        worker.enqueue("social_upload", {"production_id": production_id, "platform": pl},
                       production_id=production_id, max_attempts=3, dedupe=True)
        out[pl] = f"queued as a {LABEL[pl]} {kind}"
        logs.event("info", "social", f"{LABEL[pl]} {kind} queued ({by})", production_id=production_id)
    return out


def run(production_id: int, platform: str) -> dict[str, Any]:
    from .. import pipeline, settings
    row = db.q1("SELECT * FROM social_posts WHERE production_id=? AND platform=?", (production_id, platform))
    if not row or row["status"] == "published":
        return {"skipped": "nothing to do"}
    p = pipeline.load(production_id)
    kind, why = route(p, platform)
    if not kind:
        db.ex("UPDATE social_posts SET status='skipped', error=?, updated_at=? WHERE id=?", (why, db.iso(), row["id"]))
        return {"skipped": why}
    ch = channels.get(p["channel_id"])
    m = ch.get("meta") or {}
    token = settings.secret(meta.token_secret(ch["id"]))
    path, _ = _video(p)
    db.ex("UPDATE social_posts SET status='uploading', updated_at=? WHERE id=?", (db.iso(), row["id"]))

    def save_container(cid: str | None) -> None:
        db.ex("UPDATE social_posts SET container_id=?, status=?, updated_at=? WHERE id=?",
              (cid, "processing" if cid else "uploading", db.iso(), row["id"]))
    try:
        if platform == "instagram":
            res = meta.publish_instagram(m["ig_user_id"], token, path, caption(p, platform), row, save_container)
        elif kind == "reel":
            res = meta.publish_facebook_reel(m["page_id"], token, path, caption(p, platform), ai=True)
        else:
            pkg = p["package"]
            res = meta.publish_facebook_video(m["page_id"], token, path, pkg.get("title") or p["title"],
                                              caption(p, platform))
    except Exception as e:
        db.ex("UPDATE social_posts SET status='failed', error=?, updated_at=? WHERE id=?",
              (str(e)[:400], db.iso(), row["id"]))
        logs.event("warning", "social", f"{LABEL[platform]} upload failed: {e}", production_id=production_id)
        raise
    db.ex("UPDATE social_posts SET status='published', remote_id=?, url=?, error=NULL, updated_at=? WHERE id=?",
          (res["id"], res.get("url"), db.iso(), row["id"]))
    logs.event("info", "social", f"Published to {LABEL[platform]} ({kind}): {res.get('url') or res['id']}",
               production_id=production_id)
    return res


def maybe_auto(production_id: int) -> dict[str, str]:
    """After a video is finished: publish to the platforms the channel has set to automatic."""
    p = db.q1("SELECT * FROM productions WHERE id=?", (production_id,))
    if not p or not p.get("channel_id") or p["status"] not in ("awaiting_approval", "approved", "uploading", "uploaded"):
        return {}
    if not (db.loads(p.get("qa"), {}) or {}).get("passed") or (db.loads(p.get("request"), {}) or {}).get("no_upload"):
        return {}
    m = (channels.get(p["channel_id"]) or {}).get("meta") or {}
    want = [pl for pl in PLATFORMS if m.get(f"auto_{pl}")]
    if not want:
        return {}
    out = request(production_id, want, by="auto")
    if p["kind"] == "edit":                         # Shorts cut from an upload are finished together with their job
        for kid in db.q("SELECT id FROM productions WHERE parent_id=? AND kind='edit_clip'", (production_id,)):
            maybe_auto(kid["id"])
    return out
