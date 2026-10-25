"""Animated story production: request -> screenplay -> characters -> voices -> animation -> mix -> QA -> publish.

kind 'story'        long episode or stand-alone Short, rendered from its screenplay
kind 'story_short'  a vertical Short re-staged from one scene of a finished episode (same voices/audio,
                    re-laid-out for 9:16 — not a centre crop), linked to its episode via parent_id
"""
from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path
from typing import Any

import numpy as np

from .. import channels, config, db, logs, settings
from ..media import tts
from . import cartoon, characters as charmod, music, request as reqmod, screenplay

STAGES = ["script", "characters", "voice", "animate", "thumbnail", "qa", "review"]
SHORT_STAGES = ["animate", "thumbnail", "qa", "review"]
STATUS = {"script": "generating script", "characters": "generating assets", "voice": "generating assets",
          "animate": "animating", "thumbnail": "editing", "qa": "quality review", "review": "quality review"}
SR = 24000
LEAD, GAP, TAIL = 0.35, 0.28, 0.7
KOKORO_FALLBACK = {"male": "hm_omega", "female": "hf_alpha"}
CHAR_COLOURS = [(255, 214, 0), (120, 220, 255), (255, 140, 180), (160, 240, 140), (255, 170, 90), (200, 160, 255)]


def workdir(pid: int) -> Path:
    d = config.PRODUCTIONS_DIR / f"{pid:05d}"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------------ creation

def create(channel_id: int, request: str, *, idea_id: int | None = None, fmt: str | None = None,
           actor: str = "user", scheduled_for: str | None = None, plan: dict[str, Any] | None = None) -> int:
    from .. import worker
    ch = channels.get(channel_id)
    if not ch:
        raise ValueError("channel not found")
    idea = db.q1("SELECT * FROM topics WHERE id=?", (idea_id,)) if idea_id else None
    plan = dict(plan) if plan else reqmod.plan(request, ch, idea)
    if fmt in ("long", "short", "both"):
        plan["format"] = fmt
    plan["scheduled_for"] = scheduled_for
    plan["make_shorts"] = plan["format"] == "both"
    out_fmt = "short" if plan["format"] == "short" else "long"
    title = ((plan.get("reference") or {}).get("title") or request or (idea or {}).get("title")
             or f"{plan['content_type'].title()} episode")[:90]
    pid = db.insert("productions", {
        "topic_id": idea_id, "format": out_fmt, "title": title, "status": "queued", "stage": "script", "kind": "story",
        "channel_id": channel_id, "workspace_id": ch["workspace_id"], "language": plan["language"],
        "request": json.dumps(plan, ensure_ascii=False),
        "package": "{}", "timeline": "{}", "episode": "{}", "test_mode": 0, "created_at": db.iso(),
        "updated_at": db.iso()})
    if idea_id:
        db.update("topics", idea_id, {"status": "producing", "updated_at": db.iso()})
    worker.enqueue("production", {"production_id": pid}, production_id=pid, max_attempts=3)
    logs.event("info", "creative", f"{plan['content_type']} {plan['format']} queued for '{ch['name']}' "
               f"(decided by {plan['decided_by']})", production_id=pid)
    return pid


def plan_of(p: dict[str, Any]) -> dict[str, Any]:
    return db.loads(p.get("request"), {}) or {}


# ------------------------------------------------------------------ stages

def stage_script(p: dict[str, Any]) -> None:
    from .. import pipeline
    ch = channels.get(p["channel_id"])
    plan = plan_of(p)
    idea = db.q1("SELECT * FROM topics WHERE id=?", (p["topic_id"],)) if p.get("topic_id") else None
    sp = screenplay.write(plan, ch, idea)
    screenplay.save_new_characters(ch["id"], sp, p["id"])
    pkg = {**sp, "plan": plan}
    pipeline.save(p["id"], package=pkg, title=sp["title"])
    logs.event("info", "creative", f"Screenplay: {len(sp['scenes'])} scenes, {sp['words']} words — '{sp['title']}'",
               production_id=p["id"])


def stage_characters(p: dict[str, Any]) -> None:
    """Resolve every speaker to an approved bible character and freeze their design + voice for this episode."""
    from .. import pipeline
    pkg = p["package"]
    cast: dict[str, Any] = {}
    for i, key in enumerate(pkg["cast"]):
        c = charmod.get(int(key[1:])) if key.startswith("C") and key[1:].isdigit() else None
        if not c or c["channel_id"] != p["channel_id"]:
            raise pipeline.NeedsAttention(f"character {key} is not in this channel's bible")
        if not c.get("reference_path") or not Path(c["reference_path"]).exists():
            charmod.render_reference(c["id"])
            c = charmod.get(c["id"])
        cast[key] = {"id": c["id"], "name": c["name"], "design": c["design"], "voice": c["voice"],
                     "reference": c["reference_path"], "colour": CHAR_COLOURS[i % len(CHAR_COLOURS)]}
    ep = {**(p.get("episode") or {}), "cast": cast}
    pipeline.save(p["id"], episode=json.dumps(ep, ensure_ascii=False))


def _voice_for(speaker: str, cast: dict[str, Any], ch: dict[str, Any]) -> tuple[str, str, float]:
    if speaker in cast:
        v = cast[speaker]["voice"]
        gender = cast[speaker]["design"]["gender"]
    else:                                                    # narrator = the channel's voice
        v = ch.get("voice") or {}
        gender = "female"
    engine, voice_id, pace = v.get("engine", "sarvam"), v.get("voice_id", "ritu"), float(v.get("pace", 1.0))
    if not v.get("voice_id"):
        base = tts.voice_settings()
        engine, voice_id = base.get("engine", "sarvam"), base.get("voice_id", "ritu")
    if engine == "sarvam" and not settings.secret("sarvam_api_key"):
        engine, voice_id = "kokoro", KOKORO_FALLBACK[gender]
    return engine, voice_id, pace


def _resample(a: np.ndarray, sr: int) -> np.ndarray:
    if sr == SR or len(a) == 0:
        return a
    x = np.linspace(0, len(a) - 1, int(len(a) * SR / sr))
    return np.interp(x, np.arange(len(a)), a.astype(np.float32)).astype(np.int16)


def stage_voice(p: dict[str, Any]) -> None:
    from .. import pipeline
    ch = channels.get(p["channel_id"])
    cast = p["episode"]["cast"]
    work = workdir(p["id"]) / "voice"
    work.mkdir(exist_ok=True)
    lang = "en" if p.get("language") == "English" else "hi"
    paid_before = tts._sarvam_chars["n"]
    for attempt in range(2):
        lines_out, reused = [], 0
        try:
            with tts.track_engines() as used:
                for si, sc in enumerate(p["package"]["scenes"]):
                    for li, ln in enumerate(sc["lines"]):
                        wav = work / f"s{si:02d}_l{li:02d}.wav"
                        if wav.exists():
                            reused += 1
                        else:
                            engine, voice_id, pace = _voice_for(ln["speaker"], cast, ch)
                            audio, sr = tts._synth_sentence(screenplay.clean_text(ln["text"]), engine, voice_id,
                                                            lang, pace)
                            audio = _resample(audio, sr)
                            with wave.open(str(wav), "wb") as w:
                                w.setnchannels(1)
                                w.setsampwidth(2)
                                w.setframerate(SR)
                                w.writeframes(audio.tobytes())
                        with wave.open(str(wav), "rb") as w:
                            dur = w.getnframes() / w.getframerate()
                        lines_out.append({"scene": si, "line": li, "wav": str(wav), "duration": round(dur, 3)})
                if "fallback" in used and reused:   # earlier lines may be Sarvam: redo them all
                    raise tts.VoiceSwitched("mixed voices after a retry")
            break
        except tts.VoiceSwitched:                   # Sarvam stopped mid-episode: every character re-voiced consistently
            for f in work.glob("*.wav"):
                f.unlink()
    tl = {**(p.get("timeline") or {}), "lines": lines_out, "voice_fallback": "fallback" in used}
    if "fallback" in used:
        logs.event("warning", "creative", "Voices made with the free Kokoro fallback because Sarvam is unavailable",
                   production_id=p["id"])
    pipeline.save(p["id"], timeline=tl)
    paid = tts._sarvam_chars["n"] - paid_before
    logs.event("info", "creative", f"Voices: {len(lines_out)} lines" + (f" ({tts.sarvam_cost_note(paid)})" if paid else ""),
               production_id=p["id"])


def _read(wav: str) -> np.ndarray:
    with wave.open(wav, "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).copy()


def _ffmpeg(args: list[str], timeout: int = 3600) -> None:
    r = subprocess.run([config.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True,
                       text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg: {r.stderr[-500:]}")


def _srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def _render(p: dict[str, Any], fmt: str, scene_ids: list[int], audio_lines: dict[tuple[int, int], dict[str, Any]],
            out_dir: Path, *, title_card: bool, end_card: bool, hook: str = "") -> dict[str, Any]:
    """Draw and encode the scenes, build the dialogue track, mix music, mux. Returns timing info."""
    pkg = p["package"]
    cast = p["episode"]["cast"]
    ch = channels.get(p["channel_id"])
    hindi = (p.get("language") or "Hindi") != "English"
    out_dir.mkdir(parents=True, exist_ok=True)
    silent = out_dir / "video_noaudio.mp4"
    enc = cartoon.Encoder(silent, fmt)
    try:
        return _draw_and_mix(p, fmt, scene_ids, audio_lines, out_dir, enc, silent, title_card=title_card,
                             end_card=end_card, hook=hook)
    except Exception:
        enc.abort()
        raise


def _draw_and_mix(p, fmt, scene_ids, audio_lines, out_dir, enc, silent, *, title_card, end_card, hook):
    pkg = p["package"]
    cast = p["episode"]["cast"]
    ch = channels.get(p["channel_id"])
    hindi = (p.get("language") or "Hindi") != "English"
    voice = []
    srt = []
    scenes_t = []
    t0 = 0.0
    cast_list = [cast[k] for k in pkg["cast"] if k in cast]

    def pad(seconds: float) -> None:
        voice.append(np.zeros(int(SR * seconds), dtype=np.int16))

    if title_card:
        d = 2.6
        cartoon.render_card([(pkg["title"], 58, (255, 230, 40)), (ch["name"], 30, (255, 255, 255))], fmt, d, enc,
                            cast=cast_list, mood=pkg["scenes"][0].get("mood", "funny"))
        pad(d)
        t0 += d
    for si in scene_ids:
        sc = pkg["scenes"][si]
        lines, cur = [], LEAD
        scene_audio = [np.zeros(int(SR * LEAD), dtype=np.int16)]
        for li, ln in enumerate(sc["lines"]):
            a = _read(audio_lines[(si, li)]["wav"])
            dur = len(a) / SR
            env = cartoon.envelope(a, SR, int(dur * cartoon.FPS) + 1)
            lines.append({"speaker": ln["speaker"], "text": screenplay.clean_text(ln["text"]), "emotion": ln["emotion"],
                          "start": cur, "end": cur + dur, "env": env})
            srt.append((t0 + cur, t0 + cur + dur, (cast.get(ln["speaker"], {}).get("name", "") + ": " if
                                                   ln["speaker"] in cast else "") + screenplay.clean_text(ln["text"])))
            scene_audio += [a, np.zeros(int(SR * GAP), dtype=np.int16)]
            cur += dur + GAP
        total = cur - GAP + TAIL
        scene_audio.append(np.zeros(int(SR * TAIL), dtype=np.int16))
        a = np.concatenate(scene_audio)
        frames = int(round(total * cartoon.FPS))
        a = np.pad(a, (0, max(0, int(frames / cartoon.FPS * SR) - len(a))))[: int(frames / cartoon.FPS * SR)]
        cartoon.render_scene(sc, cast, lines, fmt, enc, frames / cartoon.FPS, hook=hook if si == scene_ids[0] else "")
        voice.append(a)
        scenes_t.append({"scene": si, "start": round(t0, 2), "end": round(t0 + frames / cartoon.FPS, 2)})
        t0 += frames / cartoon.FPS
    if end_card:
        d = 3.2
        bye = "फिर मिलेंगे अगले एपिसोड में — सब्सक्राइब करना न भूलें!" if hindi else "See you in the next episode — subscribe!"
        last_mood = pkg["scenes"][scene_ids[-1]].get("mood", "funny")
        cartoon.render_card([(bye, 40, (255, 255, 255)), (ch["name"], 30, (255, 214, 0))], fmt, d, enc, cast=cast_list,
                            mood=last_mood if last_mood in ("scary", "tense", "sad") else "happy")
        pad(d)
        t0 += d
    enc.close()
    total = enc.frames / cartoon.FPS
    track = np.concatenate(voice)
    track = np.pad(track, (0, max(0, int(total * SR) - len(track))))[: int(total * SR)]
    dialog = out_dir / "dialogue.wav"
    with wave.open(str(dialog), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(track.tobytes())
    moods = [pkg["scenes"][i].get("mood", "funny") for i in scene_ids]
    bed = music.bed(max(set(moods), key=moods.count), total, out_dir / "music.wav")
    final = out_dir / "final.mp4"
    _ffmpeg(["-i", str(silent), "-i", str(dialog), "-i", str(bed), "-filter_complex",
             "[2:a]volume=0.11[m];[1:a][m]amix=inputs=2:duration=first:dropout_transition=0,"
             "loudnorm=I=-15:TP=-1.5:LRA=11[a]", "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
             "-b:a", "160k", "-ar", "48000", "-shortest", str(final)])
    srt_path = out_dir / "captions.srt"
    srt_path.write_text("\n".join(f"{i}\n{_srt_time(a)} --> {_srt_time(b)}\n{t}\n" for i, (a, b, t) in enumerate(srt, 1)),
                        encoding="utf-8")
    silent.unlink(missing_ok=True)
    return {"final": str(final), "srt": str(srt_path), "total": round(total, 2), "scenes": scenes_t}


def stage_animate(p: dict[str, Any]) -> None:
    from .. import pipeline
    tl = p.get("timeline") or {}
    if p["kind"] == "story_short":
        parent = pipeline.load(p["parent_id"])
        seg = p["episode"]["segment"]
        p = {**p, "package": parent["package"], "episode": {**p["episode"], "cast": parent["episode"]["cast"]}}
        lines = {(x["scene"], x["line"]): x for x in parent["timeline"]["lines"]}
        res = _render(p, "short", seg["scenes"], lines, workdir(p["id"]), title_card=False, end_card=True,
                      hook=seg.get("hook", ""))
    else:
        lines = {(x["scene"], x["line"]): x for x in tl["lines"]}
        res = _render(p, p["format"], list(range(len(p["package"]["scenes"]))), lines, workdir(p["id"]),
                      title_card=p["format"] == "long", end_card=True)
    tl = {**tl, "total_duration": res["total"], "scene_times": res["scenes"]}
    pipeline.save(p["id"], timeline=tl, video_path=res["final"], srt_path=res["srt"])
    logs.event("info", "creative", f"Rendered {res['total'] / 60:.1f} min animation", production_id=p["id"])


def stage_thumbnail(p: dict[str, Any]) -> None:
    from .. import pipeline
    pkg = p["package"] if p["kind"] == "story" else pipeline.load(p["parent_id"])["package"]
    cast = p["episode"].get("cast") or pipeline.load(p["parent_id"])["episode"]["cast"]
    scene = pkg["scenes"][(p["episode"].get("segment") or {}).get("scenes", [0])[0]]
    keys = [k for k in scene.get("characters", []) if k in cast] or list(cast)
    text = p["package"].get("thumbnail_text") or p["title"]
    out = workdir(p["id"]) / "thumbnail.jpg"
    cartoon.thumbnail([cast[k] for k in keys], text, scene, out)
    pipeline.save(p["id"], thumbnail_path=str(out) if p["format"] == "long" else None)
    tl = p.get("timeline") or {}
    tl["thumbnail"] = {"path": str(out), "width": 1280, "height": 720}
    pipeline.save(p["id"], timeline=tl)


def _probe(path: str) -> dict[str, Any]:
    r = subprocess.run([config.FFPROBE, "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
                        "-of", "json", path], capture_output=True, text=True, timeout=60)
    return json.loads(r.stdout or "{}")


def stage_qa(p: dict[str, Any]) -> None:
    """Checks that the rendered file is what was asked for — not just that a file exists."""
    from .. import pipeline
    checks = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "severity": "fail", "detail": detail})
    path = p.get("video_path") or ""
    exists = bool(path) and Path(path).exists()
    add("video file", exists, path or "missing")
    info = _probe(path) if exists else {}
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    dur = float((info.get("format") or {}).get("duration") or 0)
    want = (1080, 1920) if p["format"] == "short" else (1920, 1080)
    add("resolution", (v.get("width"), v.get("height")) == want, f"{v.get('width')}x{v.get('height')}")
    add("audio track", any(s.get("codec_type") == "audio" for s in streams), "dialogue + music")
    if p["format"] == "short":
        add("Short length", 8 <= dur <= 180, f"{dur:.1f}s")
    else:
        add("episode length", dur >= 60, f"{dur / 60:.1f} min")
    add("subtitles", bool(p.get("srt_path")) and Path(p["srt_path"]).exists(), "SRT written from dialogue timings")
    plan = plan_of(p) if p["kind"] == "story" else plan_of(pipeline.load(p["parent_id"]))
    pkg = p["package"] if p["kind"] == "story" else pipeline.load(p["parent_id"])["package"]
    moods = [s.get("mood") for s in pkg.get("scenes", [])]
    genre_ok = {"horror": any(m in ("scary", "tense") for m in moods), "comedy": any(m == "funny" for m in moods)}.get(
        plan.get("content_type"), True)
    add("matches request", genre_ok, f"requested {plan.get('content_type')}; scene moods {sorted(set(moods))}")
    text = " ".join(ln["text"] for s in pkg.get("scenes", []) for ln in s["lines"])
    if plan.get("language") in ("Hindi", "Hinglish"):
        r = screenplay._devanagari_ratio(text)
        add("language", r >= 0.6, f"{r:.0%} Devanagari dialogue")
    cast = (p.get("episode") or {}).get("cast") or pipeline.load(p["parent_id"])["episode"]["cast"]
    speakers = {ln["speaker"] for s in pkg.get("scenes", []) for ln in s["lines"]} - {charmod.NARRATOR_KEY}
    add("character consistency", speakers <= set(cast),
        f"{len(cast)} bible characters with frozen designs and voices; speakers {sorted(speakers)}")
    passed = all(c["ok"] for c in checks)
    pipeline.save(p["id"], qa={"passed": passed, "checks": checks, "auto_publish_ok": passed})
    if not passed:
        raise pipeline.NeedsAttention("Quality checks failed: " + "; ".join(c["name"] + " — " + c["detail"]
                                                                            for c in checks if not c["ok"]))


def describe(p: dict[str, Any], parent_video_id: str | None = None) -> str:
    from .. import pipeline
    ch = channels.get(p["channel_id"])
    pkg = p["package"] if p["kind"] == "story" else pipeline.load(p["parent_id"])["package"]
    hindi = (p.get("language") or "Hindi") != "English"
    parts = [pkg.get("description", "").strip()]
    if p["kind"] == "story_short":
        link = f"https://youtu.be/{parent_video_id}" if parent_video_id else f"\"{pkg.get('title')}\""
        parts.append(("पूरा एपिसोड: " if hindi else "Full episode: ") + link)
    cast = [c["name"] for c in ((p.get("episode") or {}).get("cast") or {}).values()]
    if cast:
        parts.append(("किरदार: " if hindi else "Characters: ") + ", ".join(cast))
    parts.append("Original animation, story and music by this channel. Voices are AI-generated (text-to-speech)."
                 if not hindi else "ओरिजिनल एनिमेशन, कहानी और म्यूज़िक — इसी चैनल द्वारा। आवाज़ें AI (टेक्स्ट-टू-स्पीच) से बनी हैं।")
    tags = " ".join("#" + "".join(ch_.title() if ch_.isascii() else ch_ for ch_ in t.split()) for t in pkg.get("tags", [])[:6]
                    if t.strip())
    parts.append((tags + " #Shorts").strip() if p["kind"] == "story_short" or p["format"] == "short" else tags)
    return "\n\n".join(x for x in parts if x).replace("<", "").replace(">", "")[:4900]


def stage_review(p: dict[str, Any]) -> None:
    from .. import pipeline
    from ..youtube import oauth
    ch = channels.get(p["channel_id"])
    pkg = p["package"]
    pkg["final_description"] = describe(p)
    if p["kind"] == "story":
        pkg["tags"] = pkg.get("tags", [])[:15]
    pipeline.save(p["id"], package=pkg)
    if p["kind"] == "story" and plan_of(p).get("make_shorts") and p["format"] == "long":
        try:
            make_shorts(p["id"])
        except Exception as e:                       # the episode stays valid even if Shorts fail
            logs.event("warning", "creative", f"Shorts not created: {e}", production_id=p["id"])
    if ch.get("auto_publish") and ch.get("status") == "active" and oauth.connected(ch["id"]):
        pipeline.approve(p["id"], privacy=ch.get("publish_privacy") or "private", publish_at=None,
                         by="auto (channel auto-publish)")
    else:
        why = ("auto-publish is off for this channel" if not ch.get("auto_publish") else
               "this channel's YouTube account is not connected" if not oauth.connected(ch["id"]) else "channel paused")
        pipeline.save(p["id"], status="awaiting_approval", error=f"Ready for approval ({why}).")


STAGE_FN = {"script": stage_script, "characters": stage_characters, "voice": stage_voice, "animate": stage_animate,
            "thumbnail": stage_thumbnail, "qa": stage_qa, "review": stage_review}


# ------------------------------------------------------------------ Shorts from an episode

STRONG = {"laugh": 3, "shout": 2, "surprised": 2, "scared": 3, "angry": 1}


def pick_moments(p: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """Self-contained scenes with the most punch (laughs, shocks, payoffs) that fit a Short."""
    pkg, tl = p["package"], p["timeline"]
    durs: dict[int, float] = {}
    for x in tl["lines"]:
        durs[x["scene"]] = durs.get(x["scene"], LEAD + TAIL) + x["duration"] + GAP
    scored = []
    for si, sc in enumerate(pkg["scenes"]):
        if durs.get(si, 99) > 55:
            continue
        score = sum(STRONG.get(ln["emotion"], 0) for ln in sc["lines"]) + (3 if si == len(pkg["scenes"]) - 1 else 0)
        best = max(sc["lines"], key=lambda ln: STRONG.get(ln["emotion"], 0))
        hook = screenplay.clean_text(best["text"])
        scored.append({"scenes": [si], "score": score, "seconds": round(durs[si], 1),
                       "hook": (hook[:60] + "…") if len(hook) > 60 else hook})
    scored.sort(key=lambda x: -x["score"])
    return scored[:limit]


def make_shorts(parent_id: int) -> list[int]:
    from .. import pipeline, worker
    p = pipeline.load(parent_id)
    ch = channels.get(p["channel_id"])
    existing = db.q1("SELECT COUNT(*) AS n FROM productions WHERE parent_id=? AND status<>'rejected'", (parent_id,))["n"]
    made = []
    for i, m in enumerate(pick_moments(p, max(0, int(ch.get("shorts_per_episode", 4)) - existing)), 1):
        title = f"{m['hook'][:55]} | {p['package']['title']}"[:92] + " #Shorts"
        pkg = {"title": title, "description": p["package"].get("description", ""), "tags": p["package"].get("tags", []),
               "thumbnail_text": m["hook"][:30]}
        sid = db.insert("productions", {
            "topic_id": p.get("topic_id"), "format": "short", "title": title, "status": "queued", "stage": "animate",
            "kind": "story_short", "parent_id": parent_id, "channel_id": p["channel_id"],
            "workspace_id": p.get("workspace_id") or 1, "language": p.get("language"),
            "request": p.get("request"), "package": json.dumps(pkg, ensure_ascii=False),
            "episode": json.dumps({"segment": m, "cast": p["episode"]["cast"]}, ensure_ascii=False), "timeline": "{}",
            "test_mode": 0, "created_at": db.iso(), "updated_at": db.iso()})
        worker.enqueue("production", {"production_id": sid}, production_id=sid, max_attempts=3)
        made.append(sid)
    logs.event("info", "creative", f"{len(made)} Short(s) queued from episode #{parent_id}", production_id=parent_id)
    return made
