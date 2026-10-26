"""Provider routing, spending guard, validation-retry loop and usage accounting.

Every model response is parsed as JSON and validated with Pydantic (plus an optional
task-specific validator) before anything else in the app sees it. Model output is
data only: it is never executed, and never placed on a command line.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from .. import db, settings
from . import providers
from .schema import extract_json, provider_schema, schema_prompt, short_errors

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

# Paid calls are serialised so two workers cannot both pass the budget check
# and overshoot the limit together.
_paid_lock = threading.Lock()


class BudgetExceeded(Exception):
    pass


class AllProvidersFailed(Exception):
    pass


def month_start() -> str:
    now = datetime.now(timezone.utc)
    return db.iso(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))


def month_spend() -> float:
    row = db.q1("SELECT COALESCE(SUM(cost_usd),0) AS s FROM ai_usage WHERE created_at >= ?", (month_start(),))
    return float(row["s"] if row else 0.0)


def spend_by_provider() -> list[dict[str, Any]]:
    return db.q(
        "SELECT provider, model, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens, "
        "SUM(output_tokens) AS output_tokens, ROUND(SUM(cost_usd),4) AS cost_usd, "
        "SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS errors "
        "FROM ai_usage WHERE created_at >= ? GROUP BY provider, model ORDER BY cost_usd DESC",
        (month_start(),))


def _record(task: str, res: providers.LLMResult | None, provider_id: str, model: str,
            ok: bool, error: str | None, duration: float) -> None:
    db.insert("ai_usage", {
        "provider": provider_id, "model": res.model if res else model, "task": task,
        "input_tokens": res.input_tokens if res else 0, "output_tokens": res.output_tokens if res else 0,
        "cost_usd": res.cost_usd if res else 0.0, "ok": 1 if ok else 0, "error": (error or "")[:500],
        "request_id": res.request_id if res else None, "duration_s": round(duration, 2), "created_at": db.iso(),
    })


def is_free(provider_id: str | None) -> bool:
    """Costs nothing: Ollama (local, or the cloud plan's included usage) and Gemini on its free tier."""
    if provider_id in ("ollama_local", "ollama_cloud"):
        return True
    return provider_id == "gemini" and settings.get("ai.gemini_tier") != "paid" and settings.has_secret("gemini_api_key")


def provider_chain(task: str) -> list[str]:
    choice = settings.get("ai.task_providers").get(task, "default")
    primary = settings.get("ai.default_provider") if choice in ("default", "", None) else choice
    chain = [primary, settings.get("ai.fallback_provider")]
    if settings.get("ai.on_budget_exceeded") == "fallback_local":
        chain += ["ollama_local", "ollama_cloud"]   # non-billed last resorts
    if settings.get("ai.free_first", True):
        # free providers first (in the configured order, then the other free ones); paid ones only when every
        # free option failed or is used up
        free = [p for p in chain + ["ollama_local", "ollama_cloud", "gemini"] if is_free(p)]
        chain = free + [p for p in chain if not is_free(p)]
    seen: list[str] = []
    for pid in chain:
        if pid and pid in settings.PROVIDERS and pid not in seen:
            seen.append(pid)
    return seen


def generate(task: str, system: str, prompt: str, model_cls: type[T], *, max_tokens: int = 8000,
             validate: Callable[[T], None] | None = None, attempts_per_provider: int = 3) -> tuple[T, dict[str, Any]]:
    """Return a validated instance of `model_cls` and metadata about which provider produced it."""
    schema = provider_schema(model_cls)
    errors: list[str] = []
    budget_blocked = False
    budget = float(settings.get("ai.monthly_budget_usd"))
    policy = settings.get("ai.on_budget_exceeded")

    for pid in provider_chain(task):
        prov = providers.get(pid)
        ok, why = prov.available()
        if not ok:
            errors.append(f"{pid}: {why}")
            continue
        feedback = ""
        tokens = max_tokens
        # Claude enforces the schema server-side; the others get it spelled out as well.
        fmt = "" if pid == "anthropic" else schema_prompt(schema)
        for attempt in range(attempts_per_provider):
            full_prompt = prompt + fmt + feedback
            lock = _paid_lock if prov.paid else None
            if lock:
                lock.acquire()
            try:
                if prov.paid:
                    estimate = prov.estimate_cost(len(system) + len(full_prompt), tokens, sample=full_prompt[:2000])
                    spent = month_spend()
                    if spent + estimate > budget:
                        budget_blocked = True
                        msg = (f"{pid}: monthly AI budget ${budget:.2f} would be exceeded "
                               f"(spent ${spent:.2f}, this call up to ${estimate:.2f})")
                        errors.append(msg)
                        if policy == "pause":
                            raise BudgetExceeded(msg)
                        break
                t0 = time.time()
                try:
                    res = prov.complete_json(system, full_prompt, schema, tokens)
                except providers.ProviderError as e:
                    partial = getattr(e, "result", None)
                    _record(task, partial, pid, prov.model(), False, str(e), time.time() - t0)
                    errors.append(f"{pid}: {e}")
                    if e.skip_provider:
                        break
                    if "token" in str(e).lower():
                        tokens = min(int(tokens * 1.6), 32000)
                    time.sleep(min(2 ** attempt * 2, 20))
                    continue
                _record(task, res, pid, prov.model(), True, None, time.time() - t0)
            finally:
                if lock:
                    lock.release()

            try:
                obj = model_cls.model_validate(extract_json(res.text))
                if validate:
                    validate(obj)
                meta = {"provider": pid, "model": res.model, "input_tokens": res.input_tokens,
                        "output_tokens": res.output_tokens, "cost_usd": round(res.cost_usd, 5),
                        "attempt": attempt + 1, "local": prov.local}
                return obj, meta
            except (ValueError, ValidationError) as e:
                detail = short_errors(e)
                errors.append(f"{pid}: invalid output (attempt {attempt + 1}): {detail[:300]}")
                log.info("validation failed for %s via %s: %s", task, pid, detail)
                feedback = ("\n\n---\nYOUR PREVIOUS ANSWER WAS REJECTED by the validator:\n" + detail +
                            "\nFix every listed problem and return the complete corrected JSON object only.")
    if budget_blocked and policy == "pause":
        raise BudgetExceeded("; ".join(errors[-4:]))
    raise AllProvidersFailed("; ".join(errors[-6:]) or "no AI provider is configured")
