# Feature integration matrix

Legend — **status**: verified working · incomplete · added in this pass · not ported. **Cost**: free self-hosted ·
free with limited quota · paid. "Verified" means exercised by tests and/or a real run on 30–31 Oct 2026; "mock" means
only tested with the external service mocked.

| # | Lifecycle step | Our implementation | Status | Cost | Reference repo | Decision |
|---|---|---|---|---|---|---|
| 1 | Register, configure channels | `auth.py`, `channels.py`, Channels pages | verified (real) | free | single channel | ours |
| 2 | Niche & audience | channel fields + first-run interview (`profile.py`), niche research | verified | free | strategy agent | ours |
| 3 | Content requirements | channel instructions/restrictions, request planner (`creative/request.py`) | verified | free | — | ours |
| 4 | Research topics | `research/engine.py`, `sources.py` (news, Wikipedia, YouTube, RSS), `creative/news.py` | verified (real) | free / YouTube quota | strategy agent | ours |
| 5 | Ideas | `research.engine`, `creative/ideas.py`, `creative/trends.py` | verified | free quota (AI) | strategy agent | ours |
| 6 | Scripts & screenplays | `content/package.py`, `creative/screenplay.py`, `script_import.py` | verified (real) | free quota | script agent | ours; set-description bug fixed 31 Oct |
| 7 | Characters & references | `creative/characters.py`, `film_studio/casting.py` | verified (real) | free (local FLUX) | — | ours |
| 8 | Scenes & shots | `film_studio/shotlist.py` | verified | free | — | ours |
| 9 | Images & video | stock + vision check, FLUX (Cloudflare/HF/local), ZeroGPU Wan 2.2, LTX-2, SadTalker, labelled camera moves | verified (local), quota-limited (online) | free / limited quota | paid video providers | ours; paid providers not ported |
| 10 | Narration, dialogue, sound | Kokoro, Piper, Chatterbox, Parler, original music, ambience | verified | free self-hosted | Azure Speech (paid) | ours |
| 11 | Edit & render long-form | `media/render.py`, `film_studio/edit.py` (format-checked joins, render manifest) | verified | free | ffmpeg wrapper | ours |
| 12 | Shorts from long-form | `editing/clipper.py`, `finance/shorts.py`, film `shorts.py` | verified (tests) | free | shorts repurposing | ours |
| 13 | Thumbnails & metadata | `media/thumbnail.py` (strongest picture), `content.sanitize`, QA metadata checks | verified | free | thumbnail + SEO agents | ours |
| 14 | Review & approval | approval queue, `ops.auto_publish_block`, first-N rule | verified (tests) | free | — | ours |
| 15 | Schedule & publish | `scheduler.py`, `youtube/upload.py` (resumable, idempotent) | verified (real uploads) | free (API quota) | publishing agent | ours |
| 16 | Analyse performance | `youtube/analytics.py`, `growth.py` | verified (real) | free | analytics agent | ours |
| 17 | Learn from audience | `learning.py` (daily lessons, weekly decisions), **retention per scene**, **traffic sources**, **comments** | added (mock-tested; live data needs public videos ≥ 2 days old) | free | learning/retention/engagement | **ported** |
| 18 | Improve future content | lessons fed into prompts (`learning.prompt_block`), editorial scene fixes | verified | free quota | learning engine | ours |
| — | Growth experiments (title/thumbnail swaps) | — | not ported | — | growth-experiment service | needs live-video edits + a CTR metric the public API lacks |
| — | Comment replies posted automatically | — | not ported (drafts only) | — | engagement service | owner posts replies |
| — | Telemetry | — | not ported | — | anonymous telemetry | privacy |
| — | Upload & edit own media | `uploads.py`, `editing/*` | verified (tests) | free | — | ours |
| — | Download finished videos | `/download/...` | verified | free | — | ours |
| — | Emergency stop, audit, repeat check | `ops.py` | added 30 Oct, tested | free | operator service (partly) | ours |
| — | Backups & restore | `backup.py` | added 31 Oct, tested + real run | free | — | ours |
| — | Provider health | `provider_status.py`, `/providers` | added 31 Oct, tested | free | readiness service | ours |
