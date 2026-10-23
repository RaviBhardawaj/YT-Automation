"""Daily data from the owner's Pivot Reversal project (~/Downloads/Pivot Reversal Strategy).

That app computes NIFTY Fibonacci levels from its own feed and writes them to two files:
  tv_pivot_cache.json      — after 15:30 ("basis": "post_close"): today's high / low / close (Daily) and the
                             15-Min / 1-Hour high / low / close for the next session's levels
  multi_symbol_levels.json — during the session (pre-close): the previous session's high / low / close,
                             i.e. the inputs of today's levels
Only these level files are read — never the app's trade-setup / order files. Neither file has an open price
or candles, so the open is stored as unknown and there is no candle-level intraday analysis.
Every level is recomputed here and must match the project's numbers before anything is imported.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .. import config, db, settings
from . import audit, calendar, marketdata, pivots

SOURCE_ID = "pivot_reversal_project"
SOURCE_SEED = {
    "id": SOURCE_ID, "name": "Pivot Reversal project (owner's app; levels from its own market feed)",
    "kind": "local_app", "terms_url": "",
    "terms_summary": "High / low / close and levels computed by the owner's Pivot Reversal app from its own feed "
                     "(broker / web chart data for personal use). Publication rights not established — the owner "
                     "accepted this risk via the recorded owner override.",
    "automated_access_allowed": 1, "permits_public_video": None, "permits_monetization": None,
    "status": "REVIEW_REQUIRED", "notes": "Local files only; nothing is fetched from the internet by this app.",
}
FILES = ("tv_pivot_cache.json", "multi_symbol_levels.json")


class ProjectDataError(Exception):
    pass


def project_dir() -> Path:
    return Path(settings.get("nifty.pivot_project_dir") or (Path.home() / "Downloads" / "Pivot Reversal Strategy"))


def _hlc(block: dict[str, Any], what: str) -> dict[str, float]:
    try:
        h, lo, c = float(block["H"]), float(block["L"]), float(block["C"])
    except (KeyError, TypeError, ValueError) as e:
        raise ProjectDataError(f"{what}: high / low / close missing") from e
    if not (0 < lo <= c <= h) and not (0 < lo <= h and lo <= c <= h):
        raise ProjectDataError(f"{what}: inconsistent high {h}, low {lo}, close {c}")
    # the project's own levels must equal ours (same formula) — otherwise its inputs are not what we think
    ours = pivots.fibonacci_pivots(h, lo, c)["levels"]
    for k in ("P", "S1", "S2", "R1", "R2"):
        if k in block and abs(float(block[k]) - ours[k]) > 0.05:
            raise ProjectDataError(f"{what}: project {k} {block[k]} differs from recomputed {ours[k]}")
    return {"high": h, "low": lo, "close": c}


def read_today(today: date) -> dict[str, Any]:
    d = project_dir()
    paths = {name: d / name for name in FILES}
    for name, p in paths.items():
        if not p.exists():
            raise ProjectDataError(f"{name} not found in {d}")
    tv = json.loads(paths["tv_pivot_cache.json"].read_text())["NIFTY"]
    ms = json.loads(paths["multi_symbol_levels.json"].read_text())["NIFTY"]
    close_time = datetime(today.year, today.month, today.day, *marketdata.SESSION_CLOSE, tzinfo=marketdata.IST)
    mtime = datetime.fromtimestamp(paths["tv_pivot_cache.json"].stat().st_mtime, marketdata.IST)
    if tv.get("date") != today.isoformat() or tv.get("basis") != "post_close" or mtime < close_time:
        raise ProjectDataError(f"tv_pivot_cache.json is not today's post-close file (date {tv.get('date')}, "
                               f"basis {tv.get('basis')}, written {mtime:%d %b %H:%M}) — is the Pivot Reversal app "
                               "running after 15:30?")
    calc = datetime.strptime(ms.get("calculated_at", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=marketdata.IST)
    if calc.date() != today or calc >= close_time:
        raise ProjectDataError(f"multi_symbol_levels.json was calculated {calc:%d %b %H:%M}; today's pre-close "
                               "levels are needed")
    prev = calendar.previous_session(today)
    return {
        "today": _hlc(tv["Daily"], f"{today} daily"),
        "previous": _hlc(ms["mtf_pivots"]["Daily"], f"{prev} daily"),
        "previous_date": prev.isoformat(),
        "mtf": {"15m": _hlc(tv["15-Min"], "15-Min"), "1h": _hlc(tv["1-Hour"], "1-Hour")},
        "retrieved_at": mtime, "paths": paths,
    }


def import_today(today: date | None = None, actor: str = "scheduler") -> int:
    """Create today's dataset (previous + today daily bars, open unknown) from the project's files."""
    today = today or marketdata.ist_now().date()
    if not calendar.is_trading_day(today):
        raise ProjectDataError(f"{today} is not an NSE trading day")
    data = read_today(today)
    snapshot = {"read_at": db.iso(), "project_dir": str(project_dir()),
                "files": {n: json.loads(p.read_text()) for n, p in data["paths"].items()}}
    for n in snapshot["files"]:                      # keep only the NIFTY level blocks, nothing about orders
        snapshot["files"][n] = {"NIFTY": snapshot["files"][n].get("NIFTY")}
    marketdata.seed_sources()
    ds_id = db.insert("market_datasets", {
        "source_id": SOURCE_ID, "symbol": marketdata.SYMBOL, "exchange": marketdata.EXCHANGE, "timeframe": "1d",
        "timezone": "Asia/Kolkata", "data_nature": "end_of_day", "retrieved_at": db.iso(data["retrieved_at"]),
        "retrieved_by": actor, "retrieval_notes": json.dumps({"mtf_hlc": {today.isoformat(): data["mtf"]}}),
        "original_filename": "pivot_reversal_levels.json", "first_date": data["previous_date"],
        "last_date": today.isoformat(), "rows": 2,
        "validation": db.dumps({"errors": [], "warnings": ["open price not provided by the source (left unknown)"]}),
        "status": "valid_with_warnings", "created_at": db.iso()})
    stored = config.MARKET_DIR / f"dataset_{ds_id:05d}_pivot_reversal_levels.json"
    stored.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    stored.chmod(0o444)
    db.update("market_datasets", ds_id, {"stored_path": str(stored), "sha256": audit.file_sha256(stored)})
    with db.tx() as c:
        for d_, b in ((data["previous_date"], data["previous"]), (today.isoformat(), data["today"])):
            c.execute("INSERT INTO market_bars(dataset_id,trade_date,ts,open,high,low,close,volume) "
                      "VALUES(?,?,NULL,NULL,?,?,?,NULL)", (ds_id, d_, b["high"], b["low"], b["close"]))
    audit.record("dataset_imported", {"dataset_id": ds_id, "source_id": SOURCE_ID, "sessions": [data["previous_date"],
                                      today.isoformat()], "open": "unknown", "levels_cross_checked": True},
                 actor=actor, artifact=stored)
    return ds_id


def mtf_hlc(ds: dict[str, Any], trade_date: str) -> dict[str, dict[str, float]] | None:
    """15-Min / 1-Hour high-low-close recorded with a project dataset for that session, if any."""
    try:
        return (json.loads(ds.get("retrieval_notes") or "{}").get("mtf_hlc") or {}).get(trade_date)
    except ValueError:
        return None
