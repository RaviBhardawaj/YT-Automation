"""Does this picture actually show what the scene is about? A vision model looks at each stock candidate
(a small preview, before the full file is downloaded) and scores it 0-10 against the scene.

Providers, in order: Gemini Flash-Lite (free tier, paced to stay under its per-minute
limit), then Ollama Cloud (the configured cloud model, if it accepts images). When neither can look, the caller
falls back to text matching on the media's own tags/description.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import threading
import time
from typing import Any

import httpx
from PIL import Image

from .. import db, settings

log = logging.getLogger(__name__)

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
OLLAMA_CLOUD = "https://ollama.com"
PASS = 6                                     # default score needed to use a picture (visuals.min_relevance)

_lock = threading.Lock()
_calls: list[float] = []                     # Gemini call times (free-tier pacing)
_down: dict[str, float] = {}                 # provider -> time it stopped answering (skipped for 10 minutes)
_cache: dict[str, dict[str, Any]] = {}

PROMPT = ("You check stock pictures for a YouTube video. Video topic: {topic}\n"
          "This scene says: {narration}\nWanted on screen: {want}\n\n"
          "Look at the picture. Reply with JSON only: {{\"shows\": \"<what the picture really shows, max 12 words>\", "
          "\"score\": <0-10>}}.\nScore: 9-10 = shows exactly what is wanted; 7-8 = shows the wanted main subject or "
          "action even if details differ (wanted 'balance scale and books', picture shows a balance scale = 7); "
          "5-6 = clearly about this scene's topic but not the wanted subject; 3-4 = only loosely related or generic; "
          "0-2 = unrelated or misleading. Also give 0-2 for a visible watermark, large overlaid text, or a blurry / "
          "low-quality picture. A viewer must immediately see why this picture is on screen.")


def available() -> bool:
    return settings.get("visuals.vision_check", True) and (
        settings.has_secret("gemini_api_key") or settings.has_secret("ollama_api_key"))


def _small_jpeg(raw: bytes, side: int = 640) -> bytes:
    with Image.open(io.BytesIO(raw)) as im:
        im = im.convert("RGB")
        im.thumbnail((side, side))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=85)
        return out.getvalue()


def _parse(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < a:
        return None
    try:
        j = json.loads(text[a:b + 1])
        out = {"score": max(0, min(10, int(round(float(j.get("score", 0)))))), "shows": str(j.get("shows", ""))[:120]}
        if "people" in j:                                # background-art checks also say what to fix
            out["people"] = j["people"] in (True, "true", "yes", 1)
            out["fix"] = str(j.get("fix") or "")[:200]
        return out
    except (ValueError, TypeError):
        return None


MANY = ("You check stock pictures for a YouTube video. Video topic: {topic}\n"
        "This scene says: {narration}\nWanted on screen: {want}\n\n"
        "You get {n} pictures, numbered 1 to {n} in the order given. For EACH reply with what it really shows and a "
        "score. JSON only: {{\"pictures\": [{{\"n\": 1, \"shows\": \"<max 12 words>\", \"score\": <0-10>}}, ...]}}.\n"
        "Score: 9-10 = shows exactly what is wanted; 7-8 = shows the wanted main subject or action even if details "
        "differ; 5-6 = clearly about this scene's topic but not the wanted subject; 3-4 = loosely related or generic; "
        "0-2 = unrelated or misleading. Also 0-2 for a visible watermark, large overlaid text, or a blurry / "
        "low-quality picture. Judge every picture on its own.")


def _parse_many(text: str, n: int) -> list[dict[str, Any] | None] | None:
    text = (text or "").strip()
    a, b = text.find("{"), text.rfind("}")
    try:
        j = json.loads(text[a:b + 1]) if a >= 0 and b > a else None
    except ValueError:
        j = None
    items = (j or {}).get("pictures") if isinstance(j, dict) else None
    if items is None:
        try:
            items = json.loads(text[text.find("["):text.rfind("]") + 1])
        except ValueError:
            return None
    out: list[dict[str, Any] | None] = [None] * n
    for k, it in enumerate(items if isinstance(items, list) else []):
        try:
            idx = int(it.get("n", k + 1)) - 1
            if 0 <= idx < n:
                out[idx] = {"score": max(0, min(10, int(round(float(it.get("score", 0)))))),
                            "shows": str(it.get("shows", ""))[:120]}
        except (TypeError, ValueError, AttributeError):
            continue
    return out if any(out) else None


def judge_many(raws: list[bytes], *, topic: str, narration: str, want: str) -> list[dict[str, Any] | None] | None:
    """Score several candidate pictures for one scene in a single request (None = nobody could look)."""
    jpegs = []
    for raw in raws:
        try:
            jpegs.append(_small_jpeg(raw, 512))
        except Exception:
            jpegs.append(None)
    good = [j for j in jpegs if j]
    if not good:
        return [{"score": 0, "shows": "unreadable image", "by": "local"}] * len(raws)
    prompt = MANY.format(topic=topic[:200], narration=narration[:400], want=want[:200], n=len(good))
    for name, fn, has in (("gemini", _gemini, settings.has_secret("gemini_api_key")),
                          ("ollama", _ollama, settings.has_secret("ollama_api_key"))):
        if not has or time.time() - _down.get(name, 0) < 600:
            continue
        try:
            res = fn(good, prompt)
        except (httpx.HTTPError, ValueError) as e:
            log.info("vision check via %s failed: %s", name, e)
            _down[name] = time.time()
            continue
        if res:
            it = iter(res)
            out = []
            for j in jpegs:
                v = next(it, None) if j else {"score": 0, "shows": "unreadable image"}
                out.append({**v, "by": name} if v else None)
            return out
    return None


def _usage(provider: str, model: str, ok: bool, err: str = "", tin: int = 0, tout: int = 0) -> None:
    try:
        db.insert("ai_usage", {"provider": provider, "model": model, "task": "vision_check", "input_tokens": tin,
                               "output_tokens": tout, "cost_usd": 0.0, "ok": int(ok), "error": err[:300],
                               "created_at": db.iso()})
    except Exception:  # usage logging must never stop a video
        pass


def _gemini(jpeg: bytes | list[bytes], prompt: str) -> Any:
    model = settings.get("visuals.vision_gemini_model") or "gemini-flash-lite-latest"   # fast; free tier
    per_min = int(settings.get("visuals.vision_per_minute") or 14)
    with _lock:                                          # stay under the free tier's requests-per-minute
        now = time.time()
        _calls[:] = [t for t in _calls if now - t < 60]
        if len(_calls) >= per_min:
            time.sleep(max(0.0, 60 - (now - _calls[0])) + 0.5)
        _calls.append(time.time())
    imgs = jpeg if isinstance(jpeg, list) else [jpeg]
    body = {"contents": [{"parts": [{"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(j).decode()}}
                                    for j in imgs] + [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 300}}
    for attempt in range(2):
        r = httpx.post(f"{GEMINI_BASE}/models/{model}:generateContent", json=body,
                       headers={"x-goog-api-key": settings.secret("gemini_api_key") or ""}, timeout=60)
        if r.status_code in (500, 503) and attempt == 0:      # "high demand": usually gone in seconds
            time.sleep(4)
            continue
        break
    if r.status_code >= 400:
        _usage("gemini", model, False, f"HTTP {r.status_code} {r.text[:200]}")
        if r.status_code in (401, 403, 429):
            _down["gemini"] = time.time()
        elif r.status_code >= 500:
            _down["gemini"] = time.time() - 540               # busy: try again in a minute
        return None
    data = r.json()
    u = data.get("usageMetadata", {})
    parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts)
    res = _parse_many(text, len(imgs)) if isinstance(jpeg, list) else _parse(text)
    _usage("gemini", model, res is not None, "" if res else "unreadable reply",
           int(u.get("promptTokenCount", 0)), int(u.get("candidatesTokenCount", 0)))
    return res


def _ollama(jpeg: bytes | list[bytes], prompt: str) -> Any:
    model = (settings.get("ai.models") or {}).get("ollama_cloud") or "gemma4:31b"
    r = httpx.post(f"{OLLAMA_CLOUD}/api/chat", headers={"Authorization": f"Bearer {settings.secret('ollama_api_key')}"},
                   json={"model": model, "stream": False, "format": "json",
                         "messages": [{"role": "user", "content": prompt,
                                       "images": [base64.b64encode(j).decode() for j in
                                                  (jpeg if isinstance(jpeg, list) else [jpeg])]}]}, timeout=120)
    if r.status_code >= 400:
        _usage("ollama_cloud", model, False, f"HTTP {r.status_code} {r.text[:200]}")
        _down["ollama"] = time.time()
        return None
    data = r.json()
    text = (data.get("message") or {}).get("content", "")
    res = _parse_many(text, len(jpeg)) if isinstance(jpeg, list) else _parse(text)
    _usage("ollama_cloud", model, res is not None, "" if res else "unreadable reply",
           int(data.get("prompt_eval_count") or 0), int(data.get("eval_count") or 0))
    return res


ART = ("You check a background picture for a children's cartoon episode. Wanted: {want}\n\n"
       "Reply with JSON only: {{\"shows\": \"<what the picture really shows, max 15 words>\", \"score\": <0-10>, "
       "\"people\": <true|false>, \"fix\": \"<what to change in the prompt, max 25 words, or empty>\"}}.\n"
       "score = how clearly it shows the wanted place, props and effects as clean, bright 2D cartoon art a child "
       "understands at a glance (0-2 for photo-realism, text, watermark, clutter or a broken picture; at most 5 when "
       "big furniture or objects fill the left or right floor area where characters will stand). people = true "
       "if ANY person, character, face, hand or animal is visible. fix: when score < 7 or people is true, say exactly "
       "what to add, remove or change.")


def check_art(raw: bytes, *, want: str) -> dict[str, Any] | None:
    """{"score", "shows", "people", "fix", "by"} for a cartoon background, or None when no vision model could look."""
    try:
        jpeg = _small_jpeg(raw)
    except Exception:
        return {"score": 0, "shows": "unreadable image", "people": False, "fix": "", "by": "local"}
    prompt = ART.format(want=want[:500])
    for name, fn, has in (("gemini", _gemini, settings.has_secret("gemini_api_key")),
                          ("ollama", _ollama, settings.has_secret("ollama_api_key"))):
        if not has or time.time() - _down.get(name, 0) < 600:
            continue
        try:
            res = fn(jpeg, prompt)
        except (httpx.HTTPError, ValueError) as e:
            log.info("art check via %s failed: %s", name, e)
            _down[name] = time.time()
            continue
        if res:
            return {"people": False, "fix": "", **res, "by": name}
    return None


def judge(raw: bytes, *, topic: str, narration: str, want: str, key: str = "") -> dict[str, Any] | None:
    """{"score": 0-10, "shows": "...", "by": provider} for one picture, or None when no vision model could look."""
    if key and key in _cache:
        return _cache[key]
    try:
        jpeg = _small_jpeg(raw)
    except Exception:
        return {"score": 0, "shows": "unreadable image", "by": "local"}
    prompt = PROMPT.format(topic=topic[:200], narration=narration[:400], want=want[:200])
    for name, fn, has in (("gemini", _gemini, settings.has_secret("gemini_api_key")),
                          ("ollama", _ollama, settings.has_secret("ollama_api_key"))):
        if not has or time.time() - _down.get(name, 0) < 600:
            continue
        try:
            res = fn(jpeg, prompt)
        except (httpx.HTTPError, ValueError) as e:
            log.info("vision check via %s failed: %s", name, e)
            _down[name] = time.time()
            continue
        if res:
            res["by"] = name
            if key:
                _cache[key] = res
            return res
    return None
