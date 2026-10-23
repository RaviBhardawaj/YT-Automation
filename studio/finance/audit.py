"""Append-only, hash-chained audit log.

Each entry stores the SHA-256 of the previous entry, so editing or deleting a past entry
breaks the chain and `verify_chain()` reports it. Artifacts (datasets, calculations,
scripts, gate results) are written to data/audit/<production>/ and their hashes logged.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from .. import config, db

_lock = threading.Lock()


def _hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def artifact_dir(production_id: int | None) -> Path:
    d = config.AUDIT_DIR / (f"{production_id:05d}" if production_id else "general")
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_artifact(production_id: int | None, name: str, data: Any) -> Path:
    """Write a JSON artifact (never overwritten: a new version gets a numeric suffix)."""
    d = artifact_dir(production_id)
    path = d / f"{name}.json"
    n = 2
    while path.exists():
        path = d / f"{name}.v{n}.json"
        n += 1
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def _snapshot(artifact: Path, production_id: int | None) -> Path:
    """Keep an immutable, content-addressed copy of a working file (thumbnails etc. are re-rendered in place)."""
    artifact = Path(artifact)
    if artifact.resolve().is_relative_to(config.AUDIT_DIR.resolve()) or \
            artifact.resolve().is_relative_to(config.MARKET_DIR.resolve()):
        return artifact                      # already immutable (audit artifacts, read-only dataset copies)
    sha = file_sha256(artifact)
    d = artifact_dir(production_id) / "files"
    d.mkdir(parents=True, exist_ok=True)
    snap = d / f"{sha[:24]}{artifact.suffix}"
    if not snap.exists():
        snap.write_bytes(artifact.read_bytes())
        snap.chmod(0o444)
    return snap


def record(event: str, details: dict[str, Any], *, production_id: int | None = None, actor: str = "system",
           artifact: Path | None = None) -> int:
    if artifact:
        artifact = _snapshot(artifact, production_id)
    with _lock:
        prev = db.q1("SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1")
        prev_hash = prev["entry_hash"] if prev else "GENESIS"
        row = {
            "production_id": production_id, "event": event, "actor": actor,
            "details": db.dumps(details), "artifact_path": str(artifact) if artifact else None,
            "artifact_sha256": file_sha256(artifact) if artifact else None,
            "prev_hash": prev_hash, "created_at": db.iso(),
        }
        row["entry_hash"] = _hash({k: row[k] for k in sorted(row)})
        return db.insert("audit_log", row)


def log_artifact(event: str, production_id: int | None, name: str, data: Any, actor: str = "system",
                 extra: dict[str, Any] | None = None) -> Path:
    path = write_artifact(production_id, name, data)
    record(event, {"artifact": name, **(extra or {})}, production_id=production_id, actor=actor, artifact=path)
    return path


def verify_chain() -> dict[str, Any]:
    prev_hash = "GENESIS"
    problems, notes = [], []
    rows = db.q("SELECT * FROM audit_log ORDER BY id")
    immutable = (config.AUDIT_DIR.resolve(), config.MARKET_DIR.resolve())
    for r in rows:
        expected = _hash({k: r[k] for k in sorted(r) if k not in ("id", "entry_hash")})
        if r["prev_hash"] != prev_hash:
            problems.append(f"entry {r['id']}: previous-hash link broken")
        if r["entry_hash"] != expected:
            problems.append(f"entry {r['id']}: contents changed after logging")
        p = Path(r["artifact_path"]) if r["artifact_path"] else None
        if p and p.exists() and file_sha256(p) != r["artifact_sha256"]:
            if any(p.resolve().is_relative_to(d) for d in immutable):
                problems.append(f"entry {r['id']}: artifact {p.name} modified")
            else:   # logged before snapshots existed; the working file was re-rendered in place since
                notes.append(f"entry {r['id']}: working file {p.name} was re-rendered after it was logged (no snapshot)")
        prev_hash = r["entry_hash"]
    return {"entries": len(rows), "ok": not problems, "problems": problems, "notes": notes}


def entries(production_id: int | None = None, limit: int = 300) -> list[dict[str, Any]]:
    if production_id:
        rows = db.q("SELECT * FROM audit_log WHERE production_id=? ORDER BY id DESC LIMIT ?", (production_id, limit))
    else:
        rows = db.q("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        r["details"] = db.loads(r["details"], {})
    return rows
