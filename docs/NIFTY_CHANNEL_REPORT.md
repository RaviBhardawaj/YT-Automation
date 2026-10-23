# NIFTY 50 Fibonacci Pivot channel: build report

Written 23 Oct 2026, after implementing the "Master build prompt: automated NIFTY 50 YouTube channel" on the existing app. **Not legal advice.** Nothing here claims compliance or monetization. See [COMPLIANCE_REPORT.md](COMPLIANCE_REPORT.md).

## 1. Existing architecture (inspected first, kept)

- **App:** FastAPI web dashboard on `127.0.0.1:8765` (Jinja2, CSRF, CSP).
- **Storage:** SQLite (WAL mode, versioned migrations, now v4).
- **Work queue:** persistent task queue with two worker lanes, retries and crash recovery.
- **Scheduler:** persistent; missed runs catch up.
- **AI router:** per-task provider choice (local Ollama → Ollama Cloud → Claude/Gemini when configured), Pydantic-validated output, a lint-feedback retry loop and a spending limit.
- **Media:** Kokoro/Piper text-to-speech, Pillow graphics (Devanagari-capable), FFmpeg rendering, technical QA.
- **YouTube:** official Data API v3 with OAuth, resumable uploads.
- **Finance package (already present):**
  - manual data import with provenance and SHA-256;
  - `Decimal` Fibonacci pivots;
  - a fact sheet the script may not go beyond;
  - prohibited-language and unverified-number lint;
  - the 10-check compliance gate;
  - a hash-chained audit log;
  - the 7-part Hindi lesson series at 4 PM.

## 2. What was added or changed

| Spec area | Implementation | Files |
|---|---|---|
| Trading session, holidays | NSE 2026 holiday table with circular references. Previous and next session, "consecutive session" checks. Holiday bars are rejected; incomplete intraday sessions are flagged. | `studio/finance/calendar.py`, `marketdata.py` |
| 15-minute / 1-hour analysis | Per level (S2, S1, P, R1, R2): first touch, first close beyond, failed breakout (close beyond, then back), not tested (with distance). Bar-to-bar structure. 15m vs 1h agreement. **Pivot Reversal project method:** 15-Min levels from the last 25 fifteen-minute bars, 1-Hour levels from the last 35 one-hour bars. All deterministic; nothing filled in. | `studio/finance/intraday.py`, `analysis.py` |
| Long episode structure | Sections follow the data: context → calculation → S2/R2 → following session → 15-minute → 1-hour → 15-Min/1-Hour levels → concept → scenarios → limitations → recap, plus the disclosure. A section with no verified data is dropped and the lint blocks it. Length is sized to the verified material (no padding). Scripts are written in parts of up to four sections because local and cloud models stop around 700 words; each part is linted, then the joined script again. Only the recap may invite viewers to subscribe. | `studio/finance/episode.py` |
| Premium visuals | 15-minute and 1-hour candle charts with level lines. Labels are pushed apart so they never overlap, and event markers ("S2 touch", "back inside") are placed clear of nearby candles. Daily vs 15-Min vs 1-Hour panel. Animated build-ups (levels appear one by one, candles revealed left to right). A section label at every chapter start. Source, timeframe and retrieval time sit on every chart. | `studio/finance/charts.py`, `studio/media/render.py`, `studio/pipeline.py` |
| Thumbnails | The focal point comes from the data: failed breakout, close beyond, level tested, "S2 & R2 not tested", or the level ladder. The date shown is the date of what is shown. | `episode.py` |
| **Shorts** | After a long episode passes its own gate, up to N self-contained segments are cut from it (default 4; fewer when fewer are good). Segments are whole scenes, chosen as the best-fitting run within a section and favouring verified level reactions, at most 44 s. Each is re-laid out for 9:16 (not cropped) with a truthful hook, a pointer to the full episode and a short spoken disclosure. Each Short is a separate production with its own QA, gate, approval and upload record (`parent_id`). The description links the full episode once that has a YouTube ID. A manifest JSON records the segment times, titles, outputs, validation and upload status. | `studio/finance/shorts.py`, `db.py` (v4) |
| **4 PM daily episode** (your request) | A new `daily_review` mode: today's levels (from the previous session) → how the index actually traded today around them (daily + 15-minute + 1-hour) → the calculation → **reference levels for the next session** → 15-Min/1-Hour levels at today's close → conditional scenarios ("if price holds above the pivot, many analysts watch…") → limitations → recap. Title: "NIFTY 50 पिवट स्तर \| <today> का विश्लेषण और <next session> के स्तर". **No predictions:** a lint rejects forecast wording in English and Hindi ("will rise", "expected to", बढ़ेगा, गिरेगा…). | `analysis.py`, `episode.py`, `i18n.py` |
| Same-day data only | The 4 PM job (16:00, NSE trading days) needs today's daily bar **retrieved after 15:30**, plus complete 15m (25 bars) and 1h (7 bars) sessions. If anything is missing it generates nothing and re-checks every 30 minutes; after 22:00 it goes ahead without intraday. The gate makes it **critical** if an episode's reviewed session isn't the latest session closed when the episode was made, so old data can't appear as today's. | `tasks.py`, `gate.py` |
| Voice | Every video and Short uses Piper **hi_IN-pratham-medium** (your choice). Its dataset licence is CC BY-NC-SA 4.0 (non-commercial), so every video is **private-only**; descriptions carry the attribution. | settings `voice`, `voice.by_language` |
| Auto-upload | New switch "auto private upload", **on**: once the gate allows it, each video (and each Short) uploads as PRIVATE by itself. YouTube is connected to the channel "Ravi Bhardwaj". Public release stays off. | `episode._compliance_outcome`, Compliance page |
| Dashboard | Status labels: PENDING / PROCESSING / READY FOR REVIEW / APPROVED / UPLOADED · PRIVATE / PUBLISHED / FAILED / NEEDS REVIEW. Compliance page: session date, holiday sources, data freshness per timeframe, daily-episode and Shorts switches. Episode page: Shorts table, manifest link, a "Make Shorts" button, and intraday and 15-Min/1-Hour panels. | `studio/web/…` |
| Reports | A `report.md` is saved next to every finance video: data, levels, observations, script stats, chapters, QA, gate, Shorts. | `episode.write_report` |
| Gate hardening | The licence of **every** dataset an episode uses (daily, 15m, 1h) is checked. Shorts get their own title, educational-value and similarity rules. The full disclosure must appear in every description. | `studio/finance/gate.py` |
| Upload safety | A production that already has a YouTube ID is never uploaded again. Rejecting a video mid-production stops it. A non-commercial voice is private-only. | `studio/pipeline.py` |
| Voice credit | The description credits the voice actually used and its licence (Pratham's CC BY-NC-SA requires attribution). | `i18n.voice_credit` |

## 3. Data providers (spec §6)

See COMPLIANCE_REPORT §6. In short:
- **TejHQ** has end-of-day equity bhavcopy only: no NIFTY 50 index OHLC and no intraday bars.
- Your **Pivot Reversal** project gets candles from Yahoo's unofficial chart API and from broker APIs. Both are personal-use, so they are not used as a source; only the project's level method is reused.
- No permitted source for automated retrieval of the index, especially intraday, was found. Data therefore comes from **manual import**, every dataset keeps its own provenance, and the gate blocks public release until a licence is recorded.

## 4. Tests (all actually run)

`.venv/bin/python -m unittest discover -s tests`: **71 tests, all passing** (23 Oct 2026). New tests cover:
- the holiday calendar (Dussehra, Gandhi Jayanti, Muhurat, weekends), and session selection before and after the close;
- rejection of holiday bars and detection of incomplete intraday sessions;
- level states (held, closed beyond, failed breakout, not tested with distance), structure, and 15m/1h disagreement;
- the 15-Min / 1-Hour levels using exactly the last N bars;
- Shorts: whole-scene segments within 44 s, trimming, the hook / segment / pointer / disclosure order, parent numbers only, `#Shorts` titles;
- the daily trigger: skips holidays and waits for data;
- the gate: an intraday dataset from a prohibited source is critical; a non-commercial voice is review-level and never public;
- uploads: an already-uploaded production is never re-uploaded; a rejected production stops.

Earlier tests (Fibonacci formulas, CSV parsing and validation, missing, stale and malformed data, prohibited language, gate checks, audit chain) still pass.

## 5. Historical sample episode (spec §15.7–12)

Real NIFTY data isn't available on this Mac, so the samples use a **clearly labelled synthetic fixture** (source "Synthetic test fixture (NOT market data)", status PROHIBITED, shown on every chart). Its 15-minute bars aggregate exactly into the 1-hour and daily bars.

| # | What | Result |
|---|---|---|
| 6 | English historical case study (16 → 17 Sep 2026), Kokoro voice | 1,250-word script in 27 scenes → **10.0 min**, 12 chapters, technical QA **passed**. Gate **NEEDS REVIEW** only because the source is synthetic (check 3); every other critical check passed. Thumbnail "FAILED BREAKOUT AT S2" taken from the 15-minute data. |
| 15 | **Hinglish 4 PM daily review** (17 Sep reviewed, levels for 18 Sep), Pratham voice | 1,145-word conversational Hinglish script → **8.4 min**, QA **passed**, description 4,895 bytes. Gate **NEEDS REVIEW**, as designed: check 2 *"daily review is about 2026-09-17 but the latest closed session when it was made was 2026-10-22 — old data must not be shown as today's"*, and check 3 (synthetic source). |
| 16–18 | Shorts from #15 (review, S2/R2 meaning, 15m vs 1h) | Rendered 1080×1920, QA passed, gate blocked (synthetic or old data). They ran **63–71 s**, over the 60 s target, so they were superseded: the framing is now shorter, scenes are capped at about 70 words, and a "next-session levels" Short is always made from the verified fact sheet. **The re-cut wasn't run** (see below). |

| 20–23 | Re-cut Shorts from #15 (shorter framing, fact-sheet "next session levels" Short) | **50–60 s** each (#23 levels for 18 Sep: 50.3 s), QA passed. Gate still NEEDS REVIEW (synthetic / old data), so nothing was uploaded even with the owner override on. That confirms the override doesn't cover critical checks. |

**Later on 23 Oct 2026, at the owner's request:**
- **Voice:** every video now uses Kokoro `hm_omega` (Apache-2.0, commercial use allowed) instead of Pratham.
- **Publishing order:** the long video goes first. A Short waits for its episode's YouTube ID, re-checking every 10 minutes, then links to it. It is held if the episode is blocked, or after 24 hours.
- **Reach:** search-friendly but factual titles ("NIFTY 50 18 सितंबर के लेवल्स | 17 सितंबर का पूरा एनालिसिस | फिबोनाची पिवट सपोर्ट रेजिस्टेंस"), about 9 hashtags in every description (#NIFTY50 #NiftyAnalysis #ShareMarket …), and search tags. No "#prediction"-style hashtags, because they would misdescribe the content.
- **Ending:** every video ends with a news-anchor sign-off after the disclaimer ("…मिलते हैं अगले ट्रेडिंग डे, शाम 4 बजे। तब तक के लिए, नमस्कार!") over a closing card.

**Owner override (23 Oct 2026, at the owner's explicit request):** public, unattended publishing of the 4 PM long video and its Shorts is on while the legal, data-licence, Hindi-disclosure and Pratham-licence review items stay open. The acceptance is recorded in the audit log; it can be revoked on the Compliance page. Critical checks still block every upload.

Files: `data/productions/00006/` and `00015/` (final.mp4, thumbnail.jpg, report.md, shorts_manifest.json).

## 6. Remaining limitations and manual steps

1. **Real data.** Import real NIFTY 50 files (daily, plus 15-minute and 1-hour if you want the intraday sections) from a source you are allowed to use. The sample above is synthetic and can never be uploaded.
2. **Data licence.** Public, monetized display of NIFTY 50 data needs an NSE Indices licence or permission, or a licensed vendor (COMPLIANCE_REPORT §2.3).
3. **Legal review.** LEGAL-RA-STATUS and LEGAL-DAILY-LEVELS (and DISCLOSURE-HI for Hindi) must be answered by qualified counsel before any public episode. Daily "levels for tomorrow" episodes stay private until then.
4. **YouTube OAuth.** Add your Google account as a test user (Google Auth Platform → Audience), then connect in Settings › YouTube. Uploads from an unaudited API project stay private.
5. **Voice.** Pratham (Hindi) is non-commercial: private test uploads only. Use a Kokoro voice for public videos.
6. **Language default.** This spec says English by default; your earlier instruction was Hindi by default. The app is still on Hindi for the India audience. English is one setting away (Compliance › Daily episode language, or the audience on each episode).
7. **2027 holidays.** Add NSE's 2027 circular to `calendar.py` when it is published. The app warns until then.
8. **Background music** is off (no licensed track configured).
