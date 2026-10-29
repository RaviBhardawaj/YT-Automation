"""Film Studio phase 4: character bible, proposed/approved reference pictures, cast review that pauses and resumes.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

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
from studio import auth, channels, pipeline, settings, worker  # noqa: E402
from studio.creative import film, script_import  # noqa: E402
from studio.film_studio import casting, store  # noqa: E402

auth.ensure_default_workspace()
channels.ensure_default()
SCRIPT = """Title: Two Friends
CHARACTER 1: RAMU — THE FARMER
Age: 45.
Appearance: Thin, grey moustache.
Clothing: White kurta, green gamcha.
Personality: Calm.
CHARACTER 2: SHAMU — THE JOKER
Age: 40.
Appearance: Round face.
Clothing: Blue shirt.
4. ENVIRONMENT AND SET DESIGN
* A village courtyard with a neem tree.
SCENE 1 — CHAI
RAMU:
"Chai?"
SHAMU:
"Haan!"
SCENE 2 — BYE
RAMU:
"Chal."
SHAMU:
"Theek hai."
"""
PROMPTS: list[str] = []


def fake_gen(prompt, out, *, refs=None, size=(1024, 576)):
    PROMPTS.append(prompt)
    Image.new("RGB", size, (40, 80, 120)).save(out)
    return out


def production(ws=1):
    f = script_import.parse(SCRIPT)
    pid = db.insert("productions", {"format": "long", "title": "Two Friends", "status": "queued", "stage": "cast",
                                    "kind": "film", "workspace_id": ws, "package": db.dumps(f), "created_at": db.iso(),
                                    "updated_at": db.iso()})
    return pid, f


class CastingTests(unittest.TestCase):
    def setUp(self):
        settings.set("film_studio.auto_approve_refs", False)

    def test_bible_comes_from_the_script_and_drives_the_portrait(self):
        pid, f = production()
        self.assertEqual(casting.propose(pid, 1, f), 3)                  # 2 characters + location
        self.assertEqual(casting.propose(pid, 1, f), 0)                  # only once
        refs = casting.latest(pid, 1)
        ramu = refs[("character", "F1")]
        self.assertIn("grey moustache", ramu["meta"]["bible"]["appearance"])
        self.assertIn("green gamcha", ramu["meta"]["bible"]["clothing"])
        PROMPTS.clear()
        casting.generate(ramu["id"], 1, Path(tempfile.mkdtemp()), fake_gen)
        self.assertIn("grey moustache", PROMPTS[-1])
        self.assertIn("White kurta", PROMPTS[-1])

    def test_film_waits_for_approval_then_continues_with_approved_pictures(self):
        pid, _ = production()
        with mock.patch.object(film, "_gen", fake_gen):
            film.stage_cast(pipeline.load(pid))
        p = pipeline.load(pid)
        self.assertEqual(p["status"], "needs_review")
        self.assertIn("Approve the cast", p["error"])
        self.assertEqual(len(casting.missing(pid, 1)), 3)
        for r in casting.latest(pid, 1).values():
            store.set_ref_status(r["id"], 1, "approved")
        self.assertEqual(casting.missing(pid, 1), [])
        with mock.patch.object(film, "_gen", fake_gen):
            film.stage_cast(pipeline.load(pid))
        pkg = pipeline.load(pid)["package"]
        self.assertTrue(all(Path(c["portrait"]).exists() for c in pkg["cast"]))
        self.assertTrue(Path(pkg["location_ref"]).exists())

    def test_regenerating_keeps_the_approved_version_until_a_new_approval(self):
        pid, f = production()
        casting.propose(pid, 1, f)
        work = Path(tempfile.mkdtemp())
        ramu = casting.latest(pid, 1)[("character", "F1")]
        casting.generate(ramu["id"], 1, work, fake_gen)
        store.set_ref_status(ramu["id"], 1, "approved")
        v2 = casting.new_version(ramu["id"], 1, notes="older", bible={"age": "50", "hacker": "x"})
        casting.generate(v2, 1, work, fake_gen)
        latest = casting.latest(pid, 1)[("character", "F1")]
        self.assertEqual(latest["id"], v2)
        self.assertEqual(latest["approved"]["id"], ramu["id"])          # still the approved one
        self.assertEqual(latest["meta"]["bible"]["age"], "50")
        self.assertNotIn("hacker", latest["meta"]["bible"])             # only bible fields can be edited
        film_pkg = {"cast": [{"key": "F1"}, {"key": "F2"}]}
        casting.apply_to_film(pid, 1, film_pkg)
        self.assertEqual(film_pkg["cast"][0]["portrait"], ramu["file_path"] or
                         db.q1("SELECT file_path FROM film_refs WHERE id=?", (ramu["id"],))["file_path"])

    def test_auto_approve_for_unattended_runs(self):
        settings.set("film_studio.auto_approve_refs", True)
        try:
            pid, _ = production()
            with mock.patch.object(film, "_gen", fake_gen):
                film.stage_cast(pipeline.load(pid))
            self.assertEqual(casting.missing(pid, 1), [])
            self.assertNotEqual(pipeline.load(pid)["status"], "needs_review")
        finally:
            settings.set("film_studio.auto_approve_refs", False)


class CastPageTests(unittest.TestCase):
    def test_review_page_actions_resume_filming_and_are_owner_only(self):
        from fastapi.testclient import TestClient
        from studio.web import app as webapp
        pw = "a long enough password"
        if not auth.has_users():
            auth.register("first-admin@test.local", pw)
        for email in ("cast-owner@test.local", "cast-other@test.local"):
            if not db.q1("SELECT id FROM users WHERE email=?", (email,)):
                auth.approve(auth.register(email, pw)["user_id"], 1)
        tok, _ = auth.login("cast-owner@test.local", pw)
        other, _ = auth.login("cast-other@test.local", pw)
        ws = auth.session(tok)["workspace"]["id"]
        pid, _ = production(ws)
        with mock.patch.object(film, "_gen", fake_gen):
            film.stage_cast(pipeline.load(pid))
        c = TestClient(webapp.app, base_url=f"http://127.0.0.1:{config.PORT}", raise_server_exceptions=False)
        c.cookies.set("sid", tok)
        c.headers["X-CSRF-Token"] = webapp.csrf_for_seed(tok)
        page = c.get(f"/productions/{pid}/cast")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Ramu", page.text)
        self.assertIn("waiting for 3", page.text)
        refs = list(casting.latest(pid, ws).values())
        with mock.patch.object(worker, "enqueue", return_value=1) as enq:
            for r in refs:
                self.assertEqual(c.post(f"/api/productions/{pid}/cast/{r['id']}/approve").status_code, 200)
            self.assertTrue(any(call.args[0] == "production" for call in enq.call_args_list))
            r = c.post(f"/api/productions/{pid}/cast/{refs[0]['id']}/regenerate", json={"notes": "smile"})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(any(call.args[0] == "film_ref" for call in enq.call_args_list))
        self.assertEqual(pipeline.load(pid)["status"], "queued")
        c.cookies.set("sid", other)
        c.headers["X-CSRF-Token"] = webapp.csrf_for_seed(other)
        self.assertEqual(c.get(f"/productions/{pid}/cast").status_code, 404)
        self.assertEqual(c.post(f"/api/productions/{pid}/cast/{refs[0]['id']}/approve").status_code, 404)


if __name__ == "__main__":
    unittest.main()
