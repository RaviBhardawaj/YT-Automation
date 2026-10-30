# Repository comparison — YT Studio vs. youtube-automation-agent

Inspected 30–31 Oct 2026 from source (not documentation).

| | **YT Studio** (ours, primary) | **Reference** `darkzOGx/youtube-automation-agent` |
|---|---|---|
| Commit inspected | `main` (this repository) | `e973d515aef90c467da2289144df737a3bd03859` (26 Oct 2026), v2.11.0 |
| Licence | private | MIT ("YouTube Automation Agent Contributors", 2025) |
| Language | Python 3.12 | Node.js ≥ 18 |
| Size | ~27,000 lines in `studio/`, 36 test files | ~20,400 lines (agents/utils/db/dashboard), `test.js` (193 KB) |
| Backend | FastAPI, 140 routes, Jinja2 templates | Express, single `index.js` (89 KB) |
| Data | SQLite, versioned migrations (v20), WAL | SQLite (`database/db.js`, 37 tables) |
| Jobs | persistent SQLite queue, 2 worker lanes, crash recovery, scheduler | `node-cron` daily automation + in-process job runner |
| Accounts | multi-user, workspaces, plans, per-channel OAuth | single operator, one YouTube credential |
| Channels | many channels, each with its own niche/voice/schedule/OAuth | one channel |
| AI text | router over Ollama (local/cloud), Gemini (7-model rotation), Cloudflare Llama, Claude (paid, off) | OpenAI SDK, Gemini models list |
| Images | Pixabay/Openverse/Wikimedia stock with vision check; Cloudflare, HF and local FLUX | thumbnails via `sharp`; video providers render frames |
| Video | FFmpeg render; free ZeroGPU Wan 2.2; LTX-2 + SadTalker talking; labelled camera moves | paid providers: Seedance, MiniMax, Google Omni, Kling (Replicate), Wan; slideshow fallback |
| Voice | Kokoro, Piper, Chatterbox clone, Indic Parler (local); Sarvam (paid) | Microsoft Azure Speech SDK (paid key) |
| Film | Film Studio: screenplay → cast → shot list → takes → edit → QA | — (scene-level production only) |
| Finance | NIFTY compliance gate, audit chain, SEBI wording lint | — |

## Module-by-module

| Reference module | What it does | Our equivalent | Decision |
|---|---|---|---|
| `agents/content-strategy-agent.js` | AI topic ideas from niche | `research/engine.py`, `creative/trends.py`, `creative/news.py` (evidence-backed) | ours is stronger (measured demand, dedupe) |
| `agents/script-writer-agent.js` | AI script | `content/package.py` (validated schema, chapter length checks, editorial review, scene-only fixes) | ours stronger |
| `agents/seo-optimizer-agent.js`, `utils/youtube-metadata-validator.js` | titles/tags/description, length limits | `content/package.sanitize`, `media/qa.py` metadata checks, `creative/viral.py` | equivalent |
| `agents/thumbnail-designer-agent.js` | text-on-image thumbnails | `media/thumbnail.py` (contrast-measured, strongest-picture choice) | ours stronger |
| `agents/production-management-agent.js`, `utils/ai-video-generator.js`, `utils/video-providers.js` | video via paid providers or slideshow | `pipeline.py`, `media/render.py`, `film_studio/*` | **not ported** — paid providers |
| `agents/publishing-scheduling-agent.js` | upload + schedule | `youtube/upload.py` (resumable, idempotent, per-channel) | ours stronger |
| `agents/analytics-optimization-agent.js` | metrics, retention, traffic sources | `youtube/analytics.py`, `learning.py` | **ported**: retention curve + traffic sources. **Not ported**: its `impressions`/`impressionClickThroughRate` query — the official metric list (checked 2026-10-31) has no thumbnail-impression metric; `impressions` was renamed `adImpressions` (ad impressions), so those numbers would be mislabelled |
| `utils/scene-retention-engine.js` | maps the retention curve onto scenes, classifies drop-off / rewatch / strong hold | none | **ported** to `youtube/retention.py` |
| `utils/audience-engagement-service.js` | classify comments (question/request/praise/correction/spam/scam/toxic), themes, reply drafts, optional posting | none | **ported** (classification + drafts) to `youtube/comments.py`; **posting not ported** (owner sends replies in YouTube) |
| `utils/channel-learning-engine.js` | baseline + deltas per dimension | `learning.py` (AI lessons + weekly decisions) | equivalent; retention signals added |
| `utils/growth-experiment-service.js` | title/thumbnail A/B by swapping live metadata, z-test | none | **not ported now** — it edits live videos and its CTR arm relies on the unavailable impression metric; documented as a future item needing owner authorization |
| `utils/provenance-service.js` | sources + claims per production | `research/sources.py` evidence (url, title, date), `fact_checks` with evidence ids, editorial review | equivalent |
| `utils/scene-repair-service.js` | edit/regenerate one scene, rebuild narration | film `qa.regenerate`, shot retakes, editorial scene fixes (`content.fix_scenes`) | partial equivalent |
| `utils/shorts-repurposing-service.js` | windows of scenes → vertical clips (blur/crop/stacked) | `editing/clipper.py` (transcript-based), `finance/shorts.py`, film `shorts.py` | ours stronger |
| `utils/generation-recovery-service.js` | stage checkpoints, resume | `pipeline.py` resumable stages, `worker.recover` | equivalent |
| `utils/autonomous-channel-operator.js`, `operator-service.js` | run strategy → produce → QA loop | `scheduler.py` + `tasks._channel_daily` + `jobs.py` + `ops.py` gate | equivalent |
| `utils/production-readiness-service.js` | checks FFmpeg, keys, disk | `system_setup.py`, `sysinfo.py`, Storage page | equivalent |
| `utils/credential-manager.js` | credentials in a JSON file | macOS Keychain (`settings.py`) | ours safer |
| `utils/anonymous-telemetry.js` | opt-in usage pings to a remote endpoint | none | **excluded** (privacy; not needed) |
| `utils/discoverability-adapters/darkzseo.js` | adapter for an external SEO service | none | **excluded** (third-party service) |
| `dashboard/*`, `mcp/*`, `walkthrough.js`, `setup.js` | UI, MCP config, CLI wizards | our dashboard | not applicable |

## Conclusions
- Our studio already covers the reference's pipeline and goes further on multi-channel operation, free/local
  providers, Film Studio, compliance and recovery.
- Worth porting (done): scene-level retention analysis, traffic sources, comment insights with reply drafts.
- Nothing from the reference is executed, imported or downloaded at runtime; ported logic was rewritten in Python
  (MIT permits adaptation; attribution recorded in `OPEN_SOURCE_LICENSE_AUDIT.md`).
