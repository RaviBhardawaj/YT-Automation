"""The viral engine: hooks in the script prompts, honest scored titles, and checks that never block a video.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins
os.environ.setdefault("STUDIO_OFFLINE", "1")              # never call the free image services

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio.creative import viral  # noqa: E402


def opts(*rows):
    return viral.TitleSet(options=[viral.TitleOption(title=t, curiosity=c, clarity=7, emotion=e, honest=h)
                                   for t, c, e, h in rows]), {"provider": "test"}


class TitleTests(unittest.TestCase):
    def test_best_honest_title_wins_and_variants_are_kept(self):
        with mock.patch.object(viral.ai, "generate", lambda *a, **k: opts(
                ("Pappu turns the house into a disco!", 9, 9, True),
                ("You won't BELIEVE what happens", 10, 10, False),         # dishonest: never chosen
                ("Diwali episode", 3, 3, True))):
            res = viral.titles("the diya makes the lights dance", "old title", "English")
        self.assertEqual(res["title"], "Pappu turns the house into a disco!")
        self.assertEqual(len(res["variants"]), 3)
        self.assertLess(res["variants"][-1]["score"], 0)

    def test_kids_channel_never_gets_a_scary_clickbait_title(self):
        with mock.patch.object(viral.ai, "generate", lambda *a, **k: opts(
                ("डरावना भूत आया!", 10, 10, True), ("पप्पू की उड़ती साइकिल", 7, 7, True))):
            res = viral.titles("x", "old", "Hindi", kids=True)
        self.assertEqual(res["title"], "पप्पू की उड़ती साइकिल")

    def test_ai_down_keeps_the_current_title(self):
        with mock.patch.object(viral.ai, "generate", side_effect=RuntimeError("down")):
            self.assertEqual(viral.titles("x", "keep me", "English")["title"], "keep me")


class CheckTests(unittest.TestCase):
    def test_checks_warn_and_never_fail(self):
        res = viral.check(kind="story", fmt="long", seconds=754, title="x" * 80, thumb_text="one two three four five six",
                          opening_delay=2.9, kids=True)
        self.assertTrue(all(c["severity"] == "warn" for c in res))
        bad = {c["name"] for c in res if not c["ok"]}
        self.assertEqual(bad, {"viral: opens fast", "viral: video length", "viral: title", "viral: thumbnail words"})
        good = viral.check(kind="story", fmt="short", seconds=35, title="Pappu turns the house into a disco!",
                           thumb_text="डिस्को धमाका", opening_delay=0.35)
        self.assertTrue(all(c["ok"] for c in good))

    def test_hook_rules_reach_the_story_prompts(self):
        from studio.creative import screenplay
        src = open(screenplay.__file__, encoding="utf-8").read()
        self.assertIn("viral.HOOK_RULES", src)


if __name__ == "__main__":
    unittest.main()
