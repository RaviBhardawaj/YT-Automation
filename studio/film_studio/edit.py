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
from . import providers, store

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
                    "transition_out": sh["continuity"].get("transition_out")})
    out.append({"kind": "end", "seconds": END_SEC})
    t = 0.0
    for c in out:
        c["start"] = round(t, 3)
        t += c["seconds"]
        c["end"] = round(t, 3)
    return out


def _clip_for(c: dict[str, Any], i: int, work: Path, film: dict[str, Any], seed: str) -> Path:
    from ..creative import film as filmmod
    w, h = config.frame_size("long")
    out = work / f"c{i:04d}.mp4"
    sig = work / f"c{i:04d}.json"
    key = {k: c.get(k) for k in ("kind", "source", "seconds", "subtitle", "motion_type", "speech")}
    if out.exists() and sig.exists() and json.loads(sig.read_text()) == key:
        return out                                           # unchanged since the last edit: reuse
    overlays = []
    if c.get("subtitle"):
        sp = work / f"sub{i:04d}.png"
        pos = filmmod._subtitle(c["subtitle"], w, h, sp)
        overlays.append({"path": sp, **pos, "start": 0.0, "end": min(c["seconds"], (c.get("speech") or c["seconds"]) + 0.15)})
    if c["kind"] in ("title", "end"):
        img = filmmod._card(film["title"], film.get("closing_line", "") if c["kind"] == "end" else "", w, h,
                            work / f"{c['kind']}.jpg")
        render.scene_clip(i, {"kind": "image", "local_path": str(img), "is_card": True}, c["seconds"], 0.0, [],
                          "long", work, out, seed, look="real")
    elif c["motion_type"] in ("video", "dialogue_video", "lipsync_verified") and c.get("source") \
            and Path(c["source"]).exists():
        clip_len = float(visuals.probe_video(Path(c["source"])).get("duration") or 0)
        play = min(c["seconds"], clip_len) if clip_len > 0.3 else 0
        rest = round(c["seconds"] - play, 3)
        if play <= 0:
            return _still(c, i, work, overlays, out, seed)
        part_a = work / f"c{i:04d}_a.mp4"
        render.scene_clip(i, {"kind": "video", "local_path": c["source"]}, play, 0.0,
                          [o for o in overlays if o["start"] < play], "long", work, part_a, seed, look="real")
        if rest > 0.2 and c.get("keyframe") and Path(c["keyframe"]).exists():
            part_b = work / f"c{i:04d}_b.mp4"                # finish the shot on its key picture, no stretching
            shifted = [{**o, "start": 0.0, "end": max(0.0, o["end"] - play)} for o in overlays if o["end"] > play]
            render.scene_clip(i, {"kind": "image", "local_path": c["keyframe"]}, rest, 0.0, shifted, "long", work,
                              part_b, seed, look="real")
            lst = work / f"c{i:04d}.txt"
            lst.write_text(f"file '{part_a}'\nfile '{part_b}'\n")
            render.ffmpeg(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)])
        else:
            part_a.replace(out)
    else:
        _still(c, i, work, overlays, out, seed)
    sig.write_text(json.dumps(key))
    return out


def _still(c, i, work, overlays, out, seed) -> Path:
    src = c.get("keyframe") or c.get("source")
    if not src or not Path(src).exists():
        raise FileNotFoundError(f"shot {c.get('scene')}.{c.get('shot')} has no picture")
    render.scene_clip(i, {"kind": "image", "local_path": src}, c["seconds"], 0.0, overlays, "long", work, out, seed,
                      look="real")
    return out


def _ts(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def assemble(p: dict[str, Any]) -> dict[str, Any]:
    from ..creative import film as filmmod
    from ..creative import music
    film, pid = p["package"], p["id"]
    work = filmmod.workdir(pid) / "edit"
    work.mkdir(exist_ok=True)
    tl = timeline(p)
    missing = [f"{c['scene']}.{c['shot']}" for c in tl if c["kind"] == "shot" and c["motion_type"] == "missing"]
    if missing:
        raise FileNotFoundError(f"{len(missing)} shot(s) have no take yet: {', '.join(missing[:8])}")
    clips = [_clip_for(c, i, work, film, f"film{pid}") for i, c in enumerate(tl)]
    total = tl[-1]["end"]
    listing = work / "clips.txt"
    listing.write_text("".join(f"file '{c}'\n" for c in clips))
    video = work / "video.mp4"
    render.ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(video)])
    sr = filmmod.SR
    track = np.zeros(int((total + 1) * sr), dtype=np.int32)
    for c in tl:
        if c.get("wav") and Path(c["wav"]).exists():
            a = filmmod._read(Path(c["wav"])).astype(np.int32)
            at = int(c["start"] * sr)
            track[at:at + len(a)] += a[:max(0, len(track) - at)]
    dialogue = work / "dialogue.wav"
    filmmod._write(dialogue, np.clip(track, -32768, 32767))
    bed = music.bed("funny", total + 1, work / "music.wav")
    exp = export_dir(pid)
    final = exp / "film.mp4"
    graph = (f"[1:a]aresample=48000[d];[2:a]aresample=48000,volume=0.07,afade=t=out:st={max(0, total - 3):.2f}:d=3[m];"
             f"anoisesrc=color=pink:amplitude=0.012:r=48000,lowpass=f=900,highpass=f=120,atrim=0:{total:.3f}[air];"
             f"[d][m][air]amix=inputs=3:duration=first:normalize=0,loudnorm=I=-15:TP=-1.5:LRA=11[a]")
    render.ffmpeg(["-i", str(video), "-i", str(dialogue), "-i", str(bed), "-filter_complex", graph, "-map", "0:v",
                   "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                   "-t", f"{total:.3f}", str(final)])
    srt = [(c["start"], c["start"] + (c.get("speech") or c["seconds"]), c["subtitle"]) for c in tl if c.get("subtitle")]
    (exp / "film.srt").write_text("".join(f"{i + 1}\n{_ts(a)} --> {_ts(b)}\n{t}\n\n" for i, (a, b, t) in enumerate(srt)),
                                  encoding="utf-8")
    _manifests(p, tl, exp, total)
    logs.event("info", "film", f"Edited {total / 60:.1f} min from {len(tl) - 2} shots", production_id=pid)
    return {"video": str(final), "srt": str(exp / "film.srt"), "seconds": round(total, 2), "timeline": tl}


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
