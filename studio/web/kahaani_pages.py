"""KahaaniVerse section (in its channel's settings tab): the character library (every reference sheet, voice and identity), the genre mix, the story
backlog with its scores, and the buttons that research new stories or start today's film."""
from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import channels, db, worker
from ..creative import director, kahaani
from .app import actor, app, bad, body, is_admin, media_url, ok


def _channel() -> dict | None:
    return next((c for c in channels.all_channels() if c["workflow"] == "cinematic_film"), None)


def section(ch: dict) -> dict | None:
    """What the channel settings tab shows for a KahaaniVerse channel (None for any other channel)."""
    if not ch or ch.get("workflow") != "cinematic_film":
        return None
    lib = kahaani.library(ch["id"])
    for c in lib:
        c["pictures"] = [(s, media_url(c["refs"].get(s)), (c["refs_meta"].get(s) or {}).get("provider"))
                         for s in kahaani.PICTURES]
    films = db.q("SELECT id, title, status, stage, created_at FROM productions WHERE channel_id=? AND kind='film' "
                 "ORDER BY id DESC LIMIT 10", (ch["id"],))
    return {"lib": lib, "mix": kahaani.mix(ch), "genres": kahaani.GENRES, "backlog": director.backlog(ch["id"])[:20],
            "films": films, "tagline": kahaani.TAGLINE}


@app.get("/kahaaniverse")
def kahaaniverse_page(request: Request):
    """Old address: the KahaaniVerse section lives in its channel's settings tab."""
    ch = _channel()
    if not ch:
        raise HTTPException(404)
    return RedirectResponse(f"/channels/{ch['id']}?tab=settings#kahaaniverse", status_code=303)


@app.post("/api/kahaaniverse/library")
async def api_kahaani_library(request: Request):
    ch = _channel()
    if not is_admin(request) or not ch:
        return bad("Not available.")
    worker.enqueue("kahaani_library", {"channel_id": ch["id"]}, dedupe=True)
    return ok(detail="Making the missing reference pictures in the background")


@app.post("/api/kahaaniverse/mix")
async def api_kahaani_mix(request: Request):
    ch = _channel()
    if not is_admin(request) or not ch:
        return bad("Not available.")
    data = await body(request)
    mix = {g: max(0, min(100, int(data.get(g) or 0))) for g in kahaani.GENRES}
    if sum(mix.values()) != 100:
        return bad(f"The shares add up to {sum(mix.values())}% — make them 100%.")
    branding = dict(ch.get("branding") or {})
    branding["kahaani"] = {**(branding.get("kahaani") or {}), "genre_mix": mix}
    channels.update(ch["id"], {"branding": branding}, actor=actor(request))
    return ok(detail="Genre mix saved")


@app.post("/api/kahaaniverse/film")
async def api_kahaani_film(request: Request):
    ch = _channel()
    if not is_admin(request) or not ch:
        return bad("Not available.")
    data = await body(request)
    try:
        pid = director.start_film(ch["id"], minutes=float(data.get("minutes") or ch.get("long_minutes") or 10),
                                  topic_id=int(data["topic_id"]) if data.get("topic_id") else None,
                                  actor=actor(request))
    except ValueError as e:
        return bad(str(e))
    return ok(detail=f"Film #{pid} started", redirect=f"/productions/{pid}")
