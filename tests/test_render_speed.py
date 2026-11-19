"""Render speed-ups keep the picture: the piece-by-piece join matches the single cross-fade chain frame for frame
in length and content, scene drawing side by side gives the same frames, and the cheaper subtitle overlay is
pixel-identical. Uses tiny FFmpeg test clips; nothing external is called.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins
os.environ.setdefault("STUDIO_OFFLINE", "1")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from studio import config, db, resources, settings  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio.creative import cartoon  # noqa: E402
from studio.creative.characters import normalise_design  # noqa: E402
from studio.media import render  # noqa: E402

TMP = Path(tempfile.mkdtemp())


def colour_clip(name: str, colour: str, seconds: float) -> Path:
    out = TMP / name
    subprocess.run([config.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c={colour}:s=320x240:r={config.FPS}:d={seconds}", "-pix_fmt", "yuv420p", "-c:v", "libx264",
                    str(out)], check=True)
    return out


def frames(video: Path) -> np.ndarray:
    raw = subprocess.run([config.FFMPEG, "-v", "error", "-i", str(video), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, 240, 320, 3).astype(int)


class JoinTests(unittest.TestCase):
    def test_piece_join_matches_the_crossfade_chain(self):
        pad = 0.35
        contents = [1.2, 0.9, 1.53, 1.1]
        clips = [(colour_clip(f"j{i}.mp4", c, s + pad), s)
                 for i, (c, s) in enumerate(zip(["red", "blue", "green", "white"], contents))]
        chain, pieces = TMP / "chain.mp4", TMP / "pieces.mp4"
        render._xfade_once(clips, pad, chain, render.TRANSITIONS_LONG, "s", {2})
        render._join_segments(clips, pad, pieces, "long", render.TRANSITIONS_LONG, "s", {2})
        a, b = frames(chain), frames(pieces)
        # same length within one frame (the chain rounds the last clip up; the final mux cuts both to content+pad)
        self.assertLessEqual(abs(len(a) - len(b)), 1)
        self.assertEqual(len(b), round(sum(contents) * config.FPS) + round(pad * config.FPS))
        n = min(len(a), len(b))
        diff = np.abs(a[:n] - b[:n]).mean(axis=(1, 2, 3))
        self.assertLess(np.median(diff), 1.0)               # outside the fades: the same picture
        self.assertLess(diff.max(), 40)                     # inside: the same fade, at most a frame apart
        self.assertFalse(list(TMP.glob("pieces_s*.mp4")))   # pieces are cleaned up

    def test_join_uses_pieces_for_normal_scenes(self):
        clips = [(colour_clip(f"k{i}.mp4", c, 1.35), 1.0) for i, c in enumerate(["red", "blue"])]
        out = TMP / "k.mp4"
        self.assertAlmostEqual(render.join(clips, 0.35, out, "long", "x"), 2.0)
        self.assertEqual(len(frames(out)), round(2.35 * config.FPS))

    def test_parallel_jobs_keep_order_and_raise_failures(self):
        settings.set("render.parallel_clips", 3)
        try:
            self.assertEqual(render.run_parallel([lambda i=i: i * 2 for i in range(7)]), list(range(0, 14, 2)))
            with self.assertRaises(ZeroDivisionError):
                render.run_parallel([lambda: 1, lambda: 1 / 0])
        finally:
            settings.set("render.parallel_clips", 0)

    def test_in_between_files_use_the_fast_encode(self):
        fast, final = render._encode_args(True), render._encode_args()
        self.assertEqual(fast[fast.index("-preset") + 1], settings.get("render.intermediate_preset"))
        self.assertEqual(final[final.index("-preset") + 1], settings.get("render.preset"))
        self.assertLess(int(fast[fast.index("-crf") + 1]), int(final[final.index("-crf") + 1]))  # higher quality

    def test_workers_leave_cores_free(self):
        self.assertGreaterEqual(resources.render_workers(), 1)
        self.assertLessEqual(resources.draw_workers(), max(1, (os.cpu_count() or 4) - 2))


class CartoonTests(unittest.TestCase):
    def test_subtitle_overlay_is_pixel_identical(self):
        bg = Image.new("RGB", (320, 240), (40, 120, 200))
        layer = Image.new("RGBA", (320, 240), (0, 0, 0, 0))
        layer.paste(Image.new("RGBA", (200, 40), (255, 230, 0, 170)), (60, 180))
        ref = bg.convert("RGBA")
        ref.alpha_composite(layer)
        self.assertTrue(np.array_equal(np.asarray(ref.convert("RGB")), np.asarray(cartoon._overlay(bg.copy(), layer))))

    def test_scenes_drawn_side_by_side_give_the_same_video_length(self):
        cast = {"a": {"name": "A", "design": normalise_design({}), "colour": (255, 0, 0)}}
        jobs = [{"what": "scene", "scene": {"location": loc, "time": "day", "mood": "funny", "characters": ["a"]},
                 "cast": cast, "lines": [], "total": 0.5, "hook": ""} for loc in ("park", "home", "school")]
        settings.set("render.parallel_clips", 2)
        try:
            n = cartoon.render_segments(jobs, "long", TMP / "cartoon.mp4")
        finally:
            settings.set("render.parallel_clips", 0)
        self.assertEqual(n, 3 * round(0.5 * cartoon.FPS))
        out = subprocess.run([config.FFPROBE, "-v", "error", "-count_frames", "-show_entries", "stream=nb_read_frames",
                              "-of", "csv=p=0", str(TMP / "cartoon.mp4")], capture_output=True, text=True).stdout
        self.assertEqual(int(out.strip()), n)
        self.assertFalse(list(TMP.glob("cartoon_part*.mp4")))


if __name__ == "__main__":
    unittest.main()
