"""Subscription plans, limits and usage metering for workspaces.

Plans set *limits*, not prices: prices live with the billing provider once one is configured
(Settings › billing). Usage is recorded once per job step (idempotency key), so a retried job is
never charged twice. Costs are labelled `actual` (provider-reported) or `estimate` (configured rates).
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from . import db, settings

PLANS: dict[str, dict[str, Any]] = {
    "free": {"label": "Free", "videos_per_month": 10, "render_minutes_per_month": 60, "storage_gb": 5,
             "max_upload_mb": 500, "max_upload_minutes": 30, "channels": 1, "concurrent_jobs": 1},
    "creator": {"label": "Creator", "videos_per_month": 60, "render_minutes_per_month": 600, "storage_gb": 50,
                "max_upload_mb": 2000, "max_upload_minutes": 90, "channels": 3, "concurrent_jobs": 2},
    "pro": {"label": "Pro", "videos_per_month": 200, "render_minutes_per_month": 2400, "storage_gb": 250,
            "max_upload_mb": 8000, "max_upload_minutes": 240, "channels": 10, "concurrent_jobs": 4},
    "studio": {"label": "Studio", "videos_per_month": 1000, "render_minutes_per_month": 10000, "storage_gb": 1000,
               "max_upload_mb": 20000, "max_upload_minutes": 600, "channels": 50, "concurrent_jobs": 8},
}
# Features the superadmin can switch on or off per workspace (plan defaults first, then overrides).
FEATURES = {"create": "Create videos from a prompt", "upload_edit": "Upload & edit videos",
            "publish": "Publish to YouTube", "schedules": "Automatic daily videos", "ai_video": "AI video clips (paid)",
            "voice_clone": "Clone my voice"}
PLAN_FEATURES = {"free": {"create", "upload_edit", "publish", "schedules"},
                 "creator": {"create", "upload_edit", "publish", "schedules", "voice_clone"},
                 "pro": set(FEATURES), "studio": set(FEATURES)}

LIMIT_LABELS = {"videos_per_month": "Videos per month", "render_minutes_per_month": "Rendered minutes per month",
                "storage_gb": "Storage (GB)", "max_upload_mb": "Largest upload (MB)",
                "max_upload_minutes": "Longest upload (minutes)", "channels": "Channels",
                "concurrent_jobs": "Jobs at the same time"}
# Configured estimate rates (USD) used only when a provider doesn't report cost; shown as "estimate".
DEFAULT_RATES = {"voice_chars": 0.36 / 10000, "render_minutes": 0.0, "ai_calls": 0.0, "transcribe_minutes": 0.0}

_current: contextvars.ContextVar[int | None] = contextvars.ContextVar("current_workspace", default=None)


class LimitExceeded(ValueError):
    pass


def current_id() -> int | None:
    return _current.get()


@contextmanager
def using(workspace_id: int | None) -> Iterator[None]:
    token = _current.set(workspace_id)
    try:
        yield
    finally:
        _current.reset(token)


def workspace(ws_id: int) -> dict[str, Any] | None:
    w = db.q1("SELECT * FROM workspaces WHERE id=?", (ws_id,))
    if w:
        w["limits"] = db.loads(w["limits"], {}) or {}
    return w


def limits(ws_id: int) -> dict[str, Any]:
    w = workspace(ws_id) or {"plan": "free", "limits": {}}
    base = dict(PLANS.get(w["plan"], PLANS["free"]))
    base.update({k: v for k, v in (w.get("limits") or {}).items() if k in LIMIT_LABELS})
    return base


def _month_start() -> str:
    now = datetime.now().astimezone()
    return db.iso(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))


def used(ws_id: int) -> dict[str, float]:
    since = _month_start()
    rows = db.q("SELECT kind, SUM(quantity) AS q, SUM(COALESCE(cost_usd,0)) AS c FROM usage "
                "WHERE workspace_id=? AND created_at>=? GROUP BY kind", (ws_id, since))
    out = {r["kind"]: float(r["q"] or 0) for r in rows}
    out["cost_usd"] = round(sum(float(r["c"] or 0) for r in rows), 4)
    out["videos"] = db.q1("SELECT COUNT(*) AS n FROM productions WHERE workspace_id=? AND created_at>=? "
                          "AND parent_id IS NULL AND status<>'rejected'", (ws_id, since))["n"]
    out["storage_bytes"] = storage_bytes(ws_id)
    out["channels"] = db.q1("SELECT COUNT(*) AS n FROM channels WHERE workspace_id=? AND status<>'hidden'", (ws_id,))["n"]
    out["active_jobs"] = db.q1(
        "SELECT COUNT(*) AS n FROM tasks t JOIN productions p ON p.id=t.production_id "
        "WHERE p.workspace_id=? AND t.status='running'", (ws_id,))["n"]
    return out


def storage_bytes(ws_id: int) -> int:
    up = db.q1("SELECT COALESCE(SUM(size_bytes),0) AS n FROM media WHERE workspace_id=? AND status<>'deleted'",
               (ws_id,))["n"]
    return int(up) + int(db.q1("SELECT COALESCE(SUM(quantity),0) AS n FROM usage WHERE workspace_id=? AND "
                               "kind='output_bytes'", (ws_id,))["n"])


def check(ws_id: int, what: str, amount: float = 1) -> None:
    """Raise LimitExceeded before work starts if it would cross the workspace's plan."""
    lim, use = limits(ws_id), used(ws_id)
    if what == "video" and use["videos"] + amount > lim["videos_per_month"]:
        raise LimitExceeded(f"Monthly video limit reached ({lim['videos_per_month']} on the "
                            f"{PLANS.get((workspace(ws_id) or {}).get('plan'), PLANS['free'])['label']} plan).")
    if what == "render_minutes" and use.get("render_minutes", 0) + amount > lim["render_minutes_per_month"]:
        raise LimitExceeded(f"Monthly rendering limit reached ({lim['render_minutes_per_month']} minutes).")
    if what == "storage_bytes" and use["storage_bytes"] + amount > lim["storage_gb"] * 1024 ** 3:
        raise LimitExceeded(f"Storage limit reached ({lim['storage_gb']} GB). Delete old uploads or outputs.")
    if what == "channel" and use["channels"] + amount > lim["channels"]:
        raise LimitExceeded(f"Channel limit reached ({lim['channels']}).")


def record(ws_id: int, kind: str, quantity: float, *, production_id: int | None = None, idem_key: str | None = None,
           cost_usd: float | None = None) -> None:
    """Meter usage once. cost_usd given = provider-reported (actual); otherwise an estimate from configured rates."""
    rates = {**DEFAULT_RATES, **(settings.get("billing.rates") or {})}
    basis = "actual" if cost_usd is not None else ("estimate" if rates.get(kind) else None)
    if cost_usd is None and rates.get(kind):
        cost_usd = round(quantity * float(rates[kind]), 6)
    db.ex("INSERT OR IGNORE INTO usage (workspace_id, kind, quantity, cost_usd, cost_basis, production_id, idem_key, "
          "created_at) VALUES (?,?,?,?,?,?,?,?)",
          (ws_id, kind, quantity, cost_usd, basis, production_id, idem_key, db.iso()))


def features(ws_id: int) -> dict[str, bool]:
    w = db.q1("SELECT plan, permissions FROM workspaces WHERE id=?", (ws_id,)) or {"plan": "free", "permissions": "{}"}
    base = PLAN_FEATURES.get(w["plan"], PLAN_FEATURES["free"])
    over = db.loads(w.get("permissions"), {}) or {}
    return {k: bool(over[k]) if k in over else (k in base) for k in FEATURES}


def can(ws_id: int, feature: str) -> bool:
    return features(ws_id).get(feature, False)


class NotAllowed(ValueError):
    pass


def require(ws_id: int, feature: str) -> None:
    if not can(ws_id, feature):
        raise NotAllowed(f"“{FEATURES[feature]}” isn't enabled for your account. Ask the administrator, "
                         f"or see Pricing for plans that include it.")
