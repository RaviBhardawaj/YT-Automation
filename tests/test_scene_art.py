"""Drawn story backgrounds for animated episodes: checked by a vision model and re-prompted automatically when they
aren't good, never holding a video up when the free allowances are spent.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins
os.environ.setdefault("STUDIO_OFFLINE", "1")              # never call the free image services

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio.creative import cartoon, scene_art  # noqa: E402


def fake_draw(prompts):
    def draw(prompt, out, ref):
        prompts.append(prompt)
        Image.new("RGB", (64, 36), (240, 200, 40)).save(out)
        return "hf_flux"
    return draw


class FixLoopTests(unittest.TestCase):
    def test_bad_picture_is_redrawn_with_the_checks_fix(self):
        prompts = []
        checks = iter([{"score": 8, "people": True, "fix": "remove the boy", "shows": "a boy in a room"},
                       {"score": 9, "people": False, "fix": "", "shows": "room with a glowing diya"}])
        target = Path(tempfile.mkdtemp()) / "scene_01.png"
        with mock.patch.object(scene_art, "_draw", fake_draw(prompts)), \
                mock.patch.object(scene_art.vision, "available", lambda: True), \
                mock.patch.object(scene_art.vision, "check_art", lambda raw, want: next(checks)):
            res = scene_art.draw_checked("living room with a glowing diya", target, None)
        self.assertEqual(res["attempts"], 2)
        self.assertIn("remove the boy", prompts[1])                 # the check's own words fix the prompt
        self.assertIn("NO people", prompts[1])
        self.assertEqual(res["check"]["score"], 9)
        self.assertTrue(target.exists())
        self.assertEqual(list(target.parent.glob("*_try*.png")), [])  # only the chosen picture is kept

    def test_stops_after_the_fix_limit_and_keeps_the_best(self):
        prompts = []
        scores = iter([4, 6, 5])
        target = Path(tempfile.mkdtemp()) / "scene_02.png"
        with mock.patch.object(scene_art, "_draw", fake_draw(prompts)), \
                mock.patch.object(scene_art.vision, "available", lambda: True), \
                mock.patch.object(scene_art.vision, "check_art",
                                  lambda raw, want: {"score": next(scores), "people": False, "fix": "show the lights"}):
            res = scene_art.draw_checked("disco lights", target, None)
        self.assertEqual(len(prompts), scene_art.MAX_FIXES + 1)
        self.assertEqual(res["check"]["score"], 6)

    def test_without_a_vision_model_the_first_picture_is_used(self):
        prompts = []
        target = Path(tempfile.mkdtemp()) / "scene_03.png"
        with mock.patch.object(scene_art, "_draw", fake_draw(prompts)), \
                mock.patch.object(scene_art.vision, "available", lambda: False):
            res = scene_art.draw_checked("room", target, None)
        self.assertEqual((len(prompts), res["check"]), (1, None))

    def test_spent_allowance_reuses_a_drawn_set_of_the_same_place(self):
        d = Path(tempfile.mkdtemp())
        pkg = {"title": "t", "scenes": [{"location": "home", "summary": "a"}, {"location": "home", "summary": "b"},
                                        {"location": "park", "summary": "c"}]}
        made = iter([True, False, False])

        def draw_once(want, target, ref):
            if next(made):
                Image.new("RGB", (64, 36)).save(target)
                return {"by": "cloudflare", "check": None, "attempts": 1, "prompt": want}
            return None
        with mock.patch.object(scene_art, "draw_checked", draw_once), \
                mock.patch.object(scene_art, "design", lambda pkg: {i: "x" for i in range(3)}):
            n = scene_art.ensure(pkg, d)
        self.assertEqual(n, 2)
        self.assertEqual(pkg["scenes"][1]["bg_image"], pkg["scenes"][0]["bg_image"])
        self.assertNotIn("bg_image", pkg["scenes"][2])           # another place: keeps the built-in set


class RenderTests(unittest.TestCase):
    def test_drawn_set_is_used_and_the_centre_stays_clear(self):
        f = Path(tempfile.mkdtemp()) / "bg.png"
        Image.new("RGB", (1344, 768), (10, 200, 30)).save(f)
        img = cartoon.backdrop({"bg_image": str(f)}, 1280, 720)
        self.assertEqual(img.size, (1280, 720))
        self.assertEqual(img.getpixel((640, 100)), (10, 200, 30))
        for n in (1, 2, 3, 4):
            self.assertTrue(all(abs(x - 640) > 180 for x in cartoon.stage_positions(n, 1280)), n)
        built_in = cartoon.backdrop({"location": "home"}, 1280, 720)   # no drawn set: the built-in one
        self.assertNotEqual(built_in.getpixel((640, 100)), (10, 200, 30))


if __name__ == "__main__":
    unittest.main()
