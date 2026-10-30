"""Database backups: a consistent copy of the live SQLite database (SQLite's online backup API — safe while the
app runs), checked before it counts, kept by a retention policy that never deletes the newest good copy, and a
restore drill that proves a copy opens and holds the data — without ever touching the live database.

    automatic copies   data/backups/auto/studio-YYYYMMDD-HHMMSS.db  (+ .json: sha256, size, version, check result)
    manual copies      data/backups/*.db — never pruned
    retention          the newest KEEP_DAILY copies + one per ISO week for KEEP_WEEKLY weeks; never the newest good one

Keys and logins live in the macOS Keychain and are not in these files (by design). Videos live in
data/productions/ and are not copied here (size); see docs/operations/BACKUP_AND_RESTORE.md.

Restore (`python -m studio.backup restore <file>`) refuses while the studio is running, copies the current database
aside first, and checks the restored file.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import socket
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config, db, logs

AUTO_DIR = config.DATA_DIR / "backups" / "auto"
KEEP_DAILY = 7
KEEP_WEEKLY = 4
KEY_TABLES = ("channels", "productions", "settings", "users", "tasks")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check(path: Path) -> dict[str, Any]:
    """Open a backup read-only and prove it is usable: integrity check, schema version, key table counts."""
    out: dict[str, Any] = {"file": str(path), "ok": False}
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            out["integrity"] = c.execute("PRAGMA integrity_check").fetchone()[0]
            out["user_version"] = c.execute("PRAGMA user_version").fetchone()[0]
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            out["counts"] = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in KEY_TABLES if t in names}
        finally:
            c.close()
        out["ok"] = out["integrity"] == "ok" and all(t in out["counts"] for t in KEY_TABLES)
    except sqlite3.Error as e:
        out["error"] = str(e)[:200]
    return out


def make(kind: str = "auto") -> dict[str, Any]:
    """A consistent copy of the live database, checked; written under a temporary name and renamed only when good."""
    AUTO_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    final = AUTO_DIR / f"studio-{stamp}.db"
    tmp = final.with_suffix(".partial")
    src = sqlite3.connect(config.DB_PATH, timeout=30)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    res = check(tmp)
    if not res["ok"]:
        tmp.unlink(missing_ok=True)
        logs.event("error", "backup", f"Backup failed its check — not kept: {res.get('integrity') or res.get('error')}")
        raise RuntimeError(f"backup failed its check: {res}")
    tmp.replace(final)
    meta = {**res, "file": final.name, "sha256": _sha256(final), "bytes": final.stat().st_size, "kind": kind,
            "made_at": db.iso()}
    final.with_suffix(".json").write_text(json.dumps(meta, indent=1))
    removed = prune()
    logs.event("info", "backup", f"Database backed up and checked: {final.name} ({meta['bytes'] / 1e6:.1f} MB)"
               + (f"; {len(removed)} old copy/copies removed" if removed else ""))
    return {**meta, "removed": removed}


def _auto() -> list[tuple[Path, dict[str, Any]]]:
    out = []
    for f in sorted(AUTO_DIR.glob("studio-*.db")):
        side = f.with_suffix(".json")
        meta = json.loads(side.read_text()) if side.exists() else {}
        out.append((f, meta))
    return out


def prune() -> list[str]:
    """Retention: newest KEEP_DAILY + the newest of each of the last KEEP_WEEKLY ISO weeks; the newest good copy is
    always kept, and nothing outside backups/auto is touched."""
    items = _auto()
    good = [f for f, m in items if m.get("ok")]
    keep = set(good[-KEEP_DAILY:])
    weeks: dict[str, Path] = {}
    for f in good:
        d = datetime.strptime(f.stem.split("-", 1)[1], "%Y%m%d-%H%M%S")
        weeks[f"{d.isocalendar()[0]}-{d.isocalendar()[1]}"] = f                 # newest of its week wins
    keep |= set(sorted(weeks.values())[-KEEP_WEEKLY:])
    if good:
        keep.add(good[-1])                                                     # last known good: never deleted
    removed = []
    for f, _ in items:
        if f not in keep:
            f.unlink(missing_ok=True)
            f.with_suffix(".json").unlink(missing_ok=True)
            removed.append(f.name)
    return removed


def verify(path: Path) -> dict[str, Any]:
    """Re-check a stored backup: still matches its recorded checksum, still passes the integrity check."""
    side = path.with_suffix(".json")
    meta = json.loads(side.read_text()) if side.exists() else {}
    res = check(path)
    res["checksum_matches"] = (meta.get("sha256") == _sha256(path)) if meta.get("sha256") else None
    res["ok"] = res["ok"] and res["checksum_matches"] is not False
    return res


def restore_drill(path: Path | None = None) -> dict[str, Any]:
    """Prove the newest (or given) backup can be restored: copy it to a temporary place, open it there, compare it
    with the live database. The live database is never touched."""
    items = [f for f, m in _auto() if m.get("ok")]
    path = path or (items[-1] if items else None)
    if not path:
        return {"ok": False, "why": "no backup to test"}
    with tempfile.TemporaryDirectory() as td:
        copy = Path(td) / "restored.db"
        shutil.copy2(path, copy)
        res = check(copy)
    live = check(config.DB_PATH)
    res["live_counts"] = live.get("counts")
    res["same_schema"] = res.get("user_version") == live.get("user_version")
    res["file"] = path.name
    logs.event("info" if res["ok"] else "error", "backup",
               f"Restore drill on {path.name}: {'passed' if res['ok'] else 'FAILED'}")
    return res


def status() -> dict[str, Any]:
    items = _auto()
    good = [(f, m) for f, m in items if m.get("ok")]
    last = good[-1] if good else None
    return {"count": len(items), "bytes": sum(f.stat().st_size for f, _ in items),
            "last": {"file": last[0].name, "made_at": last[1].get("made_at"), "bytes": last[1].get("bytes")}
            if last else None, "folder": str(AUTO_DIR), "keep_daily": KEEP_DAILY, "keep_weekly": KEEP_WEEKLY}


def _studio_running() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", config.PORT)) == 0


def restore(path: Path) -> dict[str, Any]:
    """Put a backup back as the live database. Only while the studio is stopped; the current database is copied
    aside first, so a restore can itself be undone."""
    if _studio_running():
        raise RuntimeError("stop the studio first: launchctl bootout gui/$(id -u)/com.ytautomation.studio")
    res = check(path)
    if not res["ok"]:
        raise RuntimeError(f"this backup failed its check — not restored: {res}")
    aside = config.DB_PATH.with_name(f"studio-before-restore-{datetime.now():%Y%m%d-%H%M%S}.db")
    if config.DB_PATH.exists():
        shutil.copy2(config.DB_PATH, aside)
    for extra in ("-wal", "-shm"):
        Path(str(config.DB_PATH) + extra).unlink(missing_ok=True)
    shutil.copy2(path, config.DB_PATH)
    return {"restored": path.name, "previous_saved_as": aside.name, "check": check(config.DB_PATH)}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "now":
        print(json.dumps(make("manual"), indent=1))
    elif cmd == "drill":
        print(json.dumps(restore_drill(), indent=1))
    elif cmd == "restore" and len(sys.argv) > 2:
        print(json.dumps(restore(Path(sys.argv[2])), indent=1))
    else:
        print(json.dumps(status(), indent=1))
