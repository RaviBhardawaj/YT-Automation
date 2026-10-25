"""One entry point for every new video: a free-text request, an idea, or a channel's daily schedule.

The request is classified first (creative/request.plan), then routed to the workflow that can actually
make it for that channel:
  * animated stories (comedy, horror, drama, kids …)  -> creative.workflow (characters, dialogue, animation)
  * factual videos (explainers, finance concepts, tech) -> the researched pipeline, with the request as the brief
  * the NIFTY channel's daily slot with no request      -> the verified-data daily episode
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import channels, db, logs, worker
from .creative import request as reqmod


def _norm(title: str) -> str:
    return re.sub(r"[^\w]+", " ", title.lower()).strip()


def plan_for(channel_id: int, request: str, idea: dict[str, Any] | None = None, hint: str | None = None) -> dict[str, Any]:
    ch = channels.get(channel_id)
    plan = reqmod.plan(request, ch, idea, hint=hint)
    # nothing explicit in the request (or a linked video): the channel's own workflow decides
    if plan["decided_by"] not in ("request", "reference") and ch.get("workflow") in ("animated_story", "factual"):
        plan["workflow"] = ch["workflow"]
        plan["animated"] = ch["workflow"] == "animated_story"
    return plan


def create(channel_id: int, request: str = "", *, idea_id: int | None = None, fmt: str | None = None,
           actor: str = "user", scheduled_for: str | None = None, no_upload: bool = False,
           ai_video: bool = False) -> dict[str, Any]:
    """Queue a job. Returns {"production_ids": [...]} or {"task_id": n} for the NIFTY daily episode."""
    ch = channels.get(channel_id)
    if not ch:
        raise ValueError("channel not found")
    from . import plans
    plans.check(ch["workspace_id"], "video")
    request = (request or "").strip()[:2000]
    idea = db.q1("SELECT * FROM topics WHERE id=?", (idea_id,)) if idea_id else None
    if idea and idea.get("channel_id") not in (None, channel_id):
        raise ValueError("that idea belongs to another channel")
    if fmt not in (None, "", "long", "short", "both"):
        raise ValueError("format must be long, short or both")
    if ch["workflow"] == "nifty_daily" and not request and not idea:
        tid = worker.enqueue("nifty_daily_episode", {"channel_id": channel_id}, dedupe=True, max_attempts=2)
        logs.event("info", "jobs", f"Daily NIFTY episode queued for '{ch['name']}' ({actor})")
        return {"task_id": tid, "production_ids": []}
    if not request and not idea:
        raise ValueError("describe the video you want (or pick an idea)")
    ref = None
    if request:
        from .creative import reference
        ref = reference.resolve(request)            # a YouTube link: its subject drives the video, not the channel
    if ref:
        plan = plan_for(channel_id, ref["user_text"], idea, hint=ref["content_type"])
        r = ref["reference"]
        if not fmt and plan["decided_by"] != "request" and ("/shorts/" in request or 0 < r["duration_s"] <= 61):
            plan["format"] = "short"
        plan.update({"request": ref["brief"], "reference": {k: r[k] for k in ("id", "url", "title", "channel", "via")}})
    else:
        plan = plan_for(channel_id, request, idea)
    if fmt:
        plan["format"] = fmt
    plan["scheduled_for"] = scheduled_for
    plan["actor"] = actor
    plan["no_upload"] = bool(no_upload)                # "No channel — just make it": never auto-uploaded
    plan["ai_video"] = bool(ai_video)                  # opted in to paid AI-generated clips (researched videos)
    if plan["workflow"] == "animated_story":
        from .creative import workflow
        pid = workflow.create(channel_id, plan["request"], idea_id=idea_id, fmt=plan["format"], actor=actor,
                              scheduled_for=scheduled_for, plan=plan)
        return {"production_ids": [pid], "plan": plan}
    return {"production_ids": _factual(ch, plan, idea), "plan": plan}


def _factual(ch: dict[str, Any], plan: dict[str, Any], idea: dict[str, Any] | None) -> list[int]:
    """Researched video. The user's request becomes the brief the script is written to."""
    from . import pipeline
    req = plan["request"]
    if idea:
        tid = idea["id"]
        if not idea.get("channel_id"):
            db.update("topics", tid, {"channel_id": ch["id"], "updated_at": db.iso()})
    else:
        subject = (plan.get("reference") or {}).get("title") or req
        title = subject if len(subject) <= 110 else subject[:107].rsplit(" ", 1)[0] + "…"
        tid = db.insert("topics", {
            "channel_id": ch["id"], "title": title, "angle": f"Do exactly what the request asks: {req}",
            "format": "short" if plan["format"] == "short" else "long", "hook_idea": "", "why_now": "requested",
            "status": "approved", "score": 0, "idea_kind": "request", "style": plan["content_type"],
            "language": plan["language"], "summary": req, "search_query": subject[:200], "norm_title": _norm(title),
            "workspace_id": ch["workspace_id"], "created_at": db.iso(), "updated_at": db.iso()})
    extra = {"channel_id": ch["id"], "workspace_id": ch["workspace_id"], "language": plan["language"],
             "request": json.dumps(plan, ensure_ascii=False)}
    fmts = ["long", "short"] if plan["format"] == "both" else [plan["format"]]
    pids = [pipeline.create(tid, f, extra=extra) for f in fmts]
    logs.event("info", "jobs", f"{plan['content_type']} video queued for '{ch['name']}' as {'+'.join(fmts)} "
               f"(decided by {plan['decided_by']})", production_id=pids[0])
    return pids


# ------------------------------------------------------------------ job controls

def pause(pid: int) -> int:
    n = db.ex("UPDATE tasks SET status='paused', last_error='paused by user', updated_at=? "
              "WHERE production_id=? AND status='pending'", (db.iso(), pid))
    if n:
        db.update("productions", pid, {"status": "paused", "updated_at": db.iso()})
    return n


def resume(pid: int) -> int:
    n = db.ex("UPDATE tasks SET status='pending', run_after=?, last_error=NULL, updated_at=? "
              "WHERE production_id=? AND status='paused'", (db.iso(), db.iso(), pid))
    if n:
        db.update("productions", pid, {"status": "queued", "updated_at": db.iso()})
        worker._wake.set()
    return n


def cancel(pid: int) -> None:
    """Stops at the next stage boundary (the running stage finishes, nothing after it runs)."""
    db.ex("UPDATE tasks SET status='cancelled', updated_at=? WHERE production_id=? AND status IN "
          "('pending','paused','failed')", (db.iso(), pid))
    db.update("productions", pid, {"status": "rejected", "error": "Cancelled by user.", "updated_at": db.iso()})


def retry(pid: int) -> int:
    """Re-run from the stage that failed; finished stages are not repeated."""
    from pathlib import Path
    p = db.q1("SELECT * FROM productions WHERE id=?", (pid,))
    if not p:
        raise ValueError("not found")
    missing = bool(p["video_path"]) and not Path(p["video_path"]).is_file() and p["status"] != "uploaded"
    if p["status"] not in ("failed", "needs_attention", "paused", "rejected") and not missing:
        raise ValueError(f"nothing to retry (status is {p['status']})")
    if p["kind"] in ("edit", "edit_clip") and missing:
        # a lost output is rendered again by its job; finished outputs that still exist are kept
        job = p["parent_id"] if p["kind"] == "edit_clip" else pid
        db.update("productions", job, {"status": "queued", "stage": "render", "error": None, "updated_at": db.iso()})
        if job != pid:
            db.update("productions", pid, {"status": "queued", "error": None, "updated_at": db.iso()})
        return worker.enqueue("production", {"production_id": job}, production_id=job, max_attempts=3, dedupe=True)
    last = db.q1("SELECT type, payload FROM tasks WHERE production_id=? ORDER BY id DESC LIMIT 1", (pid,))
    kind, payload = (last["type"], db.loads(last["payload"], {})) if last else ("production", {"production_id": pid})
    db.ex("UPDATE tasks SET status='cancelled', updated_at=? WHERE production_id=? AND status IN ('pending','paused','failed')",
          (db.iso(), pid))
    db.update("productions", pid, {"status": "approved" if kind == "upload" else "queued", "error": None,
                                   "updated_at": db.iso()})
    return worker.enqueue(kind, payload, production_id=pid, max_attempts=3)
