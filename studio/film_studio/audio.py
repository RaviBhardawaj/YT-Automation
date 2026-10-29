"""Phase 7 — dialogue and sound. Every recorded line is attached to the shot that shows its speaker; the shot's
length becomes the *measured* voice length plus a natural gap; speaker identity is checked against the script;
lip-sync is reported honestly (stills cannot lip-sync; AI video lip-sync is unverified)."""
from __future__ import annotations

from typing import Any

from . import store

GAP = 0.35


def lipsync_status(motion_type: str) -> str:
    if motion_type == "lipsync_verified":
        return "verified"
    if motion_type in ("video", "dialogue_video"):
        return "unverified — AI video, mouth movement not checked against the words"
    return "not supported — still image with camera move (no mouth movement)"


def sync(production_id: int, ws: int, film: dict[str, Any]) -> dict[str, Any]:
    """Attach recorded lines to their shots; fix shot lengths from the real audio. Returns a report."""
    scenes = film.get("scenes") or []
    voices = {c["key"]: c.get("voice") or {} for c in film.get("cast") or []}
    warnings, attached, missing = [], 0, 0
    for sh in store.shots(production_id, ws):
        if not sh["dialogue"]:
            continue
        d = dict(sh["dialogue"][0])
        try:
            ln = scenes[sh["scene_no"] - 1]["lines"][d["line_index"]]
        except (IndexError, KeyError):
            warnings.append(f"shot {sh['scene_no']}.{sh['shot_no']}: its line is no longer in the script")
            continue
        if ln["speaker"] != d["speaker"]:
            warnings.append(f"shot {sh['scene_no']}.{sh['shot_no']}: line belongs to {ln['speaker']}, "
                            f"shot says {d['speaker']}")
        if d["speaker"] not in sh["characters"]:
            warnings.append(f"shot {sh['scene_no']}.{sh['shot_no']}: speaker not in the shot")
        if not ln.get("wav"):
            missing += 1
            continue
        d.update({"wav": ln["wav"], "seconds": ln["seconds"], "voice": voices.get(d["speaker"]),
                  "text_spoken": ln.get("tts") or ln["text"]})
        cont = dict(sh["continuity"])
        cont["audio"] = {**(cont.get("audio") or {}), "dialogue": True,
                         "lipsync": lipsync_status(sh["motion_type"])}
        store.set_shot(sh["id"], ws, dialogue=[d], target_seconds=round(ln["seconds"] + GAP, 3), continuity=cont)
        attached += 1
    return {"attached": attached, "missing_audio": missing, "warnings": warnings}
