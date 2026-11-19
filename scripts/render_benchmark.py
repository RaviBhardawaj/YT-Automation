"""Render speed benchmark: the old way (one scene clip at a time, final-quality encode for every in-between file)
against the current way (scene clips side by side, fast in-between encodes, join groups side by side).

Makes synthetic still pictures and caption overlays, then renders the same scene clips + cross-fade join twice.
Nothing is uploaded or called over the network; it uses a throwaway data folder.

  .venv/bin/python scripts/render_benchmark.py                 # 16 long-video scenes of 6 s
  .venv/bin/python scripts/render_benchmark.py --scenes 30 --seconds 8 --format short
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-bench-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-bench")
os.environ.setdefault("STUDIO_OFFLINE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw  # noqa: E402

from studio import config, db, settings  # noqa: E402
from studio.media import render  # noqa: E402


def make_inputs(n: int, fmt: str, work: Path) -> list[tuple[Path, Path]]:
    w, h = config.frame_size(fmt)
    rng = random.Random(7)
    out = []
    for i in range(n):
        im = Image.new("RGB", (w, h), tuple(rng.randrange(40, 220) for _ in range(3)))
        d = ImageDraw.Draw(im)
        for _ in range(60):                       # some detail so the encoder has real work
            x, y = rng.randrange(w), rng.randrange(h)
            d.ellipse((x, y, x + rng.randrange(20, 300), y + rng.randrange(20, 300)),
                      fill=tuple(rng.randrange(256) for _ in range(3)))
        pic = work / f"pic{i:03d}.jpg"
        im.save(pic, quality=92)
        cap = Image.new("RGBA", (w * 3 // 4, h // 10), (0, 0, 0, 160))
        cp = work / f"cap{i:03d}.png"
        cap.save(cp)
        out.append((pic, cp))
    return out


def run(inputs, fmt: str, seconds: float, work: Path, *, new: bool) -> float:
    w, h = config.frame_size(fmt)
    pad = 0.35
    work.mkdir(exist_ok=True)
    t = time.perf_counter()
    jobs, clips = [], []
    for i, (pic, cap) in enumerate(inputs):
        ov = [{"path": cap, "x": w // 8, "y": h * 8 // 10, "start": 0.2, "end": seconds}]
        clip = work / f"scene_{i:03d}.mp4"
        jobs.append(lambda i=i, pic=pic, ov=ov, clip=clip: render.scene_clip(
            i, {"kind": "image", "local_path": str(pic)}, seconds, pad, ov, fmt, work, clip, "bench",
            look="cartoon", intermediate=new))
        clips.append((clip, seconds))
    render.run_parallel(jobs)
    if new:
        render.join(clips, pad, work / "joined.mp4", fmt, "bench")
    else:
        legacy_join(clips, pad, work / "joined.mp4", fmt, "bench")
    return time.perf_counter() - t


def legacy_join(clips, pad: float, out: Path, fmt: str, seed: str) -> None:
    """The join as it was: one long cross-fade chain, in groups of 12 plus a top-level chain for long videos."""
    tr = render.TRANSITIONS_SHORT if fmt == "short" else render.TRANSITIONS_LONG
    if len(clips) <= render.GROUP:
        render._xfade_once(clips, pad, out, tr, seed, set())
        return
    groups = []
    for g in range(0, len(clips), render.GROUP):
        part = clips[g:g + render.GROUP]
        gp = out.with_name(f"{out.stem}_g{g // render.GROUP:02d}.mp4")
        render._xfade_once(part, pad, gp, tr, f"{seed}:{g}", set())
        groups.append((gp, sum(c for _, c in part)))
    legacy_join(groups, pad, out, fmt, seed + ":top")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenes", type=int, default=16)
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--format", choices=["long", "short"], default="long")
    ap.add_argument("--workers", type=int, default=0, help="force this many side by side (0 = automatic)")
    a = ap.parse_args()
    db.migrate()
    work = Path(tempfile.mkdtemp(prefix="render-bench-"))
    inputs = make_inputs(a.scenes, a.format, work)

    settings.set("render.parallel_clips", 1)               # old: one at a time, every file at final quality
    settings.set("render.intermediate_preset", settings.get("render.preset"))
    settings.set("render.intermediate_crf", settings.get("render.crf"))
    old = run(inputs, a.format, a.seconds, work / "old", new=False)

    settings.set("render.parallel_clips", a.workers)       # new: the defaults
    settings.set("render.intermediate_preset", settings.DEFAULTS["render.intermediate_preset"])
    settings.set("render.intermediate_crf", settings.DEFAULTS["render.intermediate_crf"])
    new = run(inputs, a.format, a.seconds, work / "new", new=True)

    from studio import resources
    print(f"{a.scenes} scenes x {a.seconds}s ({a.format}), {os.cpu_count()} cores, "
          f"{resources.render_workers()} clips side by side")
    print(f"old: {old:6.1f} s\nnew: {new:6.1f} s\nspeed-up: {old / new:.2f}x")


if __name__ == "__main__":
    main()
