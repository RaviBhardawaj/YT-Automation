# External dependency register

Everything the studio needs from outside this repository, how it is pinned, and what replaces it if it goes away.
Checked 31 Oct 2026.

## Python packages (app) — `requirements.txt` / `requirements-lock.txt`
Pinned to exact versions; installed from PyPI at setup time only. Nothing is downloaded or executed from GitHub at
runtime. Key packages: fastapi, uvicorn, Jinja2, httpx, pydantic, anthropic (SDK; the paid provider stays off),
Pillow, keyring, feedparser, piper-tts, kokoro-onnx, numpy, faster-whisper, opencv-python-headless.
Separate environments (own locks): `.venv-flux` (mflux), `.venv-voiceclone` (Chatterbox), `.venv-parler`, and
`~/sadtalker/.venv` (`install/vendor/sadtalker/requirements-lock.txt`).

## System tools
| Tool | Source | Pinned | If unavailable |
|---|---|---|---|
| Python 3.12 | Homebrew | major version | any 3.12 build |
| FFmpeg 9.0.2 | Homebrew | installed binary | any recent FFmpeg; path in `config.FFMPEG` |
| libraqm | Homebrew | — | Hindi text shaping degrades |

## Models (downloaded once, kept on disk)
| Model | Where it came from | Kept at | Pinned / verified | Fallback |
|---|---|---|---|---|
| Kokoro v1.0 (int8 + fp32) | GitHub release `thewh1teagle/kokoro-onnx` model-files-v1.0 (also on HF `hexgrad/Kokoro-82M`) | `data/voices/kokoro` | file sizes checked by the app | Piper voices; model vault |
| Piper voices | HF `rhasspy/piper-voices` | `data/voices/piper` | — | Kokoro |
| faster-whisper small | HF `Systran/faster-whisper-small` | `data/models/whisper` | HF cache | re-download / model vault |
| FLUX.2 klein 4B 4-bit | HF `Runpod/FLUX.2-klein-4B-mflux-4bit` | `~/.cache/huggingface/hub` | HF cache | online FLUX, stock pictures |
| Chatterbox | HF `ResembleAI/chatterbox` | `~/.cache/huggingface/hub` | HF cache | Kokoro |
| SadTalker code | GitHub `OpenTalker/SadTalker` @ `cd4c0465` | **vendored**: `install/vendor/sadtalker/` (patched source) | commit + patch file | none needed (in this repo) |
| SadTalker weights | HF `vinthony/SadTalker-V002rc` @ `df6f230f`, `camenduru/SadTalker` @ `7caf8d94`, `vinthony/SadTalker` @ `4aedd064` | `~/sadtalker`, `~/.cache/torch/hub` | SHA-256 in `weights.sha256` | model vault; presenter stays a still |

## Online services (runtime)
| Service | Used for | If it fails |
|---|---|---|
| Ollama Cloud, Gemini API, Cloudflare Workers AI | script writing / review | next free writer; then wait 30 min and retry |
| Cloudflare / HF FLUX Space | pictures | local FLUX, stock |
| HF ZeroGPU Spaces (Wan 2.2 `zerogpu-aoti/wan2-2-fp8da-aoti-faster`, LTX-2 `alexnasa/ltx-2-TURBO`) | motion, talking clips | labelled camera move; SadTalker on the Mac |
| Pixabay, Openverse, Wikimedia | stock pictures/clips | other stock, AI pictures, original cards |
| YouTube Data / Analytics APIs, Google OAuth | upload, stats | uploads wait and retry; videos stay downloadable |
| Google Trends / News RSS, Wikipedia | research | other sources; news channel skips a slot rather than invent a story |
| Sarvam (paid) | Hindi voices | Kokoro (already the fallback; out of credits) |

## Reference repository
`darkzOGx/youtube-automation-agent` (MIT) — **not a dependency**. Nothing from it is imported, executed or
downloaded; two analyses were re-implemented in Python (see `OPEN_SOURCE_LICENSE_AUDIT.md`).
