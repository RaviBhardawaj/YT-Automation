"""Deterministic 15-minute / 1-hour analysis of one verified session against fixed reference levels.

Every statement comes from the bars: a level counts as "tested" only if a bar's range contains it,
"closed beyond" only if a bar closed past it, and a "failed breakout" only if a later close came
back to the side the session opened on. Nothing is inferred between bars, and gaps are reported,
not filled.

Multi-timeframe levels follow the Pivot Reversal project's definitions:
  15-Min levels = Fibonacci pivots of the last 25 completed 15-minute bars (one full session)
  1-Hour levels = Fibonacci pivots of the last 35 completed 1-hour bars (about five sessions)
"""
from __future__ import annotations

from typing import Any

from . import pivots

WATCH = ("R2", "R1", "P", "S1", "S2")
MTF_BARS = {"15m": 25, "1h": 35}


def hhmm(ts: str) -> str:
    return ts[11:16]


def level_behaviour(bars: list[dict[str, Any]], name: str, value: float) -> dict[str, Any]:
    """How the session's bars interacted with one level."""
    if not bars:
        return {"level": name, "value": value, "state": "no_data"}
    origin = "above" if bars[0]["open"] > value else "below"
    if bars[0]["open"] == value:
        origin = "above" if bars[0]["close"] >= value else "below"
    touches = [b for b in bars if b["low"] <= value <= b["high"]]
    beyond = (lambda b: b["close"] < value) if origin == "above" else (lambda b: b["close"] > value)
    first_beyond = next((i for i, b in enumerate(bars) if beyond(b)), None)
    back = None
    if first_beyond is not None:
        back = next((i for i in range(first_beyond + 1, len(bars)) if not beyond(bars[i])), None)
    session_close_beyond = beyond(bars[-1])
    out = {"level": name, "value": value, "origin": origin, "touch_count": len(touches),
           "first_touch": hhmm(touches[0]["ts"]) if touches else None,
           "first_close_beyond": hhmm(bars[first_beyond]["ts"]) if first_beyond is not None else None,
           "returned_at": hhmm(bars[back]["ts"]) if back is not None else None,
           "closes_beyond": sum(1 for b in bars if beyond(b)), "session_close_beyond": session_close_beyond}
    if not touches and first_beyond is None:
        out["state"] = "not_tested"
        edge = min(b["low"] for b in bars) if origin == "above" else max(b["high"] for b in bars)
        out["distance"] = round(abs(edge - value), 2)
    elif first_beyond is None:
        out["state"] = "tested_held"            # touched, but no bar closed through it
    elif session_close_beyond:
        out["state"] = "closed_beyond"
    elif back is not None:
        out["state"] = "failed_breakout"        # closed through, then closed back and finished on the origin side
    else:
        out["state"] = "closed_beyond"
    return out


def structure(bars: list[dict[str, Any]]) -> dict[str, Any]:
    """Sequence of highs and lows bar to bar (no swing-point guessing)."""
    if len(bars) < 3:
        return {"label": "insufficient", "bars": len(bars)}
    hh = sum(1 for a, b in zip(bars, bars[1:]) if b["high"] > a["high"])
    hl = sum(1 for a, b in zip(bars, bars[1:]) if b["low"] > a["low"])
    lh = sum(1 for a, b in zip(bars, bars[1:]) if b["high"] < a["high"])
    ll = sum(1 for a, b in zip(bars, bars[1:]) if b["low"] < a["low"])
    n = len(bars) - 1
    if hh + hl >= 1.4 * n and hh > lh and hl > ll:
        label = "rising"
    elif lh + ll >= 1.4 * n and lh > hh and ll > hl:
        label = "falling"
    else:
        label = "mixed"
    hi = max(bars, key=lambda b: b["high"])
    lo = min(bars, key=lambda b: b["low"])
    return {"label": label, "bars": len(bars), "higher_highs": hh, "higher_lows": hl, "lower_highs": lh,
            "lower_lows": ll, "high": hi["high"], "high_time": hhmm(hi["ts"]), "low": lo["low"],
            "low_time": hhmm(lo["ts"]), "open": bars[0]["open"], "close": bars[-1]["close"]}


def analyse(bars15: list[dict[str, Any]], bars1h: list[dict[str, Any]], levels: dict[str, float]) -> dict[str, Any]:
    out: dict[str, Any] = {"timeframes": []}
    if bars15:
        out["timeframes"].append("15m")
        out["m15"] = {"structure": structure(bars15),
                      "levels": {k: level_behaviour(bars15, k, levels[k]) for k in WATCH},
                      "opening_bar": {k: bars15[0][k] for k in ("open", "high", "low", "close")} | {"ts": hhmm(bars15[0]["ts"])}}
    if bars1h:
        out["timeframes"].append("1h")
        out["h1"] = {"structure": structure(bars1h), "levels": {k: level_behaviour(bars1h, k, levels[k]) for k in WATCH}}
    if bars15 and bars1h:
        agree, disagree = [], []
        for k in WATCH:
            a, b = out["m15"]["levels"][k], out["h1"]["levels"][k]
            if a["state"] == "not_tested":
                continue
            m15_through = a["first_close_beyond"] is not None
            h1_through = b["first_close_beyond"] is not None
            (agree if m15_through == h1_through else disagree).append(
                {"level": k, "m15_closed_beyond": m15_through, "h1_closed_beyond": h1_through})
        out["comparison"] = {"agree": agree, "disagree": disagree,
                             "structure_15m": out["m15"]["structure"]["label"],
                             "structure_1h": out["h1"]["structure"]["label"]}
    return out


def mtf_levels(bars: list[dict[str, Any]], timeframe: str) -> dict[str, Any] | None:
    """Pivot Reversal project's 15-Min / 1-Hour levels from the last N completed bars."""
    n = MTF_BARS[timeframe]
    if len(bars) < n:
        return None
    window = bars[-n:]
    h, lo, c = max(b["high"] for b in window), min(b["low"] for b in window), window[-1]["close"]
    calc = pivots.fibonacci_pivots(h, lo, c)
    return {"timeframe": timeframe, "bars": n, "from": window[0]["ts"], "to": window[-1]["ts"],
            "high": h, "low": lo, "close": c, "levels": calc["levels"]}
