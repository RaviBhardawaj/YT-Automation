"""NSE capital-market trading calendar (weekends, notified holidays, special sessions).

Holidays are typed in from NSE's annual circular, not fetched. A year with no table is
"unverified": session lookups then fall back to weekdays only and say so, so nothing
downstream silently assumes a holiday-free year.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

SOURCES = {
    2026: [
        {"ref": "NSE/CMTR/71775 (12 Dec 2025) — trading holidays for calendar year 2026",
         "url": "https://www.nseindia.com/resources/exchange-communication-holidays", "checked": "2026-10-23"},
        {"ref": "NSE/CMTR/72260 (12 Jan 2026) — 15 Jan 2026 holiday (Maharashtra municipal elections)",
         "url": "https://nsearchives.nseindia.com/content/circulars/CMTR72260.pdf", "checked": "2026-10-23"},
    ],
}

HOLIDAYS: dict[date, str] = {
    date(2026, 1, 15): "Municipal Corporation elections (Maharashtra)",
    date(2026, 1, 26): "Republic Day",
    date(2026, 3, 3): "Holi",
    date(2026, 3, 26): "Shri Ram Navami",
    date(2026, 3, 31): "Shri Mahavir Jayanti",
    date(2026, 4, 3): "Good Friday",
    date(2026, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
    date(2026, 5, 1): "Maharashtra Day",
    date(2026, 5, 28): "Bakri Id",
    date(2026, 6, 26): "Muharram",
    date(2026, 9, 14): "Ganesh Chaturthi",
    date(2026, 10, 2): "Mahatma Gandhi Jayanti",
    date(2026, 10, 20): "Dussehra",
    date(2026, 11, 10): "Diwali — Balipratipada",
    date(2026, 11, 24): "Prakash Gurpurb Sri Guru Nanak Dev",
    date(2026, 12, 25): "Christmas",
}

# Weekend/evening special sessions. They are real sessions but too short for a normal pivot episode.
SPECIAL_SESSIONS: dict[date, str] = {
    date(2026, 11, 8): "Muhurat trading (Diwali — Laxmi Pujan; timing per NSE circular)",
}


def year_verified(y: int) -> bool:
    return y in SOURCES


def is_holiday(d: date) -> bool:
    return d in HOLIDAYS


def is_trading_day(d: date) -> bool:
    """Regular full session. Special (Muhurat) sessions are excluded on purpose."""
    return d.weekday() < 5 and d not in HOLIDAYS


def why_closed(d: date) -> str | None:
    if d in SPECIAL_SESSIONS:
        return f"special session only: {SPECIAL_SESSIONS[d]}"
    if d.weekday() >= 5:
        return "weekend"
    return HOLIDAYS.get(d)


def previous_session(d: date) -> date:
    """Last regular session strictly before d."""
    x = d - timedelta(days=1)
    for _ in range(15):
        if is_trading_day(x):
            return x
        x -= timedelta(days=1)
    raise ValueError(f"no trading session found before {d}")


def next_session(d: date) -> date:
    """First regular session strictly after d."""
    x = d + timedelta(days=1)
    for _ in range(15):
        if is_trading_day(x):
            return x
        x += timedelta(days=1)
    raise ValueError(f"no trading session found after {d}")


def consecutive(a: str, b: str) -> bool:
    """True when session b is the regular session right after a (no missing session in between)."""
    da, db_ = date.fromisoformat(a), date.fromisoformat(b)
    if year_verified(da.year) and year_verified(db_.year):
        return next_session(da) == db_
    return 0 < (db_ - da).days <= 5           # unverified year: weekday heuristic only


def session_status(today: date, after_close: bool) -> dict[str, Any]:
    """What a daily episode made 'now' would be about."""
    basis = today if (is_trading_day(today) and after_close) else previous_session(today)
    return {"basis_session": basis.isoformat(), "next_session": next_session(basis).isoformat(),
            "today_is_trading_day": is_trading_day(today), "today_closed_reason": why_closed(today),
            "calendar_verified": year_verified(today.year),
            "sources": SOURCES.get(today.year, [])}
