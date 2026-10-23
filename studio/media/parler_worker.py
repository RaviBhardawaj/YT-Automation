"""Indic Parler-TTS helper process (runs inside .venv-parler, not the app's environment).

Protocol: one JSON request per stdin line {"text", "speaker", "style", "out"}; the model is loaded
once, the WAV is written to "out", and one JSON reply line is printed {"ok", "sr", "samples"} or
{"ok": false, "error"}. Text is passed as data to the tokenizer — never to a shell.
"""
from __future__ import annotations

import json
import sys

MODEL = "ai4bharat/indic-parler-tts"


def main() -> None:
    import numpy as np
    import soundfile as sf
    import torch
    from parler_tts import ParlerTTSForConditionalGeneration
    from transformers import AutoTokenizer

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = ParlerTTSForConditionalGeneration.from_pretrained(MODEL).to(device)
    tok = AutoTokenizer.from_pretrained(MODEL)
    desc_tok = AutoTokenizer.from_pretrained(model.config.text_encoder._name_or_path)
    sr = model.config.sampling_rate
    print(json.dumps({"ready": True, "device": device, "sr": sr}), flush=True)
    for line in sys.stdin:
        try:
            req = json.loads(line)
            desc = desc_tok(req["style"], return_tensors="pt").to(device)
            prompt = tok(req["text"], return_tensors="pt").to(device)
            with torch.no_grad():
                gen = model.generate(input_ids=desc.input_ids, attention_mask=desc.attention_mask,
                                     prompt_input_ids=prompt.input_ids, prompt_attention_mask=prompt.attention_mask)
            audio = gen.cpu().numpy().squeeze().astype(np.float32)
            sf.write(req["out"], audio, sr, subtype="PCM_16")
            print(json.dumps({"ok": True, "sr": sr, "samples": int(audio.shape[-1])}), flush=True)
        except Exception as e:  # report and keep serving
            print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}), flush=True)


if __name__ == "__main__":
    main()
