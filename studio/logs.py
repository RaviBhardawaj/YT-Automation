"""Logging: rotating file for everything, SQLite `logs` table for the dashboard."""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler

from . import config, db

_configured = False

# Defence in depth: never let something that looks like a credential reach a log line.
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"ya29\.[0-9A-Za-z_\-\.]+"),
    re.compile(r"1//[0-9A-Za-z_\-]{20,}"),
    re.compile(r"\b[0-9a-f]{32}\.[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{8,}=*"),
    re.compile(r"(?i)(api[_-]?key|api[_-]?subscription[_-]?key|subscription[_-]?key|token|secret|authorization)"
               r"([\"'=:\s]+)([^\s\"'&,]{8,})"),
]


def redact(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        if pat.groups >= 3:
            text = pat.sub(lambda m: m.group(1) + m.group(2) + "[redacted]", text)
        else:
            text = pat.sub("[redacted]", text)
    return text


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup() -> None:
    global _configured
    if _configured:
        return
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(config.LOG_DIR / "studio.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(_RedactingFormatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"))
    console = logging.StreamHandler()
    console.setFormatter(_RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(console)
    for noisy in ("httpx", "httpcore", "httpx2", "anthropic", "phonemizer", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True


def event(level: str, source: str, message: str, production_id: int | None = None, task_id: int | None = None) -> None:
    """Record a user-visible event (also written to the log file)."""
    message = redact(str(message))[:4000]
    logging.getLogger(f"studio.{source}").log(getattr(logging, level.upper(), logging.INFO), message)
    try:
        db.insert("logs", {
            "level": level.lower(), "source": source, "message": message,
            "production_id": production_id, "task_id": task_id, "created_at": db.iso(),
        })
    except Exception:  # logging must never take the pipeline down
        logging.getLogger("studio.logs").exception("could not write event row")


def prune(keep: int = 20000) -> None:
    db.ex("DELETE FROM logs WHERE id < (SELECT COALESCE(MAX(id),0) - ? FROM logs)", (keep,))
