# Architecture audit — 30 Oct 2026

Audit of the existing studio before the "Master Upgrade v2" brief was implemented on branch
`upgrade/master-v2`. The application was **upgraded in place** — nothing was rebuilt or replaced.

## Baseline

| Item | Result |
|---|---|
| Branch | `upgrade/master-v2` from `main` @ `fa630a5` |
| Uncommitted work preserved | `.claude/launch.json` (modified) and `docs/AI-Video-Studio-Project-Structure.pdf` (untracked) left untouched |
| Data backup | `data/backups/studio-before-master-v2-20261030-2236.db` (SQLite online backup, before migration v20) |
| Baseline tests | `python -m unittest discover -s tests` → **364 run, OK, 3 skipped** (run on `fa630a5`) |

## What the system is

One local FastAPI app (`studio/web/app.py`, 140 routes, 43 Jinja templates) over SQLite (WAL, versioned
migrations, now v20). Two worker threads (`media` lane for heavy video work, `general` for everything else) take
tasks from a SQLite queue with retries, back-off, pause, dedupe and crash recovery. A scheduler creates daily
channel tasks. About 27,000 lines of Python in `studio/`, 36 test files.

```
request / schedule ─► jobs.create ─► tasks (SQLite) ─► worker lanes ─► pipeline stages
   research → script → review → assets → voice → render → thumbnail → qa ─► approval gate ─► upload ─► analytics
```

Workflows on the same queue: general/research videos (`pipeline.py`), news (`creative/news.py`), animated
stories (`creative/workflow.py`), photoreal films (`film_studio/*`), NIFTY finance episodes and lessons
(`finance/*`, with their own compliance gate), Upload & Edit and the Shorts clipper (`editing/*`).

## End-to-end trace (real run, 30 Oct 2026)

AI News #159: Google Trends story → research (2-day news window) → script by `gemini/gemini-3-flash-preview` →
editorial review → assets (presenter + stock + local FLUX) → Kokoro voice → FFmpeg render → QA → auto-approved
(channel auto-publish) → resumable upload → `https://youtu.be/ddVqb34ln6Q`. Verified in the log
(`data/logs/studio.log`, 20:24:26).

## Feature classification

| Area | Status | Notes |
|---|---|---|
| Queue, workers, crash recovery, retries | working | `worker.py`; recover() on restart |
| Scheduler, per-channel daily slots | working | up to 4 times a day per channel |
| Research + scripting + editorial review | working | free writer rotation; flagged scenes re-written (2 rounds) |
| Visuals (stock, vision check, AI images) | working | Pixabay, Openverse, Wikimedia, Cloudflare/HF/local FLUX |
| Voice (Kokoro, Piper, clone, Parler) | working | Sarvam (paid) out of credits → Kokoro |
| Render + captions + thumbnail | working | thumbnail picker improved (risk 9) |
| QA gate | working | frozen-picture and repeat checks added |
| Upload (resumable, idempotent) | working | session URI persisted; never uploads a production twice |
| Analytics + daily learning | working | impressions/CTR not collected |
| Growth / YPP progress | partial → fixed | thresholds were unverified and "eligible" overstated; now sourced, dated, honest |
| Film Studio (photoreal films) | working, quota-limited | real motion needs free ZeroGPU minutes |
| Talking presenter | working | LTX-2 (2/day free) then SadTalker on the Mac |
| Facebook / Instagram | stubbed (not connected) | adapter exists; no Meta app secret set |
| Gemini Omni video / local Wan 2.2 | listed only | no execution adapter — now marked and never chosen |
| Emergency stop, audit trail, approve-first-N | missing → added | `studio/ops.py` |
| Cross-channel duplicate check | missing → added | QA warn check |
| Weekly decision report | missing → added | `learning.weekly_report` |

## The ten suspected risks — verified against code

| # | Suspected risk | Verdict | Evidence | Action |
|---|---|---|---|---|
| 1 | `_resume_clip()` deletes valid clips (inverted `valid()`) | **Not a bug** | `zerogpu.valid()` returns the *reason a clip is bad* ('' = usable); `if valid(meta): unlink` removes only broken clips | none |
| 2 | `_clip_for()` cache key incomplete | **Confirmed** | key held kind/source path/seconds/subtitle/motion/speech only — no key picture, no file identity (a re-made take at the same path was reused), no card text, size, encoder settings or renderer version | `edit.cache_key()`; test `test_a_remade_take_at_the_same_path_changes_the_key` |
| 3 | Film QA inspects key pictures, not the final render | **Partly confirmed** | decode/black/audio/subtitle checks ran on the final file; no frozen-picture check; relevance check uses key pictures (labelled so) | `media.qa.freeze_check()` on the final render in both QA paths |
| 4 | Provider registry advertises providers without adapters | **Confirmed** | `wan22_local`, `gemini_omni` listed with video caps but `run_video` can't execute them; working talking providers (LTX-2, SadTalker) absent | `adapter` flag; routing skips no-adapter entries; LTX-2 + SadTalker added |
| 5 | Video budget counts only successes | **Minor** | failures were recorded (`ok=0`) but the budget showed successes only | `video_budget()` now also returns `attempts` and `failed` |
| 6 | Pause/cancel missing in loops | **Not confirmed** | film shot loop checks paused/rejected between shots; pipeline stages are resumable | none (global emergency stop added) |
| 7 | Concat assumes compatible streams | **Confirmed (latent)** | `-c copy` concat without checking formats (safe today only because every clip came from the same encoder) | `render.concat()` copies only when every clip matches, else normalises and re-encodes |
| 8 | Motion clips end and freeze on a still key picture | **Confirmed** | a clip shorter than its shot finished on the shot's *starting* key picture — a visible jump back | shot continues from the clip's last frame (`render.last_frame`); test checks the end colour |
| 9 | Thumbnail from the first frame | **Partly** | it took the *largest* picture, not the first, but ignored relevance/sharpness | `thumbnail.pick_background()` scores relevance + quality + size |
| 10 | QA warnings don't block unattended publishing | **Not a bug, but a related gap** | warnings already set `auto_publish_ok=False`; however the studio-wide "auto" mode could publish channels that never opted in | channel opt-in is now required (`ops.auto_publish_block`) |

## Other findings

- **Upload idempotency** — verified working: resumable session persisted, completed session returns the video,
  `youtube_video_id` prevents a second upload. Not changed.
- **YouTube policy** — the monetization policy page (checked 2026-10-30) lists "AI personas related to sensitive
  topics" (AI hosts presenting as experts giving health/legal/financial/political advice) as not monetizable.
  The AI News presenter covers market stories; it is flagged on the channel's policy-readiness list.
- **Disk** — 12–16 GB free on a 245 GB disk; Storage page now shows live usage and uninstall buttons.
- **Memory** — 16 GB; SadTalker's 512 model exhausts it, so the 256 model is used.
- **Security** — no change needed found in this pass: parameterised SQL (`db.q`), FFmpeg via argument lists,
  secrets in the Keychain and redacted from logs, URL guard rails in `net.py`, CSRF tokens on POSTs.
  Not re-audited in depth: CSP/CORS headers and upload MIME sniffing (see RUNBOOK "Not verified").
