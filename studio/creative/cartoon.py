"""2D cartoon renderer: original flat-style animation drawn frame by frame (Pillow) and encoded with FFmpeg.

Characters are drawn from their character-bible design parameters, so the same character looks the
same in every scene and episode. Mouths move with the actual dialogue audio (per-frame loudness),
characters blink, gesture and react with expressions; the camera eases toward the speaker; subtitles
show who is speaking. Everything is drawn by this program — no third-party footage or artwork.
"""
from __future__ import annotations

import hashlib
import math
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .. import config
from ..media import graphics
from .characters import COLOURS, HAIR_COLOUR, SKIN, normalise_design

FPS = 24
SIZES = {"long": (1280, 720), "short": (720, 1280)}           # drawn size; encoded at 1080p
OUT = {"long": (1920, 1080), "short": (1080, 1920)}
INK = (28, 24, 30)
LOCATIONS = ["home", "kitchen", "office", "school", "shop", "bank", "hospital", "street", "market", "park", "rooftop",
             "city", "forest", "beach", "temple", "graveyard", "haunted_house", "train", "space", "village"]


def _shade(c: tuple[int, int, int], f: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(v * f))) for v in c)


def _mix(a, b, t: float):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _seed(*parts: Any) -> float:
    return int(hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


# ------------------------------------------------------------------ backgrounds

def _sky(d: ImageDraw.ImageDraw, w: int, h: int, top_h: int, time: str) -> None:
    top, bottom = {"day": ((120, 190, 245), (215, 238, 252)), "evening": ((240, 140, 80), (120, 80, 140)),
                   "night": ((12, 18, 48), (40, 40, 80))}.get(time, ((120, 190, 245), (215, 238, 252)))
    for y in range(top_h):
        d.line([(0, y), (w, y)], fill=_mix(top, bottom, y / max(1, top_h)))
    if time == "night":
        for i in range(60):
            sx, sy = _seed("star", i, w) * w, _seed("stary", i, h) * top_h * 0.8
            d.ellipse([sx, sy, sx + 2, sy + 2], fill=(240, 240, 220))
        d.ellipse([w * 0.78, h * 0.06, w * 0.78 + h * 0.1, h * 0.16], fill=(245, 240, 210))
    elif time == "day":
        d.ellipse([w * 0.82, h * 0.05, w * 0.82 + h * 0.11, h * 0.16], fill=(255, 230, 120))


def _buildings(d, w, h, base_y, time, seed):
    x = 0
    i = 0
    while x < w:
        bw = int(w * (0.08 + 0.07 * _seed(seed, "bw", i)))
        bh = int(h * (0.18 + 0.3 * _seed(seed, "bh", i)))
        col = _shade((150, 140, 170), 0.6 + 0.5 * _seed(seed, "c", i)) if time != "night" else (35, 35, 60)
        d.rectangle([x, base_y - bh, x + bw, base_y], fill=col)
        for wy in range(base_y - bh + 12, base_y - 18, 26):
            for wx in range(x + 8, x + bw - 14, 22):
                lit = time == "night" and _seed(seed, wx, wy) > 0.45
                d.rectangle([wx, wy, wx + 10, wy + 14], fill=(250, 220, 120) if lit else _shade(col, 0.75))
        x += bw + 4
        i += 1


def _tree(d, x, base_y, s, time):
    trunk = (110, 75, 45) if time != "night" else (50, 35, 30)
    leaf = (60, 160, 80) if time != "night" else (25, 60, 40)
    d.rectangle([x - s * 0.08, base_y - s * 0.6, x + s * 0.08, base_y], fill=trunk)
    for dx, dy, r in ((0, -0.8, 0.38), (-0.25, -0.62, 0.28), (0.25, -0.62, 0.28)):
        d.ellipse([x + dx * s - r * s, base_y + dy * s - r * s, x + dx * s + r * s, base_y + dy * s + r * s], fill=leaf)


def _indoor(d, w, h, floor_y, wall, floor):
    d.rectangle([0, 0, w, floor_y], fill=wall)
    d.rectangle([0, floor_y, w, h], fill=floor)
    d.line([(0, floor_y), (w, floor_y)], fill=_shade(wall, 0.7), width=6)
    for x in range(0, w, 90):                                  # floor boards
        d.line([(x, floor_y), (x - 60, h)], fill=_shade(floor, 0.9), width=2)


def _window(d, x, y, ww, wh, time):
    col = {"day": (170, 215, 250), "evening": (240, 160, 110), "night": (25, 30, 70)}.get(time, (170, 215, 250))
    d.rectangle([x, y, x + ww, y + wh], fill=col, outline=(240, 235, 225), width=8)
    d.line([(x + ww / 2, y), (x + ww / 2, y + wh)], fill=(240, 235, 225), width=6)
    d.line([(x, y + wh / 2), (x + ww, y + wh / 2)], fill=(240, 235, 225), width=6)


def background(location: str, time: str, mood: str, w: int, h: int) -> Image.Image:
    location = location if location in LOCATIONS else "home"
    time = time if time in ("day", "evening", "night") else "day"
    img = Image.new("RGB", (w, h), (200, 200, 200))
    d = ImageDraw.Draw(img)
    portrait = h > w
    floor_y = int(h * (0.62 if portrait else 0.7))
    indoor = location in ("home", "kitchen", "office", "school", "shop", "bank", "hospital", "train")
    if indoor:
        walls = {"home": ((238, 214, 180), (150, 105, 70)), "kitchen": ((210, 235, 220), (190, 170, 150)),
                 "office": ((215, 222, 232), (120, 125, 140)), "school": ((240, 230, 190), (160, 120, 80)),
                 "shop": ((250, 220, 200), (170, 140, 110)), "bank": ((225, 230, 210), (140, 140, 150)),
                 "hospital": ((225, 242, 245), (190, 200, 205)), "train": ((200, 210, 190), (120, 110, 100))}
        wall, floor = walls[location]
        _indoor(d, w, h, floor_y, wall, floor)
        _window(d, w * 0.08, h * 0.12, w * 0.22, h * 0.28 if not portrait else h * 0.16, time)
        if location == "home":
            d.rounded_rectangle([w * 0.55, floor_y - h * 0.14, w * 0.95, floor_y + 4], radius=18, fill=(170, 60, 60))
            d.rounded_rectangle([w * 0.55, floor_y - h * 0.2, w * 0.95, floor_y - h * 0.12], radius=18, fill=(190, 75, 75))
            d.rectangle([w * 0.40, h * 0.12, w * 0.50, h * 0.26], fill=(120, 90, 60), outline=(240, 220, 160), width=6)
        elif location == "kitchen":
            d.rectangle([w * 0.5, floor_y - h * 0.18, w, floor_y], fill=(160, 120, 90))
            d.rectangle([w * 0.5, floor_y - h * 0.2, w, floor_y - h * 0.18], fill=(90, 90, 95))
            d.ellipse([w * 0.62, floor_y - h * 0.26, w * 0.70, floor_y - h * 0.2], fill=(60, 60, 65))
        elif location == "office":
            d.rectangle([w * 0.55, floor_y - h * 0.15, w * 0.95, floor_y - h * 0.12], fill=(120, 85, 55))
            d.rectangle([w * 0.68, floor_y - h * 0.28, w * 0.84, floor_y - h * 0.16], fill=(40, 45, 55))
            d.rectangle([w * 0.69, floor_y - h * 0.27, w * 0.83, floor_y - h * 0.17], fill=(90, 160, 220))
        elif location == "school":
            d.rectangle([w * 0.35, h * 0.1, w * 0.85, h * 0.36], fill=(40, 80, 60), outline=(150, 100, 60), width=10)
        elif location in ("shop", "bank"):
            d.rectangle([w * 0.5, floor_y - h * 0.16, w * 0.98, floor_y], fill=(130, 90, 60) if location == "shop" else (90, 95, 110))
            for i in range(4):
                d.rectangle([w * (0.55 + i * 0.1), h * 0.14, w * (0.62 + i * 0.1), h * 0.3], fill=COLOURS[["red", "yellow", "green", "blue"][i]])
        elif location == "hospital":
            d.rectangle([w * 0.55, floor_y - h * 0.12, w * 0.95, floor_y], fill=(240, 240, 245), outline=(150, 160, 170), width=4)
            d.rectangle([w * 0.42, h * 0.14, w * 0.48, h * 0.24], fill=(220, 40, 40))
        elif location == "train":
            for i in range(3):
                _window(d, w * (0.1 + i * 0.3), h * 0.16, w * 0.2, h * 0.2, time)
    else:
        _sky(d, w, h, floor_y, time)
        ground = {"street": (95, 95, 100), "market": (170, 140, 100), "park": (110, 180, 90), "rooftop": (130, 125, 120),
                  "city": (90, 90, 95), "forest": (70, 120, 60), "beach": (235, 215, 160), "temple": (200, 180, 140),
                  "graveyard": (70, 85, 60), "haunted_house": (60, 70, 55), "space": (60, 60, 70),
                  "village": (190, 160, 110)}[location]
        if time == "night":
            ground = _shade(ground, 0.45)
        if location in ("street", "city", "rooftop", "market"):
            _buildings(d, w, h, floor_y, time, location)
        if location == "beach":
            d.rectangle([0, floor_y - h * 0.08, w, floor_y], fill=(60, 140, 200) if time != "night" else (20, 40, 80))
        if location in ("park", "forest", "village", "graveyard", "haunted_house"):
            for i in range(9 if location == "forest" else 4):
                _tree(d, w * (0.05 + 0.12 * i + 0.05 * _seed(location, i)), floor_y, h * (0.35 + 0.15 * _seed("t", i)), time)
        d.rectangle([0, floor_y, w, h], fill=ground)
        if location in ("street", "city"):
            d.rectangle([0, floor_y + h * 0.05, w, floor_y + h * 0.13], fill=_shade(ground, 0.8))
            for x in range(0, w, 120):
                d.rectangle([x + 20, floor_y + h * 0.085, x + 80, floor_y + h * 0.095], fill=(240, 220, 120))
        if location == "market":
            for i in range(4):
                x = w * (0.04 + 0.25 * i)
                d.rectangle([x, floor_y - h * 0.16, x + w * 0.2, floor_y], fill=(150, 110, 70))
                d.polygon([(x - 10, floor_y - h * 0.16), (x + w * 0.21, floor_y - h * 0.16), (x + w * 0.1, floor_y - h * 0.24)],
                          fill=COLOURS[["orange", "red", "teal", "yellow"][i]])
        if location == "temple":
            d.rectangle([w * 0.55, floor_y - h * 0.3, w * 0.85, floor_y], fill=(240, 220, 170))
            d.polygon([(w * 0.53, floor_y - h * 0.3), (w * 0.87, floor_y - h * 0.3), (w * 0.7, floor_y - h * 0.52)], fill=(230, 150, 60))
        if location == "graveyard":
            for i in range(6):
                x = w * (0.1 + 0.15 * i)
                d.rounded_rectangle([x, floor_y - h * 0.09, x + w * 0.05, floor_y + 6], radius=12, fill=(120, 120, 125))
        if location == "haunted_house":
            d.polygon([(w * 0.5, floor_y), (w * 0.52, floor_y - h * 0.38), (w * 0.88, floor_y - h * 0.34), (w * 0.9, floor_y)], fill=(40, 35, 50))
            d.polygon([(w * 0.48, floor_y - h * 0.36), (w * 0.71, floor_y - h * 0.58), (w * 0.92, floor_y - h * 0.34)], fill=(30, 25, 40))
            for x, y in ((0.58, 0.28), (0.78, 0.26)):
                d.rectangle([w * x, floor_y - h * y, w * x + w * 0.05, floor_y - h * y + h * 0.07], fill=(230, 200, 90))
        if location == "space":
            d.rectangle([0, 0, w, floor_y], fill=(8, 8, 25))
            for i in range(120):
                sx, sy = _seed("sp", i) * w, _seed("spy", i) * floor_y
                d.ellipse([sx, sy, sx + 2, sy + 2], fill=(255, 255, 255))
            d.ellipse([w * 0.1, h * 0.1, w * 0.1 + h * 0.18, h * 0.28], fill=(90, 150, 220))
    if mood in ("scary", "tense") or location in ("graveyard", "haunted_house"):
        img = Image.blend(img, Image.new("RGB", (w, h), (10, 15, 40)), 0.35)
        fog = Image.new("L", (w, h), 0)
        fd = ImageDraw.Draw(fog)
        for i in range(10):
            fx, fy = _seed("fog", i) * w, floor_y - h * 0.05 + _seed("fogy", i) * h * 0.12
            fd.ellipse([fx - w * 0.2, fy - h * 0.05, fx + w * 0.2, fy + h * 0.05], fill=90)
        fog = fog.filter(ImageFilter.GaussianBlur(30))
        img = Image.composite(Image.new("RGB", (w, h), (190, 195, 210)), img, fog)
    elif mood == "sad":
        img = Image.blend(img, Image.new("RGB", (w, h), (70, 80, 110)), 0.2)
    return img


# ------------------------------------------------------------------ characters

def draw_character(img: Image.Image, design: dict[str, Any], x: float, ground: float, height: float,
                   state: dict[str, Any]) -> None:
    """Draw one character. state: t, amp (0-1 mouth from audio), emotion, talking, blink, look (-1..1)."""
    dsg = normalise_design(design)
    d = ImageDraw.Draw(img)
    t = state.get("t", 0.0)
    emo = state.get("emotion", "neutral")
    talking = state.get("talking", False)
    amp = state.get("amp", 0.0)
    age_f = {"child": 0.74, "young": 0.95, "adult": 1.0, "old": 0.96}[dsg["age"]]
    tall_f = 1.1 if dsg["body"] == "tall" else 1.0
    H = height * age_f * tall_f
    U = H / 10.0
    # motion
    dy = math.sin(t * 2.2 + x) * 0.05 * U
    dx = 0.0
    if talking:
        dy += math.sin(t * 9) * 0.1 * U
    if emo == "laugh":
        dy -= abs(math.sin(t * 11)) * 0.35 * U
    if emo == "scared":
        dx = math.sin(t * 45) * 0.12 * U
    if emo == "shout" and talking:
        dx = math.sin(t * 30) * 0.08 * U
    x += dx
    g = ground + dy
    head_r = U * (1.75 if dsg["age"] == "child" else 1.5)
    leg_l = U * (2.6 if dsg["age"] == "child" else 3.2)
    torso_h = U * (2.6 if dsg["age"] == "child" else 3.3)
    tw = U * {"thin": 2.2, "average": 2.8, "round": 3.7, "tall": 2.5}[dsg["body"]]
    hip = g - leg_l
    sh = hip - torso_h
    head_cy = sh - U * 0.15 - head_r
    skin = SKIN[dsg["skin"]]
    out1 = COLOURS[dsg["outfit"]]
    out2 = COLOURS[dsg["outfit2"]]
    hair_c = HAIR_COLOUR["grey" if dsg["age"] == "old" and dsg["hair_colour"] == "black" else dsg["hair_colour"]]
    female = dsg["gender"] == "female"
    acc = dsg["accessory"]
    lw = max(2, int(U * 0.12))
    # cape (behind)
    if acc in ("cape", "cape_mask"):
        wave = math.sin(t * 3) * 0.3 * U
        d.polygon([(x - tw * 0.45, sh + U * 0.2), (x + tw * 0.45, sh + U * 0.2), (x + tw * 0.85 + wave, g - U * 0.6),
                   (x - tw * 0.85 + wave, g - U * 0.6)], fill=COLOURS["red"], outline=INK)
    # long hair behind head
    if dsg["hair"] in ("long", "ponytail") or (female and dsg["hair"] == "long"):
        d.rounded_rectangle([x - head_r * 1.05, head_cy - head_r * 0.2, x + head_r * 1.05, head_cy + head_r * 1.6],
                            radius=int(head_r * 0.6), fill=hair_c)
    # legs and shoes
    leg_w = U * 0.8
    for side in (-1, 1):
        lx = x + side * tw * 0.22
        d.rounded_rectangle([lx - leg_w / 2, hip - U * 0.2, lx + leg_w / 2, g - U * 0.1], radius=int(leg_w / 2),
                            fill=_shade(out2, 0.8), outline=INK, width=lw)
        d.ellipse([lx - leg_w * 0.8 + side * U * 0.15, g - U * 0.45, lx + leg_w * 0.8 + side * U * 0.15, g + U * 0.1],
                  fill=INK)
    # torso (dress flares for female characters)
    if female:
        d.polygon([(x - tw * 0.42, sh), (x + tw * 0.42, sh), (x + tw * 0.75, hip + U * 0.9), (x - tw * 0.75, hip + U * 0.9)],
                  fill=out1, outline=INK)
        d.rectangle([x - tw * 0.6, hip + U * 0.55, x + tw * 0.6, hip + U * 0.75], fill=out2)
    else:
        d.rounded_rectangle([x - tw / 2, sh, x + tw / 2, hip + U * 0.2], radius=int(U * 0.8 if dsg["body"] == "round" else U * 0.45),
                            fill=out1, outline=INK, width=lw)
        d.rectangle([x - tw / 2 + lw, hip - U * 0.25, x + tw / 2 - lw, hip + U * 0.05], fill=out2)   # belt
    if acc == "tie":
        d.polygon([(x - U * 0.2, sh + U * 0.1), (x + U * 0.2, sh + U * 0.1), (x + U * 0.28, sh + U * 1.8), (x, sh + U * 2.2),
                   (x - U * 0.28, sh + U * 1.8)], fill=out2, outline=INK)
    if acc in ("cape", "cape_mask"):                     # chest emblem
        d.ellipse([x - U * 0.55, sh + U * 0.5, x + U * 0.55, sh + U * 1.5], fill=COLOURS["yellow"], outline=INK, width=lw)
        d.polygon([(x - U * 0.25, sh + U * 0.75), (x + U * 0.3, sh + U * 0.75), (x - U * 0.1, sh + U * 1.05),
                   (x + U * 0.25, sh + U * 1.05), (x - U * 0.3, sh + U * 1.35)], fill=COLOURS["red"])
    # arms: angle measured from straight down (0), outward positive, straight up = pi
    arm_len = U * 2.6
    for side in (-1, 1):
        sx, sy = x + side * (tw / 2 - U * 0.15), sh + U * 0.35
        length = arm_len
        if emo in ("scared", "surprised"):
            a = 2.6
        elif emo == "angry":
            a, length = 0.75, arm_len * 0.8                   # hands on hips
        elif emo == "laugh":
            a = -0.5                                          # holding the belly
        elif talking and side == 1:
            a = 1.25 + math.sin(t * 5) * 0.45                 # gesturing while speaking
        else:
            a = 0.12 + math.sin(t * 1.7 + side) * 0.04
        hx, hy = sx + side * math.sin(a) * length, sy + math.cos(a) * length
        d.line([(sx, sy), (hx, hy)], fill=out1, width=int(U * 0.62))
        d.ellipse([hx - U * 0.35, hy - U * 0.35, hx + U * 0.35, hy + U * 0.35], fill=skin, outline=INK, width=lw)
    # neck, head, ears
    d.rectangle([x - U * 0.35, sh - U * 0.35, x + U * 0.35, sh + U * 0.1], fill=_shade(skin, 0.92))
    for side in (-1, 1):
        d.ellipse([x + side * head_r * 0.95 - U * 0.3, head_cy - U * 0.25, x + side * head_r * 0.95 + U * 0.3,
                   head_cy + U * 0.35], fill=_shade(skin, 0.95), outline=INK, width=lw)
    face = skin if emo != "angry" else _mix(skin, (230, 90, 80), 0.25)
    d.ellipse([x - head_r, head_cy - head_r, x + head_r, head_cy + head_r], fill=face, outline=INK, width=lw)
    # hair (front)
    hs = dsg["hair"]
    if hs in ("short", "long", "ponytail", "bun", "curly", "spiky"):
        d.pieslice([x - head_r * 1.02, head_cy - head_r * 1.05, x + head_r * 1.02, head_cy + head_r * 0.6], 180, 360, fill=hair_c)
    if hs == "spiky":
        for i in range(5):
            bx = x - head_r * 0.8 + i * head_r * 0.4
            d.polygon([(bx - head_r * 0.2, head_cy - head_r * 0.7), (bx + head_r * 0.2, head_cy - head_r * 0.7),
                       (bx, head_cy - head_r * 1.35)], fill=hair_c)
    if hs == "curly":
        for i in range(7):
            a = math.pi + i * math.pi / 6
            cx, cy = x + math.cos(a) * head_r * 0.95, head_cy + math.sin(a) * head_r * 0.95
            d.ellipse([cx - head_r * 0.28, cy - head_r * 0.28, cx + head_r * 0.28, cy + head_r * 0.28], fill=hair_c)
    if hs == "bun":
        d.ellipse([x - head_r * 0.35, head_cy - head_r * 1.45, x + head_r * 0.35, head_cy - head_r * 0.8], fill=hair_c)
    if hs == "ponytail":
        d.ellipse([x + head_r * 0.7, head_cy - head_r * 0.2, x + head_r * 1.4, head_cy + head_r * 1.1], fill=hair_c)
    if hs == "bald" and dsg["age"] == "old":
        for side in (-1, 1):
            d.ellipse([x + side * head_r * 0.7 - head_r * 0.25, head_cy - head_r * 0.3, x + side * head_r * 0.7 + head_r * 0.25,
                       head_cy + head_r * 0.2], fill=hair_c)
    # headwear
    if acc == "turban":
        d.pieslice([x - head_r * 1.1, head_cy - head_r * 1.35, x + head_r * 1.1, head_cy + head_r * 0.35], 180, 360, fill=out2, outline=INK)
        d.ellipse([x - head_r * 0.18, head_cy - head_r * 0.85, x + head_r * 0.18, head_cy - head_r * 0.5], fill=COLOURS["gold"])
    elif acc == "cap":
        d.pieslice([x - head_r * 1.0, head_cy - head_r * 1.15, x + head_r * 1.0, head_cy + head_r * 0.45], 180, 360, fill=out2, outline=INK)
        d.rounded_rectangle([x - head_r * 0.2, head_cy - head_r * 0.42, x + head_r * 1.5, head_cy - head_r * 0.25], radius=6, fill=out2)
    elif acc == "hat":
        d.rectangle([x - head_r * 1.5, head_cy - head_r * 0.72, x + head_r * 1.5, head_cy - head_r * 0.55], fill=out2)
        d.rectangle([x - head_r * 0.8, head_cy - head_r * 1.4, x + head_r * 0.8, head_cy - head_r * 0.7], fill=out2)
    # eyes / mask
    ey = head_cy - head_r * 0.12
    ex = head_r * 0.38
    if acc in ("mask", "cape_mask"):
        d.rounded_rectangle([x - head_r * 0.85, ey - head_r * 0.28, x + head_r * 0.85, ey + head_r * 0.28],
                            radius=int(head_r * 0.25), fill=COLOURS["red"] if acc == "cape_mask" else INK)
    look = state.get("look", 0.0)
    blink = state.get("blink", False)
    eye_h = head_r * (0.42 if emo in ("surprised", "scared", "shout") else 0.32)
    for side in (-1, 1):
        cx = x + side * ex
        if blink:
            d.line([(cx - head_r * 0.13, ey), (cx + head_r * 0.13, ey)], fill=INK, width=lw)
        elif emo == "laugh":
            d.arc([cx - head_r * 0.15, ey - head_r * 0.12, cx + head_r * 0.15, ey + head_r * 0.12], 180, 360, fill=INK, width=lw)
        else:
            d.ellipse([cx - head_r * 0.14, ey - eye_h / 2, cx + head_r * 0.14, ey + eye_h / 2], fill=(255, 255, 255), outline=INK, width=max(1, lw // 2))
            px = cx + look * head_r * 0.05
            d.ellipse([px - head_r * 0.07, ey - head_r * 0.06, px + head_r * 0.07, ey + head_r * 0.1], fill=INK)
        # eyebrows by emotion
        by = ey - eye_h / 2 - head_r * 0.12
        inner, outer = {"angry": (0.1, -0.06), "sad": (-0.1, 0.04), "surprised": (-0.1, -0.1), "scared": (-0.12, 0.0),
                        "shout": (0.08, -0.05)}.get(emo, (0.0, 0.0))
        d.line([(cx - side * head_r * 0.03, by + inner * head_r), (cx + side * head_r * 0.2, by + outer * head_r)],
               fill=hair_c if hs != "bald" else INK, width=int(lw * 1.4))
    if acc == "glasses":
        for side in (-1, 1):
            d.ellipse([x + side * ex - head_r * 0.22, ey - head_r * 0.2, x + side * ex + head_r * 0.22, ey + head_r * 0.2],
                      outline=INK, width=lw)
        d.line([(x - ex + head_r * 0.22, ey), (x + ex - head_r * 0.22, ey)], fill=INK, width=lw)
    # nose
    d.arc([x - head_r * 0.1, head_cy + head_r * 0.0, x + head_r * 0.1, head_cy + head_r * 0.22], 0, 180, fill=_shade(skin, 0.6), width=lw)
    # mustache / beard
    my = head_cy + head_r * 0.42
    if acc == "beard":
        d.chord([x - head_r * 0.7, head_cy + head_r * 0.1, x + head_r * 0.7, head_cy + head_r * 1.1], 0, 180, fill=hair_c)
    if acc in ("mustache", "beard"):
        d.ellipse([x - head_r * 0.42, my - head_r * 0.2, x - head_r * 0.02, my + head_r * 0.02], fill=hair_c)
        d.ellipse([x + head_r * 0.02, my - head_r * 0.2, x + head_r * 0.42, my + head_r * 0.02], fill=hair_c)
    # mouth
    mw = head_r * 0.36
    mouth_y = my + head_r * 0.08
    open_h = head_r * (0.06 + 0.42 * amp) if talking else 0.0
    if emo == "shout" and talking:
        open_h *= 1.5
    if talking and open_h > head_r * 0.08:
        d.ellipse([x - mw * (1.2 if emo == "shout" else 1), mouth_y - open_h / 2, x + mw * (1.2 if emo == "shout" else 1),
                   mouth_y + open_h / 2], fill=(110, 30, 40), outline=INK, width=lw)
        if open_h > head_r * 0.2:
            d.ellipse([x - mw * 0.5, mouth_y, x + mw * 0.5, mouth_y + open_h / 2 - 2], fill=(220, 100, 110))
    elif emo == "laugh":
        d.chord([x - mw * 1.1, mouth_y - head_r * 0.12, x + mw * 1.1, mouth_y + head_r * 0.32], 0, 180, fill=(110, 30, 40), outline=INK)
    elif emo in ("happy",):
        d.arc([x - mw, mouth_y - head_r * 0.18, x + mw, mouth_y + head_r * 0.14], 20, 160, fill=INK, width=lw + 1)
    elif emo in ("sad",):
        d.arc([x - mw, mouth_y, x + mw, mouth_y + head_r * 0.3], 200, 340, fill=INK, width=lw + 1)
    elif emo in ("scared", "surprised"):
        d.ellipse([x - mw * 0.35, mouth_y - head_r * 0.08, x + mw * 0.35, mouth_y + head_r * 0.14], fill=(110, 30, 40), outline=INK)
    else:
        d.line([(x - mw * 0.7, mouth_y), (x + mw * 0.7, mouth_y)], fill=INK, width=lw + 1)
    if emo in ("happy", "laugh") or female:
        for side in (-1, 1):
            d.ellipse([x + side * head_r * 0.62 - head_r * 0.14, mouth_y - head_r * 0.2, x + side * head_r * 0.62 + head_r * 0.14,
                       mouth_y - head_r * 0.05], fill=(240, 150, 150))
    if acc == "bindi":
        d.ellipse([x - head_r * 0.06, ey - head_r * 0.42, x + head_r * 0.06, ey - head_r * 0.3], fill=COLOURS["red"])
    if dsg["age"] == "old":
        d.arc([x - head_r * 0.5, head_cy - head_r * 0.62, x + head_r * 0.5, head_cy - head_r * 0.42], 200, 340, fill=_shade(skin, 0.7), width=lw)


def reference_sheet(char: dict[str, Any], out: Path) -> Path:
    """Approved character reference: the exact drawing used in every episode, on a neutral background."""
    w, h = 900, 1100
    img = Image.new("RGB", (w, h), (238, 240, 244))
    draw_character(img, char["design"], w / 2, h * 0.86, h * 0.72, {"t": 0.0, "emotion": "happy"})
    d = ImageDraw.Draw(img)
    name = char["name"]
    d.text((w / 2, h * 0.04), name, font=graphics.font_for(name, "heavy", 64), fill=INK, anchor="ma")
    dsg = char["design"]
    spec = f"{dsg['gender']} · {dsg['age']} · {dsg['body']} · {dsg['outfit']}/{dsg['outfit2']} · {dsg['hair']} · {dsg['accessory']}"
    d.text((w / 2, h * 0.94), spec, font=graphics.font_for(spec, "body", 26), fill=(90, 95, 105), anchor="ma")
    img.save(out)
    return out


# ------------------------------------------------------------------ subtitles

_SUB_CACHE: dict[tuple, Image.Image] = {}


def _subtitle(name: str, text: str, w: int, h: int, colour: tuple[int, int, int], portrait: bool) -> Image.Image:
    colour = tuple(colour)[:3]                    # may arrive as a JSON list
    key = (name, text, w, h, portrait, colour)
    if key in _SUB_CACHE:
        return _SUB_CACHE[key]
    size = int(h * (0.032 if portrait else 0.048))
    max_w = int(w * 0.88)
    probe = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    while True:
        f = graphics.font_for(text, "heavy", size)
        lines = graphics.wrap(text, f, max_w, probe)
        if len(lines) <= 3 or size <= 16:
            break
        size -= 2
    nf = graphics.font_for(name, "heavy", int(size * 0.75))
    lh = int(size * 1.3)
    box_h = lh * len(lines) + int(size * 1.4)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    top = int(h * (0.70 if portrait else 0.97)) - (0 if portrait else box_h)
    d.rounded_rectangle([w * 0.04, top, w * 0.96, top + box_h], radius=18, fill=(0, 0, 0, 165))
    if name:
        d.text((w * 0.07, top + int(size * 0.25)), name, font=nf, fill=colour + (255,))
    y = top + int(size * 1.15)
    for ln in lines:
        d.text((w / 2, y), ln, font=f, fill=(255, 255, 255, 255), anchor="ma", stroke_width=2, stroke_fill=(0, 0, 0, 255))
        y += lh
    _SUB_CACHE[key] = layer
    if len(_SUB_CACHE) > 400:
        _SUB_CACHE.clear()
    return layer


# ------------------------------------------------------------------ audio envelope

def envelope(samples: np.ndarray, sr: int, frames: int) -> np.ndarray:
    """Per-frame mouth opening (0-1) from the dialogue audio."""
    if frames <= 0:
        return np.zeros(0)
    x = samples.astype(np.float32) / 32768.0
    per = max(1, int(sr / FPS))
    env = np.array([np.sqrt(np.mean(x[i * per:(i + 1) * per] ** 2)) if i * per < len(x) else 0.0 for i in range(frames)])
    peak = np.percentile(env[env > 0], 95) if np.any(env > 0) else 1.0
    env = np.clip(env / (peak or 1.0), 0, 1)
    return np.convolve(env, [0.25, 0.5, 0.25], mode="same")


# ------------------------------------------------------------------ encoding

class Encoder:
    def __init__(self, out: Path, fmt: str):
        self.w, self.h = SIZES[fmt]
        ow, oh = OUT[fmt]
        self.p = subprocess.Popen(
            [config.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{self.w}x{self.h}", "-r", str(FPS), "-i", "-", "-vf", f"scale={ow}:{oh}:flags=lanczos",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p", str(out)],
            stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        self.frames = 0

    def write(self, img: Image.Image) -> None:
        self.p.stdin.write(img.convert("RGB").tobytes())
        self.frames += 1

    def abort(self) -> None:
        """Stop FFmpeg after a drawing error so no half-written process is left behind."""
        try:
            self.p.stdin.close()
        except OSError:
            pass
        self.p.kill()
        self.p.wait()

    def close(self) -> None:
        self.p.stdin.close()
        err = self.p.stderr.read().decode(errors="ignore")
        if self.p.wait() != 0:
            raise RuntimeError(f"ffmpeg failed: {err[-400:]}")


def _camera(img: Image.Image, zoom: float, cx: float) -> Image.Image:
    if zoom <= 1.001:
        return img
    w, h = img.size
    cw, ch = w / zoom, h / zoom
    left = min(max(cx - cw / 2, 0), w - cw)
    top = (h - ch) * 0.55
    return img.crop((int(left), int(top), int(left + cw), int(top + ch))).resize((w, h), Image.BILINEAR)


def positions(n: int, w: int) -> list[float]:
    return [w * (i + 1) / (n + 1) for i in range(n)]


def _grade_mask(w: int, h: int, mood: str) -> tuple[Image.Image, Image.Image]:
    """Horror grade: cold dark colour blended in, strongest at the edges (vignette)."""
    strength = 0.78 if mood == "scary" else 0.55
    yy, xx = np.mgrid[0:h, 0:w]
    r = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2) / 1.414
    alpha = np.clip(strength * (0.6 + 0.8 * r ** 1.4), 0, 0.94) * 255
    return Image.new("RGB", (w, h), (6, 10, 28)), Image.fromarray(alpha.astype("uint8"), "L")


def render_scene(scene: dict[str, Any], cast: dict[str, dict[str, Any]], lines: list[dict[str, Any]], fmt: str,
                 enc: Encoder, total: float, *, hook: str = "") -> None:
    """Animate one scene. lines: [{speaker, text, emotion, start, end, env}] with times relative to the scene."""
    w, h = SIZES[fmt]
    portrait = fmt == "short"
    bg = background(scene.get("location", "home"), scene.get("time", "day"), scene.get("mood", "funny"), w, h)
    keys = [k for k in scene.get("characters", []) if k in cast]
    for ln in lines:
        if ln["speaker"] in cast and ln["speaker"] not in keys:
            keys.append(ln["speaker"])
    keys = keys[:4]
    xs = dict(zip(keys, positions(len(keys), w)))
    ground = h * (0.64 if portrait else 0.80)            # feet stay above the subtitle box
    char_h = h * (0.34 if portrait else 0.56) * (0.92 if len(keys) >= 4 else 1.0)
    frames = max(1, int(round(total * FPS)))
    hook_layer = _subtitle("", hook, w, h, (255, 214, 0), portrait) if hook else None
    grade = _grade_mask(w, h, scene.get("mood")) if scene.get("mood") in ("scary", "tense") else None
    for i in range(frames):
        t = i / FPS
        cur = next((ln for ln in lines if ln["start"] <= t < ln["end"] + 0.25), None)
        img = bg.copy()
        speaker = cur["speaker"] if cur and cur["start"] <= t <= cur["end"] else None
        for k in keys:
            talking = k == speaker
            amp = 0.0
            if talking:
                fi = int((t - cur["start"]) * FPS)
                env = cur.get("env")
                amp = float(env[fi]) if env is not None and 0 <= fi < len(env) else 0.0
            emo = (cur or {}).get("emotion", "neutral") if talking else (
                "happy" if cur and cur.get("emotion") in ("laugh", "happy") else
                "scared" if cur and cur.get("emotion") == "scared" and scene.get("mood") == "scary" else "neutral")
            sx = xs.get(speaker, w / 2) if speaker else w / 2
            look = max(-1.0, min(1.0, (sx - xs[k]) / (w * 0.3))) if speaker and k != speaker else 0.0
            blink = (t + _seed(k) * 4) % 3.6 < 0.12
            draw_character(img, cast[k]["design"], xs[k], ground, char_h,
                           {"t": t, "amp": amp, "talking": talking, "emotion": emo, "blink": blink, "look": look})
        if grade is not None:                            # whole frame, characters included; subtitles stay clear
            img = Image.composite(grade[0], img.convert("RGB"), grade[1])
        if cur:
            colour = cast.get(cur["speaker"], {}).get("colour") or (255, 214, 0)
            name = cast.get(cur["speaker"], {}).get("name", "")
            img = img.convert("RGBA")
            img.alpha_composite(_subtitle(name, cur["text"], w, h, colour, portrait))
        if hook_layer is not None and t < 2.4:
            img = img.convert("RGBA")
            top = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            top.alpha_composite(hook_layer.transform((w, h), Image.AFFINE, (1, 0, 0, 0, 1, int(h * 0.55) if portrait else int(h * 0.7))))
            img.alpha_composite(top)
        # camera eases toward the speaker during each line
        if cur and speaker:
            p = min(1.0, (t - cur["start"]) / 1.2)
            zoom = 1.0 + 0.07 * (p * p * (3 - 2 * p))
            img = _camera(img.convert("RGB"), zoom, xs.get(speaker, w / 2))
        enc.write(img)


def render_card(lines_text: list[tuple[str, int, tuple[int, int, int]]], fmt: str, seconds: float, enc: Encoder,
                location: str = "city", time: str = "evening", mood: str = "funny",
                cast: list[dict[str, Any]] | None = None) -> None:
    """Title / end card: background, characters waving and big text that pops in."""
    w, h = SIZES[fmt]
    portrait = fmt == "short"
    bg = background(location, time, mood, w, h)
    bg = Image.blend(bg, Image.new("RGB", (w, h), (10, 10, 25)), 0.45)
    frames = int(seconds * FPS)
    cast = (cast or [])[:3]
    xs = positions(len(cast), w)
    for i in range(frames):
        t = i / FPS
        img = bg.copy()
        for c, x in zip(cast, xs):
            draw_character(img, c["design"], x, h * (0.92 if not portrait else 0.8), h * (0.42 if not portrait else 0.24),
                           {"t": t, "emotion": "happy", "talking": False, "blink": (t + _seed(c["name"]) * 3) % 3 < 0.1})
        d = ImageDraw.Draw(img)
        pop = min(1.0, t / 0.5)
        y = h * (0.18 if not portrait else 0.22)
        for text, size, colour in lines_text:
            s = max(8, int(size * (0.6 + 0.4 * pop) * (h / 720 if not portrait else w / 720)))
            f = graphics.font_for(text, "heavy", s)
            for ln in graphics.wrap(text, f, int(w * 0.9), d):
                d.text((w / 2, y), ln, font=f, fill=colour, anchor="ma", stroke_width=4, stroke_fill=(0, 0, 0))
                y += s * 1.25
            y += s * 0.3
        enc.write(img)


def thumbnail(cast: list[dict[str, Any]], title: str, scene: dict[str, Any], out: Path) -> Path:
    w, h = 1280, 720
    img = background(scene.get("location", "home"), scene.get("time", "day"), scene.get("mood", "funny"), w, h)
    emos = ["surprised", "laugh", "shout", "happy"]
    xs = [w * 0.68, w * 0.88, w * 0.5][: max(1, min(3, len(cast)))]
    for i, (c, x) in enumerate(zip(cast[:3], xs)):
        draw_character(img, c["design"], x, h * 1.02, h * 0.95, {"t": 0.3 * i, "emotion": emos[i % 4], "talking": True, "amp": 0.8})
    d = ImageDraw.Draw(img)
    f = graphics.font_for(title, "heavy", 84)
    lines = graphics.wrap(title, f, int(w * 0.52), d)[:3]
    y = 60
    for ln in lines:
        d.text((40, y), ln, font=f, fill=(255, 230, 40), stroke_width=7, stroke_fill=(0, 0, 0))
        y += 104
    img.save(out, "JPEG", quality=90)
    return out
