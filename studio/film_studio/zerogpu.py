"""Free real-motion clips: a Wan 2.2 image-to-video Space on Hugging Face ZeroGPU, called over its public Gradio
HTTP API (no extra packages). The owner's free Hugging Face token makes the calls count against their own daily
ZeroGPU quota (about 5 GPU-minutes a day on a free account). Quota exhaustion is a wait, never a failure.

Protocol: upload the picture → POST /gradio_api/call/<endpoint> → read the result stream → download the video.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from .. import db, settings

DEFAULT_SPACE = "zerogpu-aoti/wan2-2-fp8da-aoti-faster"     # run by Hugging Face's ZeroGPU team
ENDPOINT = "generate_video"
NEGATIVE = ("static, still frame, blurry details, subtitles, text, watermark, worst quality, low quality, deformed, "
            "extra fingers, bad hands, bad face, fused fingers, messy background, many people in background")


class QuotaExceeded(Exception):
    """Today's free ZeroGPU minutes are used up: retried after the quota window resets."""
    temporary = True

    def __init__(self, msg: str) -> None:
        super().__init__(msg)
        start = settings.get("film_studio.zerogpu_window_start")
        reset = (datetime.fromisoformat(start.replace("Z", "+00:00")) + timedelta(hours=24)) if start else None
        now = datetime.now(timezone.utc)
        self.retry_after = int((reset - now).total_seconds()) + 300 if reset and reset > now else 3600


class SpaceError(RuntimeError):
    pass


def space() -> str:
    return settings.get("film_studio.zerogpu_space") or DEFAULT_SPACE


def base_url() -> str:
    """'owner/space_name' → https://owner-space-name.hf.space (Hugging Face's Space subdomain rule)."""
    sub = space().replace("/", "-").replace("_", "-").replace(".", "-").lower()
    return f"https://{sub}.hf.space"


def configured() -> bool:
    return settings.has_secret("hf_token")


def _client() -> httpx.Client:
    # IPv4 only: this network advertises IPv6 it can't carry reliably
    return httpx.Client(transport=httpx.HTTPTransport(local_address="0.0.0.0", retries=1),
                        timeout=httpx.Timeout(600, connect=20),
                        headers={"Authorization": f"Bearer {settings.secret('hf_token')}"})


def _quota_text(msg: str) -> bool:
    # an empty error ("null") is how a Space reports a spent GPU allowance
    return (msg or "").strip().lower() in ("null", "none", "") or \
        any(w in (msg or "").lower() for w in ("quota", "exceeded", "gpu task aborted", "zerogpu"))


def _record(ok: bool, error: str = "", seconds: float = 0.0) -> None:
    try:
        db.insert("ai_usage", {"provider": "hf_zerogpu", "model": space(), "task": "video_shot", "input_tokens": 0,
                               "output_tokens": int(seconds * 10), "cost_usd": 0.0, "ok": int(ok),
                               "error": error[:300], "created_at": db.iso()})
    except Exception:  # usage logging must never stop a film
        pass


def generate(image: Path, prompt: str, out: Path, *, seconds: float = 3.5, seed: int = 42) -> dict[str, Any]:
    """One short clip that starts from `image` and moves as `prompt` describes. Raises QuotaExceeded to wait."""
    if not configured():
        raise SpaceError("No Hugging Face token — add a free one under Settings → Credentials.")
    if not settings.get("film_studio.zerogpu_window_start"):
        settings.set("film_studio.zerogpu_window_start", db.iso())
    url = base_url()
    started = time.time()
    with _client() as c:
        up = c.post(f"{url}/gradio_api/upload", files={"files": (image.name, image.read_bytes(), "image/png")})
        up.raise_for_status()
        path = up.json()[0]
        data = [{"path": path, "meta": {"_type": "gradio.FileData"}}, prompt[:900], 6, NEGATIVE,
                max(1.5, min(float(seconds), 5.0)), 1, 1, seed, False]
        r = c.post(f"{url}/gradio_api/call/{ENDPOINT}", json={"data": data})
        r.raise_for_status()
        event = r.json()["event_id"]
        result, error = None, None
        with c.stream("GET", f"{url}/gradio_api/call/{ENDPOINT}/{event}") as s:
            kind = ""
            for line in s.iter_lines():
                if line.startswith("event:"):
                    kind = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    payload = line.split(":", 1)[1].strip()
                    if kind == "complete":
                        result = json.loads(payload)
                        break
                    if kind == "error":
                        error = payload
                        break
        if error is not None or not result:
            msg = error or "no result from the Space"
            _record(False, msg)
            if _quota_text(msg):
                raise QuotaExceeded("Today's free ZeroGPU minutes are used up — the clip is made after the reset.")
            raise SpaceError(f"The video Space returned an error: {msg[:200]}")
        video = result[0].get("video") if isinstance(result[0], dict) and "video" in result[0] else result[0]
        file_url = (video or {}).get("url") or f"{url}/gradio_api/file={(video or {}).get('path')}"
        dl = c.get(file_url)
        dl.raise_for_status()
        out.write_bytes(dl.content)
    used = time.time() - started
    _record(True, seconds=used)
    return {"file": str(out), "seconds_requested": seconds, "wall_seconds": round(used, 1), "space": space(),
            "seed": result[1] if len(result) > 1 else seed}
