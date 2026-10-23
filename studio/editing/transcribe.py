"""Speech-to-text for uploaded media with faster-whisper (runs locally, no per-minute cost).

The model downloads once (about 460 MB for 'small') into data/models/whisper. Transcripts are cached on the
media row, so re-editing the same upload never transcribes it again.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any

from .. import config, db, logs, settings

log = logging.getLogger(__name__)
_lock = threading.Lock()
_model: Any = None
_model_name: str | None = None


def _load():
    global _model, _model_name
    name = settings.get("transcribe.model") or "small"
    if _model is None or _model_name != name:
        from faster_whisper import WhisperModel
        root = config.DATA_DIR / "models" / "whisper"
        root.mkdir(parents=True, exist_ok=True)
        try:                                       # use the downloaded copy without contacting the model hub
            _model = WhisperModel(name, device="cpu", compute_type="int8", download_root=str(root),
                                  local_files_only=True)
        except Exception:                          # first use: download it (about 460 MB for 'small')
            from .. import net
            net.configure_ip_preference()
            _model = WhisperModel(name, device="cpu", compute_type="int8", download_root=str(root))
        _model_name = name
    return _model


def _pcm16k(path: str):
    """Mono 16 kHz float audio decoded by FFmpeg (avoids depending on PyAV's API)."""
    import subprocess

    import numpy as np
    r = subprocess.run([config.FFMPEG, "-hide_banner", "-loglevel", "error", "-i", path, "-vn", "-ac", "1", "-ar",
                        "16000", "-f", "f32le", "-"], capture_output=True, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError("could not decode the audio for transcription")
    return np.frombuffer(r.stdout, dtype=np.float32).copy()


def transcribe(media: dict[str, Any], language: str | None = None) -> list[dict[str, Any]]:
    """[{start, end, text, words:[{start,end,word}]}] in source seconds. Cached per media id."""
    if media.get("transcript") is not None:
        return media["transcript"]
    if not media.get("has_audio"):
        db.update("media", media["id"], {"transcript": "[]"})
        return []
    # The spoken language is detected from the audio: the job's output language can differ from the source's.
    lang = None
    with _lock:                                    # one transcription at a time (CPU and memory heavy)
        model = _load()
        segments, info = model.transcribe(_pcm16k(media["path"]), language=lang, vad_filter=True,
                                          word_timestamps=True, beam_size=5)
        out = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip(),
                "words": [{"start": round(w.start, 2), "end": round(w.end, 2), "word": w.word}
                          for w in (s.words or [])]} for s in segments if s.text.strip()]
    db.update("media", media["id"], {"transcript": json.dumps(out, ensure_ascii=False)})
    logs.event("info", "editing", f"Transcribed media #{media['id']}: {len(out)} segments, "
               f"language {info.language} ({info.language_probability:.0%})")
    media["transcript"] = out
    return out
