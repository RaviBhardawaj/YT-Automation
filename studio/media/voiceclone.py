"""Cloned voices: a person's own voice, recorded or uploaded with their consent, narrating the videos they choose.

Engine: Resemble AI Chatterbox Multilingual (MIT licence; Hindi, English and 21 more languages), run locally in its
own environment (.venv-voiceclone) through clone_worker.py, because its torch/numpy pins differ from the app's.
Nothing leaves this computer. Every clip carries Resemble's inaudible Perth watermark (marks it as AI-made).

Each voice belongs to one workspace; only that workspace can see, hear or use it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import wave
from pathlib import Path
from typing import Any

import numpy as np

from .. import config, db, logs

CLONE_PY = config.ROOT / ".venv-voiceclone" / "bin" / "python"
MODEL_CACHE = Path.home() / ".cache" / "huggingface" / "hub" / "models--ResembleAI--chatterbox" / "snapshots"
LICENSE = "MIT (Resemble AI Chatterbox Multilingual) — your own voice, used with your consent"
CONSENT = ("This is my own voice, or I have the speaker's clear permission to clone it. I will only use it for my own "
           "videos and will label AI-voiced content where a platform asks for it.")
MIN_SECONDS, MAX_SECONDS = 10, 30
MAX_BYTES = 15 * 1024 * 1024
LANG_CODES = {"Hindi": "hi", "Hinglish": "hi", "English": "en", "Arabic": "ar", "French": "fr", "German": "de",
              "Spanish": "es", "Portuguese": "pt", "Italian": "it", "Japanese": "ja", "Korean": "ko", "Chinese": "zh",
              "Russian": "ru", "Turkish": "tr", "Dutch": "nl", "Polish": "pl", "Swahili": "sw", "Malay": "ms"}

_proc: dict[str, Any] = {}
_lock = threading.Lock()


class VoiceError(ValueError):
    pass


def installed() -> bool:
    return CLONE_PY.exists()


def model_ready() -> bool:
    return MODEL_CACHE.exists() and any(MODEL_CACHE.glob("*/*.safetensors"))


def status() -> dict[str, Any]:
    return {"installed": installed(), "model": model_ready(), "running": bool(_proc.get("p") and _proc["p"].poll() is None)}


def folder(ws_id: int, voice_id: int) -> Path:
    return config.DATA_DIR / "workspaces" / f"{ws_id:05d}" / "voices" / f"{voice_id:04d}"


# ------------------------------------------------------------------ voices

def add(ws_id: int, user_id: int | None, name: str, language: str, raw: bytes, filename: str, consent: bool) -> int:
    """Save a cleaned reference clip. The person must confirm consent; 10-30 s of clear speech is needed."""
    if not consent:
        raise VoiceError("Please confirm this is your own voice (or that you have the speaker's permission).")
    if not raw:
        raise VoiceError("No audio received.")
    if len(raw) > MAX_BYTES:
        raise VoiceError("The recording is too large (15 MB max) — 10 to 30 seconds is enough.")
    name = (name or "My voice").strip()[:60] or "My voice"
    language = language if language in LANG_CODES else "Hindi"
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / ("in" + (Path(filename or "a.webm").suffix.lower()[:8] or ".bin"))
        src.write_bytes(raw)
        out = Path(tmp) / "sample.wav"
        # mono 24 kHz, leading silence removed, loudness evened out, at most 30 s
        r = subprocess.run([config.FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                            "-vn", "-ac", "1", "-ar", "24000",
                            "-af", "silenceremove=start_periods=1:start_threshold=-45dB,loudnorm=I=-18:TP=-2",
                            "-t", str(MAX_SECONDS), str(out)], capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or not out.exists():
            raise VoiceError("Couldn't read that audio file — try MP3, WAV, M4A or record again.")
        with wave.open(str(out), "rb") as w:
            seconds = w.getnframes() / float(w.getframerate())
            peak = np.abs(np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)).max(initial=0)
        if seconds < MIN_SECONDS:
            raise VoiceError(f"The recording has {seconds:.0f} s of speech — please give at least {MIN_SECONDS} s.")
        if peak < 800:
            raise VoiceError("The recording is almost silent — check the microphone and record again.")
        vid = db.insert("voices", {"workspace_id": ws_id, "user_id": user_id, "name": name, "language": language,
                                   "sample_path": "", "sample_seconds": round(seconds, 1), "consent": CONSENT,
                                   "consent_at": db.iso(), "status": "ready", "created_at": db.iso()})
        d = folder(ws_id, vid)
        d.mkdir(parents=True, exist_ok=True)
        shutil.move(str(out), d / "sample.wav")
    db.update("voices", vid, {"sample_path": str(d / "sample.wav")})
    logs.event("info", "voice", f"Cloned voice '{name}' saved ({seconds:.0f} s sample, consent recorded)")
    return vid


def list_for(ws_id: int) -> list[dict[str, Any]]:
    return db.q("SELECT * FROM voices WHERE workspace_id=? AND status='ready' ORDER BY id DESC", (ws_id,))


def get(voice_id: int | str | None, ws_id: int | None = None) -> dict[str, Any] | None:
    if not str(voice_id or "").isdigit():
        return None
    v = db.q1("SELECT * FROM voices WHERE id=? AND status='ready'", (int(voice_id),))
    if not v or (ws_id is not None and v["workspace_id"] != ws_id):
        return None
    return v


def delete(ws_id: int, voice_id: int) -> None:
    v = get(voice_id, ws_id)
    if not v:
        raise VoiceError("voice not found")
    shutil.rmtree(folder(ws_id, v["id"]), ignore_errors=True)
    db.update("voices", v["id"], {"status": "deleted", "sample_path": ""})
    logs.event("info", "voice", f"Cloned voice '{v['name']}' deleted (sample removed)")


def as_voice(v: dict[str, Any]) -> dict[str, Any]:
    """The voice record pipelines store on a video (same shape as the other engines)."""
    return {"engine": "clone", "voice_id": str(v["id"]), "label": f"{v['name']} (my voice)",
            "lang": LANG_CODES.get(v["language"], "hi"), "license": LICENSE, "commercial": True, "installed": True}


# ------------------------------------------------------------------ speech

def _reply(p) -> dict[str, Any]:
    """Next JSON line from the helper (a stray library message on its output is skipped)."""
    for _ in range(50):
        line = p.stdout.readline()
        if not line:
            return {}
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {}


def _worker():
    p = _proc.get("p")
    if p is None or p.poll() is not None:
        if not installed():
            raise RuntimeError("Voice cloning isn't installed on this computer (Settings › Voice › My voice).")
        from .. import resources
        resources.ensure(4.0, "Your cloned voice (Chatterbox)")     # starting a model: only with room for it
        p = subprocess.Popen([str(CLONE_PY), "-I", str(Path(__file__).with_name("clone_worker.py"))],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                             env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin", "HOME": str(Path.home()),
                                  "PYTORCH_ENABLE_MPS_FALLBACK": "1", "TOKENIZERS_PARALLELISM": "false",
                                  **({"HF_HUB_OFFLINE": "1"} if model_ready() else {})})
        hello = _reply(p)
        if not hello.get("ready"):
            raise RuntimeError("The voice-cloning helper did not start (model download failed?)")
        _proc["p"] = p
    return p


def say(text: str, voice_id: str, lang: str) -> tuple[np.ndarray, int]:
    """One sentence in the cloned voice -> (int16 samples, sample rate)."""
    v = get(voice_id)
    if not v or not Path(v["sample_path"]).is_file():
        raise RuntimeError(f"Cloned voice #{voice_id} was deleted — pick another voice for this video.")
    code = LANG_CODES.get(v["language"], "hi")
    if code in ("hi", "en"):                      # Hindi/English voices speak whichever of the two the line is in
        letters = [c for c in text if c.isalpha()]
        code = "hi" if letters and sum("ऀ" <= c <= "ॿ" for c in letters) / len(letters) >= 0.2 else "en"
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        out = Path(tmp.name)
    try:
        with _lock:
            p = _worker()
            p.stdin.write(json.dumps({"text": text, "lang": code, "ref": v["sample_path"], "out": str(out)},
                                     ensure_ascii=False) + "\n")
            p.stdin.flush()
            reply = _reply(p)
        if not reply.get("ok"):
            raise RuntimeError(f"Voice cloning failed: {reply.get('error', 'no reply')}")
        with wave.open(str(out), "rb") as w:
            return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).copy(), w.getframerate()
    finally:
        out.unlink(missing_ok=True)


def preview(v: dict[str, Any]) -> Path:
    """A short test line in the cloned voice, saved next to the sample."""
    text = {"hi": "नमस्ते! यह मेरी आवाज़ है, और अब मेरे वीडियो इसी आवाज़ में बनेंगे।",
            "en": "Hello! This is my voice, and my videos can now be narrated in it."}
    code = LANG_CODES.get(v["language"], "hi")
    audio, sr = say(text.get(code, text["en"]), str(v["id"]), code)
    out = folder(v["workspace_id"], v["id"]) / "preview.wav"
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(audio.tobytes())
    return out
