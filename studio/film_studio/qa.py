"""Phase 9 — quality control before a film is called ready.

Technical checks run on the real file. Creative checks use the free vision model where it is available and are
reported as "unverified" where nothing can check them — no invented scores. A shot that clearly doesn't match its
script is regenerated (new take, earlier takes kept) up to the retry limit, then escalated to the review queue.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .. import config, settings
from . import store

RELEVANCE_MIN = 4


def _probe(path: str) -> dict[str, Any]:
    r = subprocess.run([config.FFPROBE, "-v", "error", "-show_entries",
                        "format=duration:stream=codec_type,width,height,r_frame_rate", "-of", "json", path],
                       capture_output=True, text=True, timeout=120)
    return json.loads(r.stdout or "{}")


def _ff(args: list[str], timeout: int = 1800) -> str:
    r = subprocess.run([config.FFMPEG, "-hide_banner", "-nostats", *args], capture_output=True, text=True,
                       timeout=timeout)
    return r.stderr


def _check(name: str, ok: bool | None, detail: str, kind: str = "technical", severity: str = "fail") -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail, "kind": kind,
            "severity": severity if ok is not None else "unverified"}


def technical(video: str, srt: str | None, expected: float, shots_missing: list[str]) -> list[dict[str, Any]]:
    w, h = config.frame_size("long")
    checks = [_check("all shots present", not shots_missing,
                     "every shot has a take" if not shots_missing else f"missing: {', '.join(shots_missing[:8])}")]
    exists = bool(video) and Path(video).exists()
    checks.append(_check("film file exists", exists, Path(video).name if video else "no file"))
    if not exists:
        return checks
    info = _probe(video)
    streams = info.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    dur = float((info.get("format") or {}).get("duration") or 0)
    fps = v.get("r_frame_rate", "0/1")
    fps_v = round(float(fps.split("/")[0]) / max(1.0, float(fps.split("/")[1])), 2) if "/" in fps else 0
    checks += [
        _check("plays to the end", "Error" not in _ff(["-v", "error", "-i", video, "-f", "null", "-"]),
               "decoded without errors"),
        _check("sound present", any(s.get("codec_type") == "audio" for s in streams), "audio stream found"),
        _check("length as planned", abs(dur - expected) <= 1.5, f"{dur:.1f} s vs {expected:.1f} s planned"),
        _check("resolution & frame rate", (v.get("width"), v.get("height")) == (w, h) and abs(fps_v - config.FPS) < 0.5,
               f"{v.get('width')}×{v.get('height')} at {fps_v} fps"),
    ]
    black = _ff(["-i", video, "-an", "-vf", "blackdetect=d=0.6:pix_th=0.08", "-f", "null", "-"])
    black_s = sum(float(x) for x in re.findall(r"black_duration:([\d.]+)", black))
    checks.append(_check("no unintended black", black_s <= max(2.0, dur * 0.02), f"{black_s:.1f} s black", severity="warn"))
    vol = _ff(["-i", video, "-vn", "-af", "volumedetect", "-f", "null", "-"])
    peak = float((re.search(r"max_volume: ([-\d.]+) dB", vol) or [None, "-99"])[1])
    mean = float((re.search(r"mean_volume: ([-\d.]+) dB", vol) or [None, "-99"])[1])
    checks.append(_check("audio not clipped", peak <= -0.3, f"peak {peak} dB", severity="warn"))
    checks.append(_check("dialogue audible", mean > -32, f"mean {mean} dB", kind="creative"))
    if srt and Path(srt).exists():
        cues = re.findall(r"(\d\d):(\d\d):(\d\d),(\d{3}) --> (\d\d):(\d\d):(\d\d),(\d{3})", Path(srt).read_text())
        secs = [(int(a) * 3600 + int(b) * 60 + int(c) + int(d) / 1000, int(e) * 3600 + int(f) * 60 + int(g) + int(k) / 1000)
                for a, b, c, d, e, f, g, k in cues]
        bad = [i for i, (s, e) in enumerate(secs) if not (0 <= s < e <= dur + 0.5)]
        overlap = [i for i in range(1, len(secs)) if secs[i][0] < secs[i - 1][1] - 0.05]
        checks.append(_check("subtitles timed", not bad and not overlap,
                             f"{len(secs)} cues" + (f", {len(bad)} out of range" if bad else "") +
                             (f", {len(overlap)} overlapping" if overlap else "")))
    return checks


def creative(p: dict[str, Any], limit: int = 12) -> tuple[list[dict[str, Any]], list[int]]:
    """Script-to-shot relevance and character presence on the key pictures (free vision model, capped)."""
    from ..media import vision
    ws, pid, film = p["workspace_id"], p["id"], p["package"]
    shots = store.shots(pid, ws)
    names = {c["key"]: c["name"] for c in film.get("cast") or []}
    looks = {c["key"]: (c.get("look") or "")[:160] for c in film.get("cast") or []}
    checks, failing = [], []
    by_motion: dict[str, int] = {}
    for sh in shots:
        by_motion[sh["motion_type"]] = by_motion.get(sh["motion_type"], 0) + 1
    real = sum(v for k, v in by_motion.items() if k in ("video", "dialogue_video", "lipsync_verified"))
    checks.append(_check("real motion", True, f"{real} of {len(shots)} shots are real video; the rest are stills with "
                                              "camera moves (labelled)", kind="creative", severity="info"))
    checks.append(_check("lip-sync", None,                 # a limitation to report, never a pass and never a block
                         ("unverified on the AI video shots; " if real else "") +
                         "not supported on stills with camera moves — dialogue plays over the speaker's shot",
                         kind="creative"))
    warn = [w for w in (film.get("audio_sync") or {}).get("warnings", [])]
    checks.append(_check("dialogue assigned to the right character", not warn,
                         "every line is on its speaker's shot" if not warn else "; ".join(warn[:5]), kind="creative"))
    if not vision.available():
        for n in ("script-to-shot relevance", "character consistency", "movement quality", "object interaction"):
            checks.append(_check(n, None, "unverified — no vision model available", kind="creative"))
        return checks, failing
    seen: dict[str, dict[str, Any]] = {}
    scored = []
    for sh in shots:
        if len(scored) >= limit:
            break
        take = store.takes(sh["id"], ws)
        cur = next((t for t in take if t["id"] == sh.get("current_take")), None)
        key = (json.loads(cur["settings"] or "{}").get("keyframe") if cur else None)
        if not key or not Path(key).exists() or key in seen:
            continue
        who = ", ".join(f"{names.get(k, k)} ({looks.get(k, '')})" for k in sh["characters"])
        want = f"{sh['action'] or (sh['dialogue'][0]['text'] if sh['dialogue'] else '')} — people: {who}"
        verdict = vision.judge(Path(key).read_bytes(), topic=film.get("title", ""), narration=want, want=want,
                               key=f"film{pid}:{key}")
        if verdict is None:
            continue
        seen[key] = verdict
        scored.append((sh, verdict))
        if verdict["score"] < RELEVANCE_MIN:
            failing.append(sh["id"])
    if scored:
        avg = sum(v["score"] for _, v in scored) / len(scored)
        checks.append(_check("script-to-shot relevance", not failing,
                             f"{len(scored)} key pictures checked, average {avg:.1f}/10"
                             + (f"; {len(failing)} off-script" if failing else ""), kind="creative", severity="warn"))
    else:
        checks.append(_check("script-to-shot relevance", None, "unverified — vision check didn't answer",
                             kind="creative"))
    for n in ("character consistency", "movement quality", "object interaction"):
        checks.append(_check(n, None, "unverified — needs face matching / video analysis this setup doesn't have",
                             kind="creative"))
    return checks, failing


def regenerate(p: dict[str, Any], shot_ids: list[int]) -> dict[str, list[int]]:
    """Give off-script shots a fresh take (earlier takes stay); persistent failures go to the review queue."""
    ws = p["workspace_id"]
    max_retry = int(settings.get("film_studio.max_retries_per_shot") or 2)
    retried, escalated = [], []
    for sid in shot_ids:
        sh = store.shot(sid, ws)
        n = len(store.takes(sid, ws))
        if n > max_retry:
            store.set_shot(sid, ws, status="needs_review")
            escalated.append(sid)
            continue
        cont = dict(sh["continuity"])
        cont["qa_retry"] = n
        store.set_shot(sid, ws, current_take=None, status="queued", continuity=cont)
        retried.append(sid)
    return {"retried": retried, "escalated": escalated}
