"""Screenplay → shot list. Rule-based (free, predictable): every scene gets a master, every line a shot of its
speaker, every physical action an insert planned as beginning → middle → end, every laugh a reaction.

Timing is an estimate from the spoken words, pauses and actions — never from word count alone — and is replaced by
the measured voice length once the dialogue is recorded. The plan is compared with the requested runtime; a short
film is reported as missing coverage, never stretched.
"""
from __future__ import annotations

import re
from typing import Any

from . import store

WORDS_PER_SECOND = 2.5                     # conversational Hindi/Hinglish at normal speed
SENTENCE_PAUSE, LINE_GAP = 0.25, 0.35
PHYSICAL = re.compile(r"\b(places?|puts?|picks? up|shuffles?|deals?|counts?|sips?|coughs?|kicks?|pulls? out|reaches|"
                      r"grabs?|shakes?|pins?|covers?|bends?|slips?|lands?|wobbles?|catches|hides?|compares?|claps?|"
                      r"peel(?:s|ing)|repair(?:s|ing)|adjusts?|gathers?|lifts?|stands? up|walks?|blocks?|dusts?|"
                      r"moves?|throws?|holds?|finds?|looks underneath)\b", re.I)
LAUGH = re.compile(r"\blaugh", re.I)
REACT = re.compile(r"\b(looks? (confused|up|around|innocent|in horror)|smiles?|stares?|freezes?|groans?|sweating|"
                   r"pauses?|turn(s)? toward|winces?|nods?)\b", re.I)
AMBIENT = re.compile(r"\b(tractor passes|birds? chirp|wind|gust|in the distance|ambience)\b", re.I)
CAMERA_NOTE = re.compile(r"\b(camera|shot|angle|close-?ups?|fade (in|out)|pulls? back)\b", re.I)
PROPS = ("playing cards", "spare deck", "deck", "cards", "card", "table", "chai", "tea glass", "glass", "kettle",
         "coins", "charpai", "tractor", "newspaper", "phone", "sugarcane", "pillar", "jacket", "sandals", "saucer")


def speech_seconds(text: str) -> float:
    words = len(re.findall(r"\w+", text or ""))
    sentences = max(1, len(re.findall(r"[.!?।]+", text or "")))
    return round(max(1.0, words / WORDS_PER_SECOND) + (sentences - 1) * SENTENCE_PAUSE, 2)


def _props(text: str) -> list[str]:
    low, out = (text or "").lower(), []
    for p in PROPS:
        if p in low and not any(p in o for o in out):
            out.append(p)
    return out[:5]


def _who(text: str, cast: list[dict[str, Any]], last: str | None) -> list[str]:
    """Characters an action mentions; 'he/his' means the last character mentioned in the scene."""
    found = [c["key"] for c in cast if re.search(rf"\b{re.escape(c['name'])}\b", text, re.I)]
    if not found and last and re.search(r"\b(he|his|him)\b", text, re.I):
        found = [last]
    return found


def _phases(text: str) -> dict[str, str]:
    """Beginning, middle and end of a physical action, so the shot has a complete movement."""
    t = text.rstrip(".")
    return {"begin": f"Set-up: hands and body in position just before — {t[:120]}",
            "middle": t[:200],
            "end": "Hold on the result for a beat; hands settle or withdraw naturally"}


def _lighting(film: dict[str, Any], scene: dict[str, Any], si: int, n: int) -> str:
    text = f"{scene.get('action', '')} {scene.get('heading', '')}".lower()
    if "evening" in text or (si == n - 1 and "evening" in (film.get("lighting") or "").lower()):
        return "warm golden evening light"
    return "natural afternoon sunlight, soft shadows"


def plan(film: dict[str, Any]) -> dict[str, Any]:
    """Build the shot list (pure: no database). Returns {shots, runtime, target, coverage}."""
    cast = film.get("cast") or []
    names = {c["key"]: c["name"] for c in cast}
    scenes = film.get("scenes") or []
    shots: list[dict[str, Any]] = []
    for si, s in enumerate(scenes):
        lines = s.get("lines") or []
        present = list(dict.fromkeys([ln["speaker"] for ln in lines] +
                                     [k for k in names if re.search(rf"\b{re.escape(names[k])}\b", s.get("action", ""), re.I)]))
        light = _lighting(film, s, si, len(scenes))
        beats = []                                    # one beat per sentence: each physical action gets its own shot
        for b in s.get("beats") or [{"kind": "line", "line": i} for i in range(len(lines))]:
            if b["kind"] == "action":
                beats += [{"kind": "action", "text": t} for t in re.split(r"(?<=[.!?])\s+", b["text"]) if t.strip()]
            else:
                beats.append(b)
        n_shot = 0
        pending_camera: list[str] = []
        ambient = [b["text"] for b in beats if b["kind"] == "action" and AMBIENT.search(b["text"])]

        def add(**kw: Any) -> dict[str, Any]:
            nonlocal n_shot
            n_shot += 1
            shot = {"scene_no": si + 1, "shot_no": n_shot, "lighting": light, "camera": {"angle": "eye level",
                    "movement": "static"}, "characters": [], "dialogue": [], "props": [], "action": "",
                    "continuity": {}, **kw}
            if pending_camera:
                shot["camera"]["notes"] = " ".join(pending_camera)[:300]
                if re.search(r"\blow (camera )?angle\b", shot["camera"]["notes"], re.I):
                    shot["camera"]["angle"] = "low angle"
                pending_camera.clear()
            shots.append(shot)
            return shot

        opening: list[str] = []                       # descriptive sentences before the first line set the master
        for b in beats:
            if b["kind"] != "action":
                break
            if not PHYSICAL.search(b["text"]) and not LAUGH.search(b["text"]) and not REACT.search(b["text"]):
                opening.append(b["text"])
        first_actions = " ".join(opening)[:300]
        add(shot_type="establishing" if si == 0 else "wide", target_seconds=4.0 if si == 0 else 2.5,
            characters=present, action=first_actions, props=_props(first_actions),
            camera={"angle": "eye level", "movement": "slow push-in toward the table" if si == 0 else "static"},
            audio={"ambience": ambient[:3]})
        last_subject: str | None = None
        prev_speaker: str | None = None
        exchange = 0
        for bi, b in enumerate(beats):
            if b["kind"] == "action":
                text = b["text"]
                if text in opening and not CAMERA_NOTE.search(text):
                    who = _who(text, cast, last_subject)
                    last_subject = (who or [last_subject])[-1]
                    continue                                  # already covered by the master shot
                who = _who(text, cast, last_subject)
                if who:
                    last_subject = who[-1]
                if CAMERA_NOTE.search(text) and not PHYSICAL.search(text):
                    prev_shot = shots[-1] if shots and shots[-1]["scene_no"] == si + 1 else None
                    if prev_shot and prev_shot["shot_type"] in ("insert", "reaction") and \
                            re.search(r"\b(briefly|this|the hidden|show)\b", text, re.I):
                        prev_shot["camera"]["notes"] = text[:300]          # direction about the shot just before
                        if re.search(r"\blow (camera )?angle\b", text, re.I):
                            prev_shot["camera"]["angle"] = "low angle"
                        continue
                    if re.search(r"^fade (out|to black)", text.strip(), re.I) and prev_shot:
                        prev_shot["continuity"]["transition_out"] = "fade to black"   # a transition, not a shot
                        continue
                    if re.search(r"pulls? back", text, re.I):
                        add(shot_type="wide", target_seconds=4.0, characters=present, action=text,
                            camera={"angle": "eye level", "movement": "slow pull back to reveal the courtyard"})
                        continue
                    pending_camera.append(text)
                    continue
                if LAUGH.search(text):
                    add(shot_type="reaction", target_seconds=1.6, characters=present, action=text,
                        camera={"angle": "eye level", "movement": "static"})
                    continue
                if PHYSICAL.search(text):
                    add(shot_type="insert", target_seconds=2.5, characters=who, action=text, props=_props(text),
                        phases=_phases(text))
                    continue
                if REACT.search(text) and who:
                    add(shot_type="reaction", target_seconds=1.5, characters=who, action=text)
                    continue
                if AMBIENT.search(text):
                    continue                                  # heard, not seen
                add(shot_type="medium", target_seconds=2.0, characters=who or present, action=text,
                    props=_props(text))
                continue
            ln = lines[b["line"]]
            spk = ln["speaker"]
            nxt = beats[bi + 1] if bi + 1 < len(beats) else None
            punch = ln["text"].rstrip().endswith("!") or bool(nxt and nxt["kind"] == "action" and LAUGH.search(nxt["text"]))
            exchange = exchange + 1 if prev_speaker and prev_speaker != spk else 0
            if punch:
                kind, move = "close_up", "slow push-in"
            elif exchange >= 2 and exchange % 2 == 0 and prev_speaker:
                kind, move = "over_shoulder", "static"
            else:
                kind, move = "medium", "static"
            shot = add(shot_type=kind, target_seconds=round(speech_seconds(ln["text"]) + LINE_GAP, 2),
                       characters=[spk] + ([prev_speaker] if kind == "over_shoulder" and prev_speaker else []),
                       action=(ln.get("action") or "")[-160:], props=_props(ln.get("action") or ""),
                       dialogue=[{"speaker": spk, "name": names.get(spk, spk), "text": ln["text"],
                                  "line_index": b["line"], "manner": ln.get("manner", "")}],
                       camera={"angle": "eye level", "movement": move,
                               **({"over_shoulder_of": prev_speaker} if kind == "over_shoulder" else {})})
            shot["voice"] = spk
            last_subject, prev_speaker = spk, spk
    for i, sh in enumerate(shots):                            # continuity links and notes
        prev, nxt = (shots[i - 1] if i else None), (shots[i + 1] if i + 1 < len(shots) else None)
        sh["continuity"] = {**sh.get("continuity", {}),
                            "prev": f"{prev['scene_no']}.{prev['shot_no']}" if prev else None,
                            "next": f"{nxt['scene_no']}.{nxt['shot_no']}" if nxt else None,
                            "notes": "keep the seating, props and light of the scene master" +
                                     ("; new scene" if prev and prev["scene_no"] != sh["scene_no"] else "")}
    runtime = round(sum(sh["target_seconds"] for sh in shots), 1)
    target = film.get("target_seconds")
    coverage = None
    if target:
        gap = round(target - runtime, 1)
        coverage = {"target": target, "planned": runtime, "gap": gap,
                    "message": (f"Planned {runtime / 60:.1f} min of the requested {target / 60:.1f} min — "
                                f"{gap / 60:.1f} min more is needed. The script's dialogue and actions don't fill the "
                                "runtime; add scenes or action, or accept a shorter film. Nothing is stretched.")
                    if gap > target * 0.1 else "Planned runtime matches the request."}
    counts: dict[str, int] = {}
    for sh in shots:
        counts[sh["shot_type"]] = counts.get(sh["shot_type"], 0) + 1
    return {"shots": shots, "runtime": runtime, "counts": counts, "coverage": coverage}


def save(production_id: int, ws: int, planned: dict[str, Any], *, replace: bool = False) -> int:
    """Write the plan. Never replaces shots that already have takes; a re-plan replaces only untouched shots."""
    existing = store.shots(production_id, ws)
    if existing and not replace:
        return len(existing)
    from .. import db
    if any(s.get("current_take") for s in existing) or \
            db.q1("SELECT 1 FROM film_takes WHERE production_id=? LIMIT 1", (production_id,)):
        raise ValueError("Shots already have takes — re-plan individual scenes instead of the whole film.")
    db.ex("DELETE FROM film_shots WHERE production_id=? AND workspace_id=? AND status='planned'", (production_id, ws))
    for sh in planned["shots"]:
        fields = {k: sh[k] for k in ("shot_type", "target_seconds", "camera", "characters", "action", "dialogue",
                                     "props", "lighting", "continuity") if k in sh}
        extra = {k: sh[k] for k in ("phases", "audio", "voice") if k in sh}
        if extra:
            fields["camera"] = {**fields.get("camera", {}), **({"phases": extra["phases"]} if "phases" in extra else {})}
            fields["continuity"] = {**fields.get("continuity", {}),
                                    **({"audio": extra["audio"]} if "audio" in extra else {}),
                                    **({"voice": extra["voice"]} if "voice" in extra else {})}
        store.add_shot(production_id, ws, sh["scene_no"], sh["shot_no"], **fields)
    return len(planned["shots"])
