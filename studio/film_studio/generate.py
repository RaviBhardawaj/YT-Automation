"""Phase 6 — shot generation. Works through the film's shot list one shot at a time, in the background.

  * a key picture per shot setup (free FLUX, from the approved portraits + location; shots that share speaker,
    framing and scene share it, so a 185-shot film needs roughly 100 pictures);
  * real motion (free ZeroGPU Wan 2.2) for the most important shots first — physical actions, then the opening —
    within the daily free GPU quota; every other shot is a still with a camera move, *labelled* that way;
  * every attempt is a take (failures too); a shot that already has a good take is never redone;
  * after a restart, pause or quota wait it continues at the next unfinished shot.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import db, logs, settings
from . import providers, store, zerogpu

KEY_PRIORITY = {"insert": 0, "establishing": 1, "reaction": 2, "close_up": 3, "wide": 4, "over_shoulder": 5,
                "medium": 6, "cutaway": 7}
TYPE_LABEL = {"establishing": "wide establishing shot", "wide": "wide shot", "medium": "medium shot",
              "close_up": "close-up", "over_shoulder": "over-the-shoulder shot", "reaction": "reaction close-up",
              "insert": "close-up insert shot of hands and props", "cutaway": "cutaway shot"}


def setup_key(shot: dict[str, Any]) -> str:
    """Shots with the same framing, people and scene share one key picture."""
    t, sc, chars = shot["shot_type"], shot["scene_no"], tuple(sorted(shot["characters"]))
    if t == "insert":
        return f"s{sc:02d}_insert_{shot['shot_no']:03d}"
    if t in ("establishing", "wide") or (t == "reaction" and len(chars) > 1):
        return f"s{sc:02d}_wide"
    if t == "reaction":
        return f"s{sc:02d}_close_up_{'-'.join(chars)}"
    return f"s{sc:02d}_{t}_{'-'.join(chars) or 'set'}"


def _who(film: dict[str, Any], keys: list[str]) -> str:
    by = {c["key"]: c for c in film.get("cast") or []}
    parts = []
    for k in keys:
        c = by.get(k)
        if c:
            parts.append(f"{c['name']} ({(c.get('look') or '')[:140]})")
    return "; ".join(parts)


def keyframe_prompt(film: dict[str, Any], shot: dict[str, Any]) -> str:
    place = re.sub(r"(?i)^the entire film takes place primarily in one\s*", "", film.get("location") or "")[:200]
    who = _who(film, shot["characters"])
    angle = shot["camera"].get("angle", "eye level")
    what = shot["action"] or (shot["dialogue"][0]["text"] if shot["dialogue"] else "")
    speaking = f"{shot['dialogue'][0]['name']} is speaking, natural expression. " if shot["dialogue"] else ""
    return (f"Cinematic {TYPE_LABEL.get(shot['shot_type'], 'shot')}, {angle}, film still from a live-action Indian "
            f"village comedy. {('People: ' + who + '. ') if who else ''}{speaking}{what[:220]}. Setting: {place}. "
            f"{shot['lighting']}. Real people, real skin texture, shallow depth of field, 35mm")


def motion_prompt(shot: dict[str, Any]) -> str:
    ph = (shot["camera"] or {}).get("phases") or {}
    move = shot["camera"].get("movement", "static")
    action = ph.get("middle") or shot["action"] or "natural small movements, breathing, blinking"
    return (f"{action}. Natural, realistic human motion, steady {move} camera, consistent faces and clothes, "
            "no new people, no text")[:900]


def _refs(film: dict[str, Any], shot: dict[str, Any]) -> list[Path]:
    by = {c["key"]: c for c in film.get("cast") or []}
    refs = [Path(by[k]["portrait"]) for k in shot["characters"] if k in by and by[k].get("portrait")][:3]
    if film.get("location_ref"):
        refs.append(Path(film["location_ref"]))
    return [r for r in refs if r.exists()][:4]


def wants_video(shot: dict[str, Any]) -> bool:
    mode = settings.get("film_studio.video_shots") or "key"
    if mode == "none":
        return False
    if mode == "all":
        return True
    return shot["shot_type"] in ("insert", "establishing", "reaction")


def run(p: dict[str, Any], gen_image, work: Path) -> dict[str, Any]:
    """Make takes for every unfinished shot. gen_image(prompt, out, refs=, size=) is the free image function."""
    from .. import pipeline
    ws, pid, film = p["workspace_id"], p["id"], p["package"]
    keys_dir, clips_dir = work / "keys", work / "clips_video"
    keys_dir.mkdir(parents=True, exist_ok=True)
    clips_dir.mkdir(parents=True, exist_ok=True)
    max_retry = int(settings.get("film_studio.max_retries_per_shot") or 2)
    budget = int(settings.get("film_studio.video_clips_per_run") or 0)
    video_ok = True
    try:
        vpick = providers.choose("video_i2v")
        video_ok = vpick["motion_type"] == "video"
    except providers.NoFreeProvider:
        video_ok = False
    all_shots = store.shots(pid, ws)
    todo = [s for s in all_shots if not s.get("current_take") and s["status"] not in ("approved",)]
    upgrades = [s for s in all_shots if s.get("current_take") and s["continuity"].get("upgrade_requested")
                and s["motion_type"] not in ("video", "dialogue_video", "lipsync_verified")]
    # the shots most worth real motion first, then film order
    todo.sort(key=lambda s: (0 if wants_video(s) else 1, KEY_PRIORITY.get(s["shot_type"], 9), s["scene_no"],
                             s["shot_no"]))
    made = {"keyframes": 0, "video": 0, "camera_move": 0, "failed": 0, "upgraded": 0}
    for shot in upgrades:                                    # real motion for shots that only have a camera move
        if not (video_ok and budget > 0):
            break
        key = Path((store.takes(shot["id"], ws)[-1].get("settings") and
                    db.loads(store.takes(shot["id"], ws)[-1]["settings"], {}).get("keyframe")) or "")
        if not key.exists():
            continue
        out = clips_dir / f"shot_{shot['id']:05d}_up.mp4"
        try:
            providers.guard("hf_zerogpu")
            info = zerogpu.generate(key, motion_prompt(shot), out, seconds=min(5.0, max(2.0, shot["target_seconds"])))
            store.add_take(shot["id"], ws, provider="hf_zerogpu", motion_type="video", file_path=str(out),
                           model=info["space"], prompt=motion_prompt(shot), settings={"keyframe": str(key), **info})
            budget -= 1
            made["upgraded"] += 1
            cont = dict(store.shot(shot["id"], ws)["continuity"])
            cont.pop("upgrade_requested", None)
            cont.pop("video_wanted", None)
            store.set_shot(shot["id"], ws, continuity=cont)
        except zerogpu.QuotaExceeded:
            break
        except Exception as e:  # noqa: BLE001 - the shot keeps its camera-move take
            store.add_take(shot["id"], ws, provider="hf_zerogpu", motion_type="video", file_path=None,
                           prompt=motion_prompt(shot), status="failed", error=str(e)[:300])
            store.set_shot(shot["id"], ws, status="ready")
    for shot in todo:
        if pipeline.load(pid)["status"] in ("paused", "rejected"):
            break                                            # paused/cancelled: stop between shots
        failed = sum(1 for t in store.takes(shot["id"], ws) if t["status"] == "failed")
        if failed > max_retry:
            store.set_shot(shot["id"], ws, status="needs_review")
            continue
        store.set_shot(shot["id"], ws, status="generating")
        retry = int(shot["continuity"].get("qa_retry") or 0)    # QA asked for a fresh picture: its own key
        key = keys_dir / (f"{setup_key(shot)}.png" if not retry else f"shot{shot['id']:05d}_r{retry}.png")
        if not key.exists():
            prompt = keyframe_prompt(film, shot)
            if retry:
                prompt = "Show exactly this, faithfully: " + prompt
            gen_image(prompt, key, refs=_refs(film, shot), size=(1024, 576))
            made["keyframes"] += 1
        note = ""
        if wants_video(shot) and video_ok and budget > 0:
            out = clips_dir / f"shot_{shot['id']:05d}.mp4"
            try:
                providers.guard("hf_zerogpu")
                info = zerogpu.generate(key, motion_prompt(shot), out,
                                        seconds=min(5.0, max(2.0, shot["target_seconds"])))
                store.add_take(shot["id"], ws, provider="hf_zerogpu", motion_type="video", file_path=str(out),
                               model=info["space"], prompt=motion_prompt(shot), settings={"keyframe": str(key), **info})
                budget -= 1
                made["video"] += 1
                continue
            except zerogpu.QuotaExceeded as e:
                if (settings.get("film_studio.video_shots") or "key") == "all":
                    store.set_shot(shot["id"], ws, status="waiting")
                    raise                                  # 'all': wait for the quota reset, then continue
                video_ok, note = False, "real video wanted — today's free GPU minutes are used up; upgrade later"
                logs.event("info", "film", str(e), production_id=pid)
            except (zerogpu.SpaceError, providers.PaidBlocked, Exception) as e:  # noqa: BLE001 - record any failure
                store.add_take(shot["id"], ws, provider="hf_zerogpu", motion_type="video", file_path=None,
                               prompt=motion_prompt(shot), status="failed", error=str(e)[:300])
                made["failed"] += 1
                note = f"real video failed ({str(e)[:80]}); still with camera move used"
        elif wants_video(shot):
            note = "real video wanted — no free video provider set up; still with camera move used"
        store.add_take(shot["id"], ws, provider="ffmpeg_camera", motion_type="camera_move", file_path=str(key),
                       model="flux keyframe + zoompan", prompt=keyframe_prompt(film, shot),
                       settings={"keyframe": str(key), "note": note, "movement": shot["camera"].get("movement")})
        made["camera_move"] += 1
        if note:
            cont = dict(shot["continuity"])
            cont["video_wanted"] = note
            store.set_shot(shot["id"], ws, continuity=cont)
    left = [s for s in store.shots(pid, ws) if not s.get("current_take")]
    return {**made, "left": len(left), "summary": store.summary(pid, ws)}


def upgrade_to_video(shot_id: int, ws: int) -> None:
    """Ask for real motion on a shot that got a camera move (its current take stays until a video take succeeds)."""
    s = store.shot(shot_id, ws)
    cont = dict(s["continuity"])
    cont["upgrade_requested"] = db.iso()
    store.set_shot(shot_id, ws, continuity=cont, status="queued")
