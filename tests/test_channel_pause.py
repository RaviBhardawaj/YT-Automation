"""Start / pause video making per channel: schedule, queued videos, a video being made, the NIFTY jobs, the buttons.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins
os.environ.setdefault("STUDIO_OFFLINE", "1")

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import auth, channels, jobs, pipeline, scheduler, selfreview, tasks, worker  # noqa: E402

auth.ensure_default_workspace()
channels.ensure_default()


def prod(cid: int, status: str = "queued", **kw) -> int:
    return db.insert("productions", {"format": "long", "title": "A video", "status": status, "stage": "script",
                                     "kind": "general", "workspace_id": 1, "channel_id": cid, "package": "{}",
                                     "created_at": db.iso(), "updated_at": db.iso(), **kw})


def task(pid: int, status: str = "pending", type_: str = "production") -> int:
    tid = worker.enqueue(type_, {"production_id": pid}, production_id=pid)
    db.ex("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    return tid


def status_of(table: str, rid: int) -> str:
    return db.q1(f"SELECT status FROM {table} WHERE id=?", (rid,))["status"]


class PauseResumeTests(unittest.TestCase):
    def test_pause_holds_queue_and_running_video_and_resume_carries_on(self):
        cid = channels.create({"name": f"Kids {os.urandom(2).hex()}"}, 1)
        other = channels.create({"name": f"News {os.urandom(2).hex()}"}, 1)
        waiting = prod(cid); t_wait = task(waiting)
        making = prod(cid, status="scripting"); task(making, "running")
        done = prod(cid, status="awaiting_approval")
        mine = prod(cid, status="paused"); t_mine = task(mine, "paused")
        db.ex("UPDATE tasks SET last_error='paused by user' WHERE id=?", (t_mine,))
        elsewhere = prod(other); t_else = task(elsewhere)

        self.assertEqual(channels.pause_generation(cid)["held"], 2)
        self.assertTrue(channels.paused(cid))
        self.assertEqual(status_of("tasks", t_wait), "paused")
        self.assertEqual(status_of("productions", waiting), "paused")
        self.assertEqual(status_of("productions", making), "paused")      # stops after its current step
        self.assertEqual(status_of("productions", done), "awaiting_approval")
        self.assertEqual(status_of("tasks", t_else), "pending")           # other channels keep going

        self.assertEqual(channels.resume_generation(cid)["resumed"], 1)
        self.assertFalse(channels.paused(cid))
        self.assertEqual(status_of("tasks", t_wait), "pending")
        self.assertEqual(status_of("productions", waiting), "queued")
        self.assertEqual(status_of("tasks", t_mine), "paused")            # paused one by one: stays paused

    def test_running_step_stops_at_boundary_and_resumes(self):
        cid = channels.create({"name": f"Run {os.urandom(2).hex()}"}, 1)
        pid = prod(cid, status="scripting")
        tid = task(pid, "running")
        channels.pause_generation(cid)
        with self.assertRaises(pipeline.PausedByOwner) as e:
            pipeline._run_stages(pid, pipeline.load(pid))
        self.assertTrue(str(e.exception).startswith(channels.CHANNEL_HOLD))
        db.ex("UPDATE tasks SET status='paused', last_error=? WHERE id=?", (str(e.exception), tid))
        channels.resume_generation(cid)
        self.assertEqual(status_of("tasks", tid), "pending")
        self.assertEqual(status_of("productions", pid), "queued")

    def test_worker_records_channel_hold(self):
        cid = channels.create({"name": f"Wk {os.urandom(2).hex()}"}, 1)
        pid = prod(cid, status="paused")
        db.ex("UPDATE channels SET status='paused' WHERE id=?", (cid,))

        @worker.handler("_pause_probe")
        def _probe(payload, t):
            pipeline._run_stages(pid, pipeline.load(pid))

        tid = worker.enqueue("_pause_probe", {}, production_id=pid)
        worker._run(worker.claim(worker.LANE_FOR.get("_pause_probe", "general")))
        row = db.q1("SELECT status, last_error FROM tasks WHERE id=?", (tid,))
        self.assertEqual(row["status"], "paused")
        self.assertTrue(row["last_error"].startswith(channels.CHANNEL_HOLD))

    def test_schedule_switches_off_and_on(self):
        cid = channels.create({"name": f"Sched {os.urandom(2).hex()}", "schedule_enabled": True,
                               "schedule_time": "09:00"}, 1)
        scheduler.sync_channels()
        q = "SELECT enabled FROM schedules WHERE task_type='channel_daily' AND channel_id=?"
        self.assertEqual(db.q1(q, (cid,))["enabled"], 1)
        channels.pause_generation(cid); scheduler.sync_channels()
        self.assertEqual(db.q1(q, (cid,))["enabled"], 0)
        channels.resume_generation(cid); scheduler.sync_channels()
        self.assertEqual(db.q1(q, (cid,))["enabled"], 1)


class AutomaticJobsTests(unittest.TestCase):
    def test_nifty_and_lesson_jobs_skip_a_paused_finance_channel(self):
        fin = channels.finance_channel()
        channels.pause_generation(fin["id"])
        try:
            self.assertIn("paused", worker._handlers["nifty_daily_episode"]({}, {})["skipped"])
            self.assertIn("paused", worker._handlers["fib_lesson_daily"]({}, {})["skipped"])
        finally:
            channels.resume_generation(fin["id"])

    def test_daily_plan_ignores_ideas_of_paused_channels(self):
        nid = db.insert("niches", {"name": "n", "status": "approved", "approved_at": db.iso(), "created_at": db.iso()}) \
            if not db.q1("SELECT id FROM niches WHERE status='approved'") else \
            db.q1("SELECT id FROM niches WHERE status='approved' ORDER BY approved_at DESC LIMIT 1")["id"]
        cid = channels.create({"name": f"Ideas {os.urandom(2).hex()}"}, 1)
        tid = db.insert("topics", {"niche_id": nid, "channel_id": cid, "title": "t", "format": "long",
                                   "status": "approved", "score": 90, "created_at": db.iso(), "updated_at": db.iso()})
        self.assertIn(tid, [t["id"] for t in tasks._available_ideas("long")])
        channels.pause_generation(cid)
        self.assertNotIn(tid, [t["id"] for t in tasks._available_ideas("long")])

    def test_self_review_leaves_paused_channel_alone(self):
        cid = channels.create({"name": f"SR {os.urandom(2).hex()}"}, 1)
        pid = prod(cid, status="needs_review", error="QA: too quiet")
        channels.pause_generation(cid)
        self.assertEqual(selfreview.review(pid)["why"], "its channel is paused")


class ButtonTests(unittest.TestCase):
    def test_buttons_on_channels_page(self):
        from tests.test_web_actions import client
        c, ws = client()
        cid = channels.create({"name": f"Btn {os.urandom(2).hex()}"}, ws)
        self.assertIn("Pause videos", c.get("/channels").text)
        r = c.post(f"/api/channels/{cid}", json={"status": "paused"}).json()
        self.assertTrue(r["ok"]); self.assertIn("paused", r["detail"])
        self.assertTrue(channels.paused(cid))
        self.assertIn("Start video making", c.get(f"/channels/{cid}").text)
        r = c.post(f"/api/channels/{cid}", json={"status": "active"}).json()
        self.assertTrue(r["ok"]); self.assertFalse(channels.paused(cid))
        self.assertFalse(c.post(f"/api/channels/{cid}", json={"status": "sleeping"}).json()["ok"])


if __name__ == "__main__":
    unittest.main()
