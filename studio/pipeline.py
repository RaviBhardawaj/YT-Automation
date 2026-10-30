"""Production state machine. Each stage is idempotent and persists its output, so a crash or
restart resumes from the last unfinished stage (and, inside rendering, the last unfinished scene)."""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from . import ai, config, db, logs, profile, settings
from .content import package as content
from .media import captions, graphics, qa, render, thumbnail, tts, visuals
from .research import engine

log = logging.getLogger(__name__)

STAGES = ["research", "script", "review", "assets", "voice", "render", "thumbnail", "qa"]
STATUS = {"research": "researching", "script": "scripting", "review": "reviewing", "assets": "sourcing visuals",
          "voice": "voicing", "render": "rendering", "thumbnail": "making thumbnail", "qa": "quality check"}
TERMINAL = ("awaiting_approval", "needs_attention", "needs_review", "failed", "rejected", "uploaded", "approved",
            "uploading", "paused_budget", "test_passed")


FINANCE_KINDS = ("nifty_pivot", "fib_lesson", "nifty_short")


class PausedByOwner(Exception):
    """The owner paused this video while one of its steps was running; the next step waits for Resume."""


class NeedsAttention(Exception):
    """Stop without retrying; a human must look at this production."""


def workdir(pid: int) -> Path:
    d = config.PRODUCTIONS_DIR / f"{pid:05d}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load(pid: int) -> dict[str, Any]:
    p = db.q1("SELECT * FROM productions WHERE id=?", (pid,))
    if not p:
        raise NeedsAttention(f"production {pid} not found")
    p["package"] = db.loads(p["package"], {}) or {}
    p["timeline"] = db.loads(p["timeline"], {}) or {}
    p["qa"] = db.loads(p["qa"], {}) or {}
    p["episode"] = db.loads(p.get("episode"), {}) or {}
    p["compliance"] = db.loads(p.get("compliance"), {}) or {}
    return p


def save(pid: int, **fields: Any) -> None:
    for k in ("package", "timeline", "qa", "episode", "compliance"):
        if k in fields and not isinstance(fields[k], (str, type(None))):
            fields[k] = db.dumps(fields[k])
    fields["updated_at"] = db.iso()
    db.update("productions", pid, fields)


def assets_for(pid: int) -> list[dict[str, Any]]:
    rows = db.q("SELECT * FROM assets WHERE production_id=? ORDER BY scene_index", (pid,))
    for r in rows:
        r["synthetic"] = r["provider"] in ("gemini_image", "gemini_video", "ai_presenter")   # realistic AI person
        r["is_card"] = (r["provider_id"] or "").endswith("#card")
    return rows


# ------------------------------------------------------------------ creation

def create(topic_id: int | None = None, fmt: str | None = None, *, test_mode: bool = False,
           sample: bool = False, audience: str = "india", extra: dict[str, Any] | None = None) -> int:
    from . import worker
    if audience not in profile.AUDIENCES:
        raise ValueError(f"audience must be one of {list(profile.AUDIENCES)}")
    language = profile.language_for(audience)
    topic = db.q1("SELECT * FROM topics WHERE id=?", (topic_id,)) if topic_id else None
    fmt = fmt or (topic["format"] if topic else profile.formats()[0])
    pkg: dict[str, Any] = {}
    stage = "research"
    title = topic["title"] if topic else "Pipeline self-test"
    if sample:
        with profile.using_language(language):
            pkg = sample_package(fmt)
        stage, title, test_mode = "assets", pkg["title"], True
    pid = db.insert("productions", {"topic_id": topic_id, "format": fmt, "title": title, "status": "queued",
                                    "stage": stage, "package": db.dumps(pkg), "test_mode": int(test_mode),
                                    "audience": audience, "language": language,
                                    "created_at": db.iso(), "updated_at": db.iso(), **(extra or {})})
    if topic:
        db.update("topics", topic["id"], {"status": "in_production", "updated_at": db.iso()})
    worker.enqueue("production", {"production_id": pid}, production_id=pid, max_attempts=4)
    logs.event("info", "pipeline", f"Production #{pid} queued: {title} ({fmt})", production_id=pid)
    return pid


# ------------------------------------------------------------------ stages

def _topic(p: dict[str, Any]) -> dict[str, Any]:
    t = db.q1("SELECT * FROM topics WHERE id=?", (p["topic_id"],)) if p.get("topic_id") else None
    return t or {"id": 0, "title": p["title"], "angle": "", "hook_idea": "", "search_query": p["title"], "evidence_ids": "[]"}


def _evidence(p: dict[str, Any]) -> list[dict[str, Any]]:
    ids = p["package"].get("evidence_ids") or []
    if not ids:
        return []
    return engine.load_evidence(f"id IN ({','.join('?' for _ in ids)})", tuple(ids))


def stage_research(p: dict[str, Any]) -> str | None:
    if p["test_mode"] and not p.get("topic_id"):
        return None
    ev = engine.research_topic(_topic(p))
    pkg = p["package"]
    pkg["evidence_ids"] = list(dict.fromkeys(e["id"] for e in ev))
    save(p["id"], package=pkg)
    logs.event("info", "pipeline", f"Research: {len(ev)} evidence items", production_id=p["id"])
    return None


def stage_script(p: dict[str, Any]) -> str | None:
    topic, evidence = _topic(p), _evidence(p)
    old = p["package"]
    feedback = old.get("_revise_feedback", "")

    def gen(fb: str, resume: bool):
        if p["format"] == "short":
            return content.generate_short(topic, evidence, fb)
        # long-form: checkpoint outline/chapters into the package so a retry resumes (and doesn't re-bill)
        partial = dict(old.get("_long_partial") or {}) if resume else {}

        def checkpoint(state: dict) -> None:
            save(p["id"], package={**old, "_long_partial": state})
        return content.generate_long(topic, evidence, fb, partial=partial, checkpoint=checkpoint)

    pkg, meta = gen(feedback, resume=not feedback)
    orig = content.originality(pkg, evidence)
    if not orig["ok"]:
        logs.event("warning", "pipeline", f"Originality check failed ({orig}); rewriting once", production_id=p["id"])
        pkg, meta = gen(feedback + "\n\nORIGINALITY FEEDBACK: your previous draft reused wording from "
                        "the sources or a competitor title. Rewrite everything in your own words with your own structure.",
                        resume=False)
        orig = content.originality(pkg, evidence)
    pkg.update({"evidence_ids": old.get("evidence_ids", []), "generated_by": meta, "originality": orig,
                "revision": old.get("revision", 0), "publish_suggestion": content.suggest_publish_time(p["format"])})
    save(p["id"], package=pkg, title=pkg["title"])
    logs.event("info", "pipeline", f"Script ready: '{pkg['title']}' — {len(pkg['scenes'])} scenes, "
               f"{content.word_count(content.script_text(pkg))} words via {meta['provider']}/{meta['model']}",
               production_id=p["id"])
    return None


def stage_review(p: dict[str, Any]) -> str | None:
    pkg = p["package"]
    if p["test_mode"] and not p.get("topic_id"):
        return None
    try:
        rev, meta = content.review(pkg, _topic(p), _evidence(p))
    except ai.AllProvidersFailed as e:
        pkg["review"] = {"error": str(e), "issues": [], "ready": False, "overall": "review unavailable"}
        save(p["id"], package=pkg)
        return None
    high = [i for i in rev["issues"] if i["severity"] == "high"]
    if high and pkg.get("revision", 0) < 1:
        pkg["revision"] = pkg.get("revision", 0) + 1
        pkg["_revise_feedback"] = "\n\nEDITOR FEEDBACK — fix all of these:\n" + "\n".join(
            f"- scene {i['scene']}: [{i['category']}] {i['detail']} → {i['suggestion']}" for i in rev["issues"]
            if i["severity"] in ("high", "medium"))
        pkg["review_previous"] = rev
        save(p["id"], package=pkg)
        logs.event("info", "pipeline", f"Editor found {len(high)} high-severity issue(s); revising script", production_id=p["id"])
        return "script"
    rev["generated_by"] = meta
    pkg["review"] = rev
    save(p["id"], package=pkg)
    return None


def stage_assets(p: dict[str, Any]) -> str | None:
    pkg = p["package"]
    work = workdir(p["id"]) / "visuals"
    work.mkdir(exist_ok=True)
    have = {r["scene_index"] for r in db.q("SELECT scene_index FROM assets WHERE production_id=?", (p["id"],))}
    # avoid reusing the same stock clip across videos made in the last 30 days
    used = {f"{r['provider']}:{r['provider_id']}" for r in db.q(
        "SELECT provider, provider_id FROM assets WHERE created_at >= ?", (db.iso_in(-30 * 86400),))}
    seed = f"p{p['id']}"
    failures: dict[str, int] = {}
    ai_left = _ai_clip_budget(p, have)
    from .creative import news
    for idx, scene in enumerate(pkg["scenes"]):
        if idx in have:
            continue
        anchor = news.presenter_asset(p, idx, work)      # news channel: its presenter opens and closes the video
        if anchor:
            db.insert("assets", {**anchor, "production_id": p["id"], "created_at": db.iso()})
            continue
        if ai_left and scene.get("visual_type") in ("video", "illustration"):
            rec = _ai_clip(p, scene, idx, work)
            if rec:
                ai_left -= 1
                db.insert("assets", {**rec, "production_id": p["id"], "created_at": db.iso()})
                continue
        # test mode: original procedural art only (no network, no API keys needed)
        rec = visuals.choose(scene, idx, p["format"], used, work, seed,
                             providers=[] if p["test_mode"] else None, failures=failures,
                             topic=pkg.get("title") or p.get("title") or "", look=video_look(p))
        notes = rec.pop("notes", [])
        rec["relevance"] = db.dumps(rec.pop("relevance", {}))
        rec.pop("synthetic", None)
        is_card = rec.pop("is_card", False)
        db.insert("assets", {**rec, "production_id": p["id"], "created_at": db.iso(),
                             "attribution": rec.get("attribution") or "", "provider_id": rec["provider_id"]
                             + ("#card" if is_card else "")})
        if notes:
            log.info("scene %s visual notes: %s", idx, notes)
    logs.event("info", "pipeline", f"Visuals ready for {len(pkg['scenes'])} scenes", production_id=p["id"])
    return None


def _ai_clip_budget(p: dict[str, Any], have: set[int]) -> int:
    """AI clips still allowed for this video: only when the job opted in and AI video is switched on."""
    from .ai import videogen
    plan = db.loads(p.get("request"), {}) or {}
    if not plan.get("ai_video") or not videogen.cfg()["enabled"] or p.get("test_mode"):
        return 0
    made = db.q1("SELECT COUNT(*) AS n FROM assets WHERE production_id=? AND provider=?",
                 (p["id"], videogen.PROVIDER))["n"]
    return max(0, videogen.cfg()["max_clips"] - made)


def _ai_clip(p: dict[str, Any], scene: dict[str, Any], idx: int, work: Path) -> dict[str, Any] | None:
    """One AI-generated clip for a scene; None (→ normal visuals) if it can't be made or the cap is reached."""
    from .ai import videogen
    target = work / f"ai_{idx:03d}.mp4"
    prompt = (f"{scene.get('visual_description') or scene.get('visual_query')}. Cinematic, realistic, smooth camera "
              f"motion, no text, no captions, no logos, no watermark.")
    try:
        res = videogen.generate(prompt, "9:16" if p["format"] == "short" else "16:9", target,
                                label=f"#{p['id']} scene {idx + 1}")
    except videogen.VideoGenError as e:
        logs.event("warning", "videogen", f"Scene {idx + 1}: AI clip not made ({e}); using stock/original visuals",
                   production_id=p["id"])
        return None
    info = visuals.probe_video(target)
    return {"scene_index": idx, "kind": "video", "provider": videogen.PROVIDER, "provider_id": target.stem,
            "source_url": "", "page_url": "", "local_path": str(target), "license": "AI-generated (Google Gemini)",
            "license_url": "", "author": "Google Gemini Omni", "attribution": "", "width": info["width"],
            "height": info["height"], "duration": info["duration"]}


def video_look(p: dict[str, Any]) -> str:
    """'cartoon' for children's content, 'real' for everything else. A video takes the style of its own channel
    (a stand-alone video, of its own topic) — kids' visuals and market/factual visuals are never mixed."""
    from . import channels
    plan = p.get("request") if isinstance(p.get("request"), dict) else db.loads(p.get("request"), {}) or {}
    if plan.get("content_type") == "kids":
        return "cartoon"
    if plan.get("no_upload"):
        return "real"
    ch = channels.get(p.get("channel_id")) or {}
    if ch.get("workflow") == "nifty_daily":
        return "real"
    return "cartoon" if ch.get("made_for_kids") or "kids" in (ch.get("styles") or []) else "real"


def video_voice(p: dict[str, Any]) -> dict[str, Any] | None:
    """A cloned voice chosen for this video (or as its channel's narrator); None = the configured voice."""
    from .media import voiceclone
    req = p.get("request") if isinstance(p.get("request"), dict) else db.loads(p.get("request"), {}) or {}
    want = req.get("voice") or {}
    if not want and p.get("channel_id"):
        from . import channels
        want = (channels.get(p["channel_id"]) or {}).get("voice") or {}
    if want.get("engine") and want.get("engine") != "clone" and want.get("voice_id"):   # a chosen built-in voice
        match = next((v for v in tts.all_voices() if v["engine"] == want["engine"] and v["id"] == want["voice_id"]), None)
        if match:
            return {**match, "voice_id": match["id"], "speed": float(want.get("speed") or 1.0), "installed": True}
        return None
    if want.get("engine") != "clone":
        return None
    v = voiceclone.get(want.get("voice_id"), p.get("workspace_id") or 1)
    if not v:
        raise NeedsAttention("The cloned voice chosen for this video was deleted — pick another voice and retry.")
    return voiceclone.as_voice(v)


def stage_voice(p: dict[str, Any]) -> str | None:
    sarvam_before = tts._sarvam_chars["n"]
    own = video_voice(p)
    voice = own or tts.current_voice()
    if not voice.get("installed"):
        raise NeedsAttention("No TTS voice is installed/selected. Open Settings › Voice, download a voice and test it.")
    pkg, tl = p["package"], p["timeline"] or {}
    work = workdir(p["id"]) / "audio"
    work.mkdir(exist_ok=True)
    scenes = tl.get("scenes") or []
    key = f"{voice['engine']}:{voice['voice_id']}:{1.0 if own else settings.get('voice').get('speed')}"
    if voice["engine"] == "sarvam" and not tts.sarvam_status().get("ok", True):
        key += ":fallback"                          # audio made with the free voice is redone once Sarvam is back
    if tl.get("voice_key") != key:
        scenes = []
    for attempt in range(2):
        try:
            with tts.track_engines() as used:
                for idx, scene in enumerate(pkg["scenes"]):
                    wav = work / f"scene_{idx:03d}.wav"
                    if idx < len(scenes) and wav.exists() and scenes[idx].get("text") == scene["narration"]:
                        continue
                    info = tts.synthesize_scene(scene["narration"], wav, voice=own)
                    entry = {"index": idx, "wav": str(wav), "duration": info["duration"],
                             "sentences": info["sentences"], "text": scene["narration"]}
                    if idx < len(scenes):
                        scenes[idx] = entry
                    else:
                        scenes.append(entry)
            break
        except tts.VoiceSwitched:                   # Sarvam stopped mid-video: one voice for the whole video
            scenes = []
            key = key if key.endswith(":fallback") else key + ":fallback"
    scenes = scenes[:len(pkg["scenes"])]
    narration = workdir(p["id"]) / "narration.wav"
    tts.concat_wavs([Path(s["wav"]) for s in scenes], narration)
    vinfo = {k: voice.get(k) for k in ("engine", "voice_id", "license", "commercial", "lang")}
    if "fallback" in used:
        fb, _ = tts.fallback_voice(voice["voice_id"], " ".join(s["narration"] for s in pkg["scenes"])[:2000])
        vinfo = {"engine": "kokoro", "voice_id": fb, "license": "Apache-2.0", "commercial": True,
                 "lang": voice.get("lang"), "fallback_for": f"sarvam/{voice['voice_id']}"}
        key = key if key.endswith(":fallback") else key + ":fallback"
    tl.update({"scenes": scenes, "voice_key": key, "narration": str(narration), "voice": vinfo})
    save(p["id"], timeline=tl)
    paid = tts._sarvam_chars["n"] - sarvam_before
    logs.event("warning" if vinfo.get("fallback_for") else "info", "pipeline",
               f"Narration: {sum(s['duration'] for s in scenes):.1f}s with {vinfo['engine']}/{vinfo['voice_id']}"
               + (" — free fallback voice because Sarvam is unavailable" if vinfo.get("fallback_for") else "")
               + (f" ({tts.sarvam_cost_note(paid)})" if paid else ""), production_id=p["id"])
    return None


def _fmt_time(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def compose_description(p: dict[str, Any], pkg: dict[str, Any], tl: dict[str, Any], assets: list[dict[str, Any]]) -> str:
    parts = [pkg["description"].strip()]
    if p["format"] == "long" and tl.get("chapter_times") and len(tl["chapter_times"]) >= 3:
        parts.append("Chapters:\n" + "\n".join(f"{_fmt_time(t)} {c['title']}"
                                               for t, c in zip(tl["chapter_times"], pkg["chapters"])))
    ev = {e["id"]: e for e in _evidence(p)}
    cited = [f["evidence_id"] for f in pkg.get("fact_checks", []) if f.get("evidence_id") in ev]
    src_lines = [f"- {ev[i]['title'][:90]} — {ev[i]['url']}" for i in dict.fromkeys(cited)][:8]
    entries = visuals.credits(assets)
    required = [e["line"] for e in entries if e["required"]]
    optional = [e["line"] for e in entries if not e["required"]]
    if settings.get("render.music_file") and settings.get("render.music_license"):
        required.insert(0, f"Music: {settings.get('render.music_license')}")
    tail = []
    if settings.get("youtube.disclose_tts"):
        tail.append("Narration uses a synthetic (text-to-speech) voice.")
    if any(a.get("provider") == "gemini_video" for a in assets):
        tail.append("Some footage in this video is AI-generated (Google Gemini).")
    paragraphs = list(parts)

    def assemble(srcs: list[str], opt: list[str]) -> str:
        out = "\n\n".join(paragraphs)
        if srcs:
            out += "\n\nSources:\n" + "\n".join(srcs)
        credit_lines = required + opt
        if credit_lines:
            out += "\n\nCredits:\n" + "\n".join(f"- {c}" for c in credit_lines)
        if tail:
            out += "\n\n" + "\n".join(tail)
        return out.replace("<", "").replace(">", "")

    # Shed content in order of importance until it fits YouTube's 5,000-byte limit:
    # courtesy credits -> source links -> description text. Required attributions always stay.
    limit = 4900
    text = assemble(src_lines, optional)
    while len(text.encode()) > limit and optional:
        optional.pop()
        text = assemble(src_lines, optional)
    while len(text.encode()) > limit and src_lines:
        src_lines.pop()
        text = assemble(src_lines, optional)
    while len(text.encode()) > limit and len(paragraphs[0]) > 200:
        paragraphs[0] = paragraphs[0][: int(len(paragraphs[0]) * 0.8)].rsplit(" ", 1)[0] + "…"
        text = assemble(src_lines, optional)
    return text


def stage_render(p: dict[str, Any]) -> str | None:
    pkg, tl = p["package"], p["timeline"]
    fmt = p["format"]
    w, h = config.frame_size(fmt)
    pad = float(settings.get("render.transition"))
    work = workdir(p["id"])
    (work / "overlays").mkdir(exist_ok=True)
    (work / "clips").mkdir(exist_ok=True)
    style = settings.get("brand").get("caption_style", "bold")
    look = video_look(p)
    assets = {a["scene_index"]: a for a in db.q("SELECT * FROM assets WHERE production_id=?", (p["id"],))}
    from .creative import news
    news.make_presenter_talk(p, assets, tl, pad, work)    # news presenter speaks his lines (when the free GPU allows)
    wm_path = work / "overlays" / "watermark.png"
    wm = graphics.watermark_png(w, h, fmt, wm_path)
    ribbon = None
    if p.get("kind") in FINANCE_KINDS:
        from .finance import episode as fin_episode, lessons as fin_lessons
        mod = fin_lessons if p["kind"] == "fib_lesson" else fin_episode
        ribbon_path = work / "overlays" / "disclosure_ribbon.png"
        ribbon = {"path": ribbon_path, **mod.ribbon_png(p["episode"]["facts"], w, h, fmt, ribbon_path)}
    # Finance chart cards carry their own titles; a section title marks where each later chapter begins.
    section_titles = {ch["first_scene"] - 1: ch["title"] for ch in pkg.get("chapters", [])[1:]} \
        if p.get("kind") in FINANCE_KINDS and fmt == "long" else {}
    clips: list[tuple[Path, float]] = []
    all_caps: list[dict[str, Any]] = []
    srt_cues: list[dict[str, Any]] = []
    t0 = 0.0
    for idx, (scene, timing) in enumerate(zip(pkg["scenes"], tl["scenes"])):
        dur = timing["duration"]
        asset = dict(assets[idx])
        asset["is_card"] = asset["provider_id"].endswith("#card")
        cues = captions.scene_captions(scene, timing, style, fmt)
        overlays = []
        head = scene.get("on_screen_text") if not asset["is_card"] else section_titles.get(idx)
        if head:
            hp = work / "overlays" / f"s{idx:03d}_head.png"
            if asset["is_card"]:
                from .finance import charts as fin_charts
                pos = fin_charts.section_label_png(head, w, h, fmt, hp)
            else:
                pos = graphics.headline_png(head, w, h, fmt, hp)
            overlays.append({"path": hp, **pos, "start": 0.15, "end": min(dur, 3.8)})
        for k, c in enumerate(cues):
            cp = work / "overlays" / f"s{idx:03d}_c{k:02d}.png"
            pos = graphics.caption_png(c["text"], w, h, fmt, cp)
            overlays.append({"path": cp, **pos, "start": c["start"], "end": c["end"]})
            all_caps.append({"start": t0 + c["start"], "end": t0 + c["end"], "text": c["text"], "scene": idx})
        for c in captions.scene_captions(scene, timing, "subtle", "long"):
            srt_cues.append({"start": t0 + c["start"], "end": t0 + c["end"], "text": c["text"]})
        if wm:
            overlays.append({"path": wm_path, **wm, "start": 0, "end": dur + pad})
        if ribbon:
            overlays.append({**ribbon, "start": 0, "end": dur + pad})
        clip = work / "clips" / f"scene_{idx:03d}.mp4"
        sig = {"dur": round(dur, 3), "pad": pad, "asset": asset["local_path"], "kind": asset["kind"],
               "caps": [c["text"] for c in cues], "head": head, "style": style, "wm": bool(wm), "brand": settings.get("brand"),
               "ribbon": bool(ribbon), "look": look, "grade": settings.get("render.film_look")}
        sig_path = clip.with_suffix(".json")
        if not (clip.exists() and sig_path.exists() and json.loads(sig_path.read_text()) == json.loads(db.dumps(sig))):
            render.scene_clip(idx, asset, dur, pad, overlays, fmt, work / "clips", clip, f"p{p['id']}", look=look)
            sig_path.write_text(db.dumps(sig))
        clips.append((clip, dur))
        t0 += dur
    chapter_starts = set()
    chapter_times = []
    if fmt == "long" and pkg.get("chapters"):
        starts = [0.0]
        for s in tl["scenes"]:
            starts.append(starts[-1] + s["duration"])
        for ch in pkg["chapters"]:
            i = max(0, min(len(tl["scenes"]) - 1, ch["first_scene"] - 1))
            chapter_starts.add(i)
            chapter_times.append(int(starts[i]))
        if chapter_times:
            chapter_times[0] = 0
    joined = work / "video_noaudio.mp4"
    content_total = render.join(clips, pad, joined, fmt, f"p{p['id']}", chapter_starts - {0})
    total = content_total + pad
    final_tmp = work / "final.tmp.mp4"
    render.mix_audio_and_mux(joined, Path(tl["narration"]), total, final_tmp)
    final = work / "final.mp4"
    final_tmp.replace(final)
    srt = work / "captions.srt"
    srt.write_text(captions.to_srt(srt_cues), encoding="utf-8")
    tl.update({"captions": all_caps, "total_duration": total, "pad": pad, "chapter_times": chapter_times,
               "disclosure_ribbon": bool(ribbon)})
    if p.get("kind") in FINANCE_KINDS:
        chapters = [(_fmt_time(t), c["title"]) for t, c in zip(chapter_times, pkg.get("chapters", []))] \
            if len(chapter_times) >= 3 else None
        if p["kind"] == "fib_lesson":
            pkg["final_description"] = fin_lessons.describe(p["episode"]["facts"], chapters, tl.get("voice"))
        elif p["kind"] == "nifty_short":
            from .finance import shorts as fin_shorts
            parent = load(p["parent_id"])
            pkg["final_description"] = fin_shorts.describe(parent, p["episode"], parent.get("youtube_video_id"), tl.get("voice"))
        else:
            pkg["final_description"] = fin_episode.compose_description(p["episode"]["facts"], p["episode"]["disclosure"],
                                                                       chapters, tl.get("voice"))
    else:
        pkg["final_description"] = compose_description(p, pkg, tl, assets_for(p["id"]))
    save(p["id"], timeline=tl, package=pkg, video_path=str(final), srt_path=str(srt))
    logs.event("info", "pipeline", f"Rendered {total:.1f}s video ({final.stat().st_size / 1e6:.1f} MB)", production_id=p["id"])
    return None


def stage_thumbnail(p: dict[str, Any]) -> str | None:
    work = workdir(p["id"])
    info = thumbnail.make(p["package"], assets_for(p["id"]), p["format"], work, work / "thumbnail.jpg", f"p{p['id']}")
    tl = p["timeline"]
    tl["thumbnail"] = info
    save(p["id"], timeline=tl, thumbnail_path=info["path"])
    return None


def _is_finance_video(p: dict[str, Any]) -> bool:
    """Market/finance content: a finance channel, or a video planned as finance."""
    from . import channels
    ch = channels.get(p.get("channel_id")) or {}
    plan = db.loads(p.get("request"), {}) or {}
    niche = f"{ch.get('niche') or ''} {ch.get('sub_niche') or ''}".lower()
    return (ch.get("workflow") == "nifty_daily" or plan.get("content_type") == "finance"
            or any(w in niche for w in ("stock", "market", "trading", "invest", "nifty", "finance", "crypto")))


def _viral(p: dict[str, Any], pkg: dict[str, Any], tl: dict[str, Any], result: dict[str, Any]) -> None:
    """Viral engine for researched videos: the best of 5 scored titles (not on finance videos, whose wording is
    held to the compliance rules), then the viral checks — warnings that never block the video."""
    from .creative import viral
    from . import channels
    ch = channels.get(p.get("channel_id")) or {}
    kids = bool(ch.get("made_for_kids"))
    if not _is_finance_video(p) and not pkg.get("title_variants") and not p["test_mode"]:
        summary = f"{pkg.get('hook', '')} " + " ".join(sc.get("narration", "") for sc in pkg.get("scenes", [])[:6])
        best = viral.titles(summary, pkg.get("title") or p["title"], p.get("language") or "Hindi", kids=kids,
                            fmt=p["format"])
        if best["variants"] and best["title"] != pkg.get("title"):
            pkg["title_variants"], pkg["title_original"] = best["variants"], pkg.get("title")
            pkg["title"] = best["title"]
            save(p["id"], package=pkg, title=best["title"])
    first = min((float(x.get("start", 0)) for x in (tl.get("voice", {}) or {}).get("segments", [])), default=0.0)
    result["checks"] += viral.check(kind="researched", fmt=p["format"], seconds=float(result.get("duration") or 0),
                                    title=pkg.get("title") or p["title"] or "",
                                    thumb_text=(pkg.get("thumbnail") or {}).get("text", "") if isinstance(
                                        pkg.get("thumbnail"), dict) else "", opening_delay=first, kids=kids)


def stage_qa(p: dict[str, Any]) -> str | None:
    pkg, tl = p["package"], p["timeline"]
    result = qa.run(p, pkg, tl, assets_for(p["id"]), tl.get("thumbnail"), tl.get("voice", {}),
                    pkg.get("review"), pkg.get("originality"))
    if settings.get("compliance.finance_lint_all") and _is_finance_video(p):
        # Market-education videos get the same language guard rails as the NIFTY episodes. On a regular video
        # it is a warning: the video is ready for you to review/download/upload, but never published
        # automatically while such wording is in it.
        from .finance import lint as fin_lint
        # Check what the channel says — not the headlines and links of the articles it cites as sources
        desc = (pkg.get("final_description") or "").split("\n\nSources:\n")[0]
        text = "\n".join([pkg.get("title", ""), desc] +
                         [s["narration"] + "\n" + s.get("on_screen_text", "") for s in pkg.get("scenes", [])])
        found = fin_lint.prohibited_language(text)
        result["checks"].append({"name": "financial-recommendation language", "ok": not found, "severity": "warn",
                                 "detail": "none found" if not found else
                                 "; ".join(f"{f['why']}: '{f['match']}'" for f in found[:5])
                                 + " — review the wording before publishing; it won't be auto-published"})
        result["auto_publish_ok"] = result["auto_publish_ok"] and not found
    _viral(p, pkg, tl, result)
    save(p["id"], qa=result)
    from .youtube import oauth
    if p["test_mode"]:
        # the built-in sample is ~30 s, so the long-form minimum length is not meaningful here
        others = [c["name"] for c in result["checks"] if not c["ok"] and c["severity"] == "fail"
                  and c["name"] not in ("test mode", "long-form length")]
        if not others:
            save(p["id"], status="test_passed", error=None)
            logs.event("info", "pipeline", "Self-test passed: render, audio, captions, thumbnail and QA all work",
                       production_id=p["id"])
            return None
    if not result["passed"]:
        failing = "; ".join(c["name"] for c in result["checks"] if not c["ok"] and c["severity"] == "fail")
        raise NeedsAttention(f"Quality checks failed: {failing}")
    from . import channels
    chan = channels.get(p.get("channel_id"))
    if chan and chan.get("auto_publish") and chan.get("status") == "active" and oauth.connected(chan["id"]) \
            and result["auto_publish_ok"] and not p["test_mode"]:
        approve(p["id"], privacy=chan.get("publish_privacy") or "private", publish_at=None,
                by="auto (channel auto-publish)")
        return None
    auto = (settings.get("publish.mode") == "auto_after_validation" and result["auto_publish_ok"]
            and oauth.connected() and not p["test_mode"])
    if auto:
        public_ok = settings.get("youtube.audit_passed") and not nc_voice(tl)
        approve(p["id"], privacy="scheduled" if public_ok else "private",
                publish_at=(pkg.get("publish_suggestion") or {}).get("utc"), by="auto")
    else:
        save(p["id"], status="awaiting_approval", error=None)
        logs.event("info", "pipeline", "Ready for your review and approval", production_id=p["id"])
    return None


STAGE_FN = {"research": stage_research, "script": stage_script, "review": stage_review, "assets": stage_assets,
            "voice": stage_voice, "render": stage_render, "thumbnail": stage_thumbnail, "qa": stage_qa}


def stages_for(kind: str) -> tuple[list[str], dict[str, str], dict[str, Any]]:
    """Stage list, status labels and stage functions for a production kind."""
    if kind == "nifty_pivot":
        from .finance import episode as fin_episode
        fns = {**STAGE_FN, **fin_episode.STAGE_FN}
        return fin_episode.STAGES, fin_episode.STATUS, fns
    if kind == "nifty_short":
        from .finance import shorts as fin_shorts
        return fin_shorts.STAGES, fin_shorts.STATUS, {**STAGE_FN, **fin_shorts.STAGE_FN}
    if kind == "fib_lesson":
        from .finance import lessons as fin_lessons
        return fin_lessons.STAGES, fin_lessons.STATUS, {**STAGE_FN, **fin_lessons.STAGE_FN}
    if kind in ("edit", "edit_clip"):
        from .editing import workflow as editing
        return (editing.STAGES if kind == "edit" else editing.CLIP_STAGES), editing.STATUS, editing.STAGE_FN
    if kind == "film":
        from .creative import film
        return film.STAGES, film.STATUS, film.STAGE_FN
    if kind in ("story", "story_short"):
        from .creative import workflow as story
        return (story.STAGES if kind == "story" else story.SHORT_STAGES), story.STATUS, story.STAGE_FN
    return STAGES, STATUS, STAGE_FN


def run_production(payload: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    pid = int(payload["production_id"])
    p = load(pid)
    if p["status"] in TERMINAL and p["status"] not in ("paused_budget",):
        return {"skipped": f"status is {p['status']}"}
    from . import channels
    lang = p.get("language") or (channels.get(p.get("channel_id")) or {}).get("language") or \
        profile.language_for(p.get("audience") or "india")
    from . import plans
    with channels.using(p.get("channel_id") or 1), profile.using_language(lang), \
            plans.using(p.get("workspace_id") or 1):
        return _run_stages(pid, p)


def _run_stages(pid: int, p: dict[str, Any]) -> dict[str, Any]:
    stages, status, fns = stages_for(p.get("kind") or "general")
    stage = p["stage"] if p["stage"] in stages else stages[0]
    guard = 0
    while stage:
        guard += 1
        if guard > 20:
            raise NeedsAttention("stage loop guard tripped")
        if load(pid)["status"] == "paused":            # paused while a step ran: stop here, resume picks it up
            raise PausedByOwner(f"paused before '{stage}'")
        if load(pid)["status"] == "rejected":          # rejected while running: stop, never overwrite the decision
            break
        save(pid, status=status[stage], stage=stage, error=None)
        p = load(pid)
        override = fns[stage](p)
        if override:
            stage = override
            continue
        if load(pid)["status"] in ("needs_review",):
            break
        nxt = stages.index(stage) + 1
        if nxt >= len(stages):
            break
        stage = stages[nxt]
        save(pid, stage=stage)
    return {"production_id": pid, "status": load(pid)["status"]}


# ------------------------------------------------------------------ approval & upload

def nc_voice(timeline: dict[str, Any] | None) -> bool:
    """True when narration used a voice trained on non-commercial data (e.g. Piper hi_IN-pratham)."""
    return ((timeline or {}).get("voice") or {}).get("commercial") is False


def _nc_voice_guard(p: dict[str, Any], privacy: str) -> None:
    # User decision 2026-10-23: non-commercial voices are for private test uploads only, never public.
    from .finance import gate
    if nc_voice(p.get("timeline")) and privacy != "private" and not gate.owner_override():
        raise NeedsAttention(f"Voice '{p['timeline']['voice'].get('voice_id')}' is non-commercial — "
                             "private uploads only. Re-voice with a Kokoro voice to publish.")


def approve(pid: int, *, privacy: str, publish_at: str | None, by: str = "user") -> None:
    from . import worker
    p = load(pid)
    if p["test_mode"]:
        raise NeedsAttention("Test-mode productions cannot be uploaded.")
    if not p["qa"].get("passed"):
        raise NeedsAttention("QA has not passed for this production.")
    from . import channels
    ch = channels.get(p.get("channel_id"))
    if ch and not channels.uploads_allowed(ch):
        msg = f"Uploads are switched off for '{ch['name']}' — the video is kept here, not uploaded."
        if by.startswith("auto"):                   # automation never uploads to a switched-off channel
            save(pid, status="awaiting_approval", error=msg)
            logs.event("info", "pipeline", msg, production_id=pid)
            return
        raise NeedsAttention(msg + " Switch uploads on for that channel first.")
    from . import plans
    if not plans.can(p.get("workspace_id") or 1, "publish"):
        msg = "Publishing to YouTube isn't enabled for this account — the video is kept here for download."
        if by.startswith("auto"):
            save(pid, status="awaiting_approval", error=msg)
            return
        raise NeedsAttention(msg)
    if by.startswith("auto") and ch and privacy == "public" and slot_for(p, ch):
        privacy, publish_at = "scheduled", slot_for(p, ch)                # goes public at the channel's time
    if by.startswith("auto") and (db.loads(p.get("request"), {}) or {}).get("no_upload"):
        save(pid, status="awaiting_approval", error="Ready to download — made without a channel, so it isn't "
                                                    "uploaded automatically.")
        return
    _nc_voice_guard(p, privacy)
    if p.get("kind") in FINANCE_KINDS:
        from .finance import audit, gate
        allowed, why = gate.permits(p["compliance"], privacy)
        audit.record("approval_requested", {"privacy": privacy, "publish_at": publish_at, "allowed": allowed,
                                            "reason": why, "gate_status": p["compliance"].get("status")},
                     production_id=pid, actor=by)
        if not allowed:
            raise NeedsAttention(f"Blocked by the compliance gate: {why}")
    save(pid, status="approved", approved_at=db.iso(), privacy=privacy, publish_at=publish_at)
    worker.enqueue("upload", {"production_id": pid}, production_id=pid, max_attempts=8)
    logs.event("info", "pipeline", f"Approved by {by} for upload ({privacy}"
               + (f", publish at {publish_at}" if publish_at and privacy == 'scheduled' else "") + ")", production_id=pid)


def slot_for(p: dict[str, Any], ch: dict[str, Any]) -> str | None:
    """When this video goes public: the channel's next free daily slot; a Short never before its full episode."""
    from . import channels
    slot = channels.next_publish_slot(ch, p.get("kind") not in channels.SHORT_KINDS)
    if slot and p.get("parent_id"):
        parent = db.q1("SELECT publish_at, privacy FROM productions WHERE id=?", (p["parent_id"],))
        if parent and parent["privacy"] == "scheduled" and parent["publish_at"] and parent["publish_at"] > slot:
            slot = parent["publish_at"]
    return slot


def run_upload(payload: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    from . import channels
    p = load(int(payload["production_id"]))
    with channels.using(p.get("channel_id") or 1):          # this channel's YouTube authorization only
        return _run_upload(payload, task)


def _run_upload(payload: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    from . import channels
    from .youtube import oauth, upload
    pid = int(payload["production_id"])
    p = load(pid)
    if p["status"] not in ("approved", "uploading"):
        return {"skipped": f"status is {p['status']}"}
    from . import channels as _ch
    chan = _ch.get(p.get("channel_id"))
    if chan and not _ch.uploads_allowed(chan):           # switched off after approval: hold, don't upload
        save(pid, status="awaiting_approval", error=f"Uploads are switched off for '{chan['name']}' — not uploaded.")
        return {"held": "uploads switched off for this channel"}
    if p.get("youtube_video_id"):
        # Never upload the same production twice (e.g. a re-approval after a finished upload)
        save(pid, status="uploaded", error=None)
        return {"skipped": f"already uploaded as https://youtu.be/{p['youtube_video_id']}"}
    if not oauth.connected():
        save(pid, status="approved", error="This channel's YouTube account is not connected — connect it on the "
                                            "Channels page, then retry the upload.")
        raise NeedsAttention("YouTube is not connected for this channel")
    try:                                    # verify the destination channel before anything is sent
        dest = oauth.verify_destination(channels.current_id() or 1)
    except oauth.AuthError as e:
        save(pid, status="approved", error=f"Upload not started: {e}")
        raise NeedsAttention(f"Upload not started: {e}") from e
    _nc_voice_guard(p, p["privacy"] or "private")
    if p.get("kind") in ("nifty_short", "story_short") and p.get("parent_id"):
        # The full episode goes out first; the Short follows and links to it
        from . import worker
        parent = load(p["parent_id"])
        if not parent.get("youtube_video_id"):
            stale = p.get("approved_at") and p["approved_at"] < db.iso_in(-24 * 3600)
            if parent["status"] in ("rejected", "needs_review", "failed", "paused") or stale:
                save(pid, status="awaiting_approval",
                     error=f"Held: the full episode #{parent['id']} was not published, so this Short is not published either.")
                return {"held": f"full episode #{parent['id']} is {parent['status']}"}
            save(pid, status="approved", error=f"Waiting for the full episode #{parent['id']} to publish first "
                                               "(checking every 10 minutes).")
            worker.enqueue("upload", {"production_id": pid}, production_id=pid, delay=600, max_attempts=8)
            return {"waiting_for_parent": parent["id"]}
        if p["kind"] == "nifty_short":
            from .finance import shorts as fin_shorts
            p["package"]["final_description"] = fin_shorts.describe(parent, p["episode"], parent.get("youtube_video_id"),
                                                                    (p.get("timeline") or {}).get("voice"))
        else:
            from .creative import workflow as story
            p["package"]["final_description"] = story.describe(p, parent.get("youtube_video_id"))
        save(pid, package=p["package"])
    if p.get("kind") in FINANCE_KINDS:
        # Defence in depth: re-run the gate right before anything leaves the machine.
        from .finance import audit, gate
        fresh = gate.run(p, assets_for(pid))
        save(pid, compliance=fresh)
        allowed, why = gate.permits(fresh, p["privacy"] or "private")
        audit.log_artifact("pre_upload_gate", pid, "pre_upload_gate", fresh,
                           extra={"privacy": p["privacy"], "allowed": allowed, "reason": why})
        if not allowed:
            save(pid, status="needs_review", error=f"NEEDS REVIEW — upload blocked: {why}")
            raise NeedsAttention(f"Upload blocked by the compliance gate: {why}")
    if p.get("privacy") == "scheduled" and p.get("publish_at") and p["publish_at"] <= db.iso_in(15 * 60):
        # the slot passed while it waited (upload limit, pause, retry): YouTube refuses past times, so take the
        # channel's next slot (or publish now when the channel has no fixed time)
        nxt = slot_for(p, chan) if chan else None
        if nxt:
            p["publish_at"] = nxt
            save(pid, publish_at=nxt)
            logs.event("info", "pipeline", f"Scheduled time passed while waiting — moved to {nxt} (UTC)", production_id=pid)
        else:
            p["privacy"] = "public"
            save(pid, privacy="public", publish_at=None)
    save(pid, status="uploading")
    with profile.using_language(p.get("language")):          # sets YouTube language metadata correctly
        res = upload.upload(p, p["package"], assets_for(pid))
    res["destination"] = dest
    save(pid, status="uploaded", error=None)
    logs.event("info", "youtube", f"Uploaded to YouTube channel '{dest['title']}' ({dest['id']})", production_id=pid)
    if p.get("kind") == "nifty_short" and p.get("parent_id"):
        from .finance import shorts as fin_shorts
        fin_shorts.write_manifest(p["parent_id"])
    if p.get("kind") in FINANCE_KINDS:
        audit.record("uploaded", {"video_id": res.get("video_id"), "privacy_requested": p["privacy"],
                                  "notes": res.get("notes")}, production_id=pid)
    if p.get("topic_id"):
        db.update("topics", p["topic_id"], {"status": "produced", "updated_at": db.iso()})
    for kid in db.q("SELECT id, privacy FROM productions WHERE parent_id=? AND status='awaiting_approval' "
                    "AND error LIKE 'Held: the full episode%'", (pid,)):
        try:                                        # Shorts held back for this episode can follow it now
            approve(kid["id"], privacy="private" if kid["privacy"] == "private" else "public", publish_at=None,
                    by="auto (episode published)")
        except NeedsAttention as e:
            logs.event("warning", "pipeline", f"Short #{kid['id']} still held: {e}", production_id=kid["id"])
    return res


def reset_to_stage(pid: int, stage: str) -> None:
    from . import worker
    p = load(pid)
    stages, _, _ = stages_for(p.get("kind") or "general")
    if stage not in stages:
        raise ValueError(stage)
    work = workdir(pid)
    pos = stages.index(stage)
    if "assets" in stages and pos <= stages.index("assets"):
        db.ex("DELETE FROM assets WHERE production_id=?", (pid,))
    if "voice" in stages and pos <= stages.index("voice"):
        save(pid, timeline={})
        shutil.rmtree(work / "voice", ignore_errors=True)       # story dialogue audio is cached per line
    if "render" in stages and pos <= stages.index("render"):
        shutil.rmtree(work / "clips", ignore_errors=True)
    if stage in ("research", "script", "data"):
        pkg = {"evidence_ids": p["package"].get("evidence_ids", [])} if stage == "script" and p.get("kind") not in (
            "nifty_pivot", "story") else {}
        save(pid, package=pkg)
    save(pid, stage=stage, status="queued", error=None, qa={}, compliance={})
    if p.get("kind") in FINANCE_KINDS:
        from .finance import audit
        audit.record("episode_rerun_requested", {"from_stage": stage}, production_id=pid, actor="user")
    worker.enqueue("production", {"production_id": pid}, production_id=pid, max_attempts=4,
                   dedupe=True)                  # clicking Retry again doesn't queue the same video twice


# ------------------------------------------------------------------ built-in self-test package

def sample_package(fmt: str) -> dict[str, Any]:
    hindi = profile.tts_language() == "hi"
    if hindi:
        scenes = [
            ("आसमान नीला क्यों दिखता है? जवाब आपकी सोच से ज़्यादा दिलचस्प है।", "आसमान नीला क्यों?", "text_card"),
            ("सूरज की रोशनी में असल में सारे रंग छुपे होते हैं।", "सारे रंग एक साथ", "illustration"),
            ("जब यह रोशनी हवा से टकराती है, तो नीला रंग सबसे ज़्यादा बिखरता है।", "नीला सबसे ज़्यादा बिखरता है", "illustration"),
            ("इसी बिखराव को रेले स्कैटरिंग कहते हैं।", "रेले स्कैटरिंग", "text_card"),
            ("शाम को रोशनी लंबा रास्ता तय करती है, इसलिए आसमान नारंगी हो जाता है।", "शाम का नारंगी आसमान", "illustration"),
            ("अगली बार आसमान देखें, तो याद रखिए — आप रोशनी का खेल देख रहे हैं।", "", "illustration"),
        ]
        title, desc, thumb = "आसमान नीला क्यों होता है? 60 सेकंड में समझिए", "सूरज की रोशनी और हवा के कणों का दिलचस्प खेल।", "नीला क्यों?"
    else:
        scenes = [
            ("Why is the sky blue? The answer is stranger than you think.", "Why is the sky blue?", "text_card"),
            ("Sunlight looks white, but it secretly carries every colour.", "Every colour at once", "illustration"),
            ("When that light hits the air, blue gets scattered the most.", "Blue scatters most", "illustration"),
            ("Scientists call this Rayleigh scattering.", "Rayleigh scattering", "text_card"),
            ("At sunset, light travels further through the air, so the sky turns orange.", "Sunset orange", "illustration"),
            ("Next time you look up, remember: you are watching light at play.", "", "illustration"),
        ]
        title, desc, thumb = "Why Is the Sky Blue? Explained in 60 Seconds", "A quick look at how sunlight and air create the colour of the sky.", "WHY BLUE?"
    return {
        "hook": scenes[0][0], "title": title, "description": desc + " (pipeline self-test)", "tags": ["science", "sky", "physics", "light", "test"],
        "scenes": [{"narration": n, "caption_text": n, "visual_description": "abstract", "visual_query": "sky light",
                    "visual_type": v, "on_screen_text": o} for n, o, v in scenes],
        "chapters": [], "thumbnail": {"concept": "sky gradient", "text": thumb},
        "fact_checks": [{"claim": "Blue light scatters more than red light in air", "status": "general_knowledge",
                         "evidence_id": 0, "note": "textbook physics"}],
        "evidence_ids": [], "originality": {"ok": True, "copied_7gram_ratio": 0.0, "closest_competitor_title_similarity": 0.0},
        "review": {"issues": [], "overall": "self-test sample", "ready": True},
        "publish_suggestion": content.suggest_publish_time(fmt),
    }
