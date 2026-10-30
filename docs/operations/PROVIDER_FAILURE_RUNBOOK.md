# Provider failure runbook

First look: dashboard → **AI providers** (`/providers`) shows every writer, picture/video/voice provider and
platform with "ready / not now" and the reason. Activity log shows the exact error per video.

| Symptom | Likely cause | The studio does | You can |
|---|---|---|---|
| "All writers used up — waiting" | Ollama Cloud monthly limit, Gemini daily limits, Cloudflare neurons spent | waits 30 min, retries; Gemini models reset at midnight Pacific, Cloudflare 05:30 IST | pull a local Ollama model (~6 GB) for an unlimited writer; add a free Groq/OpenRouter key (not wired yet) |
| A writer shows "no … API key" | key missing or removed | skips it | Settings → Credentials |
| Gemini answers HTTP 500 | model busy | moves to the next free model | nothing |
| Pictures fall back to stock/cards | Cloudflare/HF allowances spent | local FLUX on the Mac (~1–2 min each) | wait for the reset |
| Film key pictures ignore the cast | reference-capable provider (Cloudflare) spent | draws from the written description; logged | retry those shots after the reset |
| "Free GPU minutes used up" | ZeroGPU daily quota | camera moves (labelled); talking via SadTalker | wait for the reset time shown |
| Talking face fails on the Mac | SadTalker uninstalled / memory / ffmpeg | keeps the still; reason logged | Storage page memory; `install/install-sadtalker.sh` |
| Upload "not started: … login" | YouTube token expired or revoked | video stays approved, nothing sent | Channels → Connect YouTube again |
| "YouTube API quota exhausted" | 10,000 units/day used | retries after 6 h | wait; uploads cost ~1,600 units each |
| "daily upload limit reached" | YouTube's per-channel limit | retries after 12 h | wait |
| Space "interface changed" | a Hugging Face Space was updated | refuses to call it; fallback | update the adapter (`film_studio/talking.py`, `zerogpu.py`) |

Never done automatically: switching to a paid provider, creating accounts, rotating accounts or proxies to get
more quota, posting comments.
