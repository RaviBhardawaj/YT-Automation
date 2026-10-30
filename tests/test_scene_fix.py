"""Editor review: after the one full rewrite, a scene the editor still flags is rewritten on its own and checked
again, so one unsupported detail no longer stops the upload. The AI is faked; nothing real is called.
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
from studio import pipeline  # noqa: E402
from studio.content import package as content  # noqa: E402

ISSUE = {"scene": 2, "category": "factual", "severity": "high", "detail": "199 runs not in evidence",
         "suggestion": "say India chased easily"}


def prod() -> dict:
    scenes = [{"narration": f"line {i}", "caption_text": f"line {i}", "on_screen_text": "", "visual_description": "x",
               "visual_query": "x", "visual_type": "photo"} for i in range(1, 4)]
    pkg = {"title": "t", "scenes": scenes, "fact_checks": [], "revision": 1}
    pid = db.insert("productions", {"format": "short", "title": "t", "status": "reviewing", "stage": "review",
                                    "kind": "general", "workspace_id": 1, "package": db.dumps(pkg),
                                    "created_at": db.iso(), "updated_at": db.iso()})
    return {"id": pid, "test_mode": 0, "topic_id": 1, "package": pkg}


def fake_generate(fixed_text: str):
    def gen(task, system, prompt, model_cls, **kw):
        r = model_cls.model_validate({"fixes": [{"scene": 2, "narration": fixed_text, "on_screen_text": ""}]})
        if kw.get("validate"):
            kw["validate"](r)
        return r, {"provider": "fake", "model": "m"}
    return gen


class SceneFixTests(unittest.TestCase):
    def _run(self, reviews):
        p = prod()
        from studio import profile
        with mock.patch.object(profile, "language", lambda: "English"), \
                mock.patch.object(pipeline, "_topic", lambda p: {"title": "t"}), \
                mock.patch.object(pipeline, "_evidence", lambda p: []), \
                mock.patch.object(content, "review", side_effect=[(r, {"provider": "fake"}) for r in reviews]), \
                mock.patch.object(content.ai, "generate", fake_generate("India chased easily")):
            self.assertIsNone(pipeline.stage_review(p))
        return db.loads(db.q1("SELECT package FROM productions WHERE id=?", (p["id"],))["package"], {})

    def test_flagged_scene_alone_is_rewritten_and_passes(self):
        pkg = self._run([{"issues": [ISSUE], "ready": False, "overall": ""},
                         {"issues": [], "ready": True, "overall": "ok"}])
        self.assertEqual([s["narration"] for s in pkg["scenes"]], ["line 1", "India chased easily", "line 3"])
        self.assertEqual((pkg["scene_fixes"], pkg["review"]["ready"]), (1, True))

    def test_gives_up_after_two_rounds_and_keeps_the_flag(self):
        bad = {"issues": [ISSUE], "ready": False, "overall": ""}
        pkg = self._run([bad, bad, bad])
        self.assertEqual(pkg["scene_fixes"], 2)
        self.assertFalse(pkg["review"]["ready"])          # still waits for the owner's approval


if __name__ == "__main__":
    unittest.main()
