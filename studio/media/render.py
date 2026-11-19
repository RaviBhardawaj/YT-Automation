"""FFmpeg rendering.

Safety: FFmpeg is always invoked with an argument list (no shell). Filter graphs contain
only numbers computed by this module; file paths are separate `-i` arguments and all
text was rasterised beforehand by Pillow.
"""
from __future__ import annotations

import logging
import random
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from .. import config, settings
from . import graphics

log = logging.getLogger(__name__)

TRANSITIONS_SHORT = ["fade", "slideleft", "smoothup", "circleopen", "wipeleft", "fadeblack", "slideup"]
TRANSITIONS_LONG = ["fade", "dissolve", "smoothleft", "fade", "wipeleft"]
GROUP = 12


class RenderError(Exception):
    pass


def ffmpeg(args: list[str], *, timeout: int = 3600) -> str:
    cmd = [config.FFMPEG, "-hide_banner", "-nostdin", "-y", "-v", "error", *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RenderError(f"ffmpeg timed out after {timeout}s") from e
    if p.returncode != 0:
        raise RenderError(f"ffmpeg failed ({p.returncode}): {p.stderr.strip()[-1500:]}")
    return p.stderr


PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow")
_par = threading.local()          # set inside parallel renders: each FFmpeg gets its share of the core budget


def _threads() -> str:
    from .. import resources
    return str(getattr(_par, "threads", None) or resources.ffmpeg_threads())


def _encode_args(intermediate: bool = False) -> list[str]:
    """x264 settings. `intermediate` is for in-between files that a later step re-encodes anyway (scene clips
    before the cross-fade join, join groups): a much faster preset at a higher quality, so nothing visible is lost
    and the final encode still uses the channel's chosen preset and CRF."""
    if intermediate:
        preset = settings.get("render.intermediate_preset") or "superfast"
        crf = settings.get("render.intermediate_crf") or 15
    else:
        preset, crf = settings.get("render.preset"), settings.get("render.crf")
    if preset not in PRESETS:
        preset = "veryfast"
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(int(float(crf))),
            "-pix_fmt", "yuv420p", "-r", str(config.FPS), "-threads", _threads()]


def run_parallel(jobs: list[Callable[[], Any]]) -> list[Any]:
    """Run independent FFmpeg jobs side by side (results in job order). The core budget of one render is split
    between the workers, so the Mac stays as usable as with one render. The first failure is raised after the
    others have finished, so no half-written file is left behind by a still-running FFmpeg."""
    from .. import resources
    workers = min(resources.render_workers(), len(jobs))
    if workers <= 1:
        return [j() for j in jobs]
    share = max(1, int(resources.ffmpeg_threads()) // workers)

    def run(job: Callable[[], Any]) -> Any:
        _par.threads = share
        try:
            return job()
        finally:
            _par.threads = None

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="render") as pool:
        futures = [pool.submit(run, j) for j in jobs]
    return [f.result() for f in futures]


def prepare_still(src: Path, w: int, h: int, out: Path) -> Path:
    """Pre-scale to 2x the frame so zoompan has sub-pixel headroom (smoother motion)."""
    with Image.open(src) as im:
        graphics.cover(im.convert("RGB"), w * 2, h * 2).save(out, "JPEG", quality=92)
    return out


def _motion(seed: str, frames: int, gentle: bool) -> tuple[str, str, str]:
    rng = random.Random(seed)
    a = 0.05 if gentle else rng.uniform(0.10, 0.17)
    kind = "in" if gentle else rng.choice(["in", "out", "left", "right", "up"])
    n = max(frames - 1, 1)
    cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if kind == "in":
        return f"1+{a:.4f}*on/{n}", cx, cy
    if kind == "out":
        return f"{1 + a:.4f}-{a:.4f}*on/{n}", cx, cy
    if kind == "left":
        return f"{1 + a:.4f}", f"(iw-iw/zoom)*on/{n}", cy
    if kind == "right":
        return f"{1 + a:.4f}", f"(iw-iw/zoom)*(1-on/{n})", cy
    return f"{1 + a:.4f}", cx, f"(ih-ih/zoom)*(1-on/{n})"


def anim_dir(final_frame: Path) -> Path:
    """An animation asset's step frames live next to its final frame."""
    return final_frame.with_name(final_frame.stem + "_frames")


GRADES = {
    # one consistent colour treatment for every clip of a video, so stock, photos and AI images look like one film
    "real": "eq=contrast=1.06:saturation=0.94:gamma=0.98,colorbalance=rs=0.02:bs=-0.02:rh=0.015:bh=-0.02,"
            "vignette=angle=PI/5,noise=alls=5:allf=t,",
    "cartoon": "eq=contrast=1.03:saturation=1.10,",           # bright and clean for children: no grain, no vignette
    "animated3d": "eq=contrast=1.05:saturation=1.04,colorbalance=rs=0.015:bs=-0.015,vignette=angle=PI/6,",
}


def film_grade(look: str) -> str:
    """The film look as an FFmpeg filter prefix (ending in a comma, or empty when switched off) — for renderers
    outside the scene pipeline, so every video gets the same treatment: kids' cartoons, stories, edits and clips."""
    return _grade(look, "video")


def _grade(look: str, kind: str) -> str:
    if settings.get("render.film_look") == "off" or kind == "anim":
        return ""                                            # charts/data animations stay exact
    return GRADES.get(look, "")


def scene_clip(idx: int, asset: dict[str, Any], content: float, pad: float, overlays: list[dict[str, Any]],
               fmt: str, work: Path, out: Path, seed: str, look: str = "real", intermediate: bool = False) -> Path:
    w, h = config.frame_size(fmt)
    total = content + pad
    frames = max(1, round(total * config.FPS))
    inputs: list[str] = []
    if asset["kind"] == "anim":
        # Chart build-up: play the step frames once over the first part of the scene, then hold the final chart.
        fdir = anim_dir(Path(asset["local_path"]))
        steps = len(list(fdir.glob("f_*.png")))
        reveal = min(max(1.2, total * 0.45), 0.55 * steps)
        inputs += ["-framerate", f"{steps / reveal:.4f}", "-i", str(fdir / "f_%03d.png")]
        graph = [f"[0:v]scale={w}:{h}:flags=lanczos,fps={config.FPS},tpad=stop_mode=clone:stop_duration={total:.3f},"
                 f"trim=duration={total:.3f},setsar=1,format=yuv420p[b0]"]
    elif asset["kind"] == "video":
        inputs += ["-stream_loop", "-1", "-t", f"{total:.3f}", "-i", asset["local_path"]]
        graph = [f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase:flags=bicubic,crop={w}:{h},"
                 f"fps={config.FPS},setsar=1,{_grade(look, 'video')}format=yuv420p[b0]"]
    else:
        still = prepare_still(Path(asset["local_path"]), w, h, work / f"still_{idx:03d}.jpg")
        z, x, y = _motion(f"{seed}:{idx}", frames, gentle=bool(asset.get("is_card")))
        inputs += ["-i", str(still)]
        graph = [f"[0:v]zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={w}x{h}:fps={config.FPS},"
                 f"setsar=1,{'' if asset.get('is_card') else _grade(look, 'still')}format=yuv420p[b0]"]
    for k, ov in enumerate(overlays, start=1):
        inputs += ["-loop", "1", "-t", f"{total:.3f}", "-i", str(ov["path"])]
        start, end = max(0.0, ov["start"]), min(total, ov["end"])
        graph.append(f"[b{k - 1}][{k}:v]overlay=x={int(ov['x'])}:y={int(ov['y'])}:"
                     f"enable='between(t,{start:.3f},{end:.3f})'[b{k}]")
    last = f"[b{len(overlays)}]"
    ffmpeg([*inputs, "-filter_complex", ";".join(graph), "-map", last, "-frames:v", str(frames), "-an",
            *_encode_args(intermediate), str(out)], timeout=1800)
    return out


def _xfade_once(clips: list[tuple[Path, float]], pad: float, out: Path, transitions: list[str], seed: str,
                hard_cuts: set[int], intermediate: bool = False) -> None:
    if len(clips) == 1:
        shutil.copyfile(clips[0][0], out)
        return
    rng = random.Random(seed)
    inputs: list[str] = []
    for p, _ in clips:
        inputs += ["-i", str(p)]
    graph, prev, offset = [], "[0:v]", 0.0
    for i in range(1, len(clips)):
        offset += clips[i - 1][1]
        tr = "fadeblack" if i in hard_cuts else rng.choice(transitions)
        graph.append(f"{prev}[{i}:v]xfade=transition={tr}:duration={pad:.3f}:offset={offset:.3f}[x{i}]")
        prev = f"[x{i}]"
    ffmpeg([*inputs, "-filter_complex", ";".join(graph), "-map", prev, "-an", *_encode_args(intermediate), str(out)],
           timeout=3600)


def _segment(i: int, clips: list[tuple[Path, float]], cut: int, pf: int, pad: float, tr: str | None,
             out: Path) -> None:
    """One piece of the joined video: clip i's own part (after the previous cross-fade, up to frame `cut` of the
    clip) followed by the `pf`-frame cross-fade into clip i+1 (`tr`; None for the last clip, which runs on for its
    `pf` frames). Frame counts, not seconds, so the pieces meet exactly; a clip that is a frame short repeats its
    last frame (the same as the chain's own padding)."""
    start = pf if i > 0 else 0
    clip = clips[i][0]
    hold = f"tpad=stop_mode=clone:stop_duration={pad + 0.2:.3f}"
    if tr is None:
        end = cut + pf
        graph = f"[0:v]{hold},trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS[v]"
        ffmpeg(["-i", str(clip), "-filter_complex", graph, "-map", "[v]", "-frames:v", str(end - start), "-an",
                *_encode_args(), str(out)], timeout=1800)
        return
    graph = (f"[0:v]{hold},split=2[a0][a1];[a0]trim=start_frame={start}:end_frame={cut},setpts=PTS-STARTPTS[body];"
             f"[a1]trim=start_frame={cut}:end_frame={cut + pf},setpts=PTS-STARTPTS[tail];"
             f"[1:v]{hold},trim=start_frame=0:end_frame={pf},setpts=PTS-STARTPTS[head];"
             f"[tail][head]xfade=transition={tr}:duration={pf / config.FPS:.4f}:offset=0[x];"
             f"[body][x]concat=n=2:v=1:a=0[v]")
    ffmpeg(["-i", str(clip), "-i", str(clips[i + 1][0]), "-filter_complex", graph, "-map", "[v]",
            "-frames:v", str(cut - start + pf), "-an", *_encode_args(), str(out)], timeout=1800)


def _join_segments(clips: list[tuple[Path, float]], pad: float, out: Path, fmt: str, transitions: list[str],
                   seed: str, hard_cuts: set[int]) -> None:
    """Fast join: the same picture as one long cross-fade chain, but each clip is encoded once, in its own piece,
    side by side, and the pieces are joined by stream copy. (The chain re-encoded the whole video in one FFmpeg,
    and twice for long videos.) Cut points come from the running total, so the picture never drifts from the
    narration however many scenes there are."""
    fps = config.FPS
    pf = max(1, round(pad * fps))
    rng = random.Random(seed)
    trs = ["fadeblack" if i in hard_cuts else rng.choice(transitions) for i in range(1, len(clips))]
    bounds, t = [0], 0.0
    for _, c in clips:
        t += c
        bounds.append(round(t * fps))
    cuts = [bounds[i + 1] - bounds[i] for i in range(len(clips))]   # clip i starts at bounds[i] on the timeline
    parts = [out.with_name(f"{out.stem}_s{i:03d}.mp4") for i in range(len(clips))]
    run_parallel([lambda i=i: _segment(i, clips, cuts[i], pf, pad, trs[i] if i < len(trs) else None, parts[i])
                  for i in range(len(clips))])
    w, h = config.frame_size(fmt)
    concat(parts, out, w=w, h=h)
    for p in parts:
        p.unlink(missing_ok=True)
    out.with_suffix(".concat.txt").unlink(missing_ok=True)


def join(clips: list[tuple[Path, float]], pad: float, out: Path, fmt: str, seed: str,
         chapter_starts: set[int] | None = None) -> float:
    """Cross-fade clips. Each clip is (path, content_seconds) and physically lasts content+pad.
    Returns the content duration of the result (which itself lasts content+pad)."""
    transitions = TRANSITIONS_SHORT if fmt == "short" else TRANSITIONS_LONG
    chapter_starts = chapter_starts or set()
    if len(clips) > 1 and pad > 0 and all(c >= 2 * pad + 0.1 for _, c in clips):
        _join_segments(clips, pad, out, fmt, transitions, seed, {i for i in range(len(clips)) if i in chapter_starts})
        return sum(c for _, c in clips)
    if len(clips) <= GROUP:
        _xfade_once(clips, pad, out, transitions, seed, {i for i in range(len(clips)) if i in chapter_starts})
        return sum(c for _, c in clips)
    groups: list[tuple[Path, float]] = []
    jobs = []
    for g in range(0, len(clips), GROUP):
        part = clips[g:g + GROUP]
        gp = out.with_name(f"{out.stem}_g{g // GROUP:02d}.mp4")
        local_starts = {i - g for i in chapter_starts if g <= i < g + GROUP}
        # groups are independent and re-encoded once more by the top-level join: render them side by side, fast
        jobs.append(lambda part=part, gp=gp, g=g, ls=local_starts:
                    _xfade_once(part, pad, gp, transitions, f"{seed}:{g}", ls, intermediate=True))
        groups.append((gp, sum(c for _, c in part)))
    run_parallel(jobs)
    return join(groups, pad, out, fmt, seed + ":top")


def mix_audio_and_mux(video: Path, narration: Path, total: float, out: Path) -> None:
    music = settings.get("render.music_file")
    vol = float(settings.get("render.music_volume"))
    inputs = ["-i", str(video), "-i", str(narration)]
    norm = "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000"
    if music and Path(music).exists():
        inputs += ["-stream_loop", "-1", "-i", music]
        fade_out = max(0.0, total - 2.5)
        graph = (f"[2:a]aresample=48000,volume={vol:.3f},atrim=0:{total:.3f},afade=t=in:d=1.5,"
                 f"afade=t=out:st={fade_out:.3f}:d=2.5[m];"
                 f"[1:a]aresample=48000,apad,atrim=0:{total:.3f},asplit=2[n1][n2];"
                 f"[m][n1]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[md];"
                 f"[n2][md]amix=inputs=2:duration=first:normalize=0,{norm}[a]")
    else:
        graph = f"[1:a]aresample=48000,apad,atrim=0:{total:.3f},{norm}[a]"
    ffmpeg([*inputs, "-filter_complex", graph, "-map", "0:v", "-map", "[a]", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", "-t", f"{total:.3f}", str(out)],
           timeout=1800)


def extract_frame(video: Path, at: float, out: Path) -> Path:
    ffmpeg(["-ss", f"{at:.2f}", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(out)], timeout=120)
    return out


# Bump when a change to the drawing/encoding code changes what an identical input renders to, so cached clips
# made by older code are rebuilt instead of reused.
RENDER_VERSION = "2026-10-30.1"


def file_id(path: str | Path | None) -> str | None:
    """Identity of an input file for cache keys: path + size + modification time (a re-made file changes it)."""
    if not path:
        return None
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return f"{p}|missing"
    return f"{p}|{st.st_size}|{st.st_mtime_ns}"


def stream_format(video: Path) -> dict[str, Any]:
    """The properties that must match for stream-copy concat: codec, size, pixel format, frame rate, audio."""
    import json
    r = subprocess.run([config.FFPROBE, "-v", "error", "-show_entries",
                        "stream=codec_type,codec_name,width,height,pix_fmt,r_frame_rate,sample_rate,channels",
                        "-of", "json", str(video)], capture_output=True, text=True, timeout=60)
    streams = json.loads(r.stdout or "{}").get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return {"v": (v.get("codec_name"), v.get("width"), v.get("height"), v.get("pix_fmt"), v.get("r_frame_rate")),
            "a": (a.get("codec_name"), a.get("sample_rate"), a.get("channels")) if a else None}


def concat(clips: list[Path], out: Path, *, w: int, h: int) -> str:
    """Join clips end to end. Stream copy only when every clip has the same codec, size, pixel format, frame rate
    and audio layout; otherwise every clip is normalised and re-encoded (a mismatched copy-concat plays wrongly).
    Returns 'copy' or 'reencode'."""
    if not clips:
        raise RenderError("nothing to join")
    fmts = [stream_format(c) for c in clips]
    if all(f == fmts[0] for f in fmts) and fmts[0]["v"][0]:
        lst = out.with_suffix(".concat.txt")
        lst.write_text("".join(f"file '{c}'\n" for c in clips))
        ffmpeg(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)])
        return "copy"
    inputs = [x for c in clips for x in ("-i", str(c))]
    graph = "".join(f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
                    f"setsar=1,fps={config.FPS},format=yuv420p[v{i}];" for i in range(len(clips)))
    graph += "".join(f"[v{i}]" for i in range(len(clips))) + f"concat=n={len(clips)}:v=1:a=0[v]"
    ffmpeg([*inputs, "-filter_complex", graph, "-map", "[v]", "-an", *_encode_args(), str(out)])
    return "reencode"


def last_frame(video: Path, out: Path) -> Path:
    """The final picture of a clip (to continue a shot from where its motion ended, not from its first frame)."""
    ffmpeg(["-sseof", "-0.1", "-i", str(video), "-frames:v", "1", "-update", "1", "-q:v", "2", str(out)], timeout=120)
    if not out.exists():
        raise RenderError(f"could not read the last frame of {video.name}")
    return out
