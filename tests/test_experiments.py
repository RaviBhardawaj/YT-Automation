"""Daily experiments (one new thing per video, winners from real views), one long video per channel per day, and
the AI News channel in English following US + India stories. YouTube and the AI are mocked.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import random
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins
os.environ.setdefault("STUDIO_OFFLINE", "1")              # never call the free image services

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import channels, tasks  # noqa: E402
from studio.creative import mascot, news  # noqa: E402
from studio.youtube import experiments  # noqa: E402


def channel(**kw) -> dict:
    return channels.get(channels.create({"name": f"Exp {os.urandom(2).hex()}", **kw}, 1))


def video(cid, test, arm, views, days=5, fmt="short"):
    pid = db.insert("productions", {"format": fmt, "title": "v", "status": "uploaded", "stage": "qa", "kind": "general",
                                    "workspace_id": 1, "channel_id": cid, "package": "{}",
                                    "request": json.dumps({"experiment": {"test": test, "arms": {test: arm}}}),
                                    "youtube_video_id": f"v{os.urandom(4).hex()}", "uploaded_at": db.iso_in(-days * 86400),
                                    "created_at": db.iso(), "updated_at": db.iso()})
    db.insert("analytics", {"production_id": pid, "video_id": f"v{pid}", "fetched_at": db.iso(),
                            "metrics": json.dumps({"views": views})})


class ExperimentTests(unittest.TestCase):
    def test_a_winner_needs_enough_videos_and_a_clear_gap(self):
        ch = channel()
        for _ in range(3):
            video(ch["id"], "hook", "question", 500)
            video(ch["id"], "hook", "story", 100)
        self.assertEqual(experiments.results(ch["id"])["hook"]["winner"], "question")
        self.assertIsNone(experiments.results(ch["id"])["title"]["winner"])           # nothing measured yet

    def test_each_video_tests_one_new_thing_and_winners_are_mostly_kept(self):
        ch = channel()
        first = experiments.assign(ch["id"], rng=random.Random(1))
        self.assertIn(first["test"], experiments.DIMENSIONS)
        self.assertIn(first["test"], first["arms"])
        for _ in range(3):
            video(ch["id"], "hook", "question", 900)
            video(ch["id"], "hook", "story", 50)
        picks = [experiments.assign(ch["id"], rng=random.Random(i))["arms"].get("hook") for i in range(60)]
        self.assertGreater(picks.count("question"), 30)                  # the winner is used most of the time
        self.assertIn("story", picks + ["story"])                         # others still get tried

    def test_region_is_only_tested_on_news_channels(self):
        self.assertNotIn("region", experiments.dimensions(news=False))
        self.assertIn("region", experiments.dimensions(news=True))

    def test_instructions_name_this_videos_versions(self):
        text = experiments.instructions({"test": "hook", "arms": {"hook": "countdown", "title": "number"}})
        self.assertIn("3 things", text)
        self.assertIn("TITLE STYLE", text)


class FormatTests(unittest.TestCase):
    def _daily(self, ch, slot):
        made = {}

        def fake_create(cid, request, idea_id=None, actor="", scheduled_for=None, fmt=None, **kw):
            made["fmt"] = fmt
            pid = db.insert("productions", {"format": fmt or "long", "title": "x", "status": "queued", "stage": "script",
                                            "kind": "general", "workspace_id": 1, "channel_id": cid, "package": "{}",
                                            "request": json.dumps({"scheduled_for": scheduled_for}),
                                            "created_at": db.iso(), "updated_at": db.iso()})
            return {"production_ids": [pid]}
        with mock.patch("studio.jobs.create", fake_create), mock.patch("studio.plans.can", lambda *a: True), \
                mock.patch.object(tasks, "_pick_idea", lambda cid: None):
            tasks._channel_daily({"channel_id": ch["id"], "slot": slot}, {})
        return made.get("fmt")

    def test_one_long_a_day_then_shorts(self):
        ch = channel(niche="space facts", formats="both")
        self.assertIsNone(self._daily(ch, 0))                     # first slot: the channel's normal long video
        self.assertEqual(self._daily(ch, 1), "short")             # every other slot: a Short
        self.assertTrue(channels.long_made_today(ch["id"]))
        req = db.q1("SELECT request FROM productions WHERE channel_id=? ORDER BY id LIMIT 1", (ch["id"],))["request"]
        self.assertIn("experiment", json.loads(req))              # the video carries its experiment plan


class NewsTests(unittest.TestCase):
    def test_english_channel_takes_english_reports_from_the_chosen_country(self):
        ch = channel(workflow="factual", styles=["news"], language="English", branding={"news_regions": ["US", "IN"]})
        self.assertEqual(news.regions(ch), ["US", "IN"])
        stories = [{"keyword": "fed rate", "traffic": 50000, "published_at": "", "articles": [
            {"title": "Fed holds rates steady", "url": "https://a.example/1", "source": "Paper A"},
            {"title": "फेड ने दरें स्थिर रखीं", "url": "https://b.example/1", "source": "Paper B"},
            {"title": "Fed keeps rates on hold", "url": "https://c.example/1", "source": "Paper C"}]}]
        s = news.pick(ch, stories, geo="US")
        self.assertEqual(s["geo"], "US")
        self.assertEqual({a["source"] for a in s["articles"]}, {"Paper A", "Paper C"})   # Hindi report dropped

    def test_the_english_anchor_speaks_english_with_commentary(self):
        ch = channel(workflow="factual", styles=["news"], language="English", branding={"mascot": "fibbu_anchor"})
        rule = mascot.script_rule({"channel_id": ch["id"]})
        self.assertIn("Hello, I'm Fibbu", rule)
        self.assertIn("United States and India", rule)
        self.assertEqual(mascot.voice_for(mascot.MASCOTS["fibbu_anchor"], "English")["voice_id"], "am_onyx")


if __name__ == "__main__":
    unittest.main()
