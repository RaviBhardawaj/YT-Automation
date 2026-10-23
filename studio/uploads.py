"""Uploaded media: streamed to disk, validated, inspected with ffprobe, never modified.

Originals live under data/workspaces/<ws>/uploads/ and are read-only inputs to editing jobs; every
edited output is a separate file linked back to its sources (productions.source_media).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, AsyncIterator

from . import config, db, logs, plans

KINDS = {
    "video": {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"},
    "audio": {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"},
    "image": {".jpg", ".jpeg", ".png", ".webp"},
    "subtitle": {".srt", ".vtt"},
    "document": {".txt", ".md"},
}
TEXT_LIMIT = 2 * 1024 * 1024


class UploadError(Exception):
    pass


def kind_of(name: str) -> str | None:
    ext = Path(name).suffix.lower()
    return next((k for k, exts in KINDS.items() if ext in exts), None)


def safe_name(name: str) -> str:
    base = Path(name).name
    stem = re.sub(r"[^\w.\- ]+", "_", Path(base).stem, flags=re.UNICODE).strip(" ._")[:80] or "file"
    return stem + Path(base).suffix.lower()


def ws_dir(ws_id: int) -> Path:
    d = config.DATA_DIR / "workspaces" / f"{ws_id:05d}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def probe(path: Path) -> dict[str, Any]:
    """Real media properties from ffprobe (raises UploadError when the file isn't decodable media)."""
    r = subprocess.run([config.FFPROBE, "-v", "error", "-show_entries",
                        "format=duration:stream=codec_type,width,height,r_frame_rate,codec_name",
                        "-of", "json", str(path)], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise UploadError("this file could not be read as media (damaged or unsupported format)")
    info = json.loads(r.stdout or "{}")
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fps = None
    if v and v.get("r_frame_rate") and "/" in v["r_frame_rate"]:
        n, d = v["r_frame_rate"].split("/")
        fps = round(float(n) / float(d), 3) if float(d) else None
    dur = info.get("format", {}).get("duration")
    return {"duration": float(dur) if dur not in (None, "N/A") else None, "width": v.get("width") if v else None,
            "height": v.get("height") if v else None, "fps": fps, "has_audio": int(a is not None),
            "has_video": v is not None, "vcodec": v.get("codec_name") if v else None,
            "acodec": a.get("codec_name") if a else None}


async def save_stream(ws_id: int, user_id: int | None, name: str, chunks: AsyncIterator[bytes],
                      channel_id: int | None = None) -> dict[str, Any]:
    kind = kind_of(name)
    if not kind:
        allowed = ", ".join(sorted(e for exts in KINDS.values() for e in exts))
        raise UploadError(f"'{Path(name).suffix or name}' files aren't supported. Allowed: {allowed}")
    lim = plans.limits(ws_id)
    max_bytes = TEXT_LIMIT if kind in ("subtitle", "document") else int(lim["max_upload_mb"]) * 1024 * 1024
    plans.check(ws_id, "storage_bytes", 0)
    folder = ws_dir(ws_id) / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".part-{os.getpid()}-{hashlib.sha1(os.urandom(8)).hexdigest()}"
    h, size = hashlib.sha256(), 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in chunks:
                size += len(chunk)
                if size > max_bytes:
                    raise UploadError(f"file is larger than your plan's {max_bytes // (1024 * 1024)} MB upload limit")
                h.update(chunk)
                f.write(chunk)
        if size == 0:
            raise UploadError("the file is empty")
        plans.check(ws_id, "storage_bytes", size)
        digest = h.hexdigest()
        dup = db.q1("SELECT * FROM media WHERE workspace_id=? AND sha256=? AND status='ready'", (ws_id, digest))
        if dup:
            tmp.unlink(missing_ok=True)
            return {**dup, "duplicate": True}
        meta: dict[str, Any] = {}
        if kind in ("video", "audio", "image"):
            meta = probe(tmp)
            if kind == "video" and not meta["has_video"]:
                raise UploadError("this video file has no picture track")
            if kind in ("video", "audio") and meta["duration"] and meta["duration"] > lim["max_upload_minutes"] * 60:
                raise UploadError(f"media is {meta['duration'] / 60:.0f} min; your plan allows "
                                  f"{lim['max_upload_minutes']} min per upload")
        else:
            try:
                tmp.read_text(encoding="utf-8")
            except UnicodeDecodeError as e:
                raise UploadError("text files must be UTF-8") from e
        final = folder / f"{digest[:12]}_{safe_name(name)}"
        os.replace(tmp, final)
        os.chmod(final, 0o444)                     # originals are read-only: edits always write new files
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    mid = db.insert("media", {
        "workspace_id": ws_id, "channel_id": channel_id, "kind": kind, "original_name": Path(name).name[:200],
        "path": str(final), "size_bytes": size, "sha256": digest, "mime": None,
        "duration": meta.get("duration"), "width": meta.get("width"), "height": meta.get("height"),
        "fps": meta.get("fps"), "has_audio": meta.get("has_audio"), "meta": json.dumps(meta),
        "status": "ready", "created_by": user_id, "created_at": db.iso()})
    plans.record(ws_id, "uploads", 1, idem_key=f"upload:{mid}")
    logs.event("info", "uploads", f"Uploaded {kind} '{Path(name).name}' ({size / 1e6:.1f} MB) as media #{mid}")
    return db.q1("SELECT * FROM media WHERE id=?", (mid,))


def get(media_id: int, ws_id: int | None) -> dict[str, Any] | None:
    m = db.q1("SELECT * FROM media WHERE id=? AND status='ready'", (media_id,))
    if not m or (ws_id is not None and m["workspace_id"] != ws_id):
        return None
    m["meta"] = db.loads(m["meta"], {}) or {}
    m["transcript"] = db.loads(m["transcript"], None)
    return m


def verify_intact(m: dict[str, Any]) -> bool:
    """The original upload still has exactly the bytes that were uploaded."""
    p = Path(m["path"])
    if not p.exists() or p.stat().st_size != m["size_bytes"]:
        return False
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest() == m["sha256"]


def delete(media_id: int, ws_id: int) -> None:
    """Only on the owner's explicit request. Outputs made from it are kept."""
    m = get(media_id, ws_id)
    if not m:
        raise UploadError("not found")
    p = Path(m["path"])
    if p.exists():
        os.chmod(p, 0o644)
        p.unlink()
    db.update("media", media_id, {"status": "deleted", "deleted_at": db.iso()})
    logs.event("info", "uploads", f"Media #{media_id} '{m['original_name']}' deleted by its owner")
