# Business continuity checklist

Tick these regularly (✅ = in place on 31 Oct 2026, ⬜ = your action).

## Daily (automatic)
- ✅ Database backup at 03:15, checked; 7 daily + 4 weekly kept; last good never deleted
- ✅ Interrupted tasks recovered after restarts; uploads never repeated
- ✅ Analytics, growth numbers, comments, lessons refreshed each morning
- ✅ Writers fall back across 4 free providers, then wait instead of failing

## Weekly
- ✅ Restore drill (Sundays, automatic) — check the Activity log says "passed"
- ✅ Weekly channel review (Mondays) — read each channel's decisions
- ⬜ Copy `data/backups` and `data/productions` to an external drive or cloud folder
- ⬜ Look at **AI providers** for anything "not now" for days

## Monthly
- ⬜ `scripts/model_vault.sh /Volumes/<drive>/studio-models` (≈20 GB — external drive only)
- ⬜ Re-check YouTube Partner Program pages and update `RULES` in `studio/youtube/growth.py`
- ⬜ Check free-tier terms of Gemini, Cloudflare, Ollama, Hugging Face (dates in `docs/AI_PROVIDER_MATRIX.md`)
- ⬜ `git pull` / push — the repository is the source of truth for code; `data/` is not in git

## Before big changes
- ✅ Emergency stop available (Activity log)
- ⬜ `python -m studio.backup now`
- ⬜ Work on a branch; run `python -m unittest discover -s tests`

## Known single points of failure
The Mac, the internet connection, the single Google Cloud project, the Keychain — see `DISASTER_RECOVERY_PLAN.md`.
