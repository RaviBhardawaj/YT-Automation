"""Host inspection for the setup screen and for hardware-fit checks before downloads."""
from __future__ import annotations

import platform
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx

from . import config


def _run(args: list[str], timeout: float = 10) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


@lru_cache(maxsize=1)
def hardware() -> dict[str, Any]:
    mem = _run(["sysctl", "-n", "hw.memsize"])
    return {
        "os": f"macOS {platform.mac_ver()[0]}" if sys.platform == "darwin" else platform.platform(),
        "arch": platform.machine(),
        "cpu": _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or platform.processor(),
        "cores": int(_run(["sysctl", "-n", "hw.ncpu"]) or 0),
        "ram_gb": round(int(mem) / 2**30, 1) if mem.isdigit() else None,
        "python": platform.python_version(),
    }


def disk_free_gb() -> float:
    return round(shutil.disk_usage(config.DATA_DIR).free / 2**30, 1)


@lru_cache(maxsize=1)
def ffmpeg_info() -> dict[str, Any]:
    if not Path(config.FFMPEG).exists():
        return {"ok": False, "detail": "FFmpeg not found. Install with: brew install ffmpeg"}
    version = _run([config.FFMPEG, "-hide_banner", "-version"]).splitlines()[:1]
    filters = _run([config.FFMPEG, "-hide_banner", "-filters"])
    encoders = _run([config.FFMPEG, "-hide_banner", "-encoders"])
    have = {name: bool(re.search(rf"\s{name}\s", filters)) for name in
            ("xfade", "zoompan", "overlay", "loudnorm", "volumedetect", "blackdetect", "freezedetect", "sidechaincompress")}
    have["libx264"] = "libx264" in encoders
    have["h264_videotoolbox"] = "h264_videotoolbox" in encoders
    required = ("xfade", "overlay", "loudnorm", "libx264")
    missing = [k for k in required if not have.get(k)]
    return {
        "ok": not missing and Path(config.FFPROBE).exists(),
        "version": version[0] if version else "",
        "features": have,
        "detail": ("missing: " + ", ".join(missing)) if missing else "ready",
    }


def pillow_info() -> dict[str, Any]:
    from PIL import features
    raqm = features.check("raqm")
    return {
        "ok": True,
        "raqm": raqm,
        "detail": "complex-script shaping (Hindi) available" if raqm
        else "libraqm missing — Devanagari text will not shape correctly. Run: brew install libraqm",
    }


def ollama_local(host: str) -> dict[str, Any]:
    binary = shutil.which("ollama") or ("/Applications/Ollama.app" if Path("/Applications/Ollama.app").exists() else None)
    info: dict[str, Any] = {"installed": bool(binary), "running": False, "models": []}
    try:
        r = httpx.get(host.rstrip("/") + "/api/tags", timeout=3)
        if r.status_code == 200:
            info["running"] = True
            info["models"] = [{"name": m.get("name"), "size_gb": round(m.get("size", 0) / 1e9, 2)}
                              for m in r.json().get("models", [])]
    except httpx.HTTPError:
        pass
    return info


def model_fits(size_bytes: int) -> dict[str, Any]:
    """Rough fit check for a quantised local model: weights + ~2.5 GB runtime/context headroom
    must stay under ~75 % of RAM, and the download must leave 10 GB of disk free."""
    hw = hardware()
    ram = hw.get("ram_gb") or 0
    need_ram = size_bytes / 2**30 + 2.5
    free = disk_free_gb()
    return {
        "ram_gb": ram, "needed_ram_gb": round(need_ram, 1),
        "ram_ok": ram > 0 and need_ram <= ram * 0.75,
        "disk_free_gb": free, "disk_ok": free - size_bytes / 2**30 >= 10,
    }


def summary() -> dict[str, Any]:
    from . import settings
    from .media import tts
    hw = hardware()
    return {
        "hardware": hw,
        "disk_free_gb": disk_free_gb(),
        "ffmpeg": ffmpeg_info(),
        "pillow": pillow_info(),
        "ollama": ollama_local(settings.get("ai.ollama_host")),
        "tts": tts.engine_status(),
        "data_dir": str(config.DATA_DIR),
    }
