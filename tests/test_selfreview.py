"""Self-review of held videos: publish what passes every gate, fix what can be fixed, leave the rest for the owner —
never around the auto-publish gate or a finance video's compliance gate. Nothing external is called.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
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
from studio import channels, ops, pipeline, selfreview  # noqa: E402
from studio.finance import gate, shorts  # noqa: E402


def prod(**kw) -> int:
    row = {"format": "short", "title": "A video", "status": "awaiting_approval", "stage": "qa", "kind": "general",
           "workspace_id": 1, "qa": json.dumps({"passed": True, "auto_publish_ok": True}), "package": "{}",
           "created_at": db.iso(), "updated_at": db.iso(), **kw}
    return db.insert("productions", row)


def channel(**kw) -> dict:
    return channels.get(channels.create({"name": f"SR {os.urandom(2).hex()}", "auto_publish": True,
                                         "approve_first_n": 0, **kw}, 1))


def status(pid: int) -> str:
    return db.q1("SELECT status FROM productions WHERE id=?", (pid,))["status"]


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.enq = mock.patch("studio.worker.enqueue").start()
        mock.patch("studio.plans.can", return_value=True).start()
        self.addCleanup(mock.patch.stopall)

    def test_ready_video_is_published_through_the_normal_gate(self):
        pid = prod(channel_id=channel()["id"])
        r = selfreview.review(pid)
        self.assertEqual(r["result"], "published")
        self.assertEqual(status(pid), "approved")

    def test_channel_that_did_not_opt_in_still_waits(self):
        pid = prod(channel_id=channel(auto_publish=False)["id"])
        r = selfreview.review(pid)
        self.assertEqual(r["result"], "waiting")
        self.assertIn("auto-publish is off", r["why"])
        self.assertEqual(status(pid), "awaiting_approval")

    def test_owner_hold_repeat_and_stop_are_respected(self):
        ch = channel()
        held = prod(channel_id=ch["id"], request=json.dumps({"hold_for_review": True}))
        rep = prod(channel_id=ch["id"], error="Looks like a repeat of #12 — needs a human look")
        self.assertEqual(selfreview.review(held)["result"], "waiting")
        self.assertEqual(selfreview.review(rep)["result"], "waiting")
        other = prod(channel_id=ch["id"])
        ops.set_stop(True, "test")
        try:
            self.assertEqual(selfreview.review(other)["result"], "waiting")
        finally:
            ops.set_stop(False, "test")
        self.assertEqual({status(held), status(rep), status(other)}, {"awaiting_approval"})

    def test_finance_video_needs_the_compliance_gate_to_allow_public(self):
        pid = prod(channel_id=channel()["id"], kind="fib_lesson")
        with mock.patch.object(gate, "run", return_value={"status": "NEEDS_REVIEW", "allowed_privacy": [],
                                                          "critical_failures": ["x"]}):
            r = selfreview.review(pid)
        self.assertEqual(r["result"], "waiting")
        self.assertEqual(status(pid), "awaiting_approval")


class FixTests(unittest.TestCase):
    def setUp(self):
        self.reset = mock.patch.object(pipeline, "reset_to_stage").start()
        self.addCleanup(mock.patch.stopall)

    def test_blocked_lesson_rewrites_its_script_at_most_twice(self):
        pid = prod(channel_id=channel()["id"], kind="fib_lesson", status="needs_review", stage="compliance",
                   error="NEEDS REVIEW — 4. No fabricated facts")
        self.assertEqual(selfreview.review(pid)["result"], "fixing")
        self.reset.assert_called_with(pid, "script")
        selfreview.review(pid)
        r = selfreview.review(pid)
        self.assertEqual(r["result"], "waiting")
        self.assertEqual(self.reset.call_count, selfreview.MAX_TRIES)

    def test_failed_technical_qa_renders_again(self):
        pid = prod(channel_id=channel()["id"], status="needs_review", stage="qa", error="NEEDS REVIEW — frozen video")
        selfreview.review(pid)
        self.reset.assert_called_with(pid, "render")


class RootCauseTests(unittest.TestCase):
    def test_fact_sheet_short_has_enough_teaching_for_the_gate(self):
        for lang in ("hi", "en"):
            text = shorts.tx("zones", lang, ndate="4 Nov", s2="1", p="2", r2="3", rdate="3 Nov", close="4", d="5",
                             side="x")
            self.assertGreaterEqual(len(text.split()), 40, lang)

    def test_lesson_lint_rejects_unverified_intraday(self):
        from studio.finance import gate as g
        self.assertTrue(g._affirmative_mentions(g.INTRADAY_WORDS, "इसके लिए हमें इंट्राडे डेटा देखना पड़ता है।"))
        self.assertFalse(g._affirmative_mentions(g.INTRADAY_WORDS, "इस लेसन में इंट्राडे डेटा नहीं है।"))


if __name__ == "__main__":
    unittest.main()
