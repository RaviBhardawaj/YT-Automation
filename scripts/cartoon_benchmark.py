"""Cartoon (kids/comedy story) drawing speed: scenes drawn one after another (old) against side by side (new).

Draws a synthetic episode — a title card, N scenes with two characters talking, an end card — with no network and a
throwaway data folder, and checks both results have the same number of frames.

  .venv/bin/python scripts/cartoon_benchmark.py --scenes 8 --seconds 10
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if "STUDIO_DATA_DIR" not in os.environ:
    tmp = Path(tempfile.mkdtemp(prefix="studio-bench-"))
    shutil.copytree(ROOT / "data" / "brand", tmp / "brand")          # the bundled fonts
    os.environ["STUDIO_DATA_DIR"] = str(tmp)
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-bench")
os.environ.setdefault("STUDIO_OFFLINE", "1")
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from studio import db, resources, settings  # noqa: E402
from studio.creative import cartoon  # noqa: E402
from studio.creative.characters import normalise_design  # noqa: E402


def jobs(n: int, seconds: float) -> list[dict]:
    cast = {"a": {"name": "Tara", "design": normalise_design({"shirt": "red"}), "colour": (255, 120, 120)},
            "b": {"name": "Momo", "design": normalise_design({"shirt": "blue", "hair": "long"}), "colour": (120, 180, 255)}}
    out = [{"what": "card", "text": [("A Test Story", 58, (255, 230, 40))], "seconds": 2.6, "kw": {"cast": list(cast.values())}}]
    rng = np.random.default_rng(1)
    for i in range(n):
        lines, t = [], 0.4
        while t < seconds - 1.5:
            d = 2.0
            lines.append({"speaker": "ab"[len(lines) % 2], "text": f"Line {len(lines) + 1} of scene {i + 1}, hello!",
                          "emotion": "happy", "start": t, "end": t + d, "env": rng.random(int(d * cartoon.FPS) + 1)})
            t += d + 0.3
        out.append({"what": "scene", "scene": {"location": cartoon.LOCATIONS[i % 10], "time": "day", "mood": "funny",
                                               "characters": ["a", "b"]},
                    "cast": cast, "lines": lines, "total": seconds, "hook": ""})
    out.append({"what": "card", "text": [("Bye!", 40, (255, 255, 255))], "seconds": 3.2, "kw": {"cast": list(cast.values())}})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenes", type=int, default=8)
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--format", choices=["long", "short"], default="long")
    ap.add_argument("--workers", type=int, default=0, help="force this many side by side (0 = automatic)")
    a = ap.parse_args()
    db.migrate()
    work = Path(tempfile.mkdtemp(prefix="cartoon-bench-"))
    js = jobs(a.scenes, a.seconds)
    settings.set("render.parallel_clips", 1)
    t = time.perf_counter()
    f_old = cartoon.render_segments(js, a.format, work / "old.mp4")
    old = time.perf_counter() - t
    settings.set("render.parallel_clips", a.workers)
    t = time.perf_counter()
    f_new = cartoon.render_segments(js, a.format, work / "new.mp4")
    new = time.perf_counter() - t
    print(f"{a.scenes} scenes x {a.seconds}s ({a.format}), {os.cpu_count()} cores, {resources.draw_workers()} drawn side by side")
    print(f"frames: {f_old} / {f_new}")
    print(f"old: {old:6.1f} s\nnew: {new:6.1f} s\nspeed-up: {old / new:.2f}x")


if __name__ == "__main__":
    main()
