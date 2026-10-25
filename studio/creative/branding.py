"""Channel art drawn from a channel's own cast: YouTube banner (2560×1440, title inside the 1546×423 area
every device shows) and profile picture (800×800, safe inside the round crop). Original artwork only.
"""
from __future__ import annotations

import math
import random
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter

from .. import channels, config
from ..media import graphics
from . import cartoon, characters

STATE = {"t": 0.0, "amp": 0.0, "talking": False, "emotion": "happy", "blink": False, "look": 0.0}
PALETTES = {
    "kids": ((88, 196, 255), (255, 233, 120), (120, 210, 90)),      # sky blue → sunshine, green hills
    "default": ((40, 52, 110), (240, 120, 80), (30, 40, 70)),
}


def _gradient(w: int, h: int, top: tuple, bottom: tuple) -> Image.Image:
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        d.line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    return img


def _decor(img: Image.Image, kids: bool, seed: int) -> None:
    w, h = img.size
    rnd = random.Random(seed)
    d = ImageDraw.Draw(img, "RGBA")
    if kids:
        for _ in range(7):                                # puffy clouds
            cx, cy, s = rnd.uniform(0, w), rnd.uniform(0, h * 0.45), rnd.uniform(0.6, 1.3) * h * 0.06
            for dx, dy, r in ((0, 0, 1.0), (-1.1, 0.25, 0.75), (1.1, 0.25, 0.8), (0.5, -0.45, 0.7)):
                d.ellipse([cx + dx * s - r * s, cy + dy * s - r * s, cx + dx * s + r * s, cy + dy * s + r * s],
                          fill=(255, 255, 255, 215))
        colours = [(255, 92, 120), (255, 196, 40), (90, 200, 120), (120, 120, 255), (255, 140, 40)]
        for _ in range(60):                               # confetti and stars
            x, y, r = rnd.uniform(0, w), rnd.uniform(0, h), rnd.uniform(0.004, 0.011) * h
            c = rnd.choice(colours) + (230,)
            if rnd.random() < 0.4:
                pts = [(x + r * 2 * math.cos(a * math.pi / 5 - math.pi / 2) * (1 if a % 2 == 0 else 0.45),
                        y + r * 2 * math.sin(a * math.pi / 5 - math.pi / 2) * (1 if a % 2 == 0 else 0.45))
                       for a in range(10)]
                d.polygon(pts, fill=c)
            else:
                d.ellipse([x - r, y - r, x + r, y + r], fill=c)
        d.ellipse([-w * 0.2, h * 0.78, w * 0.6, h * 1.4], fill=(110, 200, 85, 255))     # hills
        d.ellipse([w * 0.35, h * 0.82, w * 1.25, h * 1.45], fill=(90, 185, 75, 255))
    else:
        for _ in range(40):
            x, y, r = rnd.uniform(0, w), rnd.uniform(0, h), rnd.uniform(0.002, 0.006) * h
            d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, 120))


def _cast(channel_id: int, limit: int) -> list[dict[str, Any]]:
    """Faces that sell the channel: children first (newest first), then the hero, then family; rivals last."""
    def rank(c: dict[str, Any]) -> tuple:
        role = (c.get("role") or "").lower()
        rival = any(w in role for w in ("villain", "collector", "rival", "antagonist", "enemy", "shadow"))
        return (rival, c["design"]["age"] != "child", "hero" not in role and c["design"]["accessory"] != "cape_mask",
                -c["id"])
    return sorted(characters.for_channel(channel_id, include_draft=False), key=rank)[:limit]


def _title(d: ImageDraw.ImageDraw, text: str, cx: float, cy: float, max_w: int, size: int, colour, outline) -> None:
    fnt = graphics.font_for(text, "heavy", size)
    while d.textlength(text, font=fnt) > max_w and size > 30:
        size -= 4
        fnt = graphics.font_for(text, "heavy", size)
    d.text((cx, cy), text, font=fnt, fill=colour, anchor="mm", stroke_width=max(4, size // 10), stroke_fill=outline)


def banner(channel_id: int, out: Path, tagline: str = "") -> Path:
    ch = channels.get(channel_id)
    kids = bool(ch.get("made_for_kids")) or "kids" in (ch.get("styles") or [])
    top, bottom, _ = PALETTES["kids" if kids else "default"]
    w, h = 2560, 1440
    img = _gradient(w, h, top, bottom)
    _decor(img, kids, channel_id)
    safe_l, safe_t, safe_w, safe_h = (w - 1546) // 2, (h - 423) // 2, 1546, 423
    cast = _cast(channel_id, 4)
    # cast on the right of the safe area (heroes in the middle), title on the left — all inside 1546×423
    if cast:
        order = cast[1::2][::-1] + cast[0::2]            # most important characters end up in the centre
        xs = [safe_l + safe_w * (0.66 + 0.30 * i / max(1, len(order) - 1)) if len(order) > 1 else safe_l + safe_w * 0.8
              for i in range(len(order))]
        for c, x in zip(order, xs):
            scale = 0.82 if c["design"]["age"] == "child" else 1.05
            cartoon.draw_character(img, c["design"], x, safe_t + safe_h + 40, safe_h * scale, STATE)
    d = ImageDraw.Draw(img)
    text_cx = safe_l + safe_w * (0.29 if cast else 0.5)
    _title(d, ch["name"], text_cx, safe_t + safe_h * 0.40, int(safe_w * (0.54 if cast else 0.9)), 150,
           (255, 255, 255), (40, 40, 90))
    tag = tagline or ("हँसो • सीखो • मस्ती करो!" if kids and ch.get("language") in ("Hindi", "Hinglish") else
                      "New funny episodes every week!" if kids else (ch.get("niche") or "")[:60])
    _title(d, tag, text_cx, safe_t + safe_h * 0.78, int(safe_w * (0.54 if cast else 0.9)), 64,
           (255, 230, 80), (40, 40, 90))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "JPEG", quality=92)
    return out


def avatar(channel_id: int, out: Path) -> Path:
    ch = channels.get(channel_id)
    kids = bool(ch.get("made_for_kids")) or "kids" in (ch.get("styles") or [])
    size = 800
    img = _gradient(size, size, (255, 214, 64) if kids else (60, 70, 140), (255, 120, 150) if kids else (20, 25, 60))
    glow = Image.new("L", (size, size), 0)
    ImageDraw.Draw(glow).ellipse([120, 90, 680, 650], fill=170)
    img = Image.composite(Image.new("RGB", (size, size), (255, 250, 230)), img, glow.filter(ImageFilter.GaussianBlur(70)))
    cast = _cast(channel_id, 3)
    hero = next((c for c in cast if c["design"]["age"] != "child"), None)
    kid = next((c for c in cast if c["design"]["age"] == "child"), None)
    pair = [x for x in (hero, kid) if x] or cast[:1]
    # big faces inside the circle a YouTube avatar is cropped to; the child in front
    for i, c in enumerate(pair):
        child = c["design"]["age"] == "child"
        x = size * (0.5 if len(pair) == 1 else (0.36 if i == 0 else 0.64))
        cartoon.draw_character(img, c["design"], x, size * (1.18 if child else 1.25), size * (0.98 if child else 1.15),
                               STATE)
    if not cast:
        d = ImageDraw.Draw(img)
        _title(d, "".join(w[0] for w in ch["name"].split()[:2]).upper(), size / 2, size / 2, 600, 300,
               (255, 255, 255), (40, 40, 90))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG")
    return out


def make_all(channel_id: int) -> dict[str, Path]:
    folder = config.DATA_DIR / "channels" / f"{channel_id:04d}" / "branding"
    return {"banner": banner(channel_id, folder / "banner_2560x1440.jpg"),
            "avatar": avatar(channel_id, folder / "profile_800x800.png")}
