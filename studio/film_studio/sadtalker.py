"""Talking presenter on this Mac: SadTalker (Apache-2.0) animates the face in one photo from OUR voice track —
lips, expression and blinks follow the recorded line. No daily limit and no internet; used when the free LTX-2 GPU
minutes are used up (LTX-2 looks more real, so it is tried first).

SadTalker lives in its own folder with its own Python (~/sadtalker, ~3 GB) so its old library pins never touch
the app. It is called as a separate process; the animated face is pasted back into the full picture
(`--preprocess full --still`), so the studio set and the suit stay exactly as drawn. The 256 model is used:
the 512 one needs more memory than a 16 GB Mac can spare while the studio is running.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .. import config, settings

PROVIDER = "sadtalker_local"
TIMEOUT = 1800                    # seconds for one line; a 6 s line takes a few minutes on an M2


class NotInstalled(RuntimeError):
    """SadTalker isn't on this Mac (or was uninstalled from the Storage page)."""


def home() -> Path:
    return Path(settings.get("film_studio.sadtalker_dir") or Path.home() / "sadtalker").expanduser()


def python() -> Path:
    return home() / ".venv" / "bin" / "python"


def installed() -> bool:
    if os.environ.get("STUDIO_OFFLINE") == "1":       # tests / offline runs never start a minutes-long local model
        return False
    h = home()
    return (h / "inference.py").exists() and python().exists() and \
        (h / "checkpoints" / "SadTalker_V0.0.2_256.safetensors").exists()


def enabled() -> bool:
    return bool(settings.get("film_studio.sadtalker", True)) and installed()


def _duration(path: Path) -> float:
    r = subprocess.run([config.FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True, timeout=60)
    return float((json.loads(r.stdout or "{}").get("format") or {}).get("duration") or 0)


def _inspect(path: Path) -> dict[str, Any]:
    r = subprocess.run([config.FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height:format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True, timeout=60)
    d = json.loads(r.stdout or "{}")
    s = (d.get("streams") or [{}])[0]
    return {"width": int(s.get("width") or 0), "height": int(s.get("height") or 0),
            "duration": float((d.get("format") or {}).get("duration") or 0)}


def talk(image: Path, audio: Path, out: Path, *, size: tuple[int, int], hold: float = 0.0) -> dict[str, Any]:
    """A clip of the picture's person speaking `audio`, at `size`, with `hold` seconds of the last frame and the
    original voice laid over it. Raises NotInstalled / RuntimeError — never fakes a clip."""
    if not enabled():
        raise NotInstalled("SadTalker is not installed on this Mac (Storage page)")
    image, audio, out = Path(image).resolve(), Path(audio).resolve(), Path(out).resolve()  # it runs in its own folder
    seconds = _duration(audio)
    if seconds <= 0.2:
        raise RuntimeError("the voice line is empty")
    from .. import resources
    resources.ensure(4.0, "A talking face on this Mac (SadTalker)")
    started = time.time()
    tmp = Path(tempfile.mkdtemp(prefix="sadtalker-"))
    try:
        # SadTalker runs `ffmpeg` by name; the background service's PATH doesn't include Homebrew, so add the
        # folder of the ffmpeg this app uses
        path = os.pathsep.join([str(Path(config.FFMPEG).parent), os.environ.get("PATH", "/usr/bin:/bin")])
        env = {**os.environ, "PATH": path, "PYTORCH_ENABLE_MPS_FALLBACK": "1", "PYTHONUNBUFFERED": "1"}
        r = subprocess.run([str(python()), "inference.py", "--driven_audio", str(audio), "--source_image", str(image),
                            "--result_dir", str(tmp), "--still", "--preprocess", "full", "--size", "256"],
                           cwd=home(), env=env, capture_output=True, text=True, timeout=TIMEOUT)
        clips = sorted(tmp.rglob("*.mp4"), key=lambda f: f.stat().st_size)
        if r.returncode or not clips:
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or ["no video produced"]
            raise RuntimeError(f"SadTalker failed: {tail[0][:200]}")
        raw = clips[-1]
        w, h = size
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=bicubic,crop={w}:{h},setsar=1,"
              f"fps={config.FPS},tpad=stop_mode=clone:stop_duration={max(0.0, hold):.3f},format=yuv420p")
        ff = subprocess.run([config.FFMPEG, "-v", "error", "-y", "-i", str(raw), "-i", str(audio), "-vf", vf,
                             "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                             "-c:a", "aac", "-b:a", "160k", "-t", f"{seconds + max(0.0, hold):.3f}", str(out)],
                            capture_output=True, text=True, timeout=600)
        if ff.returncode:
            raise RuntimeError(f"ffmpeg failed: {ff.stderr[-200:]}")
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"SadTalker took longer than {TIMEOUT // 60} minutes") from e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    meta = _inspect(out)
    if not meta["width"] or meta["duration"] < seconds - 0.3:
        raise RuntimeError(f"SadTalker's clip is unusable ({meta})")
    return {"provider": PROVIDER, "model": "SadTalker 256 (on this Mac)", "pieces": 1,
            "speech_seconds": round(seconds, 2), "wall_seconds": round(time.time() - started, 1), "video": meta,
            "lipsync": "audio-driven (SadTalker animated the face from this voice track)"}
