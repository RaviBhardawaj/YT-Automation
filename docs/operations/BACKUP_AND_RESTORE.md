# Backup and restore

## What is backed up, where, how often
| Data | Where it lives | Backup |
|---|---|---|
| Database: channels, settings, videos list, scripts, QA, schedules, tasks, analytics, audit log | `data/studio.db` | **automatic, nightly 03:15** → `data/backups/auto/studio-YYYYMMDD-HHMMSS.db` + `.json` (SHA-256, size, schema version, integrity result, table counts). Also "Back up now" on the Storage page. |
| Manual / pre-change copies | `data/backups/*.db` | kept forever (never pruned) |
| Video files, thumbnails, voices of each video | `data/productions/<id>/` | not copied automatically (size); copy to an external drive if needed |
| Installed models | `data/voices`, `data/models`, `~/.cache/huggingface`, `~/sadtalker` | `scripts/model_vault.sh <external folder>` |
| YouTube logins, API keys | macOS Keychain (`yt-automation-studio`) | not in any backup by design; covered by your Mac's own Keychain/iCloud/Time Machine |

## Retention
Newest **7** automatic copies + the newest copy of each of the last **4** weeks. A copy that failed its check is
deleted at once and never counts. **The newest good copy is never deleted.** Only `data/backups/auto/` is pruned.

## Checks
- Every copy is made with SQLite's online backup API (consistent while the app runs), written as `.partial`, checked
  (`PRAGMA integrity_check`, schema version, key tables present) and only then renamed.
- `python -m studio.backup drill` — restores the newest copy into a temporary folder, opens it, compares table counts
  with the live database. Runs automatically every Sunday after the backup. The live database is never touched.
- Real run on 31 Oct 2026: `studio-20261031-000405.db`, 13.7 MB, integrity ok, schema 21, counts identical to live
  (4 channels, 13 videos, 53 settings, 1 user, 902 tasks).

## Restore (step by step)
1. Stop the studio: `launchctl bootout gui/$(id -u)/com.ytautomation.studio`
2. Restore: `.venv/bin/python -m studio.backup restore data/backups/auto/studio-YYYYMMDD-HHMMSS.db`
   - refuses while the studio is running and refuses a copy that fails its check;
   - saves the current database first as `data/studio-before-restore-<time>.db` (so a restore can be undone).
3. Start the studio: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ytautomation.studio.plist`
4. Tasks that were running are put back in the queue automatically; uploads already on YouTube are never repeated
   (each video keeps its YouTube ID).

## Commands
```
.venv/bin/python -m studio.backup status     # newest good copy, count, size
.venv/bin/python -m studio.backup now        # make one now
.venv/bin/python -m studio.backup drill      # prove the newest can be restored
```
Tests: `tests/test_backup.py` (consistent copy, corruption detected, retention keeps last good, drill leaves the
live database untouched, restore refused while running).
