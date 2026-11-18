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
        db.update("channels", self.ch, {"meta": db.dumps({"fresh_cast": False})})   # recurring-cast mode by default here

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

    def test_one_click_makes_both_videos_now_and_slots_dont_repeat_them(self):
        made = []

        def fake_create(cid, request="", **kw):
            pid = db.insert("productions", {"topic_id": kw["idea_id"], "format": kw["fmt"], "title": "t", "status": "queued",
                                            "stage": "script", "kind": "story", "channel_id": cid, "workspace_id": 1,
                                            "language": "Hindi", "request": db.dumps({"scheduled_for": kw["scheduled_for"]}),
                                            "package": "{}", "timeline": "{}", "episode": "{}", "test_mode": 0,
                                            "created_at": db.iso(), "updated_at": db.iso()})
            db.update("topics", kw["idea_id"], {"status": "producing"})
            made.append(kw)
            return {"production_ids": [pid]}

        def fake_ai(task, system, prompt, schema, **kw):
            out = good_plan()
            kw["validate"](out)
            return out, {}
        with mock.patch.object(settings, "has_secret", lambda n: True), \
                mock.patch.object(remake.sources, "youtube_most_popular", chart), \
                mock.patch.object(remake.ai, "generate", fake_ai), \
                mock.patch.object(jobs, "create", fake_create):
            res = remake.make_videos_now(self.ch)
            again = remake.make_videos_now(self.ch)                         # a second click starts nothing new
        self.assertEqual([m["fmt"] for m in made], ["long", "short"])
        self.assertEqual(len(res["started"]), 2)
        self.assertEqual(again["started"], [])
        self.assertTrue(remake.enabled(channels.get(self.ch)))             # one click also switches daily on
        st = remake.status(self.ch)
        self.assertIn("chore", st["story"]["premise"])
        self.assertEqual([v["production"]["format"] for v in st["videos"]], ["long", "short"])
        with mock.patch.object(channels, "long_made_today", lambda cid: True), \
                mock.patch("studio.plans.can", lambda *a: True):
            r = tasks._channel_daily({"channel_id": self.ch, "slot": 1}, {})
        self.assertIn("already exists", r["skipped"])                      # the 6 PM slot sees today's Short

    def test_channel_page_shows_the_one_click_card(self):
        from fastapi.testclient import TestClient
        from studio import config
        from studio.web import app as webapp
        if not db.q1("SELECT id FROM users WHERE email='remake@test.local'"):
            auth.approve(auth.register("remake@test.local", "a long enough password")["user_id"], 1)
        tok, _ = auth.login("remake@test.local", "a long enough password")
        ws = auth.session(tok)["workspace"]["id"]
        cid = channels.create({"name": "Card kids", "workflow": "animated_story", "made_for_kids": True,
                               "niche": "kids"}, ws)
        c = TestClient(webapp.app, base_url=f"http://127.0.0.1:{config.PORT}", follow_redirects=False)
        c.cookies.set("sid", tok)
        html = c.get(f"/channels/{cid}").text
        self.assertIn("Make today&#39;s videos now", html.replace("'", "&#39;"))
        self.assertIn("Settings → Credentials", html)                      # no key yet: step 1 is shown

    def test_kids_and_comedy_get_a_brand_new_cast_every_story(self):
        cid = channels.create({"name": "Fresh", "workflow": "animated_story", "made_for_kids": True,
                               "niche": "kids", "styles": ["kids"]}, 1)
        self.assertTrue(remake.fresh_cast(channels.get(cid)))
        self.assertFalse(remake.fresh_cast(channels.get(
            channels.create({"name": "Drama", "workflow": "animated_story", "styles": ["drama"], "niche": "d"}, 1))))
        cands = [{"title": "Billi ne kiya homework", "views_per_day": 10, "seconds": 60, "description": ""}] * 2
        seen = {}

        def fake(task, system, prompt, schema, **kw):
            seen["prompt"] = prompt
            with self.assertRaises(ValueError):                              # the old cast is refused
                kw["validate"](good_plan())
            out = good_plan(use_cast=False, new_characters=["Gauri, a brave goat", "Tinni, a robot girl"])
            kw["validate"](out)
            return out, {}
        with mock.patch.object(remake.ai, "generate", fake):
            plan = remake.adapt(channels.get(cid), cands)
        self.assertFalse(plan.use_cast)
        self.assertIn("BRAND-NEW CAST", seen["prompt"])
        self.assertNotIn("RECURRING CHARACTERS", seen["prompt"])
        tid = db.insert("topics", {"channel_id": cid, "title": "x", "status": "approved", "format": "long",
                                   "workspace_id": 1, "created_at": db.iso(), "updated_at": db.iso()})
        with mock.patch.object(remake, "make_today", lambda c: {"long": tid, "use_cast": True}):
            self.assertEqual(remake.topic_for(cid, "long"), (tid, False))    # never the old cast on this channel

    def test_clear_queue_removes_only_waiting_uploads(self):
        from fastapi.testclient import TestClient
        from studio import config
        from studio.web import app as webapp
        if not db.q1("SELECT id FROM users WHERE email='queue@test.local'"):
            auth.approve(auth.register("queue@test.local", "a long enough password")["user_id"], 1)
        tok, _ = auth.login("queue@test.local", "a long enough password")
        ws = auth.session(tok)["workspace"]["id"]
        cid = channels.create({"name": "Queue", "workflow": "animated_story", "niche": "k"}, ws)

        def prod(status, yt=None):
            return db.insert("productions", {"format": "short", "title": "t", "status": status, "stage": "review",
                                             "kind": "story", "channel_id": cid, "workspace_id": ws, "language": "Hindi",
                                             "request": "{}", "package": "{}", "timeline": "{}", "episode": "{}",
                                             "test_mode": 0, "youtube_video_id": yt, "created_at": db.iso(),
                                             "updated_at": db.iso()})
        waiting, live, review = prod("approved"), prod("uploaded", "abc"), prod("awaiting_approval")
        up = db.insert("tasks", {"type": "upload", "lane": "general", "payload": "{}", "status": "pending",
                                 "priority": 0, "attempts": 0, "max_attempts": 3, "run_after": db.iso(),
                                 "production_id": waiting, "created_at": db.iso(), "updated_at": db.iso()})
        c = TestClient(webapp.app, base_url=f"http://127.0.0.1:{config.PORT}", follow_redirects=False)
        c.cookies.set("sid", tok)
        c.headers["X-CSRF-Token"] = webapp.csrf_for_seed(tok)
        self.assertIn("Clear 1 waiting upload", c.get(f"/channels/{cid}").text)
        self.assertTrue(c.post(f"/api/channels/{cid}/clear-queue").json()["ok"])
        st = {r["id"]: r["status"] for r in db.q("SELECT id, status FROM productions WHERE channel_id=?", (cid,))}
        self.assertEqual((st[waiting], st[live], st[review]), ("rejected", "uploaded", "awaiting_approval"))
        self.assertEqual(db.q1("SELECT status FROM tasks WHERE id=?", (up,))["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
