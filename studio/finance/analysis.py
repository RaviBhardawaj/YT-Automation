"""Turn verified bars into the episode's fact sheet.

Everything an episode may state numerically is produced here, in code, from the imported data.
The script writer receives this sheet and nothing else; the gate later checks that every
number on screen or in the narration comes from it.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .. import db, profile
from . import i18n, intraday, marketdata, pivots

MODES = {
    "historical_case_study": "Historical case study (data ≥ 30 days old; reviews what happened)",
    "daily_levels": "Daily levels from the previous session (needs legal review before any public use)",
    "daily_review": "4 PM daily: how today's levels worked + reference levels for the next session (private until legal review)",
}


def fmt(v: float | None) -> str:
    """Fact-sheet number format. None = not provided by the source (e.g. an unknown open) — never estimated."""
    return "n/a" if v is None else f"{v:,.2f}"


def build_facts(ds_id: int, basis_date: str, mode: str, language: str | None = None) -> dict[str, Any]:
    language = language or profile.language()
    L = i18n.code(language)
    t = lambda key, **kw: i18n.t(key, L, **kw)  # noqa: E731
    _date_label = lambda iso: i18n.date_label(iso, L)  # noqa: E731
    if mode not in MODES:
        raise marketdata.DataError(f"unknown mode {mode}")
    ds = marketdata.dataset(ds_id)
    if ds["status"] == "invalid":
        raise marketdata.DataError("dataset failed validation; fix the source file and re-import")
    if ds["timeframe"] != "1d":
        raise marketdata.DataError("pivot episodes need a daily (1d) dataset")
    bars = marketdata.daily_bars(ds_id)
    idx = next((i for i, b in enumerate(bars) if b["trade_date"] == basis_date), None)
    if idx is None:
        raise marketdata.DataError(f"{basis_date} is not in the dataset (no invented sessions)")
    basis = bars[idx]
    calc = pivots.fibonacci_pivots(basis["high"], basis["low"], basis["close"])
    lv = calc["levels"]
    facts: dict[str, Any] = {
        "mode": mode, "language": language, "lang": L, "symbol": marketdata.SYMBOL, "exchange": marketdata.EXCHANGE,
        "dataset_id": ds_id, "dataset_sha256": ds["sha256"], "source_id": ds["source_id"],
        "data_nature": ds["data_nature"], "retrieved_at": ds["retrieved_at"], "timezone": ds["timezone"],
        "basis_date": basis_date, "basis_label": _date_label(basis_date),
        "basis": {k: basis[k] for k in ("open", "high", "low", "close")},
        "pivots": calc, "observations": [], "limitations": [], "numbers": {},
    }
    obs = facts["observations"]
    lim = facts["limitations"]

    def number(key: str, value: float) -> str:
        facts["numbers"][key] = value
        return fmt(value)

    for k in ("open", "high", "low", "close"):
        if basis[k] is not None:
            number(f"basis_{k}", basis[k])
    for k, v in lv.items():
        number(f"level_{k}", v)
    number("range", calc["range"])

    # previous session context
    if idx > 0 and marketdata.next_session_gap_ok(bars[idx - 1]["trade_date"], basis_date):
        prev = bars[idx - 1]
        chg = round(basis["close"] - prev["close"], 2)
        pct = round(chg / prev["close"] * 100, 2)
        number("prev_close", prev["close"])
        number("change_points", abs(chg))
        facts["numbers"]["change_pct"] = abs(pct)
        obs.append({"id": "change", "text": t("change", date=facts["basis_label"], close=fmt(basis["close"]),
                                              dir=t("up" if chg >= 0 else "down"), pts=fmt(abs(chg)),
                                              pct=f"{abs(pct):.2f}", prev=fmt(prev["close"]))})
    else:
        lim.append(t("lim_prev"))

    # where the close sat within the day's range
    if calc["range"] > 0:
        loc = (basis["close"] - basis["low"]) / calc["range"]
        third = t("third_upper" if loc >= 2 / 3 else "third_lower" if loc < 1 / 3 else "third_middle")
        facts["numbers"]["close_location_pct"] = round(loc * 100)
        obs.append({"id": "close_location", "text": t("close_location", third=third, range=fmt(calc["range"]),
                                                      pct=round(loc * 100))})

    # range versus the recent average (only if 20 contiguous sessions exist)
    if marketdata.recent_sessions_ok(bars, idx, 20):
        avg = round(sum(b["high"] - b["low"] for b in bars[idx - 19: idx + 1]) / 20, 2)
        ratio = round(calc["range"] / avg, 2) if avg else None
        number("avg_range_20", avg)
        facts["numbers"]["range_ratio"] = ratio
        size = t("wider" if ratio and ratio > 1.15 else "narrower" if ratio and ratio < 0.85 else "similar")
        obs.append({"id": "range_vs_avg", "text": t("range_vs_avg", size=size, avg=fmt(avg), ratio=f"{ratio:.2f}")})
    else:
        lim.append(t("lim_avg"))

    obs.append({"id": "level_spacing", "text": t("level_spacing", range=fmt(calc["range"]), s2=fmt(lv["S2"]),
                                                 r2=fmt(lv["R2"]), w=fmt(round(lv["R2"] - lv["S2"], 2)), p=fmt(lv["P"]))})
    number("s2_r2_width", round(lv["R2"] - lv["S2"], 2))

    # what happened next (historical review)
    review_ok = idx + 1 < len(bars) and marketdata.next_session_gap_ok(basis_date, bars[idx + 1]["trade_date"])
    if review_ok:
        nxt = bars[idx + 1]
        facts["review_date"] = nxt["trade_date"]
        facts["review_label"] = _date_label(nxt["trade_date"])
        facts["review"] = {k: nxt[k] for k in ("open", "high", "low", "close")}
        for k in ("open", "high", "low", "close"):
            if nxt[k] is not None:
                number(f"review_{k}", nxt[k])
        gap = round(nxt["open"] - basis["close"], 2) if nxt["open"] is not None else None
        if gap is not None:
            number("gap_points", abs(gap))
        touched = [k for k in pivots.ORDER if nxt["low"] <= lv[k] <= nxt["high"]]
        facts["touched"] = touched
        s2, r2 = lv["S2"], lv["R2"]

        def level_story(name: str, val: float, below: bool) -> str:
            if name in touched:
                held = (nxt["close"] > val) if below else (nxt["close"] < val)
                return t("reached", name=name, val=fmt(val), rdate=facts["review_label"],
                         side=t("above" if nxt["close"] > val else "below"), close=fmt(nxt["close"]),
                         held=t("held") if held else "")
            dist = round((nxt["low"] - val) if below else (val - nxt["high"]), 2)
            number(f"{name.lower()}_distance", abs(dist))
            return t("not_tested", name=name, val=fmt(val), rdate=facts["review_label"],
                     edge=t("low" if below else "high"), dist=fmt(abs(dist)))

        if gap is None:                       # source gives no open: no gap statement, and say so
            lim.append(t("lim_no_open", rdate=facts["review_label"]))
        else:
            obs.append({"id": "gap", "text": t("gap_flat", rdate=facts["review_label"], open=fmt(nxt["open"])) if gap == 0
                        else t("gap", rdate=facts["review_label"], open=fmt(nxt["open"]), pts=fmt(abs(gap)),
                               kind=t("gap_up" if gap > 0 else "gap_down"))})
        obs.append({"id": "s2_review", "text": level_story("S2", s2, True)})
        obs.append({"id": "r2_review", "text": level_story("R2", r2, False)})
        obs.append({"id": "levels_touched", "text": t("touched", rdate=facts["review_label"], low=fmt(nxt["low"]),
                                                      high=fmt(nxt["high"]), levels=", ".join(touched)) if touched else
                    t("touched_none", rdate=facts["review_label"], low=fmt(nxt["low"]), high=fmt(nxt["high"]))})
        lim.append(t("lim_order"))
        _intraday_review(facts, nxt["trade_date"], lv, t, number)
    elif mode in ("historical_case_study", "daily_review"):
        raise marketdata.DataError(f"{mode.replace('_', ' ')} needs the session after {basis_date} in the dataset "
                                   "(no invented sessions)")
    else:
        lim.append(t("lim_future"))

    if mode == "daily_review":
        _next_session(facts, t, number)
    # 15-Min / 1-Hour levels as of the latest close in the episode (today's close for the daily review)
    _mtf(facts, facts["review_date"] if mode == "daily_review" else basis_date, t, number)
    if ds["data_nature"] == "intraday" or facts.get("intraday"):
        lim.append(t("lim_bars"))
    if mode == "historical_case_study" and not marketdata.education_lag_ok(facts.get("review_date", basis_date)):
        raise marketdata.DataError(
            f"historical case study needs data at least {marketdata.EDUCATION_LAG_DAYS} days old "
            "(SEBI circular dated 8 May 2026); choose an earlier basis date")
    if ds["data_nature"] != "intraday":
        lim.append(t("lim_nature", nature=i18n.nature(ds["data_nature"], L)))
    retrieved = db.parse_iso(ds["retrieved_at"]).astimezone(marketdata.IST)
    facts["retrieved_label"] = retrieved.strftime("%d %b %Y, %H:%M IST")
    facts["date_numbers"] = sorted(
        set(facts.pop("_time_numbers", set())) |
        {date.fromisoformat(d).day for d in _dates(facts, basis_date)} |
        {date.fromisoformat(d).year for d in _dates(facts, basis_date)} |
        {retrieved.day, retrieved.year, retrieved.hour, retrieved.minute})
    return facts


def _dates(facts: dict[str, Any], basis_date: str) -> list[str]:
    """Every session date the episode may name: basis, reviewed session and the next session."""
    return [basis_date, facts.get("review_date", basis_date), (facts.get("next") or {}).get("date", basis_date)]


def _times(facts: dict[str, Any], *hhmm: str | None) -> None:
    nums = facts.setdefault("_time_numbers", set())
    for x in hhmm:
        if x:
            h, m = x.split(":")
            nums |= {int(h), int(m)}


def _intraday_review(facts: dict[str, Any], rdate: str, lv: dict[str, float], t, number) -> None:
    """15-minute / 1-hour behaviour of the review session around the daily levels."""
    obs, lim = facts["observations"], facts["limitations"]
    got: dict[str, list[dict[str, Any]]] = {}
    sources: dict[str, int] = {}
    for tf in ("15m", "1h"):
        bars, ds = marketdata.intraday_bars(rdate, tf)
        if not bars:
            continue
        if not marketdata.session_complete(bars, tf):
            lim.append(t("lim_incomplete", tf=tf, date=facts["review_label"], n=len(bars), exp=marketdata.SESSION_BARS[tf]))
            continue
        got[tf], sources[tf] = bars, ds["id"]
    if not got:
        lim.append(t("lim_intraday"))
        return
    res = intraday.analyse(got.get("15m", []), got.get("1h", []), lv)
    facts["intraday"] = {"timeframes": res["timeframes"], "dataset_ids": sources, "analysis": res,
                         "bars": {tf: [{k: b[k] for k in ("ts", "open", "high", "low", "close")} for b in bars]
                                  for tf, bars in got.items()}}
    rl = facts["review_label"]
    if "m15" in res:
        st = res["m15"]["structure"]
        number("m15_high", st["high"])
        number("m15_low", st["low"])
        _times(facts, st["high_time"], st["low_time"])
        obs.append({"id": "structure_15m", "text": t("struct_15", rdate=rl, label=t(f"s_{st['label']}"),
                                                     high=fmt(st["high"]), ht=st["high_time"], low=fmt(st["low"]),
                                                     lt=st["low_time"])})
        for name in ("S2", "R2", "P"):
            b = res["m15"]["levels"][name]
            _times(facts, b.get("first_touch"), b.get("first_close_beyond"), b.get("returned_at"))
            dirw = t("below" if b["origin"] == "above" else "above")
            if b["state"] == "not_tested":
                number(f"{name.lower()}_15m_distance", b["distance"])
                text = t("lv_not_tested_15", rdate=rl, name=name, val=fmt(b["value"]), dist=fmt(b["distance"]))
            elif b["state"] == "tested_held":
                facts["numbers"][f"{name.lower()}_touches"] = b["touch_count"]
                text = t("lv_tested_held", rdate=rl, name=name, val=fmt(b["value"]), t=b["first_touch"],
                         n=b["touch_count"], dir=dirw)
            elif b["state"] == "failed_breakout":
                text = t("lv_failed", rdate=rl, name=name, val=fmt(b["value"]), dir=dirw, tb=b["first_close_beyond"],
                         tr=b["returned_at"])
            else:
                text = t("lv_closed_beyond", rdate=rl, name=name, val=fmt(b["value"]), t=b["first_touch"] or "—",
                         dir=dirw, tb=b["first_close_beyond"])
            obs.append({"id": f"{name.lower()}_15m", "text": text, "state": b["state"]})
    if "h1" in res:
        st = res["h1"]["structure"]
        facts["numbers"]["h1_bars"] = st["bars"]
        facts["numbers"]["h1_hh"] = st.get("higher_highs", 0)
        facts["numbers"]["h1_ll"] = st.get("lower_lows", 0)
        facts["numbers"]["h1_steps"] = st["bars"] - 1
        obs.append({"id": "structure_1h", "text": t("struct_1h", rdate=rl, n=st["bars"], steps=st["bars"] - 1,
                                                    label=t(f"s_{st['label']}"),
                                                    hh=st.get("higher_highs", 0), ll=st.get("lower_lows", 0))})
    if "comparison" in res:
        cmp_ = res["comparison"]
        if cmp_["disagree"]:
            obs.append({"id": "tf_compare", "text": t("cmp_disagree", levels=", ".join(x["level"] for x in cmp_["disagree"]))})
        elif cmp_["agree"]:
            obs.append({"id": "tf_compare", "text": t("cmp_agree", levels=", ".join(x["level"] for x in cmp_["agree"]))})
    else:
        lim.append(t("lim_one_tf", tf=res["timeframes"][0]))


def _next_session(facts: dict[str, Any], t, number) -> None:
    """Reference levels for the session after the reviewed one, from that session's verified high, low and close."""
    from . import calendar
    r = facts["review"]
    nd = calendar.next_session(date.fromisoformat(facts["review_date"])).isoformat()
    calc = pivots.fibonacci_pivots(r["high"], r["low"], r["close"])
    lv = calc["levels"]
    L = facts["lang"]
    facts["next"] = {"date": nd, "label": i18n.date_label(nd, L), "levels": lv, "range": calc["range"],
                     "basis": dict(r), "formula": calc["formula"]}
    for k, v in lv.items():
        number(f"next_{k}", v)
    number("next_range", calc["range"])
    number("next_s2_r2_width", round(lv["R2"] - lv["S2"], 2))
    facts["observations"].append({"id": "next_levels", "text": t(
        "next_levels", rdate=facts["review_label"], ndate=facts["next"]["label"], s2=fmt(lv["S2"]), s1=fmt(lv["S1"]),
        p=fmt(lv["P"]), r1=fmt(lv["R1"]), r2=fmt(lv["R2"]), rng=fmt(calc["range"]))})
    side = t("above" if r["close"] > lv["P"] else "below")
    number("next_close_vs_p", abs(round(r["close"] - lv["P"], 2)))
    facts["observations"].append({"id": "next_position", "text": t(
        "next_position", rdate=facts["review_label"], close=fmt(r["close"]), side=side, p=fmt(lv["P"]),
        d=fmt(abs(round(r["close"] - lv["P"], 2))), ndate=facts["next"]["label"])})
    facts["limitations"].append(t("lim_next", ndate=facts["next"]["label"]))


def _mtf(facts: dict[str, Any], basis_date: str, t, number) -> None:
    """Pivot Reversal project's 15-Min / 1-Hour levels, computed at the given session's close."""
    out = {}
    from . import pivot_project
    recorded = pivot_project.mtf_hlc(marketdata.dataset(facts["dataset_id"]), basis_date)
    for tf in ("15m", "1h"):
        bars, ds = marketdata.intraday_window(basis_date, tf, intraday.MTF_BARS[tf])
        if bars and bars[-1]["trade_date"] == basis_date:
            m = intraday.mtf_levels(bars, tf)
            if not m:
                continue
            m["dataset_id"] = ds["id"]
        elif recorded and tf in recorded:          # high / low / close of the same N bars, from the project's file
            h = recorded[tf]
            m = {"timeframe": tf, "bars": intraday.MTF_BARS[tf], "from": None, "to": None, "high": h["high"],
                 "low": h["low"], "close": h["close"], "dataset_id": facts["dataset_id"],
                 "levels": pivots.fibonacci_pivots(h["high"], h["low"], h["close"])["levels"]}
        else:
            continue
        out[tf] = m
        for k, v in m["levels"].items():
            number(f"mtf_{tf}_{k}", v)
        facts["numbers"][f"mtf_{tf}_bars"] = m["bars"]
        lv = m["levels"]
        ref = (facts.get("next") or {}).get("levels") if basis_date == facts.get("review_date") else facts["pivots"]["levels"]
        same = tf == "15m" and ref and all(abs(lv[k] - ref[k]) < 0.005 for k in lv)
        facts["observations"].append({"id": f"mtf_{tf}", "text": t("mtf", n=m["bars"], tf=tf,
                                                                   basis=i18n.date_label(basis_date, facts["lang"]),
                                                                   s2=fmt(lv["S2"]), p=fmt(lv["P"]), r2=fmt(lv["R2"]))
                                      + (t("mtf_same") if same else "")})
    if out:
        facts["mtf"] = out
