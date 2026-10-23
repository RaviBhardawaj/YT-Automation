# Compliance report — NIFTY 50 Fibonacci pivot channel

Prepared 23 Oct 2026 from primary sources read that day. **This is not legal advice.** It records what was checked and what the software enforces, so a qualified Indian securities lawyer can review it efficiently. Nothing in this repository establishes regulatory compliance or guarantees monetization.

## 1. Verified items (primary sources read)

| ID | Source | Date / effective | What it means for this channel |
|---|---|---|---|
| SEBI-RA-REGS | [SEBI (Research Analysts) Regulations, 2014, as last amended 10 Oct 2026](https://www.sebi.gov.in/legal/regulations/oct-2026/securities-and-exchange-board-of-india-research-analysts-regulations-2014-last-amended-on-october-10-2026-_105294.html) | consolidated 10 Oct 2026 | Reg 2(1)(w) excludes "discussions on the broad-based indices" and "technical analysis relating to … the index" from *research report*. But Reg 2(1)(wa) *research services* include "giving price target or stop loss target" and "providing trading calls". Reg 2(1)(fa) defines *consideration* as any economic benefit "whether from client or otherwise". Reg 3(1) bars acting as, or holding out as, a research analyst without registration. |
| SEBI-RA-AMEND-2026 | [RA (Amendment) Regulations, 2026](https://www.sebi.gov.in/legal/regulations/oct-2026/securities-and-exchange-board-of-india-research-analysts-amendment-regulations-2026_105155.html), F. No. SEBI/LAD-NRO/GN/2026/321 | notified 8 Oct 2026; in force 30 days after the 9 Oct gazette | Only adds an "institutional investor" definition and makes call recordings optional for institutional clients. No effect on this channel. |
| SEBI-INT-16A | [Intermediaries Regulations 2008, Reg 16A](https://www.sebi.gov.in/legal/regulations/oct-2026/securities-and-exchange-board-of-india-intermediaries-regulations-2008-last-amended-on-october-10-2026-_105373.html) | in force | Regulated entities may not associate with anyone who gives unregistered advice or recommendations, or claims returns or performance. Investor education is exempt only if it does neither. This matters for any broker sponsorship. |
| SEBI-INT-AMEND2-2026 | [Intermediaries (Second Amendment) Regulations, 2026](https://www.sebi.gov.in/legal/regulations/oct-2026/securities-and-exchange-board-of-india-intermediaries-second-amendment-regulations-2026_105152.html) | in force 180 days after gazette (≈ Apr 2027) | Adds a Common Advertisement Code for regulated entities, covering social media and streaming. Applies if a regulated entity advertises through the channel. |
| SEBI-CIRC-2026-PRICE-DATA | [Norms for sharing and usage of price data for educational purposes](https://www.sebi.gov.in/legal/circulars/may-2026/norms-for-sharing-and-usage-of-price-data-for-educational-purposes_101293.html), HO/47/17/12(11)2025-MRD-POD3/I/11107/2026 | 8 May 2026; effective 1 Jul 2026 | A person "engaged solely in education" should not use "the market price data of the preceding thirty days" to indicate future price, advice or recommendation. **Daily T-1 pivot episodes fall outside that description.** |
| SEBI-CIRC-2025-ASSOC | [Association with persons engaged in prohibited activities (FAQs)](https://www.sebi.gov.in/legal/circulars/jan-2025/details-clarifications-on-provisions-related-to-association-of-persons-regulated-by-the-board-miis-and-their-agents-with-persons-engaged-in-prohibited-activities_91356.html), SEBI/HO/MIRSD/MIRSD-PoD-1/P/CIR/2025/11 | 29 Jan 2025; FAQ 8 revised 8 May 2026 | Confirms the 30-day test. FAQ 14 covers branding and marketing tie-ups with regulated entities. |
| SEBI-CIRC-2024-REALTIME | [Norms for sharing of real-time price data](https://www.sebi.gov.in/legal/circulars/may-2024/norms-for-sharing-of-real-time-price-data-to-third-parties_83572.html), SEBI/HO/MRD/MRD-PoD-3/P/CIR/2024/56 | 24 May 2024; modified 8 May 2026 | No real-time exchange data for third parties; educational sharing has a 30-day lag. |
| YT-MONETIZATION | [YouTube channel monetization policies](https://support.google.com/youtube/answer/1311392) | "inauthentic content" update 15 Jul 2025 | Templated or mass-produced series are ineligible. AI personas posing as human finance experts can't monetize. |
| YT-SPAM | [Spam, deceptive practices & scams](https://support.google.com/youtube/answer/2801973) | — | No misleading titles or thumbnails, no get-rich-quick content. |
| YT-SYNTHETIC | [Disclosing altered or synthetic content](https://support.google.com/youtube/answer/14328491) | — | Disclosure is required for *realistic* synthetic media. Scripts and captions are "minor" uses. A synthetic narrator isn't addressed, so the description discloses it voluntarily. |
| DATA-NSE-TERMS | [NSE Terms of Use](https://www.nseindia.com/static/nse-terms-of-use) | read 23 Oct 2026 | No reproduction or broadcast of site data (including technical charts) without NSE's written permission. Automated collection is prohibited. |
| ASSET-LICENCES | Noto Sans / Noto Sans Devanagari (SIL OFL, licence files stored); [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) (Apache-2.0); Piper `hi_IN-pratham-medium` (training data **CC BY-NC-SA 4.0 — non-commercial**) | — | Charts are drawn by the app; no third-party imagery. The description credits whichever voice was actually used. Videos voiced by Pratham can be uploaded **private only** (gate check 7 is review-level, and a separate upload guard refuses public, unlisted and scheduled releases). |
| NSE-HOLIDAYS-2026 | NSE circular NSE/CMTR/71775 (12 Dec 2025) and [NSE/CMTR/72260](https://nsearchives.nseindia.com/content/circulars/CMTR72260.pdf) (12 Jan 2026) | calendar year 2026 | 16 trading holidays plus Muhurat trading on Sun 8 Nov 2026 are typed into `studio/finance/calendar.py`. The PDF timed out when fetched, so the dates were cross-checked against two secondary listings (smallcase, Upstox) plus the circular's own summary. **Add the 2027 list when NSE publishes it**; until then the app warns that the year is unverified. |

## 2. Unresolved: REVIEW REQUIRED (public release is blocked)

1. **LEGAL-RA-STATUS (critical).** Does a monetized channel publishing NIFTY 50 pivot levels provide "research services" "for consideration" that require RA registration? Ad revenue may count as consideration. The index itself isn't a listed security, but its derivatives are. S2 and R2 could be read as price or stop-loss targets.
2. **LEGAL-DAILY-LEVELS (critical, daily mode only).** Is a daily episode built on the previous session's data permissible, and on what conditions, given the 30-day education test?
3. **DATA-NSE-INDICES-LICENCE (critical).** [niftyindices.com terms](https://www.niftyindices.com/terms-of-use) forbid automated collection and republication without written permission, and state that "use or distribution of … index data … require a license from NSE Indices Limited". A licence, written permission, or a licensed vendor whose terms cover public, monetized video must be recorded.
4. **SEBI-RA-MASTER-CIRCULAR (non-critical).** Not yet read. Binding only if the channel is, or is held to be, a research analyst.
5. **Font licences.** Resolved by switching to OFL fonts. macOS system fonts remain only as a fallback, and the gate flags them for review.

## 3. Assumptions

- The channel operator isn't SEBI-registered. The app never states or implies registration either way.
- "NIFTY 50" is used descriptively in titles. Trademark use is covered by item 3 above.
- A Hindi or Hinglish translation of the disclosure needs the same legal review before use. The required disclosure is currently the English text.
- YouTube's audit requirement for API uploads (uploads stay private until the project is audited) still applies, as before.

## 4. What the software enforces

- **Data.** Manual import or a licensed vendor only; no scraping. Provenance (source, retriever, retrieval time, timezone, data nature) and SHA-256 are recorded. Validation covers OHLC consistency, conflicting duplicates, weekend sessions, gaps (never filled), future dates, incomplete same-day candles, and daily data labelled as intraday.
- **Calculations.** `Decimal`-based Fibonacci pivots, recomputed and compared at gate time.
- **Script.** The model sees only the fact sheet. A deterministic linter rejects trade calls, targets, stop-losses, options and strikes, guarantees, accuracy or return claims, urgency, paid groups and registration claims (English, Hindi, Hinglish), plus any number not on the fact sheet. Rejected drafts are rewritten automatically, and three failures mean NEEDS REVIEW.
- **Deterministic packaging.** The title is always "NIFTY 50 Fibonacci Pivot Analysis | <date>"; the thumbnail shows real S2, P and R2; the description states source, date, data nature, limitations, the disclosure and credits.
- **Disclosure.** Spoken (final scene), on-screen (a ribbon on every frame) and in the description.
- **Gate.** The 10 checks run before approval and again immediately before upload. A critical failure blocks all uploads and marks NEEDS REVIEW. An open review item allows only a private upload. Public release also needs a manual switch, and unattended publishing a second one.
- **Audit.** A hash-chained log covers imports, fact sheets, scripts, charts, thumbnails, QA, gate results, approvals, licence records, review decisions, setting changes and uploads. Any edit to a past entry is detected.

## 5. Human / legal actions needed before any public episode

1. Ask qualified Indian securities counsel to answer LEGAL-RA-STATUS and LEGAL-DAILY-LEVELS, then record the decisions on the Compliance page.
2. Obtain and record an NSE Indices data licence or written permission, or a licensed vendor agreement explicitly covering public video and monetization.
3. Have the master circular for research analysts reviewed (only relevant if registration turns out to be needed).
4. Upload the first episodes **privately**, review them, then decide whether to enable public release.
5. Re-check every rule at least every 30 days. The app flags stale rules and blocks on stale critical rules.

## 6. Market-data providers reviewed (23 Oct 2026)

| Provider | What it offers | Use for this channel |
|---|---|---|
| [TejHQ](https://tejhq.dev/) / [tej-bazaar](https://github.com/tejhq/tej-bazaar) | **End-of-day only**: NSE/BSE *equity* bhavcopy (EQ/BE/BZ series), adjusted closes, MIT-licensed code. The free key allows 1,000 calls a day; commercial use is listed only for the Pro tier (₹4,999/month). | **No NIFTY 50 index OHLC and no intraday bars**, so it can't supply this channel's data. Its claim that the data is "free to redistribute" is the maintainer's own; it is not NSE's terms. |
| Your Pivot Reversal project (`~/Downloads/Pivot Reversal Strategy`) | Gets 15-minute, 1-hour and daily candles from Yahoo Finance's unofficial chart endpoint (`query1/2.finance.yahoo.com/v8/finance/chart`), and from Angel One / Dhan broker APIs. | **Not used as a data source.** Yahoo's chart endpoint is undocumented and its terms don't permit republication. Broker market data is licensed for the account holder's personal use. The app does reuse the project's **method**: 15-Min levels from the last 25 completed 15-minute bars, 1-Hour levels from the last 35 completed 1-hour bars. |
| NSE / NSE Indices websites | Official, manual download only | Allowed for verification and private review. Publication needs NSE Indices' licence or written permission (DATA-NSE-INDICES-LICENCE). |

**Result:** no documented free source permits automated retrieval *and* public, monetized display of NIFTY 50 index data, especially intraday. The pipeline therefore uses manual import of files you obtain yourself. Every dataset, including 15-minute and 1-hour data, records its source. The gate checks the licence of **each** dataset an episode uses, and intraday analysis is switched off (and claims about it are blocked) when no verified intraday file exists. A licensed vendor whose agreement covers public video and monetization is what's needed to lift the block.
