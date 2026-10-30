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


# ------------------------------------------------------------------ this Mac: memory, disk, installed AI tools

def _hf_cache() -> Path:
    import os
    home = os.environ.get("HF_HOME")
    return (Path(home) / "hub") if home else Path.home() / ".cache" / "huggingface" / "hub"


def _unique_bytes(path: Path) -> int:
    """Size on disk; Hugging Face model folders are links into shared blobs, so each blob is counted once."""
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    seen, n = set(), 0
    for p in path.rglob("*"):
        try:
            r = p.resolve()
            if r.is_file() and r not in seen:
                seen.add(r)
                n += r.stat().st_size
        except OSError:
            continue
    return n


def components() -> list[dict[str, Any]]:
    """Big optional parts installed for the studio, with what each is for and its size. `required` ones have no
    Uninstall button (the studio can't make videos without them)."""
    hf = _hf_cache()
    sad = Path.home() / "sadtalker"
    items = [
        {"key": "local_flux", "name": "Local AI pictures (FLUX.2 klein, 4-bit)",
         "what": "Draws pictures on this Mac when the free online picture services are used up.",
         "paths": [config.ROOT / ".venv-flux", hf / "models--Runpod--FLUX.2-klein-4B-mflux-4bit"],
         "after": "Pictures come from Cloudflare / Hugging Face / stock only.", "reinstall": "Settings › System › Local pictures"},
        {"key": "voiceclone", "name": "Voice cloning (Chatterbox)",
         "what": "Narrates with a cloned voice (yours, with consent).",
         "paths": [config.ROOT / ".venv-voiceclone", hf / "models--ResembleAI--chatterbox"],
         "after": "Cloned voices stop working; Kokoro voices are used.", "reinstall": "Settings › System › Voice cloning"},
        {"key": "parler", "name": "Indic Parler voices",
         "what": "Extra Indian-language voices with style control.",
         "paths": [config.ROOT / ".venv-parler", hf / "models--ai4bharat--indic-parler-tts"],
         "after": "Kokoro voices are used.", "reinstall": "Settings › System"},
        {"key": "sadtalker", "name": "Talking presenter (SadTalker)",
         "what": "Makes the AI News presenter talk on this Mac when the free online GPU minutes are used up.",
         "paths": [sad, Path.home() / ".cache" / "torch" / "hub" / "checkpoints"],
         "after": "Presenter talks only while free online GPU minutes last, otherwise stays a still picture.",
         "reinstall": "ask Claude to reinstall SadTalker"},
        {"key": "whisper", "name": "Speech-to-text (Whisper)",
         "what": "Transcribes uploads for Upload & Edit and the Shorts clipper.",
         "paths": [config.DATA_DIR / "models" / "whisper"],
         "after": "Downloaded again automatically the next time it is needed (~460 MB).", "reinstall": "automatic"},
        {"key": "piper", "name": "Piper voices", "what": "Lightweight backup voices.",
         "paths": [config.VOICES_DIR / "piper"], "after": "Kokoro voices are used.", "reinstall": "Voices page"},
        {"key": "ollama_models", "name": "Local AI writer models (Ollama)",
         "what": "Writes scripts on this Mac with no daily limit.",
         "paths": [Path.home() / ".ollama" / "models"], "after": "Scripts use the free online writers only.",
         "reinstall": "Settings › AI › pull a model"},
        {"key": "kokoro", "name": "Kokoro voices", "what": "Main narration voices (Hindi + English).",
         "paths": [config.VOICES_DIR / "kokoro"], "required": True},
        {"key": "app", "name": "The studio app itself", "what": "Python environment the app runs in.",
         "paths": [config.ROOT / ".venv"], "required": True},
    ]
    for c in items:
        c["bytes"] = sum(_unique_bytes(p) for p in c["paths"])
        c["installed"] = c["bytes"] > 0
        c.setdefault("required", False)
    return items


def uninstall(key: str) -> int:
    """Delete one optional part. Returns bytes freed. Refused while a video is being made."""
    c = next((x for x in components() if x["key"] == key), None)
    if not c or c["required"]:
        raise ValueError("This part can't be uninstalled.")
    if db.q1("SELECT 1 FROM tasks WHERE status='running' AND lane='media'"):
        raise ValueError("A video is being made right now — try again when it has finished.")
    home = Path.home().resolve()
    freed = 0
    for p in c["paths"]:
        p = Path(p)
        if not p.exists() or p.resolve() in (home, config.ROOT.resolve(), config.DATA_DIR.resolve()):
            continue
        n = _unique_bytes(p)
        shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
        freed += n
    logs.event("info", "storage", f"Uninstalled {c['name']} — {freed / 1e9:.2f} GB freed")
    from . import ops
    ops.audit("owner", "uninstalled", detail={"part": key, "bytes": freed})
    return freed


def machine() -> dict[str, Any]:
    """What this Mac is using right now: memory, swap, processor load, disk, and the biggest programs in memory."""
    import os
    import re
    import subprocess

    def run(*args: str) -> str:
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return ""
    out: dict[str, Any] = {}
    total = int(run("sysctl", "-n", "hw.memsize").strip() or 0)
    vm = run("vm_stat")
    page = int((re.search(r"page size of (\d+)", vm) or [0, 16384])[1])
    pages = {k.strip().lower(): int(v.strip().rstrip(".")) for k, v in re.findall(r"^(.+?):\s+(\d+)\.?$", vm, re.M)}
    used = (pages.get("pages active", 0) + pages.get("pages wired down", 0)
            + pages.get("pages occupied by compressor", 0)) * page
    out["ram_total"], out["ram_used"] = total, min(used, total) if total else used
    out["ram_pct"] = round(100 * out["ram_used"] / total) if total else 0
    swap = re.search(r"used = ([\d.]+)M", run("sysctl", "-n", "vm.swapusage"))
    out["swap_used"] = int(float(swap[1]) * 2**20) if swap else 0
    try:
        out["load"] = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        out["load"] = []
    out["cores"] = int(run("sysctl", "-n", "hw.ncpu").strip() or 0)
    out["cpu"] = run("sysctl", "-n", "machdep.cpu.brand_string").strip()
    du = shutil.disk_usage(config.DATA_DIR)
    out["disk_total"], out["disk_free"] = du.total, du.free
    out["disk_pct"] = round(100 * (du.total - du.free) / du.total) if du.total else 0
    procs = []
    for line in run("ps", "-axo", "rss=,pid=,comm=", "-m").splitlines()[:10]:
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit():
            procs.append({"mb": int(parts[0]) // 1024, "pid": int(parts[1]), "name": Path(parts[2]).name})
    out["top"] = procs
    out["studio_mb"] = next((p["mb"] for p in procs if p["pid"] == os.getpid()),
                            int((run("ps", "-o", "rss=", "-p", str(os.getpid())).strip() or 0)) // 1024)
    return out
