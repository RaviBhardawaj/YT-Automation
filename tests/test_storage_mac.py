"""Storage page: this Mac's memory/disk usage and uninstalling optional AI tools. Nothing real is deleted —
the parts are faked in a temporary folder.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins
os.environ.setdefault("STUDIO_OFFLINE", "1")              # never call the free image services

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import storage  # noqa: E402

TMP = Path(tempfile.mkdtemp())


def fake_parts() -> list[dict]:
    opt, need = TMP / "opt", TMP / "need"
    for d in (opt, need):
        d.mkdir(exist_ok=True)
        (d / "w.bin").write_bytes(b"0" * 5000)
    return [{"key": "opt", "name": "Optional", "what": "", "paths": [opt], "after": "", "reinstall": ""},
            {"key": "need", "name": "Needed", "what": "", "paths": [need], "required": True}]


class MacTests(unittest.TestCase):
    def test_usage_numbers_are_real(self):
        m = storage.machine()
        self.assertGreater(m["ram_total"], 0)
        self.assertTrue(0 <= m["ram_pct"] <= 100)
        self.assertGreater(m["disk_total"], m["disk_free"])

    def test_every_part_has_a_size_and_the_app_cannot_be_removed(self):
        parts = {c["key"]: c for c in storage.components()}
        self.assertTrue(parts["app"]["required"] and parts["kokoro"]["required"])
        self.assertIn("sadtalker", parts)

    def test_uninstall_frees_an_optional_part_only(self):
        with mock.patch.object(storage, "components", lambda: [
                {**c, "bytes": 5000, "installed": True, "required": c.get("required", False)} for c in fake_parts()]):
            self.assertEqual(storage.uninstall("opt"), 5000)
            self.assertFalse((TMP / "opt").exists())
            with self.assertRaises(ValueError):
                storage.uninstall("need")
            self.assertTrue((TMP / "need").exists())
            with self.assertRaises(ValueError):
                storage.uninstall("nope")

    def test_refused_while_a_video_is_being_made(self):
        tid = db.insert("tasks", {"type": "production", "lane": "media", "status": "running", "payload": "{}", "run_after": db.iso(),
                                  "created_at": db.iso(), "updated_at": db.iso()})
        try:
            with mock.patch.object(storage, "components", lambda: [
                    {**c, "bytes": 1, "installed": True, "required": False} for c in fake_parts()[:1]]):
                with self.assertRaises(ValueError):
                    storage.uninstall("opt")
        finally:
            db.ex("DELETE FROM tasks WHERE id=?", (tid,))


    def test_owner_sees_the_mac_and_uninstall_buttons(self):
        from fastapi.testclient import TestClient

        from studio import auth
        from studio.web import app as webapp
        if not db.q1("SELECT id FROM users WHERE email='mac-owner@test.local'"):
            auth.approve(auth.register("mac-owner@test.local", "a long enough password")["user_id"], 1)
        tok, _ = auth.login("mac-owner@test.local", "a long enough password")
        c = TestClient(webapp.app, base_url=f"http://127.0.0.1:{config.PORT}", follow_redirects=False)
        c.cookies.set("sid", tok)
        with mock.patch.object(webapp, "is_admin", lambda request: True):
            html = c.get("/storage").text
        self.assertIn("This Mac right now", html)
        self.assertIn("Talking presenter (SadTalker)", html)
        self.assertIn("needed", html)


if __name__ == "__main__":
    unittest.main()
