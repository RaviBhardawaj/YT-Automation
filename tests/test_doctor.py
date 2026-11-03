"""Growth doctor: findings come only from a channel's real numbers; safe automatic fixes (hook lesson, Shorts per
long video, daily ideas close to winners); analytics refreshes retry when the internet is down. YouTube is mocked.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
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
from studio import channels, learning, tasks  # noqa: E402
from studio.youtube import analytics, doctor, growth  # noqa: E402


def channel(**kw) -> int:
    return channels.create({"name": f"Doc {os.urandom(2).hex()}", "formats": "long", "shorts_per_episode": 0, **kw}, 1)


def video(cid, title, fmt, views, pct=None, secs=None, sources=None, days=10):
    pid = db.insert("productions", {"format": fmt, "title": title, "status": "uploaded", "stage": "qa",
                                    "kind": "general", "workspace_id": 1, "channel_id": cid, "package": "{}",
                                    "youtube_video_id": f"v{os.urandom(4).hex()}", "uploaded_at": db.iso_in(-days * 86400),
                                    "created_at": db.iso(), "updated_at": db.iso()})
    db.insert("analytics", {"production_id": pid, "video_id": f"v{pid}", "fetched_at": db.iso(), "metrics": json.dumps(
        {"views": views, "averageViewPercentage": pct, "averageViewDuration": secs, "subscribersGained": 0,
         "traffic_sources": sources or {}})})
    return pid


class DoctorTests(unittest.TestCase):
    def test_findings_follow_the_numbers(self):
        cid = channel()
        video(cid, "Gold rate falls below 50,000", "short", 600, 60, 20, {"SHORTS": {"views": 500}})
        video(cid, "Long market explainer", "long", 40, 12, 30, {"YT_SEARCH": {"views": 40}})
        for i in range(6):
            video(cid, f"Quiet video {i}", "short", 0)
        keys = {f["key"] for f in doctor.diagnose(cid)["findings"]}
        self.assertTrue({"hook", "shorts", "winners", "volume", "subs"} <= keys, keys)

    def test_no_data_is_said_not_guessed(self):
        self.assertEqual([f["key"] for f in doctor.diagnose(channel())["findings"]], ["no_data"])

    def test_apply_adds_a_hook_lesson_turns_on_shorts_and_daily_learning_keeps_it(self):
        cid = channel()
        video(cid, "Gold rate falls", "short", 600, 60, 20)
        video(cid, "Long explainer", "long", 40, 10, 25)
        res = doctor.apply(cid)
        self.assertIn("hook lesson", res["applied"])
        self.assertEqual(channels.get(cid)["shorts_per_episode"], 3)
        self.assertIn("Open with the payoff", learning.prompt_block(cid))
        db.ex("UPDATE lessons SET status='old' WHERE channel_id=? AND status='active' AND kind IN ('audience','quality')",
              (cid,))                                             # what the daily AI review does
        self.assertIn("Open with the payoff", learning.prompt_block(cid))
        doctor.apply(cid)                                         # running again does not pile up copies
        self.assertEqual(db.q1("SELECT COUNT(*) n FROM lessons WHERE channel_id=? AND status='active' AND kind='doctor'",
                               (cid,))["n"], 1)

    def test_daily_video_prefers_an_idea_close_to_a_winner(self):
        cid = channel()
        video(cid, "Gold rate today falls below 50000", "short", 900, 60, 20)
        base = {"channel_id": cid, "workspace_id": 1, "status": "idea", "format": "short", "created_at": db.iso(),
                "updated_at": db.iso()}
        cols = {r["name"] for r in db.q("PRAGMA table_info(topics)")}
        row = lambda **kw: {k: v for k, v in {**base, **kw}.items() if k in cols}  # noqa: E731
        db.insert("topics", row(title="Ten facts about volcanoes", score=95))
        gold = db.insert("topics", row(title="Gold rate today: why it falls again", score=50))
        self.assertEqual(tasks._pick_idea(cid)["id"], gold)


class OfflineRetryTests(unittest.TestCase):
    def test_offline_refreshes_wait_15_minutes_and_try_again(self):
        self.assertTrue(analytics.offline_error(OSError("[Errno 8] nodename nor servname provided, or not known")))
        self.assertFalse(analytics.offline_error(RuntimeError("HTTP 403: quotaExceeded")))
        cid = channel()
        with mock.patch.object(channels, "all_channels", lambda *a: [channels.get(cid)]), \
                mock.patch.object(analytics.oauth, "connected", lambda *a: True), \
                mock.patch.object(analytics, "channel_report", side_effect=OSError("[Errno 8] nodename nor servname")):
            with self.assertRaises(analytics.NoInternet) as cm:
                analytics.sync_all()
        self.assertTrue(cm.exception.temporary)
        self.assertEqual(cm.exception.retry_after, 900)
        with mock.patch.object(channels, "all_channels", lambda *a: [channels.get(cid)]), \
                mock.patch.object(growth.oauth, "connected", lambda *a: True), \
                mock.patch.object(growth, "refresh", side_effect=OSError("[Errno 8] nodename nor servname")), \
                self.assertRaises(analytics.NoInternet):
            growth.refresh_all()


if __name__ == "__main__":
    unittest.main()
