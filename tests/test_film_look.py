"""Each video keeps its own channel's style (kids cartoon vs. real footage), stand-alone videos use a hidden holder,
and the free FLUX generator stays inside its daily allowance.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import auth, channels, pipeline, plans, settings  # noqa: E402
from studio.media import render, visuals  # noqa: E402

auth.ensure_default_workspace()
channels.ensure_default()


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (1344, 768), (40, 90, 160)).save(buf, "PNG")
    return buf.getvalue()


class LookTests(unittest.TestCase):
    def test_kids_channel_is_cartoon_market_and_standalone_are_real(self):
        kids = channels.create({"name": "Kids look", "workflow": "animated_story", "made_for_kids": True}, 1)
        market = channels.create({"name": "Market look", "workflow": "factual", "niche": "stock market"}, 1)
        self.assertEqual(pipeline.video_look({"channel_id": kids, "request": "{}"}), "cartoon")
        self.assertEqual(pipeline.video_look({"channel_id": market, "request": "{}"}), "real")
        standalone = {"channel_id": kids, "request": json.dumps({"no_upload": True, "content_type": "education"})}
        self.assertEqual(pipeline.video_look(standalone), "real")

    def test_cartoon_look_never_uses_photo_only_sources(self):
        called = []

        def fake(name):
            def f(query, kind, fmt, *a):
                called.append(name)
                return []
            return f
        scene = {"visual_type": "photo", "visual_query": "happy elephant", "visual_description": "An elephant",
                 "narration": "x", "on_screen_text": "Elephant"}
        with mock.patch.dict(visuals.SEARCHERS, {k: fake(k) for k in visuals.SEARCHERS}):
            visuals.choose(scene, 0, "long", set(), Path(tempfile.mkdtemp()), "t",
                           providers=["pexels", "pixabay", "openverse", "wikimedia"], look="cartoon")
        self.assertEqual(set(called), {"pixabay"})

    def test_grade_differs_by_look_and_can_be_switched_off(self):
        self.assertIn("noise", render._grade("real", "video"))
        self.assertNotIn("noise", render._grade("cartoon", "video"))
        self.assertEqual(render._grade("real", "anim"), "")
        settings.set("render.film_look", "off")
        try:
            self.assertEqual(render._grade("real", "video"), "")
        finally:
            settings.set("render.film_look", "auto")


class HolderTests(unittest.TestCase):
    def test_holder_is_hidden_download_only_and_not_counted(self):
        ws = db.insert("workspaces", {"name": "holder ws", "plan": "free", "created_at": db.iso()})
        before = plans.used(ws)["channels"]
        h = channels.holder(ws)
        ch = channels.get(h)
        self.assertEqual(ch["status"], "hidden")
        self.assertFalse(channels.uploads_allowed(ch))
        self.assertNotIn(h, [c["id"] for c in channels.all_channels(ws)])
        self.assertNotIn(h, [c["id"] for c in channels.all_channels()])
        self.assertEqual(plans.used(ws)["channels"], before)
        self.assertEqual(channels.holder(ws), h)


class DecideLaterTests(unittest.TestCase):
    def test_create_defaults_to_no_channel_and_publish_needs_a_real_channel(self):
        from fastapi.testclient import TestClient
        from studio.web import app as webapp
        pw = "a long enough password"
        if not auth.has_users():
            auth.register("first-admin@test.local", pw)
        if not db.q1("SELECT id FROM users WHERE email='later@test.local'"):
            auth.approve(auth.register("later@test.local", pw)["user_id"], 1)
        tok, _ = auth.login("later@test.local", pw)
        ws = auth.session(tok)["workspace"]["id"]
        c = TestClient(webapp.app, base_url=f"http://127.0.0.1:{config.PORT}", raise_server_exceptions=False)
        c.cookies.set("sid", tok)
        c.headers["X-CSRF-Token"] = webapp.csrf_for_seed(tok)
        page = c.get("/create").text
        self.assertIn("No channel yet", page)
        self.assertRegex(page, r'value="none" selected')
        vid = Path(tempfile.mkdtemp()) / "v.mp4"
        vid.write_bytes(b"x")
        pid = db.insert("productions", {"format": "long", "title": "Stand-alone", "status": "awaiting_approval",
                                        "stage": "review", "kind": "general", "workspace_id": ws,
                                        "channel_id": channels.holder(ws), "video_path": str(vid), "package": "{}",
                                        "qa": json.dumps({"passed": True}), "created_at": db.iso(),
                                        "updated_at": db.iso()})
        r = c.post(f"/api/productions/{pid}/publish", json={"privacy": "private"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Pick one of your channels", r.text)


class FluxTests(unittest.TestCase):
    def test_free_generator_uses_channel_style_and_respects_daily_allowance(self):
        settings.set("visuals.cloudflare_account_id", "acct")
        sent = {}

        class R:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return {"result": {"image": base64.b64encode(png()).decode()}}

            @staticmethod
            def raise_for_status():
                return None

        def post(self, url, files=None, headers=None, timeout=None):
            sent["prompt"] = files["prompt"][1]
            return R()
        out = Path(tempfile.mkdtemp()) / "g.png"
        with mock.patch.object(settings, "has_secret", lambda n: True), \
                mock.patch.object(settings, "secret", lambda n: "tok"), mock.patch("httpx.Client.post", post):
            c = visuals.generate_cloudflare("a doctor checks a foot", "long", out, look="cartoon")
            self.assertIn("cartoon", sent["prompt"])
            self.assertEqual(c.provider, "cloudflare_image")
            db.insert("ai_usage", {"provider": "cloudflare", "model": "m", "task": "x", "input_tokens": 0,
                                   "output_tokens": 9800, "cost_usd": 0, "ok": 1, "error": "", "created_at": db.iso()})
            with self.assertRaises(Exception):
                visuals.generate_cloudflare("another", "long", out, look="real")
        settings.set("visuals.cloudflare_account_id", "")

    def test_free_generator_is_tried_before_paid_ones(self):
        order = visuals.provider_order({"visual_type": "photo"})
        if "gemini_image" in order:
            self.assertLess(order.index("cloudflare_image"), order.index("gemini_image"))
        self.assertIn("cloudflare_image", order)


if __name__ == "__main__":
    unittest.main()
