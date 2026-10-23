"""Multi-channel studio tests. Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))

from studio import config, db, settings  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import channels, jobs, scheduler, tasks, worker  # noqa: E402,F401
from studio.creative import request as reqmod  # noqa: E402

channels.ensure_default()


def make_channel(**kw) -> int:
    base = {"name": "Test Toons", "niche": "animated comedy", "language": "Hindi", "workflow": "animated_story",
            "formats": "both", "styles": ["comedy"]}
    return channels.create({**base, **kw})


class RequestFirstTests(unittest.TestCase):
    def test_request_beats_channel(self):
        comedy = channels.get(make_channel(name="Comedy A"))
        p = reqmod.plan("Create a scary animated horror story", comedy)
        self.assertEqual((p["content_type"], p["workflow"], p["decided_by"]), ("horror", "animated_story", "request"))
        p = reqmod.plan("Create a Hindi finance video explaining a stock-market concept", comedy)
        self.assertEqual((p["content_type"], p["workflow"], p["language"]), ("finance", "factual", "Hindi"))

    def test_channel_decides_when_request_is_silent(self):
        cid = make_channel(name="Comedy B")
        self.assertEqual(jobs.plan_for(cid, "")["workflow"], "animated_story")
        fid = make_channel(name="Facts", workflow="factual", styles=["tech"])
        self.assertEqual(jobs.plan_for(fid, "a new video")["workflow"], "factual")

    def test_language_and_length_from_request(self):
        ch = channels.get(make_channel(name="Comedy C"))
        p = reqmod.plan("make a 3 minute comedy short in English", ch)
        self.assertEqual((p["language"], p["format"]), ("English", "short"))


class RoutingTests(unittest.TestCase):
    def test_animated_request_goes_to_story_workflow(self):
        cid = make_channel(name="Route Toons")
        with mock.patch.object(worker, "enqueue", return_value=1):
            res = jobs.create(cid, "Create a funny animated cartoon about a lazy cat", fmt="long")
        p = db.q1("SELECT kind, channel_id, format FROM productions WHERE id=?", (res["production_ids"][0],))
        self.assertEqual((p["kind"], p["channel_id"], p["format"]), ("story", cid, "long"))

    def test_factual_request_creates_brief_topic_on_channel(self):
        cid = make_channel(name="Route Facts", workflow="factual", styles=["tech"])
        with mock.patch.object(worker, "enqueue", return_value=1):
            res = jobs.create(cid, "Explain how UPI payments work", fmt="both")
        self.assertEqual(len(res["production_ids"]), 2)
        rows = db.q(f"SELECT channel_id, format, kind, topic_id FROM productions WHERE id IN "
                    f"({','.join(str(x) for x in res['production_ids'])})")
        self.assertEqual({r["format"] for r in rows}, {"long", "short"})
        self.assertTrue(all(r["channel_id"] == cid for r in rows))
        topic = db.q1("SELECT * FROM topics WHERE id=?", (rows[0]["topic_id"],))
        self.assertEqual((topic["channel_id"], topic["idea_kind"]), (cid, "request"))
        self.assertIn("UPI", topic["angle"])

    def test_idea_from_other_channel_refused(self):
        a, b = make_channel(name="Own A"), make_channel(name="Own B")
        tid = db.insert("topics", {"channel_id": a, "title": "x", "format": "long", "created_at": db.iso(),
                                   "updated_at": db.iso()})
        with self.assertRaises(ValueError):
            jobs.create(b, "", idea_id=tid)

    def test_nifty_channel_without_request_queues_daily_episode(self):
        with mock.patch.object(worker, "enqueue", return_value=77) as enq:
            res = jobs.create(1, "")
        self.assertEqual(res["task_id"], 77)
        self.assertEqual(enq.call_args[0][0], "nifty_daily_episode")


class IsolationTests(unittest.TestCase):
    def test_each_channel_has_its_own_youtube_secret(self):
        self.assertEqual(channels.refresh_token_secret(1), "google_refresh_token")
        self.assertEqual(channels.refresh_token_secret(5), "google_refresh_token__ch5")
        self.assertTrue(settings.known_secret("google_refresh_token__ch5"))
        self.assertFalse(settings.known_secret("google_refresh_token__chX"))

    def test_verify_destination_refuses_wrong_channel(self):
        from studio.youtube import oauth
        cid = make_channel(name="Dest")
        db.ex("UPDATE channels SET youtube_channel_id='UC_expected' WHERE id=?", (cid,))
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"items": [{"id": "UC_other", "snippet": {"title": "Other"}}]}
        with mock.patch.object(oauth, "connected", return_value=True), mock.patch.object(oauth, "request", return_value=resp):
            with self.assertRaises(oauth.AuthError):
                oauth.verify_destination(cid)
        resp.json.return_value = {"items": [{"id": "UC_expected", "snippet": {"title": "Mine"}}]}
        with mock.patch.object(oauth, "connected", return_value=True), mock.patch.object(oauth, "request", return_value=resp):
            self.assertEqual(oauth.verify_destination(cid)["id"], "UC_expected")

    def test_context_switches_profile(self):
        from studio import profile
        cid = make_channel(name="Lang", language="English")
        with channels.using(cid):
            self.assertEqual(channels.current()["language"], "English")
        self.assertIsNone(channels.current_id())
        self.assertTrue(callable(profile.summary_text))


class ScheduleTests(unittest.TestCase):
    def test_sync_creates_one_slot_per_channel_and_follows_settings(self):
        cid = make_channel(name="Sched", schedule_enabled=True, schedule_time="09:15", schedule_days=[0, 2, 4])
        scheduler.sync_channels()
        scheduler.sync_channels()
        rows = db.q("SELECT * FROM schedules WHERE task_type='channel_daily' AND channel_id=?", (cid,))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["time_of_day"], rows[0]["enabled"]), ("09:15", 1))
        channels.update(cid, {"status": "paused"})
        scheduler.sync_channels()
        self.assertEqual(db.q1("SELECT enabled FROM schedules WHERE id=?", (rows[0]["id"],))["enabled"], 0)
        self.assertIsNone(db.q1("SELECT id FROM schedules WHERE task_type='channel_daily' AND channel_id=1"))

    def test_channel_daily_makes_one_video_per_day(self):
        cid = make_channel(name="Daily")
        tid = db.insert("topics", {"channel_id": cid, "title": "Best idea", "format": "long", "status": "approved",
                                   "score": 80, "created_at": db.iso(), "updated_at": db.iso()})
        with mock.patch.object(worker, "enqueue", return_value=1):
            first = tasks._channel_daily({"channel_id": cid}, {})
            second = tasks._channel_daily({"channel_id": cid}, {})
        self.assertEqual(len(first["production_ids"]), 1)
        self.assertIn("already exists", second["skipped"])
        self.assertEqual(db.q1("SELECT topic_id FROM productions WHERE id=?", (first["production_ids"][0],))["topic_id"], tid)

    def test_paused_channel_skips(self):
        cid = make_channel(name="Paused", status="paused")
        self.assertIn("skipped", tasks._channel_daily({"channel_id": cid}, {}))


class JobControlTests(unittest.TestCase):
    def _prod(self) -> int:
        cid = make_channel(name="Jobs")
        pid = db.insert("productions", {"format": "long", "title": "t", "status": "queued", "stage": "script",
                                        "kind": "story", "channel_id": cid, "created_at": db.iso(), "updated_at": db.iso()})
        worker.enqueue("production", {"production_id": pid}, production_id=pid)
        return pid

    def test_pause_resume_cancel(self):
        pid = self._prod()
        self.assertEqual(jobs.pause(pid), 1)
        self.assertEqual(db.q1("SELECT status FROM productions WHERE id=?", (pid,))["status"], "paused")
        self.assertEqual(jobs.resume(pid), 1)
        self.assertEqual(db.q1("SELECT status FROM tasks WHERE production_id=?", (pid,))["status"], "pending")
        jobs.cancel(pid)
        self.assertEqual(db.q1("SELECT status FROM productions WHERE id=?", (pid,))["status"], "rejected")
        self.assertEqual(db.q1("SELECT status FROM tasks WHERE production_id=?", (pid,))["status"], "cancelled")

    def test_retry_requeues_last_step(self):
        pid = self._prod()
        db.ex("UPDATE tasks SET status='failed' WHERE production_id=?", (pid,))
        db.ex("UPDATE productions SET status='failed', error='boom' WHERE id=?", (pid,))
        jobs.retry(pid)
        self.assertEqual(db.q1("SELECT status, error FROM productions WHERE id=?", (pid,))["status"], "queued")
        self.assertEqual(db.q1("SELECT COUNT(*) AS n FROM tasks WHERE production_id=? AND status='pending'", (pid,))["n"], 1)
        with self.assertRaises(ValueError):
            jobs.retry(pid)                         # queued: nothing to retry


class IdeasPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from studio.web import app as webapp
        from studio import auth
        cls.client = TestClient(webapp.app, base_url=f"http://127.0.0.1:{config.PORT}", follow_redirects=False)
        auth.ensure_default_workspace()
        if not db.q1("SELECT id FROM users WHERE email='owner@test.local'"):
            auth.register("owner@test.local", "correct horse battery")
        token, _ = auth.login("owner@test.local", "correct horse battery")
        cls.client.cookies.set("sid", token)
        cls.cid = make_channel(name="Filter Toons")
        for i, (fmt, style, kind) in enumerate([("long", "comedy", "ai_original"), ("short", "horror", "evergreen"),
                                                ("short", "comedy", "ai_original")]):
            db.insert("topics", {"channel_id": cls.cid, "workspace_id": 1, "title": f"Idea number {i} {style}", "format": fmt, "style": style,
                                 "idea_kind": kind, "language": "Hindi", "score": 50 + i, "status": "idea",
                                 "created_at": db.iso(), "updated_at": db.iso()})

    def _titles(self, **params) -> str:
        r = self.client.get("/ideas", params={"channel": self.cid, **params})
        self.assertEqual(r.status_code, 200, r.text[:300])
        return r.text

    def test_filters(self):
        html = self._titles(format="short")
        self.assertIn("Idea number 1 horror", html)
        self.assertNotIn("Idea number 0 comedy", html)
        html = self._titles(style="comedy", format="short")
        self.assertIn("Idea number 2 comedy", html)
        self.assertNotIn("Idea number 1 horror", html)
        self.assertIn("Idea number 1", self._titles(q="horror"))
        self.assertNotIn("Idea number 0", self._titles(kind="evergreen"))
        self.assertIn("3 idea(s)", self._titles())

    def test_pages_render(self):
        for path in ("/channels", f"/channels/{self.cid}", f"/channels/{self.cid}?tab=settings",
                     f"/channels/{self.cid}?tab=characters", f"/channels/{self.cid}?tab=youtube",
                     f"/productions?channel={self.cid}", "/productions?status=failed"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, f"{path}: {r.text[:300]}")


if __name__ == "__main__":
    unittest.main()
