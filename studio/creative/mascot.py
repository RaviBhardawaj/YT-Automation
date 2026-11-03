"""Channel mascots: an original cartoon character who presents a channel's videos — speaks the narration in its own
voice, with its mouth moving to the words, blinking, bobbing and pointing, standing in a corner of every video.

Fibbu (NIFTY 50 Fibonacci channel): a small round bullish-green character with a golden Fibonacci spiral on its belly,
a candlestick antenna, big eyes and a red bow tie. Drawn by this program (Pillow) — identical in every video, free,
no GPU, no outside artwork. Deliberately not modelled on any existing character.

A channel turns a mascot on with branding = {"mascot": "fibbu"}; its narration voice then gets the mascot's pitch
(duration unchanged, so subtitles and scene timings stay exact) and every rendered video gets the talking overlay.
The mascot's comedy is light and educational: it never gives tips, targets or buy/sell views (the finance gate
still checks every word).
"""
from __future__ import annotations

import math
import subprocess
import wave
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from .. import config, logs

INK = (16, 52, 44)
BODY = (38, 186, 128)
BODY_DARK = (24, 140, 96)
BELLY = (196, 244, 214)
GOLD = (245, 178, 34)
TIE = (214, 44, 56)
CANDLE = (46, 204, 113)
WICK = (230, 230, 230)
SS = 2                                                  # drawn at 2x, scaled down for smooth edges

MASCOTS: dict[str, dict[str, Any]] = {
    "fibbu_anchor": {"name": "Fibbu", "name_hi": "फिब्बू", "variant": "anchor",
                     "voice": {"engine": "kokoro", "voice_id": "hm_omega", "speed": 1.0, "pitch": 0.94},
                     "script_rule": (
                         "PRESENTER: the news is presented by Fibbu (फिब्बू), the channel's veteran cartoon news anchor "
                         "— calm, confident and trusted, the best anchor in the business. Fibbu speaks the whole "
                         "narration in first person, opens with 'नमस्कार, मैं फिब्बू…' only in the first line, stays "
                         "neutral and factual, and allows at most ONE light, kind remark per video (never about a "
                         "tragedy, a victim or a religion). Humour never changes a fact. No opinions, no advice."),
                     "voice_en": {"engine": "kokoro", "voice_id": "am_onyx", "speed": 1.08, "pitch": 1.0},
                     "script_rule_en": (
                         "PRESENTER: the news is presented by Fibbu, the channel's veteran cartoon news anchor — the "
                         "most trusted anchor in the world: calm, warm and authoritative. First line only: 'Hello, "
                         "I'm Fibbu.' AUDIENCE: viewers worldwide, mostly in the United States and India — explain any "
                         "local term in a few words (the Fed, Sensex, crore, SEC) and, where it is relevant, say what "
                         "the story means for people in both countries. COMMENTARY: after the facts, add one or two "
                         "lines of context that help viewers understand — background, scale, what to watch next — "
                         "always attributed ('according to Reuters', 'analysts quoted by CNBC say'); never your own "
                         "opinion stated as fact, no predictions, no advice. At most one light, kind remark per video, "
                         "never about a tragedy, a victim or a religion. Humour never changes a fact.")},
    "fibbu": {"name": "Fibbu", "name_hi": "फिब्बू",
              "voice": {"engine": "kokoro", "voice_id": "hm_omega", "speed": 1.0, "pitch": 1.32},
              "script_rule": (
                  "PRESENTER: the video is presented by Fibbu (फिब्बू), the channel's small cartoon mascot who speaks "
                  "the whole narration in first person ('मैं फिब्बू…' only in the first line). Fibbu is cheerful and "
                  "funny: add 2–3 short, gentle everyday jokes or comparisons per video (मम्मी, चाय, क्रिकेट, ट्रैफिक "
                  "— e.g. price touching a level 'जैसे गरम पराठा छू लिया हो'), never about losses, never mocking "
                  "viewers. The humour never changes a number or a fact, and Fibbu NEVER gives tips, targets, "
                  "buy/sell views or predictions — Fibbu teaches what the levels mean, as education only.")},
}


def of(channel: dict[str, Any] | None) -> dict[str, Any] | None:
    """The channel's mascot (None when it has none)."""
    key = ((channel or {}).get("branding") or {}).get("mascot")
    return {**MASCOTS[key], "key": key} if key in MASCOTS else None


def draw_in_scene(img: Image.Image, design: dict[str, Any], x: float, ground: float, height: float,
                  state: dict[str, Any]) -> None:
    """A mascot as a story character: about two thirds of a person's height, feet on the scene's ground."""
    m = MASCOTS.get(design.get("mascot") or "")
    size = max(40, int(height * 0.72))
    pic = draw_fibbu(float(state.get("t", 0.0)), float(state.get("amp", 0.0)) if state.get("talking", True) else 0.0,
                     blink=bool(state.get("blink")), look=float(state.get("look", 0.0)),
                     wave=0.8 if state.get("talking") else 0.15, size=size, variant=(m or {}).get("variant", "classic"))
    img.paste(pic, (int(x - size / 2), int(ground - size * 566 / 600)), pic)


def for_production(p: dict[str, Any]) -> dict[str, Any] | None:
    from .. import channels
    return of(channels.get(p.get("channel_id"))) if p.get("channel_id") else None


def _english(p: dict[str, Any]) -> bool:
    from .. import channels
    ch = channels.get(p.get("channel_id")) if p.get("channel_id") else None
    return (p.get("language") or (ch or {}).get("language") or "") == "English"


def script_rule(p: dict[str, Any]) -> str:
    m = for_production(p)
    if not m:
        return ""
    return m.get("script_rule_en", m["script_rule"]) if _english(p) else m["script_rule"]


def voice_for(m: dict[str, Any], language: str) -> dict[str, Any]:
    """The mascot's voice in the channel's language (an English voice for an English channel)."""
    return m.get("voice_en", m["voice"]) if language == "English" else m["voice"]


# ------------------------------------------------------------------ drawing

def _spiral(d: ImageDraw.ImageDraw, cx: float, cy: float, scale: float, width: int) -> None:
    """A golden (Fibonacci) spiral: r grows by φ every quarter turn."""
    phi = (1 + 5 ** 0.5) / 2
    pts = []
    for i in range(361):
        th = i / 360 * 3.2 * math.pi
        r = scale * 0.035 * phi ** (2 * th / math.pi)
        pts.append((cx + r * math.cos(th + math.pi), cy + r * math.sin(th + math.pi)))
    d.line(pts, fill=GOLD, width=width, joint="curve")


def draw_fibbu(t: float, amp: float, *, blink: bool, look: float = 0.6, wave: float = 0.0,
               size: int = 600, variant: str = "classic") -> Image.Image:
    """One frame of Fibbu on a transparent canvas. t = seconds (idle bob), amp = mouth 0..1 from the voice,
    blink = eyes shut, look = pupils left(-1)..right(1), wave = 0..1 pointer arm raised."""
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = S / 600                                          # design units: a 600 x 600 box, feet on y≈560
    bob = math.sin(t * 2 * math.pi * 0.9) * 6 + amp * 4
    squash = 1 + amp * 0.03
    cx, cy = 300 * u, (350 - bob) * u
    rx, ry = 165 * u * squash, 190 * u / squash
    lw = int(7 * u)
    d.ellipse([cx - 150 * u, 548 * u, cx + 150 * u, 578 * u], fill=(0, 0, 0, 70))
    for side in (-1, 1):
        fx = cx + side * 70 * u
        d.ellipse([fx - 52 * u, 520 * u, fx + 52 * u, 566 * u], fill=BODY_DARK, outline=INK, width=lw)
    d.ellipse([cx - rx - 38 * u, cy + 20 * u, cx - rx + 22 * u, cy + 110 * u], fill=BODY_DARK, outline=INK, width=lw)
    d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=BODY, outline=INK, width=lw)
    ax = cx + math.sin(t * 2 * math.pi * 0.9 - 0.6) * 10 * u           # candlestick antenna, a beat behind
    top = cy - ry
    d.line([(cx, top + 6 * u), (ax, top - 46 * u)], fill=INK, width=int(6 * u))
    d.line([(ax, top - 122 * u), (ax, top - 46 * u)], fill=WICK, width=int(5 * u))
    d.rounded_rectangle([ax - 18 * u, top - 104 * u, ax + 18 * u, top - 58 * u], radius=int(5 * u), fill=CANDLE,
                        outline=INK, width=int(5 * u))
    by = cy + 78 * u
    d.ellipse([cx - 112 * u, by - 96 * u, cx + 112 * u, by + 96 * u], fill=BELLY)
    _spiral(d, cx + 20 * u, by + 22 * u, 132 * u, int(8 * u))
    ty = cy + 26 * u                                     # bow tie at the neck, clear of the spiral
    d.polygon([(cx, ty + 8 * u), (cx - 34 * u, ty - 6 * u), (cx - 34 * u, ty + 22 * u)], fill=TIE, outline=INK)
    d.polygon([(cx, ty + 8 * u), (cx + 34 * u, ty - 6 * u), (cx + 34 * u, ty + 22 * u)], fill=TIE, outline=INK)
    d.ellipse([cx - 9 * u, ty, cx + 9 * u, ty + 16 * u], fill=TIE, outline=INK, width=int(4 * u))
    ey = cy - 70 * u
    for side in (-1, 1):
        ex = cx + side * 62 * u
        if blink:
            d.arc([ex - 44 * u, ey - 20 * u, ex + 44 * u, ey + 24 * u], 10, 170, fill=INK, width=int(8 * u))
        else:
            d.ellipse([ex - 46 * u, ey - 54 * u, ex + 46 * u, ey + 50 * u], fill=(255, 255, 255), outline=INK,
                      width=int(6 * u))
            px, py = ex + look * 18 * u, ey + 4 * u
            d.ellipse([px - 22 * u, py - 24 * u, px + 22 * u, py + 24 * u], fill=(30, 30, 36))
            d.ellipse([px - 4 * u, py - 16 * u, px + 8 * u, py - 4 * u], fill=(255, 255, 255))
        brow = ey - 70 * u - amp * 14 * u
        d.line([(ex - 34 * u, brow - side * 6 * u), (ex + 34 * u, brow + side * 6 * u)],
               fill=(225, 225, 225) if variant == "anchor" else INK, width=int(12 * u if variant == "anchor" else 10 * u))
        if variant == "anchor" and not blink:            # the senior anchor's round glasses
            d.ellipse([ex - 54 * u, ey - 60 * u, ex + 54 * u, ey + 56 * u], outline=(40, 40, 46), width=int(7 * u))
    if variant == "anchor":
        d.line([(cx - 54 * u + 46 * u, ey - 4 * u), (cx + 8 * u, ey - 4 * u)], fill=(40, 40, 46), width=int(6 * u))
    for side in (-1, 1):
        d.ellipse([cx + side * 118 * u - 22 * u, cy - 14 * u, cx + side * 118 * u + 22 * u, cy + 12 * u],
                  fill=(255, 150, 160))
    my = cy - 2 * u
    if variant == "anchor":                              # a distinguished grey moustache above the mouth
        for side in (-1, 1):
            d.ellipse([cx + side * 46 * u - 40 * u, my - 34 * u, cx + side * 46 * u + 40 * u, my - 8 * u],
                      fill=(214, 214, 214), outline=INK, width=int(4 * u))
    if amp > 0.08:
        oh = (14 + amp * 46) * u
        d.ellipse([cx - 40 * u, my - oh / 2, cx + 40 * u, my + oh / 2], fill=(120, 24, 40), outline=INK, width=lw)
        d.ellipse([cx - 20 * u, my + oh / 2 - 18 * u, cx + 20 * u, my + oh / 2 - 2 * u], fill=(240, 110, 130))
    else:
        d.arc([cx - 40 * u, my - 26 * u, cx + 40 * u, my + 16 * u], 20, 160, fill=INK, width=int(8 * u))
    ang = math.radians(-20 - 70 * wave + math.sin(t * 6) * 6 * wave)
    sx, sy = cx + rx - 18 * u, cy + 30 * u
    hx, hy = sx + math.cos(ang) * 70 * u, sy + math.sin(ang) * 70 * u
    d.line([(sx, sy), (hx, hy)], fill=INK, width=int(30 * u))
    d.line([(sx, sy), (hx, hy)], fill=BODY_DARK, width=int(20 * u))
    if variant == "anchor":                              # holds a news microphone instead of a pointer
        mx, mmy = hx + math.cos(ang) * 46 * u, hy + math.sin(ang) * 46 * u
        d.line([(hx, hy), (mx, mmy)], fill=(40, 40, 46), width=int(14 * u))
        d.ellipse([mx - 26 * u, mmy - 26 * u, mx + 26 * u, mmy + 26 * u], fill=(70, 70, 78), outline=INK, width=lw)
        d.rectangle([hx - 22 * u, hy + 4 * u, hx + 22 * u, hy + 30 * u], fill=(214, 44, 56), outline=INK)
    else:
        d.line([(hx, hy), (hx + math.cos(ang) * 120 * u, hy + math.sin(ang) * 120 * u)], fill=(150, 92, 40),
               width=int(9 * u))
    d.ellipse([hx - 30 * u, hy - 30 * u, hx + 30 * u, hy + 30 * u], fill=BODY_DARK, outline=INK, width=lw)
    return img.resize((size, size), Image.LANCZOS)


# ------------------------------------------------------------------ voice and video

def pitch_voice(wav: Path, pitch: float) -> None:
    """The mascot's voice: raise the pitch, keep the exact duration (subtitle and scene timings stay valid)."""
    if abs(pitch - 1.0) < 0.01:
        return
    with wave.open(str(wav)) as w:
        sr = w.getframerate()
    tmp = wav.with_suffix(".pitch.wav")
    subprocess.run([config.FFMPEG, "-v", "error", "-y", "-i", str(wav), "-af",
                    f"asetrate={int(sr * pitch)},aresample={sr},atempo={1 / pitch:.5f},equalizer=f=3000:t=q:w=1:g=2",
                    str(tmp)], check=True, capture_output=True, timeout=600)
    tmp.replace(wav)


def _envelope(narration: Path, frames: int) -> np.ndarray:
    from . import cartoon
    with wave.open(str(narration)) as w:
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        sr = w.getframerate()
    return cartoon.envelope(samples, sr, frames)


PLACE = {"long": (320, 30, 52), "short": (420, 30, 130)}          # size, left margin, bottom margin


def overlay(video: Path, narration: Path, out: Path, fmt: str, duration: float, variant: str = "classic") -> int:
    """Draw the talking mascot over a finished video (bottom-left, clear of the centred subtitles and the bottom
    disclaimer strip). Frames are streamed straight into FFmpeg; repeated poses are drawn once. Returns frames."""
    from ..media import render
    fps = config.FPS
    variant = (variant or "classic")
    frames = max(1, int(duration * fps))
    env = _envelope(narration, frames)
    size, left, bottom = PLACE.get(fmt, PLACE["long"])
    w, h = config.frame_size(fmt)
    x, y = left, h - bottom - size
    cache: dict[tuple, bytes] = {}
    proc = subprocess.Popen(
        [config.FFMPEG, "-hide_banner", "-nostdin", "-y", "-v", "error", "-i", str(video), "-f", "rawvideo",
         "-pix_fmt", "rgba", "-s", f"{size}x{size}", "-r", str(fps), "-i", "-", "-filter_complex",
         f"[0:v][1:v]overlay={x}:{y}:eof_action=pass:format=auto,format=yuv420p[v]", "-map", "[v]", "-map", "0:a?",
         *render._encode_args(), "-c:a", "copy", "-movflags", "+faststart", str(out)],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for i in range(frames):
            t = i / fps
            amp = round(float(env[i]) if i < len(env) else 0.0, 1)
            talking = amp > 0.08
            key = (int((t * 0.9 % 1) * 24), amp, (t % 3.7) < 0.12, 0.9 if talking else 0.15)
            if key not in cache:
                cache[key] = draw_fibbu(key[0] / 24 / 0.9, amp, blink=key[2], wave=key[3], look=0.7, size=size,
                                        variant=variant).tobytes()
            proc.stdin.write(cache[key])
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace")
        if proc.wait(timeout=3600):
            raise RuntimeError(f"mascot overlay failed: {err[-400:]}")
    finally:
        if proc.poll() is None:
            proc.kill()
    logs.event("info", "creative", f"Mascot drawn over {frames} frames ({len(cache)} distinct poses)")
    return frames
