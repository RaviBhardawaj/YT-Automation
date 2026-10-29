"""Photorealistic Film Studio, phase 2: opt-in switch, isolated records, honest takes, additive migration.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import auth, channels, film_studio, jobs, settings, worker  # noqa: E402
from studio.film_studio import store  # noqa: E402

auth.ensure_default_workspace()
channels.ensure_default()
REAL_SCRIPT_REQUEST = "A photorealistic live-action short film about two farmers arguing over a tractor"


def ws_with(perm: bool | None, plan: str = "free") -> int:
    perms = {} if perm is None else {"film_studio": perm}
    return db.insert("workspaces", {"name": "fs", "plan": plan, "permissions": json.dumps(perms), "created_at": db.iso()})


def film_production(ws: int) -> int:
    return db.insert("productions", {"format": "long", "title": "Film", "status": "queued", "stage": "script",
                                     "kind": "film", "workspace_id": ws, "created_at": db.iso(),
                                     "updated_at": db.iso()})


class SwitchTests(unittest.TestCase):
    def tearDown(self):
        settings.set("film_studio.enabled", False)

    def test_off_by_default_and_never_falls_back_to_a_cartoon(self):
        self.assertFalse(settings.get("film_studio.enabled"))
        ws = ws_with(True)
        ch = channels.holder(ws)
        before = db.q1("SELECT COUNT(*) AS n FROM productions")["n"]
        with mock.patch.object(worker, "enqueue", return_value=1):
            with self.assertRaises(film_studio.StudioOff):
                jobs.create(ch, REAL_SCRIPT_REQUEST, no_upload=True)
        self.assertEqual(db.q1("SELECT COUNT(*) AS n FROM productions")["n"], before)   # nothing was made

    def test_needs_both_the_platform_switch_and_the_account_permission(self):
        settings.set("film_studio.enabled", True)
        no = ws_with(False, plan="studio")
        yes = ws_with(True)
        self.assertFalse(film_studio.enabled(no))
        self.assertTrue(film_studio.enabled(yes))
        with mock.patch.object(worker, "enqueue", return_value=1):
            with self.assertRaises(film_studio.StudioOff):
                jobs.create(channels.holder(no), REAL_SCRIPT_REQUEST, no_upload=True)
            res = jobs.create(channels.holder(yes), REAL_SCRIPT_REQUEST, no_upload=True)
        self.assertEqual(res["plan"]["workflow"], "film")

    def test_other_workflows_unaffected_when_off(self):
        ws = ws_with(None)
        ch = channels.create({"name": "Explainers", "workflow": "factual"}, ws)
        with mock.patch.object(worker, "enqueue", return_value=1):
            res = jobs.create(ch, "explain how tea is grown")
        self.assertNotEqual(res["plan"].get("workflow"), "film")


class StoreTests(unittest.TestCase):
    def test_records_belong_to_one_account(self):
        a, b = ws_with(True), ws_with(True)
        pid = film_production(a)
        rid = store.add_ref(pid, a, "character", "F1", "Balbir", "white kurta")
        sid = store.add_shot(pid, a, 1, 1, shot_type="wide", characters=["F1"])
        with self.assertRaises(store.NotFound):
            store.refs(pid, b)
        with self.assertRaises(store.NotFound):
            store.shot(sid, b)
        with self.assertRaises(store.NotFound):
            store.set_ref_status(rid, b, "approved")
        with self.assertRaises(store.NotFound):
            store.add_shot(pid, b, 1, 2)

    def test_one_approved_version_and_old_versions_kept(self):
        ws = ws_with(True)
        pid = film_production(ws)
        v1 = store.add_ref(pid, ws, "character", "F1", "Balbir")
        v2 = store.add_ref(pid, ws, "character", "F1", "Balbir")
        store.set_ref_status(v1, ws, "approved")
        store.set_ref_status(v2, ws, "approved")
        self.assertEqual(store.approved_ref(pid, ws, "character", "F1")["id"], v2)
        self.assertEqual(len(store.refs(pid, ws, "character")), 2)

    def test_takes_are_kept_and_say_what_they_really_are(self):
        ws = ws_with(True)
        pid = film_production(ws)
        sid = store.add_shot(pid, ws, 1, 1, shot_type="medium")
        with self.assertRaises(ValueError):
            store.add_take(sid, ws, provider="x", motion_type="planned", file_path=None)
        t1 = store.add_take(sid, ws, provider="flux", motion_type="camera_move", file_path="/a.png")
        store.add_take(sid, ws, provider="wan", motion_type="video", file_path=None, status="failed", error="oom")
        s = store.shot(sid, ws)
        self.assertEqual(s["current_take"], t1)                  # a failed retake never replaces a good take
        self.assertEqual(s["motion_type"], "camera_move")
        self.assertEqual(s["motion_label"], "still image with camera move")
        self.assertEqual(len(store.takes(sid, ws)), 2)
        self.assertEqual(store.summary(pid, ws)["by_motion"], {"camera_move": 1})


class MigrationTests(unittest.TestCase):
    def test_migration_is_additive_on_a_copy_of_a_real_database(self):
        src = os.environ.get("FILM_STUDIO_REAL_DB_COPY")
        if not src or not Path(src).exists():
            self.skipTest("set FILM_STUDIO_REAL_DB_COPY to a backup of the live database to run this check")
        tmp = Path(tempfile.mkdtemp()) / "copy.db"
        shutil.copy(src, tmp)
        con = sqlite3.connect(tmp)
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        before = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        version = con.execute("PRAGMA user_version").fetchone()[0]
        for sql in db.MIGRATIONS[version:]:
            con.executescript(sql)
        after = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        self.assertEqual(before, after)                          # every existing record is still there
        self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        new = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")} - set(tables)
        self.assertEqual(new, {"film_refs", "film_shots", "film_takes"})


class SettingsFormTests(unittest.TestCase):
    def test_every_visuals_and_film_studio_field_can_be_saved(self):
        from fastapi.testclient import TestClient
        from studio.web import app as webapp
        pw = "a long enough password"
        if not auth.has_users():
            auth.register("first-admin@test.local", pw)
        admin = db.q1("SELECT email FROM users WHERE is_admin=1 ORDER BY id LIMIT 1")["email"]
        tok, _ = auth.login(admin, pw)
        c = TestClient(webapp.app, base_url=f"http://127.0.0.1:{config.PORT}", raise_server_exceptions=False)
        c.cookies.set("sid", tok)
        c.headers["X-CSRF-Token"] = webapp.csrf_for_seed(tok)
        r = c.post("/api/settings", json={"visuals.cloudflare_account_id": "abc", "render.film_look": "auto",
                                          "film_studio.enabled": False, "film_studio.max_retries_per_shot": 2})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(c.get("/settings?tab=film").status_code, 200)
        self.assertEqual(c.get("/settings?tab=visuals").status_code, 200)


if __name__ == "__main__":
    unittest.main()
