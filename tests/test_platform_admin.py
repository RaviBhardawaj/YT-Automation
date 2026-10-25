"""Superadmin approval, feature permissions, public pages, upgrade requests and ads.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins

from studio import config, db, settings  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import auth, channels, pipeline, plans  # noqa: E402

auth.ensure_default_workspace()
channels.ensure_default()
PW = "a long enough password"
if not auth.has_users():
    auth.register("first-admin@test.local", PW)            # first account = platform admin
ADMIN = db.q1("SELECT email FROM users WHERE is_admin=1 ORDER BY id LIMIT 1")["email"]


def client(token: str | None = None):
    from fastapi.testclient import TestClient
    from studio.web import app as webapp
    c = TestClient(webapp.app, base_url=f"http://127.0.0.1:{config.PORT}", follow_redirects=False)
    if token:
        c.cookies.set("sid", token)
        c.headers["X-CSRF-Token"] = webapp.csrf_for_seed(token)
    return c


def user(email: str, approve: bool = True) -> tuple[int, int, str]:
    if not db.q1("SELECT id FROM users WHERE email=?", (email,)):
        if not auth.has_users():
            auth.register("first-admin@test.local", PW)
        auth.register(email, PW)
    u = db.q1("SELECT id FROM users WHERE email=?", (email,))
    if approve:
        auth.approve(u["id"], 1)
    tok, _ = auth.login(email, PW)
    return u["id"], auth.session(tok)["workspace"]["id"], tok


class ApprovalTests(unittest.TestCase):
    def test_new_signup_waits_and_sees_only_the_waiting_page(self):
        settings.set("auth.require_approval", True)
        uid, ws, tok = user("waiting@test.local", approve=False)
        self.assertEqual(db.q1("SELECT status FROM users WHERE id=?", (uid,))["status"], "pending")
        c = client(tok)
        r = c.get("/")
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/pending"))
        self.assertEqual(c.get("/pending").status_code, 200)
        self.assertEqual(c.post("/api/create", json={"request": "x"}).status_code, 403)
        auth.approve(uid, 1, "creator")
        self.assertEqual(c.get("/channels").status_code, 200)
        self.assertEqual(plans.workspace(ws)["plan"], "creator")

    def test_reject_signs_out(self):
        uid, _, tok = user("nope@test.local", approve=False)
        auth.reject(uid, 1)
        self.assertIsNone(auth.session(tok))

    def test_approval_can_be_switched_off(self):
        settings.set("auth.require_approval", False)
        auth.register("open@test.local", PW)
        self.assertEqual(db.q1("SELECT status FROM users WHERE email='open@test.local'")["status"], "active")
        settings.set("auth.require_approval", True)


class PermissionTests(unittest.TestCase):
    def test_plan_defaults_and_overrides(self):
        _, ws, _ = user("perm@test.local")
        self.assertTrue(plans.can(ws, "create"))
        self.assertFalse(plans.can(ws, "ai_video"))                    # free plan: no paid AI video
        db.ex("UPDATE workspaces SET permissions=? WHERE id=?", ('{"publish": false, "ai_video": true}', ws))
        self.assertFalse(plans.can(ws, "publish"))
        self.assertTrue(plans.can(ws, "ai_video"))

    def test_switched_off_features_are_refused(self):
        _, ws, tok = user("blocked@test.local")
        db.ex("UPDATE workspaces SET permissions=? WHERE id=?",
              ('{"create": false, "upload_edit": false, "publish": false, "schedules": false}', ws))
        c = client(tok)
        self.assertIn("isn't enabled", c.post("/api/create", json={"request": "a cat video"}).json()["error"])
        r = c.post("/api/media/upload", headers={"X-Filename": "a.mp4"}, content=b"x")
        self.assertIn("isn't enabled", r.json()["error"])
        r = c.post("/api/channels", json={"name": "Sched", "schedule_enabled": True})
        self.assertIn("isn't enabled", r.json()["error"])
        cid = channels.create({"name": "Mine"}, ws)
        pid = db.insert("productions", {"format": "long", "title": "t", "status": "awaiting_approval", "stage": "review",
                                        "kind": "general", "workspace_id": ws, "channel_id": cid,
                                        "qa": '{"passed": true}', "package": "{}", "created_at": db.iso(),
                                        "updated_at": db.iso()})
        self.assertIn("isn't enabled", c.post(f"/api/productions/{pid}/publish", json={}).json()["error"])
        pipeline.approve(pid, privacy="public", publish_at=None, by="auto")     # automation: kept, not uploaded
        self.assertEqual(db.q1("SELECT status FROM productions WHERE id=?", (pid,))["status"], "awaiting_approval")

    def test_admin_saves_overrides_relative_to_plan(self):
        _, ws, _ = user("ovr@test.local")
        admin_tok, _ = auth.login("first-admin@test.local", PW)
        if not db.q1("SELECT is_admin FROM users WHERE email='first-admin@test.local'")["is_admin"]:
            self.skipTest("another test module created the first account")
        r = client(admin_tok).post(f"/api/admin/workspaces/{ws}/features",
                                   json={"create": True, "upload_edit": True, "publish": True, "schedules": False,
                                         "ai_video": False})
        self.assertTrue(r.json()["ok"], r.text)
        self.assertEqual(db.loads(plans.workspace(ws)["permissions"] if False else
                                  db.q1("SELECT permissions FROM workspaces WHERE id=?", (ws,))["permissions"]),
                         {"schedules": False})


class PublicSiteTests(unittest.TestCase):
    def test_public_pages_without_signing_in(self):
        c = client()
        user("someone@test.local")
        for path in ("/home", "/pricing", "/privacy", "/terms", "/login", "/signup"):
            self.assertEqual(c.get(path).status_code, 200, path)
        r = c.get("/")
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/home"))
        self.assertIn("Google API Services User Data Policy", c.get("/privacy").text)

    def test_upgrade_interest_recorded_once(self):
        _, ws, tok = user("upgrade@test.local")
        c = client(tok)
        for _ in range(2):
            self.assertTrue(c.post("/api/upgrade-request", json={"plan": "pro"}).json()["ok"])
        self.assertEqual(db.q1("SELECT COUNT(*) AS n FROM upgrade_requests WHERE workspace_id=?", (ws,))["n"], 1)
        self.assertFalse(c.post("/api/upgrade-request", json={"plan": "free"}).json()["ok"])

    def test_ads_only_for_free_users_and_csp_follows(self):
        _, ws, tok = user("ads@test.local")
        settings.set("ads.enabled", True)
        settings.set("ads.html", '<div id="test-ad">AD</div>')
        try:
            r = client(tok).get("/productions")
            self.assertIn('id="test-ad"', r.text)
            self.assertIn("googlesyndication", r.headers["content-security-policy"])
            db.ex("UPDATE workspaces SET plan='pro' WHERE id=?", (ws,))
            self.assertNotIn('id="test-ad"', client(tok).get("/productions").text)
        finally:
            settings.set("ads.enabled", False)
        self.assertNotIn("googlesyndication", client(tok).get("/productions").headers["content-security-policy"])

    def test_page_help_and_getting_started(self):
        _, ws, tok = user("newbie@test.local")
        c = client(tok)
        r = c.get("/")
        if r.status_code == 303:                                       # no channel yet → setup first
            self.assertEqual(r.headers["location"], "/welcome")
            channels.create({"name": "First"}, ws)
            r = c.get("/")
        self.assertIn("Getting started", r.text)
        self.assertIn('data-help="create"', c.get("/create").text)


class CsrfTests(unittest.TestCase):
    def test_secret_survives_restart_and_is_private(self):
        from studio.web import app as webapp
        first = webapp._csrf_secret()
        self.assertEqual(webapp._csrf_secret(), first)                  # same key after a "restart"
        self.assertEqual(oct((config.DATA_DIR / ".csrf_secret").stat().st_mode & 0o777), "0o600")

    def test_fresh_token_endpoint_matches_session(self):
        from studio.web import app as webapp
        _, _, tok = user("csrf@test.local")
        c = client(tok)
        self.assertEqual(c.get("/api/csrf").json()["token"], webapp.csrf_for_seed(tok))
        c.headers["X-CSRF-Token"] = "stale"
        self.assertEqual(c.post("/api/storage/clean", json={}).status_code, 403)
        c.headers["X-CSRF-Token"] = c.get("/api/csrf").json()["token"]
        self.assertTrue(c.post("/api/storage/clean", json={}).json()["ok"])
