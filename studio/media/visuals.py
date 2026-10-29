"""Scene visuals: licensed stock, optional AI image generation, and original procedural art.

Every asset is stored with provider, source page, author, licence and an attribution line.
Only licences that allow commercial use *and* modification are accepted.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from PIL import Image

from .. import config, db, net, settings
from . import graphics, vision

log = logging.getLogger(__name__)

ALLOWED_COMMONS = re.compile(r"^(cc0|public domain|pd[- ]|cc[- ]by[- ](sa[- ])?\d(\.\d)?)", re.I)


@dataclass
class Candidate:
    provider: str
    provider_id: str
    kind: str                      # image | video
    url: str
    page_url: str
    width: int
    height: int
    license: str
    license_url: str
    author: str
    duration: float = 0.0
    attribution: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _orientation(fmt: str) -> str:
    return "portrait" if fmt == "short" else "landscape"


_GENERIC = {"space", "animation", "illustration", "cinematic", "video", "technology", "digital", "abstract",
            "background", "concept", "view", "scene", "closeup", "close", "aerial", "slow", "motion", "the", "and",
            "of", "in", "with", "a", "an"}


def relevant(title: str, query: str) -> bool:
    """Archive search (Commons/Openverse) ranks loosely; require a meaningful query word in the
    file title so a 'satellite sensors' scene doesn't get a river-delta photo."""
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in _GENERIC and len(w) > 2]
    t = title.lower()
    return not words or any(w in t or (w.endswith("s") and w[:-1] in t) for w in words)


# ------------------------------------------------------------------ providers

def pexels(query: str, kind: str, fmt: str) -> list[Candidate]:
    key = settings.secret("pexels_api_key")
    if not key:
        return []
    hdr = {"Authorization": key}
    if kind == "video":
        data = net.get_json("https://api.pexels.com/videos/search",
                            {"query": query, "orientation": _orientation(fmt), "per_page": 12}, hdr)
        out = []
        for v in data.get("videos", []):
            files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4" and (f.get("width") or 0) >= 720]
            if not files:
                continue
            target = 1080
            f = min(files, key=lambda f: abs(min(f["width"], f["height"]) - target))
            user = v.get("user", {}).get("name", "Pexels contributor")
            slug = (v.get("url") or "").rstrip("/").rsplit("/", 1)[-1]          # /video/<words>-<id>/
            out.append(Candidate("pexels", f"v{v['id']}", "video", f["link"], v.get("url", ""), f["width"], f["height"],
                                 "Pexels License", "https://www.pexels.com/license/", user, float(v.get("duration") or 0),
                                 f"Video by {user} on Pexels",
                                 extra={"text": slug.replace("-", " "), "thumb": v.get("image") or ""}))
        return out
    data = net.get_json("https://api.pexels.com/v1/search",
                        {"query": query, "orientation": _orientation(fmt), "per_page": 15}, hdr)
    return [Candidate("pexels", f"p{p['id']}", "image", p["src"].get("large2x") or p["src"]["original"], p.get("url", ""),
                      p["width"], p["height"], "Pexels License", "https://www.pexels.com/license/",
                      p.get("photographer", "Pexels contributor"),
                      attribution=f"Photo by {p.get('photographer', 'unknown')} on Pexels",
                      extra={"text": p.get("alt") or "", "thumb": p["src"].get("medium") or ""})
            for p in data.get("photos", [])]


def pixabay(query: str, kind: str, fmt: str, look: str = "real") -> list[Candidate]:
    key = settings.secret("pixabay_api_key")
    if not key:
        return []
    lic, lic_url = "Pixabay Content License", "https://pixabay.com/service/license-summary/"
    if kind == "video":
        data = net.get_json("https://pixabay.com/api/videos/", {"key": key, "q": query[:100], "per_page": 12, "safesearch": "true",
                                                                "video_type": "animation" if look == "cartoon" else "film"})
        out = []
        for h in data.get("hits", []):
            v = h.get("videos", {}).get("large") or h.get("videos", {}).get("medium") or {}
            if not v.get("url") or (v.get("width") or 0) < 720:
                continue
            thumb = v.get("thumbnail") or (h.get("videos", {}).get("medium") or {}).get("thumbnail") or ""
            out.append(Candidate("pixabay", f"v{h['id']}", "video", v["url"], h.get("pageURL", ""), v["width"], v["height"],
                                 lic, lic_url, h.get("user", "Pixabay contributor"), float(h.get("duration") or 0),
                                 f"Video by {h.get('user', 'unknown')} on Pixabay",
                                 extra={"text": h.get("tags") or "", "thumb": thumb}))
        return out
    data = net.get_json("https://pixabay.com/api/", {"key": key, "q": query[:100], "per_page": 20,
                                                    "image_type": "illustration" if look == "cartoon" else "photo",
                                                    "safesearch": "true",
                                                    "orientation": "vertical" if fmt == "short" else "horizontal"})
    return [Candidate("pixabay", f"p{h['id']}", "image", h["largeImageURL"], h.get("pageURL", ""), h["imageWidth"],
                      h["imageHeight"], lic, lic_url, h.get("user", "Pixabay contributor"),
                      attribution=f"Image by {h.get('user', 'unknown')} on Pixabay",
                      extra={"text": h.get("tags") or "", "thumb": h.get("webformatURL") or ""})
            for h in data.get("hits", [])]


def openverse(query: str, kind: str, fmt: str) -> list[Candidate]:
    if kind != "image":
        return []
    data = net.get_json("https://api.openverse.org/v1/images/",
                        {"q": query, "license_type": "commercial,modification", "page_size": 20, "mature": "false",
                         "aspect_ratio": "tall" if fmt == "short" else "wide"}, timeout=12)
    out = []
    for r in data.get("results", []):
        lic = (r.get("license") or "").lower()
        if lic not in {"cc0", "pdm", "by", "by-sa"} or not relevant(r.get("title") or "", query):
            continue
        label = {"cc0": "CC0", "pdm": "Public Domain Mark"}.get(lic, f"CC {lic.upper()} {r.get('license_version', '')}".strip())
        out.append(Candidate("openverse", r["id"], "image", r["url"], r.get("foreign_landing_url") or "",
                             int(r.get("width") or 0), int(r.get("height") or 0), label, r.get("license_url") or "",
                             r.get("creator") or "unknown", attribution=r.get("attribution") or
                             f"\"{r.get('title', 'Image')}\" by {r.get('creator', 'unknown')}, {label}",
                             extra={"text": " ".join([r.get("title") or ""] + [t.get("name", "") for t in r.get("tags") or []]),
                                    "thumb": r.get("thumbnail") or ""}))
    return out


def wikimedia_video(query: str, fmt: str) -> list[Candidate]:
    """Short, freely licensed clips from Wikimedia Commons (no key). Long recordings (talks, lectures) are skipped."""
    data = net.get_json("https://commons.wikimedia.org/w/api.php", {
        "action": "query", "generator": "search", "gsrsearch": f"filetype:video {query}", "gsrnamespace": 6,
        "gsrlimit": 15, "prop": "imageinfo", "iiprop": "url|extmetadata|size|mime|metadata", "iiurlwidth": 640,
        "format": "json"})
    out = []
    for page in (data.get("query", {}).get("pages") or {}).values():
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        lic = (meta.get("LicenseShortName") or {}).get("value", "")
        md = {m.get("name"): m.get("value") for m in info.get("metadata") or [] if isinstance(m, dict)}
        try:
            secs = float(md.get("length") or md.get("playtime_seconds") or 0)
        except (TypeError, ValueError):
            secs = 0.0
        if not ALLOWED_COMMONS.match(lic) or info.get("mime") not in ("video/webm", "application/ogg", "video/ogg") \
                or not 3 <= secs <= 120 or int(info.get("size") or 0) > 80_000_000 \
                or not relevant(page.get("title", ""), query):
            continue
        artist = re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value", "unknown")).strip()[:120] or "unknown"
        title = page.get("title", "").removeprefix("File:")
        out.append(Candidate("wikimedia", f"v{page.get('pageid')}", "video", info["url"], info.get("descriptionurl", ""),
                             int(info.get("width") or 0), int(info.get("height") or 0), lic,
                             (meta.get("LicenseUrl") or {}).get("value", ""), artist, secs,
                             f"\"{title}\" by {artist[:80]}, {lic}, via Wikimedia Commons",
                             extra={"text": title, "thumb": info.get("thumburl") or ""}))
    return out


def wikimedia(query: str, kind: str, fmt: str) -> list[Candidate]:
    if kind == "video":
        return wikimedia_video(query, fmt)
    if kind != "image":
        return []
    data = net.get_json("https://commons.wikimedia.org/w/api.php", {
        "action": "query", "generator": "search", "gsrsearch": f"filetype:bitmap {query}", "gsrnamespace": 6,
        "gsrlimit": 15, "prop": "imageinfo", "iiprop": "url|extmetadata|size|mime", "iiurlwidth": 2000, "format": "json"})
    out = []
    for page in (data.get("query", {}).get("pages") or {}).values():
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        lic = (meta.get("LicenseShortName") or {}).get("value", "")
        if not ALLOWED_COMMONS.match(lic) or info.get("mime") not in ("image/jpeg", "image/png", "image/webp"):
            continue
        if not relevant(page.get("title", ""), query):
            continue
        artist = net.html_to_text((meta.get("Artist") or {}).get("value", "unknown"), 200)[1] or \
            re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value", "unknown"))
        title = page.get("title", "").removeprefix("File:")
        out.append(Candidate("wikimedia", str(page.get("pageid")), "image", info.get("thumburl") or info["url"],
                             info.get("descriptionurl", ""), int(info.get("thumbwidth") or info.get("width") or 0),
                             int(info.get("thumbheight") or info.get("height") or 0), lic,
                             (meta.get("LicenseUrl") or {}).get("value", ""), artist.strip()[:120] or "unknown",
                             attribution=f"\"{title}\" by {artist.strip()[:80] or 'unknown'}, {lic}, via Wikimedia Commons",
                             extra={"text": title, "thumb": info.get("thumburl") or ""}))
    return out


SEARCHERS = {"pexels": pexels, "pixabay": pixabay, "openverse": openverse, "wikimedia": wikimedia}


# ------------------------------------------------------------------ generators

def generate_local_sd(prompt: str, w: int, h: int, out: Path) -> Candidate:
    """Automatic1111/Forge-compatible API (also offered by Draw Things on macOS)."""
    url = settings.get("visuals.local_sd_url").rstrip("/") + "/sdapi/v1/txt2img"
    gw, gh = (576, 1024) if h > w else (1024, 576)
    r = httpx.post(url, json={"prompt": prompt + ", high detail, cinematic lighting, no text, no watermark",
                              "negative_prompt": "text, letters, watermark, logo, signature, deformed, blurry",
                              "width": gw, "height": gh, "steps": 25}, timeout=600)
    r.raise_for_status()
    out.write_bytes(base64.b64decode(r.json()["images"][0]))
    return Candidate("local_sd", out.stem, "image", "", "", gw, gh, "Generated locally (original)", "", "local model",
                     attribution="")


CF_MODEL = "@cf/black-forest-labs/flux-2-klein-4b"
CF_FREE_NEURONS = 10_000                      # Workers AI free allowance per day (resets 00:00 UTC)
LOOK_PROMPT = {
    "real": ("Photorealistic cinematic film still, natural lighting, realistic textures, shot on a 35mm lens, "
             "shallow depth of field, true-to-life colours. No text, no letters, no logos, no watermark."),
    "cartoon": ("Bright, friendly 2D cartoon illustration for young children, clean outlines, soft shading, "
                "cheerful colours, simple uncluttered background. No text, no letters, no logos, no watermark."),
}


def offline() -> bool:
    """STUDIO_OFFLINE=1 (the test suite): never call the free image services."""
    import os
    return os.environ.get("STUDIO_OFFLINE") == "1"


def cloudflare_ready() -> bool:
    return bool((settings.get("visuals.cloudflare_account_id") or "").strip()) and \
        settings.has_secret("cloudflare_api_token")


def cloudflare_used_today() -> int:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    return int(db.q1("SELECT COALESCE(SUM(output_tokens),0) AS n FROM ai_usage WHERE provider='cloudflare' AND ok=1 "
                     "AND created_at>=?", (day,))["n"])


class AllowanceUsedUp(net.FetchError):
    """Today's free image allowance is spent; it resets at 00:00 UTC (05:30 IST)."""


def cf_neurons(w: int, h: int, refs: int = 0) -> int:
    return int(-(-w // 512) * -(-h // 512) * 26.05 + refs * 5.37)


def generate_cloudflare(prompt: str, fmt: str, out: Path, look: str = "real", *, refs: list[Path] | None = None,
                        size: tuple[int, int] | None = None) -> Candidate:
    """Free FLUX.2 klein image on Cloudflare Workers AI, styled for the channel (photo-real or kids' cartoon).
    refs: up to 4 reference pictures the model keeps faces/places consistent with.
    Stays inside the free daily allowance: when it is used up, the next option is tried instead of paying."""
    if offline():
        raise net.FetchError("offline")
    if not cloudflare_ready():
        raise net.FetchError("Cloudflare Workers AI is not set up (token + account ID)")
    w, h = size or ((768, 1344) if fmt == "short" else (1344, 768))
    refs = [r for r in (refs or []) if r and Path(r).exists()][:4]
    neurons = cf_neurons(w, h, len(refs))
    if cloudflare_used_today() + neurons > CF_FREE_NEURONS * 0.95:
        raise AllowanceUsedUp("today's free Cloudflare image allowance is used up")
    seed = int(hashlib.sha1(out.name.encode() + prompt.encode()).hexdigest()[:7], 16)
    files = {}
    for i, ref in enumerate(refs):                 # references must be under 512x512
        with Image.open(ref) as im:
            im = im.convert("RGB")
            im.thumbnail((504, 504))
            buf = io.BytesIO()
            im.save(buf, "PNG")
        files[f"input_image_{i}"] = (f"ref{i}.png", buf.getvalue(), "image/png")
    form = {k: (None, v) for k, v in {"prompt": f"{prompt}. {LOOK_PROMPT.get(look, LOOK_PROMPT['real'])}"[:2400],
                                      "width": str(w), "height": str(h), "seed": str(seed)}.items()} | files
    account = settings.get("visuals.cloudflare_account_id").strip()
    # IPv4 only: this network advertises IPv6 it can't carry reliably, which made each image take minutes
    with httpx.Client(transport=httpx.HTTPTransport(local_address="0.0.0.0", retries=1),
                      timeout=httpx.Timeout(180, connect=15)) as client:
        r = client.post(f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{CF_MODEL}", files=form,
                        headers={"Authorization": f"Bearer {settings.secret('cloudflare_api_token')}"})
    ok = r.status_code == 200
    try:
        db.insert("ai_usage", {"provider": "cloudflare", "model": CF_MODEL, "task": "scene_image", "input_tokens": 0,
                               "output_tokens": neurons if ok else 0, "cost_usd": 0.0, "ok": int(ok),
                               "error": "" if ok else f"HTTP {r.status_code} {r.text[:250]}", "created_at": db.iso()})
    except Exception:  # usage logging must never stop a video
        pass
    if r.status_code == 429:
        raise AllowanceUsedUp("Cloudflare free image allowance reached for today")
    r.raise_for_status()
    img = (r.json().get("result") or {}).get("image") or ""
    if not img:
        raise net.FetchError("Cloudflare returned no image (the prompt may have been filtered)")
    out.write_bytes(base64.b64decode(img))
    with Image.open(out) as im:
        iw, ih = im.size
    return Candidate("cloudflare_image", out.stem, "image", "", "", iw, ih, "AI-generated (FLUX.2 klein, Cloudflare)",
                     "", "AI image", attribution="", extra={"synthetic": True})


def trim_bars(im: Image.Image) -> Image.Image:
    """Remove black letterbox/pillarbox bars an image model sometimes paints in for a "cinematic" prompt, then
    restore the original size (centre crop), so the frame stays full."""
    w, h = im.size
    g = im.convert("L")
    dark = lambda box: max(g.crop(box).getextrema()[1], 0) < 24
    top = next((y for y in range(0, h // 4) if not dark((0, y, w, y + 1))), 0)
    bottom = next((y for y in range(h - 1, h - h // 4, -1) if not dark((0, y, w, y + 1))), h - 1) + 1
    left = next((x for x in range(0, w // 4) if not dark((x, 0, x + 1, h))), 0)
    right = next((x for x in range(w - 1, w - w // 4, -1) if not dark((x, 0, x + 1, h))), w - 1) + 1
    if (top, bottom, left, right) == (0, h, 0, w):
        return im
    from PIL import ImageOps
    return ImageOps.fit(im.crop((left, top, right, bottom)), (w, h), Image.LANCZOS)


HF_FLUX_SPACE = "https://black-forest-labs-flux-1-schnell.hf.space"     # Black Forest Labs' own FLUX.1 [schnell] Space


def generate_hf_flux(prompt: str, fmt: str, out: Path, look: str = "real", *,
                     size: tuple[int, int] | None = None) -> Candidate:
    """Free FLUX.1 [schnell] (Apache-2.0) image from Black Forest Labs' Hugging Face ZeroGPU Space — the next free
    tier once Cloudflare's daily allowance is spent. Uses the owner's free Hugging Face token when saved (their own
    daily ZeroGPU minutes), otherwise the smaller anonymous allowance. Quota exhaustion raises AllowanceUsedUp."""
    if offline():
        raise net.FetchError("offline")
    w, h = size or ((768, 1344) if fmt == "short" else (1344, 768))
    seed = int(hashlib.sha1(out.name.encode() + prompt.encode()).hexdigest()[:7], 16)
    headers = {"Authorization": f"Bearer {settings.secret('hf_token')}"} if settings.has_secret("hf_token") else {}
    data = [f"{prompt}. {LOOK_PROMPT.get(look, LOOK_PROMPT['real'])}"[:1800], seed, False, w, h, 4]
    result, error = None, None
    with httpx.Client(transport=httpx.HTTPTransport(local_address="0.0.0.0", retries=1), headers=headers,
                      timeout=httpx.Timeout(240, connect=20)) as c:
        r = c.post(f"{HF_FLUX_SPACE}/gradio_api/call/infer", json={"data": data})
        r.raise_for_status()
        with c.stream("GET", f"{HF_FLUX_SPACE}/gradio_api/call/infer/{r.json()['event_id']}") as s:
            kind = ""
            for line in s.iter_lines():
                if line.startswith("event:"):
                    kind = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and kind in ("complete", "error"):
                    payload = line.split(":", 1)[1].strip()
                    result, error = (json.loads(payload), None) if kind == "complete" else (None, payload)
                    break
        ok = bool(result) and error is None
        try:
            db.insert("ai_usage", {"provider": "hf_flux", "model": "FLUX.1-schnell", "task": "scene_image",
                                   "input_tokens": 0, "output_tokens": 1 if ok else 0, "cost_usd": 0.0, "ok": int(ok),
                                   "error": "" if ok else ("quota (no detail)" if (error or "null").strip().lower()
                                                           in ("null", "none", "") else error)[:250],
                                   "created_at": db.iso()})
        except Exception:  # usage logging must never stop a video
            pass
        if not ok:
            msg = error or "no result from the Space"
            # an empty error ("null") is how the Space reports a spent anonymous GPU allowance
            if msg.strip().lower() in ("null", "none", "") or \
                    any(k in msg.lower() for k in ("quota", "exceeded", "gpu task aborted", "zerogpu")):
                raise AllowanceUsedUp("today's free Hugging Face GPU minutes are used up")
            raise net.FetchError(f"FLUX Space error: {msg[:200]}")
        img = result[0] if isinstance(result, list) else result
        url = (img or {}).get("url") or f"{HF_FLUX_SPACE}/gradio_api/file={(img or {}).get('path')}"
        dl = c.get(url)
        dl.raise_for_status()
    with Image.open(io.BytesIO(dl.content)) as im:
        im = trim_bars(im.convert("RGB"))
        im.save(out, "PNG")
        iw, ih = im.size
    return Candidate("hf_flux", out.stem, "image", "", "", iw, ih, "AI-generated (FLUX.1 schnell, Apache-2.0)",
                     "", "AI image", attribution="", extra={"synthetic": True})


def generate_gemini_image(prompt: str, fmt: str, out: Path) -> Candidate:
    from ..ai import router
    key = settings.secret("gemini_api_key")
    if not key:
        raise net.FetchError("no Gemini API key")
    price = float(settings.get("visuals.gemini_image_price"))
    if router.month_spend() + price > float(settings.get("ai.monthly_budget_usd")):
        raise net.FetchError("monthly AI budget reached — image generation skipped")
    model = settings.get("visuals.gemini_image_model")
    body = {"contents": [{"parts": [{"text": prompt + ". Photorealistic or illustrative still, no text, no logos, no watermark."}]}],
            "generationConfig": {"responseModalities": ["IMAGE"],
                                 "imageConfig": {"aspectRatio": "9:16" if fmt == "short" else "16:9"}}}
    r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                   json=body, headers={"x-goog-api-key": key}, timeout=300)
    db.insert("ai_usage", {"provider": "gemini", "model": model, "task": "image", "input_tokens": 0, "output_tokens": 0,
                           "cost_usd": price if r.status_code == 200 else 0.0, "ok": int(r.status_code == 200),
                           "error": "" if r.status_code == 200 else r.text[:300], "created_at": db.iso()})
    r.raise_for_status()
    for part in (r.json().get("candidates") or [{}])[0].get("content", {}).get("parts", []):
        if part.get("inlineData", {}).get("data"):
            out.write_bytes(base64.b64decode(part["inlineData"]["data"]))
            with Image.open(out) as im:
                w, h = im.size
            return Candidate("gemini_image", out.stem, "image", "", "", w, h, "AI-generated (Gemini)", "",
                             "Google Gemini", attribution="", extra={"synthetic": True})
    raise net.FetchError("Gemini returned no image")


# ------------------------------------------------------------------ verification & download

def probe_video(path: Path) -> dict[str, Any]:
    out = subprocess.run([config.FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,codec_name:format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, timeout=60)
    data = json.loads(out.stdout or "{}")
    st = (data.get("streams") or [{}])[0]
    return {"width": st.get("width", 0), "height": st.get("height", 0), "codec": st.get("codec_name"),
            "duration": float((data.get("format") or {}).get("duration") or 0)}


def fetch(c: Candidate) -> Path:
    ext = ".mp4" if c.kind == "video" else ".jpg"
    dest = config.ASSET_CACHE_DIR / c.provider / f"{re.sub(r'[^A-Za-z0-9_-]', '_', c.provider_id)}{ext}"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    if c.kind == "video":
        net.download(c.url, dest, max_bytes=150_000_000, allowed_types=("video/", "application/ogg", "application/octet-stream"))
        info = probe_video(dest)
        if not info["width"] or info["duration"] < 1.5:
            dest.unlink(missing_ok=True)
            raise net.FetchError("downloaded clip is not a usable video")
    else:
        raw = dest.with_suffix(".src")
        net.download(c.url, raw, max_bytes=40_000_000, allowed_types=("image/",))
        try:
            with Image.open(raw) as im:
                im.verify()
            with Image.open(raw) as im:
                im.convert("RGB").save(dest, "JPEG", quality=94)
        finally:
            raw.unlink(missing_ok=True)
    return dest


# ------------------------------------------------------------------ per-scene selection

GENERATORS = ("cloudflare_image", "hf_flux", "local_sd", "gemini_image")
FREE_GENERATORS = ("cloudflare_image", "hf_flux")   # free allowances, tried in this order before local/paid ones
PHOTO_ONLY = ("pexels", "openverse", "wikimedia")       # real photos/footage only — never used for kids' cartoon style


def provider_order(scene: dict[str, Any]) -> list[str]:
    enabled = settings.get("visuals.enabled") or {}
    order = list(settings.get("visuals.order") or [])
    for gen in FREE_GENERATORS:                    # free generators: before the local/paid ones (older saved orders)
        if gen not in order:
            at = next((i for i, p in enumerate(order) if p in ("local_sd", "gemini_image", "procedural")), len(order))
            order.insert(at, gen)
    order = [p for p in order if enabled.get(p, p in FREE_GENERATORS)]
    if scene["visual_type"] == "illustration":
        gens = [p for p in order if p in GENERATORS]
        order = gens + [p for p in order if p not in gens]
    return order


def _words(text: str) -> set[str]:
    return {w.rstrip("s") for w in re.findall(r"[a-z]+", (text or "").lower()) if len(w) > 2 and w not in _GENERIC}


def text_score(text: str, query: str) -> float | None:
    """Share of the scene's search words found in the media's own tags/description (None = no description)."""
    q, t = _words(query), _words(text)
    if not q or not t:
        return None
    return sum(1 for w in q if w in t) / len(q)


def queries_for(scene: dict[str, Any]) -> list[str]:
    """The scene's search, then the same without filler words, then the key words of its visual description."""
    q = (scene.get("visual_query") or "").strip()
    core = " ".join(w for w in q.split() if w.lower() not in _GENERIC)
    desc = " ".join([w for w in re.findall(r"[A-Za-z]+", scene.get("visual_description") or "")
                     if len(w) > 3 and w.lower() not in _GENERIC][:4])
    return [x for x in dict.fromkeys([q, core, desc]) if x]


def _min_side(kind: str, fmt: str) -> int:
    """Sharpness floor: stills must cover a 1080p frame (they're zoomed slowly); clips at least 720p."""
    return 720 if kind == "video" else (1000 if fmt == "short" else 1080)


def _frame(c: Candidate, path: Path) -> bytes:
    """A still to look at: the image itself, or a frame a third of the way into a clip."""
    if c.kind == "image":
        return path.read_bytes()
    r = subprocess.run([config.FFMPEG, "-v", "error", "-ss", f"{max(0.5, c.duration / 3):.2f}", "-i", str(path),
                        "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "-"], capture_output=True, timeout=60)
    return r.stdout


def _want(scene: dict[str, Any]) -> str:
    want = f"{scene.get('visual_description') or ''} ({scene.get('visual_query') or ''})"
    if scene.get("_look") == "cartoon":
        want += " — it must be a bright, child-friendly cartoon or illustration (a real photo scores 0-2)"
    return want


def _preview(c: Candidate) -> bytes | None:
    """The candidate's small preview image (image hosts throttle bursts: paced and retried)."""
    if not c.extra.get("thumb"):
        return None
    tmp = config.ASSET_CACHE_DIR / "previews" / f"{c.provider}_{re.sub(r'[^A-Za-z0-9_-]', '_', c.provider_id)}.img"
    for wait in (0.3, 2):
        try:
            if not tmp.exists():
                time.sleep(wait)
                net.download(c.extra["thumb"], tmp, max_bytes=8_000_000, allowed_types=("image/",))
            return tmp.read_bytes()
        except net.FetchError as e:
            if "429" not in str(e):
                break
        except OSError:
            break
    return None


def _verify(c: Candidate, scene: dict[str, Any], topic: str, full: Path | None = None) -> dict[str, Any] | None:
    """A vision model scores one candidate (its preview, or the downloaded file). None = no vision model could look."""
    if not vision.available():
        return None
    try:
        raw = _frame(c, full) if full is not None else _preview(c)
        if not raw:
            return None
        return vision.judge(raw, topic=topic, narration=scene.get("narration") or "",
                            want=_want(scene),
                            key=f"{c.provider}:{c.provider_id}:{scene.get('visual_query')}")
    except (net.FetchError, httpx.HTTPError, OSError, subprocess.SubprocessError) as e:
        log.info("preview check skipped for %s:%s: %s", c.provider, c.provider_id, e)
        return None


def _verify_many(cs: list[Candidate], scene: dict[str, Any], topic: str) -> list[dict[str, Any] | None]:
    """Score several candidates' previews in one vision request; None for any that couldn't be looked at."""
    if not vision.available() or not cs:
        return [None] * len(cs)
    raws = [_preview(c) for c in cs]
    have = [i for i, r in enumerate(raws) if r]
    if not have:
        return [None] * len(cs)
    res = vision.judge_many([raws[i] for i in have], topic=topic, narration=scene.get("narration") or "",
                            want=_want(scene))
    out: list[dict[str, Any] | None] = [None] * len(cs)
    for i, v in zip(have, res or []):
        out[i] = v
    return out


def choose(scene: dict[str, Any], idx: int, fmt: str, used: set[str], out_dir: Path, seed: str,
           providers: list[str] | None = None, failures: dict[str, int] | None = None,
           topic: str = "", look: str = "real") -> dict[str, Any]:
    """Return an asset record for one scene. Never raises.

    Stock candidates are ranked by how well their own tags/description match the scene, then a vision model
    looks at each preview and only a picture that clearly fits the scene is used (visuals.min_relevance).
    Nothing fits → an AI-generated still of the scene (when enabled) → a designed card with the scene's own
    words. An unrelated picture is never used just to fill the screen.
    `failures` is shared across a production's scenes: a provider that fails twice is skipped afterwards."""
    w, h = config.frame_size(fmt)
    scene = {**scene, "_look": look}
    notes: list[str] = []
    failures = failures if failures is not None else {}
    need = int(settings.get("visuals.min_relevance") or 6)
    looks = 0                                              # vision checks spent on this scene
    near: tuple[Candidate, Path, dict[str, Any]] | None = None   # best "on topic but not exact" (score 5)

    def take_near() -> dict[str, Any]:
        c, path, verdict = near
        used.add(f"{c.provider}:{c.provider_id}")
        rec = _record(c, path, idx)
        rec.update({"notes": notes, "relevance": {"how": "vision check (closest on-topic match)",
                                                  "score": verdict["score"], "shows": verdict["shows"],
                                                  "by": verdict["by"]}})
        return rec
    if scene["visual_type"] != "text_card":
        want_video = scene["visual_type"] == "video" and settings.get("visuals.prefer_video")
        for prov in (provider_order(scene) if providers is None else providers):
            if prov == "procedural":
                break
            if look == "cartoon" and prov in PHOTO_ONLY:
                continue                                    # kids' videos stay in cartoon style: no real photos
            if failures.get(prov, 0) >= 2:
                continue
            try:
                if prov in GENERATORS:
                    if near and prov not in FREE_GENERATORS:  # free on-topic stock before a paid/local image
                        return take_near()
                    target = out_dir / f"gen_{idx:03d}.png"
                    prompt = (f"{scene['visual_description']} ({scene['visual_query']}). For a video about: {topic}"
                              if topic else f"{scene['visual_description']} ({scene['visual_query']})")
                    if prov == "cloudflare_image":
                        c = generate_cloudflare(prompt, fmt, target, look)
                    elif prov == "hf_flux":
                        c = generate_hf_flux(prompt, fmt, target, look)
                    elif prov == "local_sd":
                        c = generate_local_sd(f"{prompt}. {LOOK_PROMPT.get(look, '')}", w, h, target)
                    else:
                        c = generate_gemini_image(f"{prompt}. {LOOK_PROMPT.get(look, '')}", fmt, target)
                    rec = _record(c, target, idx)
                    rec.update({"notes": notes, "relevance": {"how": "generated from the scene description"}})
                    return rec
                for query in queries_for(scene):
                    for kind in (["video", "image"] if want_video else ["image"]):
                        found = SEARCHERS[prov](query, kind, fmt, look) if prov == "pixabay" else \
                            SEARCHERS[prov](query, kind, fmt)
                        cands = [c for c in found
                                 if f"{c.provider}:{c.provider_id}" not in used
                                 and min(c.width, c.height) >= _min_side(c.kind, fmt)
                                 and (c.kind == "image" or c.duration >= 3)]
                        for c in cands:
                            c.extra["text_score"] = text_score(c.extra.get("text", ""), scene["visual_query"])
                        # clearly unrelated by its own tags: dropped before anyone looks at it
                        cands = [c for c in cands if c.extra["text_score"] is None or c.extra["text_score"] > 0]
                        order = sorted(range(len(cands)), key=lambda i: (
                            -(cands[i].extra["text_score"] if cands[i].extra["text_score"] is not None else 0.4),
                            (cands[i].height > cands[i].width) != (fmt == "short"), i))
                        top = [cands[i] for i in order[:4]]
                        if looks >= 3:                   # three looks for one scene: stop searching, use a fallback
                            break
                        verdicts = _verify_many(top, scene, topic)      # one request scores all four previews
                        if any(v is not None for v in verdicts):
                            looks += 1
                        ranked = sorted(zip(top, verdicts), key=lambda cv: -(cv[1]["score"] if cv[1] else -1))
                        for c, verdict in ranked:                   # best-scoring picture first
                            if failures.get(prov, 0) >= 2:
                                break
                            if verdict is None and (c.extra["text_score"] or 0) < 0.5:
                                continue                    # couldn't be looked at: only take a clear tag match
                            if verdict is None and c.kind == "video" and vision.available():
                                continue                    # never download a whole clip just to check it
                            if verdict is not None:
                                if verdict["score"] < need - 1:
                                    notes.append(f"{prov}:{c.provider_id} rejected ({verdict['score']}/10: {verdict['shows']})")
                                    continue
                            try:
                                path = fetch(c)
                            except net.FetchError as e:
                                notes.append(f"{prov}: {e}")
                                if not isinstance(e, net.BlockedURL) and "429" in str(e):
                                    failures[prov] = failures.get(prov, 0) + 1
                                continue
                            if verdict is None:             # no preview: look at the real file instead
                                verdict = _verify(c, scene, topic, full=path)
                                if verdict is None and (c.extra["text_score"] or 0) < 0.5:
                                    continue
                            if verdict is not None and verdict["score"] < need:
                                notes.append(f"{prov}:{c.provider_id} close ({verdict['score']}/10: {verdict['shows']})")
                                if verdict["score"] >= need - 1 and (not near or verdict["score"] > near[2]["score"]):
                                    near = (c, path, verdict)
                                continue
                            used.add(f"{c.provider}:{c.provider_id}")
                            rec = _record(c, path, idx)
                            rec["notes"] = notes
                            rec["relevance"] = ({"how": "vision check", "score": verdict["score"],
                                                 "shows": verdict["shows"], "by": verdict["by"]} if verdict else
                                                {"how": "tag match", "text_score": round(c.extra["text_score"] or 0, 2)})
                            return rec
            except (net.FetchError, httpx.HTTPError, KeyError, ValueError) as e:
                notes.append(f"{prov}: {e}"[:200])
                if isinstance(e, (net.FetchError, httpx.HTTPError)) and not isinstance(e, net.BlockedURL):
                    failures[prov] = failures.get(prov, 0) + 1
    if near:
        return take_near()
    # nothing fitting was found (or a text card was asked for): a designed card with the scene's own words
    path = out_dir / f"card_{idx:03d}.jpg"
    text = (scene.get("on_screen_text") or "").strip()
    if not text:
        text = re.split(r"[।.!?]", scene.get("narration") or "")[0].strip()[:70]
    img = graphics.text_card(text, w, h, f"{seed}:{idx}") if text else graphics.procedural_background(w, h, f"{seed}:{idx}")
    img.save(path, "JPEG", quality=93)
    return {"scene_index": idx, "kind": "image", "provider": "procedural", "provider_id": path.stem, "source_url": "",
            "page_url": "", "local_path": str(path), "license": "Original (generated by this app)", "license_url": "",
            "author": settings.get("brand").get("channel_name") or "this channel", "attribution": "",
            "width": w, "height": h, "duration": 0.0, "notes": notes, "is_card": bool(text),
            "relevance": {"how": "card with the scene's own words" if text else "abstract background"}}


def _record(c: Candidate, path: Path, idx: int) -> dict[str, Any]:
    return {"scene_index": idx, "kind": c.kind, "provider": c.provider, "provider_id": c.provider_id,
            "source_url": c.url, "page_url": c.page_url, "local_path": str(path), "license": c.license,
            "license_url": c.license_url, "author": c.author, "attribution": c.attribution,
            "width": c.width, "height": c.height, "duration": c.duration, "synthetic": bool(c.extra.get("synthetic")),
            "is_card": False}


_REQUIRES_ATTRIBUTION = re.compile(r"\bBY\b", re.I)


def credits(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact credit lines. `required` marks licences that legally need attribution (CC BY /
    BY-SA); public-domain, CC0, Pexels and Pixabay credits are courtesy and may be dropped
    when the 5,000-byte description limit is tight."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for a in assets:
        if a["provider"] in ("procedural", "local_sd", "gemini_image", "gemini_video", "cloudflare_image", "hf_flux") \
                or not a.get("license"):
            continue
        lic = a["license"]
        required = bool(_REQUIRES_ATTRIBUTION.search(lic)) and "CC0" not in lic.upper()
        url = a.get("page_url") or ""
        pid = (a.get("provider_id") or "").split("#")[0]
        if a["provider"] == "wikimedia" and pid.isdigit():
            url = f"https://commons.wikimedia.org/?curid={pid}"      # short, permanent link to the file page
        author = re.sub(r"\s+", " ", a.get("author") or "unknown").strip()[:60]
        if a["provider"] in ("pexels", "pixabay"):
            line = f"{'Video' if a['kind'] == 'video' else 'Photo'} by {author} on {a['provider'].title()} — {url}"
        else:
            m = re.search(r'"([^"]{1,200})"', a.get("attribution") or "")
            title = (m.group(1)[:50] + ("…" if len(m.group(1)) > 50 else "")) if m else "Image"
            line = f"\"{title}\" by {author}, {lic} — {url}"
        if line not in seen:
            seen.add(line)
            out.append({"line": line, "required": required})
    return out


def search_url_safe(q: str) -> str:
    return quote(q)
