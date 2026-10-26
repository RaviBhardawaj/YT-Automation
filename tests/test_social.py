"""Facebook & Instagram publishing — Meta's Graph API is simulated, nothing is posted.
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

from studio import config, db, settings  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import auth, channels, worker  # noqa: E402
from studio.social import meta, publisher  # noqa: E402

auth.ensure_default_workspace()
channels.ensure_default()
TMP = Path(tempfile.mkdtemp())


def video(name: str, w: int, h: int, secs: int) -> Path:
    out = TMP / name
    subprocess.run([config.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=s={w}x{h}:r=24:d={secs}", "-f", "lavfi", "-i", f"sine=d={secs}", "-shortest",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)], check=True)
    return out


SHORT, LONG = video("short.mp4", 360, 640, 4), video("long.mp4", 640, 360, 5)


class Resp:
    def __init__(self, data, status=200):
        self.status_code, self._data, self.text = status, data, json.dumps(data)

    def json(self):
        return self._data


def connected_channel(ig: bool = True, **meta_extra) -> int:
    cid = channels.create({"name": "Social"}, 1)
    m = {"page_id": "P1", "page_name": "My Page", **({"ig_user_id": "IG1", "ig_username": "myig"} if ig else {}),
         **meta_extra}
    db.update("channels", cid, {"meta": json.dumps(m)})
    settings.set_secret(meta.token_secret(cid), "PAGE-TOKEN")
    return cid


def production(cid: int, path: Path, fmt: str = "short") -> int:
    return db.insert("productions", {"format": fmt, "title": "My video", "status": "awaiting_approval", "stage": "qa",
                                     "kind": "general", "workspace_id": 1, "channel_id": cid, "video_path": str(path),
                                     "qa": '{"passed": true}', "created_at": db.iso(), "updated_at": db.iso(),
                                     "package": json.dumps({"title": "My video #Shorts",
                                                            "final_description": "Watch https://youtu.be/abc #fun #Shorts"})})


class SignInTests(unittest.TestCase):
    def setUp(self):
        settings.set("meta.app_id", "123456")
        settings.set_secret("meta_app_secret", "APP-SECRET")

    def test_sign_in_lists_pages_and_choice_saves_page_token(self):
        cid = channels.create({"name": "Connect me"}, 1)
        url = meta.auth_url(cid)
        self.assertIn("dialog/oauth", url)
        self.assertIn("instagram_content_publish", url)
        state = url.split("state=")[1].split("&")[0]
        pages = {"data": [{"id": "P9", "name": "Kids Page", "access_token": "PT9", "tasks": ["CREATE_CONTENT"],
                           "instagram_business_account": {"id": "IG9", "username": "kidsig"}}]}
        with mock.patch("httpx.get", side_effect=[Resp({"access_token": "short"}), Resp({"access_token": "long"}),
                                                  Resp(pages)]):
            self.assertEqual(meta.handle_callback("CODE", state), cid)
        self.assertEqual(meta.pending_pages(cid)[0]["ig_username"], "kidsig")
        self.assertNotIn("token", json.dumps(meta.pending_pages(cid)))      # tokens never reach the page
        meta.choose_page(cid, "P9")
        self.assertTrue(meta.connected(cid))
        self.assertEqual(settings.secret(meta.token_secret(cid)), "PT9")
        self.assertEqual(channels.get(cid)["meta"]["ig_user_id"], "IG9")

    def test_unknown_state_is_refused(self):
        with self.assertRaises(meta.MetaError):
            meta.handle_callback("CODE", "not-a-real-state")


class RoutingAndCaptionTests(unittest.TestCase):
    def test_where_each_video_goes(self):
        cid = connected_channel()
        short, long = production(cid, SHORT), production(cid, LONG, "long")
        p_s = db.q1("SELECT * FROM productions WHERE id=?", (short,))
        p_l = db.q1("SELECT * FROM productions WHERE id=?", (long,))
        self.assertEqual(publisher.route(p_s, "facebook")[0], "reel")
        self.assertEqual(publisher.route(p_l, "facebook")[0], "video")
        self.assertEqual(publisher.route(p_l, "instagram")[0], "reel")
        no_ig = production(connected_channel(ig=False), SHORT)
        self.assertIn("no Instagram", publisher.route(db.q1("SELECT * FROM productions WHERE id=?", (no_ig,)),
                                                      "instagram")[1])
        channels.update(cid, {"uploads_enabled": False})
        self.assertIn("switched off", publisher.route(p_s, "facebook")[1])

    def test_caption_cleanup(self):
        p = db.q1("SELECT * FROM productions WHERE id=?", (production(connected_channel(), SHORT),))
        cap = publisher.caption(p, "instagram")
        self.assertNotIn("youtu.be", cap)
        self.assertNotIn("#Shorts", cap)
        self.assertIn("#fun", cap)
        many = {"title": "T", "final_description": " ".join(f"#t{i}" for i in range(40))}
        p2 = {**p, "package": json.dumps(many)}
        self.assertEqual(publisher.caption(p2, "instagram").count("#"), 30)


class PublishTests(unittest.TestCase):
    def test_instagram_reel_end_to_end_and_never_twice(self):
        cid = connected_channel()
        pid = production(cid, SHORT)
        with mock.patch.object(worker, "enqueue", return_value=1):
            self.assertIn("queued", publisher.request(pid, ["instagram"])["instagram"])
            self.assertIn("already", publisher.request(pid, ["instagram"])["instagram"])
        posts = [Resp({"id": "CONT1"}), Resp({"success": True}), Resp({"id": "MEDIA1"})]
        gets = [Resp({"status_code": "FINISHED"}), Resp({"permalink": "https://instagram.com/reel/x"})]
        with mock.patch("httpx.post", side_effect=posts) as post, mock.patch("httpx.get", side_effect=gets):
            res = publisher.run(pid, "instagram")
        self.assertEqual(res["url"], "https://instagram.com/reel/x")
        first = post.call_args_list[0]
        self.assertEqual((first.kwargs["data"]["media_type"], first.kwargs["data"]["upload_type"]), ("REELS", "resumable"))
        self.assertIn("rupload.facebook.com/ig-api-upload", post.call_args_list[1].args[0])
        self.assertEqual(post.call_args_list[1].kwargs["headers"]["Authorization"], "OAuth PAGE-TOKEN")
        row = publisher.status(pid)["instagram"]
        self.assertEqual((row["status"], row["remote_id"]), ("published", "MEDIA1"))
        with mock.patch("httpx.post") as again:
            self.assertIn("skipped", publisher.run(pid, "instagram"))
        again.assert_not_called()

    def test_retry_resumes_container_instead_of_posting_again(self):
        cid = connected_channel()
        pid = production(cid, SHORT)
        with mock.patch.object(worker, "enqueue", return_value=1):
            publisher.request(pid, ["instagram"])
        with mock.patch("httpx.post", side_effect=[Resp({"id": "CONT2"}), Resp({"success": True}),
                                                   Resp({"error": {"message": "temporary"}}, 500)]), \
                mock.patch("httpx.get", side_effect=[Resp({"status_code": "FINISHED"})]):
            with self.assertRaises(meta.MetaError):
                publisher.run(pid, "instagram")
        self.assertEqual(publisher.status(pid)["instagram"]["container_id"], "CONT2")
        with mock.patch.object(worker, "enqueue", return_value=1):
            publisher.request(pid, ["instagram"])                                 # retry after failure
        with mock.patch("httpx.post", side_effect=[Resp({"id": "MEDIA2"})]) as post, \
                mock.patch("httpx.get", side_effect=[Resp({"status_code": "FINISHED"}), Resp({"permalink": "u"})]):
            publisher.run(pid, "instagram")
        self.assertIn("media_publish", post.call_args_list[0].args[0])          # no new container, no re-upload
        self.assertEqual(post.call_count, 1)

    def test_facebook_reel_and_page_video(self):
        cid = connected_channel()
        short, long = production(cid, SHORT), production(cid, LONG, "long")
        settings.set("meta.app_id", "123456")
        with mock.patch.object(worker, "enqueue", return_value=1):
            publisher.request(short, ["facebook"])
            publisher.request(long, ["facebook"])
        with mock.patch("httpx.post", side_effect=[Resp({"video_id": "V1", "upload_url": "https://rupload.facebook.com/video-upload/v26.0/V1"}),
                                                   Resp({"success": True}), Resp({"success": True})]) as post:
            res = publisher.run(short, "facebook")
        self.assertEqual(res["id"], "V1")
        self.assertEqual(post.call_args_list[2].kwargs["data"]["video_state"], "PUBLISHED")
        self.assertEqual(post.call_args_list[2].kwargs["data"]["is_ai_generated"], "true")
        with mock.patch("httpx.post", side_effect=[Resp({"id": "upload:ABC"}), Resp({"h": "HANDLE"}),
                                                   Resp({"id": "FBV1"})]) as post:
            res = publisher.run(long, "facebook")
        self.assertEqual(res["id"], "FBV1")
        self.assertIn("graph-video.facebook.com", post.call_args_list[2].args[0])
        self.assertEqual(post.call_args_list[2].kwargs["data"]["fbuploader_video_file_chunk"], "HANDLE")

    def test_auto_publish_follows_channel_switches(self):
        cid = connected_channel(auto_facebook=True, auto_instagram=False)
        pid = production(cid, SHORT)
        with mock.patch.object(worker, "enqueue", return_value=1) as enq:
            out = publisher.maybe_auto(pid)
        self.assertEqual(list(out), ["facebook"])
        self.assertEqual(enq.call_args.args[1]["platform"], "facebook")


class PagesTests(unittest.TestCase):
    def test_pages_render(self):
        from fastapi.testclient import TestClient
        from studio.web import app as webapp
        admin = db.q1("SELECT email FROM users WHERE is_admin=1 ORDER BY id LIMIT 1")
        if not admin:
            auth.register("social-admin@test.local", "a long enough password")
            admin = {"email": "social-admin@test.local"}
        if admin["email"] != "social-admin@test.local":
            self.skipTest("another module created the platform admin")
        tok, _ = auth.login(admin["email"], "a long enough password")
        c = TestClient(webapp.app, base_url=f"http://127.0.0.1:{config.PORT}", follow_redirects=False)
        c.cookies.set("sid", tok)
        cid = connected_channel()
        pid = production(cid, SHORT)
        for path in (f"/channels/{cid}?tab=social", "/settings?tab=facebook", f"/productions/{pid}", "/publishing"):
            r = c.get(path)
            self.assertEqual(r.status_code, 200, f"{path}: {r.text[:200]}")
        self.assertIn("Publish to Facebook", c.get(f"/productions/{pid}").text)
