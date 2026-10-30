"""LLM provider adapters. Each returns raw JSON text plus token usage; the router validates."""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import anthropic
import httpx

from .. import settings

log = logging.getLogger(__name__)

OLLAMA_CLOUD = "https://ollama.com"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Models that accept server-side refusal fallbacks (`fallbacks: "default"`).
_ANTHROPIC_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
_ANTHROPIC_NO_EFFORT = {"claude-haiku-4-5"}
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


class ProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, skip_provider: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.skip_provider = skip_provider


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    request_id: str | None = None


def estimate_tokens(text_chars: int, sample: str = "") -> int:
    # Devanagari tokenises far less efficiently than English; be conservative.
    per_token = 1.6 if _DEVANAGARI.search(sample) else 3.2
    return int(text_chars / per_token) + 50


class Provider:
    id = ""
    label = ""
    paid = False          # billed per token -> subject to the spending guard
    local = False

    def model(self) -> str:
        return settings.get("ai.models").get(self.id) or settings.DEFAULTS["ai.models"].get(self.id, "")

    def available(self) -> tuple[bool, str]:
        raise NotImplementedError

    def estimate_cost(self, prompt_chars: int, max_tokens: int, sample: str = "") -> float:
        return 0.0

    def complete_json(self, system: str, prompt: str, schema: dict, max_tokens: int) -> LLMResult:
        raise NotImplementedError

    def list_models(self) -> list[str]:
        return []


# ------------------------------------------------------------------ Ollama

class OllamaProvider(Provider):
    def __init__(self, cloud: bool):
        self.cloud = cloud
        self.id = "ollama_cloud" if cloud else "ollama_local"
        self.label = settings.PROVIDERS[self.id]
        self.local = not cloud

    def _base(self) -> str:
        return OLLAMA_CLOUD if self.cloud else settings.get("ai.ollama_host").rstrip("/")

    def _timeout(self) -> httpx.Timeout:
        """Cloud answers in a minute or two; a 5-minute silence is a stalled connection, not a slow model.
        A local model on this Mac can genuinely need longer."""
        return httpx.Timeout(300 if self.cloud else 1200, connect=10)

    def _headers(self) -> dict[str, str]:
        if self.cloud:
            key = settings.secret("ollama_api_key")
            return {"Authorization": f"Bearer {key}"} if key else {}
        return {}

    def available(self) -> tuple[bool, str]:
        if self.cloud:
            return (True, "key set") if settings.has_secret("ollama_api_key") else (False, "no Ollama API key")
        try:
            r = httpx.get(self._base() + "/api/tags", timeout=3)
            names = [m.get("name", "") for m in r.json().get("models", [])]
        except (httpx.HTTPError, ValueError):
            return False, "Ollama is not running on this Mac"
        want = self.model()
        if not any(n == want or n.split(":")[0] == want and ":" not in want for n in names):
            return False, f"model {want!r} not pulled (have: {', '.join(names) or 'none'})"
        return True, "running"

    def list_models(self) -> list[str]:
        try:
            r = httpx.get(self._base() + "/api/tags", headers=self._headers(), timeout=10)
            r.raise_for_status()
            return sorted(m.get("name", "") for m in r.json().get("models", []))
        except (httpx.HTTPError, ValueError):
            return []

    def complete_json(self, system: str, prompt: str, schema: dict, max_tokens: int) -> LLMResult:
        body: dict[str, Any] = {
            "model": self.model(),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "stream": False,
            "format": schema,
            "options": {"num_ctx": int(settings.get("ai.ollama_num_ctx")), "num_predict": max_tokens},
        }
        if not self.cloud:
            body["think"] = False   # keep local generations fast; structure is enforced by `format`
        try:
            r = httpx.post(self._base() + "/api/chat", json=body, headers=self._headers(),
                           timeout=self._timeout())
        except httpx.ConnectError as e:
            raise ProviderError(f"cannot reach {self.label}: {e}", skip_provider=True) from e
        except httpx.HTTPError as e:
            raise ProviderError(f"{self.label} request failed: {e}", retryable=True) from e
        if r.status_code in (401, 403):
            raise ProviderError(f"{self.label}: unauthorized (check the Ollama API key)", skip_provider=True)
        if r.status_code == 429:
            raise ProviderError(f"{self.label}: usage limit reached ({r.text[:200]})", skip_provider=True)
        if r.status_code == 400 and "think" in r.text and "think" in body:
            body.pop("think")
            r = httpx.post(self._base() + "/api/chat", json=body, headers=self._headers(), timeout=self._timeout())
        if r.status_code >= 500:
            raise ProviderError(f"{self.label}: server error {r.status_code}", retryable=True)
        if r.status_code >= 400:
            raise ProviderError(f"{self.label}: HTTP {r.status_code} {r.text[:300]}", skip_provider=True)
        data = r.json()
        if data.get("done_reason") == "length":
            raise ProviderError(f"{self.label}: output hit the token limit", retryable=True)
        return LLMResult(
            text=(data.get("message") or {}).get("content", ""),
            provider=self.id, model=self.model(),
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
        )


# ------------------------------------------------------------------ Anthropic

class AnthropicProvider(Provider):
    id = "anthropic"
    paid = True

    def __init__(self) -> None:
        self.label = settings.PROVIDERS[self.id]

    def _client(self) -> anthropic.Anthropic:
        return anthropic.Anthropic(api_key=settings.secret("anthropic_api_key"), max_retries=2, timeout=900.0)

    def _price(self, model: str) -> dict[str, float]:
        prices = settings.get("ai.prices").get("anthropic", {})
        return prices.get(model) or settings.ANTHROPIC_PRICES.get(model) or {"input": 10.0, "output": 50.0}

    def available(self) -> tuple[bool, str]:
        return (True, "key set") if settings.has_secret("anthropic_api_key") else (False, "no Claude API key")

    def estimate_cost(self, prompt_chars: int, max_tokens: int, sample: str = "") -> float:
        p = self._price(self.model())
        return (estimate_tokens(prompt_chars, sample) * p["input"] + max_tokens * p["output"]) / 1e6

    def list_models(self) -> list[str]:
        try:
            return [m.id for m in self._client().models.list()]
        except anthropic.APIError:
            return sorted(settings.ANTHROPIC_PRICES)

    def _cost(self, msg: Any) -> float:
        usage = msg.usage
        iterations = getattr(usage, "iterations", None) or []
        if iterations:
            total = 0.0
            for it in iterations:
                p = self._price(getattr(it, "model", None) or msg.model)
                total += (int(getattr(it, "input_tokens", 0) or 0) * p["input"]
                          + int(getattr(it, "output_tokens", 0) or 0) * p["output"]) / 1e6
            return total
        p = self._price(msg.model)
        inp = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * 1.25 \
            + (getattr(usage, "cache_read_input_tokens", 0) or 0) * 0.1
        return (inp * p["input"] + (usage.output_tokens or 0) * p["output"]) / 1e6

    def complete_json(self, system: str, prompt: str, schema: dict, max_tokens: int) -> LLMResult:
        model = self.model()
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if model not in _ANTHROPIC_NO_EFFORT:
            output_config["effort"] = settings.get("ai.anthropic_effort")
        kwargs: dict[str, Any] = dict(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": prompt}], output_config=output_config,
        )
        if model in _ANTHROPIC_FALLBACK_MODELS:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        try:
            with self._client().beta.messages.stream(**kwargs) as stream:
                msg = stream.get_final_message()
        except anthropic.AuthenticationError as e:
            raise ProviderError("Claude API: invalid API key", skip_provider=True) from e
        except anthropic.PermissionDeniedError as e:
            raise ProviderError(f"Claude API: permission denied ({e.message})", skip_provider=True) from e
        except anthropic.NotFoundError as e:
            raise ProviderError(f"Claude API: model {model!r} not found", skip_provider=True) from e
        except anthropic.RateLimitError as e:
            raise ProviderError("Claude API: rate limited", retryable=True) from e
        except anthropic.BadRequestError as e:
            raise ProviderError(f"Claude API: bad request ({e.message})", skip_provider=True) from e
        except anthropic.APIStatusError as e:
            raise ProviderError(f"Claude API: HTTP {e.status_code}", retryable=e.status_code >= 500) from e
        except anthropic.APIConnectionError as e:
            raise ProviderError("Claude API: network error", retryable=True) from e
        cost = self._cost(msg)
        result = LLMResult(
            text="".join(b.text for b in msg.content if getattr(b, "type", "") == "text"),
            provider=self.id, model=msg.model,
            input_tokens=int(msg.usage.input_tokens or 0), output_tokens=int(msg.usage.output_tokens or 0),
            cost_usd=cost, request_id=getattr(msg, "_request_id", None),
        )
        if msg.stop_reason == "refusal":
            err = ProviderError("Claude declined this request", skip_provider=True)
            err.result = result  # type: ignore[attr-defined]  # still billed; router records it
            raise err
        if msg.stop_reason == "max_tokens":
            err = ProviderError("Claude output hit max_tokens", retryable=True)
            err.result = result  # type: ignore[attr-defined]
            raise err
        return result


# ------------------------------------------------------------------ Gemini

class GeminiProvider(Provider):
    id = "gemini"

    def __init__(self) -> None:
        self.label = settings.PROVIDERS[self.id]

    @property
    def paid(self) -> bool:  # type: ignore[override]
        return settings.get("ai.gemini_tier") == "paid"

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": settings.secret("gemini_api_key") or ""}

    def available(self) -> tuple[bool, str]:
        return (True, "key set") if settings.has_secret("gemini_api_key") else (False, "no Gemini API key")

    def estimate_cost(self, prompt_chars: int, max_tokens: int, sample: str = "") -> float:
        if not self.paid:
            return 0.0
        p = settings.get("ai.prices").get("gemini", {"input": 0.0, "output": 0.0})
        return (estimate_tokens(prompt_chars, sample) * p["input"] + max_tokens * p["output"]) / 1e6

    def list_models(self) -> list[str]:
        try:
            r = httpx.get(f"{GEMINI_BASE}/models", params={"pageSize": 200}, headers=self._headers(), timeout=15)
            r.raise_for_status()
            return sorted(m["name"].removeprefix("models/") for m in r.json().get("models", [])
                          if "generateContent" in m.get("supportedGenerationMethods", []))
        except (httpx.HTTPError, ValueError, KeyError):
            return []

    def complete_json(self, system: str, prompt: str, schema: dict, max_tokens: int) -> LLMResult:
        model = self.model()
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": schema,
                                 "maxOutputTokens": max_tokens},
        }
        url = f"{GEMINI_BASE}/models/{model}:generateContent"
        try:
            r = httpx.post(url, json=body, headers=self._headers(), timeout=httpx.Timeout(600, connect=10))
            if r.status_code == 400 and "schema" in r.text.lower():
                # Older models may reject responseJsonSchema: fall back to plain JSON mode.
                body["generationConfig"].pop("responseJsonSchema")
                r = httpx.post(url, json=body, headers=self._headers(), timeout=600)
        except httpx.HTTPError as e:
            raise ProviderError(f"Gemini request failed: {e}", retryable=True) from e
        if r.status_code in (401, 403):
            raise ProviderError("Gemini: API key rejected", skip_provider=True)
        if r.status_code == 429:
            raise ProviderError("Gemini: rate limit / free-tier quota reached", skip_provider=True)
        if r.status_code >= 500:
            raise ProviderError(f"Gemini: server error {r.status_code}", retryable=True)
        if r.status_code >= 400:
            raise ProviderError(f"Gemini: HTTP {r.status_code} {r.text[:300]}", skip_provider=True)
        data = r.json()
        usage = data.get("usageMetadata", {})
        in_t = int(usage.get("promptTokenCount", 0))
        out_t = int(usage.get("candidatesTokenCount", 0)) + int(usage.get("thoughtsTokenCount", 0))
        cost = 0.0
        if self.paid:
            p = settings.get("ai.prices").get("gemini", {"input": 0.0, "output": 0.0})
            cost = (in_t * p["input"] + out_t * p["output"]) / 1e6
        cands = data.get("candidates") or []
        if not cands:
            raise ProviderError(f"Gemini returned no candidates ({data.get('promptFeedback', {})})", skip_provider=True)
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        result = LLMResult(text=text, provider=self.id, model=model, input_tokens=in_t, output_tokens=out_t, cost_usd=cost)
        if cands[0].get("finishReason") == "MAX_TOKENS":
            err = ProviderError("Gemini output hit max tokens", retryable=True)
            err.result = result  # type: ignore[attr-defined]
            raise err
        return result


# ------------------------------------------------------------------ Cloudflare Workers AI (free daily allowance)

CF_TEXT_NEURONS = (26_668, 204_805)       # neurons per million input / output tokens (Llama 3.3 70B fp8-fast)


class CloudflareProvider(Provider):
    """Free last resort when Ollama Cloud and Gemini are used up: Llama 3.3 70B on Workers AI with JSON-schema output.
    Shares the free 10,000 neurons a day with the Cloudflare FLUX pictures (about 700 neurons per script)."""
    id = "cloudflare_ai"

    def __init__(self) -> None:
        self.label = settings.PROVIDERS[self.id]

    def available(self) -> tuple[bool, str]:
        from ..media import visuals
        if not visuals.cloudflare_ready():
            return False, "Cloudflare Workers AI not set up (token + account ID)"
        if visuals.cloudflare_used_today() >= visuals.CF_FREE_NEURONS * 0.95:
            return False, "today's free Cloudflare allowance is used up"
        return True, "free daily allowance"

    def complete_json(self, system: str, prompt: str, schema: dict, max_tokens: int) -> LLMResult:
        from .. import db
        ok, why = self.available()
        if not ok:
            raise ProviderError(f"{self.label}: {why}", skip_provider=True)
        model = self.model()
        account = settings.get("visuals.cloudflare_account_id").strip()
        body = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                "response_format": {"type": "json_schema", "json_schema": schema}, "max_tokens": min(max_tokens, 8000)}
        try:
            r = httpx.post(f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}", json=body,
                           headers={"Authorization": f"Bearer {settings.secret('cloudflare_api_token')}"},
                           timeout=httpx.Timeout(300, connect=10))
        except httpx.HTTPError as e:
            raise ProviderError(f"Cloudflare request failed: {e}", retryable=True) from e
        if r.status_code in (401, 403):
            raise ProviderError("Cloudflare: token rejected", skip_provider=True)
        if r.status_code == 429:
            raise ProviderError("Cloudflare: today's free allowance is used up", skip_provider=True)
        if r.status_code >= 500:
            raise ProviderError(f"Cloudflare: server error {r.status_code}", retryable=True)
        if r.status_code >= 400:
            raise ProviderError(f"Cloudflare: HTTP {r.status_code} {r.text[:300]}", skip_provider=True)
        res = (r.json() or {}).get("result") or {}
        if res.get("choices"):                                   # OpenAI-style answer
            msg = res["choices"][0].get("message") or {}
            out, finish = msg.get("content"), res["choices"][0].get("finish_reason")
        else:
            out, finish = res.get("response"), None
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        usage = res.get("usage") or {}
        in_t, out_t = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        neurons = int((in_t * CF_TEXT_NEURONS[0] + out_t * CF_TEXT_NEURONS[1]) / 1e6) + 1
        db.insert("ai_usage", {"task": "text", "provider": "cloudflare", "model": model, "input_tokens": 0,
                               "output_tokens": neurons, "cost_usd": 0.0, "ok": 1, "error": "",
                               "duration_s": 0, "created_at": db.iso()})      # counts against the shared allowance
        result = LLMResult(text=text or "", provider=self.id, model=model, input_tokens=in_t, output_tokens=out_t)
        if finish == "length":
            err = ProviderError("Cloudflare output hit max tokens", retryable=True)
            err.result = result  # type: ignore[attr-defined]
            raise err
        return result


def get(provider_id: str) -> Provider:
    if provider_id == "ollama_local":
        return OllamaProvider(cloud=False)
    if provider_id == "ollama_cloud":
        return OllamaProvider(cloud=True)
    if provider_id == "anthropic":
        return AnthropicProvider()
    if provider_id == "gemini":
        return GeminiProvider()
    if provider_id == "cloudflare_ai":
        return CloudflareProvider()
    raise KeyError(provider_id)


def status_all() -> list[dict[str, Any]]:
    out = []
    for pid in settings.PROVIDERS:
        p = get(pid)
        t = time.time()
        ok, why = p.available()
        out.append({"id": pid, "label": p.label, "ok": ok, "detail": why, "model": p.model(),
                    "paid": p.paid, "local": p.local, "check_ms": int((time.time() - t) * 1000)})
    return out
