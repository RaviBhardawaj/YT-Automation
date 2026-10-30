"""Render reliability: complete cache keys (a re-made take at the same path is rebuilt), concat that only
stream-copies matching clips, and shots that continue from where their motion ended — never jumping back to the
first picture. Uses tiny FFmpeg test clips; nothing external is called.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins
os.environ.setdefault("STUDIO_OFFLINE", "1")              # never call the free image services

from PIL import Image  # noqa: E402

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio.film_studio import edit  # noqa: E402
from studio.media import render  # noqa: E402

TMP = Path(tempfile.mkdtemp())


def clip(name: str, colour: str, size: str = "320x240", d: float = 1.0, then: str | None = None) -> Path:
    out = TMP / name
    src = f"color=c={colour}:s={size}:r={config.FPS}:d={d}"
    args = ["-f", "lavfi", "-i", src]
    if then:
        args += ["-f", "lavfi", "-i", f"color=c={then}:s={size}:r={config.FPS}:d={d}",
                 "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]"]
    subprocess.run([config.FFMPEG, "-v", "error", "-y", *args, "-pix_fmt", "yuv420p", "-c:v", "libx264", str(out)],
                   check=True)
    return out


def colour_at_end(video: Path) -> tuple[int, int, int]:
    pic = render.last_frame(video, TMP / f"{video.stem}_end.png")
    return Image.open(pic).convert("RGB").resize((1, 1)).getpixel((0, 0))


class CacheKeyTests(unittest.TestCase):
    def test_a_remade_take_at_the_same_path_changes_the_key(self):
        src = clip("take.mp4", "red")
        c = {"kind": "shot", "source": str(src), "keyframe": None, "seconds": 2.0, "motion_type": "video"}
        before = edit.cache_key(c, {}, (1920, 1080))
        time.sleep(0.01)
        clip("take.mp4", "blue", d=1.2)                          # same path, new file
        self.assertNotEqual(before, edit.cache_key(c, {}, (1920, 1080)))

    def test_card_text_size_and_renderer_version_are_in_the_key(self):
        c = {"kind": "title", "seconds": 3.5}
        a = edit.cache_key(c, {"title": "One"}, (1920, 1080))
        self.assertNotEqual(a, edit.cache_key(c, {"title": "Two"}, (1920, 1080)))
        self.assertNotEqual(a, edit.cache_key(c, {"title": "One"}, (1280, 720)))
        self.assertEqual(a["renderer"], render.RENDER_VERSION)


class ConcatTests(unittest.TestCase):
    def test_matching_clips_are_copied_mismatched_ones_reencoded(self):
        a, b = clip("a.mp4", "red"), clip("b.mp4", "green")
        self.assertEqual(render.concat([a, b], TMP / "ab.mp4", w=320, h=240), "copy")
        big = clip("big.mp4", "blue", size="640x480")
        out = TMP / "mixed.mp4"
        self.assertEqual(render.concat([a, big], out, w=320, h=240), "reencode")
        fmt = render.stream_format(out)
        self.assertEqual((fmt["v"][1], fmt["v"][2]), (320, 240))
        dur = float(subprocess.run([config.FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of",
                                    "csv=p=0", str(out)], capture_output=True, text=True).stdout)
        self.assertAlmostEqual(dur, 2.0, delta=0.2)


class ShotEndingTests(unittest.TestCase):
    def test_short_motion_continues_from_its_last_frame_not_its_key_picture(self):
        src = clip("motion.mp4", "red", size="640x360", d=0.6, then="blue")   # starts red, ends blue
        key = TMP / "key.png"
        Image.new("RGB", (640, 360), "red").save(key)                       # the picture the motion started from
        work = TMP / "edit"
        work.mkdir(exist_ok=True)
        c = {"kind": "shot", "source": str(src), "keyframe": str(key), "seconds": 3.0, "motion_type": "video",
             "subtitle": None, "speech": None}
        out = edit._clip_for(c, 1, work, {"title": "t"}, "s")
        r, g, b = colour_at_end(out)
        self.assertGreater(b, r)                                            # ends blue (motion's end), not red
        self.assertTrue((work / "c0001.json").exists())


class FreezeTests(unittest.TestCase):
    def test_a_long_frozen_picture_is_flagged_moving_video_is_not(self):
        from studio.media import qa
        still = clip("still.mp4", "gray", d=10.0)
        moving = TMP / "moving.mp4"
        subprocess.run([config.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"testsrc=size=320x240:rate={config.FPS}:d=10", "-pix_fmt", "yuv420p", str(moving)], check=True)
        self.assertFalse(qa.freeze_check(still, 10.0)["ok"])
        self.assertEqual(qa.freeze_check(still, 10.0)["severity"], "warn")       # needs a look; never auto-published
        self.assertTrue(qa.freeze_check(moving, 10.0)["ok"])


class ThumbnailTests(unittest.TestCase):
    def test_the_strongest_relevant_picture_wins_not_the_first_or_largest(self):
        from PIL import ImageDraw, ImageFilter

        from studio.media import thumbnail
        dull = TMP / "dull.jpg"                     # first and largest, but dark and blurry, poorly matched
        Image.new("RGB", (4000, 2250), (20, 20, 25)).filter(ImageFilter.GaussianBlur(8)).save(dull)
        good = TMP / "good.jpg"
        im = Image.new("RGB", (1280, 720), (30, 90, 200))
        d = ImageDraw.Draw(im)
        for x in range(0, 1280, 40):
            d.rectangle((x, 200, x + 20, 520), fill=(250, 200, 30))
        im.save(good)
        card = TMP / "card.jpg"
        Image.new("RGB", (1920, 1080), "white").save(card)
        assets = [{"scene_index": 0, "kind": "image", "provider": "pixabay", "local_path": str(dull),
                   "relevance": '{"score": 3}'},
                  {"scene_index": 4, "kind": "image", "provider": "pixabay", "local_path": str(good),
                   "relevance": {"score": 9}},
                  {"scene_index": 1, "kind": "image", "provider": "procedural", "local_path": str(card)}]
        img, why = thumbnail.pick_background(assets, TMP)
        self.assertEqual((why["scene"], img.size), (4, (1280, 720)))
        self.assertEqual(thumbnail.pick_background([assets[2]], TMP), (None, {}))   # cards are never used


if __name__ == "__main__":
    unittest.main()
