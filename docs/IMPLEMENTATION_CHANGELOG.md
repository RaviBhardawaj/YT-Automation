# Implementation changelog — Master Upgrade v2 (branch `upgrade/master-v2`, 30 Oct 2026)

Baseline: 364 tests OK (3 skipped). After: **388 tests OK (3 skipped)** — `python -m unittest discover -s tests`.
No test was deleted or weakened. One test was corrected: it expected the router to choose local Wan 2.2, which has
no execution adapter; it now checks that a real adapter (free GPU Space) is chosen and Wan 2.2 never is.

Database: migration **v20** (additive) — `channels.approve_first_n` (default 3) and table `channel_reviews`.
Backup before migrating: `data/backups/studio-before-master-v2-20261030-2236.db`.

## Reliability and rendering
| Change | Files | Tests |
|---|---|---|
| Complete film clip cache key (file identity, card text, size, encoder, renderer version) | `film_studio/edit.py`, `media/render.py` | `test_render_reliability` |
| Shots continue from the motion clip's last frame (no jump back to the start picture) | `film_studio/edit.py`, `media/render.py` | `test_short_motion_continues_from_its_last_frame_not_its_key_picture` |
| Format-checked concat: copy only when all clips match, else normalise + re-encode | `media/render.py`, `film_studio/edit.py` | `test_matching_clips_are_copied_mismatched_ones_reencoded` |
| `render_manifest.json` with every export | `film_studio/edit.py` | — |
| Frozen-picture check on the final render (videos and films) | `media/qa.py`, `film_studio/qa.py` | `FreezeTests` |
| Thumbnail background: relevance + quality + size, never cards | `media/thumbnail.py` | `ThumbnailTests` |

## Providers
| Change | Files | Tests |
|---|---|---|
| `adapter` flag; routing never chooses providers it can't run | `film_studio/providers.py` | `TruthfulnessTests` |
| LTX-2 and SadTalker listed as talking-clip providers (free GPU first, then the Mac) | `film_studio/providers.py` | `test_talking_clips_try_the_free_gpu_then_this_mac` |
| Video budget reports attempts and failed tries, not only successes | `film_studio/providers.py` | `test_failed_video_tries_are_counted_apart_from_clips_made` |

## Channel operations
| Change | Files | Tests |
|---|---|---|
| Global emergency stop (worker claim, schedules, auto-approve, upload) + banner + button | `ops.py`, `worker.py`, `tasks.py`, `pipeline.py`, `web/app.py`, `templates/base.html`, `templates/queue.html` | `StopTests` |
| Per-channel pause holds approved uploads | `pipeline.py` | `test_paused_channel_holds_an_approved_upload` |
| Publishing gate: channel opt-in required; first N uploads manual; clean QA | `ops.py`, `pipeline.py`, `channels.py`, `templates/channel.html` | `GateTests` |
| Audit trail of approvals, uploads, channel switches, stops, uninstalls (hash-chained) | `ops.py`, `pipeline.py`, `channels.py`, `storage.py`, `templates/queue.html` | `test_stop_and_its_reason_are_on_the_audit_trail` |
| Repeat check across all channels in QA | `ops.py`, `pipeline.py` | `RepeatTests` |

## Monetization and growth
| Change | Files | Tests |
|---|---|---|
| YPP rules with source URLs and check date; early tier marked unverified | `youtube/growth.py` | `test_thresholds_carry_their_source_and_check_date` |
| Honest status: unknown / in progress / requirements met / owner-recorded applied-accepted-rejected | `youtube/growth.py`, `web/app.py`, `templates/channel.html`, `templates/channels.html` | `StatusTests` |
| Policy readiness checks (AI persona on sensitive topics, disclosure, repetitive, reused, facts, kids) | `youtube/growth.py` | `PolicyTests` |
| Weekly decision report per channel (Mondays 07:15) | `learning.py`, `scheduler.py`, `tasks.py` | `WeeklyTests` |

## Earlier the same day (already on `main`)
SadTalker talking presenter on the Mac, Storage page with live Mac usage and uninstall buttons, flagged-scene
rewriting in the editorial review, free writer rotation — see git history (`1f6af55`, `fa630a5`).
