# Channel automation and monetization readiness

Nothing here can guarantee views, revenue, Partner Program acceptance or a date. The studio makes progress
measurable and keeps channels inside YouTube's rules.

## Channel control plane

Each channel has its own niche, audience, language, tone, styles, workflow, formats and lengths, voice,
presenter/characters, schedule (up to 4 times a day, chosen days, timezone), publishing privacy and time,
uploads switch, made-for-kids flag, OAuth token (Keychain, per channel), analytics and lessons.

### Publishing gate (`studio/ops.py: auto_publish_block`)
A finished video is published **without a human** only when all hold:
1. the channel has **auto-publish on** (the studio-wide "auto" mode no longer overrides a channel that didn't opt in);
2. the channel is **active** (not paused);
3. the **emergency stop** is off;
4. the channel already has at least its **first N uploads** (default 3, Channel → YouTube & publishing) —
   until then each video waits for your approval;
5. **every QA check passed with no warnings** (including frozen picture, repeat, fact check, editorial review);
6. the channel's YouTube account is connected and uploads are enabled.

Otherwise the video waits in "Ready for review" with the reason logged.

### Approval modes
- manual: auto-publish off;
- first N manual, then automatic: auto-publish on with N > 0 (default 3);
- automatic after hard checks: auto-publish on, N = 0.

### Pause and emergency stop
- **Pause a channel** (channel page): no new daily videos, nothing auto-published, approved uploads wait.
- **Emergency stop** (Activity log page, owner only): nothing new starts on any channel except bookkeeping
  (analytics, growth numbers, housekeeping, learning, weekly review); nothing is approved or uploaded
  automatically; approved uploads wait and re-check every 30 minutes. A step already running finishes. A red
  banner shows on every page with a "Switch off" button.

### Audit trail
Approvals (who, privacy, time), uploads (video ID, channel), channel switches (pause, auto-publish, uploads,
privacy, first-N), emergency stop on/off with reason, monetization status changes and uninstalls go to the
hash-chained `audit_log` (editing or deleting an entry breaks the chain — Compliance page). Shown on the
Activity log page.

### Duplicate protection across channels
QA compares each new video's title and spoken script with every upload of the last 180 days **on all
channels**. A near-repeat is a warning (never auto-published). Shorts cut from their own episode don't count.

## Monetization readiness

### Rules (checked 2026-10-30 on the official pages)
- Partner Program overview — https://support.google.com/youtube/answer/72851
  - 1,000 subscribers **and** either 4,000 qualified watch hours from public long-form videos in 12 months,
    or 10 million qualified Shorts views in 90 days.
  - YPP updates announced from 1 Feb 2027; updated terms must be accepted in YouTube Studio by 31 Jan 2027.
  - The earlier-access (fan funding) thresholds are **not** on that page as checked; the app shows them as
    "unverified".
- Monetization policies — https://support.google.com/youtube/answer/1311392
  - reused content; inauthentic (mass-produced / repetitive) content;
  - **AI personas related to sensitive topics**: AI hosts presenting as experts giving health, legal, financial
    or political advice are not monetizable.
- YouTube API Services Terms — https://developers.google.com/youtube/terms/api-services-terms-of-service

Re-check these pages before relying on them; update `RULES` in `studio/youtube/growth.py` with the new date.

### Metrics (distinct, from the channel's own authorization)
subscribers (Data API), qualified watch hours from long videos in 12 months, Shorts views in 90 days, public
uploads in 90 days (Analytics/Data API). These approximate YouTube's own counters, which use public views only.

### Status (never more than the evidence)
`unknown` (numbers not loaded) → `in progress` → `requirements met` (apply in YouTube Studio → Earn; YouTube
still reviews — **eligibility is not acceptance**) → `applied` / `accepted` / `rejected` only when **you record
it** (no API reports a YPP decision).

### Policy readiness (channel → YouTube & publishing)
pass / check / fail / not checked, from the channel's own uploads of the last 60 days:
AI persona on a sensitive topic · synthetic content disclosed · inauthentic/repetitive titles · reused content
(copied wording) · metadata and open fact issues · made-for-kids on children's channels.

**Current flag (30 Oct 2026):** AI News uses a realistic AI presenter on news that includes market stories.
Keep the presenter strictly reporting (no advice, no tips) and decide — possibly a human voice/face — before
applying for monetization on that channel.

## Weekly growth loop
Every Monday 07:15 (and on demand) each channel gets a decision per public video: continue / improve / repackage /
test / too early / no data, from views per day vs the channel's median and average viewed %. Impressions, CTR,
traffic sources and returning viewers are **not collected**, so "repackage" is a stated hypothesis. Change one
packaging variable at a time. The daily AI lesson review (`learning.review`) continues alongside.

## Launch ramp (recommended)
1. Approve the first 3–5 videos of each channel yourself (default N = 3).
2. Test new channels with private/unlisted uploads.
3. Turn auto-publish on only after routing, QA and upload recovery have been seen working.
4. Give pilot capacity to the strongest one or two channels first.
