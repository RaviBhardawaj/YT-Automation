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
from pathlib import Path
from typing import Any

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


def _encode_args() -> list[str]:
    return ["-c:v", "libx264", "-preset", settings.get("render.preset"), "-crf", str(settings.get("render.crf")),
            "-pix_fmt", "yuv420p", "-r", str(config.FPS)]


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
}


def _grade(look: str, kind: str) -> str:
    if settings.get("render.film_look") == "off" or kind == "anim":
        return ""                                            # charts/data animations stay exact
    return GRADES.get(look, "")


def scene_clip(idx: int, asset: dict[str, Any], content: float, pad: float, overlays: list[dict[str, Any]],
               fmt: str, work: Path, out: Path, seed: str, look: str = "real") -> Path:
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
            *_encode_args(), str(out)], timeout=1800)
    return out


def _xfade_once(clips: list[tuple[Path, float]], pad: float, out: Path, transitions: list[str], seed: str,
                hard_cuts: set[int]) -> None:
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
    ffmpeg([*inputs, "-filter_complex", ";".join(graph), "-map", prev, "-an", *_encode_args(), str(out)], timeout=3600)


def join(clips: list[tuple[Path, float]], pad: float, out: Path, fmt: str, seed: str,
         chapter_starts: set[int] | None = None) -> float:
    """Cross-fade clips. Each clip is (path, content_seconds) and physically lasts content+pad.
    Returns the content duration of the result (which itself lasts content+pad)."""
    transitions = TRANSITIONS_SHORT if fmt == "short" else TRANSITIONS_LONG
    chapter_starts = chapter_starts or set()
    if len(clips) <= GROUP:
        _xfade_once(clips, pad, out, transitions, seed, {i for i in range(len(clips)) if i in chapter_starts})
        return sum(c for _, c in clips)
    groups: list[tuple[Path, float]] = []
    for g in range(0, len(clips), GROUP):
        part = clips[g:g + GROUP]
        gp = out.with_name(f"{out.stem}_g{g // GROUP:02d}.mp4")
        local_starts = {i - g for i in chapter_starts if g <= i < g + GROUP}
        _xfade_once(part, pad, gp, transitions, f"{seed}:{g}", local_starts)
        groups.append((gp, sum(c for _, c in part)))
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
