"""Free lip-synced talking clips: LTX-2 (Lightricks, audio-to-video) on a Hugging Face ZeroGPU Space, driven by OUR
voice track — the picture's person speaks exactly the words of the recorded line.

The Space makes ~3-second clips, so a line is cut into 3-second pieces; each piece is a clip that starts from the
last frame of the previous one (no jump between pieces); the pieces are trimmed to the audio and joined, the last
frame is held for `hold` seconds, and the original voice is laid over the result.

Shares the owner's free daily ZeroGPU minutes with the Wan clips (same quota memory: once the Space says the
minutes are used up, neither is asked again until the reset). Has its own daily clip limit. Every failure is
classified (zerogpu.SpaceError kinds / QuotaExceeded) so the caller keeps the still picture and says why.
Licence: LTX-2 Community License — free for businesses under $10M annual revenue.
"""
from __future__ import annotations

import json
import math
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .. import config, db, settings
from . import zerogpu
from .zerogpu import QuotaExceeded, SpaceError

DEFAULT_SPACE = "alexnasa/ltx-2-TURBO"
ENDPOINT = "generate_video"
PROVIDER = "hf_ltx2"
PIECE = 3.0                       # seconds of audio per clip (the Space returns ~3.04 s whatever it is given)
REQUIRED = ("first_frame", "prompt", "audio_path")
KNOWN = ("first_frame", "end_frame", "prompt", "input_video", "generation_mode", "enhance_prompt", "seed",
         "randomize_seed", "height", "width", "camera_lora", "audio_path")
DEFAULT_PER_DAY = 2               # each 3 s piece reserves ~104 s of the free account's few GPU minutes a day
_contract: dict[str, list[str]] = {}


class TooLong(ValueError):
    """The line needs more pieces than allowed for one shot — the still picture is kept."""


def space() -> str:
    return settings.get("film_studio.talking_space") or DEFAULT_SPACE


def base_url() -> str:
    return "https://" + space().replace("/", "-").replace("_", "-").replace(".", "-").lower() + ".hf.space"


def enabled() -> bool:
    return bool(settings.get("film_studio.talking", True)) and zerogpu.configured()


def budget() -> dict[str, int]:
    try:
        limit = max(0, min(100, int(settings.get("film_studio.talking_clips_per_day", DEFAULT_PER_DAY))))
    except (TypeError, ValueError):
        limit = DEFAULT_PER_DAY
    day = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    used = int(db.q1("SELECT COUNT(*) AS n FROM ai_usage WHERE provider=? AND ok=1 AND created_at>=?",
                     (PROVIDER, day))["n"])
    return {"limit": limit, "used": used, "left": max(0, limit - used)}


def pieces_for(seconds: float) -> int:
    return max(1, math.ceil(round(seconds, 2) / PIECE))


def available(seconds: float, max_pieces: int = 4) -> str:
    """'' when a talking clip of this length can be made now, else why not."""
    if not settings.get("film_studio.talking", True):
        return "talking clips are switched off (Settings → Film Studio)"
    if not zerogpu.configured():
        return "no Hugging Face token (Settings → Credentials)"
    if zerogpu.quota_until():
        return "today's free GPU minutes are used up"
    n = pieces_for(seconds)
    if n > max_pieces:
        return f"the line is {seconds:.1f} s — longer than {max_pieces * PIECE:.0f} s of talking per shot"
    if budget()["left"] < n:
        return "today's talking-clip limit is reached"
    return ""


def _record(ok: bool, error: str = "", seconds: float = 0.0) -> None:
    try:
        db.insert("ai_usage", {"provider": PROVIDER, "model": space(), "task": "talking_clip", "input_tokens": 0,
                               "output_tokens": int(seconds * 10), "cost_usd": 0.0, "ok": int(ok),
                               "error": error[:300], "created_at": db.iso()})
    except Exception:  # usage logging must never stop a video
        pass


def contract(c) -> list[str]:
    url = base_url()
    if url in _contract:
        return _contract[url]
    r = c.get(f"{url}/gradio_api/info")
    zerogpu._http(r, "info")
    try:
        names = [p["parameter_name"] for p in r.json()["named_endpoints"][f"/{ENDPOINT}"]["parameters"]]
    except (KeyError, ValueError, TypeError) as e:
        raise SpaceError(f"the talking Space has no /{ENDPOINT} endpoint — its interface changed", "interface") from e
    missing = [p for p in REQUIRED if p not in names]
    unknown = [n for n in names if n not in KNOWN]
    if missing or unknown:
        raise SpaceError(f"the talking Space's interface changed (missing {missing}, new {unknown}) — not called",
                         "interface")
    _contract[url] = names
    return names


def _ff(args: list[str]) -> None:
    r = subprocess.run([config.FFMPEG, "-v", "error", "-y", *args], capture_output=True, text=True, timeout=600)
    if r.returncode:
        raise RuntimeError(f"ffmpeg failed: {r.stderr[-300:]}")


def _duration(path: Path) -> float:
    r = subprocess.run([config.FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True, timeout=60)
    return float((json.loads(r.stdout or "{}").get("format") or {}).get("duration") or 0)


def _one(c, names: list[str], image: Path, audio: Path, prompt: str, out: Path, size: tuple[int, int],
         seed: int) -> None:
    """One ~3 s clip from the Space (talks the given audio, starts from the given picture)."""
    url = base_url()
    up = c.post(f"{url}/gradio_api/upload", files=[("files", (image.name, image.read_bytes(), "image/png")),
                                                    ("files", (audio.name, audio.read_bytes(), "audio/wav"))])
    zerogpu._http(up, "upload")
    try:
        img_path, wav_path = up.json()[:2]
    except (ValueError, TypeError) as e:
        raise SpaceError("the talking Space's upload answer was not understood", "malformed") from e
    fd = lambda p: {"path": p, "meta": {"_type": "gradio.FileData"}}  # noqa: E731
    values = {"first_frame": fd(img_path), "end_frame": None, "prompt": prompt[:900], "input_video": None,
              "generation_mode": "Image-to-Video", "enhance_prompt": False, "seed": seed, "randomize_seed": False,
              "width": size[0], "height": size[1], "camera_lora": "No LoRA", "audio_path": fd(wav_path)}
    r = c.post(f"{url}/gradio_api/call/{ENDPOINT}", json={"data": [values[n] for n in names]})
    zerogpu._http(r, "call")
    try:
        event = r.json()["event_id"]
    except (ValueError, KeyError, TypeError) as e:
        raise SpaceError("the talking Space did not return a job id", "malformed") from e
    result, error, kind = None, None, ""
    with c.stream("GET", f"{url}/gradio_api/call/{ENDPOINT}/{event}") as s:
        zerogpu._http(s, "result stream")
        for line in s.iter_lines():
            if line.startswith("event:"):
                kind = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                payload = line.split(":", 1)[1].strip()
                if kind == "complete":
                    try:
                        result = json.loads(payload)
                    except ValueError as e:
                        raise SpaceError("the talking Space's result was not valid JSON", "malformed") from e
                    break
                if kind == "error":
                    error = payload
                    break
    if error is not None or not result:
        msg = error if error is not None else "no result from the talking Space"
        _record(False, msg)
        if error is not None and zerogpu._quota_text(msg):
            q = QuotaExceeded("Today's free GPU minutes are used up — talking clips wait for the reset.",
                              retry_after=zerogpu.retry_after_from(msg))
            zerogpu._remember_quota(q.retry_after)
            raise q
        raise SpaceError(f"The talking Space returned an error: {msg[:200]}", "server")
    try:
        video = result[0].get("video") if isinstance(result[0], dict) and "video" in result[0] else result[0]
        file_url = (video or {}).get("url") or f"{url}/gradio_api/file={(video or {})['path']}"
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise SpaceError("the talking Space's result had no video file", "malformed") from e
    dl = c.get(file_url)
    zerogpu._http(dl, "download")
    out.write_bytes(dl.content)
    why = zerogpu.valid(zerogpu.inspect(out))
    if why:
        out.unlink(missing_ok=True)
        _record(False, f"invalid video: {why}")
        raise SpaceError(f"the talking Space returned an unusable video: {why}", "invalid_video")


def talk(image: Path, audio: Path, prompt: str, out: Path, *, size: tuple[int, int] = (896, 512),
         hold: float = 0.0, max_pieces: int = 4, seed: int = 42) -> dict[str, Any]:
    """A clip of the picture's person speaking `audio` (lip-synced), `hold` seconds of the last frame at the end,
    with the original audio laid over it. Raises TooLong / QuotaExceeded / SpaceError (kinds) — never fakes one."""
    seconds = _duration(audio)
    if seconds <= 0.2:
        raise SpaceError("the voice line is empty", "invalid_audio")
    why = available(seconds, max_pieces)
    if why:
        if "used up" in why:
            raise QuotaExceeded(why)
        if "longer than" in why:
            raise TooLong(why)
        raise SpaceError(why, "not_configured" if "token" in why or "switched off" in why else "limit")
    if not settings.get("film_studio.zerogpu_window_start"):
        settings.set("film_studio.zerogpu_window_start", db.iso())
    work = out.parent / f"{out.stem}_parts"
    work.mkdir(parents=True, exist_ok=True)
    n = pieces_for(seconds)
    started = time.time()
    start_img, parts = image, []
    try:
        with zerogpu._client() as c:
            names = contract(c)
            for i in range(n):
                length = min(PIECE, seconds - i * PIECE)
                chunk = work / f"a{i:02d}.wav"
                _ff(["-i", str(audio), "-ss", f"{i * PIECE:.3f}", "-t", f"{length:.3f}", str(chunk)])
                clip = work / f"v{i:02d}.mp4"
                if not (clip.exists() and not zerogpu.valid(zerogpu.inspect(clip))):   # reuse after a restart
                    t0 = time.time()
                    _one(c, names, start_img, chunk, prompt, clip, size, seed + i)
                    _record(True, seconds=time.time() - t0)
                parts.append((clip, length))
                last = work / f"last{i:02d}.png"
                _ff(["-sseof", "-0.08", "-i", str(clip), "-frames:v", "1", "-update", "1", str(last)])
                start_img = last                                      # the next piece continues from here
    except httpx.TransportError as e:
        _record(False, f"network: {type(e).__name__}")
        raise SpaceError(f"could not reach the talking Space ({type(e).__name__})", "network") from e
    graph = "".join(f"[{k}:v]trim=duration={ln:.3f},setpts=PTS-STARTPTS,fps={config.FPS},"
                    f"scale={size[0]}:{size[1]}:flags=bicubic,setsar=1[p{k}];" for k, (_, ln) in enumerate(parts))
    graph += "".join(f"[p{k}]" for k in range(len(parts))) + f"concat=n={len(parts)}:v=1:a=0[cat];" \
             f"[cat]tpad=stop_mode=clone:stop_duration={max(0.0, hold):.3f},format=yuv420p[v]"
    inputs = [x for clip, _ in parts for x in ("-i", str(clip))]
    _ff([*inputs, "-i", str(audio), "-filter_complex", graph, "-map", "[v]", "-map", f"{len(parts)}:a",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "160k", str(out)])
    meta = zerogpu.inspect(out)
    why = zerogpu.valid(meta)
    if why:
        raise SpaceError(f"joining the talking pieces failed: {why}", "invalid_video")
    return {"provider": PROVIDER, "model": f"LTX-2 audio-to-video ({space()})", "pieces": n,
            "speech_seconds": round(seconds, 2), "wall_seconds": round(time.time() - started, 1), "video": meta,
            "lipsync": "audio-driven (LTX-2 generated the mouth from this voice track)"}
