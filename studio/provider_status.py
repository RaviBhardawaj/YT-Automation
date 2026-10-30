"""One view of every outside service and local tool the studio uses: is it usable now, is it free, how much of
today's allowance was used, and what happens when it fails. Read-only and cheap: no paid call is ever made to test a
provider, and no secret value is ever returned — only whether one is set.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import db, settings


def _usage_today(provider: str) -> dict[str, int]:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    r = db.q1("SELECT COUNT(*) AS calls, COALESCE(SUM(ok),0) AS ok FROM ai_usage WHERE provider=? AND created_at>=?",
              (provider, day))
    return {"calls": int(r["calls"]), "ok": int(r["ok"]), "failed": int(r["calls"]) - int(r["ok"])}


def writers() -> list[dict[str, Any]]:
    from .ai import providers, router
    chain = router.provider_chain("script")
    out = []
    for pid, label in settings.PROVIDERS.items():
        try:
            prov = providers.get(pid)
            ok, why = prov.available()
        except Exception as e:  # a status page must never break
            prov, ok, why = None, False, f"check failed: {type(e).__name__}"
        out.append({"id": pid, "name": label, "available": bool(ok), "detail": why,
                    "cost": "paid" if prov is not None and prov.paid else "free",
                    "in_chain": pid in chain, "order": chain.index(pid) + 1 if pid in chain else None,
                    "usage": _usage_today("cloudflare" if pid == "cloudflare_ai" else pid)})
    return sorted(out, key=lambda r: (r["order"] is None, r["order"] or 99))


def media() -> list[dict[str, Any]]:
    from .film_studio import providers as fp
    rows = fp.status_all()
    for r in rows:
        r.pop("fallback", None)
    return rows


def platforms() -> list[dict[str, Any]]:
    from . import channels
    from .research import sources
    from .youtube import oauth
    out = [{"name": f"YouTube — {c['name']}", "available": oauth.connected(c["id"]),
            "detail": "connected (own login)" if oauth.connected(c["id"]) else "not connected"}
           for c in channels.all_channels()]
    used = sources.youtube_units_today()
    out.append({"name": "YouTube Data API quota (today)", "available": used < 9000,
                "detail": f"{used:,} of 10,000 units used (resets midnight Pacific)"})
    out.append({"name": "Google Trends / Google News / Wikipedia", "available": True,
                "detail": "public feeds, no key — checked when used"})
    return out


def overview() -> dict[str, Any]:
    w = writers()
    return {"writers": w, "media": media(), "platforms": platforms(),
            "paid_in_use": [r["name"] for r in w if r["cost"] == "paid" and r["in_chain"]],
            "writers_ready": sum(1 for r in w if r["available"] and r["in_chain"]),
            "budget_usd": float(settings.get("ai.monthly_budget_usd") or 0)}
