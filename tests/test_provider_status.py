"""The AI providers page: every writer, media provider and platform with status and cost, never a secret value,
owner only. Nothing external is called for paid services.
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
from studio import auth, provider_status, settings  # noqa: E402

SECRET = "sk-test-NEVER-SHOW-THIS-1234567890"


class ProviderPageTests(unittest.TestCase):
    def test_overview_lists_writers_in_chain_order_and_marks_paid(self):
        o = provider_status.overview()
        ids = [w["id"] for w in o["writers"]]
        self.assertEqual(set(ids), set(settings.PROVIDERS))
        self.assertEqual(next(w for w in o["writers"] if w["id"] == "anthropic")["cost"], "paid")
        self.assertTrue(o["media"])

    def test_page_never_shows_a_key_and_is_owner_only(self):
        from fastapi.testclient import TestClient

        from studio.web import app as webapp
        if not db.q1("SELECT id FROM users WHERE email='prov-owner@test.local'"):
            auth.approve(auth.register("prov-owner@test.local", "a long enough password")["user_id"], 1)
        tok, _ = auth.login("prov-owner@test.local", "a long enough password")
        c = TestClient(webapp.app, base_url=f"http://127.0.0.1:{config.PORT}", follow_redirects=False)
        c.cookies.set("sid", tok)
        with mock.patch.object(settings, "secret", lambda name: SECRET), \
                mock.patch.object(webapp, "is_admin", lambda request: True):
            html = c.get("/providers").text
        self.assertIn("Script writers", html)
        self.assertNotIn(SECRET, html)
        with mock.patch.object(webapp, "is_admin", lambda request: False):
            self.assertEqual(c.get("/providers").status_code, 403)


if __name__ == "__main__":
    unittest.main()
