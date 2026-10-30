# Provider fallback matrix

Live status of every entry: dashboard → **AI providers** (`/providers`). Paid providers are never used unless the
owner explicitly configures them (Claude as a writer; Film Studio budget above ₹0 plus the provider listed by name).
Accounts, identities or proxies are never rotated to get around a quota.

| Capability | 1st | 2nd | 3rd | Last resort | When everything fails |
|---|---|---|---|---|---|
| Script / review (`ai/router.py`) | Ollama local (model not pulled) | Ollama Cloud | Gemini — rotates 7 free models, parks a model for the day on 429/404 | Cloudflare Llama 3.3 70B | job waits 30 min and retries (`AllProvidersFailed.temporary`); completed stages kept |
| Scene pictures (`media/visuals.py`) | licensed stock with vision check | Cloudflare FLUX | HF FLUX Space | local FLUX on the Mac | original text card (labelled) |
| Film key pictures (`creative/film._gen`) | Cloudflare FLUX (with cast refs) | HF FLUX | local SD server | local FLUX | shot waits; film resumes after reset |
| Real motion (`film_studio/providers.choose`) | HF ZeroGPU Wan 2.2 | — | — | — | still with camera move, **labelled** `camera_move`, never called video |
| Talking clips | LTX-2 Space (≈2 pieces/day) | SadTalker on the Mac | — | — | still presenter / shot; reason logged |
| Narration | channel voice (Kokoro / clone / Sarvam if credits) | Kokoro | Piper | — | job fails with the reason (no silent video) |
| Speech-to-text | faster-whisper (local) | — | — | — | Upload & Edit / clipper wait |
| Vision checks | Gemini Flash-Lite | Ollama Cloud vision | — | — | check reported "not checked" (never "passed") |
| Research | Google News, Wikipedia, YouTube search, RSS, Ollama web search | — | — | — | fewer sources; news slot skipped rather than invented |
| Publishing | YouTube API (resumable) | — | — | — | upload waits (quota/upload limit back-off); video stays downloadable |
| Analytics | YouTube Analytics API | Data API public stats | — | — | numbers shown as "not available", never estimated |

## Failure types and handling
| Failure | Detected as | Handling | Test |
|---|---|---|---|
| quota used up | HTTP 429 / provider message / remembered reset time | skip provider until reset; next provider | `test_ai_cloudflare.GeminiRotationTests`, `test_talking`, `test_film_phases` |
| invalid / missing key | `available()` false | provider skipped, reason shown on `/providers`; job waits | `test_failures.KeyTests`, `test_provider_status` |
| expired YouTube login | OAuth refresh fails | upload not started; video stays approved with the reason | `test_failures.ExpiredLoginTests` |
| timeout / stalled connection | 300 s cloud timeout, IPv4 preferred | retried with back-off | `test_failures.TimeoutTests` |
| provider shut down / interface changed | contract check (`talking.contract`) | refused before calling; fallback | `test_film_video.test_malformed_answers_and_changed_interface` |
| all writers down | `AllProvidersFailed` (temporary) | wait and retry, no failure | `test_ai_cloudflare`, `test_failures.KeyTests` |
