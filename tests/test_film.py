"""Free photo-real film: the user's screenplay is used as written, characters keep their own looks and voices,
and the whole film is made end to end (image generator, voices and AI mocked — no free allowance spent).
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import auth, channels, jobs, pipeline, worker  # noqa: E402
from studio.creative import film, script_import  # noqa: E402

auth.ensure_default_workspace()
channels.ensure_default()


def studio_on(ws: int = 1) -> None:
    """The Film Studio is opt-in: switch it on and grant it to the account for these tests."""
    from studio import settings
    settings.set("film_studio.enabled", True)
    perms = db.loads(db.q1("SELECT permissions FROM workspaces WHERE id=?", (ws,))["permissions"], {}) or {}
    db.ex("UPDATE workspaces SET permissions=? WHERE id=?", (json.dumps({**perms, "film_studio": True}), ws))

SCRIPT = """TEST FILM
Title: Chai Pe Charcha
Visual style: Photorealistic live-action cinematic realism.
CHARACTER 1: RAMU — THE FARMER
Age: 45.
Appearance: Thin, grey moustache.
Clothing: White kurta.
Voice: Deep, mature male voice.
CHARACTER 2: SHAMU — THE JOKER
Age: 40.
Appearance: Round face.
Clothing: Blue shirt.
4. ENVIRONMENT AND SET DESIGN
* A village courtyard with a neem tree.
5. COMPLETE SCREENPLAY
SCENE 1 — CHAI TIME
Two friends sit at a table.
RAMU:
"Oye, chai thandi ho gayi!"
SHAMU:
"Toh garam kar le, bhai."
Everyone laughs.
RAMU:
"Tu hi kar de."
SCENE 2 — THE END
SHAMU, WHISPERING:
"Main toh chala."
RAMU:
"Ruk ja, ek aur cup!"
END TITLE:
"CHAI PE CHARCHA"
"""


def fake_image(prompt, fmt, out, look="real", *, refs=None, size=None):
    Image.new("RGB", size or (1024, 576), (90, 120, 60)).save(out)
    return None


def fake_tts(text, engine, voice_id, lang, speed, allow_fallback=True):
    t = np.arange(int(24000 * 0.8)) / 24000
    return (np.sin(2 * np.pi * 180 * t) * 6000).astype(np.int16), 24000


def fake_devanagari(lines):
    return ["देवनागरी " + x for x in lines]


class FilmTests(unittest.TestCase):
    def test_screenplay_is_read_as_written(self):
        f = script_import.parse(SCRIPT)
        self.assertEqual(f["title"], "Chai Pe Charcha")
        self.assertEqual([c["name"] for c in f["cast"]], ["Ramu", "Shamu"])
        self.assertIn("grey moustache", f["cast"][0]["look"])
        self.assertEqual(len(f["scenes"]), 2)
        self.assertEqual(f["scenes"][0]["lines"][0]["text"], "Oye, chai thandi ho gayi!")
        self.assertEqual(f["scenes"][1]["lines"][0]["manner"], "whispering")
        self.assertTrue(script_import.looks_like_screenplay(SCRIPT))
        self.assertTrue(film.wants_film(SCRIPT))
        self.assertFalse(film.wants_film("A funny cartoon about a crow"))

    def test_realistic_script_becomes_a_film_with_no_channel(self):
        ws = 1
        studio_on(ws)
        holder = channels.holder(ws)
        with mock.patch.object(worker, "enqueue", return_value=1):
            res = jobs.create(holder, SCRIPT, no_upload=True)
        p = db.q1("SELECT kind, channel_id, request FROM productions WHERE id=?", (res["production_ids"][0],))
        self.assertEqual(p["kind"], "film")
        self.assertEqual(p["channel_id"], holder)
        self.assertIn("Ruk ja, ek aur cup!", json.loads(p["request"])["request"])     # the whole script is kept

    def test_whole_film_is_made_end_to_end(self):
        studio_on(1)
        from studio import settings
        settings.set("film_studio.auto_approve_refs", True)       # unattended run: no cast review
        holder = channels.holder(1)
        with mock.patch.object(worker, "enqueue", return_value=1):
            pid = jobs.create(holder, SCRIPT, no_upload=True)["production_ids"][0]
        with mock.patch("studio.media.visuals.generate_cloudflare", fake_image), \
                mock.patch("studio.film_studio.providers.health", lambda pid: {"available": pid == "cloudflare_flux",
                                                                               "detail": "test"}), \
                mock.patch("studio.media.tts._synth_sentence", fake_tts), \
                mock.patch("studio.media.tts.kokoro_installed", lambda: True), \
                mock.patch.object(film, "_devanagari", fake_devanagari):
            pipeline.run_production({"production_id": pid}, {})
        p = pipeline.load(pid)
        self.assertEqual(p["status"], "awaiting_approval", p.get("error"))
        self.assertTrue(Path(p["video_path"]).is_file())
        self.assertTrue(p["qa"]["passed"])
        voices = {c["key"]: (c["voice"]["voice_id"], c["voice"]["pitch"]) for c in p["package"]["cast"]}
        self.assertEqual(len(set(voices.values())), 2)                          # each character sounds different
        self.assertLess(voices["F1"][1], 1.0)                                    # "deep" voice note → lower pitch
        self.assertIn("Oye, chai thandi ho gayi!", Path(p["srt_path"]).read_text())
        settings.set("film_studio.auto_approve_refs", False)
        from fastapi.testclient import TestClient
        from studio.web import app as webapp
        pw = "a long enough password"
        if not auth.has_users():
            auth.register("first-admin@test.local", pw)
        owner = db.q1("SELECT u.email FROM users u JOIN workspace_members m ON m.user_id=u.id WHERE m.workspace_id=1 "
                      "AND m.role='owner'")
        tok, _ = auth.login(owner["email"], pw)
        c = TestClient(webapp.app, base_url=f"http://127.0.0.1:{config.PORT}", raise_server_exceptions=True)
        c.cookies.set("sid", tok)
        page = c.get(f"/productions/{pid}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Chai Pe Charcha", page.text)
        self.assertIn("Choose a channel", page.text)                             # decide where to publish later

    def test_waits_for_tomorrow_instead_of_failing_when_free_images_run_out(self):
        from studio.media import visuals

        def used_up(*a, **k):
            raise visuals.AllowanceUsedUp("used up")
        with mock.patch("studio.media.visuals.generate_cloudflare", used_up), \
                mock.patch("studio.film_studio.providers.health", lambda pid: {"available": pid == "cloudflare_flux",
                                                                               "detail": "test"}):
            with self.assertRaises(film.WaitForAllowance) as cm:
                film._gen("x", Path(tempfile.mkdtemp()) / "a.png")
        self.assertTrue(cm.exception.temporary)
        self.assertGreater(cm.exception.retry_after, 0)


if __name__ == "__main__":
    unittest.main()
