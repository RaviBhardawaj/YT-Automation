"""Phase 6 — shot generation. Works through the film's shot list one shot at a time, in the background.

  * a key picture per shot setup (free FLUX, from the approved portraits + location; shots that share speaker,
    framing and scene share it); which provider made it and which reference pictures it used is recorded;
  * real motion for the shots most worth it — physical actions, then each scene's opening, then inserts and
    reactions ('key'), or every shot ('all') — within the free daily clip limit, executed by exactly the provider
    the registry chose;
  * every other shot is a still with a camera move, *labelled* that way, with the reason recorded;
  * every attempt is a take (failures too); a shot that already has a good take is never redone; a clip that was
    downloaded just before a restart is checked and reused instead of being generated again;
  * after a restart, pause or quota wait it continues at the next unfinished shot.
"""
from __future__ import annotations

import json
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
OUTDOOR = re.compile(r"(?i)courtyard|field|farm|road|village|street|garden|outside|outdoor|yard|market|river|tree|sky")


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


def _names(film: dict[str, Any], keys: list[str]) -> str:
    by = {c["key"]: c["name"] for c in film.get("cast") or []}
    return ", ".join(by.get(k, k) for k in keys)


def motion_prompt(shot: dict[str, Any], film: dict[str, Any] | None = None) -> str:
    """A short, scene-specific image-to-video prompt: what moves (beginning → middle → end), the environment, the
    camera only when the shot calls for it, and what must stay the same. No generic stock adjectives."""
    film = film or {}
    cam = shot.get("camera") or {}
    ph = cam.get("phases") or {}
    who = _names(film, shot.get("characters") or []) or "the people in frame"
    action = (shot.get("action") or "").strip().rstrip(".")
    t = shot.get("shot_type")
    if ph.get("start") or ph.get("end"):
        subject = (f"{ph.get('start') or action or 'The shot begins'}; then {ph.get('middle') or action}; it ends with "
                   f"{ph.get('end') or 'the movement completed and the pose held'}.")
    elif action:
        subject = (f"{who}: {action} — one continuous movement with a clear start, the action itself, and a settled "
                   "finish.")
    elif shot.get("dialogue"):
        d = shot["dialogue"][0]
        subject = (f"{d.get('name') or who} speaks with small, restrained hand gestures and natural head movement; "
                   "the listeners react with subtle expressions.")
    elif t == "reaction":
        subject = f"{who} reacts: eyes widen, then a slow change of expression, then a held look."
    else:
        subject = f"{who} make small natural movements — breathing, blinking, a shift of weight."
    if t == "insert":
        subject += " Close on the hands: fingers grip and move the object with real weight and contact."
    place = f"{film.get('location', '')} {shot.get('lighting', '')}"
    env = ("A light breeze moves leaves, cloth and dust slightly." if OUTDOOR.search(place) else
           "The room stays still apart from the people.")
    move = (cam.get("movement") or "static").replace("_", " ")
    camera = ("Camera locked off, stable framing." if move in ("static", "", "locked") else
              f"Camera: a slow {move}, smooth, no shake.")
    return (f"{subject} {env} {camera} Keep the same faces, hair, clothes, body proportions, props and place as in "
            "the first frame; no new people or objects, no cut to another scene; realistic weight, balance and "
            "shadows.")[:900]


def _refs(film: dict[str, Any], shot: dict[str, Any]) -> list[Path]:
    by = {c["key"]: c for c in film.get("cast") or []}
    refs = [Path(by[k]["portrait"]) for k in shot["characters"] if k in by and by[k].get("portrait")][:3]
    if film.get("location_ref"):
        refs.append(Path(film["location_ref"]))
    return [r for r in refs if r.exists()][:4]


def _physical(shot: dict[str, Any]) -> bool:
    """A shot whose point is an action (not just talking)."""
    return bool((shot.get("action") or "").strip()) and not shot.get("dialogue")


def video_priority(shot: dict[str, Any]) -> int:
    """Lower = more worth real motion: physical actions, the film's opening, each scene's opening, inserts,
    reactions, establishing shots; everything else last."""
    if _physical(shot):
        return 0
    if shot["scene_no"] == 1 and shot["shot_no"] == 1:
        return 1
    if shot["shot_no"] == 1:
        return 2
    return 3 + KEY_PRIORITY.get(shot["shot_type"], 9)


def wants_video(shot: dict[str, Any]) -> bool:
    mode = providers.video_mode()
    if mode == "none":
        return False
    if mode == "all":
        return True
    return video_priority(shot) <= 3 + KEY_PRIORITY["reaction"] or shot["shot_type"] == "establishing"


def _key_meta(key: Path) -> dict[str, Any]:
    """What made a key picture and which reference pictures it used (written by the image step)."""
    side = key.with_suffix(".json")
    try:
        return json.loads(side.read_text()) if side.exists() else {}
    except ValueError:
        return {}


def _resume_clip(out: Path) -> dict[str, Any] | None:
    """A clip downloaded before a restart that was never recorded: reuse it if it is a valid video."""
    if not out.exists():
        return None
    meta = zerogpu.inspect(out)
    if zerogpu.valid(meta):
        out.unlink(missing_ok=True)
        return None
    return meta


def _video_take(shot: dict[str, Any], ws: int, key: Path, out: Path, pick: dict[str, Any],
                film: dict[str, Any]) -> int:
    """Real motion for one shot with exactly the chosen provider; records the take with what really happened."""
    from . import qa
    prompt = motion_prompt(shot, film)
    seconds = min(zerogpu.MAX_SECONDS, max(2.0, float(shot["target_seconds"])))
    resumed = _resume_clip(out)
    if resumed:
        info = {"provider": pick["provider"], "model": "resumed clip", "video": resumed, "resumed": True,
                "start_image": str(key), "refs_supported": False}
    else:
        info = providers.run_video(pick["provider"], key, prompt, out, seconds=seconds)
    motion = qa.motion_stats(out)
    settings_ = {"keyframe": str(key), "start_image": str(key), "key_made_by": _key_meta(key),
                 "requested_seconds": seconds, "motion_check": motion,
                 **{k: v for k, v in info.items() if k not in ("file", "provider", "model")}}
    tid = store.add_take(shot["id"], ws, provider=info["provider"], motion_type="video", file_path=str(out),
                         model=info["model"], prompt=prompt, settings=settings_)
    cont = dict(store.shot(shot["id"], ws)["continuity"])
    for k in ("upgrade_requested", "video_wanted", "video_error"):
        cont.pop(k, None)
    if motion.get("static"):
        cont["motion_flag"] = "the generated clip is nearly static — review it, or retry the shot"
    store.set_shot(shot["id"], ws, continuity=cont)
    return tid


def _note_failure(shot: dict[str, Any], ws: int, pick: dict[str, Any], err: Exception, film: dict[str, Any]) -> str:
    kind = getattr(err, "kind", type(err).__name__)
    store.add_take(shot["id"], ws, provider=pick["provider"], motion_type="video", file_path=None,
                   prompt=motion_prompt(shot, film), status="failed", error=f"[{kind}] {str(err)[:280]}",
                   settings={"error_kind": kind})
    return kind


def run(p: dict[str, Any], gen_image, work: Path) -> dict[str, Any]:
    """Make takes for every unfinished shot. gen_image(prompt, out, refs=, size=) is the free image function."""
    from .. import pipeline
    ws, pid, film = p["workspace_id"], p["id"], p["package"]
    keys_dir, clips_dir = work / "keys", work / "clips_video"
    keys_dir.mkdir(parents=True, exist_ok=True)
    clips_dir.mkdir(parents=True, exist_ok=True)
    max_retry = int(settings.get("film_studio.max_retries_per_shot") or 2)
    mode = providers.video_mode()
    stop_reason = ""                                          # why real video stopped for the rest of this run
    try:
        pick = providers.choose("video_i2v")
        if pick["motion_type"] != "video":
            stop_reason = pick.get("limitation") or "no free video provider is available"
    except providers.NoFreeProvider as e:
        pick, stop_reason = {"provider": "", "motion_type": "none"}, str(e)

    def can_video() -> bool:
        nonlocal stop_reason
        if stop_reason:
            return False
        if providers.video_budget()["left"] <= 0:
            stop_reason = "today's real-video clip limit is reached (Settings → Film Studio)"
            return False
        return True

    all_shots = store.shots(pid, ws)
    todo = [s for s in all_shots if not s.get("current_take") and s["status"] not in ("approved",)]
    upgrades = [s for s in all_shots if s.get("current_take") and s["continuity"].get("upgrade_requested")
                and s["motion_type"] not in ("video", "dialogue_video", "lipsync_verified")]
    todo.sort(key=lambda s: (0 if wants_video(s) else 1, video_priority(s), s["scene_no"], s["shot_no"]))
    upgrades.sort(key=video_priority)
    made = {"keyframes": 0, "video": 0, "camera_move": 0, "failed": 0, "upgraded": 0, "resumed": 0}

    for shot in upgrades:                                    # real motion for shots that only have a camera move
        if not can_video():
            break
        cur = next((t for t in store.takes(shot["id"], ws) if t["id"] == shot["current_take"]), None)
        key = Path((db.loads((cur or {}).get("settings"), {}) or {}).get("keyframe") or "")
        if not key.exists():
            continue
        try:
            _video_take(shot, ws, key, clips_dir / f"shot_{shot['id']:05d}_up.mp4", pick, film)
            made["upgraded"] += 1
        except zerogpu.QuotaExceeded as e:
            stop_reason = str(e)
            break
        except Exception as e:  # noqa: BLE001 - recorded; the shot keeps its camera-move take
            kind = _note_failure(shot, ws, pick, e, film)
            store.set_shot(shot["id"], ws, status="ready")
            if kind in ("auth", "interface", "not_configured"):
                stop_reason = str(e)

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
        if wants_video(shot):
            if can_video():
                out = clips_dir / f"shot_{shot['id']:05d}.mp4"
                try:
                    _video_take(shot, ws, key, out, pick, film)
                    made["video"] += 1
                    continue
                except zerogpu.QuotaExceeded as e:
                    if mode == "all":
                        store.set_shot(shot["id"], ws, status="waiting")
                        raise                                  # 'all': wait for the quota reset, then continue
                    stop_reason = str(e)
                    logs.event("info", "film", str(e), production_id=pid)
                    note = "real video wanted — today's free GPU minutes are used up; use 'Make it video' later"
                except Exception as e:  # noqa: BLE001 - every failure is recorded with its kind
                    kind = _note_failure(shot, ws, pick, e, film)
                    made["failed"] += 1
                    if kind in ("auth", "interface", "not_configured"):
                        stop_reason = str(e)
                    note = f"real video failed ({kind}: {str(e)[:90]}); still with camera move used — retry later"
            else:
                if mode == "all" and "limit" not in stop_reason and "used up" in stop_reason:
                    store.set_shot(shot["id"], ws, status="waiting")
                    raise zerogpu.QuotaExceeded(stop_reason)
                note = f"real video wanted — {stop_reason}; still with camera move used"
        store.add_take(shot["id"], ws, provider="ffmpeg_camera", motion_type="camera_move", file_path=str(key),
                       model="flux keyframe + zoompan", prompt=keyframe_prompt(film, shot),
                       settings={"keyframe": str(key), "key_made_by": _key_meta(key), "note": note,
                                 "video_requested": wants_video(shot), "fallback_reason": note or None,
                                 "movement": shot["camera"].get("movement")})
        made["camera_move"] += 1
        if note:
            cont = dict(shot["continuity"])
            cont["video_wanted"] = note
            store.set_shot(shot["id"], ws, continuity=cont)
    left = [s for s in store.shots(pid, ws) if not s.get("current_take")]
    return {**made, "left": len(left), "summary": store.summary(pid, ws), "video_stopped": stop_reason or None}


def upgrade_to_video(shot_id: int, ws: int) -> None:
    """Ask for real motion on a shot that got a camera move (its current take stays until a video take succeeds)."""
    s = store.shot(shot_id, ws)
    cont = dict(s["continuity"])
    cont["upgrade_requested"] = db.iso()
    store.set_shot(shot_id, ws, continuity=cont, status="queued")
