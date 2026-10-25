"""Platform tests: accounts, tenant isolation, uploads, limits, usage, downloads, publishing and a real edit render.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import auth, channels, pipeline, plans, uploads  # noqa: E402

auth.ensure_default_workspace()
channels.ensure_default()
BASE = f"http://127.0.0.1:{config.PORT}"


def make_video(path: Path, seconds: int = 4, audio: bool = True) -> Path:
    args = [config.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
            f"testsrc2=s=640x360:r=30:d={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:d={seconds}", "-shortest"]
    subprocess.run([*args, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


async def _chunks(data: bytes):
    for i in range(0, len(data), 65536):
        yield data[i:i + 65536]


def save(ws: int, name: str, data: bytes) -> dict:
    return asyncio.run(uploads.save_stream(ws, None, name, _chunks(data)))


def account(email: str) -> tuple[int, str]:
    if not db.q1("SELECT id FROM users WHERE email=?", (email,)):
        auth.approve(auth.register(email, "a long enough password")["user_id"], 1)  # approval is on by default
    token, u = auth.login(email, "a long enough password")
    return auth.session(token)["workspace"]["id"], token


def client(token: str):
    from fastapi.testclient import TestClient
    from studio.web import app as webapp
    c = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
    c.cookies.set("sid", token)
    c.headers["X-CSRF-Token"] = webapp.csrf_for_seed(token)
    return c


class AuthTests(unittest.TestCase):
    def test_passwords_hashed_and_checked(self):
        account("hash@test.local")
        row = db.q1("SELECT password_hash FROM users WHERE email='hash@test.local'")
        self.assertTrue(row["password_hash"].startswith("scrypt$"))
        self.assertNotIn("a long enough password", row["password_hash"])
        with self.assertRaises(auth.AuthError):
            auth.login("hash@test.local", "wrong password!!")

    def test_validation(self):
        with self.assertRaises(auth.AuthError):
            auth.register("not-an-email", "a long enough password")
        with self.assertRaises(auth.AuthError):
            auth.register("short@test.local", "short")

    def test_session_token_stored_hashed(self):
        _, token = account("sess@test.local")
        self.assertIsNone(db.q1("SELECT 1 FROM sessions WHERE token_hash=?", (token,)))
        self.assertIsNotNone(auth.session(token))
        auth.logout(token)
        self.assertIsNone(auth.session(token))

    def test_pages_need_sign_in(self):
        from fastapi.testclient import TestClient
        from studio.web import app as webapp
        c = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
        account("anyone@test.local")
        r = c.get("/productions")
        self.assertEqual(r.status_code, 303)
        self.assertIn("/login", r.headers["location"])
        self.assertEqual(c.get("/api/status").status_code, 401)


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.ws, _ = account("uploader@test.local")
        self.tmp = Path(tempfile.mkdtemp())

    def test_rejects_wrong_type_empty_and_fake_media(self):
        with self.assertRaises(uploads.UploadError):
            save(self.ws, "virus.exe", b"MZ....")
        with self.assertRaises(uploads.UploadError):
            save(self.ws, "empty.mp4", b"")
        with self.assertRaises(uploads.UploadError):
            save(self.ws, "fake.mp4", b"this is not a video" * 100)

    def test_size_limit_from_plan(self):
        with mock.patch.object(plans, "limits", return_value={**plans.PLANS["free"], "max_upload_mb": 0}):
            with self.assertRaises(uploads.UploadError):
                save(self.ws, "big.mp4", make_video(self.tmp / "a.mp4").read_bytes())

    def test_real_video_probed_readonly_dedup_and_intact(self):
        data = make_video(self.tmp / "b.mp4").read_bytes()
        m = save(self.ws, "clip one.mp4", data)
        self.assertEqual((m["kind"], m["width"], m["height"], m["has_audio"]), ("video", 640, 360, 1))
        self.assertAlmostEqual(m["duration"], 4.0, delta=0.2)
        self.assertFalse(os.access(m["path"], os.W_OK))
        again = save(self.ws, "same.mp4", data)
        self.assertEqual(again["id"], m["id"])
        self.assertTrue(uploads.verify_intact(uploads.get(m["id"], self.ws)))

    def test_other_workspace_cannot_read(self):
        m = save(self.ws, "private.mp4", make_video(self.tmp / "c.mp4", 2).read_bytes())
        other, _ = account("someone-else@test.local")
        self.assertIsNone(uploads.get(m["id"], other))


class IsolationTests(unittest.TestCase):
    def test_other_tenant_gets_404_everywhere(self):
        ws_a, tok_a = account("tenant-a@test.local")
        _, tok_b = account("tenant-b@test.local")
        cid = channels.create({"name": "A's channel", "workflow": "factual"}, ws_a)
        pid = db.insert("productions", {"format": "long", "title": "A secret", "status": "awaiting_approval",
                                        "stage": "review", "kind": "general", "workspace_id": ws_a, "channel_id": cid,
                                        "created_at": db.iso(), "updated_at": db.iso()})
        b = client(tok_b)
        for path in (f"/productions/{pid}", f"/channels/{cid}", f"/download/{pid}/video",
                     f"/media/productions/{pid:05d}/final.mp4"):
            self.assertEqual(b.get(path).status_code, 404, path)
        self.assertEqual(b.post(f"/api/productions/{pid}/publish", json={}).status_code, 404)
        self.assertNotIn("A secret", b.get("/productions").text)
        self.assertNotIn("A&#39;s channel", b.get("/channels").text)
        a = client(tok_a)
        self.assertEqual(a.get(f"/productions/{pid}").status_code, 200)

    def test_admin_pages_blocked_for_creators(self):
        _, tok = account("creator-x@test.local")
        c = client(tok)
        if db.q1("SELECT is_admin FROM users WHERE email='creator-x@test.local'")["is_admin"]:
            self.skipTest("first account in this test database is the admin")
        self.assertEqual(c.get("/settings").status_code, 403)
        self.assertEqual(c.get("/admin").status_code, 403)


class LimitsAndUsageTests(unittest.TestCase):
    def test_usage_recorded_once_per_step(self):
        ws, _ = account("meter@test.local")
        for _ in range(3):                                   # a retried step
            plans.record(ws, "render_minutes", 2.5, idem_key="render:999:0")
        self.assertEqual(plans.used(ws)["render_minutes"], 2.5)

    def test_monthly_video_limit_blocks_new_jobs(self):
        ws, _ = account("limited@test.local")
        cid = channels.create({"name": "Limited", "workflow": "factual"}, ws)
        with mock.patch.object(plans, "limits", return_value={**plans.PLANS["free"], "videos_per_month": 0}):
            from studio import jobs
            with self.assertRaises(plans.LimitExceeded):
                jobs.create(cid, "Explain compound interest")

    def test_channel_limit(self):
        ws, tok = account("chan-limit@test.local")
        with mock.patch.object(plans, "limits", return_value={**plans.PLANS["free"], "channels": 0}):
            r = client(tok).post("/api/channels", json={"name": "One too many"})
        self.assertFalse(r.json()["ok"])
        self.assertIn("limit", r.json()["error"].lower())


class DownloadAndPublishTests(unittest.TestCase):
    def test_no_download_for_missing_file_and_real_file_served(self):
        ws, tok = account("dl@test.local")
        pid = db.insert("productions", {"format": "long", "title": "Ghost", "status": "awaiting_approval",
                                        "stage": "review", "kind": "general", "workspace_id": ws,
                                        "video_path": str(config.DATA_DIR / "nope.mp4"),
                                        "created_at": db.iso(), "updated_at": db.iso()})
        c = client(tok)
        self.assertEqual(c.get(f"/download/{pid}/video").status_code, 404)
        page = c.get(f"/productions/{pid}").text
        self.assertNotIn(f"/download/{pid}/video", page)
        real = make_video(Path(tempfile.mkdtemp()) / "real.mp4", 1)
        pipeline.save(pid, video_path=str(real))
        r = c.get(f"/download/{pid}/video")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.content), real.stat().st_size)
        self.assertIn("attachment", r.headers["content-disposition"])

    def test_publish_refused_when_channel_not_connected(self):
        ws, tok = account("pub@test.local")
        cid = channels.create({"name": "Not connected"}, ws)
        real = make_video(Path(tempfile.mkdtemp()) / "v.mp4", 1)
        pid = db.insert("productions", {"format": "long", "title": "Ready one", "status": "awaiting_approval",
                                        "stage": "review", "kind": "general", "workspace_id": ws, "channel_id": cid,
                                        "video_path": str(real), "qa": '{"passed": true}', "package": "{}",
                                        "created_at": db.iso(), "updated_at": db.iso()})
        r = client(tok).post(f"/api/productions/{pid}/publish", json={"channel_id": cid, "privacy": "private"})
        self.assertFalse(r.json()["ok"])
        self.assertIn("isn't connected", r.json()["error"])
        self.assertEqual(db.q1("SELECT status FROM productions WHERE id=?", (pid,))["status"], "awaiting_approval")

    def test_disconnected_channel_does_not_affect_others(self):
        from studio.youtube import oauth
        ws, _ = account("multi@test.local")
        a = channels.create({"name": "Connected"}, ws)
        b = channels.create({"name": "Disconnected"}, ws)
        with mock.patch.object(oauth.settings, "has_secret", side_effect=lambda n: n == channels.refresh_token_secret(a)):
            self.assertTrue(oauth.connected(a))
            self.assertFalse(oauth.connected(b))


class EditEngineTests(unittest.TestCase):
    """A real render: upload → plan (rule-based, AI mocked out) → FFmpeg → outputs in the library."""

    def test_upload_edit_render_shorts_and_original_intact(self):
        from studio.editing import workflow
        ws, _ = account("editor@test.local")
        src = make_video(Path(tempfile.mkdtemp()) / "talk.mp4", 12)
        m = save(ws, "talk.mp4", src.read_bytes())
        db.update("media", m["id"], {"transcript": '[{"start": 1.0, "end": 3.0, "text": "hello world", '
                                                  '"words": [{"start": 1.0, "end": 1.5, "word": "hello"}, '
                                                  '{"start": 1.6, "end": 2.2, "word": "world"}]}]'})
        pid = workflow.create(ws, "Add subtitles and make 2 vertical Shorts", [m["id"]])
        with mock.patch("studio.ai.generate", side_effect=RuntimeError("AI offline in tests")):
            for st in workflow.STAGES:
                pipeline.save(pid, stage=st)
                workflow.STAGE_FN[st](pipeline.load(pid))
        p = pipeline.load(pid)
        self.assertEqual(p["package"]["planner"]["planner"], "rules")
        outs = p["episode"]["outputs"]
        self.assertEqual(len(outs), 3)                    # main edit + 2 Shorts
        self.assertTrue(all(o["passed"] for o in outs), [o["checks"] for o in outs])
        self.assertEqual((outs[1]["info"]["width"], outs[1]["info"]["height"]), (1080, 1920))
        kids = db.q("SELECT * FROM productions WHERE parent_id=? AND kind='edit_clip'", (pid,))
        self.assertEqual(len(kids), 2)
        for k in kids:
            self.assertTrue(Path(k["video_path"]).is_file())
            self.assertEqual(k["status"], "awaiting_approval")
            self.assertEqual(k["workspace_id"], ws)
        self.assertTrue(Path(p["video_path"]).is_file())
        self.assertNotEqual(Path(p["video_path"]).resolve(), Path(m["path"]).resolve())
        self.assertTrue(uploads.verify_intact(uploads.get(m["id"], ws)))
        self.assertGreater(plans.used(ws)["render_minutes"], 0)
        # re-running the render stage never re-renders finished outputs
        before = Path(p["video_path"]).stat().st_mtime
        workflow.STAGE_FN["render"](pipeline.load(pid))
        self.assertEqual(Path(p["video_path"]).stat().st_mtime, before)


if __name__ == "__main__":
    unittest.main()
