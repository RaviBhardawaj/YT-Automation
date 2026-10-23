"""Original background music synthesised by this program (no samples, no third-party recordings).

Mood -> a simple, loopable bed: comedy = bouncy plucked major arpeggios; horror = low drone with
dissonant swells; drama/sad = slow minor pads; motivation/happy = bright pads with a pulse.
It sits well under dialogue (mixed about 20 dB below the voices).
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SR = 24000
NOTE = {"C": 261.63, "D": 293.66, "E": 329.63, "F": 349.23, "G": 392.0, "A": 440.0, "B": 493.88}


def _f(name: str, octave: int = 0) -> float:
    return NOTE[name] * (2 ** octave)


def _pluck(freq: float, dur: float, decay: float = 6.0) -> np.ndarray:
    t = np.arange(int(SR * dur)) / SR
    tone = np.sin(2 * np.pi * freq * t) + 0.35 * np.sin(4 * np.pi * freq * t) + 0.15 * np.sin(6 * np.pi * freq * t)
    return tone * np.exp(-decay * t)


def _pad(freqs: list[float], dur: float) -> np.ndarray:
    t = np.arange(int(SR * dur)) / SR
    sig = sum(np.sin(2 * np.pi * f * t) + 0.3 * np.sin(2 * np.pi * f * 1.003 * t) for f in freqs)
    env = np.minimum(1, t / 0.6) * np.minimum(1, (dur - t) / 0.6)
    return sig * env / max(1, len(freqs))


def _comedy(seconds: float) -> np.ndarray:
    bpm, out = 126, []
    beat = 60 / bpm / 2
    prog = [["C", "E", "G", "E"], ["F", "A", "C", "A"], ["G", "B", "D", "B"], ["C", "E", "G", "C"]]
    while sum(len(x) for x in out) < SR * seconds:
        for chord in prog:
            for i, n in enumerate(chord * 2):
                oct_ = 1 if (n in ("C", "D") and i % 4 == 3) else 0
                note = _pluck(_f(n, oct_), beat, 7.0)
                bass = _pluck(_f(chord[0], -2), beat, 3.0) * (0.8 if i % 4 == 0 else 0.0)
                out.append(note * 0.5 + bass)
    return np.concatenate(out)[: int(SR * seconds)]


def _horror(seconds: float) -> np.ndarray:
    t = np.arange(int(SR * seconds)) / SR
    drone = np.sin(2 * np.pi * 55 * t) * 0.6 + np.sin(2 * np.pi * 58.3 * t) * 0.4
    swell = np.sin(2 * np.pi * 0.07 * t) ** 2
    eerie = np.sin(2 * np.pi * 740 * t + 3 * np.sin(2 * np.pi * 0.3 * t)) * 0.08 * swell
    noise = np.random.default_rng(7).normal(0, 0.03, len(t)) * swell
    return drone * (0.5 + 0.5 * swell) + eerie + noise


def _pads(seconds: float, minor: bool) -> np.ndarray:
    chords = ([["A", "C", "E"], ["F", "A", "C"], ["C", "E", "G"], ["G", "B", "D"]] if minor else
              [["C", "E", "G"], ["G", "B", "D"], ["A", "C", "E"], ["F", "A", "C"]])
    out = []
    while sum(len(x) for x in out) < SR * seconds:
        for ch in chords:
            out.append(_pad([_f(n, -1) for n in ch], 3.2))
    sig = np.concatenate(out)[: int(SR * seconds)]
    if not minor:
        t = np.arange(len(sig)) / SR
        sig = sig + 0.15 * np.sin(2 * np.pi * _f("C", -1) * t) * (np.sin(2 * np.pi * 2 * t) > 0)
    return sig


def bed(mood: str, seconds: float, out: Path) -> Path:
    mood = (mood or "funny").lower()
    if mood in ("scary", "tense", "horror", "thriller", "mystery"):
        sig = _horror(seconds)
    elif mood in ("sad", "drama", "emotional", "romance"):
        sig = _pads(seconds, minor=True)
    elif mood in ("calm", "happy", "motivation", "inspiring"):
        sig = _pads(seconds, minor=False)
    else:
        sig = _comedy(seconds)
    sig = sig / (np.max(np.abs(sig)) or 1) * 0.8
    fade = min(len(sig), SR * 2)
    sig[:fade] *= np.linspace(0, 1, fade)
    sig[-fade:] *= np.linspace(1, 0, fade)
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((sig * 32767).astype(np.int16).tobytes())
    return out
