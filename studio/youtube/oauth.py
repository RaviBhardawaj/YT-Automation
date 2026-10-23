"""Google OAuth 2.0 for installed apps (loopback redirect + PKCE).

You sign in on Google's own page; this app only ever receives an authorization code,
which it exchanges for a refresh token stored in the macOS Keychain.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from .. import config, db, logs, settings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
SCOPE_CAPTIONS = "https://www.googleapis.com/auth/youtube.force-ssl"
REDIRECT_URI = f"{config.BASE_URL}/oauth/google/callback"

_pending: dict[str, tuple[str, float]] = {}
_token: dict[str, Any] = {"access": None, "expires": 0.0}
_lock = threading.Lock()


class AuthError(Exception):
    pass


def save_client(raw_json: str) -> dict[str, str]:
    try:
        data = json.loads(raw_json)
    except ValueError as e:
        raise AuthError("That file is not valid JSON.") from e
    block = data.get("installed") or data.get("web")
    if not block or not block.get("client_id") or not block.get("client_secret"):
        raise AuthError("Expected a Google OAuth client JSON (type 'Desktop app') with client_id and client_secret.")
    if "web" in data:
        raise AuthError("This is a 'Web application' client. Create an OAuth client of type 'Desktop app' instead.")
    settings.set("youtube.client_id", block["client_id"])
    settings.set_secret("google_oauth_client_secret", block["client_secret"])
    return {"client_id": block["client_id"]}


def configured() -> bool:
    return bool(settings.get("youtube.client_id")) and settings.has_secret("google_oauth_client_secret")


def connected() -> bool:
    return settings.has_secret("google_refresh_token")


def auth_url() -> str:
    if not configured():
        raise AuthError("Add your OAuth client first.")
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    now = time.time()
    with _lock:
        for k in [k for k, (_, t) in _pending.items() if now - t > 900]:
            _pending.pop(k, None)
        _pending[state] = (verifier, now)
    scopes = SCOPES + ([SCOPE_CAPTIONS] if settings.get("youtube.upload_captions") else [])
    return AUTH_URL + "?" + urlencode({
        "client_id": settings.get("youtube.client_id"), "redirect_uri": REDIRECT_URI, "response_type": "code",
        "scope": " ".join(scopes), "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })


def handle_callback(code: str, state: str) -> dict[str, Any]:
    with _lock:
        entry = _pending.pop(state, None)
    if not entry:
        raise AuthError("Authorization expired or did not start here. Please try Connect again.")
    r = httpx.post(TOKEN_URL, data={
        "client_id": settings.get("youtube.client_id"), "client_secret": settings.secret("google_oauth_client_secret"),
        "code": code, "code_verifier": entry[0], "grant_type": "authorization_code", "redirect_uri": REDIRECT_URI,
    }, timeout=30)
    if r.status_code != 200:
        raise AuthError(f"Google rejected the code: {r.json().get('error_description', r.text[:200])}")
    tok = r.json()
    if not tok.get("refresh_token"):
        raise AuthError("Google did not return a refresh token. Remove the app's access at "
                        "myaccount.google.com/permissions and connect again.")
    settings.set_secret("google_refresh_token", tok["refresh_token"])
    settings.set("youtube.granted_scopes", tok.get("scope", "").split())
    with _lock:
        _token.update(access=tok["access_token"], expires=time.time() + int(tok.get("expires_in", 3600)) - 60)
    channel = fetch_channel()
    logs.event("info", "youtube", f"Connected YouTube channel: {channel.get('title')}")
    return channel


def access_token() -> str:
    with _lock:
        if _token["access"] and time.time() < _token["expires"]:
            return _token["access"]
    refresh = settings.secret("google_refresh_token")
    if not refresh:
        raise AuthError("YouTube is not connected.")
    r = httpx.post(TOKEN_URL, data={"client_id": settings.get("youtube.client_id"),
                                    "client_secret": settings.secret("google_oauth_client_secret"),
                                    "refresh_token": refresh, "grant_type": "refresh_token"}, timeout=30)
    if r.status_code != 200:
        err = r.json().get("error", "") if r.headers.get("content-type", "").startswith("application/json") else ""
        if err == "invalid_grant":
            settings.delete_secret("google_refresh_token")
            raise AuthError("YouTube authorization was revoked or expired. Please connect again.")
        raise AuthError(f"Could not refresh YouTube access ({r.status_code}).")
    tok = r.json()
    with _lock:
        _token.update(access=tok["access_token"], expires=time.time() + int(tok.get("expires_in", 3600)) - 60)
        return _token["access"]


def request(method: str, url: str, **kwargs: Any) -> httpx.Response:
    headers = kwargs.pop("headers", {})
    for attempt in range(2):
        r = httpx.request(method, url, headers={**headers, "Authorization": f"Bearer {access_token()}"},
                          timeout=kwargs.pop("timeout", 60), **kwargs)
        if r.status_code == 401 and attempt == 0:
            with _lock:
                _token["access"] = None
            continue
        return r
    return r


def fetch_channel() -> dict[str, Any]:
    r = request("GET", "https://www.googleapis.com/youtube/v3/channels",
                params={"part": "snippet,statistics,status", "mine": "true"})
    items = r.json().get("items", []) if r.status_code == 200 else []
    if not items:
        raise AuthError("This Google account has no YouTube channel yet. Create one at youtube.com, then connect.")
    c = items[0]
    channel = {"id": c["id"], "title": c["snippet"]["title"], "url": f"https://www.youtube.com/channel/{c['id']}",
               "subscribers": c.get("statistics", {}).get("subscriberCount"),
               "videos": c.get("statistics", {}).get("videoCount"), "fetched_at": db.iso()}
    settings.set("youtube.channel", channel)
    return channel


def disconnect() -> None:
    token = settings.secret("google_refresh_token")
    if token:
        try:
            httpx.post(REVOKE_URL, params={"token": token}, timeout=15)
        except httpx.HTTPError:
            pass
    settings.delete_secret("google_refresh_token")
    settings.set("youtube.channel", {})
    with _lock:
        _token.update(access=None, expires=0.0)
