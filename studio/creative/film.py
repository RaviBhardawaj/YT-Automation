"""Free photo-real film: a screenplay made into a real-looking movie with free tools.

  script  — the user's own screenplay read as written (script_import), or one written by the AI for a prompt;
            Hinglish lines get a Devanagari copy for the Hindi voices (subtitles keep the original words)
  cast    — one photo-real portrait per character and one establishing picture of the location (free FLUX);
            these references are passed into every shot so faces and the set stay the same
  voice   — a distinct Hindi voice per character (Kokoro voices, each with its own pitch), comic pauses kept
  shots   — per scene: a wide establishing shot and a shot of each speaker, made with their references
  render  — every line becomes a shot with a slow camera move and a subtitle; hard cuts like a film; dialogue,
            a quiet music bed and village air mixed underneath; one consistent film grade
  qa      — the file plays, has sound and the expected length; then it waits for the owner

People do not move or speak on screen (that needs paid AI video); everything here is free. If today's free image
allowance runs out, the job waits and continues by itself after it resets (00:00 UTC, 05:30 IST).
"""
from __future__ import annotations

import json
import re
import subprocess
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from pydantic import BaseModel, Field

from .. import ai, channels, config, db, logs, settings
from ..media import graphics, tts, visuals
from . import script_import

STAGES = ["script", "plan", "cast", "voice", "sync", "shots", "render", "qa"]
STATUS = {"script": "reading the script", "plan": "planning the shots", "cast": "casting the characters",
          "voice": "recording the voices", "sync": "syncing dialogue to shots",
          "shots": "filming the shots", "render": "editing the film", "qa": "quality check"}
SR = 24000
SHOT = (1024, 576)                                     # generated size; upscaled to 1080p in the edit
REALISM = re.compile(r"\b(photo-?realistic|live[- ]action|realistic|real (human|people|actors?|movie|film)|"
                     r"cinematic realism)\b", re.I)
MALE = [("hm_omega", 1.0), ("hm_psi", 1.0), ("hm_omega", 0.90), ("hm_psi", 1.08), ("hm_omega", 1.07),
        ("hm_psi", 0.93)]
FEMALE = [("hf_alpha", 1.0), ("hf_beta", 1.0), ("hf_alpha", 1.07), ("hf_beta", 0.94)]


class WaitForAllowance(Exception):
    """Today's free images are used up; the worker retries after the reset without counting a failure."""
    temporary = True

    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        reset = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        # check again within the hour: the free GPU minutes on Hugging Face come back during the day
        self.retry_after = min(int((reset - now).total_seconds()), 3600)
        super().__init__("All free image allowances are used up for now — filming continues by itself as soon as "
                         "one is back (checked every hour; Cloudflare resets at 05:30 IST).")


class NetworkHiccup(Exception):
    """A dropped/slow connection while making an image: retried in a few minutes, never counted as a failure."""
    temporary = True
    retry_after = 180


def wants_film(request: str) -> bool:
    """A request for a real-looking film (not a cartoon)."""
    return bool(REALISM.search(request or ""))


def workdir(pid: int) -> Path:
    d = config.PRODUCTIONS_DIR / f"{pid:05d}" / "film"
    d.mkdir(parents=True, exist_ok=True)
    return d


def create(channel_id: int, request: str, *, plan: dict[str, Any], actor: str = "user") -> int:
    from .. import worker
    ch = channels.get(channel_id)
    title = (script_import.parse(request)["title"] if script_import.looks_like_screenplay(request)
             else request.strip().splitlines()[0])[:90] or "Film"
    plan = {**plan, "workflow": "film", "look": "real", "actor": actor}
    pid = db.insert("productions", {
        "format": "long", "title": title, "status": "queued", "stage": "script", "kind": "film",
        "channel_id": channel_id, "workspace_id": ch["workspace_id"], "language": plan.get("language") or "Hinglish",
        "request": json.dumps(plan, ensure_ascii=False), "package": "{}", "timeline": "{}", "episode": "{}",
        "test_mode": 0, "created_at": db.iso(), "updated_at": db.iso()})
    worker.enqueue("production", {"production_id": pid}, production_id=pid, max_attempts=3)
    logs.event("info", "film", f"Photo-real film queued: '{title}'", production_id=pid)
    return pid


def _plan(p: dict[str, Any]) -> dict[str, Any]:
    return db.loads(p.get("request"), {}) or {}


# ------------------------------------------------------------------ script

class _Devanagari(BaseModel):
    lines: list[str] = Field(..., description="each input line written in Devanagari, same order and count")


def _devanagari(lines: list[str]) -> list[str]:
    """Hinglish (Roman) → Devanagari so the Hindi voices pronounce it; English words are transliterated."""
    if not lines:
        return []
    if all(re.search(r"[ऀ-ॿ]", x) for x in lines):
        return lines

    def check(out: _Devanagari) -> None:
        if len(out.lines) != len(lines):
            raise ValueError(f"return exactly {len(lines)} lines, one per input line")
        bad = [i for i, x in enumerate(out.lines) if not re.search(r"[ऀ-ॿ]", x)]
        if bad:
            raise ValueError(f"lines {bad} are not in Devanagari")
    numbered = "\n".join(f"{i + 1}. {x}" for i, x in enumerate(lines))
    res, _ = ai.generate("script", "You transliterate Hinglish dialogue for a Hindi text-to-speech voice.",
                         "Write each line in Devanagari script exactly as it is spoken — same words, same order, "
                         "nothing added or removed. English words are written in Devanagari too (e.g. 'tractor' → "
                         "'ट्रैक्टर', 'confidence' → 'कॉन्फिडेंस'). Keep punctuation (! ? , .).\n\n" + numbered,
                         _Devanagari, max_tokens=4000, validate=check)
    return res.lines


def _from_ai(plan: dict[str, Any], ch: dict[str, Any]) -> dict[str, Any]:
    """No screenplay given: the AI writes one (fresh characters), turned into the film format."""
    from . import screenplay
    sp = screenplay.write({**plan, "use_cast": False}, ch, None)
    chars = {c["key"]: c for c in sp.get("new_characters") or []}
    cast = []
    for i, key in enumerate(sp.get("cast") or []):
        c = chars.get(key) or {"name": key}
        d = c.get("design") or {}
        look = ", ".join(str(v) for k, v in d.items() if k not in ("voice",) and isinstance(v, (str, int)))
        cast.append({"key": key, "name": c.get("name", key), "role": c.get("role", ""), "look": look[:500],
                     "personality": c.get("personality", ""), "voice_note": "",
                     "gender": "female" if (d.get("gender") or "").startswith("f") else "male"})
    scenes = [{"number": i + 1, "heading": s.get("title") or f"Scene {i + 1}", "action": s.get("setting", ""),
               "lines": [{"speaker": ln["speaker"], "text": ln["text"], "manner": ln.get("emotion", ""),
                          "action": ln.get("action", "")} for ln in s["lines"] if ln["speaker"] in chars]}
              for i, s in enumerate(sp["scenes"])]
    return {"title": sp["title"], "cast": cast, "scenes": [s for s in scenes if s["lines"]],
            "location": " ".join(s.get("setting", "") for s in sp["scenes"])[:600], "lighting": "",
            "closing_line": "", "description": sp.get("description", ""), "tags": sp.get("tags", [])}


def stage_script(p: dict[str, Any]) -> None:
    from .. import pipeline
    plan = _plan(p)
    text = plan.get("request") or ""
    film = script_import.parse(text) if script_import.looks_like_screenplay(text) else \
        _from_ai(plan, channels.get(p["channel_id"]))
    if not film["scenes"] or not film["cast"]:
        raise pipeline.NeedsAttention("Couldn't find scenes and dialogue in the script — use 'NAME:' before each line "
                                      "and 'SCENE 1 — …' headings.")
    for s in film["scenes"]:                            # Devanagari copy for the voices, one request per scene
        tts_lines = _devanagari([ln["text"] for ln in s["lines"]])
        for ln, t in zip(s["lines"], tts_lines):
            ln["tts"] = t
    n = sum(len(s["lines"]) for s in film["scenes"])
    film.setdefault("description", f"{film['title']} — a short village comedy.")
    film.setdefault("tags", ["comedy", "village", "hinglish", "short film"])
    pipeline.save(p["id"], package={**film, "kind": "film"}, title=film["title"])
    logs.event("info", "film", f"Script read as written: {len(film['scenes'])} scenes, {n} lines, "
               f"{len(film['cast'])} characters ({', '.join(c['name'] for c in film['cast'])})", production_id=p["id"])


# ------------------------------------------------------------------ shot list

def stage_plan(p: dict[str, Any]) -> None:
    """Screenplay → structured shot list (saved as film_shots); runtime checked against the request."""
    from .. import pipeline
    from ..film_studio import shotlist
    film = p["package"]
    planned = shotlist.plan(film)
    n = shotlist.save(p["id"], p["workspace_id"], planned)
    film["shot_plan"] = {"shots": n, "runtime": planned["runtime"], "counts": planned["counts"],
                         "coverage": planned["coverage"]}
    pipeline.save(p["id"], package=film)
    cov = (planned["coverage"] or {}).get("message", "")
    logs.event("info", "film", f"Shot list: {n} shots, {planned['runtime'] / 60:.1f} min planned. {cov}",
               production_id=p["id"])


# ------------------------------------------------------------------ cast (portraits + location)

def _gen(prompt: str, out: Path, *, refs: list[Path] | None = None, size: tuple[int, int] = SHOT) -> Path:
    """One free image. Every free provider is tried in turn (Cloudflare with the cast pictures, then the free
    FLUX Space, then a local Stable Diffusion server); filming only waits when all of them are used up."""
    if out.exists():
        return out
    from ..film_studio import providers
    picks, tried = providers.candidates("image_ref" if refs else "image"), []
    if refs:                                      # no reference-capable provider left: describe the cast instead
        picks += [p for p in providers.candidates("image") if p["provider"] not in {q["provider"] for q in picks}]
    hiccup = None
    for pick in picks:
        providers.guard(pick["provider"])
        try:
            if pick["provider"] == "cloudflare_flux":
                visuals.generate_cloudflare(prompt, "long", out, look="real", refs=refs, size=size)
            elif pick["provider"] == "hf_flux":
                visuals.generate_hf_flux(prompt, "long", out, look="real", size=size)
            elif pick["provider"] == "local_flux":
                visuals.generate_local_flux(prompt, "long", out, look="real", size=size)
            elif pick["provider"] == "local_sd":
                visuals.generate_local_sd(f"{prompt}. {visuals.LOOK_PROMPT['real']}", size[0], size[1], out)
            else:
                continue
        except visuals.AllowanceUsedUp:
            tried.append(f"{pick['name']}: used up")
            continue
        except (httpx.TransportError, httpx.HTTPStatusError, visuals.net.FetchError) as e:
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500 and \
                    e.response.status_code != 429:
                raise
            hiccup = e
            tried.append(f"{pick['name']}: {type(e).__name__}")
            continue
        used = [str(r) for r in refs or []] if "image_ref" in providers.BY_ID[pick["provider"]]["caps"] else []
        if refs and not used:
            logs.event("info", "film", f"{out.name}: made by {pick['name']} without the cast pictures (the reference "
                                       "allowance is used up) — retry the shot later to match faces exactly")
        try:                                            # which provider made the picture and which references it used
            out.with_suffix(".json").write_text(json.dumps({"provider": pick["provider"], "refs_used": used,
                                                            "refs_wanted": [str(r) for r in refs or []]}))
        except OSError:
            pass
        return out
    if hiccup is not None and not any(t.endswith("used up") for t in tried):
        raise NetworkHiccup(f"image services didn't answer ({type(hiccup).__name__}) — retrying in a few minutes")
    raise WaitForAllowance()


def stage_cast(p: dict[str, Any]) -> None:
    """Propose a picture for every character and the location; continue only with approved pictures."""
    from .. import pipeline
    from ..film_studio import casting
    film, ws = p["package"], p["workspace_id"]
    work = workdir(p["id"]) / "cast"
    work.mkdir(exist_ok=True)
    casting.propose(p["id"], ws, film)
    made = 0
    for r in casting.latest(p["id"], ws).values():
        if not r.get("file_path") and r["status"] != "rejected":
            casting.generate(r["id"], ws, work, _gen)
            made += 1
    still = casting.missing(p["id"], ws)
    if still:
        pipeline.save(p["id"], status="needs_review",
                      error=f"Approve the cast to continue filming — waiting for: {', '.join(still)}. Open Cast review.")
        casting.log(p["id"], f"Cast proposed ({made} new pictures) — waiting for approval of {len(still)}")
        return
    casting.apply_to_film(p["id"], ws, film)
    pipeline.save(p["id"], package=film)
    logs.event("info", "film", f"Cast approved: {len(film['cast'])} characters + location", production_id=p["id"])


# ------------------------------------------------------------------ voices

def _assign_voices(cast: list[dict[str, Any]]) -> None:
    pools = {"male": list(MALE), "female": list(FEMALE)}
    for c in cast:
        pool = pools[c["gender"]]
        deep = re.search(r"\b(deep|mature|heavy)\b", c.get("voice_note", ""), re.I)
        pick = next((v for v in pool if deep and v[1] < 1.0), None) or pool[0]
        pool.remove(pick)
        if not pool:
            pools[c["gender"]] = list(MALE if c["gender"] == "male" else FEMALE)
        c["voice"] = {"engine": "kokoro", "voice_id": pick[0], "pitch": pick[1]}


def _pitch(audio: np.ndarray, factor: float, work: Path) -> np.ndarray:
    """Shift pitch keeping the duration (so each character sounds like a different person)."""
    if abs(factor - 1.0) < 0.01:
        return audio
    src, dst = work / "_p_in.wav", work / "_p_out.wav"
    _write(src, audio)
    subprocess.run([config.FFMPEG, "-v", "error", "-y", "-i", str(src), "-af",
                    f"asetrate={int(SR * factor)},aresample={SR},atempo={1 / factor:.4f}", str(dst)], check=True)
    return _read(dst)


def _write(path: Path, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(np.asarray(audio, dtype=np.int16).tobytes())


def _read(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        sr = w.getframerate()
    if sr != SR:
        a = np.interp(np.linspace(0, len(a) - 1, int(len(a) * SR / sr)), np.arange(len(a)), a).astype(np.int16)
    return a


def stage_voice(p: dict[str, Any]) -> None:
    from .. import pipeline
    film = p["package"]
    if not tts.kokoro_installed():
        raise pipeline.NeedsAttention("The free Kokoro voices aren't installed — Computer & setup → Install.")
    if not all(c.get("voice") for c in film["cast"]):
        _assign_voices(film["cast"])
    voices = {c["key"]: c["voice"] for c in film["cast"]}
    work = workdir(p["id"]) / "voice"
    work.mkdir(exist_ok=True)
    for si, s in enumerate(film["scenes"]):
        for li, ln in enumerate(s["lines"]):
            out = work / f"s{si:02d}_l{li:02d}.wav"
            if not out.exists():
                v = voices[ln["speaker"]]
                speed = 0.94 if "whisper" in (ln.get("manner") or "") else 1.0
                audio, sr = tts._synth_sentence(ln.get("tts") or ln["text"], "kokoro", v["voice_id"], "hi", speed)
                if sr != SR:
                    audio = np.interp(np.linspace(0, len(audio) - 1, int(len(audio) * SR / sr)),
                                      np.arange(len(audio)), audio).astype(np.int16)
                audio = _pitch(audio, v["pitch"], work)
                if "whisper" in (ln.get("manner") or ""):
                    audio = (audio * 0.55).astype(np.int16)
                _write(out, audio)
            with wave.open(str(out), "rb") as w:
                ln["seconds"] = round(w.getnframes() / w.getframerate(), 3)
            ln["wav"] = str(out)
    pipeline.save(p["id"], package=film)
    total = sum(ln["seconds"] for s in film["scenes"] for ln in s["lines"])
    logs.event("info", "film", f"Voices: {sum(len(s['lines']) for s in film['scenes'])} lines, {total / 60:.1f} min, "
               f"{len(voices)} distinct voices", production_id=p["id"])


# ------------------------------------------------------------------ shots

def stage_sync(p: dict[str, Any]) -> None:
    """Phase 7: attach each recorded line to its shot; shot lengths come from the real audio."""
    from .. import pipeline
    from ..film_studio import audio
    rep = audio.sync(p["id"], p["workspace_id"], p["package"])
    film = p["package"]
    film["audio_sync"] = rep
    pipeline.save(p["id"], package=film)
    logs.event("info", "film", f"Dialogue synced to {rep['attached']} shots"
               + (f"; {len(rep['warnings'])} warning(s)" if rep["warnings"] else ""), production_id=p["id"])


def stage_shots(p: dict[str, Any]) -> None:
    """Phase 6: a take for every shot (real video for key shots within the free GPU quota)."""
    from .. import pipeline
    from ..film_studio import generate
    res = generate.run(p, _gen, workdir(p["id"]))
    film = pipeline.load(p["id"])["package"]
    film["generation"] = {k: res[k] for k in ("keyframes", "video", "talking", "camera_move", "failed", "upgraded",
                                              "left")}
    pipeline.save(p["id"], package=film)
    if pipeline.load(p["id"])["status"] in ("paused", "rejected"):
        return
    if res["left"]:
        from ..film_studio import store
        stuck = [s for s in store.shots(p["id"], p["workspace_id"]) if s["status"] == "needs_review"]
        if stuck:
            pipeline.save(p["id"], status="needs_review",
                          error=f"{len(stuck)} shot(s) failed repeatedly — open the Film Studio page to retry or "
                                "change them.")
            return
    logs.event("info", "film", f"Shots: {res['video']} real video, {res['talking']} lip-synced, {res['camera_move']} stills with camera move, "
               f"{res['keyframes']} new pictures", production_id=p["id"])


# ------------------------------------------------------------------ render

def _subtitle(text: str, w: int, h: int, out: Path) -> dict[str, int]:
    """Cinema-style subtitle: white text, thin outline, near the bottom."""
    max_w = int(w * 0.80)
    fnt, lines, lh = graphics.fit_text(text, "body", max_w, int(h * 0.14), int(h * 0.040), int(h * 0.026),
                                       max_lines=2)
    stroke = max(2, fnt.size // 12)
    pad = stroke * 3 + 8
    bw, bh = max_w + pad * 2, lh * len(lines) + pad * 2
    img = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    graphics.draw_lines(ImageDraw.Draw(shadow), lines, fnt, lh, bw // 2, pad + 3, (0, 0, 0, 170), stroke, (0, 0, 0, 170))
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(4)))
    graphics.draw_lines(ImageDraw.Draw(img), lines, fnt, lh, bw // 2, pad, (255, 255, 255), stroke, (0, 0, 0))
    img.save(out)
    return {"x": (w - bw) // 2, "y": int(h * 0.90 - bh), "w": bw, "h": bh}


def _card(text: str, sub: str, w: int, h: int, out: Path) -> Path:
    img = Image.new("RGB", (w, h), (8, 8, 8))
    d = ImageDraw.Draw(img)
    fnt, lines, lh = graphics.fit_text(text, "heavy", int(w * 0.8), int(h * 0.3), int(h * 0.09), int(h * 0.04), 2)
    graphics.draw_lines(d, lines, fnt, lh, w // 2, int(h * 0.40 - lh * len(lines) / 2), (240, 230, 210))
    if sub:
        f2, l2, lh2 = graphics.fit_text(sub, "body", int(w * 0.8), int(h * 0.12), int(h * 0.035), int(h * 0.022), 2)
        graphics.draw_lines(d, l2, f2, lh2, w // 2, int(h * 0.62), (200, 190, 170))
    img.save(out, "JPEG", quality=94)
    return out


def stage_render(p: dict[str, Any]) -> None:
    """Phase 8: assemble the timeline from the takes and write the export bundle."""
    from .. import pipeline
    from ..film_studio import edit
    res = edit.assemble(p)
    exp = edit.export_dir(p["id"])
    thumb = exp / "thumbnail.jpg"
    first = next((c for c in res["timeline"] if c["kind"] == "shot" and c.get("keyframe")), None)
    if first:
        with Image.open(first["keyframe"]) as im:
            graphics.cover(im.convert("RGB"), 1280, 720).save(thumb, "JPEG", quality=92)
    film = pipeline.load(p["id"])["package"]
    film.setdefault("description", f"{film.get('title')} — a short film.")
    pipeline.save(p["id"], video_path=res["video"], srt_path=res["srt"],
                  thumbnail_path=str(thumb) if thumb.exists() else None,
                  timeline={"total_duration": res["seconds"], "clips": len(res["timeline"])}, package=film)


def stage_qa(p: dict[str, Any]) -> str | None:
    """Phase 9: technical + creative checks; off-script shots get a fresh take, then the film is re-edited."""
    from .. import pipeline
    from ..film_studio import qa, store
    shots = store.shots(p["id"], p["workspace_id"])
    missing = [f"{s['scene_no']}.{s['shot_no']}" for s in shots if not s.get("current_take")]
    checks = qa.technical(p.get("video_path"), p.get("srt_path"),
                          float((p.get("timeline") or {}).get("total_duration") or 0), missing)
    creative, failing = qa.creative(p)
    checks += creative
    clips, counts = qa.clip_checks(p)                    # what each shot really is + real-video file/motion checks
    checks += clips
    film = p["package"]
    rounds = int(film.get("qa_rounds") or 0)
    if failing and rounds < int(settings.get("film_studio.max_retries_per_shot") or 2):
        res = qa.regenerate(p, failing)
        film["qa_rounds"] = rounds + 1
        pipeline.save(p["id"], package=film)
        if res["retried"]:
            logs.event("info", "film", f"QA: {len(res['retried'])} off-script shot(s) get a fresh take",
                       production_id=p["id"])
            return "shots"                                  # redo only those shots, then re-edit
    hard = [c for c in checks if c["severity"] == "fail" and c["ok"] is False]
    report = {"passed": not hard, "auto_publish_ok": False, "checks": checks, "shot_counts": counts,
              "unverified": [c["name"] for c in checks if c["ok"] is None]}
    from ..film_studio import edit
    (edit.export_dir(p["id"]) / "quality_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    pipeline.save(p["id"], qa=report)
    if hard:
        raise pipeline.NeedsAttention("Quality checks failed: " + "; ".join(c["name"] for c in hard))
    from ..film_studio import shorts
    tl = json.loads((edit.export_dir(p["id"]) / "timeline.json").read_text())
    film = pipeline.load(p["id"])["package"]
    film["shorts_proposals"] = shorts.propose(tl)           # proposals only — the owner picks what to make
    pipeline.save(p["id"], package=film, status="awaiting_approval",
                  error="Ready — watch it, then download it or choose a channel to publish it to.")
    return None


STAGE_FN = {"script": stage_script, "plan": stage_plan, "cast": stage_cast, "voice": stage_voice, "sync": stage_sync,
            "shots": stage_shots,
            "render": stage_render, "qa": stage_qa}
