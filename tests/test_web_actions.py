"""Buttons that change things, pressed through the web app (not only the functions behind them): approve,
emergency stop. A crash here means a dead button on the dashboard.
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
from studio import auth, channels, ops  # noqa: E402


def client():
    from fastapi.testclient import TestClient

    from studio.web import app as webapp
    if not db.q1("SELECT id FROM users WHERE email='buttons@test.local'"):
        auth.approve(auth.register("buttons@test.local", "a long enough password")["user_id"], 1)
    db.ex("UPDATE users SET is_admin=1 WHERE email='buttons@test.local'")
    tok, _ = auth.login("buttons@test.local", "a long enough password")
    c = TestClient(webapp.app, base_url=f"http://127.0.0.1:{config.PORT}", follow_redirects=False,
                   raise_server_exceptions=True)
    c.cookies.set("sid", tok)
    c.headers["X-CSRF-Token"] = webapp.csrf_for_seed(tok)
    return c, auth.session(tok)["workspace"]["id"]


class ButtonTests(unittest.TestCase):
    def test_approve_button_works_and_records_who_approved(self):
        c, ws = client()
        cid = channels.create({"name": f"Btn {os.urandom(2).hex()}"}, ws)
        pid = db.insert("productions", {"format": "long", "title": "Ready video", "status": "awaiting_approval",
                                        "stage": "qa", "kind": "general", "workspace_id": ws, "channel_id": cid,
                                        "package": "{}", "qa": json.dumps({"passed": True}), "created_at": db.iso(),
                                        "updated_at": db.iso()})
        with mock.patch("studio.plans.can", lambda *a: True):
            r = c.post(f"/api/productions/{pid}/approve", json={"privacy": "private"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"], r.text)
        self.assertEqual(db.q1("SELECT status FROM productions WHERE id=?", (pid,))["status"], "approved")
        self.assertTrue(any(a["actor"] == "buttons@test.local" and a["production_id"] == pid for a in ops.recent(20)))

    def test_emergency_stop_button_on_and_off(self):
        c, _ = client()
        try:
            self.assertTrue(c.post("/api/ops/emergency-stop", json={"on": True, "reason": "test"}).json()["ok"])
            self.assertIsNotNone(ops.stopped())
            self.assertIn("Emergency stop is ON", c.get("/queue").text)
        finally:
            c.post("/api/ops/emergency-stop", json={"on": False})
        self.assertIsNone(ops.stopped())


if __name__ == "__main__":
    unittest.main()
