"""Fibonacci pivot points — deterministic, Decimal-based, reproducible.

    P  = (H + L + C) / 3
    R1 = P + 0.382 × (H − L)      S1 = P − 0.382 × (H − L)
    R2 = P + 0.618 × (H − L)      S2 = P − 0.618 × (H − L)
    R3 = P + 1.000 × (H − L)      S3 = P − 1.000 × (H − L)

Inputs are the previous completed session's high, low and close. Results are rounded to
2 decimals (ROUND_HALF_UP) only at the end.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

RATIOS = {"1": Decimal("0.382"), "2": Decimal("0.618"), "3": Decimal("1.000")}
ORDER = ["R3", "R2", "R1", "P", "S1", "S2", "S3"]
FORMULA = ("P = (H + L + C) / 3; R1/S1 = P ± 0.382 × (H − L); R2/S2 = P ± 0.618 × (H − L); "
           "R3/S3 = P ± 1.000 × (H − L)")


def _d(x: float | str | Decimal) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _r(x: Decimal) -> float:
    return float(x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def fibonacci_pivots(high: float, low: float, close: float) -> dict[str, Any]:
    h, l, c = _d(high), _d(low), _d(close)
    if not (h >= c >= l and h >= l):
        raise ValueError(f"inconsistent session: high {high}, low {low}, close {close}")
    rng = h - l
    p = (h + l + c) / Decimal(3)
    levels = {"P": _r(p)}
    for n, ratio in RATIOS.items():
        levels[f"R{n}"] = _r(p + ratio * rng)
        levels[f"S{n}"] = _r(p - ratio * rng)
    return {
        "inputs": {"high": float(h), "low": float(l), "close": float(c)},
        "range": _r(rng),
        "levels": levels,
        "order": ORDER,
        "formula": FORMULA,
    }
