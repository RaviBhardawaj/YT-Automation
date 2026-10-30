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

_pending: dict[str, tuple[str, float, int | None]] = {}
_tokens: dict[int | None, dict[str, Any]] = {}          # access-token cache per studio channel
_lock = threading.Lock()


def _cid(channel_id: int | None = None) -> int | None:
    """The studio channel whose YouTube authorization to use (explicit, else the one being worked on, else #1)."""
    from .. import channels
    return channel_id or channels.current_id() or 1


def _secret(channel_id: int | None = None) -> str:
    from .. import channels
    return channels.refresh_token_secret(_cid(channel_id))


def _cache(channel_id: int | None = None) -> dict[str, Any]:
    return _tokens.setdefault(_cid(channel_id), {"access": None, "expires": 0.0})


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


def connected(channel_id: int | None = None) -> bool:
    return settings.has_secret(_secret(channel_id))


def auth_url(channel_id: int | None = None) -> str:
    if not configured():
        raise AuthError("Add your OAuth client first.")
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    now = time.time()
    with _lock:
        for k in [k for k, v in _pending.items() if now - v[1] > 900]:
            _pending.pop(k, None)
        _pending[state] = (verifier, now, _cid(channel_id))
    scopes = SCOPES + ([SCOPE_CAPTIONS] if settings.get("youtube.upload_captions") else [])
    return AUTH_URL + "?" + urlencode({
        "client_id": settings.get("youtube.client_id"), "redirect_uri": REDIRECT_URI, "response_type": "code",
        "scope": " ".join(scopes), "access_type": "offline", "prompt": "select_account consent", "include_granted_scopes": "true",
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
    cid = entry[2]
    old = settings.secret(_secret(cid))
    settings.set_secret(_secret(cid), tok["refresh_token"])
    settings.set("youtube.granted_scopes", tok.get("scope", "").split())
    with _lock:
        _cache(cid).update(access=tok["access_token"], expires=time.time() + int(tok.get("expires_in", 3600)) - 60)
    try:
        channel = fetch_channel(cid)
    except AuthError:                         # wrong channel picked: discard the new login, keep the previous one
        # (not revoked at Google: revoking can also cut off another studio channel that uses that Google identity)
        if old:
            settings.set_secret(_secret(cid), old)
        else:
            settings.delete_secret(_secret(cid))
        with _lock:
            _cache(cid).update(access=None, expires=0)
        raise
    logs.event("info", "youtube", f"Studio channel #{cid} connected to YouTube channel: {channel.get('title')}")
    return {**channel, "studio_channel_id": cid}


def access_token(channel_id: int | None = None) -> str:
    cache = _cache(channel_id)
    with _lock:
        if cache["access"] and time.time() < cache["expires"]:
            return cache["access"]
    refresh = settings.secret(_secret(channel_id))
    if not refresh:
        raise AuthError("YouTube is not connected.")
    r = httpx.post(TOKEN_URL, data={"client_id": settings.get("youtube.client_id"),
                                    "client_secret": settings.secret("google_oauth_client_secret"),
                                    "refresh_token": refresh, "grant_type": "refresh_token"}, timeout=30)
    if r.status_code != 200:
        err = r.json().get("error", "") if r.headers.get("content-type", "").startswith("application/json") else ""
        if err == "invalid_grant":
            settings.delete_secret(_secret(channel_id))
            raise AuthError("YouTube authorization was revoked or expired. Please connect again.")
        raise AuthError(f"Could not refresh YouTube access ({r.status_code}).")
    tok = r.json()
    with _lock:
        cache.update(access=tok["access_token"], expires=time.time() + int(tok.get("expires_in", 3600)) - 60)
        return cache["access"]


def request(method: str, url: str, channel_id: int | None = None, **kwargs: Any) -> httpx.Response:
    headers = kwargs.pop("headers", {})
    timeout = kwargs.pop("timeout", 60)
    for attempt in range(2):
        r = httpx.request(method, url, headers={**headers, "Authorization": f"Bearer {access_token(channel_id)}"},
                          timeout=timeout, **kwargs)
        if r.status_code == 401 and attempt == 0:
            with _lock:
                _cache(channel_id)["access"] = None
            continue
        return r
    return r


def fetch_channel(channel_id: int | None = None) -> dict[str, Any]:
    cid = _cid(channel_id)
    r = request("GET", "https://www.googleapis.com/youtube/v3/channels", channel_id=cid,
                params={"part": "snippet,statistics,status", "mine": "true"})
    items = r.json().get("items", []) if r.status_code == 200 else []
    if not items:
        raise AuthError("This Google account has no YouTube channel yet. Create one at youtube.com, then connect.")
    c = items[0]
    channel = {"id": c["id"], "title": c["snippet"]["title"], "url": f"https://www.youtube.com/channel/{c['id']}",
               "subscribers": c.get("statistics", {}).get("subscriberCount"),
               "videos": c.get("statistics", {}).get("videoCount"), "fetched_at": db.iso()}
    from .. import channels as chmod
    studio = chmod.get(cid) or {}
    expected = studio.get("youtube_expected_id")
    if expected and expected != c["id"]:
        raise AuthError(f"You signed in to “{c['snippet']['title']}”, but this studio channel is locked to "
                        f"{studio.get('youtube_handle') or expected} (“{studio.get('youtube_title')}”). Click Connect "
                        f"again and choose that channel in Google's window.")
    st = c.get("status", {})
    readiness = {"long_uploads": st.get("longUploadsStatus"), "privacy": st.get("privacyStatus"),
                 "linked": st.get("isLinked"), "checked_at": db.iso()}
    if cid == 1:
        settings.set("youtube.channel", channel)
    db.ex("UPDATE channels SET youtube_channel_id=?, youtube_title=?, youtube_status=?, updated_at=? WHERE id=?",
          (channel["id"], channel["title"], db.dumps(readiness), db.iso(), cid))
    return channel


def verify_destination(channel_id: int) -> dict[str, Any]:
    """Before any upload: the authorized account must be the YouTube channel this studio channel is linked to."""
    from .. import channels
    ch = channels.get(channel_id)
    if not ch:
        raise AuthError(f"studio channel #{channel_id} not found")
    if not connected(channel_id):
        raise AuthError(f"'{ch['name']}' has no YouTube authorization — connect it on the Channels page.")
    r = request("GET", "https://www.googleapis.com/youtube/v3/channels", channel_id=channel_id,
                params={"part": "snippet", "mine": "true"})
    items = r.json().get("items", []) if r.status_code == 200 else []
    if not items:
        raise AuthError(f"could not read the YouTube channel for '{ch['name']}' (HTTP {r.status_code})")
    actual = items[0]["id"]
    if ch.get("youtube_expected_id") and actual != ch["youtube_expected_id"]:
        raise AuthError(f"authorized account is YouTube channel {actual}, but '{ch['name']}' is locked to "
                        f"{ch.get('youtube_handle') or ch['youtube_expected_id']} — refusing to upload to the wrong channel")
    if ch.get("youtube_channel_id") and actual != ch["youtube_channel_id"]:
        raise AuthError(f"authorized account is YouTube channel {actual}, but '{ch['name']}' is linked to "
                        f"{ch['youtube_channel_id']} — refusing to upload to the wrong channel")
    return {"id": actual, "title": items[0]["snippet"]["title"]}


def disconnect(channel_id: int | None = None) -> None:
    cid = _cid(channel_id)
    token = settings.secret(_secret(cid))
    if token:
        try:
            httpx.post(REVOKE_URL, params={"token": token}, timeout=15)
        except httpx.HTTPError:
            pass
    settings.delete_secret(_secret(cid))
    if cid == 1:
        settings.set("youtube.channel", {})
    db.ex("UPDATE channels SET youtube_channel_id=NULL, youtube_title=NULL, updated_at=? WHERE id=?", (db.iso(), cid))
    with _lock:
        _cache(cid).update(access=None, expires=0.0)
