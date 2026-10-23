"""Pre-upload compliance gate (the 10 checks of the channel's compliance specification).

Severity:
  critical — blocks every upload; the episode is marked NEEDS REVIEW
  review   — blocks public / unlisted / scheduled release; a PRIVATE upload for manual review
             is still allowed after a human approves it
Nothing here can be silently overridden: results are stored with the production and written
to the hash-chained audit log, and upload code re-runs the decision before sending anything.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import db, settings
from ..media import graphics
from ..research import dedup
from . import analysis, audit, lint, marketdata, pivots, rules

CRITICAL, REVIEW = "critical", "review"
LIVE_WORDS = re.compile(r"(?<!not\s)\b(live|real[\s-]?time|right\s+now|as\s+we\s+speak|currently\s+trading)\b"
                        r"|लाइव|रियल[\s-]?टाइम|अभी\s+के\s+अभी", re.I)
INTRADAY_WORDS = re.compile(r"\b(15[\s-]?minute|1[\s-]?hour|hourly|intraday|15m|1h)\b|इंट्राडे|15[\s-]?मिनट|1[\s-]?घंटे|घंटे\s+के\s+चार्ट",
                            re.I)
NEGATION = re.compile(r"\b(no|not|unavailable|without|cannot|can't|isn't|wasn't)\b|नहीं|बिना|उपलब्ध\s+न", re.I)


def _affirmative_mentions(rx: re.Pattern, text: str) -> list[str]:
    """Matches of `rx` in sentences that are not negated ('no intraday data', 'not live' are fine)."""
    out = []
    for sentence in re.split(r"(?<=[.!?।])\s+|\n", text):
        if rx.search(sentence) and not NEGATION.search(sentence):
            out += [m.group(0) for m in rx.finditer(sentence)]
    return out


def owner_override() -> dict[str, Any] | None:
    """The channel owner's recorded acceptance of the open review-level risks (legal, data licence, voice licence).
    It never covers critical checks: wrong or unverified numbers, old data, recommendation/forecast language,
    missing disclosures or a prohibited (e.g. synthetic) data source still block every upload."""
    o = settings.get("compliance.owner_override")
    return o if isinstance(o, dict) and o.get("active") else None


def _check(no: int, name: str, ok: bool, severity: str, detail: str, evidence: Any = None) -> dict[str, Any]:
    return {"no": no, "name": name, "ok": bool(ok), "severity": severity, "detail": detail, "evidence": evidence}


def _texts(p: dict[str, Any]) -> dict[str, str]:
    pkg = p["package"]
    scenes = pkg.get("scenes", [])
    return {
        "narration": "\n".join(s["narration"] for s in scenes),
        "on_screen": "\n".join(s.get("on_screen_text", "") for s in scenes),
        "captions": "\n".join(s.get("caption_text", "") for s in scenes),
        "title": pkg.get("title", ""),
        # chapter timestamps ("1:23 Title") are generated from the render, not data claims
        # chapter timestamps ("1:23 Title") and a Short's excerpt range ("(3:09–3:34)") come from the render
        "description": re.sub(r"\(\d{1,2}:\d{2}[–-]\d{1,2}:\d{2}\)", "",
                              re.sub(r"(?m)^\d{1,2}:\d{2}(:\d{2})?\s", "", pkg.get("final_description") or pkg.get("description", ""))),
        "thumbnail": "\n".join(pkg.get("thumbnail", {}).get("lines", [])) or pkg.get("thumbnail", {}).get("text", ""),
        "tags": ", ".join(pkg.get("tags", [])),
    }


def _shingles(text: str, n: int = 5) -> set[tuple[str, ...]]:
    words = dedup.normalize(text).split()
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def _similarity_to_previous(p: dict[str, Any], body: str) -> tuple[float, int | None]:
    mine = _shingles(body)
    framing = ("disclosure", "intro", "outro", "hook", "signoff") if p.get("kind") == "nifty_short" else ("disclosure", "intro", "outro", "signoff")
    best, best_id = 0.0, None
    kinds = ("nifty_short",) if p.get("kind") == "nifty_short" else ("nifty_pivot", "fib_lesson")
    for other in db.q(f"SELECT id, package FROM productions WHERE kind IN ({','.join('?' * len(kinds))}) AND id<>? "
                      "AND package IS NOT NULL AND status NOT IN ('rejected')", (*kinds, p["id"])):
        pkg = db.loads(other["package"], {}) or {}
        theirs = _shingles("\n".join(s["narration"] for s in pkg.get("scenes", [])
                                     if s.get("purpose") not in framing))
        if mine and theirs:
            j = len(mine & theirs) / len(mine | theirs)
            if j > best:
                best, best_id = j, other["id"]
    return best, best_id


def run(p: dict[str, Any], assets: list[dict[str, Any]]) -> dict[str, Any]:
    from .. import profile
    facts = (p.get("episode") or {}).get("facts") or {}
    with profile.using_language(facts.get("language") or p.get("language")):
        return _run(p, assets)


def _run(p: dict[str, Any], assets: list[dict[str, Any]]) -> dict[str, Any]:
    ep = p.get("episode") or {}
    facts = ep.get("facts") or {}
    pkg = p["package"]
    mode = facts.get("mode", "daily_levels")
    texts = _texts(p)
    everything = "\n".join(texts.values())
    checks: list[dict[str, Any]] = []
    src = marketdata.source(facts["source_id"]) if facts.get("source_id") else None

    # 1 — applicable rules reviewed
    blockers = [r for r in rules.blocking() if rules.applies(r["id"], mode, facts.get("lang", "en"))]
    stale = [r["id"] for r in rules.all_rules() if r["state"] == "RECHECK_DUE"]
    checks.append(_check(1, "Current applicable rules reviewed", not blockers and not stale, REVIEW,
                         ("all applicable rules verified or resolved by a recorded review" if not blockers and not stale else
                          "unresolved: " + ", ".join([r["id"] for r in blockers] + [f"{s} (re-check due)" for s in stale])),
                         [r["id"] for r in blockers]))

    # 2 — figures match verified data and deterministic calculations
    problems = []
    lesson = mode == "lesson"
    if facts and not lesson:
        ds = marketdata.dataset(facts["dataset_id"])
        if ds["sha256"] != facts.get("dataset_sha256") or not Path(ds["stored_path"] or "").exists() or \
                audit.file_sha256(Path(ds["stored_path"])) != ds["sha256"]:
            problems.append("stored dataset is missing or its SHA-256 no longer matches")
        bars = {b["trade_date"]: b for b in marketdata.daily_bars(facts["dataset_id"])}
        basis = bars.get(facts["basis_date"])
        if not basis:
            problems.append("basis session not found in the dataset")
        else:
            recalc = pivots.fibonacci_pivots(basis["high"], basis["low"], basis["close"])["levels"]
            if recalc != facts["pivots"]["levels"]:
                problems.append("recomputed pivot levels differ from the episode's levels")
    if mode == "daily_review" and facts.get("review_date"):
        # The 4 PM show must be about the session that had just closed when the episode was made — never old data
        from . import calendar
        made = db.parse_iso(p.get("created_at") or db.iso()).astimezone(marketdata.IST)
        expected = calendar.session_status(made.date(), after_close=(made.hour, made.minute) >= marketdata.SESSION_CLOSE)
        if facts["review_date"] != expected["basis_session"]:
            problems.append(f"daily review is about {facts['review_date']} but the latest closed session when it was made "
                            f"was {expected['basis_session']} — old data must not be shown as today's")
    if facts:
        for field, text in texts.items():
            bad = lint.unverified_numbers(text, facts)
            if bad:
                problems.append(f"{field}: numbers not in the verified fact sheet: {', '.join(sorted(set(bad))[:8])}")
    else:
        problems.append("episode has no verified fact sheet")
    checks.append(_check(2, "Displayed figures match verified data and calculations", not problems, CRITICAL,
                         "every number traced to the dataset or the pivot calculation" if not problems else "; ".join(problems),
                         problems))

    # 3 — data source permits the intended use
    if lesson:
        permitted, why = True, "no market data used — hypothetical teaching examples only"
    elif src:
        permitted, why = marketdata.source_permits_publication(src)
        # every dataset the episode draws on (daily, 15-minute, 1-hour) must permit the use
        extra = set((facts.get("intraday") or {}).get("dataset_ids", {}).values()) | \
            {m["dataset_id"] for m in (facts.get("mtf") or {}).values()}
        for ds_id in sorted(extra):
            s2 = marketdata.source(marketdata.dataset(ds_id)["source_id"])
            ok2, why2 = marketdata.source_permits_publication(s2)
            if not ok2:
                permitted, why = False, f"{why}; intraday dataset #{ds_id} ({s2['name']}): {why2}"
                if s2["status"] == "PROHIBITED":
                    src = s2
    else:
        permitted, why = False, "no data source recorded"
    checks.append(_check(3, "Data source permits public video and monetization", permitted,
                         CRITICAL if src and src["status"] == "PROHIBITED" else REVIEW, why,
                         {"source": src["name"] if src else None, "terms": src["terms_url"] if src else None}))

    # 4 — no fabricated facts or performance claims
    fab = [f for f in lint.prohibited_language(everything) if f["category"] in ("performance_claim", "guarantee")]
    intraday_mentions = _affirmative_mentions(INTRADAY_WORDS, texts["narration"] + "\n" + texts["on_screen"])
    if intraday_mentions and not facts.get("intraday") and not facts.get("mtf"):
        fab.append({"category": "fabrication", "match": ", ".join(intraday_mentions[:3]),
                    "why": "intraday analysis mentioned but no verified intraday data was used"})
    unknown_obs = [o for s in pkg.get("scenes", []) for o in s.get("observation_ids", [])
                   if o not in {x["id"] for x in facts.get("observations", [])}]
    if unknown_obs:
        fab.append({"category": "fabrication", "match": ", ".join(unknown_obs), "why": "cites observations that do not exist"})
    checks.append(_check(4, "No fabricated facts or performance claims", not fab, CRITICAL,
                         "none found" if not fab else "; ".join(f"{f['why']}: '{f['match']}'" for f in fab[:6]), fab))

    # 5 — no prohibited direct recommendation
    rec = [f for f in lint.prohibited_language(everything)
           if f["category"] not in ("performance_claim", "guarantee", "registration_claim")]
    checks.append(_check(5, "No prohibited recommendation, target, call or urgency language", not rec, CRITICAL,
                         "none found" if not rec else "; ".join(f"{f['why']}: '{f['match']}'" for f in rec[:6]), rec))

    # 6 — title, thumbnail, voiceover and description represent the video accurately
    issues = []
    from . import i18n
    L = facts.get("lang", "en")
    from . import episode as fin_episode
    expected_title = facts["title"] if lesson else ep.get("expected_title") or (
        fin_episode.title_for(facts) if facts.get("basis_label") else "?")
    if texts["title"] != expected_title:
        issues.append(f"title must be the factual template: {expected_title!r}")
    live = _affirmative_mentions(LIVE_WORDS, everything)
    if facts and facts.get("data_nature") != "intraday" and live:
        issues.append(f"end-of-day data described as live: '{live[0]}'")
    desc = texts["description"]
    from . import lessons as fin_lessons
    needles = (((fin_lessons.HYPO_NOTE[L], "hypothetical-example notice"),) if lesson else
               ((facts.get("basis_label", "§"), "trading date"), (src["name"] if src else "§", "data source"),
                (i18n.nature(facts.get("data_nature", "§"), L), "data nature")))
    for needle, label in needles:
        if needle not in desc:
            issues.append(f"description does not state the {label}")
    thumb = pkg.get("thumbnail", {})
    if thumb.get("generated_by") != "deterministic":
        issues.append("thumbnail was not generated from the verified levels")
    checks.append(_check(6, "Title, thumbnail, voiceover and description are accurate", not issues, CRITICAL,
                         ("deterministic title and thumbnail; hypothetical-example notice stated" if lesson else
                          "deterministic title and thumbnail; source, date and data nature stated") if not issues else
                         "; ".join(issues), issues))

    # 7 — copyright and asset permissions
    asset_issues, asset_review = [], []
    for a in assets:
        lic = (a.get("license") or "").lower()
        if not lic:
            asset_issues.append(f"scene {a['scene_index']}: no licence recorded")
        elif re.search(r"\bnc\b|non-?commercial", lic):
            asset_issues.append(f"scene {a['scene_index']}: non-commercial licence")
    voice = (p.get("timeline") or {}).get("voice", {})
    nc_voice = voice.get("commercial") is False
    if nc_voice:
        # User decision 2026-10-23: allowed for private test uploads only, never public.
        asset_review.append(f"voice {voice.get('voice_id')} has non-commercial training data "
                            "(private test uploads only; use an Apache-2.0 Kokoro voice for public videos)")
    elif voice.get("commercial") is None:
        asset_review.append(f"voice {voice.get('voice_id')} licence unconfirmed")
    for f in graphics.font_licences():
        if f["ok"] != "yes":
            asset_review.append(f"font {f['font']}: {f['licence']}")
    if settings.get("render.music_file") and not settings.get("render.music_license"):
        asset_issues.append("background music has no licence recorded")
    sev7 = CRITICAL if asset_issues else REVIEW
    checks.append(_check(7, "Copyright and asset permissions checked", not asset_issues and not asset_review, sev7,
                         "charts generated from the dataset; OFL fonts; Apache-2.0 voice" if not (asset_issues or asset_review)
                         else "; ".join(asset_issues + asset_review), {"fonts": graphics.font_licences()}))

    # 8 — required disclosures present and accurate
    d_issues = []
    expected_disclosure = ep.get("disclosure", "")
    spoken = [s for s in pkg.get("scenes", []) if s.get("purpose") == "disclosure"]
    if not expected_disclosure:
        d_issues.append("no disclosure text generated")
    if not spoken or spoken[-1]["narration"].strip() != expected_disclosure.strip():
        d_issues.append("spoken disclosure scene missing or altered")
    if not (p.get("timeline") or {}).get("disclosure_ribbon"):
        d_issues.append("on-screen disclosure ribbon not rendered")
    if (ep.get("full_disclosure") or expected_disclosure) and (ep.get("full_disclosure") or expected_disclosure) not in desc:
        d_issues.append("description lacks the full disclosure")
    reg = [f for f in lint.prohibited_language(everything) if f["category"] == "registration_claim"]
    if reg:
        d_issues.append("registration/approval claim present: " + ", ".join(f["match"] for f in reg))
    checks.append(_check(8, "Required disclosures present and accurate", not d_issues, CRITICAL,
                         "spoken, on-screen and description disclosures present; no registration claims" if not d_issues
                         else "; ".join(d_issues), d_issues))

    # 9 — meaningful, original, episode-specific educational value
    framing = ("disclosure", "intro", "outro", "hook", "signoff") if p.get("kind") == "nifty_short" else ("disclosure", "intro", "outro", "signoff")
    body = "\n".join(s["narration"] for s in pkg.get("scenes", []) if s.get("purpose") not in framing)
    used = {o for s in pkg.get("scenes", []) for o in s.get("observation_ids", [])}
    sim, sim_id = _similarity_to_previous(p, body)
    v_issues = []
    if p.get("kind") == "nifty_short":
        seg = ep.get("segment", {})
        if seg.get("purpose") not in ("calculation", "levels", "mtf") and not used:
            v_issues.append("the Short cites no verified observation")
        if len(body.split()) < 40:
            v_issues.append(f"educational segment too short ({len(body.split())} words)")
    else:
        if len(used) < 3:
            v_issues.append(f"only {len(used)} episode-specific observations used (need at least 3)")
        if len(body.split()) < 110:
            v_issues.append(f"educational narration too short ({len(body.split())} words)")
        teaching = ("concept", "teach") + (("calculation", "scenarios") if mode == "daily_review" else ())
        if not any(s.get("purpose") in teaching for s in pkg.get("scenes", [])):
            v_issues.append("no educational concept segment")
    if sim > 0.45:
        v_issues.append(f"too similar to episode #{sim_id} ({sim:.0%} shared 5-word phrases) — skip rather than publish filler")
    checks.append(_check(9, "Meaningful, original educational value", not v_issues, CRITICAL,
                         f"{len(used)} observations, {len(body.split())} words, max similarity to past episodes {sim:.0%}"
                         if not v_issues else "; ".join(v_issues), {"observations_used": sorted(used), "similarity": sim}))

    # 10 — unresolved legal / regulatory questions referred
    legal_open = [r for r in rules.all_rules() if r["category"] == "legal" and r["state"] not in ("RESOLVED",)
                  and rules.applies(r["id"], mode, facts.get("lang", "en"))]
    checks.append(_check(10, "Unresolved legal questions referred for qualified review", not legal_open, REVIEW,
                         "none open" if not legal_open else
                         "open for qualified Indian securities-law review: " + "; ".join(r["id"] for r in legal_open),
                         [{"id": r["id"], "question": r["title"]} for r in legal_open]))

    critical_fail = [c for c in checks if not c["ok"] and c["severity"] == CRITICAL]
    review_open = [c for c in checks if not c["ok"] and c["severity"] == REVIEW]
    public_switch = bool(settings.get("compliance.public_publishing_enabled"))
    if critical_fail:
        status, allowed = "NEEDS_REVIEW", []
    elif review_open:
        status, allowed = "PRIVATE_REVIEW_ONLY", ["private"]
    elif not public_switch:
        status, allowed = "CLEARED_PRIVATE_UNTIL_ENABLED", ["private"]
    else:
        status, allowed = "CLEARED", ["private", "unlisted", "public", "scheduled"]
    if nc_voice and not critical_fail:
        status, allowed = "PRIVATE_REVIEW_ONLY", ["private"]
    override = owner_override()
    if override and not critical_fail and status != "CLEARED":
        status, allowed = "OWNER_OVERRIDE", ["private", "unlisted", "public", "scheduled"]
    return {"checked_at": db.iso(), "mode": mode, "status": status, "allowed_privacy": allowed,
            "owner_override": {k: override.get(k) for k in ("accepted_at", "accepted_by", "statement")}
            if override and status == "OWNER_OVERRIDE" else None,
            "critical_failures": [c["name"] for c in critical_fail], "review_items": [c["name"] for c in review_open],
            "public_switch": public_switch, "checks": checks,
            "note": "Automated guard rails only — not legal advice and not a determination of compliance."}


def permits(gate: dict[str, Any] | None, privacy: str) -> tuple[bool, str]:
    if not gate:
        return False, "compliance gate has not run"
    if privacy not in gate.get("allowed_privacy", []):
        if gate["status"] == "NEEDS_REVIEW":
            return False, "compliance gate failed: " + ", ".join(gate["critical_failures"])
        return False, (f"'{privacy}' is not allowed (gate status {gate['status']}); "
                       f"allowed: {', '.join(gate['allowed_privacy']) or 'none'}")
    return True, "ok"


__all__ = ["run", "permits", "CRITICAL", "REVIEW", "analysis"]
