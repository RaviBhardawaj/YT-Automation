# Multi-channel studio — implementation report (24 Oct 2026)

## Why videos kept coming out as educational explainers
1. The pasted master prompt was split into 14 "niches"; the approved one was a prompt heading
   ("YOUR ROLE — YOU ARE THE CREATOR, NOT THE TEACHER").
2. Ideation searched that heading as a YouTube strategist, so ideas were advice about YouTube.
3. Global styles defaulted to "educational", and the only pipeline was a fact-checked explainer.
4. The user's own request never reached the script writer.

Fixed: a pasted prompt is now one niche, the fake niches and their ideas were rejected, and the legacy
global daily plan and refill schedules are off. Every job now starts from a request and a channel.

## What changed
| Area | Now |
|---|---|
| Channels | `channels` table (niche, language, tone, styles, workflow, formats, lengths, instructions, restrictions, schedule, auto-publish, privacy, YouTube link). Channels page to create, edit and pause channels. |
| Request first | `creative/request.plan`: request > channel > idea > defaults. A horror request on the comedy channel makes horror, with horror tone and music. |
| Routing | `studio/jobs.py`: animated stories → character/animation workflow; factual requests → researched pipeline with the request as the brief; empty request on the NIFTY channel → daily episode. |
| Animation | LLM screenplay (outline + dialogue), character bible with frozen designs and voices, cartoon renderer (lip-sync, expressions, camera, subtitles), original music, 1080p; horror scenes get a dark, cold grade with a vignette. |
| Characters | Per-channel bible; new characters are auto-added with a unique voice; add, approve and retire them in the UI. |
| Ideas | Per channel: story concepts for creative channels, evidence-backed topics for factual ones. Filters: channel, niche, format, language, style, type, age, status, sort, search, pagination, reset. Actions: Generate video, Create long, Create Short, Save, Variations, Reject/Restore, Open. |
| Dashboard / library | Channel selector, per-channel counts, next schedule, YouTube status. Library filters plus download and per-job Pause/Resume/Retry/Cancel. |
| Scheduling | One `channel_daily` slot per channel (time, days, on/off); at most one scheduled video per channel per day; tops up ideas when fewer than 4 remain. The NIFTY 3:40 PM schedule is unchanged. |
| Publishing | Separate YouTube login per channel (Keychain entry per channel). Before upload, the login is checked against the linked channel and the upload is refused on a mismatch. Auto-publish and privacy are set per channel. Long video first, then Shorts. |

## Acceptance tests (live service unless noted)
| # | Test | Result |
|---|---|---|
| 1 | Comedy animated episode | ✅ #39: 7 scenes, 3.6 min, QA passed, Shorts #41–43 |
| 2 | Hindi finance concept | ✅ #45 "शेयर मार्केट क्या है?" on a factual test channel, 27 scenes, QA passed |
| 3 | Scary animated horror on the comedy channel | ✅ #44: 4.3 min, scary/tense moods, request beat channel tone; Shorts #47–49 queued (paused from the dashboard, not rendered) |
| 4 | Niche change | ✅ New horror channel got 6 original horror/mystery ideas; no tutorials |
| 5 | Separate channels | ✅ 4 channels, separate ideas, libraries, counts, logins (unit + live) |
| 6 | Character consistency | ✅ #44 reused C1–C4 with frozen designs and voices; comedy ideas reuse the cast |
| 7 | Ideas filtering | ✅ unit tests + live pages |
| 8 | Idea → video | ✅ idea #201 → #46 queued as a Short (paused from the dashboard before rendering); a horror idea → #50 from the UI |
| 9 | Persistence after restart | ✅ restarted mid-script; task recovered and resumed |
| 10 | Failure and retry | ✅ #40 auto-retried with back-off; manual Retry re-runs the failed step (unit test) |
| 11 | Concurrency | ✅ one render at a time (media lane) while ideas run in parallel |
| 12 | Destination verification | ✅ NIFTY → "Talk with Trader" verified; unconnected channel refused; mismatch refused (unit test) |

Unit tests: 103 pass (`.venv/bin/python -m unittest discover -s tests`).

## Bugs found and fixed during testing
- A horror request inherited the comedy channel's "slapstick" tone.
- A new character could get a voice another character already uses.
- Horror scenes looked bright (only the background was tinted).
- "Created today" counted by UTC date.
- The dashboard redirected to the old setup interview.

## Dependencies / limits
- Visuals are the built-in 2D cartoon renderer; no external AI image or video generator is connected.
- Voices: Sarvam Bulbul (paid per character). Scripts: Ollama Cloud. Local Ollama has no models pulled.
- Only the NIFTY channel has a YouTube login. Other channels hold videos for approval until you connect
  them (Channels → channel → YouTube & publishing).
- Factual videos on Hindi channels sometimes fail the "on-screen text must be Hindi" check (as #40 did).
- Test channels "Paisa Samjho (test)" and "Raat Ki Kahaniyan (test)" can be paused or kept.
