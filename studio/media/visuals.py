"""Scene visuals: licensed stock, optional AI image generation, and original procedural art.

Every asset is stored with provider, source page, author, licence and an attribution line.
Only licences that allow commercial use *and* modification are accepted.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from PIL import Image

from .. import config, db, net, settings
from . import graphics

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
            out.append(Candidate("pexels", f"v{v['id']}", "video", f["link"], v.get("url", ""), f["width"], f["height"],
                                 "Pexels License", "https://www.pexels.com/license/", user, float(v.get("duration") or 0),
                                 f"Video by {user} on Pexels"))
        return out
    data = net.get_json("https://api.pexels.com/v1/search",
                        {"query": query, "orientation": _orientation(fmt), "per_page": 15}, hdr)
    return [Candidate("pexels", f"p{p['id']}", "image", p["src"].get("large2x") or p["src"]["original"], p.get("url", ""),
                      p["width"], p["height"], "Pexels License", "https://www.pexels.com/license/",
                      p.get("photographer", "Pexels contributor"),
                      attribution=f"Photo by {p.get('photographer', 'unknown')} on Pexels")
            for p in data.get("photos", [])]


def pixabay(query: str, kind: str, fmt: str) -> list[Candidate]:
    key = settings.secret("pixabay_api_key")
    if not key:
        return []
    lic, lic_url = "Pixabay Content License", "https://pixabay.com/service/license-summary/"
    if kind == "video":
        data = net.get_json("https://pixabay.com/api/videos/", {"key": key, "q": query[:100], "per_page": 12, "safesearch": "true"})
        out = []
        for h in data.get("hits", []):
            v = h.get("videos", {}).get("large") or h.get("videos", {}).get("medium") or {}
            if not v.get("url") or (v.get("width") or 0) < 720:
                continue
            out.append(Candidate("pixabay", f"v{h['id']}", "video", v["url"], h.get("pageURL", ""), v["width"], v["height"],
                                 lic, lic_url, h.get("user", "Pixabay contributor"), float(h.get("duration") or 0),
                                 f"Video by {h.get('user', 'unknown')} on Pixabay"))
        return out
    data = net.get_json("https://pixabay.com/api/", {"key": key, "q": query[:100], "image_type": "photo", "per_page": 20,
                                                    "safesearch": "true",
                                                    "orientation": "vertical" if fmt == "short" else "horizontal"})
    return [Candidate("pixabay", f"p{h['id']}", "image", h["largeImageURL"], h.get("pageURL", ""), h["imageWidth"],
                      h["imageHeight"], lic, lic_url, h.get("user", "Pixabay contributor"),
                      attribution=f"Image by {h.get('user', 'unknown')} on Pixabay") for h in data.get("hits", [])]


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
                             f"\"{r.get('title', 'Image')}\" by {r.get('creator', 'unknown')}, {label}"))
    return out


def wikimedia(query: str, kind: str, fmt: str) -> list[Candidate]:
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
                             attribution=f"\"{title}\" by {artist.strip()[:80] or 'unknown'}, {lic}, via Wikimedia Commons"))
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
        net.download(c.url, dest, max_bytes=150_000_000, allowed_types=("video/", "application/octet-stream"))
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

def provider_order(scene: dict[str, Any]) -> list[str]:
    enabled = settings.get("visuals.enabled")
    order = [p for p in settings.get("visuals.order") if enabled.get(p)]
    if scene["visual_type"] == "illustration":
        gens = [p for p in order if p in ("local_sd", "gemini_image")]
        order = gens + [p for p in order if p not in gens]
    return order


def choose(scene: dict[str, Any], idx: int, fmt: str, used: set[str], out_dir: Path, seed: str,
           providers: list[str] | None = None, failures: dict[str, int] | None = None) -> dict[str, Any]:
    """Return an asset record for one scene. Never raises: falls back to procedural art.
    `failures` is shared across a production's scenes: a provider that fails twice
    (timeouts, DNS, HTTP errors) is skipped for the remaining scenes."""
    w, h = config.frame_size(fmt)
    notes: list[str] = []
    failures = failures if failures is not None else {}
    if scene["visual_type"] != "text_card":
        want_video = scene["visual_type"] == "video" and settings.get("visuals.prefer_video")
        q = scene["visual_query"].strip()
        queries = list(dict.fromkeys([q, " ".join(q.split()[:2])]))
        for prov in (provider_order(scene) if providers is None else providers):
            if prov == "procedural":
                break
            if failures.get(prov, 0) >= 2:
                continue
            try:
                if prov in ("local_sd", "gemini_image"):
                    target = out_dir / f"gen_{idx:03d}.png"
                    prompt = f"{scene['visual_description']} ({q})"
                    c = generate_local_sd(prompt, w, h, target) if prov == "local_sd" else generate_gemini_image(prompt, fmt, target)
                    return _record(c, target, idx)
                for query in queries:
                    kinds = ["video", "image"] if want_video else ["image"]
                    for kind in kinds:
                        cands = SEARCHERS[prov](query, kind, fmt)
                        cands = [c for c in cands if f"{c.provider}:{c.provider_id}" not in used
                                 and min(c.width, c.height) >= 600 and (c.kind == "image" or c.duration >= 3)]
                        cands.sort(key=lambda c: ((c.height > c.width) != (fmt == "short"), -min(c.width, c.height)))
                        for c in cands[:3]:
                            if failures.get(prov, 0) >= 2:
                                break
                            try:
                                path = fetch(c)
                            except net.FetchError as e:
                                notes.append(f"{prov}: {e}")
                                if not isinstance(e, net.BlockedURL) and "429" in str(e):
                                    failures[prov] = failures.get(prov, 0) + 1
                                continue
                            used.add(f"{c.provider}:{c.provider_id}")
                            rec = _record(c, path, idx)
                            rec["notes"] = notes
                            return rec
            except (net.FetchError, httpx.HTTPError, KeyError, ValueError) as e:
                notes.append(f"{prov}: {e}"[:200])
                if isinstance(e, (net.FetchError, httpx.HTTPError)) and not isinstance(e, net.BlockedURL):
                    failures[prov] = failures.get(prov, 0) + 1
    # procedural fallback / text cards: original artwork rendered locally
    path = out_dir / f"card_{idx:03d}.jpg"
    text = scene.get("on_screen_text") or ""
    if scene["visual_type"] == "text_card" and not text:
        text = scene["narration"].split("।")[0].split(".")[0][:70]
    img = graphics.text_card(text, w, h, f"{seed}:{idx}") if text and scene["visual_type"] == "text_card" \
        else graphics.procedural_background(w, h, f"{seed}:{idx}")
    img.save(path, "JPEG", quality=93)
    return {"scene_index": idx, "kind": "image", "provider": "procedural", "provider_id": path.stem, "source_url": "",
            "page_url": "", "local_path": str(path), "license": "Original (generated by this app)", "license_url": "",
            "author": settings.get("brand").get("channel_name") or "this channel", "attribution": "",
            "width": w, "height": h, "duration": 0.0, "notes": notes, "is_card": scene["visual_type"] == "text_card"}


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
        if a["provider"] in ("procedural", "local_sd", "gemini_image", "gemini_video") or not a.get("license"):
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
