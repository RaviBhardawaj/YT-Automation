# Open-source licence audit

Checked 31 Oct 2026 from each project's licence file or model card as recorded in the app (`film_studio/providers.py`
keeps per-provider licence + check date). "Recheck" = confirm on the official page before relying on it
commercially. This is an engineering record, not legal advice.

## Code adapted into this repository
| Source | Licence | What was taken | How |
|---|---|---|---|
| `darkzOGx/youtube-automation-agent` @ e973d51 (`utils/scene-retention-engine.js`) | MIT | scene mapping of the retention curve, signal thresholds | re-implemented in Python: `studio/youtube/retention.py` (attribution in the module docstring) |
| same (`utils/audience-engagement-service.js`) | MIT | comment flags, quarantine rule, reply-eligibility and prompt rules | re-implemented: `studio/youtube/comments.py`; posting deliberately left out |

## Code redistributed in this repository
| Source | Licence | Where | Obligations met |
|---|---|---|---|
| SadTalker @ cd4c0465 (OpenTalker) | Apache-2.0 | `install/vendor/sadtalker/` | licence file included (`LICENSE-SadTalker`); our changes listed in `local-patches.diff` |

## Libraries and tools (used, not redistributed)
| Component | Licence | Commercial use |
|---|---|---|
| FastAPI, pydantic, httpx, uvicorn, Jinja2, numpy, Pillow, keyring, feedparser | MIT / BSD | yes |
| OpenCV (headless) | Apache-2.0 | yes |
| PyTorch, torchvision | BSD-3 | yes |
| face_alignment | BSD-3 | yes |
| facexlib | MIT | yes |
| kornia | Apache-2.0 | yes |
| librosa | ISC | yes |
| FFmpeg (Homebrew build) | GPL (with x264) | yes — used as a separate program, not linked |
| Lucide icons (dashboard) | ISC | yes |

## Models
| Model | Licence | Commercial use | Notes |
|---|---|---|---|
| Kokoro-82M | Apache-2.0 | yes | main narration |
| Piper voices | varies per voice | Hindi voices **non-commercial** | not used for Hindi on monetized channels |
| faster-whisper small (Whisper) | MIT | yes (recheck) | |
| FLUX.1 schnell, FLUX.2 klein 4B | Apache-2.0 | yes | |
| Chatterbox Multilingual | MIT | yes | voice cloning only with the speaker's consent |
| Indic Parler-TTS | Apache-2.0 (recheck) | recheck | |
| SadTalker weights | released with Apache-2.0 code; no separate model licence stated | recheck | |
| Wan 2.2 | Apache-2.0 (recheck) | recheck | via free Space |
| LTX-2 | LTX-2 Community License | free under US$10M annual revenue | via free Space |
| Sarvam Bulbul | Sarvam terms (paid) | per terms | out of credits |

## Excluded on licence/privacy/cost grounds
Reference repo's paid video providers (Seedance, MiniMax, Kling via Replicate, Google Omni), Azure Speech,
anonymous telemetry, the "darkzseo" discoverability service.
