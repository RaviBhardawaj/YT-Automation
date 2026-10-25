"""AI text-to-video clips with Google Gemini Omni Flash (Gemini API key; paid tier only).

Google retired Veo from the Gemini API on 22 Oct 2026; Gemini Omni Flash is its replacement there.
Off until the owner enables it. Every job shows a cost estimate first, a monthly cap is enforced before each
clip, a finished clip is never generated twice (retries reuse the file), and the cost recorded is Google's
reported token usage when present — otherwise the configured per-second estimate, labelled as such.
"""
from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .. import db, logs, settings

API = "https://generativelanguage.googleapis.com/v1beta"
PROVIDER = "gemini_video"


class VideoGenError(Exception):
    pass


def cfg() -> dict[str, Any]:
    return {"enabled": bool(settings.get("videogen.enabled")), "model": settings.get("videogen.model"),
            "usd_per_second": float(settings.get("videogen.usd_per_second")),
            "usd_per_million_tokens": float(settings.get("videogen.usd_per_million_output_tokens")),
            "seconds_per_clip": int(settings.get("videogen.seconds_per_clip")),
            "max_clips": int(settings.get("videogen.max_clips_per_video")),
            "monthly_cap_usd": float(settings.get("videogen.monthly_cap_usd")),
            "resolution": settings.get("videogen.resolution")}


def _month_start() -> str:
    now = datetime.now().astimezone()
    return db.iso(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))


def month_spent() -> float:
    row = db.q1("SELECT COALESCE(SUM(cost_usd),0) AS s FROM ai_usage WHERE provider=? AND created_at>=?",
                (PROVIDER, _month_start()))
    return round(float(row["s"]), 4)


def estimate(clips: int | None = None) -> dict[str, Any]:
    c = cfg()
    n = c["max_clips"] if clips is None else max(0, min(clips, c["max_clips"]))
    usd = round(n * c["seconds_per_clip"] * c["usd_per_second"], 2)
    spent = month_spent()
    return {"clips": n, "seconds": n * c["seconds_per_clip"], "usd": usd, "spent_this_month": spent,
            "cap": c["monthly_cap_usd"], "fits_cap": spent + usd <= c["monthly_cap_usd"], "enabled": c["enabled"],
            "basis": f"{n} clip(s) × ~{c['seconds_per_clip']} s × ${c['usd_per_second']:.2f}/s ({c['resolution']}) — estimate"}


def check_access() -> dict[str, Any]:
    """Free check: is the model visible to this key? (Google hides paid-only models on the free tier.)"""
    key = settings.secret("gemini_api_key")
    if not key:
        return {"available": False, "detail": "No Gemini API key saved (Settings › Credentials)."}
    model = cfg()["model"]
    names, token = [], None
    try:
        while True:
            params = {"pageSize": 100, **({"pageToken": token} if token else {})}
            r = httpx.get(f"{API}/models", params=params, headers={"x-goog-api-key": key}, timeout=30)
            if r.status_code != 200:
                return {"available": False, "detail": f"Google returned HTTP {r.status_code} for the model list."}
            j = r.json()
            names += [m["name"].split("/")[-1] for m in j.get("models", [])]
            token = j.get("nextPageToken")
            if not token:
                break
    except httpx.HTTPError as e:
        return {"available": False, "detail": f"Couldn't reach Google: {e}"}
    if model in names:
        return {"available": True, "detail": f"{model} is available to your Gemini key."}
    return {"available": False, "detail": f"{model} isn't available to your Gemini key yet. It's a paid-tier model: "
            f"turn on billing for this key's project in Google AI Studio (aistudio.google.com → Billing), then check again."}


def _cost_from_usage(j: dict[str, Any], seconds: float) -> tuple[float, str, int]:
    """Google-reported output tokens → cost (actual); otherwise per-second estimate."""
    c = cfg()
    tokens = 0

    def walk(o: Any) -> None:
        nonlocal tokens
        if isinstance(o, dict):
            for k, v in o.items():
                if "output_tokens" in k and isinstance(v, (int, float)):
                    tokens = max(tokens, int(v))
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(j.get("usage") or j.get("usage_metadata") or {})
    if tokens:
        return round(tokens / 1e6 * c["usd_per_million_tokens"], 4), "actual", tokens
    return round(seconds * c["usd_per_second"], 4), "estimate", 0


def generate(prompt: str, aspect: str, out: Path, *, label: str = "") -> dict[str, Any]:
    """One clip → out (mp4). Re-uses an existing file (no second charge). Raises VideoGenError."""
    if out.exists() and out.stat().st_size > 10_000:
        return {"path": out, "reused": True, "cost_usd": 0.0}
    c = cfg()
    if not c["enabled"]:
        raise VideoGenError("AI video generation is switched off (Settings › AI video)")
    key = settings.secret("gemini_api_key")
    if not key:
        raise VideoGenError("no Gemini API key")
    one = c["seconds_per_clip"] * c["usd_per_second"]
    if month_spent() + one > c["monthly_cap_usd"]:
        raise VideoGenError(f"monthly AI-video cap reached (${month_spent():.2f} of ${c['monthly_cap_usd']:.2f})")
    body = {"model": c["model"], "input": prompt[:1800],
            "response_format": {"type": "video", "aspect_ratio": "9:16" if aspect == "9:16" else "16:9",
                                "resolution": c["resolution"], "delivery": "inline"},
            "generation_config": {"video_config": {"task": "text_to_video"}},
            "background": False, "store": False, "stream": False}
    try:
        r = httpx.post(f"{API}/interactions", json=body, headers={"x-goog-api-key": key},
                       timeout=httpx.Timeout(900, connect=20))
    except httpx.HTTPError as e:
        raise VideoGenError(f"couldn't reach Google: {e}") from e
    if r.status_code != 200:
        db.insert("ai_usage", {"provider": PROVIDER, "model": c["model"], "task": "video", "input_tokens": 0,
                               "output_tokens": 0, "cost_usd": 0.0, "ok": 0, "error": r.text[:300],
                               "created_at": db.iso()})
        raise VideoGenError(f"Google returned HTTP {r.status_code}: {r.text[:200]}")
    j = r.json()
    data = None
    for step in j.get("steps", []):
        for part in step.get("content", []) if step.get("type") == "model_output" else []:
            if part.get("type") == "video" and part.get("data"):
                data = part["data"]
    if not data:
        raise VideoGenError("Google returned no video (the prompt may have been blocked by safety filters)")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(base64.b64decode(data))
    from ..media.visuals import probe_video
    info = probe_video(out)
    if not info["width"] or info["duration"] < 1:
        out.unlink(missing_ok=True)
        raise VideoGenError("the generated file isn't a playable video")
    cost, basis, tokens = _cost_from_usage(j, info["duration"])
    db.insert("ai_usage", {"provider": PROVIDER, "model": c["model"], "task": f"video ({basis})", "input_tokens": 0,
                           "output_tokens": tokens, "cost_usd": cost, "ok": 1, "error": "", "created_at": db.iso()})
    logs.event("info", "videogen", f"AI clip {label}: {info['duration']:.1f}s {info['width']}x{info['height']}, "
               f"${cost:.2f} ({basis})")
    return {"path": out, "reused": False, "cost_usd": cost, "basis": basis, **info}
