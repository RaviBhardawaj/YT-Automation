"""Film Studio phase 3: screenplay → shot list (coverage, timing, camera directions, continuity, safe re-planning).
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
from studio import auth, channels  # noqa: E402
from studio.creative import script_import  # noqa: E402
from studio.film_studio import shotlist, store  # noqa: E402

auth.ensure_default_workspace()
channels.ensure_default()

SCRIPT = """Title: Card Trouble
Duration: Exactly 2 minutes
CHARACTER 1: BALBIR — THE FARMER
Appearance: Grey moustache.
CHARACTER 2: LAKHWINDER — THE CHEATER
Appearance: Light stubble.
SCENE 1 — THE GAME
A village courtyard in the afternoon.
Balbir shuffles the cards. Lakhwinder sips chai.
BALBIR:
"Chal, baant de!"
Everyone laughs.
LAKHWINDER:
"Main toh bas dekh raha hoon."
He kicks the spare deck under the charpai.
Use a low camera angle to briefly show the hidden deck, then return to the characters.
SCENE 2 — CAUGHT
Balbir looks underneath the table.
BALBIR:
"Yeh kya hai?"
LAKHWINDER:
"Mera future."
The camera slowly pulls back, revealing the courtyard.
Fade out.
END TITLE:
"CARD TROUBLE"
"""


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.film = script_import.parse(SCRIPT)
        self.plan = shotlist.plan(self.film)
        self.shots = self.plan["shots"]

    def test_every_line_has_a_shot_of_its_speaker(self):
        spoken = [s for s in self.shots if s["dialogue"]]
        self.assertEqual(len(spoken), 4)
        for s in spoken:
            self.assertIn(s["dialogue"][0]["speaker"], s["characters"])
        self.assertEqual(spoken[0]["shot_type"], "close_up")          # punchline followed by a laugh

    def test_each_physical_action_is_its_own_insert_with_a_full_movement(self):
        inserts = [s for s in self.shots if s["shot_type"] == "insert"]
        actions = [s["action"] for s in inserts]
        self.assertIn("Balbir shuffles the cards.", actions)
        self.assertIn("Lakhwinder sips chai.", actions)
        for s in inserts:
            self.assertEqual(set(s["phases"]), {"begin", "middle", "end"})

    def test_camera_direction_applies_to_the_shot_it_describes(self):
        kick = next(s for s in self.shots if "kicks the spare deck" in s["action"])
        self.assertEqual(kick["camera"]["angle"], "low angle")
        self.assertIn("hidden deck", kick["camera"]["notes"])

    def test_scenes_open_wide_laughs_get_reactions_and_fade_is_a_transition(self):
        self.assertEqual(self.shots[0]["shot_type"], "establishing")
        self.assertTrue(any(s["shot_type"] == "reaction" and "laughs" in s["action"] for s in self.shots))
        last = self.shots[-1]
        self.assertIn("pull back", last["camera"]["movement"])
        self.assertEqual(last["continuity"]["transition_out"], "fade to black")
        self.assertFalse(any("CARD TROUBLE" in (s["action"] + json.dumps(s["dialogue"])) for s in self.shots))

    def test_continuity_links_every_shot_to_its_neighbours(self):
        self.assertIsNone(self.shots[0]["continuity"]["prev"])
        self.assertEqual(self.shots[1]["continuity"]["prev"], "1.1")
        self.assertIsNone(self.shots[-1]["continuity"]["next"])

    def test_short_script_is_reported_as_missing_runtime_not_stretched(self):
        cov = self.plan["coverage"]
        self.assertEqual(cov["target"], 120.0)
        self.assertGreater(cov["gap"], 60)
        self.assertIn("more is needed", cov["message"])
        self.assertLess(self.plan["runtime"], 60)

    def test_timing_comes_from_speech_and_pauses(self):
        self.assertGreater(shotlist.speech_seconds("Ek do teen char paanch chhe saat aath nau das. Gyarah barah."),
                           shotlist.speech_seconds("Ek do teen char paanch chhe saat aath nau das gyarah barah"))


class SaveTests(unittest.TestCase):
    def _prod(self, ws):
        return db.insert("productions", {"format": "long", "title": "F", "status": "queued", "stage": "plan",
                                         "kind": "film", "workspace_id": ws, "created_at": db.iso(),
                                         "updated_at": db.iso()})

    def test_save_is_idempotent_and_never_replaces_shots_with_takes(self):
        plan = shotlist.plan(script_import.parse(SCRIPT))
        pid = self._prod(1)
        n = shotlist.save(pid, 1, plan)
        self.assertEqual(n, len(plan["shots"]))
        self.assertEqual(shotlist.save(pid, 1, plan), n)                 # already planned: unchanged
        first = store.shots(pid, 1)[0]
        store.add_take(first["id"], 1, provider="flux", motion_type="camera_move", file_path="/x.png")
        with self.assertRaises(ValueError):
            shotlist.save(pid, 1, plan, replace=True)
        self.assertEqual(len(store.shots(pid, 1)), n)

    def test_shot_list_api_is_scoped_to_the_owner(self):
        from fastapi.testclient import TestClient
        from studio.web import app as webapp
        pw = "a long enough password"
        if not auth.has_users():
            auth.register("first-admin@test.local", pw)
        for email in ("shots-owner@test.local", "shots-other@test.local"):
            if not db.q1("SELECT id FROM users WHERE email=?", (email,)):
                auth.approve(auth.register(email, pw)["user_id"], 1)
        tok_a, _ = auth.login("shots-owner@test.local", pw)
        tok_b, _ = auth.login("shots-other@test.local", pw)
        ws_a = auth.session(tok_a)["workspace"]["id"]
        pid = self._prod(ws_a)
        shotlist.save(pid, ws_a, shotlist.plan(script_import.parse(SCRIPT)))
        c = TestClient(webapp.app, base_url=f"http://127.0.0.1:{config.PORT}", raise_server_exceptions=False)
        c.cookies.set("sid", tok_a)
        r = c.get(f"/api/productions/{pid}/shots")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["summary"]["shots"], len(r.json()["shots"]))
        self.assertEqual(r.json()["shots"][0]["motion_label"], "not made yet")
        c.cookies.set("sid", tok_b)
        self.assertEqual(c.get(f"/api/productions/{pid}/shots").status_code, 404)


if __name__ == "__main__":
    unittest.main()
