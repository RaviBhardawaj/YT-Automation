"""Performance engine for films: who speaks, how they sound, when their mouth moves, and what the audience hears.

Voice Director   deliver(line, profile) → the line's speed / pitch / gain from its emotion; names are written the
                 way the character's profile says they are pronounced; one fixed voice per character, never chosen
                 at random; narration is not allowed to borrow a character's voice.
Identity check   voice_identity(lines) measures every recorded line's pitch (F0) and flags lines that drift from their
                 character's own median, and characters whose voices are too close to tell apart.
Dialogue timeline  dialogue_timeline(tl) — per line: character, dialogue id, audio file, start/end on the film's
                 clock, word timestamps (local Whisper, when installed) and the speech envelope used for lip-sync.
Lip-sync check   lipsync_score(clip, wav) correlates the clip's frame-to-frame change with the voice's loudness:
                 a mouth driven by the line moves when the line is loud and rests in its pauses.
Music & sound    music_plan(film, tl) → one cue per scene from its emotion, with fades; render_music(...) ducks it
                 under every line (dialogue-aware gain envelope, not a fixed level); ambience(...) per place.
Mix              mix(...) sums dialogue, music and ambience on ONE clock, measures loudness (EBU R128) and peaks,
                 and normalises in two passes to -16 LUFS / -1.5 dBTP (no blind one-pass normalisation).
Checks / report  sync_checks(...) and report(...) — the performance QC, separate from the technical QC; a check
                 that could not be measured is reported as unverified, never as passed.
"""
from __future__ import annotations

import json
import re
import subprocess
import wave
from pathlib import Path
from typing import Any

import numpy as np

from .. import config
from . import score

SR = 48000
# emotion → (speed factor, pitch factor, gain) applied on top of the character's own profile
DELIVERY = {"neutral": (1.0, 1.0, 1.0), "fear": (1.07, 1.03, 0.9), "anger": (1.05, 0.98, 1.15),
            "happiness": (1.04, 1.02, 1.05), "sadness": (0.9, 0.98, 0.85), "whisper": (0.94, 1.0, 0.55),
            "excitement": (1.08, 1.04, 1.1), "confusion": (0.95, 1.01, 0.95), "surprise": (1.03, 1.04, 1.05)}
MANNER = [("whisper", r"whisper|softly|quietly|फुसफुस|धीरे"), ("fear", r"fear|scared|afraid|tremb|nervous|panic|डर"),
          ("anger", r"ang|furious|shout|yell|sharp|गुस्स"), ("sadness", r"sad|tear|cry|broken|soft sigh|दुख"),
          ("excitement", r"excit|eager|thrill"), ("happiness", r"happy|laugh|smil|cheer|warm"),
          ("confusion", r"confus|puzzl|unsure|blank|hesit"), ("surprise", r"surpris|shock|gasp|stunned")]
DUCK_DB = -12.0                 # music under a spoken line, relative to its own level
DUCK_ATTACK, DUCK_RELEASE = 0.25, 0.8
TARGET_LUFS, TARGET_TP = -16.0, -1.5
LINE_RMS_DB = -20.0             # every line levelled to the same speech loudness before the mix


def emotion_of(manner: str) -> str:
    m = (manner or "").lower()
    for emotion, rx in MANNER:
        if re.search(rx, m):
            return emotion
    return "neutral"


def pronounce(text: str, rules: dict[str, str] | None) -> str:
    """Recurring names and words said the same way every time (the profile's pronunciation map)."""
    for written, said in (rules or {}).items():
        text = re.sub(rf"(?<![\wऀ-ॿ]){re.escape(written)}(?![\wऀ-ॿ])", said, text)
    return text


def deliver(line: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """How this line is spoken by this character: fixed voice, emotion-shaped speed / pitch / gain."""
    emotion = emotion_of(line.get("manner", ""))
    s, p, g = DELIVERY[emotion]
    return {"voice_id": profile["voice_id"], "engine": profile.get("engine", "kokoro"),
            "logical_id": profile.get("logical_id"), "emotion": emotion,
            "speed": round(float(profile.get("speed", 1.0)) * s, 3), "pitch": round(float(profile.get("pitch", 1.0)) * p, 3),
            "gain": g, "text": pronounce(line.get("tts") or line["text"], profile.get("pronunciations"))}


def level(audio: np.ndarray, target_db: float = LINE_RMS_DB) -> np.ndarray:
    """Every line at the same speech loudness (RMS of the voiced part), peaks kept under full scale."""
    a = audio.astype(np.float64)
    voiced = a[np.abs(a) > 0.02 * (np.max(np.abs(a)) or 1)]
    rms = float(np.sqrt(np.mean(voiced ** 2))) if len(voiced) else 0.0
    if rms <= 0:
        return audio
    gain = (32767 * 10 ** (target_db / 20)) / rms
    peak = float(np.max(np.abs(a))) * gain
    if peak > 32000:
        gain *= 32000 / peak
    return np.clip(a * gain, -32768, 32767).astype(np.int16)


# ------------------------------------------------------------------ voice identity

def f0(audio: np.ndarray, sr: int) -> float | None:
    """Median fundamental frequency of the voiced frames (autocorrelation; 70–400 Hz)."""
    a = audio.astype(np.float64) / 32768.0
    win, hop = int(0.04 * sr), int(0.02 * sr)
    lo, hi = int(sr / 400), int(sr / 70)
    vals = []
    for i in range(0, len(a) - win, hop):
        fr = a[i:i + win] * np.hanning(win)
        if np.sqrt(np.mean(fr ** 2)) < 0.02:
            continue
        ac = np.correlate(fr, fr, "full")[win - 1:]
        if ac[0] <= 0:
            continue
        lag = lo + int(np.argmax(ac[lo:hi]))
        if ac[lag] / ac[0] > 0.45:
            vals.append(sr / lag)
    return round(float(np.median(vals)), 1) if len(vals) >= 3 else None


def _read(path: str | Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16), w.getframerate()


def voice_identity(lines: list[dict[str, Any]], drift: float = 0.15, apart: float = 0.06) -> dict[str, Any]:
    """lines: [{speaker, wav}] → per-character pitch, lines that sound like someone else, voices too alike."""
    per: dict[str, list[tuple[str, float]]] = {}
    for ln in lines:
        if ln.get("wav") and Path(ln["wav"]).exists():
            a, sr = _read(ln["wav"])
            hz = f0(a, sr)
            if hz:
                per.setdefault(ln["speaker"], []).append((ln["wav"], hz))
    chars = {k: round(float(np.median([h for _, h in v])), 1) for k, v in per.items()}
    flagged = [{"speaker": k, "wav": w, "hz": h, "median": chars[k]} for k, v in per.items() for w, h in v
               if abs(h - chars[k]) / chars[k] > drift]
    close = [(a, b) for i, a in enumerate(sorted(chars)) for b in sorted(chars)[i + 1:]
             if abs(chars[a] - chars[b]) / max(chars[a], chars[b]) < apart]
    return {"median_hz": chars, "lines_measured": sum(len(v) for v in per.values()), "drifting_lines": flagged,
            "too_alike": close, "ok": not flagged and not close}


# ------------------------------------------------------------------ dialogue timeline

def word_times(wav: str, language: str = "hi") -> list[dict[str, Any]]:
    """Word start/end inside a line (local Whisper). [] when Whisper isn't installed — reported as unmeasured."""
    try:
        from ..editing import transcribe
        model = transcribe._load()
        segs, _ = model.transcribe(transcribe._pcm16k(wav), language=language, word_timestamps=True, beam_size=1)
        return [{"word": w.word.strip(), "start": round(w.start, 2), "end": round(w.end, 2)}
                for s in segs for w in (s.words or [])]
    except Exception:  # noqa: BLE001 - optional measurement
        return []


def envelope(wav: str, fps: int) -> list[float]:
    """Speech loudness per video frame (0..1) — what the mouth should follow."""
    a, sr = _read(wav)
    hop = sr // fps
    frames = [float(np.sqrt(np.mean((a[i:i + hop].astype(np.float64) / 32768) ** 2))) for i in range(0, len(a), hop)]
    top = max(frames) if frames else 0
    return [round(f / top, 3) if top else 0.0 for f in frames]


def dialogue_timeline(tl: list[dict[str, Any]], *, words: bool = True) -> list[dict[str, Any]]:
    out = []
    for c in tl:
        if c.get("kind") != "shot" or not c.get("wav"):
            continue
        start = c["start"]
        end = round(start + float(c.get("speech") or 0), 3)
        out.append({"dialogue_id": f"s{c['scene']:02d}.{c['shot']:02d}", "character": c.get("speaker_key"),
                    "name": c.get("speaker"), "shot_id": c.get("shot_id"), "audio": c["wav"], "start": start,
                    "end": end, "shot_end": c["end"], "emotion": c.get("emotion", "neutral"),
                    "on_screen": c.get("characters") or [], "motion_type": c.get("motion_type"),
                    "words": [{**w, "start": round(start + w["start"], 2), "end": round(start + w["end"], 2)}
                              for w in (word_times(c["wav"]) if words else [])],
                    "speaking": True})
    return out


# ------------------------------------------------------------------ lip-sync measurement

def lipsync_score(clip: str | Path, wav: str | Path, fps: int = 25) -> dict[str, Any]:
    """Correlation between the clip's frame-to-frame change and the line's loudness (lagged ±2 frames)."""
    w, h = 96, 54
    r = subprocess.run([config.FFMPEG, "-v", "error", "-i", str(clip), "-vf", f"fps={fps},scale={w}:{h},format=gray",
                        "-f", "rawvideo", "-"], capture_output=True, timeout=180)
    n = len(r.stdout) // (w * h)
    if r.returncode or n < 8:
        return {"measured": False, "reason": "could not read the clip"}
    fr = np.frombuffer(r.stdout[: n * w * h], dtype=np.uint8).reshape(n, h, w).astype(np.float32)
    motion = np.abs(np.diff(fr, axis=0)).mean(axis=(1, 2))
    env = np.array(envelope(str(wav), fps))[1:len(motion) + 1]
    m = motion[: len(env)]
    if len(m) < 8 or m.std() == 0 or env.std() == 0:
        return {"measured": True, "correlation": 0.0, "moves": bool(float(m.std()) > 0)}
    best = max(float(np.corrcoef(np.roll(m, k), env)[0, 1]) for k in (-2, -1, 0, 1, 2))
    quiet = env < 0.15
    rest = float(m[quiet].mean() / (m[~quiet].mean() or 1)) if quiet.any() and (~quiet).any() else None
    return {"measured": True, "correlation": round(float(best), 2), "moves": True,
            "motion_in_pauses_vs_speech": round(rest, 2) if rest is not None else None}


# ------------------------------------------------------------------ music and ambience

def music_plan(film: dict[str, Any], tl: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One cue per scene, from the scene's emotion; consecutive scenes with the same emotion share one cue."""
    scenes = film.get("scenes") or []
    spans: dict[int, list[float]] = {}
    for c in tl:
        if c.get("kind") == "shot":
            s = spans.setdefault(c["scene"], [c["start"], c["end"]])
            s[1] = c["end"]
    plan: list[dict[str, Any]] = []
    for no in sorted(spans):
        scene = scenes[no - 1] if no - 1 < len(scenes) else {}
        emotion = score.profile_for(scene, film.get("genre", ""), last=no == max(spans))
        a, b = spans[no]
        if plan and plan[-1]["emotion"] == emotion:
            plan[-1]["end"] = b
            plan[-1]["scenes"].append(no)
            continue
        plan.append({"cue_id": f"cue{len(plan) + 1:02d}-{emotion}", "scenes": [no], "emotion": emotion, "start": a,
                     "end": b, "level_db": score.PROFILES[emotion]["db"], "fade_in": 1.2, "fade_out": 1.5,
                     "ducking": {"under_dialogue_db": DUCK_DB, "attack": DUCK_ATTACK, "release": DUCK_RELEASE},
                     "provenance": "original — synthesised by the studio (studio/film_studio/score.py)"})
    for i, cue_ in enumerate(plan):                       # overlap neighbours by the fade for a crossfade
        if i:
            cue_["start"] = max(0.0, cue_["start"] - cue_["fade_in"] / 2)
    return plan


def duck_envelope(total: float, speech: list[tuple[float, float]]) -> np.ndarray:
    """Gain (linear) over the film: 1 outside dialogue, DUCK_DB under it, with smooth attack/release ramps."""
    n = int(total * SR) + 1
    target = np.ones(n)
    low = 10 ** (DUCK_DB / 20)
    for a, b in speech:
        target[max(0, int((a - 0.1) * SR)):min(n, int((b + 0.15) * SR))] = low
    out = np.empty(n)
    g, up, down = 1.0, 1 / (DUCK_RELEASE * SR), 1 / (DUCK_ATTACK * SR)
    for i in range(0, n, 480):                             # 10 ms steps: smooth and fast enough
        t = target[i]
        g = max(t, g - down * 480) if t < g else min(t, g + up * 480)
        out[i:i + 480] = g
    return out


def render_music(plan: list[dict[str, Any]], total: float, speech: list[tuple[float, float]], out: Path) -> Path:
    n = int(total * SR) + 1
    track = np.zeros(n)
    for i, c in enumerate(plan):
        a, b = int(c["start"] * SR), min(n, int(c["end"] * SR) + int(c["fade_out"] * SR / 2))
        sig = score.cue(c["emotion"], (b - a) / SR, seed=i + 1)[: b - a]
        track[a:a + len(sig)] += sig * 10 ** (c["level_db"] / 20)
    track *= duck_envelope(total, speech)[:n]
    _write(out, track)
    return out


AMBIENCE = [("rain", r"rain|storm|monsoon|baarish|बारिश"), ("street", r"street|lane|alley|market|bazaar|station|road"),
            ("night", r"night|crickets|village|field|forest"), ("room", r".")]


def ambience(description: str, seconds: float, seed: int = 0) -> tuple[str, np.ndarray]:
    """Room tone, rain, a distant street or a night field — quiet, under everything (original, synthesised)."""
    kind = next(k for k, rx in AMBIENCE if re.search(rx, (description or "").lower()))
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    noise = rng.normal(0, 1, n)
    if kind == "rain":
        sig = np.convolve(noise, np.ones(6) / 6, "same") + 0.6 * (rng.random(n) > 0.9995) * rng.normal(0, 4, n)
        db = -34
    elif kind == "street":
        sig = np.convolve(noise, np.ones(40) / 40, "same") * (1 + 0.5 * np.sin(2 * np.pi * 0.05 * np.arange(n) / SR))
        db = -38
    elif kind == "night":
        t = np.arange(n) / SR
        sig = 0.3 * np.convolve(noise, np.ones(60) / 60, "same") + \
            0.2 * np.sin(2 * np.pi * 4300 * t) * (np.sin(2 * np.pi * 3 * t) > 0.6) * (np.sin(2 * np.pi * 0.2 * t) > 0)
        db = -40
    else:
        sig = np.convolve(noise, np.ones(120) / 120, "same")
        db = -46
    sig = sig / (np.max(np.abs(sig)) or 1)
    return kind, sig * 10 ** (db / 20)


def render_ambience(film: dict[str, Any], tl: list[dict[str, Any]], total: float, out: Path) -> tuple[Path, list]:
    n = int(total * SR) + 1
    track, used = np.zeros(n), []
    envs = film.get("environments") or {}
    scenes = film.get("scenes") or []
    spans: dict[int, list[float]] = {}
    for c in tl:
        if c.get("kind") == "shot":
            s = spans.setdefault(c["scene"], [c["start"], c["end"]])
            s[1] = c["end"]
    for no, (a, b) in sorted(spans.items()):
        sc = scenes[no - 1] if no - 1 < len(scenes) else {}
        desc = f"{(envs.get(sc.get('location') or '') or {}).get('description', '')} {sc.get('action', '')}"
        kind, sig = ambience(desc, b - a + 1.0, seed=no)
        i = int(a * SR)
        seg = sig[: n - i]
        edge = min(len(seg) // 3, int(0.5 * SR))
        if edge:
            seg[:edge] *= np.linspace(0, 1, edge)
            seg[-edge:] *= np.linspace(1, 0, edge)
        track[i:i + len(seg)] += seg
        used.append({"scene": no, "ambience": kind, "start": a, "end": b})
    _write(out, track)
    return out, used


def _write(path: Path, x: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(np.clip(x * 32767, -32768, 32767).astype(np.int16).tobytes())


# ------------------------------------------------------------------ mix and measurements

def loudness(path: str | Path) -> dict[str, float]:
    """Integrated loudness, true peak and range (EBU R128, measured by FFmpeg)."""
    r = subprocess.run([config.FFMPEG, "-hide_banner", "-i", str(path), "-af",
                        f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TP}:LRA=11:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=600)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
    if not m:
        return {}
    d = json.loads(m.group(0))
    return {k: float(d[k]) for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}


def mix(dialogue: Path, music: Path, amb: Path, out: Path) -> dict[str, Any]:
    """Sum the three stems (already on one clock), then two-pass loudness normalisation to the target."""
    raw = out.with_name("mix_raw.wav")
    subprocess.run([config.FFMPEG, "-v", "error", "-y", "-i", str(dialogue), "-i", str(music), "-i", str(amb),
                    "-filter_complex", "[0:a]aresample=48000[d];[1:a]aresample=48000[m];[2:a]aresample=48000[x];"
                    "[d][m][x]amix=inputs=3:duration=longest:normalize=0[a]", "-map", "[a]", str(raw)],
                   check=True, capture_output=True, timeout=600)
    before = loudness(raw)
    if before:
        f = (f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TP}:LRA=11:measured_I={before['input_i']}:"
             f"measured_TP={before['input_tp']}:measured_LRA={before['input_lra']}:"
             f"measured_thresh={before['input_thresh']}:offset={before['target_offset']}:linear=true")
    else:
        f = f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TP}:LRA=11"
    subprocess.run([config.FFMPEG, "-v", "error", "-y", "-i", str(raw), "-af", f"{f},aresample=48000", "-ac", "2",
                    str(out)], check=True, capture_output=True, timeout=600)
    after = loudness(out)
    return {"before": before, "after": after, "target_lufs": TARGET_LUFS, "target_tp": TARGET_TP}


def dialogue_vs_music(dialogue: Path, music: Path, speech: list[tuple[float, float]]) -> dict[str, Any]:
    """How far below the voices the music sits while a line is spoken (dB) — must stay ≥ 10 dB."""
    d, _ = _read(dialogue)
    m, _ = _read(music)
    gaps = []
    for a, b in speech:
        i, j = int(a * SR), int(b * SR)
        dv, mv = d[i:j].astype(np.float64), m[i:j].astype(np.float64)
        if len(dv) < SR // 4 or len(mv) < SR // 4:
            continue
        rd, rm = np.sqrt(np.mean(dv ** 2)), np.sqrt(np.mean(mv ** 2))
        if rd > 0 and rm > 0:
            gaps.append(float(20 * np.log10(rd / rm)))
    return {"lines": len(gaps), "min_db": round(min(gaps), 1) if gaps else None,
            "median_db": round(float(np.median(gaps)), 1) if gaps else None, "ok": bool(gaps and min(gaps) >= 10)}


# ------------------------------------------------------------------ checks and report

def sync_checks(dlg: list[dict[str, Any]], tl: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []

    def add(name: str, ok: bool | None, detail: str) -> None:
        out.append({"name": name, "ok": ok, "detail": detail})
    wrong = [d["dialogue_id"] for d in dlg if d["character"] and d["character"] not in d["on_screen"]]
    add("speaker is on screen when their line plays", not wrong, f"lines off-screen: {wrong}" if wrong else "every line")
    crowded = [d["dialogue_id"] for d in dlg if len(d["on_screen"]) > 1]
    add("only the speaker is framed on a dialogue shot", not crowded,
        f"two-shots with dialogue: {crowded}" if crowded else "every dialogue shot frames its speaker alone")
    spill = [d["dialogue_id"] for d in dlg if d["end"] > d["shot_end"] + 0.05]
    add("dialogue ends before the shot changes", not spill, f"lines running past the cut: {spill}" if spill else "ok")
    subs = [c for c in tl if c.get("subtitle")]
    bad_subs = [f"{c['scene']}.{c['shot']}" for c in subs if not c.get("speech") or
                c["start"] + float(c["speech"]) > c["end"] + 0.05]
    add("subtitles inside their line's time", not bad_subs, f"off: {bad_subs}" if bad_subs else f"{len(subs)} subtitles")
    still = [d["dialogue_id"] for d in dlg if d["motion_type"] not in ("dialogue_video", "lipsync_verified")]
    add("every line has a talking face", not still,
        f"{len(still)} line(s) play over a still picture: {still}" if still else "every line is lip-synced")
    return out


def report(checks: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """{area: checks} → per-area status: passed / failed / unverified (never 'passed' for an unmeasured check)."""
    out = {}
    for area, items in checks.items():
        status = "failed" if any(c["ok"] is False for c in items) else \
            "unverified" if any(c["ok"] is None for c in items) or not items else "passed"
        out[area] = {"status": status, "checks": items}
    out["overall"] = "failed" if any(v["status"] == "failed" for v in out.values() if isinstance(v, dict)) else \
        "unverified" if any(v["status"] == "unverified" for v in out.values() if isinstance(v, dict)) else "passed"
    return out


def film_qc(master: dict[str, Any], cast: list[dict[str, Any]]) -> dict[str, Any]:
    """The performance QC of a finished film, from its master timeline (measured, not assumed)."""
    dlg, shots = master.get("dialogue") or [], master.get("shots") or []
    talking = ("dialogue_video", "lipsync_verified")
    # LIP-SYNC
    lips = []
    for d in dlg:
        shot = next((s for s in shots if f"s{s['scene']:02d}.{s['shot']:02d}" == d["dialogue_id"]), {})
        if d["motion_type"] not in talking:
            lips.append({"name": f"{d['dialogue_id']} {d['name']}: mouth moves with the line", "ok": False,
                         "detail": "still picture — no mouth movement"})
            continue
        clip = d.get("clip")
        m = lipsync_score(clip, d["audio"]) if clip and Path(clip).exists() else {"measured": False}
        ok = None if not m.get("measured") else m.get("correlation", 0) >= 0.2
        lips.append({"name": f"{d['dialogue_id']} {d['name']}: mouth moves with the line", "ok": ok,
                     "detail": f"speech/motion correlation {m.get('correlation')}" if m.get("measured")
                     else "talking clip not measurable", "provider": shot.get("provider")})
    # MOVEMENT
    moves = []
    for s in shots:
        anim = (s.get("animation") or {}).get("body_action") or ""
        real = s["motion_type"] in ("video",) + talking
        moves.append({"name": f"{s['scene']}.{s['shot']} {s['shot_type']}: {anim[:60] or 'no action written'}",
                      "ok": True if s["motion_type"] == "video" else
                      None if s["motion_type"] in talking else False,
                      "detail": {"video": "AI video clip (movement quality not machine-checked)",
                                 "dialogue_video": "talking face only — the body does not move",
                                 }.get(s["motion_type"], "still picture with a camera move — no character animation")
                      if not real or s["motion_type"] != "video" else "AI video clip"})
    # VOICE
    profiles = {c["key"]: (c.get("voice") or {}) for c in cast}
    ident = voice_identity([{"speaker": d["character"], "wav": d["audio"]} for d in dlg])
    voice = [{"name": "every line uses its character's own voice", "ok": all(d["character"] in profiles for d in dlg),
              "detail": ", ".join(sorted({f"{k}→{v.get('logical_id') or v.get('voice_id')}" for k, v in profiles.items()}))},
             {"name": "each character sounds the same in every line", "ok": not ident["drifting_lines"],
              "detail": f"pitch per character {ident['median_hz']}; drifting: {len(ident['drifting_lines'])}"},
             {"name": "characters' voices are distinct", "ok": not ident["too_alike"],
              "detail": f"too alike: {ident['too_alike']}" if ident["too_alike"] else "all distinct"}]
    # MUSIC & MIX
    mx = master.get("mix") or {}
    dvm, after = mx.get("dialogue_over_music") or {}, (mx.get("after") or {})
    cues = master.get("music") or []
    music = [{"name": "music follows the scenes' emotions (not one track)", "ok": len(cues) >= 2 or len(shots) < 6,
              "detail": " → ".join(c["emotion"] for c in cues)},
             {"name": "dialogue clearly above the music", "ok": dvm.get("ok"),
              "detail": f"voices {dvm.get('min_db')} dB (min) / {dvm.get('median_db')} dB (median) above the music"},
             {"name": "loudness on target", "ok": bool(after) and abs(after.get("input_i", 0) - TARGET_LUFS) <= 1.5
              and after.get("input_tp", 0) <= TARGET_TP + 0.5,
              "detail": f"{after.get('input_i')} LUFS, true peak {after.get('input_tp')} dBTP" if after else "not measured"},
             {"name": "sound effects / foley", "ok": None,
              "detail": "ambience per place is mixed; spot foley (footsteps, door, cup) has no library yet"}]
    subs = {round(x["start"], 2): x for x in master.get("subtitles") or []}
    sync = sync_checks(dlg, [{"kind": "shot", **s, "subtitle": (subs.get(round(s["start"], 2)) or {}).get("text"),
                              "speech": (round(subs[round(s["start"], 2)]["end"] - s["start"], 3)
                                         if round(s["start"], 2) in subs else None)} for s in shots])
    return report({"lipsync": lips, "movement": moves, "voice": voice, "music": music, "sync": sync})


def jsonable(o: Any) -> Any:
    """json.dumps default for NumPy numbers and booleans in reports."""
    return o.item() if hasattr(o, "item") else str(o)
