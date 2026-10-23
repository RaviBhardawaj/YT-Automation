# YT Automation Studio

A local web app that researches a niche, finds and ranks topics with evidence, writes original scripts, voices them with a local TTS engine, renders Shorts (9:16) and long-form (16:9) videos with FFmpeg, makes thumbnails, runs a quality gate, and uploads to your YouTube channel — on a schedule, with your approval.

Everything runs on this Mac. External services are optional and only used when you add their key.

## Start

```bash
./run.sh
```

Opens http://127.0.0.1:8765. The first visit walks you through the channel interview (one question at a time); answers are stored in `data/studio.db`.

To keep it running in the background at login (optional):

```bash
scripts/install_launch_agent.sh
```

Remove with `scripts/install_launch_agent.sh remove`.

## How a video is made

1. **Niche** — the AI proposes niches (or you list your own). The app measures each with real data — YouTube view/subscriber counts (YouTube API key), Wikipedia pageviews, Google News volume, Ollama web search — and the AI writes a labelled interpretation (cost, monetization, uncertainty). You approve one.
2. **Ideas** — fresh evidence is collected, the AI proposes topics citing evidence ids, duplicates are removed, and each idea gets a 0–100 score whose parts are labelled *measured*, *estimate* or *hypothesis*.
3. **Production** (one at a time, resumable after crashes): topic research → script/storyboard/metadata (validated JSON) → originality check → editorial fact-check review (one automatic revision) → licensed visuals → narration → render → thumbnail → QA.
4. **Approval & upload** — you review the video, thumbnail, script, sources and QA, then approve. Or choose auto-publish, which only applies when every check passes with no warnings.

## Providers

| Purpose | Default | Optional |
|---|---|---|
| Text AI | Ollama local (install separately), Ollama Cloud (your Ollama key) | Claude API (billed per token, separate from Claude Pro/Max), Gemini |
| Research | Google News RSS, Wikipedia + pageviews, Ollama web search | YouTube Data API key, Hacker News, your RSS feeds |
| Voice | Kokoro (Apache-2.0) — Hindi + English | Piper voices (check each licence) |
| Visuals | Original procedural art | Pexels, Pixabay, Openverse, Wikimedia Commons, local Stable Diffusion API, Gemini images |

A monthly AI spending limit (Settings › AI) blocks any paid call that could cross it. When the limit is reached, paid calls stop and the app either continues with Ollama or pauses the task until next month.

## Important limits

- **YouTube audit:** uploads from Google Cloud projects that haven't passed the YouTube API audit are locked **private** by YouTube. The app uploads as private until you tick "audit passed" in Settings › YouTube.
- **Voice licences:** all three Hindi Piper voices were trained on non-commercial datasets. Use Kokoro for a monetized channel. QA warns when a non-commercial voice is used.
- **No fabricated data:** the models see only collected evidence and cite it by id. Claims without support are downgraded or flagged, and descriptions list the sources.
- **Cinematic AI video** isn't generated locally (this Mac doesn't have the GPU memory for it). Motion comes from stock clips, camera moves, transitions and kinetic captions.

## Safety design

- The server binds to 127.0.0.1. It checks the Host header (blocks DNS rebinding), requires a CSRF token and same-origin on every state change, and sends a strict CSP.
- Keys live in the macOS Keychain (service `yt-automation-studio`). Logs redact credentials.
- Model output is validated with Pydantic and is never executed or put on a command line. FFmpeg filter graphs contain only app-computed numbers, and all on-screen text is rasterised by Pillow first.
- Outbound fetches refuse private/loopback addresses, cap sizes, and honour robots.txt for web pages.

## Files

```
studio/            application package (see module docstrings)
  ai/              provider adapters, router, spending guard, prompts
  research/        evidence sources, niche research, ideation, dedup
  content/         script/storyboard/metadata generation + validation
  media/           TTS, captions, visuals, graphics, render, thumbnail, QA
  youtube/         OAuth (PKCE), resumable upload, analytics
  web/             FastAPI app, templates, static assets
  pipeline.py      production state machine
  worker.py        persistent task queue (2 lanes, retries, recovery)
  scheduler.py     persistent schedules
data/              database, logs, voices, renders (created at runtime)
tests/             python -m unittest discover -s tests
```

## Troubleshooting

- **Hindi text looks broken:** `brew install libraqm`, then restart.
- **"No TTS voice installed":** Settings › Voice → install Kokoro, then test and select a voice.
- **Ollama local unavailable:** install from ollama.com, open the app, then pull a model under Settings › AI (the app checks its size against your RAM first). Ollama Cloud works without a local install.
- Technical log: `data/logs/studio.log`.
