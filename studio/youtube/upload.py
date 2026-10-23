"""Resumable video upload + thumbnail + captions via the YouTube Data API v3."""
from __future__ import annotations

import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any

from .. import db, logs, profile, settings
from . import oauth

log = logging.getLogger(__name__)
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
CHUNK = 8 * 1024 * 1024        # multiple of 256 KiB, as the API requires


class UploadError(Exception):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def _reason(r: Any) -> str:
    try:
        err = r.json().get("error", {})
        reasons = ",".join(e.get("reason", "") for e in err.get("errors", []))
        return f"{err.get('message', '')} [{reasons}]"
    except ValueError:
        return r.text[:300]


def privacy_for(production: dict[str, Any]) -> dict[str, Any]:
    """Decide visibility. Public/scheduled visibility needs: your publish mode allows it AND
    you confirmed in Settings that your Google Cloud project passed the YouTube API audit
    (unaudited projects' uploads are locked private by YouTube)."""
    requested = production.get("privacy") or settings.get("youtube.privacy_default")
    audit_ok = bool(settings.get("youtube.audit_passed"))
    status: dict[str, Any] = {"privacyStatus": "private"}
    note = "uploaded as private"
    if requested in ("public", "scheduled", "unlisted") and audit_ok:
        if requested == "unlisted":
            status["privacyStatus"], note = "unlisted", "uploaded as unlisted"
        elif production.get("publish_at") and requested == "scheduled":
            status["publishAt"] = production["publish_at"]
            note = f"scheduled to go public at {production['publish_at']} (UTC)"
        else:
            status["privacyStatus"], note = "public", "published publicly"
    elif requested != "private" and not audit_ok:
        note = ("uploaded as PRIVATE: confirm in Settings › YouTube that your API project passed Google's audit "
                "before public publishing can be enabled")
    return {"status": status, "note": note}


def upload(production: dict[str, Any], pkg: dict[str, Any], assets: list[dict[str, Any]]) -> dict[str, Any]:
    video = Path(production["video_path"])
    total = video.stat().st_size
    lang = profile.tts_language()
    priv = privacy_for(production)
    body = {
        "snippet": {
            "title": pkg["title"][:100], "description": (pkg.get("final_description") or pkg["description"])[:4900],
            "tags": pkg.get("tags", []), "categoryId": str(settings.get("youtube.category_id")),
            "defaultLanguage": lang, "defaultAudioLanguage": lang,
        },
        "status": {**priv["status"], "selfDeclaredMadeForKids": bool(profile.made_for_kids()),
                   "containsSyntheticMedia": any(a.get("synthetic") for a in assets),
                   "embeddable": True, "license": "youtube"},
    }
    session = production.get("youtube_upload_uri")
    offset = 0
    if session:
        r = oauth.request("PUT", session, headers={"Content-Length": "0", "Content-Range": f"bytes */{total}"})
        if r.status_code in (200, 201):
            return _finish(production, r.json(), pkg, priv)
        if r.status_code == 308:
            rng = r.headers.get("Range")
            offset = int(rng.split("-")[1]) + 1 if rng else 0
        else:
            session = None
    if not session:
        r = oauth.request("POST", UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                          json=body, headers={"X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(total)})
        if r.status_code == 403 and "quota" in r.text.lower():
            raise UploadError(f"YouTube API quota exhausted: {_reason(r)}", retry_after=6 * 3600)
        if r.status_code == 400 and "uploadLimitExceeded" in r.text:
            raise UploadError("YouTube daily upload limit reached for this channel", retry_after=12 * 3600)
        if r.status_code >= 400:
            raise UploadError(f"could not start upload: HTTP {r.status_code} {_reason(r)}")
        session = r.headers["Location"]
        db.update("productions", production["id"], {"youtube_upload_uri": session})
    with open(video, "rb") as fh:
        retries = 0
        while offset < total:
            fh.seek(offset)
            data = fh.read(CHUNK)
            end = offset + len(data) - 1
            try:
                r = oauth.request("PUT", session, content=data, timeout=600,
                                  headers={"Content-Length": str(len(data)), "Content-Range": f"bytes {offset}-{end}/{total}"})
            except Exception as e:  # network hiccup: resume from the server's offset
                retries += 1
                if retries > 6:
                    raise UploadError(f"upload interrupted: {e}", retry_after=300) from e
                time.sleep(min(60, 2 ** retries))
                r = oauth.request("PUT", session, headers={"Content-Length": "0", "Content-Range": f"bytes */{total}"})
            if r.status_code in (200, 201):
                return _finish(production, r.json(), pkg, priv)
            if r.status_code == 308:
                rng = r.headers.get("Range")
                offset = int(rng.split("-")[1]) + 1 if rng else 0
                retries = 0
                continue
            if r.status_code in (500, 502, 503, 504):
                retries += 1
                if retries > 6:
                    raise UploadError(f"YouTube server errors during upload ({r.status_code})", retry_after=600)
                time.sleep(min(60, 2 ** retries))
                continue
            if r.status_code == 404:
                db.update("productions", production["id"], {"youtube_upload_uri": None})
                raise UploadError("upload session expired; will restart", retry_after=60)
            raise UploadError(f"upload failed: HTTP {r.status_code} {_reason(r)}")
    raise UploadError("upload ended without a response")


def _finish(production: dict[str, Any], video: dict[str, Any], pkg: dict[str, Any], priv: dict[str, Any]) -> dict[str, Any]:
    vid = video["id"]
    notes = [priv["note"]]
    got = (video.get("status") or {}).get("privacyStatus")
    if priv["status"].get("privacyStatus") == "public" and got and got != "public":
        notes.append(f"YouTube set this upload to {got.upper()} (API projects that have not passed Google's audit are "
                     "locked private) — make it public in YouTube Studio")
        logs.event("warning", "youtube", f"Requested public, YouTube applied '{got}' to https://youtu.be/{vid}",
                   production_id=production["id"])
    db.update("productions", production["id"], {"youtube_video_id": vid, "youtube_upload_uri": None,
                                                "uploaded_at": db.iso()})
    if production.get("thumbnail_path") and Path(production["thumbnail_path"]).exists():
        r = oauth.request("POST", "https://www.googleapis.com/upload/youtube/v3/thumbnails/set",
                          params={"videoId": vid, "uploadType": "media"},
                          content=Path(production["thumbnail_path"]).read_bytes(), headers={"Content-Type": "image/jpeg"})
        notes.append("custom thumbnail set" if r.status_code == 200 else
                     f"thumbnail not set ({_reason(r)}) — custom thumbnails need a phone-verified channel")
    if settings.get("youtube.upload_captions") and production.get("srt_path") and \
            oauth.SCOPE_CAPTIONS in (settings.get("youtube.granted_scopes") or []):
        notes.append(_upload_captions(vid, Path(production["srt_path"])))
    logs.event("info", "youtube", f"Uploaded '{pkg['title']}' → https://youtu.be/{vid} ({'; '.join(notes)})",
               production_id=production["id"])
    return {"video_id": vid, "url": f"https://youtu.be/{vid}", "notes": notes, "status": video.get("status", {})}


def _upload_captions(video_id: str, srt: Path) -> str:
    boundary = uuid.uuid4().hex
    meta = db.dumps({"snippet": {"videoId": video_id, "language": profile.tts_language(), "name": "", "isDraft": False}})
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
            f"--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n").encode() + srt.read_bytes() + \
        f"\r\n--{boundary}--".encode()
    r = oauth.request("POST", "https://www.googleapis.com/upload/youtube/v3/captions",
                      params={"part": "snippet", "uploadType": "multipart"}, content=body,
                      headers={"Content-Type": f"multipart/related; boundary={boundary}"})
    return "captions uploaded" if r.status_code in (200, 201) else f"captions not uploaded ({_reason(r)})"


def video_status(video_id: str) -> dict[str, Any]:
    r = oauth.request("GET", "https://www.googleapis.com/youtube/v3/videos",
                      params={"part": "status,processingDetails", "id": video_id})
    items = r.json().get("items", []) if r.status_code == 200 else []
    return items[0] if items else {}


def safe_tag(t: str) -> str:
    return re.sub(r"[<>]", "", t)[:30]
