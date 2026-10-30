"""Operations control: the global emergency stop, the audit trail of critical actions, and the publishing gate
that decides whether a finished video may go out on its own.

Emergency stop — one switch that halts new work everywhere: no task is started except harmless bookkeeping
(analytics, housekeeping), nothing is approved or uploaded automatically, and daily schedules don't create videos.
A task already running finishes its current step; an upload already sending finishes (cutting it off would leave
a half-sent video). Switching it off lets everything continue from where it waited.

Publishing gate — a video is published without a human only when ALL of these hold: the channel opted in to
auto-publish, the channel is active (not paused), the emergency stop is off, the channel has had at least its
first N uploads approved by the owner, and every quality check passed with no warnings.
"""
from __future__ import annotations

from typing import Any

from . import db, logs, settings

SAFE_DURING_STOP = ("analytics_sync", "channel_stats", "housekeeping", "learning_review", "weekly_review",
                    "comments_daily", "backup")


def stopped() -> dict[str, Any] | None:
    """The emergency stop record when it is on, else None."""
    s = settings.get("ops.emergency_stop") or {}
    return s if s.get("on") else None


def set_stop(on: bool, actor: str, reason: str = "") -> None:
    rec = {"on": bool(on), "reason": reason.strip()[:300], "by": actor, "at": db.iso()}
    settings.set("ops.emergency_stop", rec)
    audit(actor, "emergency_stop_on" if on else "emergency_stop_off", detail={"reason": rec["reason"]})
    logs.event("warning" if on else "info", "ops", ("EMERGENCY STOP — all new work and publishing halted"
                                                    + (f": {rec['reason']}" if rec["reason"] else "")) if on
               else "Emergency stop switched off — work continues")


def audit(actor: str, action: str, *, channel_id: int | None = None, production_id: int | None = None,
          detail: dict[str, Any] | None = None) -> None:
    """Append one critical action to the studio's hash-chained audit log (editing or deleting an entry later breaks
    the chain, which the Compliance page reports). Secrets are never put in `detail`."""
    from .finance import audit as chain
    try:
        chain.record(action, {"channel_id": channel_id, **(detail or {})}, production_id=production_id,
                     actor=str(actor or "unknown")[:120])
    except Exception:  # the audit trail must never stop the work it records
        logs.event("warning", "ops", f"could not write audit entry for {action}")


def recent(limit: int = 100) -> list[dict[str, Any]]:
    rows = db.q("SELECT id, created_at, actor, event, production_id, details FROM audit_log ORDER BY id DESC LIMIT ?",
                (limit,))
    for r in rows:
        r["details"] = db.loads(r["details"], {})
    return rows


def uploads_done(channel_id: int) -> int:
    return int(db.q1("SELECT COUNT(*) AS n FROM productions WHERE channel_id=? AND youtube_video_id IS NOT NULL",
                     (channel_id,))["n"])


def auto_publish_block(chan: dict[str, Any] | None, qa_auto_ok: bool) -> str:
    """'' when this channel's video may be published without a human, else the reason it waits for approval."""
    if stopped():
        return "the emergency stop is on"
    if not chan:
        return "the video has no channel"
    if not chan.get("auto_publish"):
        return f"auto-publish is off for '{chan['name']}' — approval required"
    if chan.get("status") != "active":
        return f"'{chan['name']}' is paused"
    need = int(chan.get("approve_first_n") or 0)
    done = uploads_done(chan["id"])
    if done < need:
        return f"'{chan['name']}' has {done} of its first {need} uploads approved by you — this one needs your approval"
    if not qa_auto_ok:
        return "a quality check needs a human look"
    return ""


REPEAT_TITLE = 0.80       # title similarity that counts as the same video
REPEAT_SCRIPT = 0.35      # share of 7-word phrases shared with an earlier upload's script


def spoken_text(pkg: dict[str, Any]) -> str:
    """Everything said in a video, whatever its kind: narration scenes, or story/film scenes made of dialogue lines."""
    out = []
    for s in (pkg or {}).get("scenes") or []:
        if s.get("narration"):
            out.append(s["narration"])
        out += [ln.get("text", "") for ln in s.get("lines") or [] if isinstance(ln, dict)]
    return "\n".join(t for t in out if t)


def repeat_check(p: dict[str, Any], days: int = 180) -> dict[str, Any]:
    """Is this video (nearly) the same as one already uploaded — on ANY channel of the studio? Compares the title
    and the spoken script with uploads of the last `days` days. A repeat needs a human look (never auto-published):
    near-duplicate uploads count against YouTube's reused / repetitious content rules."""
    from .research import dedup
    pkg = p.get("package") or {}
    title, script = pkg.get("title") or p.get("title") or "", spoken_text(pkg)
    rows = db.q("SELECT id, channel_id, title, package FROM productions WHERE youtube_video_id IS NOT NULL AND id<>? "
                "AND uploaded_at>=? AND (parent_id IS NULL OR parent_id<>?)",
                (p["id"], db.iso_in(-days * 86400), p["id"]))
    worst: dict[str, Any] = {}
    for r in rows:
        if p.get("parent_id") and r["id"] == p["parent_id"]:
            continue                                   # a Short cut from its own episode is not a repeat
        other = db.loads(r["package"], {}) or {}
        t = dedup.similarity(title, other.get("title") or r["title"] or "")
        theirs = spoken_text(other)
        sc = dedup.overlap_ratio(script, [theirs]) if script and theirs else 0.0
        if max(t, sc) > max(worst.get("title", 0), worst.get("script", 0)):
            worst = {"id": r["id"], "channel_id": r["channel_id"], "title": round(t, 2), "script": round(sc, 2)}
    bad = bool(worst) and (worst["title"] >= REPEAT_TITLE or worst["script"] >= REPEAT_SCRIPT)
    detail = "no earlier upload is similar" if not worst else (
        f"closest earlier upload #{worst['id']} (channel {worst['channel_id']}): title {worst['title']:.0%} alike, "
        f"script {worst['script']:.0%} shared phrases" + (" — looks like a repeat" if bad else ""))
    return {"name": "not a repeat of an earlier upload (all channels)", "ok": not bad, "detail": detail,
            "severity": "warn"}
