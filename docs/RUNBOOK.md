# Runbook

## Start, stop, restart
- The studio runs as a LaunchAgent and opens at http://127.0.0.1:8765 (this Mac only).
- Restart: `launchctl kickstart -k gui/$(id -u)/com.ytautomation.studio`
- By hand (without the service): `./run.sh`
- Log: `data/logs/studio.log` (secrets redacted). Activity log page = the same events + tasks + audit trail.

## Emergency stop
Activity log → **Emergency stop** (owner only). Switch off from the red banner on any page. While on: nothing new
starts except bookkeeping, nothing is approved or uploaded automatically. A running step finishes.

## Pause one channel
Channel page → **Pause channel**. Resume with the same button.

## Backups and restore
- Before risky changes: `.venv/bin/python -c "import sqlite3; s=sqlite3.connect('data/studio.db'); d=sqlite3.connect('data/backups/studio-manual.db'); s.backup(d)"`
- Restore: stop the service, copy the backup over `data/studio.db` (remove `studio.db-wal`/`-shm`), start it.
- Videos are in `data/productions/<id>/`; keys are in the macOS Keychain (service `yt-automation-studio`) and are
  not in backups.

## Jobs that stopped
- Tasks left "running" by a crash are put back in the queue at start-up (`worker.recover`).
- Activity log → **Retry** on a failed task, **Cancel** to drop it.
- A video's page → retry from a stage; a Retry never queues the same video twice.
- "All writers used up" is not a failure: the video waits 30 minutes and tries again.

## Channel setup and OAuth
1. Channels → New channel; fill niche, audience, language, schedule.
2. **Connect YouTube** (Google's own sign-in page; choose the right channel). Each channel keeps its own token.
3. YouTube & publishing: uploads on, privacy, go-public time, auto-publish, "you approve the first … uploads".
4. Approve the first uploads yourself; test with private/unlisted first.

## Approval and publishing
Ready videos wait on Publishing / the video page. A video goes out automatically only through the gate in
`docs/CHANNEL_AUTOMATION_AND_MONETIZATION.md`. Uploads are resumable; a finished upload is never repeated.

## Analytics and monetization
- Analytics sync every 6 hours; growth numbers daily 06:00; AI lessons daily 06:30; weekly review Mondays 07:15.
- Channel → YouTube & publishing → status, policy readiness, weekly review. Record YouTube's decision yourself.
- Re-check the official YPP pages (links in the monetization doc) and update `RULES` in `studio/youtube/growth.py`.

## Disk and memory
Storage page: live memory/CPU/disk, biggest programs, clear working files and cache, **uninstall optional AI
tools** (local FLUX 6 GB, voice cloning 4.5 GB, SadTalker 2.4 GB, Parler 1.4 GB, Whisper 0.5 GB). Uninstalling is
refused while a video is being made.

## Talking presenter
LTX-2 on the free GPU first (~2 pieces a day), then SadTalker on this Mac (`~/sadtalker`, own Python). If
SadTalker is uninstalled the presenter stays a still picture. Reinstall: ask Claude (the app has no one-click
installer for it yet).

## Tests
`.venv/bin/python -m unittest discover -s tests` (~11 minutes, all external services mocked). Live checks not run
by the suite: real YouTube uploads, real ZeroGPU/LTX-2 calls, real SadTalker inside a scheduled video.

## Environment variables
| Variable | Use |
|---|---|
| `STUDIO_DATA_DIR` | data folder (tests use a temp folder) |
| `STUDIO_KEYRING_SERVICE` | Keychain service name (tests use a separate one) |
| `STUDIO_OFFLINE=1` | never call online picture services (tests) |
| `HF_HOME` | Hugging Face cache location (default `~/.cache/huggingface`) |

## Not verified in this pass
CSP/CORS headers and upload MIME sniffing were not re-audited; caption safe zones beyond the fixed caption band;
visible lip-sync accuracy.
