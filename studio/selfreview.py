"""Self-review: videos the studio held ('needs review' / 'waiting for approval') are re-checked, fixed and published
without the owner having to step in — through the same gates as every other upload, never around them.

For each held video (not uploaded, not test mode, not held by the owner on purpose):
  * finance videos blocked by the compliance gate: the script is made again (a lesson rewrites its script, which is
    now linted against the gate's checks; the daily episode switches to the app's fact-sheet script; a Short is
    rebuilt from its episode). The rebuilt video goes through QA and the gate again, and the gate's own outcome
    publishes it when it passes. A critical gate failure is never waved through.
  * other videos that stopped at a check: that stage runs again (the review stage rewrites only the flagged scenes;
    a failed technical QA renders again).
  * videos ready and only waiting for approval: published now if the channel's automatic-publish gate
    (ops.auto_publish_block) and, for finance, the compliance gate allow it.
Each video gets at most MAX_TRIES fix attempts; after that it stays for the owner, with the reason.
Never touched: near-duplicates of an earlier upload (YouTube's reused-content rule needs a human), videos the owner
held for review, and anything while the emergency stop is on.
"""
from __future__ import annotations

from typing import Any

from . import channels, db, logs, ops, pipeline

MAX_TRIES = 2
BY = "auto (self-review)"


def _tries(p: dict[str, Any]) -> int:
    return int(((db.loads(p.get("request"), {}) or {}).get("self_review") or {}).get("tries") or 0)


def _note(pid: int, action: str) -> None:
    row = db.q1("SELECT request FROM productions WHERE id=?", (pid,))
    req = db.loads(row["request"], {}) or {}
    sr = req.get("self_review") or {}
    req["self_review"] = {"tries": int(sr.get("tries") or 0) + (action != "published"), "last": action,
                          "at": db.iso()}
    db.update("productions", pid, {"request": db.dumps(req)})


def _fix_finance(p: dict[str, Any]) -> str:
    kind = p["kind"]
    if kind == "fib_lesson":
        pipeline.reset_to_stage(p["id"], "script")
        return "lesson script rewritten"
    if kind == "nifty_pivot":
        ep = {**(p.get("episode") or {}), "force_fallback": True}
        pipeline.reset_to_stage(p["id"], "script")
        pipeline.save(p["id"], episode=db.dumps(ep))
        return "episode remade with the fact-sheet script"
    if kind == "nifty_short":
        from .finance import shorts
        parent = pipeline.load(p["episode"]["segment"]["parent_id"])
        seg = {k: v for k, v in p["episode"]["segment"].items() if k not in ("parent_id", "parent_title")}
        built = shorts.build(parent, seg)
        pipeline.save(p["id"], package=built["package"], episode=db.dumps(built["episode"]),
                      title=built["package"]["title"])
        pipeline.reset_to_stage(p["id"], "voice")              # new narration: voice it again
        return "Short rebuilt from its episode"
    return ""


def _fix_general(p: dict[str, Any]) -> str:
    stages, _, _ = pipeline.stages_for(p.get("kind") or "general")
    stage = p.get("stage") or ""
    if stage == "qa" and "render" in stages:
        stage = "render"
    if stage not in stages:
        return ""
    pipeline.reset_to_stage(p["id"], stage)
    return f"'{stage}' run again"


def _publish(p: dict[str, Any]) -> str:
    """'' when published (or queued to publish), else why it still waits."""
    ch = channels.get(p.get("channel_id"))
    if p.get("kind") in pipeline.FINANCE_KINDS:
        from .finance import gate
        result = gate.run(p, pipeline.assets_for(p["id"]))          # rules or the owner's acceptance may have changed
        pipeline.save(p["id"], compliance=db.dumps(result))
        ok, why = gate.permits(result, "public")
        if not ok:
            return why
        privacy = "public"
    else:
        privacy = (ch or {}).get("publish_privacy") or "private"
    block = ops.auto_publish_block(ch, bool((p.get("qa") or {}).get("auto_publish_ok")))
    if block:
        return block
    pipeline.approve(p["id"], privacy=privacy, publish_at=None, by=BY)
    now = pipeline.load(p["id"])["status"]
    return "" if now in ("approved", "uploading", "uploaded") else (now + ": " + (pipeline.load(p["id"]).get("error") or ""))


def review(pid: int) -> dict[str, Any]:
    """Re-check one held video: publish it, fix it, or say why it stays for the owner."""
    p = pipeline.load(pid)
    req = db.loads(p.get("request"), {}) or {}
    base = {"id": pid, "title": p.get("title")}
    if p["status"] not in ("needs_review", "awaiting_approval") or p.get("youtube_video_id") or p.get("test_mode"):
        return {**base, "result": "skipped", "why": f"status {p['status']}"}
    if ops.stopped():
        return {**base, "result": "waiting", "why": "the emergency stop is on"}
    if channels.paused(p.get("channel_id")):
        return {**base, "result": "waiting", "why": "its channel is paused"}
    if req.get("hold_for_review"):
        return {**base, "result": "waiting", "why": "you held it for your own review"}
    err = p.get("error") or ""
    if "repeat" in err.lower() or "same as" in err.lower():
        return {**base, "result": "waiting", "why": "it looks like an earlier upload — needs your look"}
    if p["status"] == "awaiting_approval":
        if not (p.get("qa") or {}).get("passed"):
            return {**base, "result": "waiting", "why": "QA has not passed"}
        try:
            why = _publish(p)
        except pipeline.NeedsAttention as e:
            why = str(e)
        if not why:
            _note(pid, "published")
            logs.event("info", "self-review", "Self-review: checks passed — published", production_id=pid)
            return {**base, "result": "published"}
        return {**base, "result": "waiting", "why": why}
    if _tries(p) >= MAX_TRIES:
        return {**base, "result": "waiting", "why": f"{MAX_TRIES} automatic fixes didn't clear it: {err[:200]}"}
    script_problem = p.get("kind") in pipeline.FINANCE_KINDS and "technical quality" not in err
    done = _fix_finance(p) if script_problem else _fix_general(p)
    if not done:
        return {**base, "result": "waiting", "why": "no automatic fix for this stage"}
    _note(pid, done)
    ops.audit(BY, "self_review_fix", channel_id=p.get("channel_id"), production_id=pid,
              detail={"action": done, "was": err[:500]})
    logs.event("info", "self-review", f"Self-review: {done} (was: {err[:160]}) — it goes through every check again "
               "and publishes itself when they pass", production_id=pid)
    return {**base, "result": "fixing", "action": done}


def run_all() -> list[dict[str, Any]]:
    rows = db.q("SELECT id FROM productions WHERE status IN ('needs_review','awaiting_approval') "
                "AND youtube_video_id IS NULL AND COALESCE(test_mode,0)=0 ORDER BY id")
    out = []
    for r in rows:
        try:
            out.append(review(r["id"]))
        except Exception as e:                   # one bad video never stops the others
            logs.event("warning", "self-review", f"Self-review failed: {e}", production_id=r["id"])
            out.append({"id": r["id"], "result": "error", "why": str(e)[:300]})
    return out
