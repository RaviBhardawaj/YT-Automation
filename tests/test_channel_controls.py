"""Upload switch, no-upload videos, monetization progress and storage clean-up.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))

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
            auth.register("store-a@test.local", "a long enough password")
            auth.register("store-b@test.local", "a long enough password")
        tok_a, _ = auth.login("store-a@test.local", "a long enough password")
        tok_b, _ = auth.login("store-b@test.local", "a long enough password")
        ws_a = auth.session(tok_a)["workspace"]["id"]
        pid = prod(workspace_id=ws_a)
        c = TestClient(webapp.app, base_url=f"http://127.0.0.1:{config.PORT}", follow_redirects=False)
        c.headers["X-CSRF-Token"] = webapp.CSRF
        c.cookies.set("sid", tok_b)
        r = c.post("/api/storage/delete", json={"ids": [pid]})
        self.assertFalse(r.json()["ok"])
        self.assertEqual(c.post(f"/api/productions/{pid}/delete").status_code, 404)
        self.assertIsNotNone(db.q1("SELECT 1 FROM productions WHERE id=?", (pid,)))
        c.cookies.set("sid", tok_a)
        self.assertTrue(c.post(f"/api/productions/{pid}/delete").json()["ok"])
        self.assertIsNone(db.q1("SELECT 1 FROM productions WHERE id=?", (pid,)))
        self.assertEqual(c.get("/storage").status_code, 200)


if __name__ == "__main__":
    unittest.main()
