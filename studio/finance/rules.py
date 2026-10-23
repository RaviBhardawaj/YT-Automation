"""Regulatory / platform rule registry.

Seeded from primary sources read on 2026-10-23 (see `evidence` for short quotes). Statuses:
  VERIFIED         text read at the official source; its effect on this channel is recorded
  REVIEW_REQUIRED  could not be verified, or its application to this channel is a legal
                   judgement — needs a qualified human decision (recorded in compliance_reviews)
A rule's review is only "resolved" by a recorded human review. Anything critical and unresolved
blocks public publication. Rules older than RECHECK_DAYS must be re-checked.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from .. import db

CHECKED_ON = "2026-10-23T00:00:00Z"
RECHECK_DAYS = 30

SEED: list[dict[str, Any]] = [
    # ------------------------------------------------------------------ SEBI
    {
        "id": "SEBI-RA-REGS", "category": "sebi", "authority": "SEBI",
        "title": "SEBI (Research Analysts) Regulations, 2014 [Last amended on October 10, 2026]",
        "url": "https://www.sebi.gov.in/legal/regulations/oct-2026/securities-and-exchange-board-of-india-research-analysts-regulations-2014-last-amended-on-october-10-2026-_105294.html",
        "document_url": "https://www.sebi.gov.in/sebi_data/attachdocs/oct-2026/1792127227860.pdf",
        "reference": "Consolidated text", "document_date": "2026-10-10", "effective_date": "as amended",
        "status": "VERIFIED", "critical": 0,
        "summary": "Reg 2(1)(u): a research analyst is a person who, for consideration, is engaged in the business of "
                   "providing research services. Reg 2(1)(fa): consideration is any economic benefit, 'whether from client "
                   "or otherwise'. Reg 2(1)(w): a 'research report' excludes (ii) discussions on broad-based indices and "
                   "(viii) technical analysis relating to the demand and supply in a sector or the index. Reg 2(1)(wa): "
                   "research services include buy/sell/hold recommendations, 'giving price target or stop loss target', "
                   "model portfolios and 'providing trading calls' with respect to listed securities, to the general "
                   "public. Reg 3(1): no person shall act as, or hold itself out as, a research analyst without registration.",
        "effect": "The channel must never give buy/sell/hold calls, price or stop-loss targets, trading calls, option "
                  "strikes or model portfolios, and must never describe itself as a research analyst. Index-level "
                  "technical discussion falls within the report exclusions, but whether a monetized channel publishing "
                  "forward-looking index levels provides 'research services' is a legal question (see LEGAL-RA-STATUS).",
        "evidence": "“research analyst” means a person who, for consideration, is engaged in the business of providing "
                    "research services | (ii) discussions on the broad-based indices | (viii) technical analysis relating to "
                    "the demand and supply in a sector or the index | iv. giving price target or stop loss target",
    },
    {
        "id": "SEBI-RA-AMEND-2026", "category": "sebi", "authority": "SEBI",
        "title": "SEBI (Research Analysts) (Amendment) Regulations, 2026",
        "url": "https://www.sebi.gov.in/legal/regulations/oct-2026/securities-and-exchange-board-of-india-research-analysts-amendment-regulations-2026_105155.html",
        "document_url": "https://www.sebi.gov.in/sebi_data/attachdocs/oct-2026/1791796596784.pdf",
        "reference": "F. No. SEBI/LAD-NRO/GN/2026/321 (Gazette No. 607, 9 Oct 2026)", "document_date": "2026-10-08",
        "effective_date": "30th day after Gazette publication (on or about 2026-11-08)",
        "status": "VERIFIED", "critical": 0,
        "summary": "Inserts a definition of 'institutional investor' and makes call recordings optional for "
                   "institutional-investor clients of registered research analysts.",
        "effect": "No direct effect on an unregistered educational channel. Recorded so later reviews know the latest "
                  "amendment was checked.",
        "evidence": "Provided that the requirement to maintain call recordings of communications with clients which are "
                    "institutional investors shall not be mandatory.",
    },
    {
        "id": "SEBI-INT-16A", "category": "sebi", "authority": "SEBI",
        "title": "SEBI (Intermediaries) Regulations, 2008 [Last amended on October 10, 2026] — Chapter IIIA, Reg 16A",
        "url": "https://www.sebi.gov.in/legal/regulations/oct-2026/securities-and-exchange-board-of-india-intermediaries-regulations-2008-last-amended-on-october-10-2026-_105373.html",
        "document_url": "https://www.sebi.gov.in/sebi_data/attachdocs/oct-2026/1792668197365.pdf",
        "reference": "Reg 16A, Explanation 4", "document_date": "2026-10-10", "effective_date": "in force",
        "status": "VERIFIED", "critical": 0,
        "summary": "SEBI-regulated persons and their agents may not associate with anyone who (i) gives advice or "
                   "recommendations on securities without registration/permission or (ii) makes any express or implied "
                   "claim of returns or performance. A person engaged in investor education is excluded only if they do "
                   "neither.",
        "effect": "The channel must never claim returns, accuracy or performance of its levels. Any sponsorship, "
                  "referral or promotion involving a broker or other regulated entity depends on the channel staying "
                  "outside both prohibited activities — refer such deals for legal review.",
        "evidence": "makes any claim, of returns or performance expressly or impliedly | shall not include a person who is "
                    "engaged in investor education",
    },
    {
        "id": "SEBI-INT-AMEND2-2026", "category": "sebi", "authority": "SEBI",
        "title": "SEBI (Intermediaries) (Second Amendment) Regulations, 2026 — Common Advertisement Code",
        "url": "https://www.sebi.gov.in/legal/regulations/oct-2026/securities-and-exchange-board-of-india-intermediaries-second-amendment-regulations-2026_105152.html",
        "document_url": "https://www.sebi.gov.in/sebi_data/attachdocs/oct-2026/1791796362093.pdf",
        "reference": "F. No. SEBI/LAD-NRO/GN/2026/323 (Chapter IIID, Reg 16G onwards)", "document_date": "2026-10-10",
        "effective_date": "180th day after Gazette publication (on or about April 2027)",
        "status": "VERIFIED", "critical": 0,
        "summary": "Adds a Common Advertisement Code for regulated entities' advertisements in any medium including "
                   "social media and streaming, with rules on celebrities/influencers.",
        "effect": "Applies to advertisements by regulated entities. Relevant if a broker, fund house or other regulated "
                  "entity sponsors or advertises through the channel — such content needs legal review first.",
        "evidence": "“advertisement” shall include any form of communication ... digital media (websites, social media, "
                    "streaming services",
    },
    {
        "id": "SEBI-CIRC-2026-PRICE-DATA", "category": "sebi", "authority": "SEBI",
        "title": "Norms for sharing and usage of price data for educational purposes",
        "url": "https://www.sebi.gov.in/legal/circulars/may-2026/norms-for-sharing-and-usage-of-price-data-for-educational-purposes_101293.html",
        "document_url": "https://www.sebi.gov.in/sebi_data/attachdocs/may-2026/1778242522289.pdf",
        "reference": "HO/47/17/12(11)2025-MRD-POD3/I/11107/2026", "document_date": "2026-05-08",
        "effective_date": "2026-07-01",
        "status": "VERIFIED", "critical": 0,
        "summary": "Uniform 30-day lag for sharing and usage of market price data for educational purposes. Modifies FAQ 8 "
                   "of the 29 Jan 2025 circular: a person engaged solely in education should not use the market price "
                   "data of the preceding thirty days to display the name of any security in a video etc. indicating the "
                   "future price, advice or recommendation.",
        "effect": "Episodes built only on sessions at least 30 days old, framed as historical case studies, stay closest "
                  "to the 'solely education' description. Daily episodes using the previous session to discuss the next "
                  "session fall outside it — the app marks that mode for legal review (LEGAL-DAILY-LEVELS) and never "
                  "publishes it publicly by default.",
        "evidence": "should not be using the market price data of the preceding thirty days | applicable with effect from "
                    "July 01, 2026",
    },
    {
        "id": "SEBI-CIRC-2025-ASSOC", "category": "sebi", "authority": "SEBI",
        "title": "Details/clarifications on provisions related to association of persons regulated by the Board, MIIs, "
                 "and their agents with persons engaged in prohibited activities (with FAQs)",
        "url": "https://www.sebi.gov.in/legal/circulars/jan-2025/details-clarifications-on-provisions-related-to-association-of-persons-regulated-by-the-board-miis-and-their-agents-with-persons-engaged-in-prohibited-activities_91356.html",
        "document_url": "https://www.sebi.gov.in/sebi_data/attachdocs/jul-2026/1784884437684.pdf",
        "reference": "SEBI/HO/MIRSD/MIRSD-PoD-1/P/CIR/2025/11 (FAQ 8 as modified on 2026-05-08)",
        "document_date": "2025-01-29", "effective_date": "in force; FAQ 8 revised w.e.f. 2026-07-01",
        "status": "VERIFIED", "critical": 0,
        "summary": "FAQs on the Reg 16A association restrictions, including what distinguishes education from "
                   "advice (FAQ 8) and branding/marketing tie-ups with regulated entities (FAQ 14).",
        "effect": "Confirms the 30-day data-lag test for 'solely education' and that regulated entities must not use "
                  "unregistered persons who give advice or claim returns for branding/marketing.",
        "evidence": "What distinguishes education and advice/recommendation? | Whether persons regulated by the Board and "
                    "their agents are allowed to associate for their branding/marketing/promotional activities",
    },
    {
        "id": "SEBI-CIRC-2024-REALTIME", "category": "sebi", "authority": "SEBI",
        "title": "Norms for sharing of real time price data to third parties",
        "url": "https://www.sebi.gov.in/legal/circulars/may-2024/norms-for-sharing-of-real-time-price-data-to-third-parties_83572.html",
        "document_url": "https://www.sebi.gov.in/sebi_data/attachdocs/may-2024/1716559553659.pdf",
        "reference": "SEBI/HO/MRD/MRD-PoD-3/P/CIR/2024/56 (para 2(iii)-(iv) modified 2026-05-08)",
        "document_date": "2024-05-24", "effective_date": "in force",
        "status": "VERIFIED", "critical": 0,
        "summary": "Exchanges, clearing corporations, depositories and intermediaries may not share real-time price data "
                   "with third parties except for orderly functioning or regulatory needs; education sharing now needs a "
                   "30-day lag and a legal agreement with audit-trail provisions.",
        "effect": "Real-time or near-real-time NIFTY data from an exchange or intermediary must not be used. The app "
                  "never labels end-of-day data as live and records the nature of every dataset.",
        "evidence": "ensure that no real time price data is shared with any third party",
    },
    {
        "id": "SEBI-RA-MASTER-CIRCULAR", "category": "sebi", "authority": "SEBI",
        "title": "Master Circular for Research Analysts (latest version) and RA public-appearance / social-media guidelines",
        "url": "https://www.sebi.gov.in/legal/master-circulars/",
        "status": "REVIEW_REQUIRED", "critical": 0,
        "summary": "Not yet read. Applies to registered research analysts.",
        "effect": "Only binding if the channel becomes (or is held to be) a research analyst. Must be reviewed together "
                  "with LEGAL-RA-STATUS.",
    },
    # ------------------------------------------------------------------ legal judgements
    {
        "id": "LEGAL-RA-STATUS", "category": "legal", "authority": "Qualified Indian securities-law counsel",
        "title": "Does a monetized YouTube channel publishing NIFTY 50 pivot levels provide 'research services' "
                 "'for consideration' requiring RA registration?",
        "status": "REVIEW_REQUIRED", "critical": 1,
        "summary": "Ad revenue may be 'consideration' (any economic benefit, from client or otherwise). The index itself is "
                   "not a listed security, but index derivatives are; S2/R2 levels could be read as price or stop-loss "
                   "targets.",
        "effect": "Public publication stays blocked until a qualified reviewer records a decision and any conditions.",
    },
    {
        "id": "LEGAL-DAILY-LEVELS", "category": "legal", "authority": "Qualified Indian securities-law counsel",
        "title": "Is a daily episode using the previous session's data (inside the 30-day window) permissible for this "
                 "channel, and on what conditions?",
        "status": "REVIEW_REQUIRED", "critical": 1,
        "summary": "SEBI's 30-day test describes 'solely education'. Daily pivot episodes discuss the next session using "
                   "the last session's prices.",
        "effect": "Daily-levels mode is never published publicly until resolved. Historical case-study mode (data at "
                  "least 30 days old, no forward-looking framing) is the default.",
    },
    {
        "id": "DISCLOSURE-HI", "category": "legal", "authority": "Qualified reviewer (Hindi + Indian securities law)",
        "title": "Hindi translation of the required disclosure",
        "status": "REVIEW_REQUIRED", "critical": 1,
        "summary": "Hindi videos speak, show and describe a Hindi translation of the English disclosure. The translation "
                   "was drafted by the app's author and has not been reviewed.",
        "effect": "Hindi episodes and lessons stay private-only until a reviewer confirms the Hindi wording (text in "
                  "studio/finance/i18n.py, key 'disclosure', and lessons.py).",
    },
    # ------------------------------------------------------------------ YouTube
    {
        "id": "YT-MONETIZATION", "category": "youtube", "authority": "YouTube",
        "title": "YouTube channel monetization policies (inauthentic and reused content)",
        "url": "https://support.google.com/youtube/answer/1311392",
        "document_date": "2025-07-15", "effective_date": "in force",
        "status": "VERIFIED", "critical": 0,
        "summary": "Inauthentic (repetitive or mass-produced, templated) and reused content is ineligible; AI personas "
                   "presenting as human experts giving finance advice cannot monetize; tools are fine if the final "
                   "product shows creative vision.",
        "effect": "Each episode must contain episode-specific observations and education (checked), must not be a "
                  "near-duplicate of earlier episodes (checked), and the synthetic narrator must never present itself "
                  "as a human expert or give advice.",
        "evidence": "renamed the “repetitious content” policy to “inauthentic content” | templated storylines",
    },
    {
        "id": "YT-SPAM", "category": "youtube", "authority": "YouTube",
        "title": "Spam, deceptive practices & scams policies",
        "url": "https://support.google.com/youtube/answer/2801973",
        "status": "VERIFIED", "critical": 0,
        "summary": "Prohibits misleading titles/thumbnails/descriptions, get-rich-quick schemes, and repetitive or templated "
                   "content aimed at inflating engagement.",
        "effect": "Titles and thumbnails are generated deterministically from verified data; return or profit language "
                  "is blocked.",
        "evidence": "maliciously misleading titles, thumbnails, descriptions | get rich quick",
    },
    {
        "id": "YT-SYNTHETIC", "category": "youtube", "authority": "YouTube",
        "title": "Disclosing use of altered or synthetic (GenAI) content",
        "url": "https://support.google.com/youtube/answer/14328491",
        "status": "VERIFIED", "critical": 0,
        "summary": "Disclosure is required for realistic altered/synthetic content (real people, real events, realistic "
                   "scenes). AI help with scripts and captions is listed as minor. A synthetic narrator voice is not "
                   "directly addressed.",
        "effect": "Charts are generated from data (not realistic depictions). The description states the narration is a "
                  "synthetic voice; the 'altered content' flag stays off unless realistic synthetic imagery is used.",
    },
    # ------------------------------------------------------------------ data & assets
    {
        "id": "DATA-NSE-TERMS", "category": "data", "authority": "National Stock Exchange of India",
        "title": "NSE website Terms of Use", "url": "https://www.nseindia.com/static/nse-terms-of-use",
        "status": "VERIFIED", "critical": 0,
        "summary": "Website data and content (including technical charts) may not be reproduced, broadcast or published "
                   "without NSE's prior written permission; systematic or automated data collection is prohibited.",
        "effect": "The app never scrapes NSE. Data copied from nseindia.com may not be shown in public videos without "
                  "written permission or a licence.",
        "evidence": "shall not be copied ... broadcasted, published ... without prior written permission of NSE | "
                    "prohibited to conduct any systematic or automated data collection",
    },
    {
        "id": "DATA-NSE-INDICES-LICENCE", "category": "data", "authority": "NSE Indices Limited",
        "title": "niftyindices.com Terms of Use and Disclaimer (index data licence, NIFTY trademarks)",
        "url": "https://www.niftyindices.com/terms-of-use",
        "status": "REVIEW_REQUIRED", "critical": 1,
        "summary": "No automated data collection without written consent; no republication without prior written "
                   "permission; 'Use or distribution of the Company’s ... index data ... require a license'; NIFTY marks "
                   "belong to NSE Indices.",
        "effect": "Public, monetized use of NIFTY 50 values needs a licence or written permission (or a licensed vendor "
                  "whose terms cover public video). Record the licence on the Compliance page; until then public "
                  "publication is blocked.",
        "evidence": "may not conduct any systematic or automated data collection | require a license from NSE Indices "
                    "Limited",
    },
    {
        "id": "ASSET-LICENCES", "category": "assets", "authority": "Asset licensors",
        "title": "Fonts, voice, music and chart assets",
        "status": "VERIFIED", "critical": 0,
        "summary": "Captions use Noto Sans / Noto Sans Devanagari (SIL Open Font License 1.1). Kokoro voice weights are "
                   "Apache-2.0. No music by default. Charts are drawn by this app from the imported data.",
        "effect": "The gate checks every rendered asset's licence on each episode.",
    },
]


def seed() -> None:
    now = db.iso()
    for r in SEED:
        row = {"url": None, "document_url": None, "reference": None, "document_date": None, "effective_date": None,
               "evidence": None, "last_checked": CHECKED_ON, "updated_at": now, **r}
        existing = db.q1("SELECT id FROM compliance_rules WHERE id=?", (r["id"],))
        if existing:
            continue
        db.insert("compliance_rules", row)


# Rules that only apply to some episodes: (modes, languages); None = all.
APPLIES = {
    "LEGAL-DAILY-LEVELS": ({"daily_levels", "daily_review"}, None),
    "DISCLOSURE-HI": (None, {"hi"}),
    "LEGAL-RA-STATUS": ({"daily_levels", "daily_review", "historical_case_study"}, None),
    "DATA-NSE-INDICES-LICENCE": ({"daily_levels", "daily_review", "historical_case_study"}, None),
}


def applies(rule_id: str, mode: str, lang: str) -> bool:
    modes, langs = APPLIES.get(rule_id, (None, None))
    return (modes is None or mode in modes) and (langs is None or lang in langs)


def latest_review(rule_id: str) -> dict[str, Any] | None:
    return db.q1("SELECT * FROM compliance_reviews WHERE rule_id=? ORDER BY id DESC LIMIT 1", (rule_id,))


def effective_status(rule: dict[str, Any]) -> dict[str, Any]:
    """Combine the source status, any recorded human review, and staleness."""
    review = latest_review(rule["id"])
    checked = db.parse_iso(rule.get("last_checked"))
    stale = bool(checked and db.utcnow() - checked > timedelta(days=RECHECK_DAYS))
    if review and review["decision"] in ("resolved", "resolved_with_conditions"):
        state = "RESOLVED"
    elif rule["status"] == "VERIFIED":
        state = "VERIFIED"
    else:
        state = "REVIEW_REQUIRED"
    if stale and state != "RESOLVED":
        state = "RECHECK_DUE"
    return {**rule, "state": state, "stale": stale, "review": review,
            "blocking": bool(rule["critical"]) and state not in ("RESOLVED", "VERIFIED")}


def all_rules() -> list[dict[str, Any]]:
    return [effective_status(r) for r in db.q("SELECT * FROM compliance_rules ORDER BY category, id")]


def blocking() -> list[dict[str, Any]]:
    return [r for r in all_rules() if r["blocking"] or (r["critical"] and r["state"] == "RECHECK_DUE")]


def add_review(rule_id: str, reviewer: str, decision: str, *, qualification: str = "", conditions: str = "",
               notes: str = "", evidence_url: str = "", reviewed_on: str = "") -> int:
    from . import audit
    if decision not in ("resolved", "resolved_with_conditions", "still_open"):
        raise ValueError("decision must be resolved, resolved_with_conditions or still_open")
    if not reviewer.strip():
        raise ValueError("reviewer name is required")
    rid = db.insert("compliance_reviews", {
        "rule_id": rule_id, "reviewer": reviewer.strip(), "qualification": qualification.strip(), "decision": decision,
        "conditions": conditions.strip(), "notes": notes.strip(), "evidence_url": evidence_url.strip(),
        "reviewed_on": reviewed_on or db.iso()[:10], "created_at": db.iso()})
    audit.record("compliance_review_recorded", {"rule_id": rule_id, "reviewer": reviewer, "decision": decision,
                                                "conditions": conditions, "evidence_url": evidence_url}, actor=reviewer)
    return rid


def mark_rechecked(rule_id: str, notes: str, actor: str) -> None:
    from . import audit
    db.ex("UPDATE compliance_rules SET last_checked=?, updated_at=? WHERE id=?", (db.iso(), db.iso(), rule_id))
    audit.record("compliance_rule_rechecked", {"rule_id": rule_id, "notes": notes}, actor=actor)
