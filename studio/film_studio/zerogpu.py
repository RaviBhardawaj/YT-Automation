"""Free real-motion clips: a Wan 2.2 image-to-video Space on Hugging Face ZeroGPU, called over its public Gradio
HTTP API (no extra packages). The owner's free Hugging Face token makes the calls count against their own daily
ZeroGPU quota (a few GPU-minutes a day on a free account). Quota exhaustion is a wait, never a failure.

Protocol: read the Space's API description → upload the picture → POST /gradio_api/call/<endpoint> → read the
result stream → download the video → check with ffprobe/ffmpeg that it really is a playable, moving clip.

Errors are classified, never guessed: an auth problem (401/403), a rate limit (429), a server error (5xx), a
changed Space interface and a malformed answer are each reported as what they are. Only the Space's own quota
messages (or its empty error, which is how ZeroGPU reports a spent allowance) count as "quota used up", and that
is remembered so the Space is not asked again before the window resets.
"""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from .. import config, db, settings

DEFAULT_SPACE = "zerogpu-aoti/wan2-2-fp8da-aoti-faster"     # run by Hugging Face's ZeroGPU team
ENDPOINT = "generate_video"
NEGATIVE = ("static, still frame, frozen, blurry details, subtitles, text, watermark, worst quality, low quality, "
            "deformed, morphing face, extra limbs, extra fingers, bad hands, bad face, fused fingers, messy background, "
            "many people in background, walking backwards, scene cut, sudden transition")
# the parameters this adapter fills, by the names the Space publishes (checked against /gradio_api/info)
PARAMS = ("input_image", "prompt", "steps", "negative_prompt", "duration_seconds", "guidance_scale",
          "guidance_scale_2", "seed", "randomize_seed")
MIN_SECONDS, MAX_SECONDS = 1.5, 5.0
_contract: dict[str, list[str]] = {}


class QuotaExceeded(Exception):
    """Today's free ZeroGPU minutes are used up: retried after the quota window resets."""
    temporary = True

    def __init__(self, msg: str, retry_after: int | None = None) -> None:
        super().__init__(msg)
        if retry_after is None:
            start = settings.get("film_studio.zerogpu_window_start")
            reset = (datetime.fromisoformat(start.replace("Z", "+00:00")) + timedelta(hours=24)) if start else None
            now = datetime.now(timezone.utc)
            retry_after = int((reset - now).total_seconds()) + 300 if reset and reset > now else 3600
        self.retry_after = retry_after


class SpaceError(RuntimeError):
    """A real failure, with what kind it is: auth | rate_limited | server | interface | malformed | invalid_video |
    not_configured | network."""

    def __init__(self, msg: str, kind: str = "error") -> None:
        super().__init__(msg)
        self.kind = kind
        self.temporary = kind in ("rate_limited", "server", "network")


def space() -> str:
    return settings.get("film_studio.zerogpu_space") or DEFAULT_SPACE


def base_url() -> str:
    """'owner/space_name' → https://owner-space-name.hf.space (Hugging Face's Space subdomain rule)."""
    sub = space().replace("/", "-").replace("_", "-").replace(".", "-").lower()
    return f"https://{sub}.hf.space"


def configured() -> bool:
    return settings.has_secret("hf_token")


def quota_until() -> datetime | None:
    """When the Space last said the free minutes are used up, the time it may be asked again."""
    until = db.parse_iso(settings.get("film_studio.zerogpu_quota_until") or "")
    return until if until and until > db.utcnow() else None


def _remember_quota(retry_after: int) -> None:
    settings.set("film_studio.zerogpu_quota_until", db.iso(db.utcnow() + timedelta(seconds=max(600, retry_after))))


def _client() -> httpx.Client:
    # IPv4 only: this network advertises IPv6 it can't carry reliably
    return httpx.Client(transport=httpx.HTTPTransport(local_address="0.0.0.0", retries=1),
                        timeout=httpx.Timeout(600, connect=20),
                        headers={"Authorization": f"Bearer {settings.secret('hf_token')}"})


def retry_after_from(msg: str) -> int | None:
    """Seconds until the free minutes come back, from the Space's own 'Try again in H:MM:SS'."""
    import re
    m = re.search(r"(?i)try again in (\d+):(\d\d):(\d\d)", msg or "")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + 120 if m else None


def _quota_text(msg: str) -> bool:
    # an empty error ("null") is how a ZeroGPU Space reports a spent GPU allowance
    low = (msg or "").strip().lower()
    return low in ("null", "none", "") or any(w in low for w in ("quota", "gpu task aborted", "zerogpu",
                                                                  "exceeded your gpu", "daily limit"))


def _http(r: httpx.Response, step: str) -> None:
    """Raise the right error for a failed HTTP step — never 'quota' for auth or server trouble."""
    if r.status_code < 400:
        return
    if r.status_code in (401, 403):
        raise SpaceError(f"Hugging Face refused the token at {step} (HTTP {r.status_code}) — check the token under "
                         "Settings → Credentials.", "auth")
    if r.status_code == 429:
        raise SpaceError(f"the Space is rate-limiting requests at {step} (HTTP 429) — tried again later", "rate_limited")
    if r.status_code >= 500:
        raise SpaceError(f"the Space had a server error at {step} (HTTP {r.status_code})", "server")
    if r.status_code == 404:
        raise SpaceError(f"the Space has no '{step}' endpoint (HTTP 404) — its interface may have changed", "interface")
    raise SpaceError(f"the Space answered HTTP {r.status_code} at {step}: {r.text[:120]}", "malformed")


def contract(c: httpx.Client) -> list[str]:
    """The Space's published parameter names for the endpoint (cached); raises if the interface no longer fits."""
    url = base_url()
    if url in _contract:
        return _contract[url]
    r = c.get(f"{url}/gradio_api/info")
    _http(r, "info")
    try:
        ep = r.json()["named_endpoints"][f"/{ENDPOINT}"]
        names = [p["parameter_name"] for p in ep["parameters"]]
    except (KeyError, ValueError, TypeError) as e:
        raise SpaceError(f"the Space's API description has no /{ENDPOINT} endpoint — its interface changed",
                         "interface") from e
    missing = [p for p in ("input_image", "prompt") if p not in names]
    unknown = [n for n in names if n not in PARAMS]
    if missing or unknown:
        raise SpaceError(f"the Space's interface changed (missing {missing}, new {unknown}) — not called",
                         "interface")
    _contract[url] = names
    return names


def _record(ok: bool, error: str = "", seconds: float = 0.0) -> None:
    try:
        db.insert("ai_usage", {"provider": "hf_zerogpu", "model": space(), "task": "video_shot", "input_tokens": 0,
                               "output_tokens": int(seconds * 10), "cost_usd": 0.0, "ok": int(ok),
                               "error": error[:300], "created_at": db.iso()})
    except Exception:  # usage logging must never stop a film
        pass


def inspect(path: Path) -> dict[str, Any]:
    """Technical facts of a clip from ffprobe, and whether it decodes. {} when it isn't a readable video."""
    if not path.exists() or path.stat().st_size < 1024:
        return {}
    r = subprocess.run([config.FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=codec_name,width,height,r_frame_rate,nb_frames:format=duration", "-of", "json",
                        str(path)], capture_output=True, text=True, timeout=60)
    try:
        info = json.loads(r.stdout or "{}")
        v = (info.get("streams") or [{}])[0]
    except (ValueError, IndexError):
        return {}
    if not v.get("codec_name"):
        return {}
    num, _, den = (v.get("r_frame_rate") or "0/1").partition("/")
    fps = round(float(num) / max(1.0, float(den or 1)), 2)
    dec = subprocess.run([config.FFMPEG, "-v", "error", "-i", str(path), "-f", "null", "-"], capture_output=True,
                         text=True, timeout=120)
    return {"codec": v["codec_name"], "width": v.get("width"), "height": v.get("height"), "fps": fps,
            "duration": round(float((info.get("format") or {}).get("duration") or 0), 2),
            "decodes": dec.returncode == 0 and "error" not in (dec.stderr or "").lower(),
            "bytes": path.stat().st_size}


def valid(meta: dict[str, Any]) -> str:
    """'' when the clip is usable, else the reason it is not."""
    if not meta:
        return "not a readable video file"
    if not meta.get("decodes"):
        return "the file does not decode to the end (corrupted or truncated)"
    if (meta.get("duration") or 0) < 0.8:
        return f"too short ({meta.get('duration')} s)"
    if not meta.get("width") or not meta.get("height") or not meta.get("fps"):
        return "no picture size or frame rate"
    return ""


def generate(image: Path, prompt: str, out: Path, *, seconds: float = 3.5, seed: int = 42) -> dict[str, Any]:
    """One short clip that starts from `image` and moves as `prompt` describes.
    Raises QuotaExceeded to wait for the free allowance, SpaceError (with .kind) for everything else."""
    if not configured():
        raise SpaceError("No Hugging Face token — add a free one under Settings → Credentials.", "not_configured")
    if quota_until():
        raise QuotaExceeded("Today's free ZeroGPU minutes are used up (remembered) — waiting for the reset.",
                            retry_after=int((quota_until() - db.utcnow()).total_seconds()))
    if not settings.get("film_studio.zerogpu_window_start"):
        settings.set("film_studio.zerogpu_window_start", db.iso())
    url = base_url()
    started = time.time()
    seconds = max(MIN_SECONDS, min(float(seconds), MAX_SECONDS))
    try:
        with _client() as c:
            names = contract(c)
            up = c.post(f"{url}/gradio_api/upload", files={"files": (image.name, image.read_bytes(), "image/png")})
            _http(up, "upload")
            try:
                path = up.json()[0]
            except (ValueError, IndexError, KeyError, TypeError) as e:
                raise SpaceError("the Space's upload answer was not understood", "malformed") from e
            values = {"input_image": {"path": path, "meta": {"_type": "gradio.FileData"}}, "prompt": prompt[:900],
                      "steps": 6, "negative_prompt": NEGATIVE, "duration_seconds": seconds, "guidance_scale": 1,
                      "guidance_scale_2": 1, "seed": seed, "randomize_seed": False}
            r = c.post(f"{url}/gradio_api/call/{ENDPOINT}", json={"data": [values[n] for n in names]})
            _http(r, "call")
            try:
                event = r.json()["event_id"]
            except (ValueError, KeyError, TypeError) as e:
                raise SpaceError("the Space did not return a job id", "malformed") from e
            result, error = None, None
            with c.stream("GET", f"{url}/gradio_api/call/{ENDPOINT}/{event}") as s:
                _http(s, "result stream")
                kind = ""
                for line in s.iter_lines():
                    if line.startswith("event:"):
                        kind = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        payload = line.split(":", 1)[1].strip()
                        if kind == "complete":
                            try:
                                result = json.loads(payload)
                            except ValueError as e:
                                raise SpaceError("the Space's result was not valid JSON", "malformed") from e
                            break
                        if kind == "error":
                            error = payload
                            break
            if error is not None or not result:
                msg = error if error is not None else "no result from the Space"
                _record(False, msg)
                if error is not None and _quota_text(msg):
                    q = QuotaExceeded("Today's free ZeroGPU minutes are used up — the clip is made after the reset.",
                                      retry_after=retry_after_from(msg))
                    _remember_quota(q.retry_after)
                    raise q
                raise SpaceError(f"The video Space returned an error: {msg[:200]}", "server")
            try:
                video = result[0].get("video") if isinstance(result[0], dict) and "video" in result[0] else result[0]
                file_url = (video or {}).get("url") or f"{url}/gradio_api/file={(video or {})['path']}"
            except (KeyError, IndexError, TypeError, AttributeError) as e:
                raise SpaceError("the Space's result had no video file", "malformed") from e
            dl = c.get(file_url)
            _http(dl, "download")
            out.write_bytes(dl.content)
    except httpx.TransportError as e:
        _record(False, f"network: {type(e).__name__}")
        raise SpaceError(f"could not reach the Space ({type(e).__name__})", "network") from e
    meta = inspect(out)
    why = valid(meta)
    if why:                                         # HTTP 200 is not enough: the file must be a playable clip
        out.unlink(missing_ok=True)
        _record(False, f"invalid video: {why}")
        raise SpaceError(f"the Space returned an unusable video: {why}", "invalid_video")
    used = time.time() - started
    _record(True, seconds=used)
    return {"file": str(out), "seconds_requested": seconds, "wall_seconds": round(used, 1), "space": space(),
            "seed": result[1] if isinstance(result, list) and len(result) > 1 else seed, "video": meta}
