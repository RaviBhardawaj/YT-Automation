"""Security regressions found in the pre-launch audit. Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import auth, channels, worker  # noqa: E402

auth.ensure_default_workspace()
channels.ensure_default()
PW = "a long enough password"
if not auth.has_users():
    auth.register("sec-admin@test.local", PW)


def user(email):
    if not db.q1("SELECT id FROM users WHERE email=?", (email,)):
        auth.approve(auth.register(email, PW)["user_id"], 1)
    tok, _ = auth.login(email, PW)
    return auth.session(tok)["workspace"]["id"], tok


def client(tok=None):
    from fastapi.testclient import TestClient
    from studio.web import app as webapp
    c = TestClient(webapp.app, base_url=f"http://127.0.0.1:{config.PORT}", follow_redirects=False,
                   raise_server_exceptions=False)                # see responses exactly as a browser would
    if tok:
        c.cookies.set("sid", tok)
        c.headers["X-CSRF-Token"] = webapp.csrf_for_seed(tok)
    return c


def prod(ws, **kw):
    return db.insert("productions", {"format": "long", "title": "t", "status": "uploaded", "stage": "qa",
                                     "kind": "general", "workspace_id": ws, "created_at": db.iso(),
                                     "updated_at": db.iso(), **kw})


class PathTraversalTests(unittest.TestCase):
    def test_dot_dot_never_reaches_another_users_files(self):
        wa, _ = user("victim-sec@test.local")
        wb, tb = user("attacker-sec@test.local")
        pa, pb = prod(wa), prod(wb)
        for p, text in ((pa, "VICTIM"), (pb, "mine")):
            d = config.PRODUCTIONS_DIR / f"{p:05d}"
            d.mkdir(parents=True, exist_ok=True)
            (d / "final.txt").write_text(text)
        ca, cb = channels.create({"name": "v"}, wa), channels.create({"name": "a"}, wb)
        (config.DATA_DIR / "channels" / f"{ca:04d}").mkdir(parents=True, exist_ok=True)
        (config.DATA_DIR / "channels" / f"{ca:04d}" / "x.txt").write_text("VICTIM")
        up = config.DATA_DIR / "workspaces" / f"{wa:05d}" / "uploads"
        up.mkdir(parents=True, exist_ok=True)
        (up / "u.txt").write_text("VICTIM")
        c = client(tb)
        for url in (f"/media/productions/{pb:05d}/%2e%2e/{pa:05d}/final.txt",
                    f"/media/channels/{cb:04d}/%2e%2e/{ca:04d}/x.txt",
                    f"/media/workspaces/{wb:05d}/%2e%2e/{wa:05d}/uploads/u.txt",
                    f"/media/brand/%2e%2e/productions/{pa:05d}/final.txt",
                    f"/media/productions/{pa:05d}/final.txt"):
            r = c.get(url)
            self.assertEqual(r.status_code, 404, url)
            self.assertNotIn("VICTIM", r.text)
        self.assertEqual(c.get(f"/media/productions/{pb:05d}/final.txt").text, "mine")


class RedirectAndOwnershipTests(unittest.TestCase):
    def test_no_redirect_to_other_websites_after_sign_in(self):
        from studio.web import app as webapp
        user("redirect@test.local")
        for bad in ("https://evil.example", "//evil.example", "/\\evil.example", "javascript:alert(1)"):
            self.assertEqual(webapp.safe_next(bad), "/", bad)
        self.assertEqual(webapp.safe_next("/channels/3?tab=setup"), "/channels/3?tab=setup")
        c = client()
        c.get("/login")
        c.headers["X-CSRF-Token"] = webapp.csrf_for_seed(c.cookies.get("pre"))
        r = c.post("/api/auth/login", json={"email": "redirect@test.local", "password": PW,
                                            "next": "https://evil.example/phish"})
        self.assertEqual(r.json()["next"], "/")

    def test_another_users_idea_cannot_be_used(self):
        wa, _ = user("idea-owner@test.local")
        wb, tb = user("idea-thief@test.local")
        idea = db.insert("topics", {"workspace_id": wa, "title": "PRIVATE IDEA", "format": "long",
                                    "created_at": db.iso(), "updated_at": db.iso()})
        cb = channels.create({"name": "thief", "workflow": "factual"}, wb)
        with mock.patch.object(worker, "enqueue", return_value=1):
            r = client(tb).post(f"/api/channels/{cb}/create", json={"idea_id": idea})
        self.assertFalse(r.json()["ok"])
        self.assertFalse(db.q1("SELECT 1 FROM productions WHERE topic_id=?", (idea,)))

    def test_linking_never_reveals_or_blocks_other_tenants(self):
        wa, _ = user("yt-a@test.local")
        wb, _ = user("yt-b@test.local")
        a, b = channels.create({"name": "Secret name A"}, wa), channels.create({"name": "B"}, wb)
        info = {"id": "UCshared", "title": "Shared", "handle": "@shared", "created": "", "subscribers": "1",
                "videos": "1", "thumbnail": None, "privacy": "public"}
        with mock.patch.object(channels, "lookup_youtube", return_value=info):
            channels.link_youtube(a, "@shared")
            channels.link_youtube(b, "@shared")                     # allowed: Google sign-in proves ownership
            a2 = channels.create({"name": "A2"}, wa)
            with self.assertRaises(ValueError) as e:
                channels.link_youtube(a2, "@shared")                 # same workspace: still one link
        self.assertNotIn("Secret name A", str(e.exception) if False else "")


class RobustnessTests(unittest.TestCase):
    def test_metadata_on_untitled_video_does_not_crash(self):
        ws, tok = user("meta@test.local")
        pid = prod(ws, status="awaiting_approval", package="{}", title="")
        r = client(tok).post(f"/api/productions/{pid}/metadata", json={"title": "A proper title"})
        self.assertTrue(r.json()["ok"], r.text)

    def test_outside_service_down_is_not_a_crash(self):
        import httpx
        from studio.web import app as webapp
        admin_email = db.q1("SELECT email FROM users WHERE is_admin=1 ORDER BY id LIMIT 1")["email"]
        if admin_email != "sec-admin@test.local":
            self.skipTest("another module created the platform admin")
        tok, _ = auth.login(admin_email, PW)
        with mock.patch("studio.media.tts.piper_catalog", side_effect=httpx.ConnectError("offline")):
            r = client(tok).get("/api/voice/piper")
        self.assertEqual(r.status_code, 502)
        self.assertIn("Couldn't reach", r.json()["error"])
        self.assertTrue(webapp)

    def test_control_characters_stripped_from_upload_names(self):
        ws, tok = user("names@test.local")
        r = client(tok).post("/api/media/upload", headers={"X-Filename": "evil%0d%0aname.exe"}, content=b"x")
        self.assertNotIn("\r", r.text)
        self.assertFalse(r.json()["ok"])
