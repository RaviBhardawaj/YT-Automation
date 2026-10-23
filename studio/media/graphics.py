"""Pillow drawing: fonts, fitted text, caption/headline overlays, procedural art, contrast maths.

All on-screen text is drawn here (libraqm shapes Devanagari correctly), so model-generated
text never reaches an FFmpeg filter string.
"""
from __future__ import annotations

import hashlib
import math
import random
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .. import config, settings

DEVANAGARI = re.compile(r"[ऀ-ॿ]")

_BRAND = str(config.FONTS_DIR)
FONT_CANDIDATES = {
    # (path, face index or variable-font instance name). SIL OFL fonts in data/brand/fonts come first:
    # their licence explicitly allows use in videos; macOS system fonts are a fallback only.
    "deva_heavy": [(f"{_BRAND}/NotoSansDevanagari-VF.ttf", "Black"), ("/System/Library/Fonts/Kohinoor.ttc", 3),
                   ("/System/Library/Fonts/Supplemental/ITFDevanagari.ttc", 1)],
    "deva_body": [(f"{_BRAND}/NotoSansDevanagari-VF.ttf", "SemiBold"), ("/System/Library/Fonts/Kohinoor.ttc", 2)],
    "latin_heavy": [(f"{_BRAND}/NotoSans-VF.ttf", "Black"), ("/System/Library/Fonts/Avenir Next.ttc", 8)],
    "latin_display": [(f"{_BRAND}/NotoSans-VF.ttf", "ExtraBold"), ("/System/Library/Fonts/HelveticaNeue.ttc", 9)],
    "latin_body": [(f"{_BRAND}/NotoSans-VF.ttf", "SemiBold"), ("/System/Library/Fonts/Avenir Next.ttc", 2)],
    "fallback": [("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 0)],
}
_used_fonts: set[str] = set()


@lru_cache(maxsize=None)
def _resolve(kind: str) -> tuple[str, int | str]:
    for path, idx in FONT_CANDIDATES.get(kind, []) + FONT_CANDIDATES["fallback"]:
        if Path(path).exists():
            return path, idx
    raise RuntimeError("no usable font found")


@lru_cache(maxsize=256)
def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    path, idx = _resolve(kind)
    _used_fonts.add(path)
    engine = ImageFont.Layout.RAQM if hasattr(ImageFont, "Layout") else None
    if isinstance(idx, str):
        f = ImageFont.truetype(path, size, layout_engine=engine)
        f.set_variation_by_name(idx)
        return f
    return ImageFont.truetype(path, size, index=idx, layout_engine=engine)


def font_licences() -> list[dict[str, str]]:
    """Licence status of every font file used so far in this process (for the compliance gate)."""
    out = []
    for path in sorted(_used_fonts or {_resolve(k)[0] for k in ("latin_heavy", "latin_body")}):
        p = Path(path)
        ofl = p.parent / (p.stem.replace("-VF", "") + "-OFL.txt")
        if p.is_relative_to(config.FONTS_DIR) and ofl.exists() and "Open Font License" in ofl.read_text(errors="ignore"):
            out.append({"font": p.name, "licence": "SIL Open Font License 1.1", "ok": "yes"})
        else:
            out.append({"font": p.name, "licence": "macOS system font (Apple licence terms)", "ok": "review"})
    return out


def font_for(text: str, weight: str, size: int) -> ImageFont.FreeTypeFont:
    if DEVANAGARI.search(text):
        return font("deva_heavy" if weight in ("heavy", "display") else "deva_body", size)
    return font({"heavy": "latin_heavy", "display": "latin_display"}.get(weight, "latin_body"), size)


def hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) if len(h) == 6 else (255, 255, 255)  # type: ignore[return-value]


def luminance(rgb: tuple[int, int, int]) -> float:
    def ch(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb[:3]
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# ------------------------------------------------------------------ text layout

def wrap(text: str, fnt: ImageFont.FreeTypeFont, max_w: int, draw: ImageDraw.ImageDraw) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=fnt) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def fit_text(text: str, weight: str, max_w: int, max_h: int, start: int, minimum: int,
             max_lines: int = 4, spacing: float = 1.12) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    size = start
    while True:
        fnt = font_for(text, weight, size)
        lines = wrap(text, fnt, max_w, probe)
        line_h = int(size * spacing * (1.35 if DEVANAGARI.search(text) else 1.0))
        widest = max((probe.textlength(ln, font=fnt) for ln in lines), default=0)
        if (len(lines) <= max_lines and line_h * len(lines) <= max_h and widest <= max_w) or size <= minimum:
            return fnt, lines, line_h
        size = max(minimum, int(size * 0.92))


def draw_lines(draw: ImageDraw.ImageDraw, lines: list[str], fnt: ImageFont.FreeTypeFont, line_h: int,
               center_x: int, top: int, fill: tuple, stroke: int = 0, stroke_fill: tuple = (0, 0, 0),
               highlight: tuple | None = None) -> None:
    for i, ln in enumerate(lines):
        y = top + i * line_h
        if highlight and i == 0 and len(lines) == 1 and len(ln.split()) > 1:
            # colour the longest word to give captions a focal point
            words = ln.split()
            key = max(range(len(words)), key=lambda k: len(words[k]))
            total = draw.textlength(ln, font=fnt)
            x = center_x - total / 2
            for k, w in enumerate(words):
                seg = w + (" " if k < len(words) - 1 else "")
                draw.text((x, y), seg, font=fnt, fill=highlight if k == key else fill,
                          stroke_width=stroke, stroke_fill=stroke_fill)
                x += draw.textlength(seg, font=fnt)
        else:
            draw.text((center_x, y), ln, font=fnt, fill=fill, anchor="ma", stroke_width=stroke, stroke_fill=stroke_fill)


# ------------------------------------------------------------------ overlays (RGBA, cropped)

def caption_png(text: str, frame_w: int, frame_h: int, fmt: str, out: Path) -> dict[str, int]:
    """Render one caption chunk. Returns the overlay position."""
    brand = settings.get("brand")
    bold = brand.get("caption_style", "bold") == "bold"
    max_w = int(frame_w * (0.84 if fmt == "short" else 0.78))
    if bold:
        start = int(frame_w * (0.085 if fmt == "short" else 0.042))
        fnt, lines, lh = fit_text(text, "heavy", max_w, int(frame_h * 0.22), start, int(start * 0.55), max_lines=3)
        stroke = max(4, fnt.size // 9)
    else:
        start = int(frame_w * (0.052 if fmt == "short" else 0.026))
        fnt, lines, lh = fit_text(text, "body", max_w, int(frame_h * 0.14), start, int(start * 0.6), max_lines=2)
        stroke = max(2, fnt.size // 14)
    pad = stroke * 3 + 10
    w = max_w + pad * 2
    h = lh * len(lines) + pad * 2 + int(fnt.size * 0.35)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if not bold:
        d.rounded_rectangle((pad // 2, pad // 2, w - pad // 2, h - pad // 2), radius=18, fill=(0, 0, 0, 150))
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw_lines(ImageDraw.Draw(shadow), lines, fnt, lh, w // 2, pad + 6, (0, 0, 0, 160), stroke, (0, 0, 0, 160))
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(6)))
    d = ImageDraw.Draw(img)
    draw_lines(d, lines, fnt, lh, w // 2, pad, hex_rgb(brand.get("text", "#FFFFFF")), stroke, (0, 0, 0),
               highlight=hex_rgb(brand.get("primary", "#FFD23F")) if bold else None)
    img.save(out)
    y_center = 0.66 if fmt == "short" else 0.84
    return {"x": (frame_w - w) // 2, "y": int(frame_h * y_center - h / 2), "w": w, "h": h}


def headline_png(text: str, frame_w: int, frame_h: int, fmt: str, out: Path) -> dict[str, int]:
    brand = settings.get("brand")
    max_w = int(frame_w * (0.82 if fmt == "short" else 0.6))
    start = int(frame_w * (0.06 if fmt == "short" else 0.03))
    fnt, lines, lh = fit_text(text, "display", max_w - 60, int(frame_h * 0.16), start, int(start * 0.6), max_lines=2)
    tw = int(max(ImageDraw.Draw(Image.new("RGB", (4, 4))).textlength(ln, font=fnt) for ln in lines))
    w, h = tw + 80, lh * len(lines) + 50 + int(fnt.size * 0.2)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = hex_rgb(brand.get("primary", "#FFD23F"))
    fg = (15, 15, 20) if contrast(bg, (15, 15, 20)) >= contrast(bg, (255, 255, 255)) else (255, 255, 255)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=min(28, h // 3), fill=bg + (240,))
    d.rectangle((0, h - 10, w, h), fill=hex_rgb(brand.get("accent", "#FF4D6D")) + (255,))
    draw_lines(d, lines, fnt, lh, w // 2, 22, fg)
    img.save(out)
    y = int(frame_h * (0.12 if fmt == "short" else 0.07))
    x = (frame_w - w) // 2 if fmt == "short" else int(frame_w * 0.05)
    return {"x": x, "y": y, "w": w, "h": h}


def watermark_png(frame_w: int, frame_h: int, fmt: str, out: Path) -> dict[str, int] | None:
    brand = settings.get("brand")
    name = (brand.get("channel_name") or "").strip()
    logo = brand.get("logo_path")
    if not brand.get("watermark") or not (name or logo):
        return None
    size = int(frame_w * (0.03 if fmt == "short" else 0.016))
    parts: list[Image.Image] = []
    if logo and Path(logo).exists():
        lg = Image.open(logo).convert("RGBA")
        lg.thumbnail((size * 3, size * 3))
        parts.append(lg)
    if name:
        fnt = font_for(name, "heavy", size)
        tw = int(ImageDraw.Draw(Image.new("RGB", (4, 4))).textlength(name, font=fnt))
        t = Image.new("RGBA", (tw + 8, int(size * 1.6)), (0, 0, 0, 0))
        ImageDraw.Draw(t).text((4, 0), name, font=fnt, fill=(255, 255, 255, 200), stroke_width=2, stroke_fill=(0, 0, 0, 160))
        parts.append(t)
    w = sum(p.width for p in parts) + 12 * (len(parts) - 1)
    h = max(p.height for p in parts)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    x = 0
    for p in parts:
        img.alpha_composite(p, (x, (h - p.height) // 2))
        x += p.width + 12
    img.save(out)
    margin = int(frame_w * 0.04)
    return {"x": frame_w - w - margin, "y": margin if fmt == "long" else int(frame_h * 0.04), "w": w, "h": h}


# ------------------------------------------------------------------ procedural art

def _rng(seed_text: str) -> random.Random:
    return random.Random(int(hashlib.sha256(seed_text.encode()).hexdigest()[:12], 16))


def gradient(w: int, h: int, c1: tuple, c2: tuple, angle: float) -> Image.Image:
    import numpy as np
    a = math.radians(angle)
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    t = xs * math.cos(a) + ys * math.sin(a)
    t = (t - t.min()) / max(1e-6, float(t.max() - t.min()))
    rgb = (np.array(c1, np.float32)[None, None, :] * (1 - t[..., None]) + np.array(c2, np.float32)[None, None, :] * t[..., None])
    return Image.fromarray(rgb.clip(0, 255).astype(np.uint8), "RGB")


def procedural_background(w: int, h: int, seed: str) -> Image.Image:
    """Original abstract artwork in the channel palette (no third-party content)."""
    rng = _rng(seed)
    pal = [hex_rgb(c) for c in settings.get("brand").get("palette", ["#14213D", "#2B1B4E"])]
    c1, c2 = rng.sample(pal, 2) if len(pal) >= 2 else (pal[0], (0, 0, 0))
    img = gradient(w, h, c1, tuple(max(0, int(c * 0.45)) for c in c2), rng.uniform(0, 360)).convert("RGBA")
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    accent = [hex_rgb(settings.get("brand").get(k, "#FFFFFF")) for k in ("primary", "accent")]
    for _ in range(rng.randint(5, 9)):
        r = rng.randint(min(w, h) // 10, min(w, h) // 3)
        x, y = rng.randint(-r, w + r), rng.randint(-r, h + r)
        col = rng.choice(accent + pal)
        gd.ellipse((x - r, y - r, x + r, y + r), fill=col + (rng.randint(35, 80),))
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(min(w, h) // 14)))
    lines = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lines)
    for _ in range(rng.randint(2, 4)):
        r = rng.randint(min(w, h) // 4, int(max(w, h) * 0.7))
        cx, cy = rng.randint(0, w), rng.randint(0, h)
        ld.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(255, 255, 255, 28), width=max(2, w // 300))
    img = Image.alpha_composite(img, lines)
    noise = Image.effect_noise((w // 2, h // 2), 18).resize((w, h)).convert("L")
    img = Image.blend(img.convert("RGB"), Image.merge("RGB", (noise, noise, noise)), 0.05)
    return img


def text_card(text: str, w: int, h: int, seed: str, kicker: str = "") -> Image.Image:
    img = procedural_background(w, h, seed).convert("RGBA")
    brand = settings.get("brand")
    d = ImageDraw.Draw(img)
    max_w = int(w * 0.8)
    fnt, lines, lh = fit_text(text, "display", max_w, int(h * 0.45), int(min(w, h) * 0.13), 28, max_lines=4)
    block_h = lh * len(lines)
    top = (h - block_h) // 2
    bar_w = int(w * 0.18)
    d.rectangle(((w - bar_w) // 2, top - 40, (w + bar_w) // 2, top - 28), fill=hex_rgb(brand.get("accent", "#FF4D6D")))
    draw_lines(d, lines, fnt, lh, w // 2, top, hex_rgb(brand.get("primary", "#FFD23F")),
               stroke=max(2, fnt.size // 18), stroke_fill=(0, 0, 0))
    if kicker:
        kf, kl, klh = fit_text(kicker, "body", max_w, int(h * 0.12), int(min(w, h) * 0.045), 18, max_lines=2)
        draw_lines(d, kl, kf, klh, w // 2, top + block_h + 30, (255, 255, 255))
    return img.convert("RGB")


def cover(img: Image.Image, w: int, h: int, focus_y: float = 0.45) -> Image.Image:
    """Scale-and-crop to fill w x h."""
    scale = max(w / img.width, h / img.height)
    nw, nh = max(w, math.ceil(img.width * scale)), max(h, math.ceil(img.height * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - w) // 2
    top = int(min(max(0, (nh - h) * focus_y), nh - h))
    return img.crop((left, top, left + w, top + h))


def save_jpeg_under(img: Image.Image, out: Path, max_bytes: int, quality: int = 92) -> int:
    img = img.convert("RGB")
    while True:
        img.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
        size = out.stat().st_size
        if size <= max_bytes or quality <= 50:
            return size
        quality -= 6


def describe_palette() -> dict[str, Any]:
    return settings.get("brand")
