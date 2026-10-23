"""Local text-to-speech: Kokoro (Apache-2.0 weights) and Piper (licence varies per voice).

Text is passed to each engine's Python API — never through a shell. Each scene is
synthesised sentence by sentence so captions get exact sentence timings.
"""
from __future__ import annotations

import hashlib
import logging
import re
import shutil
import threading
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np

from .. import config, db, net, settings

log = logging.getLogger(__name__)
_lock = threading.Lock()          # espeak-ng keeps process-global state
_loaded: dict[str, Any] = {}

KOKORO_DIR = config.VOICES_DIR / "kokoro"
PIPER_DIR = config.VOICES_DIR / "piper"
KOKORO_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
KOKORO_MODELS = {
    "int8": ("kokoro-v1.0.int8.onnx", 92_361_271),
    "fp32": ("kokoro-v1.0.onnx", 325_532_387),
}
KOKORO_VOICES_FILE = ("voices-v1.0.bin", 28_214_398)
KOKORO_LICENSE = {"license": "Apache-2.0 (hexgrad/Kokoro-82M weights)", "commercial": True,
                  "url": "https://huggingface.co/hexgrad/Kokoro-82M"}
KOKORO_LANGS = {"a": ("en-us", "English (US)"), "b": ("en-gb", "English (UK)"), "h": ("hi", "Hindi"),
                "e": ("es", "Spanish"), "f": ("fr-fr", "French"), "i": ("it", "Italian"),
                "j": ("ja", "Japanese"), "p": ("pt-br", "Portuguese (BR)"), "z": ("cmn", "Mandarin")}
PIPER_INDEX = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
SAMPLE_TEXT = {
    "hi": "नमस्ते! यह इस आवाज़ का एक छोटा सा नमूना है। क्या यह आपके चैनल के लिए सही लगती है?",
    "en": "Hello! This is a short sample of this voice. Does it sound right for your channel?",
}
SENTENCE_SPLIT = re.compile(r"(?<=[.!?।॥])\s+")


# ------------------------------------------------------------------ espeak data (short path)

def _espeak_dirs() -> dict[str, Path]:
    import espeakng_loader
    import piper
    return {
        "kokoro": Path(espeakng_loader.get_data_path()),
        "piper": Path(piper.__file__).parent / "espeak-ng-data",
    }


def ensure_espeak_data() -> dict[str, Path]:
    """Copy each engine's espeak-ng data to a short path (espeak truncates long ones)."""
    out = {}
    for engine, src in _espeak_dirs().items():
        dst = config.SHORT_CACHE_DIR / ("espeak-ng-data" if engine == "kokoro" else "piper-espeak-ng-data")
        marker = dst / ".source"
        stamp = f"{src}:{(src / 'phontab').stat().st_size if (src / 'phontab').exists() else 0}"
        if not (dst / "phontab").exists() or not marker.exists() or marker.read_text() != stamp:
            if dst.exists() and not dst.is_symlink():
                shutil.rmtree(dst)
            elif dst.is_symlink():
                dst.unlink()
            shutil.copytree(src, dst)
            marker.write_text(stamp)
        out[engine] = dst
    return out


# ------------------------------------------------------------------ Kokoro

def kokoro_paths() -> tuple[Path, Path]:
    variant = settings.get("voice").get("kokoro_model", "int8")
    name = KOKORO_MODELS.get(variant, KOKORO_MODELS["int8"])[0]
    return KOKORO_DIR / name, KOKORO_DIR / KOKORO_VOICES_FILE[0]


def kokoro_installed() -> bool:
    m, v = kokoro_paths()
    return m.exists() and v.exists()


def kokoro_download(variant: str = "int8") -> None:
    name, size = KOKORO_MODELS[variant]
    for fname, expected in ((name, size), KOKORO_VOICES_FILE):
        dest = KOKORO_DIR / fname
        if dest.exists() and dest.stat().st_size == expected:
            continue
        net.download(KOKORO_RELEASE + fname, dest, max_bytes=expected + 1_000_000,
                     allowed_types=("application/octet-stream", "binary/octet-stream", "application/x-"))
        if dest.stat().st_size != expected:
            dest.unlink(missing_ok=True)
            raise net.FetchError(f"{fname}: size mismatch")
    settings.merge("voice", {"kokoro_model": variant})


def _kokoro():
    key = "kokoro:" + str(kokoro_paths()[0])
    if key not in _loaded:
        import espeakng_loader
        from kokoro_onnx import Kokoro
        from kokoro_onnx.config import EspeakConfig
        data = ensure_espeak_data()["kokoro"]
        m, v = kokoro_paths()
        _loaded[key] = Kokoro(str(m), str(v), espeak_config=EspeakConfig(
            lib_path=espeakng_loader.get_library_path(), data_path=str(data)))
    return _loaded[key]


def kokoro_voices() -> list[dict[str, Any]]:
    if not kokoro_installed():
        return []
    try:
        with _lock:
            names = _kokoro().get_voices()
    except Exception:
        log.exception("kokoro voices unavailable")
        return []
    out = []
    for n in sorted(names):
        lang_code, lang_label = KOKORO_LANGS.get(n[0], ("en-us", n[0]))
        gender = {"f": "female", "m": "male"}.get(n[1], "")
        out.append({"engine": "kokoro", "id": n, "label": f"{n.split('_', 1)[-1].title()} — {lang_label}, {gender}",
                    "lang": lang_code, **KOKORO_LICENSE})
    return out


# ------------------------------------------------------------------ Piper

def piper_catalog() -> dict[str, Any]:
    cache = PIPER_DIR / "voices.json"
    if not cache.exists() or time.time() - cache.stat().st_mtime > 7 * 86400:
        PIPER_DIR.mkdir(parents=True, exist_ok=True)
        data = net.get_json(PIPER_INDEX + "voices.json", max_bytes=5_000_000)
        cache.write_text(db.dumps(data))
    return db.loads(cache.read_text(), {})


def piper_card(key: str) -> dict[str, Any]:
    """Fetch and interpret a Piper voice's MODEL_CARD (training-data licence)."""
    cat = piper_catalog().get(key)
    if not cat:
        raise KeyError(key)
    card_path = PIPER_DIR / "_cards" / f"{key}.txt"
    if not card_path.exists():
        rel = next(f for f in cat["files"] if f.endswith("MODEL_CARD"))
        _, text = net.get_text(PIPER_INDEX + rel, respect_robots=False)
        card_path.parent.mkdir(parents=True, exist_ok=True)
        card_path.write_text(text)
    text = card_path.read_text()
    return {**license_from_card(text), "card": text[:3000]}


def license_from_card(text: str) -> dict[str, Any]:
    """Interpret the dataset licence line of a Piper MODEL_CARD. None = needs human review."""
    m = re.search(r"License:\s*(.+)", text)
    lic = (m.group(1).strip() if m else "unknown")
    low = lic.lower()
    if re.search(r"\bnc\b|by-nc|non-?commercial|noncommercial", low) or "indictts" in low or "blizzard" in low:
        commercial: bool | None = False
    elif re.search(r"public domain|cc0|cc[- ]by(-sa)?[- ]\d|creativecommons\.org/licenses/by(-sa)?/|mit|apache", low):
        commercial = True
    else:
        commercial = None
    return {"license": lic, "commercial": commercial}


def piper_installed() -> list[dict[str, Any]]:
    out = []
    if not PIPER_DIR.exists():
        return out
    for d in sorted(p for p in PIPER_DIR.iterdir() if p.is_dir() and not p.name.startswith("_")):
        onnx = d / f"{d.name}.onnx"
        if onnx.exists() and (d / f"{d.name}.onnx.json").exists():
            if (d / "meta.json").exists():
                meta = db.loads((d / "meta.json").read_text(), {})
            elif (d / "MODEL_CARD").exists():
                meta = license_from_card((d / "MODEL_CARD").read_text())
            else:
                meta = {}
            lang = d.name.split("-")[0]
            out.append({"engine": "piper", "id": d.name, "label": f"{d.name} (Piper)",
                        "lang": "hi" if lang.startswith("hi") else lang.split("_")[0],
                        "license": meta.get("license", "see MODEL_CARD"), "commercial": meta.get("commercial"),
                        "url": f"https://huggingface.co/rhasspy/piper-voices/tree/main/{lang.split('_')[0]}/{lang}"})
    return out


def piper_download(key: str) -> dict[str, Any]:
    cat = piper_catalog().get(key)
    if not cat:
        raise KeyError(f"unknown Piper voice {key}")
    dest_dir = PIPER_DIR / key
    for rel, info in cat["files"].items():
        name = rel.rsplit("/", 1)[-1]
        dest = dest_dir / name
        if dest.exists() and dest.stat().st_size == info["size_bytes"]:
            continue
        net.download(PIPER_INDEX + rel, dest, max_bytes=info["size_bytes"] + 1024, allowed_types=())
        digest = hashlib.md5(dest.read_bytes()).hexdigest()
        if info.get("md5_digest") and digest != info["md5_digest"]:
            dest.unlink(missing_ok=True)
            raise net.FetchError(f"{name}: checksum mismatch")
    card = piper_card(key)
    (dest_dir / "meta.json").write_text(db.dumps({"license": card["license"], "commercial": card["commercial"]}))
    return card


def _piper(voice_id: str):
    key = "piper:" + voice_id
    if key not in _loaded:
        from piper import PiperVoice
        data = ensure_espeak_data()["piper"]
        _loaded[key] = PiperVoice.load(str(PIPER_DIR / voice_id / f"{voice_id}.onnx"), espeak_data_dir=str(data))
    return _loaded[key]


# ------------------------------------------------------------------ synthesis

# ------------------------------------------------------------------ Indic Parler-TTS (separate environment)

PARLER_PY = config.ROOT / ".venv-parler" / "bin" / "python"
PARLER_MODEL = "ai4bharat/indic-parler-tts"
PARLER_SPEAKERS = {"rohit": "Rohit", "divya": "Divya", "aman": "Aman", "rani": "Rani"}   # recommended Hindi speakers
PARLER_STYLE = ("{name} speaks in a lively, confident and friendly voice at a moderate pace, like a TV news presenter, "
                "with clear expression. The recording is very clear with no background noise, and the speaker sounds "
                "close to the microphone.")
_parler: dict[str, Any] = {}


def parler_ready() -> bool:
    """Environment installed and the gated model downloaded (accepted on Hugging Face by the user)."""
    cache = Path.home() / ".cache" / "huggingface" / "hub" / "models--ai4bharat--indic-parler-tts" / "snapshots"
    return PARLER_PY.exists() and cache.exists() and any(cache.glob("*/model*.safetensors"))


def parler_voices() -> list[dict[str, Any]]:
    if not parler_ready():
        return []
    return [{"engine": "parler", "id": k, "label": f"{v} (Indic Parler-TTS)", "lang": "hi",
             "license": "Apache-2.0 (ai4bharat/indic-parler-tts)", "commercial": True,
             "url": "https://huggingface.co/ai4bharat/indic-parler-tts"} for k, v in PARLER_SPEAKERS.items()]


def _parler_proc():
    import json
    import subprocess
    p = _parler.get("proc")
    if p is None or p.poll() is not None:
        p = subprocess.Popen([str(PARLER_PY), "-I", str(Path(__file__).with_name("parler_worker.py"))],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                             env={"HF_HUB_OFFLINE": "1", "PATH": "/usr/bin:/bin", "HOME": str(Path.home()),
                                  "PYTORCH_ENABLE_MPS_FALLBACK": "1"})
        hello = json.loads(p.stdout.readline() or "{}")
        if not hello.get("ready"):
            raise RuntimeError("Indic Parler-TTS helper did not start (is the model downloaded?)")
        _parler["proc"] = p
    return p


def _parler_say(text: str, speaker: str) -> tuple[np.ndarray, int]:
    import json
    import tempfile
    p = _parler_proc()
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        out = Path(tmp.name)
    try:
        style = PARLER_STYLE.format(name=PARLER_SPEAKERS.get(speaker, "Rohit"))
        p.stdin.write(json.dumps({"text": text, "style": style, "out": str(out)}, ensure_ascii=False) + "\n")
        p.stdin.flush()
        reply = json.loads(p.stdout.readline() or "{}")
        if not reply.get("ok"):
            raise RuntimeError(f"Indic Parler-TTS failed: {reply.get('error', 'no reply')}")
        with wave.open(str(out), "rb") as w:
            return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).copy(), w.getframerate()
    finally:
        out.unlink(missing_ok=True)


# ------------------------------------------------------------------ Sarvam Bulbul (paid cloud API)

SARVAM_URL = "https://api.sarvam.ai/text-to-speech"
SARVAM_LICENCE = "Sarvam AI production licence for generated audio (paid per character)"
# Shortlist for a Hinglish market show (the full bulbul:v3 list has 36 speakers)
SARVAM_SPEAKERS = {"shubh": "Shubh", "aditya": "Aditya", "rahul": "Rahul", "amit": "Amit", "kabir": "Kabir",
                   "varun": "Varun", "ritu": "Ritu", "priya": "Priya", "neha": "Neha", "kavya": "Kavya"}
SARVAM_RUPEES_PER_10K_CHARS = 30.0          # bulbul:v3 list price (docs.sarvam.ai pricing, Oct 2026)
_sarvam_chars = {"n": 0}


def sarvam_voices() -> list[dict[str, Any]]:
    if not settings.secret("sarvam_api_key"):
        return []
    return [{"engine": "sarvam", "id": k, "label": f"{v} (Sarvam Bulbul)", "lang": "hi", "license": SARVAM_LICENCE,
             "commercial": True, "url": "https://docs.sarvam.ai/api-reference-docs/getting-started/commercial-licensing"}
            for k, v in SARVAM_SPEAKERS.items()]


def _sarvam_say(text: str, speaker: str, lang: str, speed: float) -> tuple[np.ndarray, int]:
    import base64
    import io
    import httpx
    key = settings.secret("sarvam_api_key")
    if not key:
        raise RuntimeError("Sarvam API key missing — add it in Settings › Keys")
    # Decide by the text itself: Hinglish scripts are written in Devanagari; an English script has (almost) none
    letters = [c for c in text if c.isalpha()]
    devanagari = sum(1 for c in letters if "ऀ" <= c <= "ॿ") / len(letters) if letters else 1.0
    body = {"text": text[:2400], "language_code": "hi-IN" if devanagari >= 0.2 else "en-IN",
            "model": settings.get("voice.sarvam_model", "bulbul:v3"), "speaker": speaker,
            "pace": max(0.5, min(2.0, speed)), "speech_sample_rate": 24000, "output_audio_codec": "wav"}
    for attempt in range(5):
        r = httpx.post(SARVAM_URL, json=body, headers={"api-subscription-key": key},
                       timeout=httpx.Timeout(90, connect=15))
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 4:
            time.sleep(2 ** attempt * 2)
            continue
        if r.status_code >= 400:
            msg = (r.json().get("error") or {}).get("message") if "json" in r.headers.get("content-type", "") else r.text
            raise RuntimeError(f"Sarvam TTS HTTP {r.status_code}: {str(msg)[:200]}")
        break
    _sarvam_chars["n"] += len(body["text"])
    wav_bytes = base64.b64decode("".join(r.json()["audios"]))
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        sr, width, ch = w.getframerate(), w.getsampwidth(), w.getnchannels()
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16 if width == 2 else np.int32)
    if width != 2:
        audio = (audio / 65536).astype(np.int16)
    if ch > 1:
        audio = audio.reshape(-1, ch).mean(axis=1).astype(np.int16)
    return audio.copy(), sr


def sarvam_cost_note(chars: int) -> str:
    return f"Sarvam: {chars:,} characters ≈ ₹{chars / 10000 * SARVAM_RUPEES_PER_10K_CHARS:,.1f}"


def all_voices() -> list[dict[str, Any]]:
    return kokoro_voices() + piper_installed() + parler_voices() + sarvam_voices()


def voice_settings(lang: str | None = None) -> dict[str, Any]:
    """Voice for a language: voice.by_language[lang] if set, else the general voice setting."""
    from .. import profile
    lang = lang or profile.language()
    base = settings.get("voice")
    override = (settings.get("voice.by_language") or {}).get(lang)
    return {**base, **override} if override else base


def current_voice(lang: str | None = None) -> dict[str, Any]:
    v = voice_settings(lang)
    match = next((x for x in all_voices() if x["engine"] == v.get("engine") and x["id"] == v.get("voice_id")), None)
    return {**v, **(match or {}), "installed": bool(match)}


_THOUSANDS = re.compile(r"(?<=\d),(?=\d{2,3}\b)")


def spoken_numbers(text: str) -> str:
    """Remove thousands separators before synthesis: espeak's Hindi rules stop reading at the comma
    ('24,591.41' would be spoken as 24,591 and lose the decimals). Captions keep the commas."""
    return _THOUSANDS.sub("", text)


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in SENTENCE_SPLIT.split(text.strip()) if p.strip()]
    return parts or [text.strip()]


def _synth_sentence(text: str, engine: str, voice_id: str, lang: str, speed: float) -> tuple[np.ndarray, int]:
    text = spoken_numbers(text)
    if engine == "sarvam":                     # network call: no espeak state, so no global lock
        return _sarvam_say(text, voice_id, lang, speed)
    with _lock:
        if engine == "parler":
            return _parler_say(text, voice_id)
        if engine == "kokoro":
            kokoro_lang = lang if lang in {c for c, _ in KOKORO_LANGS.values()} else (
                KOKORO_LANGS.get(voice_id[0], ("en-us", ""))[0])
            audio, sr = _kokoro().create(text, voice=voice_id, speed=speed, lang=kokoro_lang, trim=True)
            return (np.clip(audio, -1, 1) * 32767).astype(np.int16), sr
        if engine == "piper":
            from piper import SynthesisConfig
            v = _piper(voice_id)
            chunks = list(v.synthesize(text, syn_config=SynthesisConfig(length_scale=1.0 / max(0.5, speed))))
            if not chunks:
                return np.zeros(1, dtype=np.int16), v.config.sample_rate
            return np.concatenate([np.frombuffer(c.audio_int16_bytes, dtype=np.int16) for c in chunks]), chunks[0].sample_rate
    raise ValueError(f"unknown TTS engine {engine!r}")


def synthesize_scene(narration: str, out_wav: Path, *, sentence_gap: float = 0.16, tail: float = 0.30) -> dict[str, Any]:
    """Write one WAV for a scene. Returns its duration and per-sentence timings (seconds)."""
    v = voice_settings()
    engine, voice_id, speed = v["engine"], v["voice_id"], float(v.get("speed", 1.0))
    lang = current_voice().get("lang", "en-us")
    pieces: list[np.ndarray] = []
    timings: list[dict[str, Any]] = []
    sr = 24000
    cursor = 0
    sentences = split_sentences(narration)
    for i, sentence in enumerate(sentences):
        audio, sr = _synth_sentence(sentence, engine, voice_id, lang, speed)
        start = cursor
        pieces.append(audio)
        cursor += len(audio)
        timings.append({"text": sentence, "start": start / sr, "end": cursor / sr})
        gap = int(sr * (sentence_gap if i < len(sentences) - 1 else tail))
        pieces.append(np.zeros(gap, dtype=np.int16))
        cursor += gap
    samples = np.concatenate(pieces) if pieces else np.zeros(int(sr * tail), dtype=np.int16)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(samples.tobytes())
    return {"duration": len(samples) / sr, "sample_rate": sr, "sentences": timings,
            "engine": engine, "voice": voice_id}


def sample(engine: str, voice_id: str) -> Path:
    voice = next((x for x in all_voices() if x["engine"] == engine and x["id"] == voice_id), None)
    if not voice:
        raise KeyError("voice not installed")
    lang = "hi" if voice["lang"].startswith("hi") else "en"
    out = config.SAMPLES_DIR / f"{engine}_{voice_id}.wav"
    audio, sr = _synth_sentence(SAMPLE_TEXT[lang], engine, voice_id, voice["lang"], 1.0)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(audio.tobytes())
    return out


def engine_status() -> dict[str, Any]:
    v = current_voice()
    return {
        "kokoro_installed": kokoro_installed(),
        "piper_voices": len(piper_installed()),
        "selected": v,
        "ok": bool(v.get("installed")),
        "detail": (f"{v.get('engine')} · {v.get('voice_id')}" + ("" if v.get("installed") else " (not installed)")),
    }


def concat_wavs(paths: list[Path], out: Path) -> float:
    """Concatenate scene WAVs (same engine => same format). Returns duration in seconds."""
    params = None
    frames = 0
    with wave.open(str(out), "wb") as dst:
        for p in paths:
            with wave.open(str(p), "rb") as src:
                if params is None:
                    params = src.getparams()
                    dst.setparams(params)
                elif src.getframerate() != params.framerate:
                    raise ValueError("scene audio sample rates differ")
                data = src.readframes(src.getnframes())
                dst.writeframes(data)
                frames += src.getnframes()
    return frames / params.framerate if params else 0.0
