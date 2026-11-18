"""Film score: one original music cue per scene, chosen from the scene's emotion (never one track for a whole film).
Every cue is synthesised by this program — no samples, no third-party recordings — so its provenance is simply
"original, made by the studio". Cues are written for underscoring: low and mid registers, slow movement and
space for dialogue; the mixer ducks them under every spoken line.

PROFILES   emotion → how the cue is built (scale, tempo, texture) and how loud it sits under the scene.
profile_for(scene) picks the emotion from the scene's purpose, mood and action (a twist gets a reveal sting,
              a climax tension, a resolution a resolving cue; 'silence' is a real choice).
cue(emotion, seconds, seed) the cue as float samples at SR.
"""
from __future__ import annotations

import re
from typing import Any

import numpy as np

SR = 48000
A4 = 440.0

# emotion: (root semitone from A3, scale, beats per minute, texture, level in dB under the dialogue-free mix)
PROFILES: dict[str, dict[str, Any]] = {
    "suspense": {"root": -5, "scale": "minor", "bpm": 72, "texture": "pulse", "db": -22},
    "mystery": {"root": -2, "scale": "dorian", "bpm": 60, "texture": "arp", "db": -23},
    "horror": {"root": -9, "scale": "locrian", "bpm": 40, "texture": "drone", "db": -21},
    "sadness": {"root": 0, "scale": "minor", "bpm": 54, "texture": "pad", "db": -23},
    "happiness": {"root": 3, "scale": "major", "bpm": 96, "texture": "arp", "db": -24},
    "romance": {"root": 5, "scale": "major", "bpm": 66, "texture": "pad", "db": -24},
    "tension": {"root": -7, "scale": "minor", "bpm": 92, "texture": "ostinato", "db": -21},
    "action": {"root": -7, "scale": "minor", "bpm": 120, "texture": "ostinato", "db": -20},
    "emotional_reveal": {"root": -3, "scale": "minor", "bpm": 50, "texture": "swell", "db": -20},
    "peaceful": {"root": 3, "scale": "major", "bpm": 58, "texture": "pad", "db": -26},
    "dramatic_ending": {"root": 0, "scale": "minor", "bpm": 60, "texture": "swell", "db": -19},
    "silence": {"root": 0, "scale": "minor", "bpm": 60, "texture": "silence", "db": -60},
}
SCALES = {"major": [0, 2, 4, 5, 7, 9, 11], "minor": [0, 2, 3, 5, 7, 8, 10], "dorian": [0, 2, 3, 5, 7, 9, 10],
          "locrian": [0, 1, 3, 5, 6, 8, 10]}
WORDS = [("horror", r"ghost|bhoot|haunt|scream|blood|dead body|corpse|darr"), ("sadness", r"tears|cries|grief|sad|alone"),
         ("romance", r"love|pyaar|romantic"), ("action", r"runs?|chase|fight|escape"),
         ("happiness", r"laugh|celebrat|happy|smile"), ("tension", r"argu|shout|angry|confront|threat")]
BY_PURPOSE = {"hook": "suspense", "setup": "mystery", "conflict": "tension", "escalation": "tension",
              "twist": "emotional_reveal", "climax": "tension", "resolution": "peaceful", "flashback": "sadness"}


def profile_for(scene: dict[str, Any], genre: str = "", last: bool = False) -> str:
    """The scene's music emotion: explicit mood words first, then what the scene does in the story."""
    text = f"{scene.get('mood', '')} {scene.get('action', '')}".lower()
    for emotion, rx in WORDS:
        if re.search(rf"\b({rx})", text):
            return emotion
    mood = (scene.get("mood") or "").lower()
    for emotion in PROFILES:
        if emotion.split("_")[0] in mood:
            return emotion
    if last:
        return "dramatic_ending" if genre in ("psychological_thriller", "suspense_mystery") else "peaceful"
    if genre == "scifi_supernatural" and scene.get("purpose") in ("hook", "setup"):
        return "mystery"
    return BY_PURPOSE.get(scene.get("purpose") or "", "mystery")


def _hz(semitones: float) -> float:
    return 220.0 * 2 ** (semitones / 12)


def _tone(freq: float, t: np.ndarray, bright: float = 0.25) -> np.ndarray:
    return np.sin(2 * np.pi * freq * t) + bright * np.sin(4 * np.pi * freq * t) + 0.08 * np.sin(6 * np.pi * freq * t)


def _env(n: int, attack: float, release: float) -> np.ndarray:
    t = np.arange(n) / SR
    return np.minimum(1, t / max(attack, 1e-3)) * np.exp(-t / max(release, 1e-3))


def _chords(p: dict[str, Any]) -> list[list[int]]:
    s = SCALES[p["scale"]]
    degrees = [0, 5, 3, 4] if p["scale"] in ("major",) else [0, 5, 2, 4]   # i–VI–III/iv–v style movement
    return [[p["root"] + s[(d + k) % 7] + 12 * ((d + k) // 7) for k in (0, 2, 4)] for d in degrees]


def cue(emotion: str, seconds: float, seed: int = 0) -> np.ndarray:
    """The cue for `emotion`, `seconds` long, peak-normalised to 0.8 (the mixer sets its level)."""
    p = PROFILES.get(emotion) or PROFILES["mystery"]
    n = int(SR * max(0.5, seconds))
    if p["texture"] == "silence":
        return np.zeros(n)
    rng = np.random.default_rng(seed)
    t = np.arange(n) / SR
    beat = 60.0 / p["bpm"]
    chords = _chords(p)
    bar = beat * 4
    out = np.zeros(n)
    # harmony bed: slow pad on the chord of each bar (every texture has it, quietly)
    for i in range(int(seconds / bar) + 1):
        a, b = int(i * bar * SR), min(n, int((i + 1) * bar * SR))
        if a >= n:
            break
        seg = t[: b - a]
        ch = chords[i % len(chords)]
        pad = sum(_tone(_hz(s - 12), seg, 0.1) + 0.4 * _tone(_hz(s - 12) * 1.004, seg, 0.05) for s in ch) / 3
        fade = np.minimum(1, seg / 0.8) * np.minimum(1, (seg[-1] - seg + 1e-3) / 0.8)
        out[a:b] += pad * fade * (0.5 if p["texture"] != "pad" else 0.9)
    tex = p["texture"]
    if tex in ("arp", "ostinato", "pulse"):
        step = beat / (2 if tex == "arp" else 4 if tex == "ostinato" else 1)
        k = 0
        while k * step < seconds:
            a = int(k * step * SR)
            ch = chords[int(k * step / bar) % len(chords)]
            note = ch[k % 3] + (12 if tex == "arp" else 0) - (12 if tex == "pulse" else 0)
            m = min(n - a, int(step * SR * 1.5))
            if m <= 0:
                break
            amp = 0.55 if tex != "pulse" or k % 4 == 0 else 0.3
            out[a:a + m] += amp * _tone(_hz(note), t[:m], 0.35) * _env(m, 0.005, step * (0.9 if tex == "arp" else 0.35))
            k += 1
    elif tex == "drone":
        drone = _tone(_hz(p["root"] - 24), t, 0.4) + 0.6 * _tone(_hz(p["root"] - 23), t, 0.2)
        swell = np.sin(2 * np.pi * 0.06 * t) ** 2
        out += drone * (0.4 + 0.6 * swell) + 0.05 * rng.normal(0, 1, n) * swell
    elif tex == "swell":
        rise = np.clip(t / max(seconds * 0.7, 1.0), 0, 1) ** 1.6
        out *= 0.4 + 0.9 * rise
        out += 0.35 * _tone(_hz(p["root"] + 12), t, 0.1) * rise * np.minimum(1, (seconds - t) / 1.5)
    if emotion in ("suspense", "tension"):                    # a soft clock-like tick under suspense
        tick = np.zeros(n)
        for k in range(int(seconds / beat)):
            a = int(k * beat * SR)
            m = min(n - a, int(0.03 * SR))
            tick[a:a + m] += rng.normal(0, 1, m) * _env(m, 0.0005, 0.008)
        out += 0.25 * tick
    peak = float(np.max(np.abs(out))) or 1.0
    out = out / peak * 0.8
    edge = min(n // 4, int(1.0 * SR))
    out[:edge] *= np.linspace(0, 1, edge)
    out[-edge:] *= np.linspace(1, 0, edge)
    return out
