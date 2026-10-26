"""The daily learning loop: lessons come from a channel's own results, reach its next prompts, and can be switched off.
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
from studio import auth, channels, learning  # noqa: E402

auth.ensure_default_workspace()
channels.ensure_default()


def published(cid: int, views: int, pct: float, days_ago: float) -> int:
    pid = db.insert("productions", {"format": "long", "title": f"Video {views}", "status": "uploaded", "stage": "review",
                                    "kind": "general", "workspace_id": 1, "channel_id": cid, "package": json.dumps(
                                        {"scenes": [{"narration": f"hook {views}"}]}), "timeline": "{}",
                                    "publish_at": db.iso_in(-days_ago * 86400), "created_at": db.iso(),
                                    "updated_at": db.iso()})
    db.insert("analytics", {"production_id": pid, "video_id": f"yt{pid}", "fetched_at": db.iso(),
                            "metrics": json.dumps({"views": views, "averageViewPercentage": pct, "likes": 1})})
    return pid


class LearningTests(unittest.TestCase):
    def test_review_writes_cited_lessons_and_respects_switched_off_ones(self):
        cid = channels.create({"name": "Learner"}, 1)
        ids = [published(cid, v, p, 5) for v, p in ((900, 61.0), (120, 30.0), (450, 44.0))]
        self.assertEqual(len(learning.results(cid)), 3)
        db.insert("lessons", {"channel_id": cid, "workspace_id": 1, "kind": "audience", "lesson": "Use clickbait titles",
                              "status": "off", "created_at": db.iso(), "updated_at": db.iso()})
        seen = {}

        def fake_generate(task, system, prompt, model_cls, **kw):
            seen["prompt"] = prompt
            return model_cls(lessons=[
                {"lesson": "Open with the result in the first 3 seconds", "kind": "audience",
                 "evidence": f"#{ids[0]} 61% viewed vs #{ids[1]} 30%", "confidence": "low"},
                {"lesson": "Use clickbait titles", "kind": "audience", "evidence": "x", "confidence": "low"}]), \
                {"provider": "fake", "model": "fake"}
        with mock.patch("studio.ai.generate", fake_generate):
            out = learning.review(cid)
        self.assertIn(f"#{ids[0]}", seen["prompt"])
        self.assertIn("Use clickbait titles", seen["prompt"])            # told not to propose it again
        self.assertEqual(out["videos"], 3)
        active = [r["lesson"] for r in learning.active(cid)]
        self.assertEqual(active, ["Open with the result in the first 3 seconds"])
        self.assertIn("first 3 seconds", learning.prompt_block(cid))

    def test_too_few_results_give_no_audience_lessons(self):
        cid = channels.create({"name": "New one"}, 1)
        published(cid, 10, 50.0, 5)
        db.insert("productions", {"format": "long", "title": "made", "status": "awaiting_approval", "stage": "review",
                                  "kind": "general", "workspace_id": 1, "channel_id": cid, "created_at": db.iso(),
                                  "updated_at": db.iso()})

        def fake_generate(task, system, prompt, model_cls, **kw):
            self.assertIn("Give NO audience lessons", prompt)
            return model_cls(lessons=[
                {"lesson": "Viewers love long intros", "kind": "audience", "evidence": "-", "confidence": "low"},
                {"lesson": "Name a concrete subject in every visual search", "kind": "quality", "evidence": "card share",
                 "confidence": "medium"}]), {"provider": "fake", "model": "fake"}
        with mock.patch("studio.ai.generate", fake_generate):
            learning.review(cid)
        self.assertEqual([r["kind"] for r in learning.active(cid)], ["quality"])

    def test_no_channel_gives_no_lessons(self):
        self.assertEqual(learning.prompt_block(None), "")


if __name__ == "__main__":
    unittest.main()
