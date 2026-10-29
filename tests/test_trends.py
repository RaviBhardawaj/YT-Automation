"""Daily trend research (official API metadata only → original ideas) and Shorts that stand on their own.
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
from studio import auth, channels, settings  # noqa: E402
from studio.creative import trends, workflow  # noqa: E402

auth.ensure_default_workspace()
channels.ensure_default()


def popular(q, language, region, *, days, limit, order, duration):
    assert order == "viewCount" and region == "IN"
    short = duration == "short"
    return [{"url": f"https://www.youtube.com/watch?v={q[:3]}{i}{duration}", "title": f"{q} popular {i}",
             "published_at": db.iso_in(-2 * 86400),
             "metrics": {"views": 50_000 * (i + 1), "duration_s": 40 if short else 300, "channel": f"Chan {i}",
                         "channel_url": "u", "channel_subscribers": 1000}} for i in range(9)]


def patterns(*a, **k):
    return trends.TrendPatterns(themes=[trends.Theme(theme="a pet causes chaos at home", why_it_works="slapstick",
                                                     videos=5)],
                                title_style="short and excited", hook_style="the mess in the first second",
                                what_we_are_missing=["slow openings"], avoid=["copying"]), {}


class TrendTests(unittest.TestCase):
    def setUp(self):
        self.ch = channels.create({"name": "Trend kids", "workflow": "animated_story", "made_for_kids": True,
                                   "language": "Hindi", "niche": "kids cartoons"}, 1)

    def test_scan_uses_metadata_only_and_stores_patterns(self):
        with mock.patch.object(settings, "has_secret", lambda n: True), \
                mock.patch.object(trends.sources, "youtube_search", popular), \
                mock.patch.object(trends.ai, "generate", patterns):
            row = trends.scan(self.ch)
        self.assertGreater(row["stats"]["videos"], 10)
        self.assertEqual(row["stats"]["median_short_seconds"], 40)
        self.assertEqual(row["patterns"]["themes"][0]["theme"], "a pet causes chaos at home")
        self.assertTrue(all("download" not in k for v in row["videos"] for k in v))   # nothing is downloaded
        self.assertIn("ORIGINAL", trends.focus_text(row))

    def test_ideas_too_close_to_a_popular_title_are_dropped(self):
        row = {"id": db.insert("trend_scans", {"channel_id": self.ch, "workspace_id": 1, "created_at": db.iso()}),
               "channel_id": self.ch, "videos": [{"title": "Motu Patlu aur jadui samosa"}],
               "patterns": {"themes": []}, "stats": {}}

        def fake_generate(cid, count, fmts, focus=""):
            close = db.insert("topics", {"channel_id": cid, "title": "Motu Patlu aur jadui samosa", "status": "idea", "format": "long",
                                         "workspace_id": 1, "created_at": db.iso(), "updated_at": db.iso()})
            own = db.insert("topics", {"channel_id": cid, "title": "Pappu ki udti cycle", "status": "idea", "format": "short",
                                       "workspace_id": 1, "created_at": db.iso(), "updated_at": db.iso()})
            return {"created": [close, own], "duplicates_skipped": 0}
        with mock.patch("studio.creative.ideas.generate", fake_generate):
            res = trends.ideas_from(row)
        self.assertEqual(len(res["created"]), 1)
        self.assertEqual(res["dropped_too_close"], ["Motu Patlu aur jadui samosa"])
        self.assertIn("original", db.q1("SELECT why_now FROM topics WHERE id=?", (res["created"][0],))["why_now"])

    def test_kids_channel_drops_clickbait_titles(self):
        row = {"id": db.insert("trend_scans", {"channel_id": self.ch, "workspace_id": 1, "created_at": db.iso()}),
               "channel_id": self.ch, "videos": [], "patterns": {"themes": []}, "stats": {}}
        seen = {}

        def fake_generate(cid, count, fmts, focus=""):
            seen["focus"] = focus
            ids = [db.insert("topics", {"channel_id": cid, "title": t, "status": "idea", "format": "long",
                                        "workspace_id": 1, "created_at": db.iso(), "updated_at": db.iso()})
                   for t in ("🚨 Danger! चिंटू की किडनैपिंग?", "पप्पू और उड़ने वाली साइकिल 🚲")]
            return {"created": ids, "duplicates_skipped": 0}
        with mock.patch("studio.creative.ideas.generate", fake_generate):
            res = trends.ideas_from(row)
        self.assertEqual(len(res["created"]), 1)
        self.assertIn("MADE-FOR-KIDS", seen["focus"])

    def test_every_active_channel_except_the_nifty_data_channel(self):
        factual = channels.create({"name": "Facts", "workflow": "factual", "niche": "space science"}, 1)
        nifty = channels.create({"name": "Levels", "workflow": "nifty_daily", "niche": "nifty"}, 1)
        self.assertTrue(trends.eligible(channels.get(factual)))
        self.assertTrue(trends.eligible(channels.get(self.ch)))
        self.assertFalse(trends.eligible(channels.get(nifty)))
        with self.assertRaises(ValueError):
            trends.scan(nifty)
        q = trends._queries(channels.get(factual))                 # its own niche, not kids' cartoon searches
        self.assertIn("space science", q["long"])
        self.assertNotIn("hindi cartoon kahani", q["long"])


class TrendPageTests(unittest.TestCase):
    def test_ideas_page_shows_the_scan_for_a_story_channel(self):
        from tests.test_film_dashboard import client
        c, ws = client("trends-owner@test.local")
        cid = channels.create({"name": "Page kids", "workflow": "animated_story", "made_for_kids": True,
                               "language": "Hindi", "niche": "kids cartoons"}, ws)
        db.insert("trend_scans", {"channel_id": cid, "workspace_id": ws, "created_at": db.iso(),
                                  "videos": db.dumps([{"title": "Popular one", "url": "https://www.youtube.com/watch?v=x",
                                                       "channel": "C", "views_per_day": 12345, "seconds": 300}]),
                                  "stats": db.dumps({"videos": 1, "channels": 1, "median_long_seconds": 300}),
                                  "patterns": db.dumps({"themes": [{"theme": "talking vegetables", "videos": 3,
                                                                    "why_it_works": "silly"}],
                                                        "title_style": "t", "hook_style": "h",
                                                        "what_we_are_missing": ["simpler stories"]})})
        r = c.get(f"/ideas?channel={cid}")
        self.assertEqual(r.status_code, 200, r.text[:300])
        for text in ("Popular this week", "talking vegetables", "simpler stories", "12,345/day"):
            self.assertIn(text, r.text)


def episode(emotions_per_scene, seconds=6.0):
    scenes = [{"lines": [{"speaker": "a", "text": f"line {si}.{li}", "emotion": e} for li, e in enumerate(es)]}
              for si, es in enumerate(emotions_per_scene)]
    lines = [{"scene": si, "line": li, "duration": seconds} for si, es in enumerate(emotions_per_scene)
             for li, _ in enumerate(es)]
    return {"id": 1, "language": "Hindi", "package": {"title": "Episode", "scenes": scenes}, "timeline": {"lines": lines}}


class ShortsTests(unittest.TestCase):
    def test_a_short_must_end_on_a_payoff_and_be_long_enough(self):
        p = episode([["neutral", "neutral", "neutral"],          # no payoff → never a Short
                     ["neutral", "neutral", "laugh"],            # setup → laugh: a complete joke
                     ["laugh"],                                  # too short on its own
                     ["neutral", "surprised", "neutral", "neutral", "neutral", "neutral", "neutral",
                      "neutral", "neutral", "neutral"]])        # ends flat and is too long
        picks = workflow.pick_moments(p, 4)
        self.assertTrue(picks)
        for m in picks:
            self.assertGreaterEqual(m["seconds"], workflow.MIN_SHORT)
            self.assertLessEqual(m["seconds"], workflow.MAX_SHORT)
            self.assertNotIn(0, m["scenes"][-1:])
        self.assertEqual(picks[0]["scenes"][0], 1)
        flat = [s for m in picks for s in m["scenes"]]
        self.assertEqual(len(flat), len(set(flat)))              # Shorts never overlap

    def test_title_is_written_not_a_dialogue_line(self):
        p = episode([["neutral", "neutral", "laugh"]])
        m = workflow.pick_moments(p, 1)[0]
        made = workflow.ShortTitle(title="पप्पू ने समोसे में रॉकेट लगा दिया!", on_screen="समोसा उड़ेगा?")
        with mock.patch("studio.ai.generate", lambda *a, **k: (made, {})):
            title, setup = workflow.short_title(p, m)
        self.assertEqual(title, "पप्पू ने समोसे में रॉकेट लगा दिया!")
        self.assertNotIn("|", title)
        self.assertEqual(setup, "समोसा उड़ेगा?")
        with mock.patch("studio.ai.generate", side_effect=RuntimeError("down")):
            title, _ = workflow.short_title(p, m)                 # AI down: still a title, never a stop
        self.assertEqual(title, "Episode")


if __name__ == "__main__":
    unittest.main()
