"""User settings (SQLite) and credentials (macOS Keychain via `keyring`).

Secrets never touch the database, logs or HTML. The UI only ever learns whether a
secret is set, plus its last four characters.
"""
from __future__ import annotations

import copy
import re
import threading
from typing import Any

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from . import config, db

# Prices are USD per million tokens, copied from the providers' published pricing
# pages (Anthropic: cached 2026-10-06). They are editable in Settings because
# providers change them; the spending guard always uses the values stored here.
ANTHROPIC_PRICES = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50},
    "claude-fable-5-1": {"input": 10.00, "output": 50.00},
    "claude-opus-5": {"input": 5.00, "output": 25.00},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
}

AI_TASKS = {
    "niche_research": "Niche research synthesis",
    "ideation": "Topic ideation & ranking",
    "script": "Scriptwriting, storyboard & metadata",
    "review": "Fact-check & originality review",
    "learning": "Daily learning from results",
}

PROVIDERS = {
    "ollama_local": "Ollama (local, on this Mac)",
    "ollama_cloud": "Ollama Cloud (hosted models, your Ollama key)",
    "anthropic": "Claude API (Anthropic)",
    "gemini": "Gemini API (Google)",
}

DEFAULTS: dict[str, Any] = {
    "interview": {},
    "interview_complete": False,
    "ai.default_provider": "ollama_local",
    "ai.fallback_provider": "ollama_cloud",
    "ai.task_providers": {t: "default" for t in AI_TASKS},
    "ai.models": {
        "ollama_local": "qwen3.5:9b",
        "ollama_cloud": "gemma4:31b",
        "anthropic": "claude-opus-5-5",
        "gemini": "gemini-flash-latest",          # alias: Google retired 2.5 Flash for new keys
    },
    "ai.anthropic_effort": "medium",
    "ai.free_first": True,                        # free providers first; paid ones only when free ones fail
    "ai.monthly_budget_usd": 5.0,
    "ai.on_budget_exceeded": "fallback_local",   # fallback_local | pause
    "ai.prices": {
        "anthropic": ANTHROPIC_PRICES,
        "gemini": {"input": 0.30, "output": 2.50},
    },
    "ai.gemini_tier": "free",                     # free | paid
    "ai.ollama_host": "http://127.0.0.1:11434",
    "ai.ollama_num_ctx": 16384,
    "research.sources": {
        "google_news": True, "wikipedia": True, "youtube": True,
        "ollama_web": True, "hackernews": False, "rss": True,
    },
    "research.region": "IN",
    "research.contact": "",
    "research.max_page_fetch": 4,
    "research.youtube_daily_unit_budget": 3000,
    "voice": {"engine": "kokoro", "voice_id": "hf_alpha", "speed": 1.0, "kokoro_model": "int8"},
    # Every video is Hindi unless it targets an international audience.
    "channel.audience_languages": {"india": "Hindi", "international": "English"},
    "voice.by_language": {"Hindi": {"engine": "kokoro", "voice_id": "hf_alpha"}},
    "visuals.order": ["pexels", "pixabay", "openverse", "wikimedia", "local_sd", "gemini_image", "procedural"],
    "visuals.enabled": {
        "pexels": True, "pixabay": True, "openverse": True, "wikimedia": True,
        "local_sd": False, "gemini_image": False, "procedural": True,
    },
    "visuals.prefer_video": True,
    "visuals.local_sd_url": "http://127.0.0.1:7860",
    "visuals.gemini_image_model": "gemini-2.5-flash-image",
    "visuals.gemini_image_price": 0.039,
    "visuals.vision_check": True,                 # a vision model checks each stock picture fits its scene
    "visuals.vision_gemini_model": "gemini-flash-lite-latest",
    "visuals.vision_per_minute": 14,              # Gemini Flash-Lite free-tier pacing for those checks
    "visuals.min_relevance": 6,                   # 0-10: pictures scoring lower are not used
    "visuals.cloudflare_account_id": "",          # Workers AI account for free FLUX.2 klein images
    "render.film_look": "auto",                   # auto = cinematic grade for real footage, bright for kids; off
    "brand": {
        "channel_name": "",
        "primary": "#FFD23F",
        "accent": "#FF4D6D",
        "text": "#FFFFFF",
        "palette": ["#14213D", "#2B1B4E", "#0B3954", "#3A0F1E", "#103A30", "#3B2A0B"],
        "watermark": True,
        "caption_style": "bold",                 # bold (centre, word-group) | subtle (bottom line)
        "logo_path": "",
    },
    "render.preset": "veryfast",
    "render.crf": 20,
    "render.transition": 0.35,
    "render.music_file": "",
    "render.music_license": "",
    "render.music_volume": 0.12,
    "youtube.privacy_default": "private",
    "youtube.category_id": "27",
    "youtube.upload_captions": False,
    "youtube.made_for_kids": False,
    "youtube.audit_passed": False,
    "youtube.client_id": "",
    "meta.app_id": "",                       # Meta (Facebook) app ID; the app secret is in the Keychain
    "meta.config_id": "",                    # optional: Facebook Login for Business configuration ID
    "meta.graph_version": "v26.0",
    "youtube.channel": {},
    "publish.mode": "approve_first",              # approve_first | auto_after_validation
    "publish.slots": {"short": ["18:30"], "long": ["19:30"]},
    "shorts.enabled": True,                  # cut Shorts from every finished long NIFTY episode
    "shorts.per_episode": 4,                 # upper bound; fewer are made when fewer good segments exist
    "nifty.daily_episode": True,             # after-close daily episode when that session's data is imported
    "nifty.daily_audience": "india",
    "nifty.require_intraday": True,
    "nifty.auto_import_pivot_project": True,  # no manual import: read today's data from the Pivot Reversal app's files
    "nifty.pivot_project_dir": "",            # empty = ~/Downloads/Pivot Reversal Strategy
    "script.sections_per_call": 2,           # long NIFTY scripts are written a few sections per AI call (models stop ~300-700 words)          # wait for today's complete 15m + 1h bars (until 22:00) before the 4 PM episode
    "compliance.auto_private_upload": False, # upload automatically as PRIVATE once the gate allows it
    "videogen.enabled": False,               # AI text-to-video clips (Gemini Omni Flash); off until the owner enables it
    "videogen.model": "gemini-omni-1.1-flash",
    "videogen.resolution": "720p",
    "videogen.usd_per_second": 0.10,         # Google's published ~720p rate; used for estimates
    "videogen.usd_per_million_output_tokens": 17.50,
    "videogen.seconds_per_clip": 8,
    "videogen.max_clips_per_video": 3,
    "videogen.monthly_cap_usd": 5.0,
    "storage.auto_clean_days": 3,            # clear working files of finished videos after N days (0 = never)
    "storage.auto_delete_rejected_days": 0,  # delete rejected/cancelled videos after N days (0 = never)
    "billing.rates": {},                     # per-unit USD estimate rates overriding plans.DEFAULT_RATES
    "billing.provider": "",                  # e.g. "stripe" once keys are configured; empty = billing not live
    "auth.allow_signup": True,               # new creators can register their own workspace
    "auth.require_approval": True,           # new sign-ups wait until the superadmin approves them
    "site.name": "AI Video Studio",          # public website name
    "site.contact_email": "",                # shown on the public pages (privacy/terms/contact) when set
    "ads.enabled": False,                    # show the ad slot to Free-plan users (superadmin sets the code)
    "ads.html": "",                          # ad unit HTML from the ad network (e.g. Google AdSense)
    "transcribe.model": "small",             # faster-whisper model: tiny | base | small | medium
    "compliance.owner_override": None,       # {active, accepted_at, accepted_by, statement}: see finance.gate.owner_override
    "production.test_mode": False,
    "production.max_active": 2,
    "production.short_seconds": 50,
    "production.long_minutes": 8,
    "production.min_topic_score": 35,
    "production.auto_pick_topics": True,
    "youtube.disclose_tts": True,
    # Compliance (finance content). Public release of NIFTY episodes stays off until a human enables it
    # after the regulatory position is reviewed; unattended publishing is a separate, second switch.
    "compliance.public_publishing_enabled": False,
    "compliance.unattended_publishing": False,
    "compliance.finance_lint_all": True,
}

SECRETS: dict[str, dict[str, str]] = {
    "ollama_api_key": {
        "label": "Ollama API key",
        "purpose": "Ollama Cloud models and Ollama's web search / web fetch API (research evidence).",
        "url": "https://ollama.com/settings/keys",
    },
    "sarvam_api_key": {
        "label": "Sarvam AI API key",
        "purpose": "Natural Hindi / Hinglish narration with Sarvam Bulbul (paid per character, about ₹30 per 10,000 characters).",
        "url": "https://dashboard.sarvam.ai",
    },
    "anthropic_api_key": {
        "label": "Claude API key (Anthropic)",
        "purpose": "Optional high-quality research synthesis and scriptwriting. Billed per token, separately from Claude Pro/Max.",
        "url": "https://platform.claude.com/settings/keys",
    },
    "cloudflare_api_token": {
        "label": "Cloudflare Workers AI token",
        "purpose": "Free daily AI images (FLUX.2 klein): photo-real scene images when no stock footage fits, and "
                   "kids' cartoon art. Needs the account ID under Settings → Visuals.",
        "url": "https://developers.cloudflare.com/workers-ai/get-started/rest-api/",
    },
    "gemini_api_key": {
        "label": "Gemini API key",
        "purpose": "Optional alternative text model and optional image generation. Free-tier limits and terms vary.",
        "url": "https://aistudio.google.com/apikey",
    },
    "youtube_api_key": {
        "label": "YouTube Data API key",
        "purpose": "Public competitor / demand data (search, view counts, subscriber counts). Free quota, no billing.",
        "url": "https://console.cloud.google.com/apis/library/youtube.googleapis.com",
    },
    "pexels_api_key": {
        "label": "Pexels API key",
        "purpose": "Free, properly licensed stock photos and video clips.",
        "url": "https://www.pexels.com/api/",
    },
    "pixabay_api_key": {
        "label": "Pixabay API key",
        "purpose": "Free, properly licensed stock photos and video clips.",
        "url": "https://pixabay.com/api/docs/",
    },
    "meta_app_secret": {
        "label": "Meta (Facebook) app secret",
        "purpose": "Lets channels connect their Facebook Page and Instagram account to publish videos and Reels.",
        "url": "https://developers.facebook.com/apps/",
    },
    "google_oauth_client_secret": {
        "label": "Google OAuth client secret",
        "purpose": "Lets the app upload to YOUR channel after you authorize it in Google's own consent screen.",
        "url": "https://console.cloud.google.com/apis/credentials",
    },
    "google_refresh_token": {
        "label": "YouTube authorization (refresh token)",
        "purpose": "Stored after you connect your channel. Delete it to disconnect.",
        "url": "https://myaccount.google.com/permissions",
    },
}

_cache: dict[str, Any] = {}
_lock = threading.Lock()


def get(key: str, default: Any = None) -> Any:
    with _lock:
        if key in _cache:
            return copy.deepcopy(_cache[key])
    row = db.q1("SELECT value FROM settings WHERE key=?", (key,))
    if row is not None:
        value = db.loads(row["value"])
    elif key in DEFAULTS:
        value = copy.deepcopy(DEFAULTS[key])
    else:
        return default
    with _lock:
        _cache[key] = value
    return copy.deepcopy(value)


def set(key: str, value: Any) -> None:  # noqa: A001 - mirrors get()
    db.ex(
        "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, db.dumps(value), db.iso()),
    )
    with _lock:
        _cache[key] = copy.deepcopy(value)


def merge(key: str, patch: dict[str, Any]) -> dict[str, Any]:
    current = get(key) or {}
    current.update(patch)
    set(key, current)
    return current


def all_settings() -> dict[str, Any]:
    out = {k: get(k) for k in DEFAULTS}
    for row in db.q("SELECT key FROM settings"):
        out.setdefault(row["key"], get(row["key"]))
    return out


# ---------------------------------------------------------------- secrets

_secret_cache: dict[str, str | None] = {}


def secret(name: str) -> str | None:
    if not known_secret(name):
        raise KeyError(name)
    with _lock:
        if name in _secret_cache:
            return _secret_cache[name]
    try:
        value = keyring.get_password(config.KEYRING_SERVICE, name)
    except KeyringError:
        value = None
    with _lock:
        _secret_cache[name] = value
    return value


def set_secret(name: str, value: str) -> None:
    if not known_secret(name):
        raise KeyError(name)
    value = value.strip()
    if not value:
        raise ValueError("empty secret")
    keyring.set_password(config.KEYRING_SERVICE, name, value)
    with _lock:
        _secret_cache[name] = value


def delete_secret(name: str) -> None:
    if not known_secret(name):
        raise KeyError(name)
    try:
        keyring.delete_password(config.KEYRING_SERVICE, name)
    except PasswordDeleteError:
        pass
    with _lock:
        _secret_cache[name] = None


def known_secret(name: str) -> bool:
    """Registered secrets, plus one YouTube authorization and one Facebook Page token per studio channel."""
    return name in SECRETS or bool(re.fullmatch(r"(google_refresh_token|meta_page_token)__ch\d+", name))


def has_secret(name: str) -> bool:
    return bool(secret(name))


def secret_status() -> list[dict[str, Any]]:
    out = []
    for name, meta in SECRETS.items():
        value = secret(name)
        out.append({
            "name": name, **meta,
            "set": bool(value),
            "hint": ("…" + value[-4:]) if value and len(value) > 8 else ("set" if value else ""),
        })
    return out
