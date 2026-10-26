"""Facebook Pages and Instagram (professional accounts) through Meta's official Graph API.

One Meta app for the whole studio (app ID in settings, app secret in the Keychain). Each studio channel signs in
with Facebook Login and picks ONE Facebook Page; the Instagram professional account linked to that Page comes
with it. The Page access token (long-lived, derived from a 60-day user token) is stored per channel in the
Keychain. Uploads are direct (resumable) uploads from this computer — no public video URL is needed.

  Shorts (9:16, 3–90 s)      → Facebook Reels + Instagram Reels
  Longer videos              → Facebook Page video (+ Instagram Reel when it is within Instagram's limits)
"""
from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from .. import config, db, logs, settings

SCOPES = ["pages_show_list", "pages_read_engagement", "pages_manage_posts", "instagram_basic",
          "instagram_content_publish", "business_management"]
REDIRECT_URI = f"{config.BASE_URL}/oauth/meta/callback"
IG_MAX_SECONDS, IG_MAX_BYTES = 15 * 60, 300 * 1024 * 1024
FB_REEL_MAX_SECONDS = 90

_lock = threading.Lock()
_pending: dict[str, tuple[int, float]] = {}                 # oauth state -> (channel id, time)
_choices: dict[int, tuple[list[dict[str, Any]], float]] = {}  # channel id -> (pages to choose from, time)


class MetaError(Exception):
    pass


def _v() -> str:
    return settings.get("meta.graph_version") or "v26.0"


def graph(path: str = "") -> str:
    return f"https://graph.facebook.com/{_v()}/{path.lstrip('/')}"


def token_secret(channel_id: int) -> str:
    return f"meta_page_token__ch{channel_id}"


def configured() -> bool:
    return bool(settings.get("meta.app_id")) and settings.has_secret("meta_app_secret")


def connected(channel_id: int) -> bool:
    from .. import channels
    ch = channels.get(channel_id) or {}
    return bool((ch.get("meta") or {}).get("page_id")) and settings.has_secret(token_secret(channel_id))


def _check(r: httpx.Response) -> dict[str, Any]:
    try:
        j = r.json()
    except ValueError:
        j = {}
    if r.status_code >= 400 or (isinstance(j, dict) and j.get("error")):
        err = (j.get("error") or {}) if isinstance(j, dict) else {}
        raise MetaError(f"Meta: {err.get('message') or r.text[:200]} (HTTP {r.status_code}"
                        + (f", code {err.get('code')}" if err.get("code") else "") + ")")
    return j


# ------------------------------------------------------------------ sign-in

def auth_url(channel_id: int) -> str:
    if not configured():
        raise MetaError("The platform admin must add the Meta app ID and secret first (Settings → Facebook & Instagram).")
    state = secrets.token_urlsafe(24)
    with _lock:
        for k in [k for k, (_, t) in _pending.items() if time.time() - t > 900]:
            _pending.pop(k, None)
        _pending[state] = (channel_id, time.time())
    params = {"client_id": settings.get("meta.app_id"), "redirect_uri": REDIRECT_URI, "state": state,
              "response_type": "code"}
    if settings.get("meta.config_id"):                     # Facebook Login for Business configuration
        params["config_id"] = settings.get("meta.config_id")
    else:
        params["scope"] = ",".join(SCOPES)
    return f"https://www.facebook.com/{_v()}/dialog/oauth?" + urlencode(params)


def handle_callback(code: str, state: str) -> int:
    """Exchange the code, then list the Pages this person manages. Returns the channel id (choice comes next)."""
    with _lock:
        entry = _pending.pop(state, None)
    if not entry:
        raise MetaError("This sign-in expired or didn't start here — click Connect again.")
    channel_id = entry[0]
    app_id, app_secret = settings.get("meta.app_id"), settings.secret("meta_app_secret")
    short = _check(httpx.get(graph("oauth/access_token"), params={
        "client_id": app_id, "client_secret": app_secret, "redirect_uri": REDIRECT_URI, "code": code}, timeout=30))
    long = _check(httpx.get(graph("oauth/access_token"), params={
        "grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": app_secret,
        "fb_exchange_token": short["access_token"]}, timeout=30))
    pages = _check(httpx.get(graph("me/accounts"), params={
        "access_token": long["access_token"], "limit": 100,
        "fields": "id,name,access_token,tasks,instagram_business_account{id,username,profile_picture_url}"},
        timeout=30)).get("data", [])
    usable = [p for p in pages if p.get("access_token") and "CREATE_CONTENT" in (p.get("tasks") or ["CREATE_CONTENT"])]
    if not usable:
        raise MetaError("No Facebook Page found that you can post to. Create a Page (or ask its admin for full "
                        "control), then connect again.")
    with _lock:
        _choices[channel_id] = ([{"id": p["id"], "name": p["name"], "token": p["access_token"],
                                  "ig": p.get("instagram_business_account") or {}} for p in usable], time.time())
    logs.event("info", "social", f"Channel #{channel_id}: Facebook sign-in done, {len(usable)} Page(s) to choose from")
    return channel_id


def pending_pages(channel_id: int) -> list[dict[str, Any]]:
    """Pages waiting to be chosen (without their tokens), valid for 15 minutes after sign-in."""
    with _lock:
        entry = _choices.get(channel_id)
    if not entry or time.time() - entry[1] > 900:
        return []
    return [{"id": p["id"], "name": p["name"], "ig_username": p["ig"].get("username"), "ig_id": p["ig"].get("id")}
            for p in entry[0]]


def choose_page(channel_id: int, page_id: str) -> dict[str, Any]:
    from .. import channels
    with _lock:
        entry = _choices.get(channel_id)
    page = next((p for p in (entry[0] if entry else []) if p["id"] == str(page_id)), None)
    if not page or time.time() - entry[1] > 900:
        raise MetaError("That choice expired — click Connect again.")
    settings.set_secret(token_secret(channel_id), page["token"])
    ch = channels.get(channel_id)
    meta = {**(ch.get("meta") or {}), "page_id": page["id"], "page_name": page["name"],
            "ig_user_id": page["ig"].get("id"), "ig_username": page["ig"].get("username"), "connected_at": db.iso()}
    db.update("channels", channel_id, {"meta": db.dumps(meta), "updated_at": db.iso()})
    with _lock:
        _choices.pop(channel_id, None)
    logs.event("info", "social", f"Channel #{channel_id} connected to Facebook Page '{page['name']}'"
               + (f" and Instagram @{page['ig'].get('username')}" if page["ig"].get("id") else " (no Instagram linked)"))
    return meta


def disconnect(channel_id: int) -> None:
    from .. import channels
    settings.delete_secret(token_secret(channel_id))
    ch = channels.get(channel_id)
    meta = {k: v for k, v in (ch.get("meta") or {}).items() if k in ("auto_facebook", "auto_instagram")}
    db.update("channels", channel_id, {"meta": db.dumps(meta), "updated_at": db.iso()})


def verify(channel_id: int) -> dict[str, Any]:
    """The stored Page token still works and still points at the linked Page (and its Instagram account)."""
    from .. import channels
    meta = (channels.get(channel_id) or {}).get("meta") or {}
    tok = settings.secret(token_secret(channel_id))
    if not tok or not meta.get("page_id"):
        raise MetaError("Facebook isn't connected for this channel.")
    me = _check(httpx.get(graph("me"), params={"access_token": tok, "fields": "id,name,instagram_business_account{id,username}"},
                          timeout=30))
    if me.get("id") != meta["page_id"]:
        raise MetaError("The saved login belongs to a different Page — connect again.")
    ig = me.get("instagram_business_account") or {}
    return {"page": me.get("name"), "instagram": ig.get("username")}


# ------------------------------------------------------------------ uploads

def _rupload(url: str, token: str, path: Path, header_name: str = "offset") -> dict[str, Any]:
    size = path.stat().st_size
    with open(path, "rb") as f:
        r = httpx.post(url, headers={"Authorization": f"OAuth {token}", header_name: "0", "file_size": str(size)},
                       content=f.read(), timeout=httpx.Timeout(1800, connect=30))
    return _check(r)


def publish_instagram(ig_user_id: str, token: str, video: Path, caption: str, row: dict[str, Any],
                      save_container) -> dict[str, Any]:
    """Instagram Reel: container → direct upload → wait until FINISHED → publish. Resumes from a saved container."""
    container = row.get("container_id")
    if not container:
        c = _check(httpx.post(graph(f"{ig_user_id}/media"), data={
            "media_type": "REELS", "upload_type": "resumable", "caption": caption[:2200], "share_to_feed": "true",
            "access_token": token}, timeout=60))
        container = c["id"]
        save_container(container)
        _rupload(f"https://rupload.facebook.com/ig-api-upload/{_v()}/{container}", token, video)
    for _ in range(30):                                     # Instagram processes the video (usually < 2 min)
        st = _check(httpx.get(graph(container), params={"fields": "status_code,status", "access_token": token},
                              timeout=30))
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code == "PUBLISHED":
            return {"id": row.get("remote_id") or container, "already": True}
        if code in ("ERROR", "EXPIRED"):
            save_container(None)
            raise MetaError(f"Instagram couldn't process the video ({st.get('status') or code})")
        time.sleep(10)
    else:
        raise MetaError("Instagram is still processing the video — it will be retried")
    pub = _check(httpx.post(graph(f"{ig_user_id}/media_publish"), data={"creation_id": container,
                                                                        "access_token": token}, timeout=60))
    link = _check(httpx.get(graph(pub["id"]), params={"fields": "permalink", "access_token": token}, timeout=30))
    return {"id": pub["id"], "url": link.get("permalink")}


def publish_facebook_reel(page_id: str, token: str, video: Path, caption: str, ai: bool) -> dict[str, Any]:
    start = _check(httpx.post(graph(f"{page_id}/video_reels"), data={"upload_phase": "start", "access_token": token},
                              timeout=60))
    vid = start["video_id"]
    _rupload(start.get("upload_url") or f"https://rupload.facebook.com/video-upload/{_v()}/{vid}", token, video)
    _check(httpx.post(graph(f"{page_id}/video_reels"), data={
        "upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED", "description": caption[:2200],
        "is_ai_generated": "true" if ai else "false", "access_token": token}, timeout=60))
    return {"id": vid, "url": f"https://www.facebook.com/reel/{vid}"}


def publish_facebook_video(page_id: str, token: str, video: Path, title: str, caption: str) -> dict[str, Any]:
    app_id = settings.get("meta.app_id")
    size = video.stat().st_size
    sess = _check(httpx.post(graph(f"{app_id}/uploads"), params={
        "file_name": video.name, "file_length": size, "file_type": "video/mp4", "access_token": token}, timeout=60))
    handle = _rupload(graph(sess["id"]), token, video, header_name="file_offset")["h"]
    r = _check(httpx.post(f"https://graph-video.facebook.com/{_v()}/{page_id}/videos", data={
        "title": title[:250], "description": caption[:5000], "fbuploader_video_file_chunk": handle,
        "access_token": token}, timeout=300))
    return {"id": r["id"], "url": f"https://www.facebook.com/{page_id}/videos/{r['id']}"}
