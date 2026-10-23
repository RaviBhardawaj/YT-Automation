# Video platform: implementation report (24 Oct 2026)

The studio is now a multi-user video platform: any niche, from a prompt or from uploaded footage, with
preview, download and publishing. Everything below runs in this codebase. Nothing is mocked.

## What was added

| Area | What it does | Where |
|---|---|---|
| Accounts & workspaces | Sign-up, sign-in (scrypt passwords, hashed session tokens, 30-day sessions). The first account becomes the platform admin and owns the existing studio; each later creator gets an isolated workspace. Roles: owner / editor / viewer. | `studio/auth.py` |
| Tenant isolation | A central check on every request: a job, idea, channel, upload, character, task, media file or download of another workspace returns 404. Admin-only areas: Settings, Compliance, Analytics, Niche research, Admin. | `web/app.py` `guard()` |
| Uploads | Streamed to disk (no size-limited JSON), with progress and per-file retry in the page. Validated by type, plan size limit, plan duration limit and a real decode check (ffprobe). Stored read-only with SHA-256; a duplicate upload reuses the stored file. Originals are never modified. | `studio/uploads.py` |
| Upload & edit | Instructions + files → inspect (integrity, scene cuts) → local transcription (faster-whisper) → AI edit plan, with a rule-based fallback → FFmpeg render of every planned output → quality checks → review. Supported: cutting, pause removal, 9:16 / 1:1 / 16:9 reframing (blur-fit or crop), burned-in captions with highlighted keywords and an .srt (as spoken or translated), intro/outro cards, original background music, loudness normalisation, speed, titles/descriptions/tags, thumbnails. Things it can't do are listed on the job, and everything else is still done. | `studio/editing/` |
| Create from a prompt | One box for any niche: animated stories (comedy, horror, kids …), researched videos (finance, tech, education …) or NIFTY daily. With files attached it becomes an edit job. A channel is optional. | `/create`, `jobs.py` |
| Outputs & traceability | Every output is a library item linked to its job (parent) and to its source uploads; the original and edited versions are kept separately. | `productions.source_media`, `parent_id` |
| Preview & download | In-app player with duration, resolution, aspect, size and codecs. Downloads (video, version without burned-in subtitles, .srt, thumbnail) are offered only for files that exist; a missing file shows "Video file missing", and Retry re-renders only that output. | `/download/{id}/{variant}` |
| Publishing | Choose a connected channel, edit the title/description/tags, then publish now, schedule, or keep private — or download instead. Destination verification runs before every upload. A video is marked published only after YouTube returns its ID. | `/api/productions/{id}/publish`, `/publishing` |
| Plans & usage | Free / Creator / Pro / Studio limits (videos/month, render minutes, storage, upload size and length, channels, concurrent jobs), with no prices hard-coded. Usage is metered once per job step (idempotency key), so retries never double-charge. Costs are labelled actual or estimate. | `studio/plans.py`, `/billing` |
| Admin | Users (enable/disable), workspaces (change plan), usage per workspace, recent errors, open/close sign-ups. | `/admin` |
| Pages | Dashboard (quick actions, continue, recent uploads), Create video, Upload & edit, Ideas, Projects, Video library + Uploads, AI agents & tools (live capability status), YouTube channels, Automation & schedules, Publishing, Usage & billing, Queue & logs. | `web/templates/` |

## Acceptance tests (run on a separate running instance with test accounts)

| # | Test | Result |
|---|---|---|
| 1 | New video from a prompt | ✅ "The Gourmet Gato": 43 s vertical English animated comedy, all checks passed |
| 2 | Different niches, different formats | ✅ The comedy prompt went to the animated story workflow; "index fund" went to the researched video workflow; the edit request went to the edit engine |
| 3 | Upload + specific edits | ✅ 74 s 1080p podcast: pauses removed (→ 59 s), 28 captions with highlighted keywords, intro card, background music — submitted through the Upload & edit page |
| 4 | Upload stays intact | ✅ SHA-256 identical before and after; the stored original is read-only |
| 5 | Render + preview | ✅ The in-page player loads 1920×1080, 59.2 s |
| 6 | Download the real file | ✅ Video / no-subtitle version / .srt / thumbnail downloads are byte-identical to the files on disk |
| 7 | Shorts from a long video | ✅ 3 vertical 1080×1920 Shorts with their own titles; a later run made a 27.8 s Short after the 15 s minimum was enforced |
| 8 | Outputs stored in the right project | ✅ Outputs #4–6 linked to job #3 and to source upload #1 |
| 9 | Restart persistence | ✅ After a restart: sessions, jobs, uploads and downloads all intact |
| 10 | Publish to the authorised channel | ⚠️ **Not executed**: it would upload a test video to the real "Talk with Trader" channel. The destination check passed (read-only call to YouTube). |
| 11 | Publishing failures reported accurately | ✅ An unconnected channel and someone else's channel were both refused with clear messages; the status stayed "ready", never "published" |
| 12 | A disconnected account doesn't affect others | ✅ The creator's unconnected channel was refused while the owner's channel verified at the same time |
| 13 | Data isolation | ✅ The admin account got 404 for the creator's job, upload, channel, download and media URLs; the creator got 403 on admin pages |
| 14 | Usage limits + retries | ✅ A 4th video over a 3-video limit was refused before any work started; a job failed honestly on a missing source, Retry finished it, a second Retry was refused, and usage was recorded once |
| 15 | No nonexistent file shown as complete | ✅ A deleted output shows "Video file missing" with no download offered; Retry re-rendered only that file |

Unit tests: 120 pass (`.venv/bin/python -m unittest discover -s tests`), including a real FFmpeg edit render,
isolation over HTTP, upload validation, limits and idempotent usage.

## Needs integrations or configuration

- **Billing is not live.** Plans and limits are enforced, but no payment provider (e.g. Stripe) is connected and
  nobody is charged. Connecting one needs the provider account and keys, then checkout and webhooks.
- **Hosting for other creators.** The server listens on 127.0.0.1 (this Mac only). Selling it as SaaS needs a
  server or cloud host, HTTPS, a domain, object storage for media, and a privacy policy.
- **Per-creator YouTube.** Each creator connects their own channels through your Google OAuth client. For
  public uploads and more than 100 users, the Google project must pass verification and the YouTube API audit.
- **Not available yet.** AI text-to-video generation (no provider connected), voice replacement/dubbing of
  uploads, and animal characters in the 2D animation (the cat story's "Billu" is drawn as a person).
- **Transcription** runs locally (faster-whisper "small", 460 MB, already downloaded). Hindi accuracy is fair;
  the "medium" model is more accurate but slower.
