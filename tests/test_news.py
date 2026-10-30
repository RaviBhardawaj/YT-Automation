"""Daily trending-news channel: story picking from Google Trends, two videos a day, the AI presenter, and the
News category / synthetic-content disclosure. External services are mocked; nothing real is called.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins
os.environ.setdefault("STUDIO_OFFLINE", "1")              # never call the free image services

from PIL import Image  # noqa: E402

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import channels, scheduler, tasks  # noqa: E402
from studio.creative import news  # noqa: E402

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>
<item><title>big match</title><ht:approx_traffic>20,000+</ht:approx_traffic><pubDate>Fri, 30 Oct 2026 00:30:00 -0700</pubDate>
 <ht:news_item><ht:news_item_title>India win the big match</ht:news_item_title><ht:news_item_url>https://a.example/1</ht:news_item_url><ht:news_item_source>Paper A</ht:news_item_source></ht:news_item>
 <ht:news_item><ht:news_item_title>Big match: India win</ht:news_item_title><ht:news_item_url>https://b.example/1</ht:news_item_url><ht:news_item_source>Paper B</ht:news_item_source></ht:news_item>
</item>
<item><title>one source</title><ht:approx_traffic>50K+</ht:approx_traffic><pubDate>Fri, 30 Oct 2026 00:30:00 -0700</pubDate>
 <ht:news_item><ht:news_item_title>Only one outlet has this</ht:news_item_title><ht:news_item_url>https://c.example/1</ht:news_item_url><ht:news_item_source>Paper C</ht:news_item_source></ht:news_item>
</item>
<item><title>ధర</title><ht:approx_traffic>90K+</ht:approx_traffic><pubDate>Fri, 30 Oct 2026 00:30:00 -0700</pubDate>
 <ht:news_item><ht:news_item_title>బంగారం ధర</ht:news_item_title><ht:news_item_url>https://d.example/1</ht:news_item_url><ht:news_item_source>Paper D</ht:news_item_source></ht:news_item>
 <ht:news_item><ht:news_item_title>వెండి ధర</ht:news_item_title><ht:news_item_url>https://e.example/1</ht:news_item_url><ht:news_item_source>Paper E</ht:news_item_source></ht:news_item>
</item>
</channel></rss>"""


def news_channel(name: str = "News test", **kw) -> dict:
    cid = channels.create({"name": name, "niche": "today's trending news", "workflow": "factual", "styles": ["news"],
                           "language": "Hinglish", **kw}, 1)
    return channels.get(cid)


class StoryTests(unittest.TestCase):
    def test_feed_is_parsed_with_traffic_and_articles(self):
        s = news.parse_trends(FEED)
        self.assertEqual([x["keyword"] for x in s], ["big match", "one source", "ధర"])
        self.assertEqual((s[0]["traffic"], s[1]["traffic"]), (20000, 50000))
        self.assertEqual(len(s[0]["articles"]), 2)

    def test_pick_needs_two_readable_outlets_and_skips_what_was_covered(self):
        ch = news_channel("Pick test")
        stories = news.parse_trends(FEED)
        self.assertEqual(news.pick(ch, stories)["keyword"], "big match")   # more searched ones fail the checks
        with mock.patch.object(news.sources, "page_text", side_effect=news.net.FetchError("offline")):
            tid = news.create_topic(ch, news.pick(ch, stories))
        t = db.q1("SELECT * FROM topics WHERE id=?", (tid,))
        self.assertEqual((t["idea_kind"], t["channel_id"]), ("news", ch["id"]))
        self.assertIn("Paper A", t["angle"])
        self.assertEqual(len(db.loads(t["evidence_ids"], [])), 2)           # today's articles are its sources
        self.assertIsNone(news.pick(ch, stories))                           # covered today: not repeated


class ScheduleTests(unittest.TestCase):
    def test_two_times_make_two_daily_slots_and_fewer_times_remove_one(self):
        ch = news_channel("Slots", schedule_enabled=1, schedule_time="18:00, 09:00")
        self.assertEqual(channels.get(ch["id"])["schedule_time"], "09:00, 18:00")
        scheduler.sync_channels()
        rows = db.q("SELECT time_of_day, payload FROM schedules WHERE channel_id=? ORDER BY time_of_day", (ch["id"],))
        self.assertEqual([r["time_of_day"] for r in rows], ["09:00", "18:00"])
        channels.update(ch["id"], {"schedule_time": "07:30"})
        scheduler.sync_channels()
        self.assertEqual([r["time_of_day"] for r in db.q("SELECT time_of_day FROM schedules WHERE channel_id=?",
                                                         (ch["id"],))], ["07:30"])

    def test_bad_times_are_refused(self):
        with self.assertRaises(ValueError):
            channels.clean({"schedule_time": "9am"})

    def test_news_slot_starts_todays_story_once_per_slot(self):
        ch = news_channel("Daily news", schedule_enabled=1, schedule_time="09:00, 18:00")
        made = []

        def fake_create(cid, request, idea_id=None, actor="", scheduled_for=None):
            made.append(scheduled_for)
            db.insert("productions", {"format": "long", "title": "x", "status": "queued", "stage": "research",
                                      "kind": "general", "workspace_id": 1, "channel_id": cid, "package": "{}",
                                      "request": db.dumps({"scheduled_for": scheduled_for}),
                                      "created_at": db.iso(), "updated_at": db.iso()})
            return {"production_ids": [1]}
        with mock.patch.object(news, "trending", lambda: news.parse_trends(FEED)), \
                mock.patch.object(news.sources, "page_text", side_effect=news.net.FetchError("offline")), \
                mock.patch("studio.jobs.create", fake_create), mock.patch("studio.plans.can", lambda *a: True):
            first = tasks._channel_daily({"channel_id": ch["id"]}, {})
            again = tasks._channel_daily({"channel_id": ch["id"]}, {})
            second = tasks._channel_daily({"channel_id": ch["id"], "slot": 1}, {})
        self.assertEqual(first["story"], "big match")
        self.assertIn("already exists", again["skipped"])
        self.assertIn("no new trending story", second["skipped"])     # the only good story is already covered
        self.assertEqual(len(made), 1)
        self.assertTrue(made[0].endswith(made[0][:10]))


class PresenterTests(unittest.TestCase):
    def test_presenter_is_made_once_and_opens_and_closes_news_videos(self):
        ch = news_channel("Presenter")
        calls = []

        def fake_gen(prompt, out, size=(1024, 576), refs=None):
            calls.append(prompt)
            Image.new("RGB", size, "navy").save(out)
            return out
        p1 = news.ensure_presenter(ch["id"], gen=fake_gen)
        news.ensure_presenter(ch["id"], gen=fake_gen)
        self.assertEqual(len(calls), 2)                                 # long + short once, never redrawn
        self.assertEqual(p1["role"], news.PRESENTER_ROLE)
        self.assertNotIn("cartoon", " ".join(calls).lower())
        work = Path(tempfile.mkdtemp())
        prod = {"channel_id": ch["id"], "format": "short", "package": {"scenes": [{}, {}, {}]}}
        first, middle, last = (news.presenter_asset(prod, i, work) for i in range(3))
        self.assertEqual((first["provider"], first["height"] > first["width"]), (news.PRESENTER_PROVIDER, True))
        self.assertIsNone(middle)
        self.assertEqual(last["scene_index"], 2)
        other = channels.create({"name": "Not news", "workflow": "animated_story"}, 1)
        self.assertIsNone(news.presenter_asset({**prod, "channel_id": other}, 0, work))

    def test_presenter_videos_are_disclosed_and_filed_under_news(self):
        from studio import pipeline
        from studio.youtube import upload
        ch = news_channel("Upload news")
        pid = db.insert("productions", {"format": "long", "title": "t", "status": "approved", "stage": "upload",
                                        "kind": "general", "workspace_id": 1, "channel_id": ch["id"], "package": "{}",
                                        "created_at": db.iso(), "updated_at": db.iso()})
        db.insert("assets", {"production_id": pid, "scene_index": 0, "kind": "image", "provider": "ai_presenter",
                             "provider_id": "presenter0", "local_path": "/x.png", "created_at": db.iso()})
        assets = pipeline.assets_for(pid)
        self.assertTrue(assets[0]["synthetic"])
        video = Path(tempfile.mkdtemp()) / "v.mp4"
        video.write_bytes(b"0" * 10)
        sent = {}

        def fake_request(method, url, **kw):
            sent.update(kw.get("json") or {})
            raise RuntimeError("stop here")
        prod = {**db.q1("SELECT * FROM productions WHERE id=?", (pid,)), "video_path": str(video)}
        with mock.patch.object(upload.oauth, "request", fake_request), self.assertRaises(RuntimeError):
            upload.upload(prod, {"title": "t", "description": "d"}, assets)
        self.assertEqual(sent["snippet"]["categoryId"], "25")
        self.assertTrue(sent["status"]["containsSyntheticMedia"])


if __name__ == "__main__":
    unittest.main()
