"""Film Studio phases 10–11: Shorts proposals (never auto-published), dashboard pages, shot actions, isolation.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import auth, channels, pipeline, settings, worker  # noqa: E402
from studio.film_studio import edit, shorts, store  # noqa: E402
from tests.test_film_phases import new_film, run_all  # noqa: E402

auth.ensure_default_workspace()
channels.ensure_default()
PW = "a long enough password"


def client(email):
    from fastapi.testclient import TestClient
    from studio.web import app as webapp
    if not auth.has_users():
        auth.register("first-admin@test.local", PW)
    if not db.q1("SELECT id FROM users WHERE email=?", (email,)):
        auth.approve(auth.register(email, PW)["user_id"], 1)
    tok, _ = auth.login(email, PW)
    c = TestClient(webapp.app, base_url=f"http://127.0.0.1:{config.PORT}", raise_server_exceptions=True)
    c.cookies.set("sid", tok)
    c.headers["X-CSRF-Token"] = webapp.csrf_for_seed(tok)
    return c, auth.session(tok)["workspace"]["id"]


def owner_of_ws1():
    row = db.q1("SELECT u.email FROM users u JOIN workspace_members m ON m.user_id=u.id WHERE m.workspace_id=1 "
                "ORDER BY u.id LIMIT 1")
    return row["email"] if row else None


class ShortsTests(unittest.TestCase):
    def test_proposals_start_on_a_line_and_end_on_a_payoff(self):
        tl = [{"kind": "title", "start": 0, "end": 3.5, "seconds": 3.5}]
        t = 3.5
        for i, (kind, sub) in enumerate([("medium", "A?"), ("close_up", "B!"), ("reaction", None), ("medium", "C"),
                                         ("medium", "D"), ("reaction", None)]):
            tl.append({"kind": "shot", "scene": 1, "shot": i + 1, "shot_type": kind, "subtitle": sub, "start": t,
                       "end": t + 4.0, "seconds": 4.0, "motion_type": "camera_move"})
            t += 4.0
        props = shorts.propose(tl)
        self.assertTrue(props)
        for pr in props:
            starts = {c["start"] for c in tl if c["kind"] == "shot" and c.get("subtitle")}
            ends = {c["end"] for c in tl if c["kind"] == "shot"}
            self.assertIn(pr["start"], starts)                       # never starts mid-sentence
            self.assertIn(pr["end"], ends)                            # ends on a shot boundary
            self.assertTrue(shorts.MIN_S <= pr["end"] - pr["start"] <= shorts.MAX_S)

    def test_made_short_is_vertical_unpublished_and_download_only(self):
        pid = new_film()
        p = run_all(pid, video=False)
        tl = json.loads((edit.export_dir(pid) / "timeline.json").read_text())
        film_pkg = p["package"]
        film_pkg["shorts_proposals"] = [{"start": tl[1]["start"], "end": tl[-2]["end"], "scene": 1, "lines": 2,
                                         "video_shots": 0, "score": 1, "first_line": "Do rupaye!", "payoff": True}]
        pipeline.save(pid, package=film_pkg)
        sid = shorts.make(pipeline.load(pid), 0)
        s = pipeline.load(sid)
        self.assertEqual(s["kind"], "film_short")
        self.assertEqual(s["status"], "awaiting_approval")
        self.assertTrue(json.loads(db.q1("SELECT request FROM productions WHERE id=?", (sid,))["request"])["no_upload"])
        out = subprocess.run([config.FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height", "-of", "csv=p=0", s["video_path"]], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "1080,1920")
        self.assertFalse(db.q1("SELECT 1 FROM tasks WHERE type='upload' AND production_id=?", (sid,)))


class DashboardTests(unittest.TestCase):
    def tearDown(self):
        settings.set("film_studio.enabled", True)

    def test_switched_off_shows_a_message_and_no_menu(self):
        settings.set("film_studio.enabled", False)
        c, _ = client("dash-off@test.local")
        r = c.get("/film-studio")
        self.assertIn("switched off", r.text)
        self.assertNotIn('href="/film-studio"', c.get("/ideas").text)

    def test_owner_sees_timeline_and_can_act_on_shots_others_cannot(self):
        pid = new_film()
        run_all(pid, video=False)
        email = owner_of_ws1()
        if not email:
            self.skipTest("workspace 1 has no owner account in this test database")
        db.ex("UPDATE users SET password_hash=(SELECT password_hash FROM users WHERE email='first-admin@test.local') "
              "WHERE email=?", (email,)) if email != "first-admin@test.local" else None
        c, ws = client(email)
        self.assertEqual(ws, 1)
        self.assertIn('href="/film-studio"', c.get("/ideas").text)
        listing = c.get("/film-studio")
        self.assertIn("Coin Trick", listing.text)
        page = c.get(f"/film-studio/{pid}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("still image with camera move", page.text)
        self.assertIn("Quality checks", page.text)
        self.assertIn("film.mp4", page.text)
        shot = store.shots(pid, 1)[0]
        with mock.patch.object(worker, "enqueue", return_value=1) as enq:
            self.assertEqual(c.post(f"/api/productions/{pid}/shots/{shot['id']}/retry").status_code, 200)
            self.assertEqual(enq.call_args.args[0], "film_shots_run")
        self.assertEqual(len(store.takes(shot["id"], 1)), 1)              # the earlier take is kept
        self.assertIsNone(store.shot(shot["id"], 1)["current_take"])
        other, _ = client("dash-other@test.local")
        self.assertEqual(other.get(f"/film-studio/{pid}").status_code, 404)
        self.assertEqual(other.post(f"/api/productions/{pid}/shots/{shot['id']}/approve").status_code, 404)

    def test_new_film_starts_without_a_channel(self):
        settings.set("film_studio.enabled", True)
        c, ws = client("dash-new@test.local")
        perms = db.loads(db.q1("SELECT permissions FROM workspaces WHERE id=?", (ws,))["permissions"], {}) or {}
        db.ex("UPDATE workspaces SET permissions=? WHERE id=?", (json.dumps({**perms, "film_studio": True}), ws))
        with mock.patch.object(worker, "enqueue", return_value=1):
            r = c.post("/api/film-studio/new", json={"text": "A realistic short film about two friends and a stubborn "
                                                             "buffalo that won't leave the road."})
        self.assertEqual(r.status_code, 200, r.text)
        pid = int(r.json()["next"].rsplit("/", 1)[1])
        p = pipeline.load(pid)
        self.assertEqual(p["kind"], "film")
        self.assertEqual(channels.get(p["channel_id"])["status"], "hidden")


if __name__ == "__main__":
    unittest.main()
