"""Market data intake with provenance and validation.

No source is fetched automatically: NSE and NSE Indices prohibit automated collection and
republication without permission (see rules DATA-NSE-TERMS / DATA-NSE-INDICES-LICENCE). Data
enters through a file you obtained yourself (manual import) or from a licensed vendor, and
every dataset records where it came from, when, what it is (end-of-day vs intraday) and the
SHA-256 of an immutable stored copy. Missing values are never filled in.
"""
from __future__ import annotations

import csv
import io
import re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .. import config, db
from . import audit, calendar

IST = ZoneInfo("Asia/Kolkata")
SYMBOL = "NIFTY 50"
EXCHANGE = "NSE"
SESSION_CLOSE = (15, 30)
EDUCATION_LAG_DAYS = 30          # SEBI circular HO/47/17/12(11)2025-MRD-POD3/I/11107/2026

SOURCES_SEED = [
    {
        "id": "manual_niftyindices", "name": "Manual download from niftyindices.com (NSE Indices Ltd) — official",
        "kind": "manual_import", "terms_url": "https://www.niftyindices.com/terms-of-use",
        "terms_summary": "Personal access to the site is permitted; automated collection and republication require "
                         "written consent; distribution of index data requires a licence from NSE Indices.",
        "automated_access_allowed": 0, "permits_public_video": None, "permits_monetization": None,
        "status": "REVIEW_REQUIRED",
        "notes": "Use for verification and private review. Record an NSE Indices licence/permission before public use.",
    },
    {
        "id": "manual_nseindia", "name": "Manual download from nseindia.com (NSE) — official",
        "kind": "manual_import", "terms_url": "https://www.nseindia.com/static/nse-terms-of-use",
        "terms_summary": "Content and data may not be reproduced, broadcast or published without NSE's prior written "
                         "permission; automated collection prohibited.",
        "automated_access_allowed": 0, "permits_public_video": 0, "permits_monetization": 0,
        "status": "REVIEW_REQUIRED",
        "notes": "Publication needs NSE's written permission or a licensed source.",
    },
    {
        "id": "licensed_vendor", "name": "Licensed data vendor (record licence details)",
        "kind": "licensed_vendor", "terms_url": "",
        "terms_summary": "Fill in after reviewing the vendor agreement: must explicitly cover public display in "
                         "videos and monetized redistribution of NIFTY 50 index values.",
        "automated_access_allowed": 0, "permits_public_video": None, "permits_monetization": None,
        "status": "REVIEW_REQUIRED", "notes": "",
    },
    {
        "id": "test_fixture", "name": "Synthetic test fixture (NOT market data)", "kind": "test_fixture",
        "terms_url": "", "terms_summary": "Generated numbers used only to test the software.",
        "automated_access_allowed": 1, "permits_public_video": 0, "permits_monetization": 0,
        "status": "PROHIBITED", "notes": "Never usable for publication.",
    },
]


class DataError(Exception):
    pass


def seed_sources() -> None:
    from .pivot_project import SOURCE_SEED as project_source
    for s in SOURCES_SEED + [project_source]:
        if not db.q1("SELECT id FROM data_sources WHERE id=?", (s["id"],)):
            db.insert("data_sources", {"terms_checked": "2026-10-23T00:00:00Z", "licence_reference": None,
                                       "licence_holder": None, "licence_expiry": None, "licence_evidence": None,
                                       "updated_at": db.iso(), **s})


def source(source_id: str) -> dict[str, Any]:
    s = db.q1("SELECT * FROM data_sources WHERE id=?", (source_id,))
    if not s:
        raise DataError(f"unknown data source {source_id}")
    return s


def source_permits_publication(s: dict[str, Any]) -> tuple[bool, str]:
    if s["status"] == "PROHIBITED":
        return False, "source is prohibited for publication"
    if s["status"] != "PERMITTED":
        return False, "licence/permission for public, monetized video use not recorded"
    if not (s["permits_public_video"] and s["permits_monetization"]):
        return False, "recorded terms do not cover public video and monetization"
    if s["licence_expiry"] and s["licence_expiry"] < db.iso()[:10]:
        return False, f"licence expired on {s['licence_expiry']}"
    return True, f"licence {s['licence_reference']} held by {s['licence_holder']}"


def record_licence(source_id: str, *, reference: str, holder: str, expiry: str, permits_public_video: bool,
                   permits_monetization: bool, evidence: str, actor: str) -> None:
    if not reference.strip() or not holder.strip() or not evidence.strip():
        raise DataError("licence reference, holder and evidence are required")
    status = "PERMITTED" if permits_public_video and permits_monetization else "REVIEW_REQUIRED"
    db.ex("UPDATE data_sources SET licence_reference=?, licence_holder=?, licence_expiry=?, permits_public_video=?, "
          "permits_monetization=?, licence_evidence=?, status=?, updated_at=? WHERE id=?",
          (reference.strip(), holder.strip(), expiry.strip() or None, int(permits_public_video),
           int(permits_monetization), evidence.strip(), status, db.iso(), source_id))
    audit.record("data_licence_recorded", {"source_id": source_id, "reference": reference, "holder": holder,
                                           "expiry": expiry, "public_video": permits_public_video,
                                           "monetization": permits_monetization, "evidence": evidence}, actor=actor)


# ------------------------------------------------------------------ parsing

_DATE_FORMATS = ("%d %b %Y", "%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d %B %Y", "%b %d, %Y")
_TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d-%b-%Y %H:%M", "%d-%m-%Y %H:%M", "%Y-%m-%dT%H:%M:%S",
               "%Y-%m-%dT%H:%M")


def _parse_date(s: str) -> date:
    s = s.strip().strip('"')
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(s.title() if "%b" in f or "%B" in f else s, f).date()
        except ValueError:
            continue
    raise DataError(f"unrecognised date {s!r}")


def _parse_ts(s: str) -> datetime:
    s = s.strip().strip('"').replace("+05:30", "")
    for f in _TS_FORMATS:
        try:
            return datetime.strptime(s, f).replace(tzinfo=IST)
        except ValueError:
            continue
    raise DataError(f"unrecognised timestamp {s!r}")


def _num(s: str) -> float:
    s = (s or "").strip().replace(",", "").replace("₹", "")
    if s in ("", "-", "NA", "N/A", "null"):
        raise DataError("missing numeric value")
    return float(s)


def parse_csv(text: str, timeframe: str) -> list[dict[str, Any]]:
    """Accepts the official NSE / NSE Indices historical CSV layouts and simple Date,Open,High,Low,Close files."""
    text = text.lstrip("﻿")
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        raise DataError("file is empty")
    header = [re.sub(r"[^a-z]", "", h.lower()) for h in rows[0]]

    def col(*names: str) -> int:
        for n in names:
            if n in header:
                return header.index(n)
        raise DataError(f"column {names[0]!r} not found in header {rows[0]}")

    i_date = col("date", "datetime", "timestamp", "time", "tradedate", "indexdate")
    i_o, i_h, i_l, i_c = col("open", "openindexvalue"), col("high", "highindexvalue"), col("low", "lowindexvalue"), \
        col("close", "closingindexvalue", "closeindexvalue", "ltp")
    i_v = next((header.index(n) for n in ("sharestraded", "volume") if n in header), None)
    i_name = next((header.index(n) for n in ("indexname", "symbol") if n in header), None)
    bars = []
    for line_no, r in enumerate(rows[1:], start=2):
        try:
            if i_name is not None and r[i_name].strip() and r[i_name].strip().upper() not in ("NIFTY 50", "NIFTY50", "NIFTY"):
                raise DataError(f"row is for {r[i_name]!r}, not NIFTY 50")
            if timeframe == "1d":
                d = _parse_date(r[i_date])
                ts = None
            else:
                t = _parse_ts(r[i_date])
                d, ts = t.date(), t.strftime("%Y-%m-%dT%H:%M:%S+05:30")
            bars.append({"trade_date": d.isoformat(), "ts": ts, "open": _num(r[i_o]), "high": _num(r[i_h]),
                         "low": _num(r[i_l]), "close": _num(r[i_c]),
                         "volume": _num(r[i_v]) if i_v is not None and r[i_v].strip() not in ("", "-") else None,
                         "line": line_no})
        except (DataError, IndexError, ValueError) as e:
            raise DataError(f"line {line_no}: {e}") from e
    return bars


# ------------------------------------------------------------------ validation

def validate(bars: list[dict[str, Any]], timeframe: str, *, retrieved_at: datetime) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    key = (lambda b: b["trade_date"]) if timeframe == "1d" else (lambda b: b["ts"])
    seen: dict[str, int] = {}
    for b in bars:
        k = key(b)
        if k in seen:
            if (b["open"], b["high"], b["low"], b["close"]) != seen[k]:
                errors.append(f"conflicting duplicate rows for {k}")
            else:
                warnings.append(f"exact duplicate row for {k} (ignored)")
        seen[k] = (b["open"], b["high"], b["low"], b["close"])
        o, h, l, c = b["open"], b["high"], b["low"], b["close"]
        known = [x for x in (o, h, l, c) if x is not None]          # open may be unknown (NULL), never estimated
        if min(known) <= 0:
            errors.append(f"{k}: non-positive price")
        if h < max(x for x in (o, c, l) if x is not None) or l > min(x for x in (o, c, h) if x is not None):
            errors.append(f"{k}: inconsistent OHLC (high {h}, low {l}, open {o}, close {c})")
        if h > 0 and (h - l) / h > 0.2:
            warnings.append(f"{k}: unusually wide range ({(h - l) / h:.1%}) — verify against the source")
        d = date.fromisoformat(b["trade_date"])
        if d in calendar.SPECIAL_SESSIONS:
            warnings.append(f"{k}: {calendar.SPECIAL_SESSIONS[d]} — special session, excluded from regular analysis")
        elif d.weekday() >= 5:
            errors.append(f"{k}: weekend date with no notified special session")
        elif calendar.is_holiday(d):
            errors.append(f"{k}: NSE trading holiday ({calendar.HOLIDAYS[d]}) — the file has a bar for a closed day")
        if d > retrieved_at.astimezone(IST).date():
            errors.append(f"{k}: dated after the retrieval time")
        if timeframe == "1d" and d == retrieved_at.astimezone(IST).date() and \
                (retrieved_at.astimezone(IST).hour, retrieved_at.astimezone(IST).minute) < SESSION_CLOSE:
            errors.append(f"{k}: session was still open when the data was retrieved (incomplete candle)")
    uniq = sorted({key(b): b for b in bars}.values(), key=key)
    if timeframe == "1d":
        regular = [b for b in uniq if calendar.is_trading_day(date.fromisoformat(b["trade_date"]))]
        for a, b in zip(regular, regular[1:]):
            if not calendar.consecutive(a["trade_date"], b["trade_date"]):
                warnings.append(f"missing session(s) between {a['trade_date']} and {b['trade_date']} — NOT filled; "
                                "observations spanning this gap are disabled")
    else:
        by_day: dict[str, int] = {}
        for b in uniq:
            by_day[b["trade_date"]] = by_day.get(b["trade_date"], 0) + 1
        expected = 25 if timeframe == "15m" else 7          # 09:15-15:30 session
        for d_, n in sorted(by_day.items()):
            if n < expected and date.fromisoformat(d_) not in calendar.SPECIAL_SESSIONS:
                warnings.append(f"{d_}: {n} of {expected} {timeframe} bars — incomplete session; gaps are not filled")
    return {"errors": errors, "warnings": warnings, "rows": len(uniq),
            "first": key(uniq[0]) if uniq else None, "last": key(uniq[-1]) if uniq else None}


def import_file(path: Path | None, *, text: str | None = None, original_name: str, source_id: str, timeframe: str,
                data_nature: str, retrieved_at_local: str, retrieved_by: str, notes: str = "") -> dict[str, Any]:
    if timeframe not in ("1d", "1h", "15m"):
        raise DataError("timeframe must be 1d, 1h or 15m")
    if data_nature not in ("end_of_day", "delayed", "intraday"):
        raise DataError("data nature must be end_of_day, delayed or intraday")
    if timeframe == "1d" and data_nature == "intraday":
        raise DataError("daily bars cannot be labelled intraday")
    src = source(source_id)
    try:
        retrieved_at = datetime.fromisoformat(retrieved_at_local)
    except ValueError as e:
        raise DataError("retrieval time must be an ISO date-time, e.g. 2026-10-23T18:05") from e
    if retrieved_at.tzinfo is None:
        retrieved_at = retrieved_at.replace(tzinfo=IST)
    raw = text if text is not None else Path(path).read_text(encoding="utf-8-sig", errors="strict")
    bars = parse_csv(raw, timeframe)
    report = validate(bars, timeframe, retrieved_at=retrieved_at)
    status = "invalid" if report["errors"] else ("valid_with_warnings" if report["warnings"] else "valid")
    ds_id = db.insert("market_datasets", {
        "source_id": src["id"], "symbol": SYMBOL, "exchange": EXCHANGE, "timeframe": timeframe, "timezone": "Asia/Kolkata",
        "data_nature": data_nature, "retrieved_at": db.iso(retrieved_at), "retrieved_by": retrieved_by,
        "retrieval_notes": notes, "original_filename": original_name, "first_date": report["first"],
        "last_date": report["last"], "rows": report["rows"], "validation": db.dumps(report), "status": status,
        "created_at": db.iso()})
    stored = config.MARKET_DIR / f"dataset_{ds_id:05d}_{re.sub(r'[^A-Za-z0-9._-]', '_', original_name)[:60]}"
    if path is not None:
        shutil.copyfile(path, stored)
    else:
        stored.write_text(raw, encoding="utf-8")
    stored.chmod(0o444)
    sha = audit.file_sha256(stored)
    db.update("market_datasets", ds_id, {"stored_path": str(stored), "sha256": sha})
    if status != "invalid":
        uniq = {(b["trade_date"], b["ts"]): b for b in bars}
        with db.tx() as c:
            for b in sorted(uniq.values(), key=lambda b: (b["trade_date"], b["ts"] or "")):
                c.execute("INSERT INTO market_bars(dataset_id,trade_date,ts,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?,?)",
                          (ds_id, b["trade_date"], b["ts"], b["open"], b["high"], b["low"], b["close"], b["volume"]))
    audit.record("dataset_imported", {"dataset_id": ds_id, "source_id": source_id, "timeframe": timeframe,
                                      "data_nature": data_nature, "retrieved_at": db.iso(retrieved_at),
                                      "retrieved_by": retrieved_by, "rows": report["rows"], "status": status,
                                      "errors": report["errors"][:20], "warnings": report["warnings"][:20]},
                 actor=retrieved_by or "user", artifact=stored)
    return {"dataset_id": ds_id, "status": status, **report}


def dataset(ds_id: int) -> dict[str, Any]:
    d = db.q1("SELECT * FROM market_datasets WHERE id=?", (ds_id,))
    if not d:
        raise DataError(f"dataset {ds_id} not found")
    d["validation"] = db.loads(d["validation"], {})
    return d


def daily_bars(ds_id: int) -> list[dict[str, Any]]:
    return db.q("SELECT trade_date, open, high, low, close, volume FROM market_bars WHERE dataset_id=? ORDER BY trade_date",
                (ds_id,))


def intraday_bars(trade_date: str, timeframe: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Intraday bars for one session from the most recent valid dataset, with that dataset's record."""
    ds = db.q1("SELECT * FROM market_datasets WHERE timeframe=? AND status!='invalid' AND first_date<=? AND last_date>=? "
               "ORDER BY id DESC LIMIT 1", (timeframe, trade_date + "T99", trade_date))
    if not ds:
        return [], None
    bars = db.q("SELECT ts, open, high, low, close FROM market_bars WHERE dataset_id=? AND trade_date=? ORDER BY ts",
                (ds["id"], trade_date))
    return bars, ds


def intraday_window(end_date: str, timeframe: str, n: int) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """The last n bars up to and including end_date's session, from the newest valid dataset covering end_date."""
    _, ds = intraday_bars(end_date, timeframe)
    if not ds:
        return [], None
    bars = db.q("SELECT ts, trade_date, open, high, low, close FROM market_bars WHERE dataset_id=? AND trade_date<=? "
                "ORDER BY ts DESC LIMIT ?", (ds["id"], end_date, n))
    return list(reversed(bars)), ds


SESSION_BARS = {"15m": 25, "1h": 7}


def session_complete(bars: list[dict[str, Any]], timeframe: str) -> bool:
    return len(bars) >= SESSION_BARS[timeframe]


def education_lag_ok(trade_date: str, today: date | None = None) -> bool:
    today = today or datetime.now(IST).date()
    return (today - date.fromisoformat(trade_date)).days >= EDUCATION_LAG_DAYS


def all_datasets() -> list[dict[str, Any]]:
    rows = db.q("SELECT d.*, s.name AS source_name, s.status AS source_status FROM market_datasets d "
                "JOIN data_sources s ON s.id=d.source_id ORDER BY d.id DESC")
    for r in rows:
        r["validation"] = db.loads(r["validation"], {})
    return rows


def recent_sessions_ok(bars: list[dict[str, Any]], idx: int, n: int) -> bool:
    """True if the n sessions ending at idx have no >5-calendar-day gaps (so averages are meaningful)."""
    if idx - n + 1 < 0:
        return False
    window = bars[idx - n + 1: idx + 1]
    return all(calendar.consecutive(a["trade_date"], b["trade_date"]) for a, b in zip(window, window[1:]))


def next_session_gap_ok(a: str, b: str) -> bool:
    """b is the very next regular session after a (NSE holiday calendar aware)."""
    return calendar.consecutive(a, b)


def ist_now() -> datetime:
    return datetime.now(IST)


def add_days(s: str, n: int) -> str:
    return (date.fromisoformat(s) + timedelta(days=n)).isoformat()
