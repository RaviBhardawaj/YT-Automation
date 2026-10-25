"""Disk space: delete videos for good, clear working files of finished videos, and report what uses space.

A video's folder holds its final files (video, thumbnail, subtitles, edited outputs) and working files
(clips, overlays, stock visuals, per-line audio, intermediate renders). Working files are only needed to
re-render, so they can be cleared once a video is finished. Deleting a video here never touches YouTube.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from . import config, db, logs

FINAL_NAMES = {"final.mp4", "thumbnail.jpg", "captions.srt"}
ACTIVE = ("queued", "uploading")                    # never delete while it's being worked on or uploaded
FINISHED = ("uploaded", "rejected", "failed", "awaiting_approval", "needs_attention", "needs_review", "test_passed")


def folder(pid: int) -> Path:
    return config.PRODUCTIONS_DIR / f"{pid:05d}"


def size_of(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.exists() else 0


def _is_final(f: Path, d: Path) -> bool:
    rel = f.relative_to(d)
    if rel.parts[0] == "outputs":                    # edited outputs: keep videos, thumbnails, subtitles
        return f.suffix in (".mp4", ".jpg", ".srt")
    return f.name in FINAL_NAMES


def working_bytes(pid: int) -> int:
    d = folder(pid)
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file() and not _is_final(f, d)) if d.exists() else 0


def _children(pid: int) -> list[int]:
    return [r["id"] for r in db.q("SELECT id FROM productions WHERE parent_id=?", (pid,))]


def _running(pid: int) -> bool:
    return bool(db.q1("SELECT 1 FROM tasks WHERE production_id=? AND status='running'", (pid,)))


def delete_video(pid: int) -> int:
    """Delete a video, its Shorts/outputs, files and records. Returns bytes freed."""
    p = db.q1("SELECT id, status, title, youtube_video_id FROM productions WHERE id=?", (pid,))
    if not p:
        raise ValueError("not found")
    ids = [pid] + _children(pid)
    for i in ids:
        if _running(i) or db.q1("SELECT 1 FROM productions WHERE id=? AND status='uploading'", (i,)):
            raise ValueError(f"#{i} is being worked on or uploaded right now — cancel it or wait, then delete")
    freed = 0
    for i in ids:
        freed += size_of(folder(i))
        shutil.rmtree(folder(i), ignore_errors=True)
        db.ex("UPDATE tasks SET status='cancelled', updated_at=? WHERE production_id=? AND status IN ('pending','paused')",
              (db.iso(), i))
        db.ex("DELETE FROM assets WHERE production_id=?", (i,))
        db.ex("DELETE FROM analytics WHERE production_id=?", (i,))
        db.ex("DELETE FROM productions WHERE id=?", (i,))
    logs.event("info", "storage", f"Deleted video #{pid} '{p['title'][:60]}' and {len(ids) - 1} linked output(s): "
               f"{freed / 1e6:.0f} MB freed" + (" (still on YouTube)" if p.get("youtube_video_id") else ""))
    return freed


def clear_working_files(pid: int) -> int:
    """Remove a finished video's working files; the final video, thumbnail and subtitles stay."""
    p = db.q1("SELECT status FROM productions WHERE id=?", (pid,))
    if not p or p["status"] not in FINISHED or _running(pid):
        return 0
    d = folder(pid)
    if not d.exists():
        return 0
    freed = 0
    for f in sorted(d.rglob("*"), reverse=True):
        if f.is_file() and not _is_final(f, d):
            freed += f.stat().st_size
            f.unlink()
        elif f.is_dir() and not any(f.iterdir()):
            f.rmdir()
    return freed


def report(workspace_id: int | None = None) -> dict[str, Any]:
    where, args = ("WHERE workspace_id=?", (workspace_id,)) if workspace_id else ("", ())
    rows = db.q(f"SELECT id, title, status, format, kind, parent_id, youtube_video_id, created_at FROM productions "
                f"{where} ORDER BY id DESC", args)
    total_final = total_work = 0
    for r in rows:
        r["bytes"] = size_of(folder(r["id"]))
        r["working"] = working_bytes(r["id"])
        total_final += r["bytes"] - r["working"]
        total_work += r["working"]
    uploads = db.q1("SELECT COALESCE(SUM(size_bytes),0) AS n FROM media WHERE status='ready'"
                    + (" AND workspace_id=?" if workspace_id else ""), args)["n"]
    out = {"videos": rows, "final_bytes": total_final, "working_bytes": total_work, "uploads_bytes": int(uploads)}
    if workspace_id is None or workspace_id == 1:
        out["cache_bytes"] = size_of(config.DATA_DIR / "cache")
        out["disk_free_bytes"] = shutil.disk_usage(config.DATA_DIR).free
    return out


def clear_cache(older_than_days: int = 0) -> int:
    """Downloaded stock clips/images are re-fetched when needed again."""
    import time
    cutoff = time.time() - older_than_days * 86400
    freed = 0
    for f in (config.DATA_DIR / "cache").rglob("*"):
        if f.is_file() and f.stat().st_mtime < cutoff:
            freed += f.stat().st_size
            f.unlink()
    return freed
