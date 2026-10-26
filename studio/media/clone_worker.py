"""Voice-clone helper process (runs inside .venv-voiceclone, not the app's environment).

Engine: Resemble AI Chatterbox Multilingual (MIT licence, 23 languages incl. Hindi). Every clip it makes carries
Resemble's inaudible Perth watermark, which marks it as AI-generated; it is left on.

Protocol: one JSON request per stdin line {"text", "lang", "ref", "out"}; the model is loaded once, the
speaker's conditioning is cached per reference file, the WAV is written to "out", and one JSON reply line is
printed {"ok", "sr", "samples"} or {"ok": false, "error"}. Text is passed as data to the model — never to a shell.
A request {"download": true} only fetches the model weights and replies.
"""
from __future__ import annotations

import json
import socket
import sys

_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_first(*args, **kwargs):                 # this Mac's network advertises IPv6 it can't route
    res = _orig_getaddrinfo(*args, **kwargs)
    v4 = [r for r in res if r[0] == socket.AF_INET]
    return v4 or res


socket.getaddrinfo = _ipv4_first


def main() -> None:
    out = sys.stdout                                 # replies only: libraries print chatter to stdout
    sys.stdout = sys.stderr

    def reply(obj: dict) -> None:
        print(json.dumps(obj), file=out, flush=True)

    import numpy as np
    import soundfile as sf
    import torch
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    try:
        model = ChatterboxMultilingualTTS.from_pretrained(device=device)
    except Exception:                               # some ops are not on MPS in every torch build
        device = "cpu"
        model = ChatterboxMultilingualTTS.from_pretrained(device=device)
    sr = model.sr
    reply({"ready": True, "device": device, "sr": sr})
    current_ref = None
    for line in sys.stdin:
        try:
            req = json.loads(line)
            if req.get("download"):
                reply({"ok": True})
                continue
            if req["ref"] != current_ref:
                model.prepare_conditionals(req["ref"], exaggeration=float(req.get("exaggeration", 0.5)))
                current_ref = req["ref"]
            with torch.no_grad():
                wav = model.generate(req["text"], language_id=req.get("lang", "en"),
                                     cfg_weight=float(req.get("cfg_weight", 0.5)),
                                     temperature=float(req.get("temperature", 0.8)))
            audio = wav.squeeze().detach().cpu().numpy().astype(np.float32)
            sf.write(req["out"], np.clip(audio, -1, 1), sr, subtype="PCM_16")
            reply({"ok": True, "sr": sr, "samples": int(audio.shape[-1])})
        except Exception as e:  # report and keep serving
            reply({"ok": False, "error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()
