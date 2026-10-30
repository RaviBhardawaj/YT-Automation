# Production readiness report — 31 Oct 2026

Verdict: **ready for continued single-owner operation on this Mac, with the limits below.** Not yet ready as a
multi-customer hosted SaaS (single machine, no off-site backup automation, one Google Cloud project).

## Evidence
| Area | Result | How verified |
|---|---|---|
| Test suite | **413 run, OK, 3 skipped** | `python -m unittest discover -s tests` (all external services mocked) |
| App start + pages | 200 on /login after restarts; page-render tests for Storage, Channel, Providers, Activity log | TestClient + live restart |
| Database migrations | v19 → v21 applied to the live database (additive) | backup before, `PRAGMA user_version` = 21 |
| Backup / restore | real backup 13.7 MB, integrity ok; restore drill counts identical to live | real run 31 Oct |
| Repository independence | SadTalker rebuilt from this repo only in 91 s, checksums OK | real run |
| Provider fallback | writers rotate, then wait; pictures fall back to local FLUX; talking falls back to SadTalker | real runs 30–31 Oct + tests |
| Interrupted jobs | recovered after 3 live restarts (videos #156, #161) | observed |
| Duplicate uploads | finished session reconciled, no second upload; re-approval after upload skipped | tests |
| Real uploads | 4 AI News videos (30 Oct); Kids video #156 + Short #162 scheduled for 31 Oct 12:30 UTC | YouTube IDs in the database |
| Film Studio | sample film #161 rendered: 34 s, 1080p, 4 talking-face shots, QA honest | real run |

## Incident found and fixed during this pass
Animated-story videos used their own automatic approval and skipped the new "first N uploads need the owner"
rule: Kids video #156 and Short #162 were approved automatically and uploaded (scheduled, not yet public at the
time of writing). Fixed in `pipeline.approve()` so **every** workflow's automatic approval passes the same gate
(`test_ops.EveryWorkflowTests`). Shorts #163/#164, approved but not yet sent, were put back to "Ready for review".

## Status by category
| Category | Status |
|---|---|
| Passed (real) | uploads, backup + drill, SadTalker independence, film render, provider fallback, restart recovery |
| Passed (mock only) | retention per scene, traffic sources, comment sorting + drafts, expired login, timeouts, all-writers-down |
| Requires credentials / time | retention & comments on live videos (needs public videos ≥ 2 days old); LTX-2 and Wan 2.2 live calls (free GPU minutes, reset daily) |
| Requires manual verification | lip-sync quality, caption safe zones on unusual graphics, offline operation with the network physically off |
| Not implemented | growth experiments that change live titles/thumbnails; automatic comment replies (deliberately); off-site backup copy; impressions/CTR (not in the public Analytics API) |

## Security review (this pass)
No `shell=True`/`os.system` in our code (SadTalker's own code shells out to ffmpeg with app-generated paths);
SQL uses placeholders (f-strings only insert fixed table names); no route returns a secret (tested); file paths are
confined to the data folder; providers page and backup/uninstall actions are owner-only; comment text is treated
as data in prompts and reply drafts are never posted. Not re-audited: CSP/CORS headers, upload MIME sniffing.

## Deployment
Mac: `install/install-mac.sh --service`; optional talking presenter: `install/install-sadtalker.sh`; restart:
`launchctl kickstart -k gui/$(id -u)/com.ytautomation.studio`. See `docs/RUNBOOK.md`.
