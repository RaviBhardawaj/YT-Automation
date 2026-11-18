"""Trending-story remakes: today's most-viewed story idea → our own long episode + Short, one of each a day.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")
os.environ.setdefault("STUDIO_OFFLINE", "1")

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import auth, channels, jobs, settings, tasks  # noqa: E402
from studio.creative import remake  # noqa: E402

auth.ensure_default_workspace()
channels.ensure_default()


def chart(region, category, *, limit=25):
    return [{"id": f"{region}{category}{i}", "url": f"https://www.youtube.com/watch?v={region}{category}{i}",
             "title": f"Billi ne kiya homework {category}{i}" if i else "SHOCKING secret of the haunted school",
             "description": "a cat does the homework", "channel": "Other", "published_at": db.iso_in(-86400),
             "views": 100_000 * (i + 1), "seconds": 300, "category": category, "region": region}
            for i in range(3)]


def good_plan(index=1, **over):
    base = dict(source_index=index, premise="a pet secretly helps a child with a chore and it goes hilariously wrong",
                why_it_trends="pets plus school", use_cast=True, new_characters=[],
                changes=["set in a village fair, not a school", "a goat instead of a cat", "the child saves the day"],
                long=remake.Episode(title="Chintu ki bakri aur mele ka jhoola", logline="Chintu's goat wants to win "
                                    "the fair prize; everything goes wrong; the goat wins by accident.",
                                    hook="the goat jumps onto the giant wheel"),
                short=remake.Episode(title="Bakri jhoole par!", logline="One minute of goat chaos at the fair.",
                                     hook="the goat is already on the wheel"))
    base.update(over)
    return remake.RemakePlan(**base)


class RemakeTests(unittest.TestCase):
    def setUp(self):
        self.ch = channels.create({"name": "Remake kids", "workflow": "animated_story", "made_for_kids": True,
                                   "language": "Hindi", "niche": "kids cartoons", "styles": ["kids", "comedy"]}, 1)

    def test_enable_sets_one_long_and_one_short_a_day(self):
        res = remake.enable(self.ch, True, times="08:30, 19:00")
        ch = channels.get(self.ch)
        self.assertTrue(res["trend_remake"])
        self.assertEqual(ch["schedule_time"], "08:30, 19:00")
        self.assertEqual(ch["formats"], "both")
        self.assertTrue(remake.enabled(ch))
        rows = db.q("SELECT payload FROM schedules WHERE task_type='channel_daily' AND channel_id=?", (self.ch,))
        self.assertEqual(len(rows), 2)
        with self.assertRaises(ValueError):
            remake.enable(self.ch, True, times="08:30")

    def test_factual_channel_cannot_switch_on(self):
        cid = channels.create({"name": "Facts", "workflow": "factual", "niche": "tech"}, 1)
        with self.assertRaises(ValueError):
            remake.enable(cid, True)

    def test_kids_channel_set_up_for_research_becomes_a_story_channel(self):
        cid = channels.create({"name": "Kids facts", "workflow": "factual", "niche": "kids", "made_for_kids": True}, 1)
        self.assertTrue(remake.can_offer(channels.get(cid)))
        remake.enable(cid, True)
        self.assertEqual(channels.get(cid)["workflow"], "animated_story")
        self.assertTrue(remake.enabled(channels.get(cid)))

    def test_candidates_skip_clickbait_old_and_used_videos(self):
        with mock.patch.object(remake.sources, "youtube_most_popular", chart):
            got = remake.candidates(channels.get(self.ch))
        self.assertTrue(got)
        self.assertFalse(any("SHOCKING" in v["title"] for v in got))      # kids channel: clickbait is skipped
        self.assertEqual(got[0]["views_per_day"], max(v["views_per_day"] for v in got))

    def test_make_today_writes_original_long_and_short_once(self):
        calls = []

        def fake(task, system, prompt, schema, **kw):
            calls.append(prompt)
            out = good_plan()
            kw["validate"](out)
            return out, {}
        with mock.patch.object(settings, "has_secret", lambda n: True), \
                mock.patch.object(remake.sources, "youtube_most_popular", chart), \
                mock.patch.object(remake.ai, "generate", fake):
            first = remake.make_today(self.ch)
            again = remake.make_today(self.ch)
        self.assertEqual(len(calls), 1)                                   # once a day
        self.assertEqual(first["long"], again["long"])
        long_t = db.q1("SELECT * FROM topics WHERE id=?", (first["long"],))
        short_t = db.q1("SELECT * FROM topics WHERE id=?", (first["short"],))
        self.assertEqual((long_t["format"], short_t["format"]), ("long", "short"))
        self.assertIn("nothing copied", long_t["summary"])
        self.assertIn("goat", long_t["summary"])
        self.assertIn("titles and descriptions only", calls[0])
        self.assertIn("Never ask viewers to comment", calls[0])           # kids channel rules in the prompt
        # the source is remembered, so tomorrow never retells the same video
        self.assertIn(first["source"]["url"], remake._used_sources(self.ch))

    def test_titles_too_close_to_the_source_are_rejected(self):
        cands = [{"title": "Billi ne kiya homework", "views_per_day": 10, "seconds": 60, "description": ""}] * 2
        close = good_plan(long=remake.Episode(title="Billi ne kiya homework", logline="x" * 20, hook="y"))
        few = good_plan(changes=["only one"])
        newcast = good_plan(use_cast=False, new_characters=[])
        asks = good_plan(short=remake.Episode(title="Subscribe karo! Bakri aayi", logline="x" * 20, hook="y"))
        for bad in (close, few, newcast, asks):
            def fake(task, system, prompt, schema, bad=bad, **kw):
                kw["validate"](bad)
                return bad, {}
            with mock.patch.object(remake.ai, "generate", fake), self.assertRaises(ValueError):
                remake.adapt(channels.get(self.ch), cands)

    def test_daily_slots_make_long_then_short_with_the_remake(self):
        remake.enable(self.ch, True)
        made = []

        def fake_create(cid, request="", **kw):
            made.append(kw)
            return {"production_ids": [len(made)]}
        with mock.patch.object(remake, "topic_for", lambda cid, fmt: (100 if fmt == "long" else 101, False)), \
                mock.patch.object(jobs, "create", fake_create), \
                mock.patch.object(channels, "long_made_today", lambda cid: False), \
                mock.patch("studio.plans.can", lambda *a: True):
            tasks._channel_daily({"channel_id": self.ch, "slot": 0}, {})
            tasks._channel_daily({"channel_id": self.ch, "slot": 1}, {})
            third = tasks._channel_daily({"channel_id": self.ch, "slot": 2}, {})
        self.assertEqual([(m["fmt"], m["idea_id"], m["use_cast"]) for m in made],
                         [("long", 100, False), ("short", 101, False)])
        self.assertIn("one long video and one Short", third["skipped"])

    def test_first_start_switches_on_kids_and_comedy_story_channels_once(self):
        settings.set("remake.auto_enabled_v2", False)
        comedy = channels.create({"name": "Fun", "workflow": "animated_story", "styles": ["comedy"], "niche": "fun"}, 1)
        facts = channels.create({"name": "Facts2", "workflow": "factual", "niche": "tech"}, 1)
        done = remake.auto_enable()
        self.assertIn(comedy, done)
        self.assertIn(self.ch, done)
        self.assertNotIn(facts, done)
        remake.enable(comedy, False)
        self.assertEqual(remake.auto_enable(), [])                        # never again: switching off sticks
        self.assertFalse(remake.enabled(channels.get(comedy)))


if __name__ == "__main__":
    unittest.main()
