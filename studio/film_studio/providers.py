"""Film Studio provider manager: what each provider can do, whether it is really free, and free-first routing.

Rules (budget ₹0 by default):
  * free local tools first, then free allowances that still have capacity;
  * paid providers are never used at ₹0 — not as a fallback either; above ₹0 only those the owner listed;
  * when no free provider can make real video, the shot becomes a labelled "still image with camera move"
    (motion type camera_move) — never reported as video;
  * free-tier facts carry the date they were checked and where; nothing is called "permanently free".
Health checks are local and cheap: no paid call is ever made to test a provider.
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .. import config, db, settings

CAPS = ("image", "image_ref", "video_t2v", "video_i2v", "dialogue_video", "camera_move", "tts", "voice_clone", "stt",
        "vision_check")
VIDEO_CAPS = ("video_t2v", "video_i2v", "dialogue_video")
FREE = ("free_local", "free_allowance")


def _cf_health() -> tuple[bool, str]:
    from ..media import visuals
    if not visuals.cloudflare_ready():
        return False, "add the Cloudflare token (Credentials) and account ID (Visuals)"
    used = visuals.cloudflare_used_today()
    left = visuals.CF_FREE_NEURONS - used
    return left > 300, f"{max(0, left):,} of {visuals.CF_FREE_NEURONS:,} free neurons left today"


def _local_sd_health() -> tuple[bool, str]:
    import httpx
    url = (settings.get("visuals.local_sd_url") or "").rstrip("/")
    try:
        httpx.get(url + "/sdapi/v1/options", timeout=2)
        return True, f"running at {url}"
    except Exception:
        return False, "no local Stable Diffusion server running"


def _kokoro_health() -> tuple[bool, str]:
    from ..media import tts
    return (True, "installed") if tts.kokoro_installed() else (False, "not installed — Computer & setup")


def _whisper_health() -> tuple[bool, str]:
    d = config.DATA_DIR / "models" / "whisper"
    ok = d.exists() and any(d.glob("models--*/snapshots/*/model.bin"))
    return (ok, "installed") if ok else (False, "not installed — Computer & setup")


def _clone_health() -> tuple[bool, str]:
    from ..media import voiceclone
    s = voiceclone.status()
    return (s["installed"] and s["model"], "installed" if s["installed"] and s["model"] else "not installed")


def _ffmpeg_health() -> tuple[bool, str]:
    ok = bool(shutil.which(config.FFMPEG) or Path(config.FFMPEG).exists())
    return ok, "installed" if ok else "FFmpeg missing"


def _secret(name: str, what: str) -> Callable[[], tuple[bool, str]]:
    return lambda: (settings.has_secret(name), "key saved" if settings.has_secret(name) else f"no {what} key")


def _wan_health() -> tuple[bool, str]:
    marker = config.DATA_DIR / "models" / "wan2.2-ti2v-5b"
    return (marker.exists(), "installed" if marker.exists() else
            "not installed — about 17–21 GB download; needs your approval and free disk")


def _zerogpu_health() -> tuple[bool, str]:
    if not settings.has_secret("hf_token"):
        return False, "add a free Hugging Face token (Credentials) — about 5 GPU-minutes a day"
    return True, f"token saved · Space {settings.get('film_studio.zerogpu_space')}"


CHECKED = "2026-10-29"
REGISTRY: list[dict[str, Any]] = [
    {"id": "cloudflare_flux", "name": "Cloudflare Workers AI — FLUX.2 klein 4B", "caps": ["image", "image_ref"],
     "where": "remote", "model": "@cf/black-forest-labs/flux-2-klein-4b", "cost": "free_allowance",
     "allowance": "about 10,000 neurons a day (~60–90 images), resets 00:00 UTC", "checked": CHECKED,
     "source": "Cloudflare Workers AI free plan", "licence": "Apache-2.0", "commercial": True,
     "hardware": "none (cloud)", "limits": "about 1 minute per image; 4 reference pictures max",
     "est_cost_usd": 0.0, "fallback": ["local_sd"], "health": _cf_health},
    {"id": "local_sd", "name": "Local Stable Diffusion (A1111/Forge/Draw Things API)", "caps": ["image"],
     "where": "local", "model": "your local checkpoint", "cost": "free_local", "checked": CHECKED,
     "licence": "depends on the checkpoint you load", "commercial": None, "hardware": "Apple Silicon / GPU",
     "limits": "speed depends on the Mac", "est_cost_usd": 0.0, "fallback": [], "health": _local_sd_health},
    {"id": "gemini_image", "name": "Gemini image generation", "caps": ["image", "image_ref"], "where": "remote",
     "model": "gemini image models", "cost": "paid", "checked": CHECKED, "licence": "Google terms",
     "commercial": True, "hardware": "none (cloud)", "limits": "billed per image",
     "est_cost_usd": 0.039, "fallback": [], "health": _secret("gemini_api_key", "Gemini")},
    {"id": "wan22_local", "name": "Wan 2.2 TI2V-5B (local, open weights)", "caps": ["video_t2v", "video_i2v"],
     "where": "local", "model": "Wan2.2-TI2V-5B", "cost": "free_local", "checked": CHECKED,
     "licence": "Apache-2.0 (recheck before install)", "commercial": None,
     "hardware": "17–21 GB download; 16 GB Mac unconfirmed; minutes per few-second clip",
     "limits": "a few seconds per clip", "est_cost_usd": 0.0, "fallback": ["hf_zerogpu"], "health": _wan_health},
    {"id": "hf_zerogpu", "name": "Hugging Face ZeroGPU Space (video)", "caps": ["video_t2v", "video_i2v"],
     "where": "remote", "model": "Wan 2.2 14B fast (zerogpu-aoti Space)", "cost": "free_allowance",
     "allowance": "5 GPU-minutes a day on a free account (1–3 short clips)", "checked": CHECKED,
     "source": "huggingface.co/docs/hub/spaces-zerogpu", "licence": "Wan 2.2: Apache-2.0 (recheck)",
     "commercial": None, "hardware": "none (cloud)", "limits": "queues; daily quota",
     "est_cost_usd": 0.0, "fallback": [], "health": _zerogpu_health},
    {"id": "gemini_omni", "name": "Gemini Omni Flash (AI video with sound)",
     "caps": ["video_t2v", "video_i2v", "dialogue_video"], "where": "remote", "model": "gemini-omni-1.1-flash",
     "cost": "paid", "checked": CHECKED, "licence": "Google terms", "commercial": True, "hardware": "none (cloud)",
     "limits": "about $0.10/second at 720p", "est_cost_usd": 0.8, "fallback": [],
     "health": _secret("gemini_api_key", "Gemini")},
    {"id": "ffmpeg_camera", "name": "Camera move on a still (FFmpeg)", "caps": ["camera_move"], "where": "local",
     "model": "zoompan", "cost": "free_local", "checked": CHECKED, "licence": "LGPL/GPL (FFmpeg)",
     "commercial": True, "hardware": "any", "limits": "no real motion — labelled as a still with camera move",
     "est_cost_usd": 0.0, "fallback": [], "health": _ffmpeg_health},
    {"id": "kokoro", "name": "Kokoro voices (Hindi + English)", "caps": ["tts"], "where": "local",
     "model": "Kokoro-82M", "cost": "free_local", "checked": CHECKED, "licence": "Apache-2.0", "commercial": True,
     "hardware": "any", "limits": "2 male + 2 female Hindi voices", "est_cost_usd": 0.0, "fallback": [],
     "health": _kokoro_health},
    {"id": "sarvam", "name": "Sarvam Bulbul voices", "caps": ["tts"], "where": "remote", "model": "bulbul",
     "cost": "paid", "checked": CHECKED, "licence": "Sarvam terms", "commercial": True, "hardware": "none (cloud)",
     "limits": "billed per character", "est_cost_usd": 0.36, "fallback": ["kokoro"],
     "health": _secret("sarvam_api_key", "Sarvam")},
    {"id": "chatterbox", "name": "Chatterbox voice cloning (with consent)", "caps": ["voice_clone", "tts"],
     "where": "local", "model": "Chatterbox Multilingual", "cost": "free_local", "checked": CHECKED,
     "licence": "MIT", "commercial": True, "hardware": "Apple Silicon; ~8–10× slower than real time on M2",
     "limits": "needs a consented voice sample", "est_cost_usd": 0.0, "fallback": ["kokoro"],
     "health": _clone_health},
    {"id": "whisper", "name": "Whisper small (speech-to-text, offline)", "caps": ["stt"], "where": "local",
     "model": "faster-whisper small", "cost": "free_local", "checked": CHECKED,
     "licence": "MIT (recheck)", "commercial": None, "hardware": "any", "limits": "",
     "est_cost_usd": 0.0, "fallback": [], "health": _whisper_health},
    {"id": "gemini_vision", "name": "Gemini Flash-Lite picture checks", "caps": ["vision_check"], "where": "remote",
     "model": "gemini-flash-lite-latest", "cost": "free_allowance", "allowance": "free tier, paced to ~14/min",
     "checked": CHECKED, "licence": "Google terms", "commercial": True, "hardware": "none (cloud)",
     "limits": "rate limited", "est_cost_usd": 0.0, "fallback": ["ollama_vision"],
     "health": _secret("gemini_api_key", "Gemini")},
    {"id": "ollama_vision", "name": "Ollama Cloud vision checks", "caps": ["vision_check"], "where": "remote",
     "model": "gemma4:31b", "cost": "free_allowance", "allowance": "plan's included usage", "checked": CHECKED,
     "licence": "Gemma terms", "commercial": True, "hardware": "none (cloud)", "limits": "",
     "est_cost_usd": 0.0, "fallback": [], "health": _secret("ollama_api_key", "Ollama")},
]
BY_ID = {p["id"]: p for p in REGISTRY}


class NoFreeProvider(RuntimeError):
    """Nothing free can do this right now (or at all); the message names the options."""


class PaidBlocked(PermissionError):
    pass


def budget_inr() -> float:
    return float(settings.get("film_studio.budget_inr") or 0)


def paid_allowed(provider_id: str) -> bool:
    """Paid only when the budget is above ₹0 AND the owner listed this provider by name."""
    return budget_inr() > 0 and provider_id in (settings.get("film_studio.paid_providers") or [])


def guard(provider_id: str) -> None:
    """Call before using a provider: raises PaidBlocked for any paid provider the budget doesn't allow."""
    p = BY_ID.get(provider_id)
    if p is None:
        raise PaidBlocked(f"unknown provider {provider_id!r}")
    if p["cost"] == "paid" and not paid_allowed(provider_id):
        raise PaidBlocked(f"{p['name']} is paid and the Film Studio budget is ₹{budget_inr():.0f} — not used.")


def health(provider_id: str) -> dict[str, Any]:
    p = BY_ID[provider_id]
    try:
        ok, detail = p["health"]()
    except Exception as e:  # a health check must never break a page
        ok, detail = False, f"check failed: {type(e).__name__}"
    return {"available": bool(ok), "detail": detail}


def status_all() -> list[dict[str, Any]]:
    out = []
    for p in REGISTRY:
        h = health(p["id"])
        out.append({**{k: v for k, v in p.items() if k != "health"}, **h,
                    "usable_now": h["available"] and (p["cost"] in FREE or paid_allowed(p["id"]))})
    return out


def choose(cap: str) -> dict[str, Any]:
    """The provider to use for a capability, free-first. Real video falls back to a *labelled* camera move."""
    if cap not in CAPS:
        raise ValueError(f"unknown capability {cap!r}")
    rank = {"free_local": 0, "free_allowance": 1, "paid": 2}
    tried = []
    for p in sorted((p for p in REGISTRY if cap in p["caps"]), key=lambda p: rank[p["cost"]]):
        if p["cost"] == "paid" and not paid_allowed(p["id"]):
            tried.append(f"{p['name']}: paid — off at ₹{budget_inr():.0f}")
            continue
        h = health(p["id"])
        if h["available"]:
            return {"provider": p["id"], "name": p["name"], "cost": p["cost"],
                    "motion_type": _motion(cap), "tried": tried}
        tried.append(f"{p['name']}: {h['detail']}")
    if cap in VIDEO_CAPS:
        cam = health("ffmpeg_camera")
        if cam["available"]:
            return {"provider": "ffmpeg_camera", "name": BY_ID["ffmpeg_camera"]["name"], "cost": "free_local",
                    "motion_type": "camera_move", "tried": tried,
                    "limitation": "No free video provider is available, so this shot will be a still image with a "
                                  "camera move — labelled as such, not as video. Options: set up a free Hugging Face "
                                  "token (ZeroGPU), install Wan 2.2 locally, or allow a paid provider."}
    raise NoFreeProvider(f"No free provider can do '{cap}' right now. Tried: " + "; ".join(tried))


def _motion(cap: str) -> str:
    return {"video_t2v": "video", "video_i2v": "video", "dialogue_video": "dialogue_video",
            "camera_move": "camera_move"}.get(cap, "static")


def usage(days: int = 30) -> list[dict[str, Any]]:
    """Recorded calls and costs per provider (from the app's usage log)."""
    since = datetime.now(timezone.utc).timestamp() - days * 86400
    since_iso = db.iso(datetime.fromtimestamp(since, timezone.utc))
    return db.q("SELECT provider, task, COUNT(*) AS calls, SUM(ok) AS ok, ROUND(SUM(cost_usd),4) AS cost_usd, "
                "SUM(output_tokens) AS units FROM ai_usage WHERE created_at>=? GROUP BY provider, task "
                "ORDER BY provider, task", (since_iso,))
