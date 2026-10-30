"""Failure simulations: missing/invalid keys, expired YouTube login, stalled connections, every writer down, an
unreachable reference repository. External services are mocked; nothing real is called.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins
os.environ.setdefault("STUDIO_OFFLINE", "1")              # never call the free image services

from pydantic import BaseModel  # noqa: E402

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import ai, channels, pipeline, settings  # noqa: E402
from studio.ai import providers, router  # noqa: E402
from studio.youtube import oauth  # noqa: E402


class Tiny(BaseModel):
    x: int


class KeyTests(unittest.TestCase):
    def test_missing_keys_skip_the_provider_and_the_job_waits_instead_of_failing(self):
        with mock.patch.object(settings, "has_secret", lambda name: False), \
                mock.patch.object(providers.OllamaProvider, "available", lambda self: (False, "no model")):
            for pid in ("gemini", "ollama_cloud"):
                self.assertFalse(providers.get(pid).available()[0])
            with self.assertRaises(router.AllProvidersFailed) as cm:
                ai.generate("script", "s", "p", Tiny)
        self.assertTrue(cm.exception.temporary)                     # the worker waits and retries later

    def test_a_paid_writer_is_never_added_to_the_chain_by_itself(self):
        """With the shipped settings no paid writer is used; only choosing Claude in Settings adds it."""
        keys = ("ai.default_provider", "ai.fallback_provider", "ai.task_providers", "ai.on_budget_exceeded",
                "ai.free_first")
        real = settings.get
        with mock.patch.object(settings, "get", lambda k, *d: settings.DEFAULTS[k] if k in keys else real(k, *d)):
            for task in settings.AI_TASKS:
                self.assertNotIn("anthropic", router.provider_chain(task))


class TimeoutTests(unittest.TestCase):
    def test_cloud_calls_give_up_on_a_stalled_connection(self):
        t = providers.OllamaProvider(cloud=True)._timeout()
        self.assertLessEqual(t.read, 300)
        self.assertLessEqual(t.connect, 10)


class ExpiredLoginTests(unittest.TestCase):
    def test_an_expired_youtube_login_never_starts_an_upload(self):
        cid = channels.create({"name": f"Expired {os.urandom(2).hex()}"}, 1)
        pid = db.insert("productions", {"format": "long", "title": "t", "status": "approved", "stage": "qa",
                                        "kind": "general", "workspace_id": 1, "channel_id": cid, "package": "{}",
                                        "qa": json.dumps({"passed": True}), "created_at": db.iso(),
                                        "updated_at": db.iso()})
        sent = []
        with mock.patch.object(oauth, "connected", lambda *a: True), \
                mock.patch.object(oauth, "verify_destination",
                                  side_effect=oauth.AuthError("Google says the login expired — connect again")), \
                mock.patch("studio.youtube.upload.upload", lambda *a, **k: sent.append(1)):
            with self.assertRaises(pipeline.NeedsAttention):
                pipeline._run_upload({"production_id": pid}, {})
        row = db.q1("SELECT status, error FROM productions WHERE id=?", (pid,))
        self.assertEqual((row["status"], sent), ("approved", []))
        self.assertIn("expired", row["error"])


class InterruptedUploadTests(unittest.TestCase):
    def test_a_finished_session_is_reconciled_not_uploaded_again(self):
        """Crash after YouTube received the whole file but before we saved the video ID: the retry asks the saved
        session, gets the finished video back and starts no new upload."""
        import httpx

        from studio.youtube import upload
        video = Path(tempfile.mkdtemp()) / "v.mp4"
        video.write_bytes(b"0" * 2048)
        pid = db.insert("productions", {"format": "long", "title": "t", "status": "uploading", "stage": "qa",
                                        "kind": "general", "workspace_id": 1, "package": "{}", "qa": "{}",
                                        "video_path": str(video), "youtube_upload_uri": "https://upload.example/s1",
                                        "created_at": db.iso(), "updated_at": db.iso()})
        calls = []

        def fake(method, url, **kw):
            calls.append((method, url))
            return httpx.Response(200, json={"id": "VIDDONE", "status": {"privacyStatus": "private"}},
                                  request=httpx.Request(method, url))
        prod = db.q1("SELECT * FROM productions WHERE id=?", (pid,))
        with mock.patch.object(upload.oauth, "request", fake):
            upload.upload(prod, {"title": "t", "description": "d"}, [])
        self.assertEqual(calls[0], ("PUT", "https://upload.example/s1"))       # asked the old session first
        self.assertNotIn("POST", [m for m, _ in calls])                        # no second upload started
        self.assertEqual(db.q1("SELECT youtube_video_id FROM productions WHERE id=?", (pid,))["youtube_video_id"],
                         "VIDDONE")


class RepositoryTests(unittest.TestCase):
    def test_the_app_never_refers_to_the_reference_repository(self):
        out = subprocess.run(["grep", "-rIl", "darkzOGx", str(config.ROOT / "studio")], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "")

    def test_sadtalker_can_be_rebuilt_from_this_repository(self):
        v = config.ROOT / "install" / "vendor" / "sadtalker"
        for f in ("sadtalker-src-cd4c046-patched.tar.gz", "LICENSE-SadTalker", "requirements-lock.txt",
                  "weights.sha256", "local-patches.diff"):
            self.assertTrue((v / f).exists(), f)
        names = subprocess.run(["tar", "-tzf", str(v / "sadtalker-src-cd4c046-patched.tar.gz")], capture_output=True,
                               text=True).stdout
        self.assertIn("inference.py", names)
        self.assertIn("src/facerender/modules/util.py", names)
        self.assertNotIn("github.com", Path(config.ROOT / "install" / "install-sadtalker.sh").read_text())


if __name__ == "__main__":
    unittest.main()
