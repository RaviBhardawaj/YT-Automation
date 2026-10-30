"""Automated quality gate for a finished production."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image

from .. import config


def _check(name: str, ok: bool, detail: str, severity: str = "fail") -> dict[str, Any]:
    """severity: fail (blocks upload) | warn (needs human approval) | info"""
    return {"name": name, "ok": bool(ok), "detail": detail, "severity": severity}


def probe(path: Path) -> dict[str, Any]:
    p = subprocess.run([config.FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                       capture_output=True, text=True, timeout=120)
    return json.loads(p.stdout or "{}")


def _ffmpeg_stderr(args: list[str], timeout: int = 1800) -> str:
    p = subprocess.run([config.FFMPEG, "-hide_banner", "-nostdin", *args], capture_output=True, text=True, timeout=timeout)
    return p.stderr


FREEZE_LONGEST = 8.0      # seconds: a single frozen stretch this long looks broken (charts hold 2–5 s on purpose)
FREEZE_SHARE = 0.40       # share of the video frozen in 2 s+ stretches


def freeze_check(video: Path, duration: float) -> dict[str, Any]:
    """Frozen / repeated picture on the FINAL render (not the storyboard): stretches of 2 s or more with no change.
    Short holds are normal (charts, a held last frame); a long one or a mostly frozen video needs a human look."""
    out = _ffmpeg_stderr(["-i", str(video), "-an", "-vf", "freezedetect=n=-60dB:d=2", "-map", "0:v", "-f", "null", "-"])
    spans = [float(x) for x in re.findall(r"freeze_duration: ([\d.]+)", out)]
    starts = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", out)]
    if len(starts) > len(spans):                  # still frozen when the video ends: no end marker is printed
        spans.append(max(0.0, duration - starts[-1]))
    longest, total = max(spans, default=0.0), sum(spans)
    ok = longest < FREEZE_LONGEST and total < duration * FREEZE_SHARE
    return _check("frozen picture", ok, f"{len(spans)} still stretch(es) of 2 s+, longest {longest:.1f} s, "
                  f"{total:.1f} s in all" + ("" if ok else " — looks frozen; check before publishing"), "warn")


def run(production: dict[str, Any], pkg: dict[str, Any], timeline: dict[str, Any], assets: list[dict[str, Any]],
        thumb: dict[str, Any] | None, voice: dict[str, Any], review: dict[str, Any] | None,
        originality: dict[str, Any] | None) -> dict[str, Any]:
    fmt = production["format"]
    video = Path(production["video_path"] or "")
    checks: list[dict[str, Any]] = []
    if not video.exists():
        return {"passed": False, "auto_publish_ok": False, "checks": [_check("video file", False, "missing")]}

    info = probe(video)
    vs = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    aus = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
    w, h = config.frame_size(fmt)
    duration = float(info.get("format", {}).get("duration") or 0)
    expected = float(timeline.get("total_duration") or 0)
    size = video.stat().st_size

    checks.append(_check("resolution", (vs.get("width"), vs.get("height")) == (w, h),
                         f"{vs.get('width')}x{vs.get('height')} (expected {w}x{h}, {'9:16' if fmt == 'short' else '16:9'})"))
    num, _, den = (vs.get("avg_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0
    checks.append(_check("frame rate", abs(fps - config.FPS) < 0.5, f"{fps:.2f} fps"))
    checks.append(_check("codecs", vs.get("codec_name") == "h264" and aus.get("codec_name") == "aac",
                         f"video {vs.get('codec_name')}, audio {aus.get('codec_name') or 'missing'}"))
    checks.append(_check("duration matches timeline", abs(duration - expected) <= 0.7,
                         f"{duration:.1f}s (timeline {expected:.1f}s)"))
    if fmt == "short":
        checks.append(_check("Shorts length", duration <= 180, f"{duration:.1f}s (YouTube Shorts max 3 min)"))
        if duration > 60:
            checks.append(_check("Shorts sweet spot", False, f"{duration:.0f}s — over 60s is allowed but shorter Shorts usually hold attention better", "info"))
    else:
        checks.append(_check("long-form length", duration >= 60, f"{duration / 60:.1f} min"))
    checks.append(_check("file size", size < 2 * 1024**3, f"{size / 1e6:.1f} MB"))

    vol = _ffmpeg_stderr(["-i", str(video), "-vn", "-af", "volumedetect", "-f", "null", "-"])
    mean = re.search(r"mean_volume:\s*(-?[\d.]+) dB", vol)
    peak = re.search(r"max_volume:\s*(-?[\d.]+) dB", vol)
    mean_db = float(mean.group(1)) if mean else -99.0
    peak_db = float(peak.group(1)) if peak else 0.0
    checks.append(_check("audio present & audible", mean_db > -35, f"mean {mean_db:.1f} dB, peak {peak_db:.1f} dB"))
    checks.append(_check("no clipping", peak_db <= -0.5, f"peak {peak_db:.1f} dBFS", "warn"))

    decode = subprocess.run([config.FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-i", str(video), "-f", "null", "-"],
                            capture_output=True, text=True, timeout=1800)
    checks.append(_check("full decode (playback)", decode.returncode == 0 and not decode.stderr.strip(),
                         "decoded without errors" if not decode.stderr.strip() else decode.stderr.strip()[:300]))

    black = _ffmpeg_stderr(["-i", str(video), "-an", "-vf", "blackdetect=d=0.5:pix_th=0.10", "-f", "null", "-"])
    black_total = sum(float(x) for x in re.findall(r"black_duration:([\d.]+)", black))
    checks.append(_check("black frames", black_total <= max(1.5, duration * 0.08), f"{black_total:.1f}s black"))
    checks.append(freeze_check(video, duration))

    distinct = {a["local_path"] for a in assets}
    procedural = [a for a in assets if a["provider"] == "procedural" and not a.get("is_card")]
    n = len(timeline.get("scenes", []))
    checks.append(_check("visual variety", len(distinct) >= min(3, n), f"{len(distinct)} distinct visuals for {n} scenes"))
    checks.append(_check("real footage share", len(procedural) <= n * 0.5,
                         f"{len(procedural)}/{n} scenes use abstract fallback art (add a Pexels/Pixabay key for footage)", "warn"))
    rel = [json.loads(a["relevance"]) if isinstance(a.get("relevance"), str) and a["relevance"].startswith("{")
           else (a.get("relevance") or {}) for a in assets]
    looked = [r for r in rel if r.get("how", "").startswith("vision check")]
    if looked:                                   # each stock picture was checked against its scene by a vision model
        weak = [r for r in looked if r.get("score", 0) < 6]
        avg = sum(r.get("score", 0) for r in looked) / len(looked)
        checks.append(_check("visuals match the topic", not weak or len(weak) <= len(looked) * 0.25,
                             f"{len(looked)} stock visual(s) checked by AI, average {avg:.1f}/10"
                             + (f", {len(weak)} only loosely on topic" if weak else ""), "warn"))
    if production.get("test_mode"):
        checks.append(_check("test mode", False, "rendered in minimal test mode — not for upload"))

    speech = sum(s["duration"] for s in timeline.get("scenes", []))
    covered = sum(c["end"] - c["start"] for c in timeline.get("captions", []))
    checks.append(_check("captions synchronised", speech > 0 and covered / speech >= 0.8,
                         f"captions cover {covered / speech * 100 if speech else 0:.0f}% of narration time"))

    if thumb:
        tp = Path(thumb["path"])
        ok = tp.exists()
        if ok:
            with Image.open(tp) as im:
                dims = im.size
        checks.append(_check("thumbnail size", ok and dims == (config.THUMB_W, config.THUMB_H) and tp.stat().st_size < 2 * 1024**2,
                             f"{dims if ok else 'missing'}, {tp.stat().st_size / 1e6 if ok else 0:.2f} MB"))
        checks.append(_check("thumbnail text contrast", thumb.get("contrast", 0) >= 4.5, f"contrast ratio {thumb.get('contrast')}:1 (≥ 4.5)"))
    elif fmt == "short" and timeline.get("cover"):
        checks.append(_check("thumbnail", True, "Shorts show a frame of the video (cover kept for the dashboard)", "info"))
    else:
        checks.append(_check("thumbnail", False, "missing"))

    title = pkg.get("title", "")
    desc = pkg.get("final_description") or pkg.get("description", "")
    checks.append(_check("title", 0 < len(title) <= 100 and "<" not in title and ">" not in title, f"{len(title)} chars"))
    checks.append(_check("description", len(desc.encode()) <= 5000 and "<" not in desc and ">" not in desc,
                         f"{len(desc.encode())} bytes"))
    checks.append(_check("tags", sum(len(t) + 3 for t in pkg.get("tags", [])) <= 500, f"{len(pkg.get('tags', []))} tags"))
    if fmt == "long" and pkg.get("chapters"):
        times = timeline.get("chapter_times", [])
        ok = len(times) >= 3 and times[0] == 0 and all(b - a >= 10 for a, b in zip(times, times[1:]))
        checks.append(_check("chapters valid", ok, f"{len(times)} chapters (YouTube needs ≥3, first at 0:00, each ≥10s)", "warn"))

    unlicensed = [a for a in assets if not a.get("license")]
    checks.append(_check("asset licences recorded", not unlicensed, f"{len(assets) - len(unlicensed)}/{len(assets)} assets"))
    if voice.get("commercial") is False:
        checks.append(_check("voice licence", False, f"voice '{voice.get('voice_id')}' is trained on non-commercial data "
                             f"({voice.get('license')}) — fine for testing, not for a monetized channel", "warn"))
    elif voice.get("commercial") is None:
        checks.append(_check("voice licence", False, "voice licence not confirmed — review the model card", "warn"))

    facts = pkg.get("fact_checks", [])
    unverified = [f for f in facts if f.get("status") == "unverified"]
    checks.append(_check("fact checks", not unverified, f"{len(unverified)} unverified claim(s) of {len(facts)}", "warn"))
    if originality:
        checks.append(_check("originality", originality.get("ok", False),
                             f"{originality.get('copied_7gram_ratio', 0) * 100:.1f}% of 7-word phrases match sources; "
                             f"closest competitor title similarity {originality.get('closest_competitor_title_similarity')}",
                             "warn"))
    if review:
        high = [i for i in review.get("issues", []) if i.get("severity") == "high"]
        checks.append(_check("editorial review", not high, f"{len(high)} high-severity issue(s) remain", "warn"))

    passed = all(c["ok"] for c in checks if c["severity"] == "fail")
    auto_ok = passed and all(c["ok"] for c in checks if c["severity"] == "warn")
    return {"passed": passed, "auto_publish_ok": auto_ok, "duration": duration, "size_bytes": size, "checks": checks}
