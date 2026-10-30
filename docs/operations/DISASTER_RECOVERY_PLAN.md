# Disaster recovery plan

No promise of zero downtime: the studio runs on one Mac. This plan says what survives what, and how to get back.

| Event | What happens automatically | What you do | Data at risk |
|---|---|---|---|
| App crash / Mac restart | LaunchAgent restarts it; `worker.recover()` puts interrupted tasks back; each stage resumes from its saved output | nothing | the step that was running is redone |
| Crash during upload | resumable session URI is saved; the retry asks YouTube how much arrived and continues; a video with a YouTube ID is never uploaded again | nothing | none |
| AI writer / picture / video provider down, quota out, key invalid | next free provider; then the job waits and retries (see `PROVIDER_FAILURE_RUNBOOK.md`) | optional: add a key | none |
| Bad batch of videos | — | **Emergency stop** (Activity log) or pause the channel | none published while stopped |
| Database corrupted / bad change | nightly checked copies | restore (`BACKUP_AND_RESTORE.md`) | changes since the last copy (≤ 24 h) |
| Disk full | renders fail with a clear error | Storage page: clear working files, uninstall unused AI tools | none |
| Internet down | online steps wait (`OFFLINE_CAPABILITIES.md`) | nothing | none |
| Public repository deleted (GitHub/HF) | nothing at runtime depends on one | reinstalls use the vendored SadTalker and the model vault (`REPOSITORY_INDEPENDENCE.md`) | none if the vault was run |
| Mac lost / dead | — | new Mac: clone repo, `install/install-mac.sh --service`, restore `data/` from your copy, `scripts/model_vault.sh <vault> restore`, sign in to YouTube again | everything not copied off the Mac |

## Single points of failure (remaining)
1. **The Mac itself** — the database copies are on the same disk. Copy `data/backups` and `data/productions` to an
   external drive or cloud folder regularly (not automated yet).
2. **Internet / hotspot** — every writer except a local Ollama model needs it; no local writer model is installed.
3. **YouTube API project** — one Google Cloud project for uploads; its quota and audit status apply to all channels.
4. **Keychain** — keys and logins exist only there.

## Recovery tests performed (31 Oct 2026)
- Restore drill on the real database: passed.
- SadTalker rebuild without GitHub: passed (91 s).
- Interrupted task after restart: observed live on 30 Oct (Kids video #156 recovered and resumed after two restarts).
- Upload duplicate protection: `test_finance` (re-approval after an upload is skipped), `test_failures.InterruptedUploadTests` (a finished session is reconciled, no second upload).
