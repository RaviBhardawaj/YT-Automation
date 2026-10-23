"""Deterministic content checks for finance episodes (and finance-adjacent general videos).

* prohibited_language(): direct trade instructions, targets/stop-losses, guarantees, return or
  accuracy claims, urgency/fear, paid-signal promotion and registration claims — English,
  Devanagari Hindi and Romanised Hinglish.
* unverified_numbers(): every number shown or spoken must come from the episode fact sheet.
* These are guard rails, not legal advice: a pass means "nothing obviously prohibited was
  found", never "compliant".
"""
from __future__ import annotations

import re
from typing import Any

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

# (pattern, category, explanation)
PROHIBITED: list[tuple[str, str, str]] = [
    # direct trade instructions
    (r"\b(buy|sell|short)\s+(now|today|tomorrow|immediately|at|above|below|near|around|on|nifty|calls?|puts?|the\s+dip|on\s+dips?|on\s+rise|futures?)\b",
     "trade_instruction", "direct buy/sell instruction"),
    (r"\b(go|going)\s+(long|short)\b", "trade_instruction", "position instruction"),
    (r"\b(enter|exit|take|initiate|square\s+off)\s+(a|the|this|your|new)?\s*(trade|position|entry|long|short)\b",
     "trade_instruction", "trade entry/exit instruction"),
    (r"\b(entry|entry\s+point|entry\s+level|buying\s+zone|selling\s+zone|buy\s+zone|sell\s+zone)\b", "trade_instruction",
     "entry-level language"),
    (r"\bbook\s+(your\s+)?profits?\b", "trade_instruction", "profit-booking instruction"),
    (r"\bstop[\s-]?loss(es)?\b|\bSL\b|\btrailing\s+stop\b", "target_stoploss", "stop-loss language"),
    (r"(?<!not\s)(?<!not\sa\s)(?<!aren't\s)\b(price\s+|profit\s+|upside\s+|downside\s+|next\s+)?targets?\b",
     "target_stoploss", "target language (levels must be called reference levels)"),
    (r"\b(strike\s+price|strikes?|call\s+option|put\s+option|option\s+(trade|buying|selling)|straddle|strangle|expiry\s+trade)\b",
     "options_call", "options/strike language"),
    # "signal" / "tips" only in the trading-call sense ("GMP signals strong demand" or "5 tips to learn" are fine)
    (r"\b(trading|intraday|positional|btst|stbt)\s+calls?\b"
     r"|\b(stock|share|trading|intraday|option|options|nifty|banknifty|free|paid|sure[\s-]?shot|jackpot)\s+tips?\b"
     r"|\b(buy|sell|buying|selling|entry|exit|trading|trade|intraday|option|options|free|paid|premium)\s+signals?\b"
     r"|\bsignals?\s+(group|channel|service|subscription)\b", "paid_signal", "trading calls / tips / signals"),
    # guarantees and performance claims
    (r"\b(guarantee[sd]?|assured|risk[\s-]?free|no\s+risk|zero\s+risk|sure[\s-]?shot|100\s?%|confirmed\s+(profit|move|breakout)|"
     r"fixed\s+returns?|double\s+your|get\s+rich|jackpot|multi[\s-]?bagger|can'?t\s+lose|never\s+fails?)\b",
     "guarantee", "guarantee or certainty claim"),
    (r"\b(win\s*rate|hit\s*rate|accuracy\s+of|accurate\s+\d|success\s+rate|backtest(ed|ing)?|track\s+record|"
     r"returns?\s+of\s+\d|made\s+\d+\s*%|\d+\s*%\s+(profit|returns?|gain))\b", "performance_claim",
     "performance / accuracy / backtest claim"),
    (r"\b(will|is\s+going\s+to|is\s+set\s+to|is\s+bound\s+to|must|definitely|certainly|surely)\s+(rise|fall|go\s+up|go\s+down|"
     r"rally|crash|reach|hit|touch|break|bounce|reverse|test)\b", "certain_prediction",
     "prediction stated with certainty (use conditional language)"),
    # urgency / fear / promotion
    (r"\b(hurry|act\s+(now|fast)|don'?t\s+miss|last\s+chance|before\s+it'?s\s+too\s+late|limited\s+time|"
     r"crash\s+is\s+coming|panic|fomo)\b", "urgency", "urgency or fear messaging"),
    (r"\b(telegram|whats\s?app\s+group|premium\s+(group|channel|membership)|paid\s+(group|course|membership)|join\s+(my|our))\b",
     "promotion", "paid group / off-platform promotion"),
    # regulatory identity claims
    (r"\bsebi[\s-]*(registered|approved|certified|recognised|recognized|authori[sz]ed|licensed|exempt)\b|"
     r"\bregistered\s+(research\s+analyst|investment\s+advis[eo]r|RA|RIA)\b|\bIN[HA]\d{9}\b", "registration_claim",
     "regulatory registration/approval claim"),
    (r"\bnot\s+(investment|financial)\s+advice\b.*\b(so|therefore)\b", "disclaimer_bypass",
     "disclaimer used to justify advice"),
    # Hindi (Devanagari)
    (r"(अभी|आज|कल)\s*(खरीद|बेच)|खरीद(ें|ो|िए|ना\s+चाहिए)|बेच(ें|ो|िए|ना\s+चाहिए)|एंट्री\s*(लें|करें|लो)",
     "trade_instruction", "Hindi buy/sell/entry instruction"),
    (r"स्टॉप\s*लॉस|टारगेट|लक्ष्य\s*मूल्य", "target_stoploss", "Hindi target/stop-loss language"),
    (r"गारंटी|पक्का\s*(मुनाफा|प्रॉफिट|फायदा)|100\s*%\s*(सही|पक्का)|जैकपॉट|मल्टीबैगर|बिना\s+जोखिम|जोखिम\s*मुक्त",
     "guarantee", "Hindi guarantee/certainty claim"),
    (r"सेबी\s*(रजिस्टर्ड|पंजीकृत|प्रमाणित|अनुमोदित)", "registration_claim", "Hindi registration claim"),
    (r"(ज़रूर|जरूर|निश्चित\s+रूप\s+से|पक्का)\s+\S*\s*(जाएगा|पहुँचेगा|पहुंचेगा|गिरेगा|चढ़ेगा|टूटेगा)", "certain_prediction",
     "Hindi prediction stated with certainty"),
    (r"जल्दी\s+करें|आखिरी\s+मौका|मौका\s+हाथ\s+से|छूट\s+न\s+जाए", "urgency", "Hindi urgency messaging"),
    (r"टेलीग्राम|व्हाट्सएप\s+ग्रुप|प्रीमियम\s+ग्रुप", "promotion", "Hindi paid group promotion"),
    # Hinglish (Roman)
    (r"\b(abhi|aaj|kal)\s+(kharid|bech)\w*|\bkharid(o|en|iye|lo)\b|\bbech(o|en|iye|do)\b", "trade_instruction",
     "Hinglish buy/sell instruction"),
    (r"\bpakk[ae]\s+(profit|munafa|faayda|fayda)\b|\bpakka\s+(target|move)\b", "guarantee", "Hinglish certainty claim"),
]

_COMPILED = [(re.compile(p, re.I), cat, why) for p, cat, why in PROHIBITED]
# Option contract codes are matched case-sensitively: lowercase "pe" is everyday Hinglish ("level pe").
_COMPILED.append((re.compile(r"\b\d{3,6}\s?(CE|PE)\b|\b(CE|PE)\s+(option|strike|buy|sell|kharid)", 0), "options_call",
                  "option contract (CE/PE) language"))

# Phrases that legitimately contain a flagged word in an educational/disclosure context.
ALLOWED_CONTEXT = [
    re.compile(r"not\s+(a\s+)?(price\s+)?targets?", re.I),
    re.compile(r"not\s+guarantees?\s+of", re.I),
    re.compile(r"no\s+guarantee", re.I),
    re.compile(r"are\s+not\s+(trade\s+)?signals?", re.I),
    re.compile(r"not\s+(a\s+)?(buy|sell)\s+(or\s+(buy|sell)\s+)?(signal|recommendation)", re.I),
    re.compile(r"100\s?%\s+of\s+the\s+(range|way)", re.I),
    re.compile(r"(की\s+)?गारंटी\s+नहीं"),
    re.compile(r"टारगेट\s+नहीं|लक्ष्य\s+नहीं"),
]


def prohibited_language(text: str) -> list[dict[str, str]]:
    findings = []
    t = text.translate(_DEVANAGARI_DIGITS)
    masked = t
    for ok in ALLOWED_CONTEXT:
        masked = ok.sub(lambda m: " " * len(m.group(0)), masked)
    for rx, cat, why in _COMPILED:
        for m in rx.finditer(masked):
            snippet = t[max(0, m.start() - 30): m.end() + 30].replace("\n", " ")
            findings.append({"category": cat, "match": m.group(0), "why": why, "context": snippet})
    return findings


_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\w])")

ALWAYS_ALLOWED = {0.0, 0.382, 0.618, 1.0, 38.2, 61.8, 100.0, 50.0, 15.0, 30.0, 20.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0}


def allowed_numbers(facts: dict[str, Any]) -> set[float]:
    allowed = set(ALWAYS_ALLOWED)
    for v in facts.get("numbers", {}).values():
        if v is None:
            continue
        for nd in (0, 1, 2):
            allowed.add(round(float(v), nd))
    allowed |= {float(x) for x in facts.get("date_numbers", [])}
    return allowed


def _ascii_digits(text: str) -> str:
    """Map digits of any script (Devanagari, Bengali, …) to ASCII so their values get checked too."""
    import unicodedata
    return "".join(str(unicodedata.decimal(ch)) if ch.isdecimal() and not ch.isascii() else ch for ch in text)


def unverified_numbers(text: str, facts: dict[str, Any]) -> list[str]:
    allowed = allowed_numbers(facts)
    bad = []
    for m in _NUM.finditer(_ascii_digits(text)):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if not any(abs(v - a) < 0.0051 for a in allowed):
            bad.append(m.group(1))
    return bad


def disclosure_text(facts: dict[str, Any], source_name: str) -> str:
    from . import i18n
    L = facts.get("lang", "en")
    return i18n.t("disclosure", L, nature=i18n.nature(facts["data_nature"], L), source=source_name,
                  date=facts["basis_label"])
