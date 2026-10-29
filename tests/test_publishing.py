"""Scheduled publishing survives pauses, YouTube's upload limit and outages: nothing is lost, nothing stacks up.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import auth, channels, jobs, settings, worker  # noqa: E402
from studio.youtube.upload import UploadError  # noqa: E402

auth.ensure_default_workspace()
channels.ensure_default()


def prod(cid: int, **kw) -> int:
    row = {"format": "long", "title": "Episode", "status": "approved", "stage": "review", "kind": "story",
           "workspace_id": 1, "channel_id": cid, "qa": json.dumps({"passed": True}), "package": "{}",
           "privacy": "scheduled", "created_at": db.iso(), "updated_at": db.iso(), **kw}
    return db.insert("productions", row)


class PublishingTests(unittest.TestCase):
    def test_backlog_takes_one_slot_per_day_and_shorts_share_their_day(self):
        cid = channels.create({"name": "Slots", "publish_time": "18:00"}, 1)
        first = channels.next_publish_slot(channels.get(cid))
        prod(cid, publish_at=first)
        second = channels.next_publish_slot(channels.get(cid))
        self.assertGreater(second, first)
        self.assertEqual(channels.next_publish_slot(channels.get(cid), main=False), first)

    def test_short_never_goes_public_before_its_episode(self):
        from studio import pipeline
        cid = channels.create({"name": "Order", "publish_time": "18:00"}, 1)
        first = channels.next_publish_slot(channels.get(cid))
        prod(cid, publish_at=first)                                   # the 1st slot is already taken
        ep = prod(cid, publish_at=channels.next_publish_slot(channels.get(cid)))
        short = {"kind": "story_short", "parent_id": ep}
        self.assertEqual(pipeline.slot_for(short, channels.get(cid)),
                         db.q1("SELECT publish_at FROM productions WHERE id=?", (ep,))["publish_at"])

    def test_resuming_a_paused_upload_uploads_it(self):
        cid = channels.create({"name": "Resume"}, 1)
        pid = prod(cid, status="paused")
        worker.enqueue("upload", {"production_id": pid}, production_id=pid)
        db.ex("UPDATE tasks SET status='paused' WHERE production_id=?", (pid,))
        self.assertEqual(jobs.resume(pid), 1)
        self.assertEqual(db.q1("SELECT status FROM productions WHERE id=?", (pid,))["status"], "approved")

    def test_upload_limit_retries_without_using_up_attempts(self):
        cid = channels.create({"name": "Limits"}, 1)
        pid = prod(cid)
        tid = worker.enqueue("upload", {"production_id": pid}, production_id=pid, max_attempts=2)
        db.ex("UPDATE tasks SET attempts=2 WHERE id=?", (tid,))       # already at its last attempt
        task = db.q1("SELECT * FROM tasks WHERE id=?", (tid,))
        task["payload"] = {"production_id": pid}

        def boom(*_):
            raise UploadError("YouTube daily upload limit reached for this channel", retry_after=12 * 3600)
        with mock.patch.dict(worker._handlers, {"upload": boom}):
            worker._run(task)
        t = db.q1("SELECT status, attempts FROM tasks WHERE id=?", (tid,))
        self.assertEqual(t["status"], "pending")
        self.assertEqual(t["attempts"], 1)
        self.assertNotEqual(db.q1("SELECT status FROM productions WHERE id=?", (pid,))["status"], "failed")

    def test_dashboard_lists_paused_and_deleted_daily_video(self):
        from studio.web import app as webapp
        cid = channels.create({"name": "Dash"}, 1)
        prod(cid, status="paused", title="Paused one")
        sid = db.insert("schedules", {"name": "daily", "task_type": "channel_daily", "kind": "daily",
                                      "time_of_day": "18:00", "enabled": 1, "channel_id": cid,
                                      "payload": json.dumps({"channel_id": cid}), "created_at": db.iso()})
        db.insert("tasks", {"type": "channel_daily", "lane": "general", "payload": json.dumps({"channel_id": cid}),
                            "status": "done", "priority": 0, "attempts": 1, "max_attempts": 2,
                            "result": json.dumps({"production_ids": [999999]}), "run_after": db.iso(),
                            "created_at": db.iso(), "updated_at": db.iso()})
        texts = [x["text"] for x in webapp._publishing_problems(1)]
        self.assertTrue(any("Paused one" in t for t in texts))
        self.assertTrue(any("scheduled video for “Dash”" in t for t in texts))
        self.assertTrue(any(x.get("post") == f"/api/schedules/{sid}" for x in webapp._publishing_problems(1)))


    def test_upload_limit_notice_can_be_closed_and_comes_back_for_a_new_limit(self):
        from studio.web import app as webapp
        cid = channels.create({"name": "Closable"}, 1)
        pid = prod(cid)
        tid = worker.enqueue("upload", {"production_id": pid}, production_id=pid, max_attempts=2)
        db.ex("UPDATE tasks SET last_error=?, run_after=? WHERE id=?",
              ("UploadError: YouTube daily upload limit reached for this channel", "2026-10-29T17:41:27Z", tid))
        mine = lambda: [x for x in webapp._publishing_problems(1) if "Closable" in x["text"]]
        self.assertEqual(len(mine()), 1)
        self.assertNotIn("verification", mine()[0]["text"])            # the channel is verified: no nagging
        key = mine()[0]["key"]
        allk = settings.get("ui.dismissed_notices") or {}
        allk["1"] = allk.get("1", []) + [key]
        settings.set("ui.dismissed_notices", allk)                      # what the ✕ button saves
        self.assertEqual(mine(), [])
        db.ex("UPDATE tasks SET run_after=? WHERE id=?", ("2026-10-30T17:41:27Z", tid))
        self.assertEqual(len(mine()), 1)                                # a new limit event shows again


    def test_pausing_a_running_video_stops_it_before_the_next_step_and_resume_continues(self):
        from studio import jobs, pipeline, tasks  # noqa: F401 - tasks registers the worker handlers
        cid = channels.create({"name": "Pausable"}, 1)
        pid = prod(cid, status="paused", stage="script", kind="general")       # paused while a step was running
        tid = worker.enqueue("production", {"production_id": pid}, production_id=pid, max_attempts=3)
        task = db.q1("SELECT * FROM tasks WHERE id=?", (tid,))
        task["payload"] = {"production_id": pid}
        with mock.patch.dict(pipeline.STAGE_FN, {"script": lambda p: self.fail("a paused video must not continue")}):
            worker._run(task)
        self.assertEqual(db.q1("SELECT status FROM tasks WHERE id=?", (tid,))["status"], "paused")
        self.assertEqual(db.q1("SELECT status FROM productions WHERE id=?", (pid,))["status"], "paused")
        self.assertEqual(jobs.resume(pid), 1)                                 # Resume picks it up again
        self.assertEqual(db.q1("SELECT status FROM tasks WHERE id=?", (tid,))["status"], "pending")


class FinanceRuleScopeTests(unittest.TestCase):
    def test_finance_wording_rule_only_for_finance_videos(self):
        from studio import pipeline
        kids = channels.create({"name": "Kids scope", "workflow": "animated_story", "niche": "kids comedy"}, 1)
        health = {"channel_id": kids, "request": json.dumps({"content_type": "education"})}
        silver = {"channel_id": kids, "request": json.dumps({"content_type": "finance"})}
        self.assertFalse(pipeline._is_finance_video(health))
        self.assertTrue(pipeline._is_finance_video(silver))


class FreeFirstAndSetupTests(unittest.TestCase):
    def test_free_providers_come_before_paid(self):
        from studio import settings
        from studio.ai import router
        settings.set("ai.default_provider", "anthropic")
        try:
            chain = router.provider_chain("script")
            self.assertLess(chain.index("ollama_cloud"), chain.index("anthropic"))
        finally:
            settings.set("ai.default_provider", "ollama_local")

    def test_setup_lists_machine_and_components(self):
        from studio import system_setup
        s = system_setup.summary()
        self.assertIn(s["machine"]["cpu_kind"], ("apple", "intel", "amd", "other"))
        self.assertTrue(any(c["id"] == "ffmpeg" for c in s["components"]))


if __name__ == "__main__":
    unittest.main()
