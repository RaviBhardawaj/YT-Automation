# AI provider matrix

Budget default: **₹0**. Paid providers stay off unless the owner sets a budget above ₹0 **and** lists the
provider by name (Settings → Film Studio). There is no hidden paid fallback. Accounts, identities or proxies are
never rotated to get around a quota.

Cost classes: `free_local` (runs on this Mac), `free_allowance` (free tier with a published daily/monthly
limit), `paid`. "Checked" is the date the free-tier facts were last read; re-check before relying on them.

## Pictures, video, voice, checks (generated from `studio/film_studio/providers.py`)

| Provider | Capability | Where | Cost class | Free allowance / limit | Licence | Adapter | Checked |
|---|---|---|---|---|---|---|---|
| Cloudflare Workers AI — FLUX.2 klein 4B (`cloudflare_flux`) | image, image_ref | remote | free_allowance | about 10,000 neurons a day (~60–90 images), resets 00:00 UTC | Apache-2.0 | yes | 2026-10-29 |
| FLUX.1 schnell — Hugging Face ZeroGPU Space (`hf_flux`) | image | remote | free_allowance | free ZeroGPU minutes (more with a free Hugging Face token); about 8 s per image | Apache-2.0 | yes | 2026-10-29 |
| FLUX.2 klein 4B on this Mac (mflux) (`local_flux`) | image | local | free_local | slower: about a minute a picture; one at a time | Apache-2.0 | yes | 2026-10-29 |
| Local Stable Diffusion (A1111/Forge/Draw Things API) (`local_sd`) | image | local | free_local | speed depends on the Mac | depends on the checkpoint you load | yes | 2026-10-29 |
| Gemini image generation (`gemini_image`) | image, image_ref | remote | paid | billed per image | Google terms | yes | 2026-10-29 |
| Wan 2.2 TI2V-5B (local, open weights) (`wan22_local`) | video_t2v, video_i2v | local | free_local | a few seconds per clip | Apache-2.0 (recheck before install) | **no — never chosen** | 2026-10-29 |
| Hugging Face ZeroGPU Space (video) (`hf_zerogpu`) | video_t2v, video_i2v | remote | free_allowance | a few GPU-minutes a day on a free account (1–3 short clips); app limit per day in Settings | Wan 2.2: Apache-2.0 (recheck) | yes | 2026-10-29 |
| Gemini Omni Flash (AI video with sound) (`gemini_omni`) | video_t2v, video_i2v, dialogue_video | remote | paid | about $0.10/second at 720p | Google terms | **no — never chosen** | 2026-10-29 |
| LTX-2 talking clips — Hugging Face ZeroGPU Space (`hf_ltx2`) | dialogue_video | remote | free_allowance | shares the free ZeroGPU minutes: about 2 three-second pieces a day | LTX-2 Community (free under $10M revenue) | yes | 2026-10-30 |
| SadTalker talking face on this Mac (`sadtalker_local`) | dialogue_video | local | free_local | face only (lips, expression, blinks); about 1 minute per second of speech | Apache-2.0 | yes | 2026-10-30 |
| Camera move on a still (FFmpeg) (`ffmpeg_camera`) | camera_move | local | free_local | no real motion — labelled as a still with camera move | LGPL/GPL (FFmpeg) | yes | 2026-10-29 |
| Kokoro voices (Hindi + English) (`kokoro`) | tts | local | free_local | 2 male + 2 female Hindi voices | Apache-2.0 | yes | 2026-10-29 |
| Sarvam Bulbul voices (`sarvam`) | tts | remote | paid | billed per character | Sarvam terms | yes | 2026-10-29 |
| Chatterbox voice cloning (with consent) (`chatterbox`) | voice_clone, tts | local | free_local | needs a consented voice sample | MIT | yes | 2026-10-29 |
| Whisper small (speech-to-text, offline) (`whisper`) | stt | local | free_local |  | MIT (recheck) | yes | 2026-10-29 |
| Gemini Flash-Lite picture checks (`gemini_vision`) | vision_check | remote | free_allowance | free tier, paced to ~14/min | Google terms | yes | 2026-10-29 |
| Ollama Cloud vision checks (`ollama_vision`) | vision_check | remote | free_allowance | plan's included usage | Gemma terms | yes | 2026-10-29 |

Routing (`providers.choose` / `candidates`): free local first, then free allowances with capacity, paid only
when allowed; entries **without an adapter are never chosen**; a provider marked `last_resort` is tried after
the online allowances. When no free provider can make real video, the shot becomes a labelled still with a camera
move — never reported as video. Attempts, successes and failures are recorded in `ai_usage`; the video budget
shows clips made, attempts and failed tries separately.

## Script writers and reviewers (`studio/ai/router.py`, `providers.py`)

| Order | Provider | Model(s) | Cost | Limit (as observed 30 Oct 2026) |
|---|---|---|---|---|
| 1 | Ollama on this Mac | qwen3.5:9b | free_local | no model downloaded (~6 GB over the hotspot — not pulled) |
| 2 | Ollama Cloud | gemma4:31b | free_allowance | monthly allowance; used up in Oct 2026 |
| 3 | Gemini API | rotates gemini-3.5-flash, gemini-3-flash-preview, gemini-3.5-flash-lite, gemini-3.1-flash-lite, gemini-flash-lite-latest, gemma-4-31b-it, gemma-4-26b-a4b-it | free_allowance | each model has its own small daily limit; a 429/404 parks that model for the Pacific day |
| 4 | Cloudflare Workers AI | Llama 3.3 70B | free_allowance | 10,000 neurons/day, shared with Cloudflare FLUX pictures |
| — | Claude API | Claude Opus | paid | key saved; skipped at ₹0 |

When every writer is used up the job **waits 30 minutes and retries** (`AllProvidersFailed.temporary`).

## Platform APIs

| API | Use | Limit |
|---|---|---|
| YouTube Data API v3 (OAuth per channel) | upload, thumbnails, captions, channel info, research | 10,000 units/day per Google project |
| YouTube Analytics API | views, watch time, average view %, subscribers gained | — (impressions/CTR not requested) |
| Google Trends RSS, Google News RSS, Wikipedia, Hacker News | research evidence | free, no key |
| Meta Graph API | Facebook/Instagram posting | adapter present, not connected |

Terms to respect: YouTube API Services Terms (https://developers.google.com/youtube/terms/api-services-terms-of-service),
Hugging Face ZeroGPU fair use, each model's licence (column above).
