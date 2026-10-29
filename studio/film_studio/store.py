"""Film Studio records: references (characters, locations, props), the shot list and every take of a shot.

Every read and write is scoped to a workspace. Takes are never overwritten: a regenerated shot gets a new take and
the earlier ones stay available. A take's motion type records what the file really is (a still, a still with a
camera move, real video…) and is never upgraded by a label.
"""
from __future__ import annotations

from typing import Any

from .. import db

REF_KINDS = ("character", "location", "prop")
REF_STATUS = ("proposed", "approved", "rejected")
SHOT_TYPES = ("establishing", "wide", "medium", "close_up", "over_shoulder", "reaction", "insert", "cutaway")
MOTION_TYPES = ("planned", "static", "camera_move", "animated_image", "video", "dialogue_video", "lipsync_verified")
SHOT_STATUS = ("planned", "queued", "generating", "ready", "needs_review", "approved", "rejected", "failed", "waiting")
MOTION_LABEL = {"planned": "not made yet", "static": "still image", "camera_move": "still image with camera move",
                "animated_image": "animated image", "video": "generated video (continuous motion)",
                "dialogue_video": "dialogue-driven video", "lipsync_verified": "video with verified lip-sync"}
JSON_SHOT = ("camera", "characters", "dialogue", "props", "continuity", "qa")


class NotFound(LookupError):
    pass


def _owned(table: str, row_id: int, ws: int) -> dict[str, Any]:
    row = db.q1(f"SELECT * FROM {table} WHERE id=? AND workspace_id=?", (row_id, ws))
    if not row:
        raise NotFound(f"{table} #{row_id} not found")
    return row


def _production(production_id: int, ws: int) -> dict[str, Any]:
    p = db.q1("SELECT id, workspace_id, kind FROM productions WHERE id=? AND workspace_id=?", (production_id, ws))
    if not p:
        raise NotFound(f"production #{production_id} not found")
    return p


# ------------------------------------------------------------------ references

def add_ref(production_id: int, ws: int, kind: str, ref_key: str, name: str, description: str = "",
            file_path: str | None = None, meta: dict[str, Any] | None = None) -> int:
    """A new version of a reference; earlier versions are kept (an approved one is never silently replaced)."""
    if kind not in REF_KINDS:
        raise ValueError(f"kind must be one of {REF_KINDS}")
    _production(production_id, ws)
    last = db.q1("SELECT MAX(version) AS v FROM film_refs WHERE production_id=? AND kind=? AND ref_key=?",
                 (production_id, kind, ref_key))
    return db.insert("film_refs", {"production_id": production_id, "workspace_id": ws, "kind": kind,
                                   "ref_key": ref_key, "name": name[:80], "description": description[:2000],
                                   "file_path": file_path, "status": "proposed", "version": (last["v"] or 0) + 1,
                                   "meta": db.dumps(meta or {}), "created_at": db.iso(), "updated_at": db.iso()})


def set_ref_status(ref_id: int, ws: int, status: str) -> None:
    if status not in REF_STATUS:
        raise ValueError(f"status must be one of {REF_STATUS}")
    ref = _owned("film_refs", ref_id, ws)
    with db.tx():
        if status == "approved":                     # one approved version per reference
            db.ex("UPDATE film_refs SET status='proposed', updated_at=? WHERE production_id=? AND kind=? AND "
                  "ref_key=? AND status='approved' AND id<>?",
                  (db.iso(), ref["production_id"], ref["kind"], ref["ref_key"], ref_id))
        db.ex("UPDATE film_refs SET status=?, updated_at=? WHERE id=?", (status, db.iso(), ref_id))


def refs(production_id: int, ws: int, kind: str | None = None) -> list[dict[str, Any]]:
    _production(production_id, ws)
    where, args = "production_id=? AND workspace_id=?", [production_id, ws]
    if kind:
        where += " AND kind=?"
        args.append(kind)
    return db.q(f"SELECT * FROM film_refs WHERE {where} ORDER BY kind, ref_key, version", tuple(args))


def approved_ref(production_id: int, ws: int, kind: str, ref_key: str) -> dict[str, Any] | None:
    return db.q1("SELECT * FROM film_refs WHERE production_id=? AND workspace_id=? AND kind=? AND ref_key=? AND "
                 "status='approved' ORDER BY version DESC LIMIT 1", (production_id, ws, kind, ref_key))


# ------------------------------------------------------------------ shots

def _shot_row(r: dict[str, Any]) -> dict[str, Any]:
    for k in JSON_SHOT:
        r[k] = db.loads(r.get(k), [] if k in ("characters", "dialogue", "props") else {})
    r["motion_label"] = MOTION_LABEL.get(r["motion_type"], r["motion_type"])
    return r


def add_shot(production_id: int, ws: int, scene_no: int, shot_no: int, **fields: Any) -> int:
    _production(production_id, ws)
    if fields.get("shot_type", "medium") not in SHOT_TYPES:
        raise ValueError(f"shot_type must be one of {SHOT_TYPES}")
    row = {"production_id": production_id, "workspace_id": ws, "scene_no": scene_no, "shot_no": shot_no,
           "created_at": db.iso(), "updated_at": db.iso()}
    for k, v in fields.items():
        row[k] = db.dumps(v) if k in JSON_SHOT else v
    return db.insert("film_shots", row)


def shots(production_id: int, ws: int) -> list[dict[str, Any]]:
    _production(production_id, ws)
    return [_shot_row(r) for r in db.q("SELECT * FROM film_shots WHERE production_id=? AND workspace_id=? "
                                       "ORDER BY scene_no, shot_no", (production_id, ws))]


def shot(shot_id: int, ws: int) -> dict[str, Any]:
    return _shot_row(_owned("film_shots", shot_id, ws))


def set_shot(shot_id: int, ws: int, **fields: Any) -> None:
    _owned("film_shots", shot_id, ws)
    if "status" in fields and fields["status"] not in SHOT_STATUS:
        raise ValueError(f"status must be one of {SHOT_STATUS}")
    if "motion_type" in fields and fields["motion_type"] not in MOTION_TYPES:
        raise ValueError(f"motion_type must be one of {MOTION_TYPES}")
    row = {k: (db.dumps(v) if k in JSON_SHOT else v) for k, v in fields.items()}
    db.update("film_shots", shot_id, {**row, "updated_at": db.iso()})


# ------------------------------------------------------------------ takes

def add_take(shot_id: int, ws: int, *, provider: str, motion_type: str, file_path: str | None, model: str = "",
             prompt: str = "", settings: dict[str, Any] | None = None, cost_usd: float = 0.0,
             status: str = "ready", error: str | None = None) -> int:
    """Record a new take (earlier takes are kept). The shot points at it only when it succeeded."""
    if motion_type not in MOTION_TYPES or motion_type == "planned":
        raise ValueError("a take must say what it really is (static, camera_move, video …)")
    s = _owned("film_shots", shot_id, ws)
    n = db.q1("SELECT COALESCE(MAX(take_no),0) AS n FROM film_takes WHERE shot_id=?", (shot_id,))["n"] + 1
    tid = db.insert("film_takes", {"shot_id": shot_id, "production_id": s["production_id"], "workspace_id": ws,
                                   "take_no": n, "provider": provider, "model": model, "prompt": prompt[:4000],
                                   "settings": db.dumps(settings or {}), "file_path": file_path,
                                   "motion_type": motion_type, "cost_usd": cost_usd, "status": status,
                                   "error": error, "created_at": db.iso()})
    if status == "ready":
        set_shot(shot_id, ws, current_take=tid, motion_type=motion_type, provider=provider, status="ready")
    else:
        # a failed retake never demotes a shot that already has a good take
        set_shot(shot_id, ws, status="failed" if status == "failed" and not s.get("current_take") else s["status"])
    return tid


def takes(shot_id: int, ws: int) -> list[dict[str, Any]]:
    _owned("film_shots", shot_id, ws)
    return db.q("SELECT * FROM film_takes WHERE shot_id=? ORDER BY take_no", (shot_id,))


def summary(production_id: int, ws: int) -> dict[str, Any]:
    """Counts by status and by real motion type — what the film actually contains."""
    rows = shots(production_id, ws)
    by_status: dict[str, int] = {}
    by_motion: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        by_motion[r["motion_type"]] = by_motion.get(r["motion_type"], 0) + 1
    cost = db.q1("SELECT COALESCE(SUM(cost_usd),0) AS c FROM film_takes WHERE production_id=? AND workspace_id=?",
                 (production_id, ws))["c"]
    return {"shots": len(rows), "by_status": by_status, "by_motion": by_motion, "cost_usd": round(cost, 4)}
