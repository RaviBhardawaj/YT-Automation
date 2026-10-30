"""Film Studio phase 5: provider registry, free-first routing and the ₹0 guard (no network, no paid calls).
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import auth, channels, pipeline, settings  # noqa: E402
from studio.creative import film  # noqa: E402
from studio.film_studio import providers as pv  # noqa: E402
from studio.media import visuals  # noqa: E402

auth.ensure_default_workspace()
channels.ensure_default()


def healthy(*ids):
    """Patch health so exactly these providers report available."""
    return mock.patch.object(pv, "health", lambda pid: {"available": pid in ids, "detail": "test"})


class RegistryTests(unittest.TestCase):
    def test_every_provider_documents_what_it_is(self):
        need = {"id", "name", "caps", "where", "model", "cost", "checked", "licence", "commercial", "hardware",
                "limits", "est_cost_usd", "fallback", "health"}
        for p in pv.REGISTRY:
            self.assertLessEqual(need, set(p), p["id"])
            self.assertIn(p["cost"], ("free_local", "free_allowance", "paid"))
            self.assertTrue(set(p["caps"]) <= set(pv.CAPS), p["id"])
            if p["cost"] == "free_allowance":
                self.assertTrue(p.get("allowance"), f"{p['id']} must say what the free allowance is")

    def test_status_page_data_never_calls_paid_services(self):
        with mock.patch("httpx.get", side_effect=AssertionError("network")), \
                mock.patch("httpx.post", side_effect=AssertionError("network")):
            rows = pv.status_all()
        self.assertEqual(len(rows), len(pv.REGISTRY))


class RoutingTests(unittest.TestCase):
    def tearDown(self):
        settings.set("film_studio.budget_inr", 0)
        settings.set("film_studio.paid_providers", [])

    def test_free_local_first_then_free_allowance(self):
        with healthy("cloudflare_flux", "local_sd"):
            self.assertEqual(pv.choose("image")["provider"], "local_sd")
        with healthy("cloudflare_flux"):
            self.assertEqual(pv.choose("image_ref")["provider"], "cloudflare_flux")

    def test_paid_never_used_at_zero_budget_even_with_a_key(self):
        with healthy("gemini_image", "gemini_omni", "ffmpeg_camera"):
            with self.assertRaises(pv.NoFreeProvider):
                pv.choose("image_ref")
            v = pv.choose("video_i2v")
        self.assertNotEqual(v["provider"], "gemini_omni")
        with self.assertRaises(pv.PaidBlocked):
            pv.guard("gemini_omni")

    def test_paid_needs_budget_and_explicit_listing(self):
        settings.set("film_studio.budget_inr", 500)
        with healthy("gemini_image"):
            with self.assertRaises(pv.NoFreeProvider):
                pv.choose("image_ref")                       # budget alone is not enough
            settings.set("film_studio.paid_providers", ["gemini_image"])
            self.assertEqual(pv.choose("image_ref")["provider"], "gemini_image")
        pv.guard("gemini_image")

    def test_no_free_video_becomes_a_labelled_camera_move(self):
        with healthy("ffmpeg_camera"):
            c = pv.choose("video_i2v")
        self.assertEqual(c["provider"], "ffmpeg_camera")
        self.assertEqual(c["motion_type"], "camera_move")
        self.assertIn("not as video", c["limitation"])
        with healthy("ffmpeg_camera", "hf_zerogpu"):         # a provider this app can really run
            self.assertEqual(pv.choose("video_i2v")["motion_type"], "video")
        with healthy("ffmpeg_camera", "wan22_local"):        # listed for information only: never chosen
            self.assertEqual(pv.choose("video_i2v")["motion_type"], "camera_move")


class TruthfulnessTests(unittest.TestCase):
    def test_providers_without_an_adapter_are_never_chosen_even_when_healthy(self):
        no_adapter = [p for p in pv.REGISTRY if not p["adapter"]]
        self.assertTrue(no_adapter)
        healthy = {p["id"]: (lambda: (True, "pretend ready")) for p in no_adapter}
        with mock.patch.dict(pv.BY_ID, {i: {**pv.BY_ID[i], "health": f} for i, f in healthy.items()}), \
                mock.patch.object(pv, "REGISTRY", [{**p, "health": healthy.get(p["id"], p["health"])}
                                                   for p in pv.REGISTRY]):
            for cap in pv.CAPS:
                for c in pv.candidates(cap):
                    self.assertTrue(pv.BY_ID[c["provider"]]["adapter"], (cap, c))
            self.assertFalse(any(not s["adapter"] and s["usable_now"] for s in pv.status_all()))

    def test_talking_clips_try_the_free_gpu_then_this_mac(self):
        with mock.patch.dict(pv.BY_ID["hf_ltx2"], health=lambda: (True, "ok")), \
                mock.patch.dict(pv.BY_ID["sadtalker_local"], health=lambda: (True, "ok")):
            self.assertEqual([c["provider"] for c in pv.candidates("dialogue_video")], ["hf_ltx2", "sadtalker_local"])

    def test_failed_video_tries_are_counted_apart_from_clips_made(self):
        for ok in (1, 0, 0):
            db.insert("ai_usage", {"provider": "hf_zerogpu", "model": "m", "task": "video_shot", "input_tokens": 0,
                                   "output_tokens": 0, "cost_usd": 0.0, "ok": ok, "error": "", "created_at": db.iso()})
        b = pv.video_budget()
        self.assertGreaterEqual(b["attempts"] - b["used"], 2)
        self.assertEqual(b["failed"], b["attempts"] - b["used"])


class FilmImageRoutingTests(unittest.TestCase):
    def test_film_images_go_through_the_manager(self):
        out = Path(tempfile.mkdtemp()) / "a.png"
        with healthy():
            with self.assertRaises(film.WaitForAllowance) as w:
                film._gen("x", out)                          # every free allowance spent: wait, never fail
        self.assertLessEqual(w.exception.retry_after, 3600)  # and look again within the hour
        called = {}
        with healthy("cloudflare_flux"), mock.patch("studio.media.visuals.generate_cloudflare",
                                                    lambda *a, **k: called.setdefault("cf", True)):
            film._gen("x", out, refs=[out])
        self.assertTrue(called.get("cf"))


    def test_spent_cloudflare_moves_on_to_the_free_flux_space(self):
        out = Path(tempfile.mkdtemp()) / "b.png"
        calls = []

        def spent(*a, **k):
            calls.append("cf")
            raise visuals.AllowanceUsedUp("used up")

        def flux(prompt, fmt, path, look="real", **k):
            calls.append(("hf", k.get("refs")))
            path.write_bytes(b"png")
        with healthy("cloudflare_flux", "hf_flux"), mock.patch.object(visuals, "generate_cloudflare", spent), \
                mock.patch.object(visuals, "generate_hf_flux", flux):
            self.assertEqual(film._gen("x", out, refs=[out]), out)   # a cast shot still gets made
        self.assertEqual(calls, ["cf", ("hf", None)])

    def test_flux_space_rests_an_hour_after_a_quota_answer(self):
        db.insert("ai_usage", {"provider": "hf_flux", "model": "m", "task": "t", "input_tokens": 0, "output_tokens": 0,
                               "cost_usd": 0.0, "ok": 0, "error": "ZeroGPU quota exceeded", "created_at": db.iso()})
        self.assertFalse(pv.health("hf_flux")["available"])
        db.insert("ai_usage", {"provider": "hf_flux", "model": "m", "task": "t", "input_tokens": 0, "output_tokens": 1,
                               "cost_usd": 0.0, "ok": 1, "error": "", "created_at": db.iso()})
        self.assertTrue(pv.health("hf_flux")["available"])


    def test_local_flux_is_the_never_stop_floor_after_the_online_allowances(self):
        with healthy("cloudflare_flux", "hf_flux", "local_flux"):
            self.assertEqual([p["provider"] for p in pv.candidates("image")],
                             ["cloudflare_flux", "hf_flux", "local_flux"])
        out = Path(tempfile.mkdtemp()) / "c.png"
        calls = []

        def spent(*a, **k):
            calls.append("spent")
            raise visuals.AllowanceUsedUp("used up")

        def local(prompt, fmt, path, look="real", **k):
            calls.append("local")
            path.write_bytes(b"png")
        with healthy("cloudflare_flux", "hf_flux", "local_flux"), \
                mock.patch.object(visuals, "generate_cloudflare", spent), \
                mock.patch.object(visuals, "generate_hf_flux", spent), \
                mock.patch.object(visuals, "generate_local_flux", local):
            self.assertEqual(film._gen("x", out), out)
        self.assertEqual(calls, ["spent", "spent", "local"])
        order = visuals.provider_order({"visual_type": "photo"})
        self.assertEqual(order.index("local_flux"), order.index("hf_flux") + 1)


class SettingsPageTests(unittest.TestCase):
    def test_film_settings_tab_lists_providers(self):
        from fastapi.testclient import TestClient
        from studio.web import app as webapp
        pw = "a long enough password"
        if not auth.has_users():
            auth.register("first-admin@test.local", pw)
        admin = db.q1("SELECT email FROM users WHERE is_admin=1 ORDER BY id LIMIT 1")["email"]
        tok, _ = auth.login(admin, pw)
        c = TestClient(webapp.app, base_url=f"http://127.0.0.1:{config.PORT}", raise_server_exceptions=True)
        c.cookies.set("sid", tok)
        r = c.get("/settings?tab=film")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Cloudflare Workers AI", r.text)
        self.assertIn("free allowance", r.text)
        self.assertIn("paid", r.text)


if __name__ == "__main__":
    unittest.main()
