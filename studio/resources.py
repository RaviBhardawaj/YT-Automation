"""Keep the Mac usable while the studio works.

A 16 GB Mac freezes when a heavy local model (FLUX pictures, SadTalker faces, voice cloning) starts while memory is
already short, or when the disk is so full that macOS can't grow its swap file. So heavy steps first check that
there is room; when there isn't, the step WAITS and tries again later instead of starting — the video is delayed,
the Mac stays responsive. Nothing is skipped or faked: the step simply runs when memory or disk is back.

Together with the studio's lower CPU priority (LaunchAgent `Nice`), heavy-task lanes (only one heavy job at a time)
and a cap on FFmpeg threads, this keeps the Mac usable while videos are made.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

from . import config

MIN_FREE_DISK_GB = 4.0          # below this, heavy work waits (renders and swap both need room)
RETRY_SECONDS = 600


class NotEnoughRoom(RuntimeError):
    """Temporary: the worker waits RETRY_SECONDS and tries again (the attempt isn't counted as a failure)."""
    temporary = True
    retry_after = RETRY_SECONDS


def available_memory_gb() -> float:
    """Memory macOS can give a new process without swapping: free + inactive + purgeable + speculative pages."""
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return 99.0                                     # not macOS / can't tell: never block
    page = int((re.search(r"page size of (\d+)", out) or [0, 16384])[1])
    pages = {k.strip().lower(): int(v) for k, v in re.findall(r"^(.+?):\s+(\d+)\.?$", out, re.M)}
    n = sum(pages.get(k, 0) for k in ("pages free", "pages inactive", "pages purgeable", "pages speculative"))
    return round(n * page / 2**30, 1) if n else 99.0


def free_disk_gb() -> float:
    return round(shutil.disk_usage(config.DATA_DIR).free / 2**30, 1)


def ensure(memory_gb: float, what: str) -> None:
    """Raise NotEnoughRoom when starting `what` now would push the Mac into heavy swapping or a full disk."""
    if os.environ.get("STUDIO_OFFLINE") == "1" and os.environ.get("STUDIO_RESOURCE_GUARD") != "1":
        return                                          # tests run without the guard unless they ask for it
    disk = free_disk_gb()
    if disk < MIN_FREE_DISK_GB:
        raise NotEnoughRoom(f"{what} waits: only {disk} GB free on the disk (needs {MIN_FREE_DISK_GB} GB) — "
                            "clear working files or uninstall unused AI tools on the Storage page")
    mem = available_memory_gb()
    if mem < memory_gb:
        raise NotEnoughRoom(f"{what} waits: {mem} GB memory free, needs about {memory_gb} GB — it starts by itself "
                            "when other apps or tasks free memory")


def ffmpeg_threads() -> str:
    """Leave two cores for you: FFmpeg would otherwise use every core."""
    return str(max(2, (os.cpu_count() or 4) - 2))
