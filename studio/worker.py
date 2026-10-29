"""Persistent task queue (SQLite) with two worker threads, retries, pausing and crash recovery.

Lanes keep heavy media work (one production at a time) from blocking research and uploads.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from typing import Any, Callable

import httpx

from . import ai, db, logs

log = logging.getLogger(__name__)

LANES = ("general", "media")
LANE_FOR = {"production": "media"}
BACKOFF = [60, 300, 1800, 3600, 3 * 3600]

_handlers: dict[str, Callable[[dict[str, Any], dict[str, Any]], Any]] = {}
_stop = threading.Event()
_threads: list[threading.Thread] = []
_wake = threading.Event()


def handler(name: str):
    def deco(fn):
        _handlers[name] = fn
        return fn
    return deco


def enqueue(task_type: str, payload: dict[str, Any] | None = None, *, priority: int = 0, delay: float = 0,
            max_attempts: int = 3, production_id: int | None = None, schedule_id: int | None = None,
            dedupe: bool = False) -> int:
    payload = payload or {}
    body = db.dumps(payload)
    if dedupe:
        row = db.q1("SELECT id FROM tasks WHERE type=? AND payload=? AND status IN ('pending','running','paused')",
                    (task_type, body))
        if row:
            return row["id"]
    now = db.iso()
    tid = db.insert("tasks", {"type": task_type, "lane": LANE_FOR.get(task_type, "general"), "payload": body,
                              "status": "pending", "priority": priority, "attempts": 0, "max_attempts": max_attempts,
                              "run_after": db.iso_in(delay), "production_id": production_id,
                              "schedule_id": schedule_id, "created_at": now, "updated_at": now})
    _wake.set()
    return tid


def claim(lane: str) -> dict[str, Any] | None:
    now = db.iso()
    with db.tx() as c:
        row = c.execute("SELECT id FROM tasks WHERE status='pending' AND lane=? AND run_after<=? "
                        "ORDER BY priority DESC, id LIMIT 1", (lane, now)).fetchone()
        if not row:
            return None
        c.execute("UPDATE tasks SET status='running', attempts=attempts+1, locked_at=?, updated_at=? WHERE id=?",
                  (now, now, row["id"]))
        task = c.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
    task["payload"] = db.loads(task["payload"], {})
    return task


def _finish(task_id: int, status: str, **fields: Any) -> None:
    db.update("tasks", task_id, {"status": status, "updated_at": db.iso(), "locked_at": None, **fields})


def recover() -> int:
    """After a crash/restart, anything left 'running' is put back in the queue."""
    n = db.ex("UPDATE tasks SET status='pending', locked_at=NULL, updated_at=? WHERE status='running'", (db.iso(),))
    if n:
        logs.event("warning", "worker", f"Recovered {n} interrupted task(s) after restart")
    return n


def retry(task_id: int) -> None:
    db.ex("UPDATE tasks SET status='pending', run_after=?, attempts=0, last_error=NULL, updated_at=? WHERE id=?",
          (db.iso(), db.iso(), task_id))
    _wake.set()


def cancel(task_id: int) -> None:
    db.ex("UPDATE tasks SET status='cancelled', updated_at=? WHERE id=? AND status IN ('pending','paused','failed')",
          (db.iso(), task_id))


def resume_paused(reason_like: str = "%budget%") -> int:
    return db.ex("UPDATE tasks SET status='pending', run_after=?, updated_at=? WHERE status='paused' AND last_error LIKE ?",
                 (db.iso(), db.iso(), reason_like))


def _run(task: dict[str, Any]) -> None:
    from . import pipeline
    fn = _handlers.get(task["type"])
    pid = task.get("production_id")
    if not fn:
        _finish(task["id"], "failed", last_error=f"no handler for {task['type']}")
        return
    started = time.time()
    try:
        result = fn(task["payload"], task)
        _finish(task["id"], "done", result=db.dumps(result), last_error=None)
        log.info("task %s %s done in %.1fs", task["id"], task["type"], time.time() - started)
    except pipeline.PausedByOwner as e:
        _finish(task["id"], "paused", last_error=f"paused by user ({e})")
        if pid:
            pipeline.save(pid, status="paused")
        logs.event("info", "worker", f"Paused: {e}", production_id=pid, task_id=task["id"])
    except ai.BudgetExceeded as e:
        _finish(task["id"], "paused", last_error=f"budget: {e}")
        if pid:
            pipeline.save(pid, status="paused_budget", error=f"Monthly AI budget reached: {e}")
        logs.event("warning", "worker", f"Paused task {task['type']} — AI spending limit reached", production_id=pid, task_id=task["id"])
    except pipeline.NeedsAttention as e:
        _finish(task["id"], "failed", last_error=str(e))
        if pid:
            p = db.q1("SELECT status, kind FROM productions WHERE id=?", (pid,))
            if p and p["status"] not in ("approved", "needs_review"):
                # compliance-related stops keep the explicit NEEDS REVIEW status
                review = p.get("kind") in ("nifty_pivot", "fib_lesson", "nifty_short") or str(e).startswith("NEEDS REVIEW")
                pipeline.save(pid, status="needs_review" if review else "needs_attention", error=str(e))
            elif p and p["status"] == "needs_review":
                pipeline.save(pid, error=str(e))
        logs.event("warning", "worker", f"{task['type']} needs attention: {e}", production_id=pid, task_id=task["id"])
    except Exception as e:  # retry with backoff
        detail = f"{type(e).__name__}: {e}"
        log.error("task %s %s failed: %s\n%s", task["id"], task["type"], detail, traceback.format_exc())
        retry_after = getattr(e, "retry_after", None)
        # YouTube's daily limit/quota, a dropped connection or a YouTube outage is temporary: the upload just goes
        # out at the next chance and never uses up its attempts. Any job can also say it is only waiting
        # (e.g. a free daily allowance that resets tomorrow) by raising an error with temporary=True.
        temporary = bool(getattr(e, "temporary", False)) or task["type"] in ("upload", "social_upload") and (
            retry_after is not None or isinstance(e, (httpx.TransportError, OSError)))
        if temporary or task["attempts"] < task["max_attempts"]:
            delay = retry_after or (900 if temporary else BACKOFF[min(task["attempts"] - 1, len(BACKOFF) - 1)])
            db.update("tasks", task["id"], {"status": "pending", "run_after": db.iso_in(delay), "locked_at": None,
                                            "last_error": detail[:2000], "updated_at": db.iso(),
                                            **({"attempts": max(0, task["attempts"] - 1)} if temporary else {})})
            logs.event("warning", "worker", f"{task['type']} failed (attempt {task['attempts']}/{task['max_attempts']}), "
                       f"retrying in {delay // 60} min: {detail[:300]}", production_id=pid, task_id=task["id"])
        else:
            _finish(task["id"], "failed", last_error=detail[:2000])
            if pid:
                pipeline.save(pid, status="failed", error=detail[:1000])
            logs.event("error", "worker", f"{task['type']} failed permanently: {detail[:500]}", production_id=pid, task_id=task["id"])


def _loop(lane: str) -> None:
    while not _stop.is_set():
        try:
            task = claim(lane)
        except Exception:
            log.exception("claim failed")
            task = None
        if task is None:
            _wake.wait(3)
            _wake.clear()
            continue
        _run(task)


def start() -> None:
    if _threads:
        return
    recover()
    for lane in LANES:
        t = threading.Thread(target=_loop, args=(lane,), name=f"worker-{lane}", daemon=True)
        t.start()
        _threads.append(t)


def stop() -> None:
    _stop.set()
    _wake.set()


def running() -> list[dict[str, Any]]:
    return db.q("SELECT id, type, lane, production_id, attempts, locked_at FROM tasks WHERE status='running'")
