"""Upload & Edit jobs: the creator's files + instructions → edited video(s) in the library.

Stages: inspect (re-check originals, scene cuts) → transcribe (local speech-to-text) → plan (AI edit plan)
→ render (every planned output, resumable per output) → review (quality results; publish only if the channel
allows auto-publish). The first output belongs to this production; each further output (e.g. Shorts 2–5)
becomes its own library item linked to the same job (parent_id) and to the same source files.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .. import ai, channels, config, db, logs, plans, uploads
from . import engine, planner
from .transcribe import transcribe

STAGES = ["inspect", "transcribe", "plan", "render", "review"]
CLIP_STAGES = ["review"]
STATUS = {"inspect": "analysing media", "transcribe": "transcribing", "plan": "planning edit",
          "render": "rendering", "review": "quality review"}


class SourceProblem(Exception):
    """A problem a retry can't fix (missing/changed original, plan limit): stop and tell the creator."""


def workdir(pid: int) -> Path:
    d = config.PRODUCTIONS_DIR / f"{pid:05d}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def output_language(request: str, ch: dict[str, Any] | None) -> str:
    """Titles/descriptions language: what the request asks for, else the channel's, else the request's script."""
    low = (request or "").lower()
    for word, lang in (("hinglish", "Hinglish"), ("hindi", "Hindi"), ("english", "English"), ("marathi", "Marathi"),
                       ("tamil", "Tamil"), ("telugu", "Telugu"), ("bengali", "Bengali"), ("gujarati", "Gujarati")):
        if re.search(rf"\b{word}\b", low) and not re.search(rf"\b{word}\s+(subtitles?|captions?)\b", low):
            return lang
    if ch and ch.get("language"):
        return ch["language"]
    return "Hindi" if re.search(r"[\u0900-\u097F]", request or "") else "English"


def create(ws_id: int, request: str, media_ids: list[int], *, channel_id: int | None = None, fmt: str | None = None,
           user_id: int | None = None, voice_id: int | str | None = None) -> int:
    from .. import worker
    media = [uploads.get(int(m), ws_id) for m in media_ids]
    if not media or any(m is None for m in media):
        raise ValueError("one of the selected files was not found in this workspace")
    if not any(m["kind"] in ("video", "audio") for m in media):
        raise ValueError("add at least one video or audio file to edit")
    ch = channels.get(channel_id) if channel_id else None
    if channel_id and (not ch or ch["workspace_id"] != ws_id):
        raise ValueError("channel not found")
    plans.check(ws_id, "video")
    voice = None
    if voice_id:                                     # re-voice the speech with this account's cloned voice
        from ..media import voiceclone
        v = voiceclone.get(voice_id, ws_id)
        if not v:
            raise ValueError("that voice isn't one of yours")
        voice = {"engine": "clone", "voice_id": str(v["id"]), "name": v["name"]}
    language = output_language(request, ch)
    title = (request.strip() or f"Edit of {media[0]['original_name']}")[:90]
    pid = db.insert("productions", {
        "format": "short" if fmt == "short" else "long", "title": title, "status": "queued", "stage": "inspect",
        "kind": "edit", "channel_id": channel_id, "workspace_id": ws_id, "language": language,
        "request": json.dumps({"request": request, "format": fmt, "user_id": user_id, "voice": voice},
                              ensure_ascii=False),
        "source_media": json.dumps([m["id"] for m in media]), "package": "{}", "timeline": "{}", "episode": "{}",
        "test_mode": 0, "created_at": db.iso(), "updated_at": db.iso()})
    worker.enqueue("production", {"production_id": pid}, production_id=pid, max_attempts=3)
    logs.event("info", "editing", f"Edit job #{pid} queued with {len(media)} file(s)", production_id=pid)
    return pid


def _sources(p: dict[str, Any]) -> list[dict[str, Any]]:
    ids = db.loads(p.get("source_media"), []) or []
    out = []
    for i in ids:
        m = uploads.get(i, p["workspace_id"])
        if not m:
            raise SourceProblem(f"source file #{i} is no longer available (deleted?)")
        out.append(m)
    return out


def _save(pid: int, **fields: Any) -> None:
    from .. import pipeline
    pipeline.save(pid, **fields)


def stage_inspect(p: dict[str, Any]) -> None:
    ep = p["episode"]
    scenes = {}
    for m in _sources(p):
        if not uploads.verify_intact(m):
            raise SourceProblem(f"original '{m['original_name']}' changed or is missing — it must stay intact")
        if m["kind"] == "video":
            scenes[str(m["id"])] = engine.scene_cuts(m["path"], m.get("duration") or 0)
    ep["scenes"] = scenes
    _save(p["id"], episode=ep)


def stage_transcribe(p: dict[str, Any]) -> None:
    n = 0.0
    for m in _sources(p):
        if m["kind"] in ("video", "audio") and m.get("has_audio") and m.get("transcript") is None:
            transcribe(m, p.get("language"))
            n += (m.get("duration") or 0) / 60
    if n:
        plans.record(p["workspace_id"], "transcribe_minutes", round(n, 2), production_id=p["id"],
                     idem_key=f"transcribe:{p['id']}")


def stage_plan(p: dict[str, Any]) -> None:
    req = db.loads(p.get("request"), {}) or {}
    media = _sources(p)
    scenes = {int(k): v for k, v in (p["episode"].get("scenes") or {}).items()}
    edit, meta = planner.plan(req.get("request") or "", media, scenes, language=p.get("language") or "English",
                              fmt=req.get("format"))
    first = edit.outputs[0]
    pkg = {"edit_plan": edit.model_dump(), "planner": meta, "unsupported": edit.unsupported,
           "title": first.title, "description": first.description, "tags": first.tags}
    _save(p["id"], package=pkg, title=first.title[:100], format="short" if first.aspect == "9:16" else "long")
    note = f"Plan: {edit.summary}" + (f" — not possible here: {'; '.join(edit.unsupported)}" if edit.unsupported else "")
    logs.event("info", "editing", note[:500], production_id=p["id"])


def _render_one(p: dict[str, Any], idx: int, o: dict[str, Any], media: dict[int, dict[str, Any]], work: Path) -> dict[str, Any]:
    srcs = [m for m in media.values() if m["kind"] in ("video", "audio")]
    first = srcs[0]
    w, h = engine.target_size(o["aspect"], first.get("width"), first.get("height"))
    segs = o["segments"] or [{"media_id": m["id"], "start": 0.0, "end": float(m["duration"] or 0)} for m in srcs]
    speed = float(o.get("speed") or 1.0)
    clips, pieces, t = [], [], 0.0
    if o.get("intro_text"):
        c = work / f"o{idx}_intro.mp4"
        engine.card_clip(o["intro_text"], w, h, 2.5, c, f"{p['id']}-{idx}-in")
        clips.append(c)
        t += 2.5
    for si, s in enumerate(segs):
        m = media[int(s["media_id"])]
        start, end = float(s["start"]), min(float(s["end"]), float(m["duration"] or s["end"]))
        parts = engine.keep_intervals(start, end, engine.silences(m["path"], start, end)) \
            if o.get("remove_silences") and m.get("has_audio") else [(start, end)]
        for pi, (a, b) in enumerate(parts):
            c = work / f"o{idx}_s{si:03d}_{pi:03d}.mp4"
            engine.cut_clip(m, a, b, w, h, o.get("reframe", "fit_blur"), speed, c)
            real = engine.probe(c)["duration"] or (b - a) / speed
            pieces.append({"media_id": m["id"], "src_start": a, "src_end": b, "out_start": t, "speed": speed})
            clips.append(c)
            t += real
    if o.get("outro_text"):
        c = work / f"o{idx}_outro.mp4"
        engine.card_clip(o["outro_text"], w, h, 3.0, c, f"{p['id']}-{idx}-out")
        clips.append(c)
        t += 3.0
    joined = work / f"o{idx}_joined.mp4"
    engine.concat(clips, joined)
    total = engine.probe(joined)["duration"] or t
    voice = (db.loads(p.get("request"), {}) or {}).get("voice") or {}
    revoiced = 0
    if voice.get("engine") == "clone":           # "replace the voice with my voice": same words, cloned voice
        lines = []
        for m_id in {pc["media_id"] for pc in pieces}:
            lines += engine.speech_lines(media[m_id].get("transcript") or [],
                                         [pc for pc in pieces if pc["media_id"] == m_id])
        lines.sort(key=lambda x: x["start"])
        if lines:
            joined = engine.revoice(joined, lines, voice["voice_id"], total, work / f"o{idx}_voice")
            revoiced = len(lines)
    out_dir = workdir(p["id"]) / "outputs"
    out_dir.mkdir(exist_ok=True)
    stem = f"{idx + 1:02d}"
    clean = out_dir / f"{stem}_video.mp4"
    engine.finish(joined, clean, music_mood=o.get("music_mood", "none"), subs=None, total=total, work=work)
    result: dict[str, Any] = {"name": o["name"], "aspect": o["aspect"], "video": str(clean), "clean_video": None,
                              "srt": None, "pieces": pieces}
    if voice.get("engine") == "clone":
        result["note"] = (f"re-voiced {revoiced} line(s) in your cloned voice" if revoiced
                          else "no speech found to re-voice — the original audio was kept")
    cue_list: list[dict[str, Any]] = []
    for m_id in {pc["media_id"] for pc in pieces}:
        tr = media[m_id].get("transcript") or []
        cue_list += engine.cues(tr, [pc for pc in pieces if pc["media_id"] == m_id])
    cue_list.sort(key=lambda c: c["start"])
    if cue_list and (o.get("subtitle_language") or "original").lower() not in ("original", "as spoken", ""):
        cue_list = translate_cues(cue_list, o["subtitle_language"])
    if cue_list:
        srt = out_dir / f"{stem}_subtitles.srt"
        engine.write_srt(cue_list, srt)
        result["srt"] = str(srt)
    if o.get("subtitles") and cue_list:
        track = engine.subtitle_track(cue_list, w, h, total, o.get("highlight_words") or [], work / f"o{idx}_subs")
        subbed = out_dir / f"{stem}_video_subtitled.mp4"
        engine.finish(joined, subbed, music_mood=o.get("music_mood", "none"), subs=track, total=total, work=work)
        result["clean_video"], result["video"] = str(clean), str(subbed)
    elif o.get("subtitles"):
        result["note"] = "subtitles requested but no speech was found in the source"
    thumb = engine.thumbnail(Path(result["video"]), o["title"], out_dir / f"{stem}_thumbnail.jpg", total * 0.33)
    checks = engine.check_output(Path(result["video"]), w, h, total)
    if o.get("subtitles"):
        checks.append({"name": "subtitles", "ok": bool(cue_list), "severity": "warn",
                       "detail": f"{len(cue_list)} captions" if cue_list else "no speech found to caption"})
    result.update({"thumbnail": str(thumb), "checks": checks, "seconds": round(total, 2),
                   "info": engine.media_info(Path(result["video"])),
                   "passed": all(c["ok"] for c in checks if c["severity"] == "fail")})
    plans.record(p["workspace_id"], "render_minutes", round(total / 60, 3), production_id=p["id"],
                 idem_key=f"render:{p['id']}:{idx}")
    plans.record(p["workspace_id"], "output_bytes", sum(Path(x).stat().st_size for x in
                                                        (result["video"], result["clean_video"]) if x),
                 production_id=p["id"], idem_key=f"bytes:{p['id']}:{idx}")
    return result


class _Lines(BaseModel):
    lines: list[str]


def translate_cues(cues: list[dict[str, Any]], language: str) -> list[dict[str, Any]]:
    """Caption translation, in batches; timings stay the same. Each line keeps the same index."""
    out = [dict(c) for c in cues]
    for i in range(0, len(out), 40):
        batch = out[i:i + 40]
        numbered = "\n".join(f"{n}. {c['text']}" for n, c in enumerate(batch))

        def check(r: _Lines, n: int = len(batch)) -> None:
            if len(r.lines) != n:
                raise ValueError(f"return exactly {n} lines")
        res, _ = ai.generate("script", "You translate video captions. Output one JSON object {\"lines\": [...]}.",
                             f"Translate each caption into natural, spoken {language} (short, same meaning, "
                             f"same order, one output line per input line, no numbering):\n{numbered}", _Lines,
                             max_tokens=4000, validate=check)
        for c, t in zip(batch, res.lines):
            c["text"] = t.strip() or c["text"]
    return out


def _package(o: dict[str, Any], plan_pkg: dict[str, Any], src_names: list[str]) -> dict[str, Any]:
    desc = (o.get("description") or "").strip()
    tags = [t.strip("# ") for t in (o.get("tags") or []) if t.strip("# ")][:15]
    final = desc + ("\n\n" + " ".join("#" + t.replace(" ", "") for t in tags[:5]) if tags else "")
    if o["aspect"] == "9:16" and "#shorts" not in final.lower():
        final = (final + " #Shorts").strip()
    return {"title": o["title"][:100], "description": desc, "tags": tags, "final_description": final[:4900],
            "sources": src_names}


def stage_render(p: dict[str, Any]) -> None:
    pkg, ep = p["package"], p["episode"]
    outputs = pkg["edit_plan"]["outputs"]
    done: list[dict[str, Any]] = ep.get("outputs") or []
    for _ in outputs:
        lim_minutes = sum(float(s["end"]) - float(s["start"]) for s in _["segments"]) / 60 if _["segments"] else 0
        plans.check(p["workspace_id"], "render_minutes", lim_minutes)
    media = {m["id"]: m for m in _sources(p)}
    work = workdir(p["id"]) / "work"
    work.mkdir(exist_ok=True)
    for idx, o in enumerate(outputs):
        if idx < len(done) and done[idx].get("video") and Path(done[idx]["video"]).exists():
            continue                                # finished before a restart/retry: never re-rendered
        res = _render_one(p, idx, o, media, work)
        done = done[:idx] + [res]
        ep["outputs"] = done
        _save(p["id"], episode=ep)
        logs.event("info", "editing", f"Rendered '{o['name']}' ({res['seconds']:.0f}s, "
                   f"{res['info']['width']}x{res['info']['height']})", production_id=p["id"])
    shutil.rmtree(work, ignore_errors=True)


def stage_review(p: dict[str, Any]) -> None:
    from .. import pipeline, worker
    from ..youtube import oauth
    if p["kind"] == "edit_clip":                    # an extra output: it only needs its file to exist
        ok = bool(p.get("video_path")) and Path(p["video_path"]).exists()
        _save(p["id"], status="awaiting_approval" if ok else "failed",
              error="Ready to preview and download." if ok else "Output file is missing — retry the parent job.")
        return
    pkg, ep = p["package"], p["episode"]
    outs = ep.get("outputs") or []
    names = [m["original_name"] for m in _sources(p)]
    plan_outs = pkg["edit_plan"]["outputs"]
    ch = channels.get(p.get("channel_id")) if p.get("channel_id") else None
    auto = bool(ch and ch.get("auto_publish") and ch.get("status") == "active" and oauth.connected(ch["id"]))
    unsupported = pkg.get("unsupported") or []
    note = ("Not possible with the available tools: " + "; ".join(unsupported) + ". Everything else was done. ") \
        if unsupported else ""
    existing = {r["episode_idx"]: r["id"] for r in db.q(
        "SELECT id, json_extract(episode, '$.output_index') AS episode_idx FROM productions WHERE parent_id=? "
        "AND kind='edit_clip'", (p["id"],))}
    failed = []
    for idx, res in enumerate(outs):
        qa = {"passed": res["passed"], "checks": res["checks"], "auto_publish_ok": res["passed"]}
        out_pkg = _package(plan_outs[idx], pkg, names)
        fields = {"video_path": res["video"], "thumbnail_path": res["thumbnail"], "srt_path": res.get("srt"),
                  "qa": json.dumps(qa), "format": "short" if res["aspect"] == "9:16" else "long",
                  "title": out_pkg["title"]}
        if not res["passed"]:
            failed.append(res["name"])
        if idx == 0:
            pipeline.save(p["id"], package={**pkg, **out_pkg}, **{k: v for k, v in fields.items() if k != "qa"},
                          qa=qa)
            target = p["id"]
        else:
            target = existing.get(idx)
            if not target:
                target = db.insert("productions", {
                    "format": fields["format"], "title": fields["title"], "status": "queued", "stage": "review",
                    "kind": "edit_clip", "parent_id": p["id"], "channel_id": p.get("channel_id"),
                    "workspace_id": p["workspace_id"], "language": p.get("language"), "request": p.get("request"),
                    "source_media": p.get("source_media"), "package": json.dumps(out_pkg, ensure_ascii=False),
                    "episode": json.dumps({"output_index": idx, "output": res}, ensure_ascii=False),
                    "timeline": "{}", "test_mode": 0, "created_at": db.iso(), "updated_at": db.iso()})
            pipeline.save(target, package=out_pkg, qa=qa, video_path=res["video"], thumbnail_path=res["thumbnail"],
                          srt_path=res.get("srt"), format=fields["format"], title=fields["title"])
        status_msg = note + ("Ready to preview and download." if res["passed"] else
                             "Quality check failed: " + "; ".join(c["name"] + " — " + c["detail"]
                                                                  for c in res["checks"] if not c["ok"]))
        if res["passed"] and auto:
            pipeline.approve(target, privacy=ch.get("publish_privacy") or "private", publish_at=None,
                             by="auto (channel auto-publish)")
        else:
            pipeline.save(target, status="awaiting_approval" if res["passed"] else "needs_attention",
                          error=status_msg.strip())
    if failed:
        logs.event("warning", "editing", f"{len(failed)} output(s) failed checks: {', '.join(failed)}",
                   production_id=p["id"])
    worker._wake.set()


def _guard(fn):
    def run(p: dict[str, Any]) -> None:
        from ..pipeline import NeedsAttention
        try:
            return fn(p)
        except (SourceProblem, plans.LimitExceeded) as e:
            raise NeedsAttention(str(e)) from e
    return run


STAGE_FN = {k: _guard(v) for k, v in {"inspect": stage_inspect, "transcribe": stage_transcribe, "plan": stage_plan,
                                      "render": stage_render, "review": stage_review}.items()}
