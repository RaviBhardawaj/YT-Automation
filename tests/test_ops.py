"""Operations control: emergency stop, per-channel pause, the auto-publish gate (channel opt-in, first N uploads
approved by the owner, clean QA), the audit trail and repeat detection across channels. Nothing external is called.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins
os.environ.setdefault("STUDIO_OFFLINE", "1")              # never call the free image services

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import channels, ops, pipeline, worker  # noqa: E402


def prod(**kw) -> int:
    row = {"format": "long", "title": "A video", "status": "awaiting_approval", "stage": "qa", "kind": "general",
           "workspace_id": 1, "qa": json.dumps({"passed": True}), "package": "{}", "created_at": db.iso(),
           "updated_at": db.iso(), **kw}
    return db.insert("productions", row)


def channel(name: str, **kw) -> dict:
    return channels.get(channels.create({"name": f"{name} {os.urandom(2).hex()}", "auto_publish": True, **kw}, 1))


class StopTests(unittest.TestCase):
    def tearDown(self):
        ops.set_stop(False, "test")
        db.ex("DELETE FROM tasks WHERE type IN ('production','analytics_sync') AND payload LIKE '%stoptest%'")

    def test_only_bookkeeping_starts_while_stopped(self):
        worker.enqueue("production", {"stoptest": 1})
        worker.enqueue("analytics_sync", {"stoptest": 2})
        ops.set_stop(True, "owner@test", "checking something")
        got = [worker.claim(lane) for lane in worker.LANES]
        types = {t["type"] for t in got if t}
        self.assertNotIn("production", types)
        ops.set_stop(False, "owner@test")
        self.assertIsNotNone(db.q1("SELECT 1 FROM tasks WHERE type='production' AND status='pending' "
                                   "AND payload LIKE '%stoptest%'"))

    def test_nothing_is_approved_or_uploaded_automatically_while_stopped(self):
        ch = channel("Stopped")
        pid = prod(channel_id=ch["id"])
        ops.set_stop(True, "owner@test")
        pipeline.approve(pid, privacy="public", publish_at=None, by="auto (channel auto-publish)")
        self.assertEqual(db.q1("SELECT status FROM productions WHERE id=?", (pid,))["status"], "awaiting_approval")
        up = prod(channel_id=ch["id"], status="approved")
        self.assertIn("held", pipeline._run_upload({"production_id": up}, {}))
        self.assertEqual(db.q1("SELECT status FROM productions WHERE id=?", (up,))["status"], "approved")

    def test_stop_and_its_reason_are_on_the_audit_trail(self):
        ops.set_stop(True, "owner@test", "bad thumbnail batch")
        last = ops.recent(5)
        self.assertTrue(any(a["event"] == "emergency_stop_on" and a["details"].get("reason") == "bad thumbnail batch"
                            for a in last))


class GateTests(unittest.TestCase):
    def test_every_gate_must_hold(self):
        ch = channel("Gate", approve_first_n=2)
        self.assertIn("first 2", ops.auto_publish_block(ch, True))          # no uploads yet
        for i in range(2):
            prod(channel_id=ch["id"], youtube_video_id=f"v{os.urandom(3).hex()}", status="uploaded")
        self.assertEqual(ops.auto_publish_block(ch, True), "")
        self.assertIn("human look", ops.auto_publish_block(ch, False))
        self.assertIn("auto-publish is off", ops.auto_publish_block({**ch, "auto_publish": 0}, True))
        self.assertIn("paused", ops.auto_publish_block({**ch, "status": "paused"}, True))

    def test_paused_channel_holds_an_approved_upload(self):
        ch = channel("Paused")
        channels.update(ch["id"], {"status": "paused"}, actor="owner@test")
        up = prod(channel_id=ch["id"], status="approved")
        self.assertIn("paused", pipeline._run_upload({"production_id": up}, {})["held"])
        self.assertTrue(any(a["event"] == "channel_settings" and a["details"].get("channel_id") == ch["id"]
                            for a in ops.recent(10)))

    def test_studio_wide_auto_mode_never_overrides_a_channel_that_did_not_opt_in(self):
        from unittest import mock

        from studio import settings
        ch = channel("No opt-in", auto_publish=False, approve_first_n=0)
        pid = prod(channel_id=ch["id"], status="quality check", video_path="/nope.mp4",
                   package=json.dumps({"title": "x", "scenes": []}), timeline="{}")
        p = pipeline.load(pid)
        clean = {"passed": True, "auto_publish_ok": True, "checks": []}
        with mock.patch.object(pipeline.qa, "run", lambda *a, **k: dict(clean, checks=[])), \
                mock.patch.object(settings, "get", side_effect=lambda k, d=None: "auto_after_validation"
                                  if k == "publish.mode" else settings.DEFAULTS.get(k, d)), \
                mock.patch.object(pipeline, "_viral", lambda *a: None), \
                mock.patch("studio.youtube.oauth.connected", lambda *a: True):
            pipeline.stage_qa(p)
        self.assertEqual(db.q1("SELECT status FROM productions WHERE id=?", (pid,))["status"], "awaiting_approval")


class EveryWorkflowTests(unittest.TestCase):
    def test_a_workflow_calling_approve_directly_still_meets_the_gate(self):
        ch = channel("Story channel", approve_first_n=3)          # auto-publish on, no uploads yet
        pid = prod(channel_id=ch["id"], kind="story", qa=json.dumps({"passed": True, "auto_publish_ok": True}))
        pipeline.approve(pid, privacy="public", publish_at=None, by="auto (channel auto-publish)")
        row = db.q1("SELECT status, error FROM productions WHERE id=?", (pid,))
        self.assertEqual(row["status"], "awaiting_approval")
        self.assertIn("first 3", row["error"])
        self.assertIsNone(db.q1("SELECT 1 FROM tasks WHERE production_id=? AND type='upload'", (pid,)))

    def test_a_private_automatic_upload_is_not_publishing(self):
        ch = channel("Private review", approve_first_n=3)
        pid = prod(channel_id=ch["id"], qa=json.dumps({"passed": True, "auto_publish_ok": False}))
        pipeline.approve(pid, privacy="private", publish_at=None, by="auto (private upload)")
        self.assertEqual(db.q1("SELECT status FROM productions WHERE id=?", (pid,))["status"], "approved")

    def test_the_owner_can_still_approve_by_hand(self):
        ch = channel("Manual", approve_first_n=3)
        pid = prod(channel_id=ch["id"])
        pipeline.approve(pid, privacy="public", publish_at=None, by="owner@test")
        self.assertEqual(db.q1("SELECT status FROM productions WHERE id=?", (pid,))["status"], "approved")


class RepeatTests(unittest.TestCase):
    def test_the_same_story_on_another_channel_is_flagged(self):
        scenes = [{"narration": "The central bank raised interest rates by half a percent on Thursday and markets "
                                "fell sharply across Asia as investors moved money into bonds and gold."}] * 3
        a, b = channel("First"), channel("Second")
        prod(channel_id=a["id"], youtube_video_id="old1", status="uploaded", uploaded_at=db.iso(),
             package=json.dumps({"title": "Why the bank raised rates today", "scenes": scenes}))
        pid = prod(channel_id=b["id"], package=json.dumps({"title": "Why the bank raised rates today",
                                                           "scenes": scenes}))
        r = ops.repeat_check(pipeline.load(pid))
        self.assertFalse(r["ok"])
        self.assertEqual(r["severity"], "warn")
        other = prod(channel_id=b["id"], package=json.dumps({"title": "A puppy learns to swim at the lake",
                                                             "scenes": [{"narration": "Milo the puppy wades in."}]}))
        self.assertTrue(ops.repeat_check(pipeline.load(other))["ok"])

    def test_story_uploads_with_dialogue_lines_do_not_break_the_check(self):
        story = {"title": "Talking samosa", "scenes": [{"lines": [{"speaker": "pappu", "text": "Samosa bol raha hai!"}]}]}
        prod(channel_id=channel("Stories")["id"], kind="story", youtube_video_id="st1", status="uploaded",
             uploaded_at=db.iso(), package=json.dumps(story))
        pid = prod(channel_id=channel("News")["id"],
                   package=json.dumps({"title": "Markets today", "scenes": [{"narration": "बाज़ार आज गिरा।"}]}))
        self.assertTrue(ops.repeat_check(pipeline.load(pid))["ok"])
        self.assertIn("Samosa bol raha hai", ops.spoken_text(story))


if __name__ == "__main__":
    unittest.main()
