"""Upload switch, no-upload videos, monetization progress and storage clean-up.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import auth, channels, pipeline, storage  # noqa: E402
from studio.youtube import growth  # noqa: E402

auth.ensure_default_workspace()
channels.ensure_default()


def prod(**kw) -> int:
    row = {"format": "long", "title": "A video", "status": "awaiting_approval", "stage": "review", "kind": "general",
           "workspace_id": 1, "qa": json.dumps({"passed": True}), "package": "{}", "created_at": db.iso(),
           "updated_at": db.iso(), **kw}
    return db.insert("productions", row)


def files_for(pid: int) -> None:
    d = storage.folder(pid)
    (d / "clips").mkdir(parents=True, exist_ok=True)
    (d / "final.mp4").write_bytes(b"v" * 1000)
    (d / "thumbnail.jpg").write_bytes(b"t" * 100)
    (d / "clips" / "c1.mp4").write_bytes(b"w" * 5000)
    (d / "narration.wav").write_bytes(b"a" * 3000)


class UploadSwitchTests(unittest.TestCase):
    def test_automation_holds_and_manual_is_refused_when_off(self):
        cid = channels.create({"name": "Switched off", "uploads_enabled": False}, 1)
        pid = prod(channel_id=cid)
        pipeline.approve(pid, privacy="public", publish_at=None, by="auto (channel auto-publish)")
        row = db.q1("SELECT status, error FROM productions WHERE id=?", (pid,))
        self.assertEqual(row["status"], "awaiting_approval")
        self.assertIn("switched off", row["error"])
        self.assertIsNone(db.q1("SELECT 1 FROM tasks WHERE production_id=? AND type='upload'", (pid,)))
        with self.assertRaises(pipeline.NeedsAttention):
            pipeline.approve(pid, privacy="private", publish_at=None, by="user")

    def test_switching_off_after_approval_holds_the_upload(self):
        cid = channels.create({"name": "Turned off later"}, 1)
        pid = prod(channel_id=cid, status="approved")
        channels.update(cid, {"uploads_enabled": False})
        res = pipeline._run_upload({"production_id": pid}, {})
        self.assertIn("held", res)
        self.assertEqual(db.q1("SELECT status FROM productions WHERE id=?", (pid,))["status"], "awaiting_approval")

    def test_no_channel_video_never_auto_uploads(self):
        cid = channels.create({"name": "General"}, 1)
        pid = prod(channel_id=cid, request=json.dumps({"no_upload": True}))
        pipeline.approve(pid, privacy="public", publish_at=None, by="auto")
        self.assertEqual(db.q1("SELECT status FROM productions WHERE id=?", (pid,))["status"], "awaiting_approval")


class MonetizationTests(unittest.TestCase):
    def test_progress_math(self):
        m = {"subscribers": 600, "public_uploads_90d": 4, "watch_hours_12m": 3100, "shorts_views_90d": 0}
        early, full = growth.progress(m, growth.TIERS["early"]), growth.progress(m, growth.TIERS["full"])
        self.assertTrue(early["eligible"])
        self.assertFalse(full["eligible"])
        self.assertEqual(full["subscribers_pct"], 60.0)
        m2 = {"subscribers": 1200, "public_uploads_90d": 0, "watch_hours_12m": 10, "shorts_views_90d": 12_000_000}
        self.assertTrue(growth.progress(m2, growth.TIERS["full"])["eligible"])      # the Shorts route
        self.assertFalse(growth.progress(m2, growth.TIERS["early"])["eligible"])    # early needs 3 uploads


class StorageTests(unittest.TestCase):
    def test_clear_working_files_keeps_finals(self):
        pid = prod(status="uploaded")
        files_for(pid)
        self.assertEqual(storage.working_bytes(pid), 8000)
        self.assertEqual(storage.clear_working_files(pid), 8000)
        d = storage.folder(pid)
        self.assertTrue((d / "final.mp4").exists() and (d / "thumbnail.jpg").exists())
        self.assertFalse((d / "clips").exists() or (d / "narration.wav").exists())

    def test_not_cleared_while_in_production(self):
        pid = prod(status="rendering")
        files_for(pid)
        self.assertEqual(storage.clear_working_files(pid), 0)

    def test_delete_removes_video_outputs_files_and_rows(self):
        pid = prod()
        kid = prod(parent_id=pid, kind="edit_clip")
        files_for(pid)
        files_for(kid)
        db.insert("tasks", {"type": "production", "payload": "{}", "status": "pending", "run_after": db.iso(),
                            "production_id": pid, "created_at": db.iso(), "updated_at": db.iso()})
        freed = storage.delete_video(pid)
        self.assertEqual(freed, 2 * 9100)
        self.assertIsNone(db.q1("SELECT 1 FROM productions WHERE id IN (?,?)", (pid, kid)))
        self.assertFalse(storage.folder(pid).exists() or storage.folder(kid).exists())
        self.assertEqual(db.q1("SELECT status FROM tasks WHERE production_id=?", (pid,))["status"], "cancelled")

    def test_refuses_while_running(self):
        pid = prod(status="rendering")
        db.insert("tasks", {"type": "production", "payload": "{}", "status": "running", "run_after": db.iso(),
                            "production_id": pid, "created_at": db.iso(), "updated_at": db.iso()})
        with self.assertRaises(ValueError):
            storage.delete_video(pid)

    def test_bulk_delete_rejects_other_workspaces(self):
        from fastapi.testclient import TestClient
        from studio.web import app as webapp
        if not db.q1("SELECT id FROM users WHERE email='store-a@test.local'"):
            auth.approve(auth.register("store-a@test.local", "a long enough password")["user_id"], 1)  # approval is on by default
            auth.approve(auth.register("store-b@test.local", "a long enough password")["user_id"], 1)  # approval is on by default
        tok_a, _ = auth.login("store-a@test.local", "a long enough password")
        tok_b, _ = auth.login("store-b@test.local", "a long enough password")
        ws_a = auth.session(tok_a)["workspace"]["id"]
        pid = prod(workspace_id=ws_a)
        c = TestClient(webapp.app, base_url=f"http://127.0.0.1:{config.PORT}", follow_redirects=False)
        c.cookies.set("sid", tok_b)
        c.headers["X-CSRF-Token"] = webapp.csrf_for_seed(tok_b)
        r = c.post("/api/storage/delete", json={"ids": [pid]})
        self.assertFalse(r.json()["ok"])
        self.assertEqual(c.post(f"/api/productions/{pid}/delete").status_code, 404)
        self.assertIsNotNone(db.q1("SELECT 1 FROM productions WHERE id=?", (pid,)))
        c.cookies.set("sid", tok_a)
        c.headers["X-CSRF-Token"] = webapp.csrf_for_seed(tok_a)
        self.assertTrue(c.post(f"/api/productions/{pid}/delete").json()["ok"])
        self.assertIsNone(db.q1("SELECT 1 FROM productions WHERE id=?", (pid,)))
        self.assertEqual(c.get("/storage").status_code, 200)


if __name__ == "__main__":
    unittest.main()


class KeychainSafetyTests(unittest.TestCase):
    def test_tests_never_use_the_real_keychain(self):
        self.assertNotEqual(config.KEYRING_SERVICE, "yt-automation-studio")


class ChannelManagementTests(unittest.TestCase):
    def test_locked_channel_refuses_other_account_and_keeps_old_login(self):
        from unittest import mock
        from studio import settings
        from studio.youtube import oauth
        cid = channels.create({"name": "Kids lock"}, 1)
        db.update("channels", cid, {"youtube_expected_id": "UCkids", "youtube_handle": "@kids",
                                    "youtube_title": "Kids and Comedy"})
        other = mock.Mock(status_code=200)
        other.json.return_value = {"items": [{"id": "UCwrong", "snippet": {"title": "Wrong One"}, "statistics": {},
                                              "status": {}}]}
        with mock.patch.object(oauth, "request", return_value=other):
            with self.assertRaises(oauth.AuthError) as e:
                oauth.fetch_channel(cid)
        self.assertIn("locked", str(e.exception))
        with mock.patch.object(oauth, "connected", return_value=True), \
                mock.patch.object(oauth, "request", return_value=other):
            with self.assertRaises(oauth.AuthError):
                oauth.verify_destination(cid)
        right = mock.Mock(status_code=200)
        right.json.return_value = {"items": [{"id": "UCkids", "snippet": {"title": "Kids and Comedy"}, "statistics": {},
                                              "status": {"longUploadsStatus": "eligible"}}]}
        with mock.patch.object(oauth, "request", return_value=right):
            oauth.fetch_channel(cid)
        ch = channels.get(cid)
        self.assertEqual((ch["youtube_channel_id"], ch["youtube_status"]["long_uploads"]), ("UCkids", "eligible"))
        self.assertIsNone(settings.secret(channels.refresh_token_secret(cid)))

    def test_made_for_kids_is_per_channel(self):
        from studio.youtube import upload
        kids = channels.create({"name": "Kids flag", "made_for_kids": True}, 1)
        grown = channels.create({"name": "Grown-ups"}, 1)
        self.assertTrue(upload._made_for_kids({"channel_id": kids}))
        self.assertFalse(upload._made_for_kids({"channel_id": grown}))

    def test_one_youtube_channel_per_studio_channel(self):
        from unittest import mock
        a = channels.create({"name": "Link A"}, 1)
        b = channels.create({"name": "Link B"}, 1)
        info = {"id": "UCsame", "title": "Same", "handle": "@same", "created": "", "subscribers": "0", "videos": "0",
                "thumbnail": None, "privacy": "public"}
        with mock.patch.object(channels, "lookup_youtube", return_value=info):
            channels.link_youtube(a, "@same")
            with self.assertRaises(ValueError):
                channels.link_youtube(b, "@same")

    def test_handle_parsing(self):
        for text in ("@KidsandComedy-l1s", "https://www.youtube.com/@KidsandComedy-l1s",
                     "youtube.com/channel/UCbQc3MzunNeYBLYLppl6wkA", "UCbQc3MzunNeYBLYLppl6wkA"):
            self.assertTrue(channels.HANDLE_URL.search(text), text)
        with self.assertRaises(ValueError):
            channels.lookup_youtube("not a channel")

    def test_remove_keeps_videos(self):
        cid = channels.create({"name": "To remove"}, 1)
        pid = prod(channel_id=cid)
        res = channels.remove(cid)
        self.assertEqual(res["videos_kept"], 1)
        self.assertIsNone(channels.get(cid))
        self.assertIsNone(db.q1("SELECT channel_id FROM productions WHERE id=?", (pid,))["channel_id"])


class SetupAndPublishTimeTests(unittest.TestCase):
    def test_auto_publish_is_scheduled_at_channel_time(self):
        # an opted-in channel past its first manual uploads, with a clean QA (the gate itself: test_ops)
        cid = channels.create({"name": "Evening", "publish_time": "18:00", "auto_publish": True,
                               "approve_first_n": 0}, 1)
        pid = prod(channel_id=cid, qa=json.dumps({"passed": True, "auto_publish_ok": True}))
        pipeline.approve(pid, privacy="public", publish_at=None, by="auto (channel auto-publish)")
        row = db.q1("SELECT status, privacy, publish_at FROM productions WHERE id=?", (pid,))
        self.assertEqual((row["status"], row["privacy"]), ("approved", "scheduled"))
        from datetime import datetime
        local = db.parse_iso(row["publish_at"]).astimezone()
        self.assertEqual((local.hour, local.minute), (18, 0))
        self.assertGreater(local, datetime.now().astimezone())

    def test_publish_time_validated(self):
        with self.assertRaises(ValueError):
            channels.create({"name": "Bad time", "publish_time": "25:99"}, 1)

    def test_wizard_checkbox_payloads(self):
        cid = channels.create({"name": "Wizard", "styles": {"kids": True, "comedy": True, "horror": False},
                               "schedule_days": {"0": True, "3": True, "6": False}}, 1)
        ch = channels.get(cid)
        self.assertEqual((sorted(ch["styles"]), ch["schedule_days"]), (["comedy", "kids"], [0, 3]))

    def test_checklist_and_pages(self):
        from fastapi.testclient import TestClient
        from studio.web import app as webapp
        cid = channels.create({"name": "Checklist", "niche": "kids", "styles": ["kids"], "language": "Hindi"}, 1)
        steps = {s["name"]: s["done"] for s in webapp.channel_checklist(channels.get(cid))}
        self.assertTrue(steps["Niche, style and language"])
        self.assertFalse(steps["Connect YouTube (one Google sign-in)"])
        if not db.q1("SELECT id FROM users WHERE email='setup@test.local'"):
            auth.approve(auth.register("setup@test.local", "a long enough password")["user_id"], 1)  # approval is on by default
        tok, _ = auth.login("setup@test.local", "a long enough password")
        c = TestClient(webapp.app, base_url=f"http://127.0.0.1:{config.PORT}", follow_redirects=False)
        c.cookies.set("sid", tok)
        c.headers["X-CSRF-Token"] = webapp.csrf_for_seed(tok)
        for path in ("/channels/new", "/welcome"):
            self.assertEqual(c.get(path).status_code, 200, path)
        r = c.post("/api/channels", json={"name": "From wizard", "niche": "cooking", "workflow": "factual",
                                           "styles": {"education": True}, "publish_time": "19:30"})
        self.assertTrue(r.json()["ok"], r.text)
        new = int(r.json()["next"].split("/")[2].split("?")[0])
        self.assertEqual(c.get(f"/channels/{new}?tab=setup").status_code, 200)

    def test_channel_art(self):
        from studio.creative import branding, characters
        from PIL import Image
        cid = channels.create({"name": "Art Kids", "made_for_kids": True, "styles": ["kids"]}, 1)
        characters.create(cid, "Hero", {"gender": "male", "age": "adult", "accessory": "cape_mask"}, role="hero")
        characters.create(cid, "Kid", {"gender": "female", "age": "child"}, role="friend")
        paths = branding.make_all(cid)
        self.assertEqual(Image.open(paths["banner"]).size, (2560, 1440))
        self.assertEqual(Image.open(paths["avatar"]).size, (800, 800))
        self.assertEqual(branding._cast(cid, 1)[0]["name"], "Kid")            # children first
