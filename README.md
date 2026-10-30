# YT Automation Studio

A local, multi-channel YouTube studio: each channel has its own niche, language, voice, schedule and YouTube login. It researches topics with evidence, writes original scripts and screenplays, makes pictures, talking presenters and short films, voices them, renders long videos and Shorts with FFmpeg, runs a quality gate on the final render, and uploads on a schedule — only through your approval or a channel's explicit auto-publish opt-in. Afterwards it reads analytics, retention per scene and comments, and learns from them.

Everything runs on your own Mac or Windows PC with free providers first (budget ₹0 by default). Paid services stay off unless you switch them on.

## Install and start

**Mac:** `install/install-mac.sh --service` · **Windows:** `powershell -ExecutionPolicy Bypass -File install\install-windows.ps1 -Service`

Then open http://127.0.0.1:8765. Full guide, including moving to another computer: [docs/INSTALL.md](docs/INSTALL.md).

## How a video is made

1. **Niche** — the AI proposes niches (or you list your own). The app measures each with real data — YouTube view/subscriber counts (YouTube API key), Wikipedia pageviews, Google News volume, Ollama web search — and the AI writes a labelled interpretation (cost, monetization, uncertainty). You approve one.
2. **Ideas** — fresh evidence is collected, the AI proposes topics citing evidence ids, duplicates are removed, and each idea gets a 0–100 score whose parts are labelled *measured*, *estimate* or *hypothesis*.
3. **Production** (one at a time, resumable after crashes): topic research → script/storyboard/metadata (validated JSON) → originality check → editorial fact-check review (one automatic revision) → licensed visuals → narration → render → thumbnail → QA.
4. **Approval & upload** — you review the video, thumbnail, script, sources and QA, then approve. Or choose auto-publish, which only applies when every check passes with no warnings.

## Providers

| Purpose | Default | Optional |
|---|---|---|
| Text AI | Ollama local/Cloud, Gemini (rotates 7 free models), Cloudflare Llama 3.3 70B — free, tried in turn, then wait | Claude API (paid, off unless chosen) |
| Research | Google Trends/News RSS, Wikipedia + pageviews, Ollama web search | YouTube Data API key, Hacker News, your RSS feeds |
| Voice | Kokoro (Apache-2.0) — Hindi + English | Piper, Chatterbox clone (consent), Indic Parler, Sarvam (paid) |
| Pictures | stock (Pixabay, Openverse, Wikimedia) with a vision check; Cloudflare / Hugging Face / local FLUX | Pexels, local Stable Diffusion, Gemini images (paid) |
| Motion & talking | free ZeroGPU Wan 2.2 and LTX-2 (few a day); SadTalker talking face on the Mac (no limit) | — |

Live status of every provider: dashboard → **AI providers**. Fallback order: [docs/architecture/PROVIDER_FALLBACK_MATRIX.md](docs/architecture/PROVIDER_FALLBACK_MATRIX.md).

A monthly AI spending limit (Settings › AI) blocks any paid call that could cross it. When the limit is reached, paid calls stop and the app either continues with Ollama or pauses the task until next month.

## Important limits

- **YouTube audit:** uploads from Google Cloud projects that haven't passed the YouTube API audit are locked **private** by YouTube. The app uploads as private until you tick "audit passed" in Settings › YouTube.
- **Voice licences:** all three Hindi Piper voices were trained on non-commercial datasets. Use Kokoro for a monetized channel. QA warns when a non-commercial voice is used.
- **No fabricated data:** the models see only collected evidence and cite it by id. Claims without support are downgraded or flagged, and descriptions list the sources.
- **AI video** is limited by free quotas: real motion and the most realistic talking clips come from free Hugging Face GPU minutes (a few clips a day). Otherwise shots are stills with camera moves or a SadTalker talking face — always labelled as such, never called video.
- **Monetization:** progress is shown from YouTube's own numbers with the official rules and their check date; eligibility is not acceptance, and no date or income is promised.

## Safety design

- The server binds to 127.0.0.1. It checks the Host header (blocks DNS rebinding), requires a CSRF token and same-origin on every state change, and sends a strict CSP.
- Keys live in the macOS Keychain or Windows Credential Manager (service `yt-automation-studio`). Logs redact credentials.
- Model output is validated with Pydantic and is never executed or put on a command line. FFmpeg filter graphs contain only app-computed numbers, and all on-screen text is rasterised by Pillow first.
- Outbound fetches refuse private/loopback addresses, cap sizes, and honour robots.txt for web pages.

## Operations

- **Emergency stop** (Activity log) halts all new work and publishing; channels can be paused one by one.
- **Backups**: the database is copied and checked every night (Storage page; `python -m studio.backup`), with a weekly restore drill.
- **Audit trail** of approvals, uploads, channel switches and stops (Activity log).
- **Storage page**: live memory/disk use and uninstall buttons for optional AI tools.

Docs: [RUNBOOK](docs/RUNBOOK.md) · [Disaster recovery](docs/operations/DISASTER_RECOVERY_PLAN.md) · [Backup & restore](docs/operations/BACKUP_AND_RESTORE.md) · [Provider failures](docs/operations/PROVIDER_FAILURE_RUNBOOK.md) · [Offline](docs/operations/OFFLINE_CAPABILITIES.md) · [Continuity checklist](docs/operations/BUSINESS_CONTINUITY_CHECKLIST.md) · [Production readiness](docs/operations/PRODUCTION_READINESS_REPORT.md) · [Architecture audit](docs/ARCHITECTURE_AUDIT.md) · [Repository independence](docs/architecture/REPOSITORY_INDEPENDENCE.md) · [Licences](docs/architecture/OPEN_SOURCE_LICENSE_AUDIT.md) · [Monetization](docs/CHANNEL_AUTOMATION_AND_MONETIZATION.md)

## Files

```
studio/            application package (see module docstrings)
  ai/              provider adapters, router, spending guard, prompts
  research/        evidence sources, niche research, ideation, dedup
  content/         script/storyboard/metadata generation + validation
  media/           TTS, captions, visuals, graphics, render, thumbnail, QA
  creative/        news, animated stories, photo-real films, characters, music
  film_studio/     Film Studio: casting, shot list, takes, talking clips, edit, QA
  finance/         NIFTY channel: verified data, compliance gate, audit chain
  editing/         Upload & Edit, AI Shorts clipper
  youtube/         OAuth (PKCE), resumable upload, analytics, retention per scene, comments, growth
  ops.py           emergency stop, publishing gate, audit trail, repeat check
  backup.py        checked database backups, restore drill, restore
  web/             FastAPI app, templates, static assets
  pipeline.py      production state machine
  worker.py        persistent task queue (2 lanes, retries, recovery)
  scheduler.py     persistent schedules
data/              database, logs, voices, renders (created at runtime)
install/           installers; install/vendor/sadtalker = patched SadTalker (no GitHub needed)
scripts/           model_vault.sh (offline copy of all models), benchmarks
docs/              runbook, audits, architecture/ and operations/ documents
tests/             python -m unittest discover -s tests
```

## Troubleshooting

- **Hindi text looks broken:** `brew install libraqm`, then restart.
- **"No TTS voice installed":** Settings › Voice → install Kokoro, then test and select a voice.
- **Ollama local unavailable:** install from ollama.com, open the app, then pull a model under Settings › AI (the app checks its size against your RAM first). Ollama Cloud works without a local install.
- Technical log: `data/logs/studio.log`.
