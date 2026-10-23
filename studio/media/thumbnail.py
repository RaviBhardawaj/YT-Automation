"""Thumbnail generation (1280x720, < 2 MB) with a measured text-contrast guarantee."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageStat

from .. import config, settings
from . import graphics, render

MAX_BYTES = 2 * 1024 * 1024
MIN_CONTRAST = 4.5


def _background(assets: list[dict[str, Any]], work: Path, seed: str) -> Image.Image:
    """Best visual from the production: largest non-card image, else a frame from a clip."""
    imgs = [a for a in assets if a["kind"] == "image" and a["provider"] != "procedural"]
    vids = [a for a in assets if a["kind"] == "video"]
    if imgs:
        best = max(imgs, key=lambda a: (a.get("width") or 0) * (a.get("height") or 0))
        return Image.open(best["local_path"]).convert("RGB")
    if vids:
        frame = render.extract_frame(Path(vids[0]["local_path"]), 1.0, work / "thumb_frame.jpg")
        return Image.open(frame).convert("RGB")
    return graphics.procedural_background(config.THUMB_W, config.THUMB_H, seed)


def make(pkg: dict[str, Any], assets: list[dict[str, Any]], fmt: str, work: Path, out: Path, seed: str) -> dict[str, Any]:
    brand = settings.get("brand")
    w, h = config.THUMB_W, config.THUMB_H
    bg = graphics.cover(_background(assets, work, seed), w, h, focus_y=0.4)
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
            "text": text, "font_size": fnt.size}
