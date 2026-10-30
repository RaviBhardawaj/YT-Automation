"""Thumbnail generation (1280x720, < 2 MB) with a measured text-contrast guarantee."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageStat

from .. import config, settings
from . import graphics, render

MAX_BYTES = 2 * 1024 * 1024
MIN_CONTRAST = 4.5


def _quality(img: Image.Image) -> float:
    """0–1: sharp, colourful, well-exposed pictures make readable thumbnails; blurry, flat or dark ones don't."""
    small = img.convert("RGB").resize((256, max(1, int(256 * img.height / max(1, img.width)))))
    edges = ImageStat.Stat(small.convert("L").filter(ImageFilter.FIND_EDGES)).mean[0]
    r, g, b = ImageStat.Stat(small).mean[:3]
    contrast = ImageStat.Stat(small.convert("L")).stddev[0]
    colour = abs(r - g) + abs((r + g) / 2 - b)
    exposure = 1.0 - min(1.0, abs((r + g + b) / 3 - 120) / 120)
    return max(0.0, min(1.0, 0.45 * min(1.0, edges / 25) + 0.25 * min(1.0, (contrast + colour) / 90) + 0.30 * exposure))


def _relevance(a: dict[str, Any]) -> float:
    rel = a.get("relevance")
    if isinstance(rel, str):
        import json
        try:
            rel = json.loads(rel or "{}")
        except ValueError:
            rel = {}
    score = (rel or {}).get("score")
    if isinstance(score, (int, float)):
        return max(0.0, min(1.0, score / 10))
    return 0.6 if a.get("provider") not in ("procedural",) else 0.0     # drawn from the scene, not checked


def pick_background(assets: list[dict[str, Any]], work: Path) -> tuple[Image.Image | None, dict[str, Any]]:
    """The strongest picture for the thumbnail: how well it matches its scene (vision score where checked), how
    sharp/colourful/well-exposed it is, and whether it is big enough — never simply the first shot or the largest
    file. Clips are judged on a frame from their middle. Cards and drawn backgrounds are skipped."""
    best, best_score, why = None, -1.0, {}
    for a in assets:
        if a["provider"] == "procedural" or "#card" in str(a.get("provider_id") or "") or not a.get("local_path"):
            continue
        try:
            if a["kind"] == "video":
                mid = max(0.5, float(a.get("duration") or 2.0) / 2)
                frame = render.extract_frame(Path(a["local_path"]), mid, work / f"thumb_cand_{a.get('scene_index', 0)}.jpg")
                img = Image.open(frame).convert("RGB")
            elif a["kind"] == "image":
                img = Image.open(a["local_path"]).convert("RGB")
            else:
                continue
        except Exception:  # noqa: BLE001 - an unreadable candidate is just skipped
            continue
        small_side = min(img.size)
        size_ok = 1.0 if small_side >= 700 else small_side / 700
        q, rel = _quality(img), _relevance(a)
        score = 0.5 * rel + 0.35 * q + 0.15 * size_ok + (0.03 if (a.get("scene_index") or 0) <= 2 else 0.0)
        if score > best_score:
            best, best_score = img, score
            why = {"scene": a.get("scene_index"), "provider": a["provider"], "kind": a["kind"],
                   "score": round(score, 3), "relevance": round(rel, 2), "quality": round(q, 2)}
    return best, why


def make(pkg: dict[str, Any], assets: list[dict[str, Any]], fmt: str, work: Path, out: Path, seed: str) -> dict[str, Any]:
    brand = settings.get("brand")
    w, h = config.THUMB_W, config.THUMB_H
    chosen, why = pick_background(assets, work)
    bg = graphics.cover(chosen if chosen is not None else
                        graphics.procedural_background(w, h, seed), w, h, focus_y=0.4)
    bg = ImageEnhance.Contrast(ImageEnhance.Color(bg).enhance(1.18)).enhance(1.10)
    text = pkg["thumbnail"]["text"].strip()
    if not graphics.DEVANAGARI.search(text):
        text = text.upper()
    text_color = graphics.hex_rgb(brand.get("primary", "#FFD23F"))
    box_w = int(w * 0.58)
    fnt, lines, lh = graphics.fit_text(text, "display", box_w, int(h * 0.62), int(h * 0.24), 48, max_lines=3, spacing=1.02)
    block_h = lh * len(lines)
    top = (h - block_h) // 2
    left_center = int(w * 0.06) + box_w // 2
    region = (int(w * 0.03), max(0, top - 20), int(w * 0.03) + box_w + 40, min(h, top + block_h + 30))

    alpha = 150
    ratio = 0.0
    for _ in range(8):
        scrim = Image.new("L", (w, h), 0)
        sd = ImageDraw.Draw(scrim)
        for x in range(int(w * 0.72)):
            sd.line([(x, 0), (x, h)], fill=int(alpha * (1 - x / (w * 0.72)) ** 0.8))
        composed = Image.composite(Image.new("RGB", (w, h), (0, 0, 0)), bg, scrim)
        mean = tuple(int(c) for c in ImageStat.Stat(composed.crop(region)).mean[:3])
        ratio = graphics.contrast(text_color, mean)
        if ratio >= MIN_CONTRAST or alpha >= 250:
            break
        alpha = min(250, alpha + 25)
    img = composed.convert("RGBA")

    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    graphics.draw_lines(ImageDraw.Draw(shadow), lines, fnt, lh, left_center + 6, top + 8, (0, 0, 0, 220),
                        stroke=max(6, fnt.size // 10), stroke_fill=(0, 0, 0, 220))
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(10)))
    d = ImageDraw.Draw(img)
    graphics.draw_lines(d, lines, fnt, lh, left_center, top, text_color, stroke=max(5, fnt.size // 12), stroke_fill=(0, 0, 0))
    d.rectangle((int(w * 0.06), top - 26, int(w * 0.06) + 140, top - 12), fill=graphics.hex_rgb(brand.get("accent", "#FF4D6D")))
    if brand.get("channel_name"):
        bf = graphics.font_for(brand["channel_name"], "heavy", 30)
        d.text((int(w * 0.06), h - 60), brand["channel_name"], font=bf, fill=(255, 255, 255), stroke_width=3, stroke_fill=(0, 0, 0))
    size = graphics.save_jpeg_under(img, out, MAX_BYTES)
    return {"path": str(out), "width": w, "height": h, "size_bytes": size, "contrast": round(ratio, 2),
            "text": text, "font_size": fnt.size, "background": why or {"provider": "procedural"}}
