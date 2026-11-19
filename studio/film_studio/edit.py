"""Phase 8 — timeline assembly and export.

The timeline follows the shot list: a title card, every shot's current take, an end card. Nothing is looped or
slowed down to fill time: a video clip shorter than its shot plays at normal speed and the rest of the shot is a
gentle camera move on its key picture; a longer clip is trimmed. Every clip gets the same film grade; subtitles
are cinema style; dialogue sits on the shot that shows its speaker; music and village air sit underneath.

Exports (in the production's export folder): film.mp4, film.srt, timeline.json, shot_manifest.json,
metadata.json, licences.json — quality_report.json is added by QA.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from .. import config, db, logs
from ..media import render, visuals
from . import performance, providers, store

TITLE_SEC, END_SEC = 3.5, 4.5


def export_dir(pid: int) -> Path:
    from ..creative import film
    d = film.workdir(pid) / "export"
    d.mkdir(parents=True, exist_ok=True)
    return d


def timeline(p: dict[str, Any]) -> list[dict[str, Any]]:
    """Ordered clips with their source take and real motion type."""
    ws, pid = p["workspace_id"], p["id"]
    out: list[dict[str, Any]] = [{"kind": "title", "seconds": TITLE_SEC}]
    for sh in store.shots(pid, ws):
        take = db.q1("SELECT * FROM film_takes WHERE id=?", (sh["current_take"],)) if sh.get("current_take") else None
        settings_ = db.loads((take or {}).get("settings"), {}) or {}
        d = sh["dialogue"][0] if sh["dialogue"] else None
        out.append({"kind": "shot", "shot_id": sh["id"], "scene": sh["scene_no"], "shot": sh["shot_no"],
                    "shot_type": sh["shot_type"], "seconds": float(sh["target_seconds"]),
                    "motion_type": (take or {}).get("motion_type") or "missing",
                    "source": (take or {}).get("file_path"), "keyframe": settings_.get("keyframe"),
                    "provider": (take or {}).get("provider"),
                    "subtitle": d["text"] if d else None, "speech": (d or {}).get("seconds"),
                    "wav": (d or {}).get("wav"), "speaker": (d or {}).get("name"),
                    "speaker_key": (d or {}).get("speaker"), "characters": sh["characters"],
                    "emotion": performance.emotion_of((d or {}).get("manner", "")) if d else None,
                    "animation": (sh["camera"] or {}).get("animation"),
                    "transition_out": sh["continuity"].get("transition_out")})
    out.append({"kind": "end", "seconds": END_SEC})
    t = 0.0
    for c in out:
        c["start"] = round(t, 3)
        t += c["seconds"]
        c["end"] = round(t, 3)
    return out


def cache_key(c: dict[str, Any], film: dict[str, Any], size: tuple[int, int]) -> dict[str, Any]:
    """Everything that changes how one timeline clip looks: the shot's own fields, the identity (size + time) of
    its video and key picture — a re-made take at the same path counts as changed — the card text, frame size,
    encoder settings and the renderer version."""
    from .. import settings
    key = {k: c.get(k) for k in ("kind", "source", "keyframe", "seconds", "subtitle", "motion_type", "speech",
                                 "speaker", "transition_out")}
    key["source_id"] = render.file_id(c.get("source"))
    key["keyframe_id"] = render.file_id(c.get("keyframe"))
    if c.get("kind") in ("title", "end"):
        key["card"] = [film.get("title"), film.get("closing_line") if c["kind"] == "end" else None]
    key["size"] = list(size)
    key["encode"] = [settings.get("render.preset"), settings.get("render.crf")]
    key["renderer"] = render.RENDER_VERSION
    return key


def _clip_for(c: dict[str, Any], i: int, work: Path, film: dict[str, Any], seed: str) -> Path:
    from ..creative import film as filmmod
    w, h = config.frame_size("long")
    out = work / f"c{i:04d}.mp4"
    sig = work / f"c{i:04d}.json"
    key = cache_key(c, film, (w, h))
    if out.exists() and sig.exists() and json.loads(sig.read_text()) == key:
        return out                                           # nothing that changes the picture changed: reuse
    overlays = []
    if c.get("subtitle"):
        sp = work / f"sub{i:04d}.png"
        pos = filmmod._subtitle(c["subtitle"], w, h, sp)
        overlays.append({"path": sp, **pos, "start": 0.0, "end": min(c["seconds"], (c.get("speech") or c["seconds"]) + 0.15)})
    if c["kind"] in ("title", "end"):
        img = filmmod._card(film["title"], film.get("closing_line", "") if c["kind"] == "end" else "", w, h,
                            work / f"{c['kind']}.jpg")
        render.scene_clip(i, {"kind": "image", "local_path": str(img), "is_card": True}, c["seconds"], 0.0, [],
                          "long", work, out, seed, look=filmmod.current_look())
    elif c["motion_type"] in ("video", "dialogue_video", "lipsync_verified") and c.get("source") \
            and Path(c["source"]).exists():
        clip_len = float(visuals.probe_video(Path(c["source"])).get("duration") or 0)
        play = min(c["seconds"], clip_len) if clip_len > 0.3 else 0
        rest = round(c["seconds"] - play, 3)
        if play <= 0:
            return _still(c, i, work, overlays, out, seed)
        part_a = work / f"c{i:04d}_a.mp4"
        render.scene_clip(i, {"kind": "video", "local_path": c["source"]}, play, 0.0,
                          [o for o in overlays if o["start"] < play], "long", work, part_a, seed, look=filmmod.current_look())
        if rest > 0.2:
            # finish the shot from where the motion ended (its last frame) with a gentle camera move — never jump
            # back to the key picture the motion started from, and never stretch the clip
            part_b = work / f"c{i:04d}_b.mp4"
            end_pic = render.last_frame(Path(c["source"]), work / f"c{i:04d}_last.jpg")
            shifted = [{**o, "start": 0.0, "end": max(0.0, o["end"] - play)} for o in overlays if o["end"] > play]
            render.scene_clip(i, {"kind": "image", "local_path": str(end_pic)}, rest, 0.0, shifted, "long", work,
                              part_b, seed, look=filmmod.current_look())
            render.concat([part_a, part_b], out, w=w, h=h)
        else:
            part_a.replace(out)
    else:
        _still(c, i, work, overlays, out, seed)
    sig.write_text(json.dumps(key))
    return out


def _still(c, i, work, overlays, out, seed) -> Path:
    from ..creative import film as filmmod
    src = c.get("keyframe") or c.get("source")
    if not src or not Path(src).exists():
        raise FileNotFoundError(f"shot {c.get('scene')}.{c.get('shot')} has no picture")
    render.scene_clip(i, {"kind": "image", "local_path": src}, c["seconds"], 0.0, overlays, "long", work, out, seed,
                      look=filmmod.current_look())
    return out


def _ts(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def assemble(p: dict[str, Any]) -> dict[str, Any]:
    from ..creative import film as filmmod
    film, pid = p["package"], p["id"]
    work = filmmod.workdir(pid) / "edit"
    work.mkdir(exist_ok=True)
    tl = timeline(p)
    missing = [f"{c['scene']}.{c['shot']}" for c in tl if c["kind"] == "shot" and c["motion_type"] == "missing"]
    if missing:
        raise FileNotFoundError(f"{len(missing)} shot(s) have no take yet: {', '.join(missing[:8])}")
    # every timeline clip is independent (cached ones return at once): render several side by side
    clips = render.run_parallel([lambda c=c, i=i: _clip_for(c, i, work, film, f"film{pid}") for i, c in enumerate(tl)])
    total = tl[-1]["end"]
    video = work / "video.mp4"
    w, h = config.frame_size("long")
    joined = render.concat(clips, video, w=w, h=h)            # copy only when every clip's format matches
    # ONE clock for everything: dialogue, music, ambience and subtitles are all placed from the timeline's times
    sr = performance.SR
    track = np.zeros(int((total + 1) * sr), dtype=np.float64)
    speech: list[tuple[float, float]] = []
    for c in tl:
        if c.get("wav") and Path(c["wav"]).exists():
            a, a_sr = performance._read(c["wav"])
            if a_sr != sr:
                a = np.interp(np.linspace(0, len(a) - 1, int(len(a) * sr / a_sr)), np.arange(len(a)), a)
            at = int(c["start"] * sr)
            track[at:at + len(a)] += a[:max(0, len(track) - at)] / 32768
            speech.append((c["start"], c["start"] + len(a) / sr))
    dialogue = work / "dialogue.wav"
    performance._write(dialogue, track)
    plan = performance.music_plan(film, tl)
    music_wav = performance.render_music(plan, total + 1, speech, work / "music.wav")
    amb_wav, amb_used = performance.render_ambience(film, tl, total + 1, work / "ambience.wav")
    mixed = work / "mix.wav"
    mix_report = performance.mix(dialogue, music_wav, amb_wav, mixed)
    exp = export_dir(pid)
    final = exp / "film.mp4"
    render.ffmpeg(["-i", str(video), "-i", str(mixed), "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac",
                   "-b:a", "192k", "-movflags", "+faststart", "-t", f"{total:.3f}", str(final)])
    srt = [(c["start"], c["start"] + (c.get("speech") or c["seconds"]), c["subtitle"]) for c in tl if c.get("subtitle")]
    (exp / "film.srt").write_text("".join(f"{i + 1}\n{_ts(a)} --> {_ts(b)}\n{t}\n\n" for i, (a, b, t) in enumerate(srt)),
                                  encoding="utf-8")
    _manifests(p, tl, exp, total)
    _render_manifest(exp, tl, film, (w, h), joined, final, srt_count=len(srt))
    dlg = performance.dialogue_timeline(tl)
    for d in dlg:                                       # the talking clip each line plays over (for lip-sync QC)
        d["clip"] = next((c.get("source") for c in tl if c.get("shot_id") == d["shot_id"]), None)
    master = {"clock": "seconds from the first frame of film.mp4", "duration": round(total, 3),
              "shots": [{k: c.get(k) for k in ("scene", "shot", "start", "end", "shot_type", "characters",
                                               "motion_type", "provider", "animation")}
                        for c in tl if c["kind"] == "shot"],
              "dialogue": dlg, "music": plan, "ambience": amb_used,
              "subtitles": [{"start": a, "end": b, "text": t} for a, b, t in srt],
              "mix": {**mix_report, "dialogue_over_music": performance.dialogue_vs_music(dialogue, music_wav, speech)}}
    (exp / "master_timeline.json").write_text(json.dumps(master, ensure_ascii=False, indent=1,
                                                        default=performance.jsonable))
    logs.event("info", "film", f"Edited {total / 60:.1f} min from {len(tl) - 2} shots", production_id=pid)
    return {"video": str(final), "srt": str(exp / "film.srt"), "seconds": round(total, 2), "timeline": tl}


def _render_manifest(exp: Path, tl: list[dict[str, Any]], film: dict[str, Any], size: tuple[int, int], joined: str,
                     final: Path, srt_count: int) -> None:
    """What this export was made from and how: every clip's cache key (inputs + their file identity), the join
    method, renderer version and the real output properties — enough to tell whether a re-export would differ."""
    out = render.stream_format(final)
    manifest = {"renderer": render.RENDER_VERSION, "made_at": db.iso(), "size": list(size), "fps": config.FPS,
                "join": joined, "subtitles": srt_count,
                "clips": [{"index": i, **cache_key(c, film, size)} for i, c in enumerate(tl)],
                "output": {"file": final.name, "bytes": final.stat().st_size, "video": out["v"], "audio": out["a"]}}
    (exp / "render_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))


def _manifests(p: dict[str, Any], tl: list[dict[str, Any]], exp: Path, total: float) -> None:
    ws, pid, film = p["workspace_id"], p["id"], p["package"]
    shots = store.shots(pid, ws)
    manifest = []
    for sh in shots:
        takes = store.takes(sh["id"], ws)
        manifest.append({**{k: sh[k] for k in ("scene_no", "shot_no", "shot_type", "target_seconds", "characters",
                                               "action", "dialogue", "camera", "lighting", "continuity", "status",
                                               "motion_type")},
                         "motion": store.MOTION_LABEL.get(sh["motion_type"]),
                         "takes": [{k: t[k] for k in ("take_no", "provider", "model", "motion_type", "status",
                                                      "file_path", "error", "cost_usd")} for t in takes]})
    (exp / "timeline.json").write_text(json.dumps(tl, ensure_ascii=False, indent=1))
    (exp / "shot_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    used = sorted({t["provider"] for m in manifest for t in m["takes"] if t["status"] != "failed"} |
                  {"cloudflare_flux", "kokoro", "ffmpeg_camera"})
    lic = [{"provider": u, "name": providers.BY_ID[u]["name"], "model": providers.BY_ID[u]["model"],
            "licence": providers.BY_ID[u]["licence"], "commercial_use": providers.BY_ID[u]["commercial"]}
           for u in used if u in providers.BY_ID]
    (exp / "licences.json").write_text(json.dumps(lic, ensure_ascii=False, indent=1))
    summary = store.summary(pid, ws)
    meta = {"production_id": pid, "workspace_id": ws, "title": film.get("title"), "seconds": round(total, 2),
            "cast": [{k: c.get(k) for k in ("key", "name", "bible", "voice")} for c in film.get("cast") or []],
            "shots": summary, "plan": film.get("shot_plan"), "cost_usd": summary["cost_usd"],
            "made_with_ai": "AI-generated images, voices and (where marked) video", "created_at": db.iso()}
    (exp / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
