"""KahaaniVerse: channel conversion (finance schedules off, audited), the genre mix, the permanent character
library (one row per main character, front view first, every other sheet drawn FROM it, nothing kept that was drawn
without it) and films reusing the library pictures. Nothing external is called.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import random
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")
os.environ.setdefault("STUDIO_OFFLINE", "1")

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import channels  # noqa: E402
from studio.creative import film, kahaani  # noqa: E402
from studio.film_studio import casting  # noqa: E402


def channel(**kw) -> dict:
    return channels.get(channels.create({"name": f"K {os.urandom(2).hex()}", **kw}, 1))


def fake_gen(log: list, with_refs: bool = True):
    def gen(prompt, out, *, refs=None, size=(768, 768)):
        out.write_bytes(b"png")
        out.with_suffix(".json").write_text(json.dumps({"provider": "test", "refs_used":
                                                        [str(r) for r in refs or []] if with_refs else []}))
        log.append((out.name, film.current_look(), bool(refs)))
        return out
    return gen


class ConvertTests(unittest.TestCase):
    def test_conversion_switches_finance_schedules_off_and_audits_the_old_settings(self):
        ch = channel(workflow="nifty_daily", branding={"mascot": "fibbu"})
        sid = db.insert("schedules", {"name": "NIFTY test", "task_type": "nifty_daily_episode", "payload": "{}",
                                      "kind": "daily", "time_of_day": "15:40", "enabled": 1, "created_at": db.iso()})
        kahaani.convert_channel(ch["id"], "owner@test")
        now = channels.get(ch["id"])
        self.assertEqual(now["name"], kahaani.NAME)
        self.assertEqual(now["workflow"], "cinematic_film")
        self.assertNotIn("mascot", now["branding"])
        self.assertFalse(now["schedule_enabled"])            # no daily film until the daily pipeline is ready
        self.assertEqual(db.q1("SELECT enabled FROM schedules WHERE id=?", (sid,))["enabled"], 0)
        a = [json.loads(r["details"]) for r in db.q("SELECT details FROM audit_log WHERE event='channel_converted'")]
        mine = next(d for d in a if d["channel_id"] == ch["id"])
        self.assertEqual(mine["before"]["workflow"], "nifty_daily")
        self.assertEqual(len(kahaani.library(ch["id"])), len(kahaani.UNIVERSE))
        kahaani.ensure_universe(ch["id"])                     # running again never duplicates characters
        self.assertEqual(len(kahaani.library(ch["id"])), len(kahaani.UNIVERSE))

    def test_genre_mix_defaults_and_weighted_pick(self):
        ch = channel()
        self.assertEqual(sum(kahaani.mix(ch).values()), 100)
        picks = [kahaani.pick_genre(ch, random.Random(i)) for i in range(400)]
        self.assertGreater(picks.count("suspense_mystery"), picks.count("scifi_supernatural"))


class LibraryTests(unittest.TestCase):
    def test_front_first_then_every_sheet_from_the_front_in_3d(self):
        ch = channel()
        log: list = []
        res = kahaani.build_sheets(ch["id"], fake_gen(log))
        self.assertIsNone(res["waiting"])
        self.assertEqual(len(res["made"]), len(kahaani.UNIVERSE) * len(kahaani.PICTURES))
        aarav = [x for x in log if x[0].startswith("aarav_")]
        self.assertEqual(aarav[0][0], "aarav_front.png")
        self.assertFalse(aarav[0][2])                         # the front view is the original drawing
        self.assertTrue(all(r for _, _, r in aarav[1:]))     # everything else is drawn from it
        self.assertTrue(all(look == kahaani.LOOK for _, look, _ in log))
        lib = {c["key"]: c for c in kahaani.library(ch["id"])}
        self.assertEqual(lib["SIYA"]["missing"], [])
        self.assertEqual(lib["VEER"]["voice"]["voice_id"], "hm_omega")
        self.assertIn("pocket watch", lib["VEER"]["identity"]["accessories"])

    def test_sheet_drawn_without_the_front_view_is_not_kept(self):
        ch = channel()
        log: list = []
        res = kahaani.build_sheets(ch["id"], fake_gen(log, with_refs=False))
        self.assertIn("reference", res["waiting"])
        self.assertIn("front", {c["key"]: list(c["refs"]) for c in kahaani.library(ch["id"])}["SIYA"])
        lib = {c["key"]: c for c in kahaani.library(ch["id"])}
        self.assertEqual(list(lib["AARAV"]["refs"]), ["front"])
        self.assertFalse((kahaani.library_dir(ch["id"]) / "aarav_side.png").exists())


class FilmReuseTests(unittest.TestCase):
    def test_recurring_character_uses_the_approved_library_picture(self):
        ch = channel()
        pic = Path(tempfile.mkdtemp()) / "front.png"
        pic.write_bytes(b"png")
        pid = db.insert("productions", {"format": "long", "title": "T", "status": "queued", "stage": "cast",
                                        "kind": "film", "channel_id": ch["id"], "workspace_id": 1, "package": "{}",
                                        "created_at": db.iso(), "updated_at": db.iso()})
        filmd = {"cast": [{"key": "AARAV", "name": "Aarav", "library_ref": str(pic)},
                          {"key": "RAJU", "name": "Raju", "look": "a tea seller"}], "location": "a lane"}
        casting.propose(pid, 1, filmd)
        self.assertEqual(casting.missing(pid, 1), ["Raju", "Main location"])
        with film.look_as("animated3d"):
            self.assertIn("3D animated", casting.character_prompt("Raju", {"appearance": "a tea seller"}))
        self.assertIn("Photorealistic", casting.character_prompt("Raju", {"appearance": "a tea seller"}))


if __name__ == "__main__":
    unittest.main()


class PageTests(unittest.TestCase):
    def test_page_shows_library_mix_and_backlog(self):
        from fastapi.testclient import TestClient

        from studio import auth
        from studio.web import app as webapp
        email = "kv@test.local"
        if not db.q1("SELECT id FROM users WHERE email=?", (email,)):
            auth.approve(auth.register(email, "a long enough password")["user_id"], 1)
        db.ex("UPDATE users SET is_admin=1 WHERE email=?", (email,))
        tok, _ = auth.login(email, "a long enough password")
        for c in channels.all_channels():                    # one KahaaniVerse channel for this test
            if c["workflow"] == "cinematic_film":
                channels.update(c["id"], {"workflow": "auto"})
        ws = auth.session(tok)["workspace"]["id"]            # the channel belongs to this user's own workspace
        ch = channels.get(channels.create({"name": f"KV page {os.urandom(2).hex()}"}, ws))
        kahaani.convert_channel(ch["id"], "test")
        kahaani.build_sheets(ch["id"], fake_gen([]), limit=2)
        client = TestClient(webapp.app, base_url=f"http://127.0.0.1:{config.PORT}", raise_server_exceptions=True)
        client.cookies.set("sid", tok)
        client.headers["X-CSRF-Token"] = webapp.csrf_for_seed(tok)
        r = client.get("/kahaaniverse", follow_redirects=False)
        self.assertEqual(r.headers["location"], f"/channels/{ch['id']}?tab=settings#kahaaniverse")
        r = client.get(f"/channels/{ch['id']}?tab=settings")
        self.assertEqual(r.status_code, 200)
        for text in ("Aarav", "Dadaji", "pocket watch", "Genre mix", "Story backlog"):
            self.assertIn(text, r.text)
        pic = f"/media/characters/channel_{ch['id']}/aarav_front.png"
        self.assertIn(pic, r.text)
        self.assertEqual(client.get(pic).status_code, 200)       # the library pictures are served to the owner
        self.assertNotIn("Genre mix", client.get(f"/channels/{ch['id']}?tab=overview").text)
        bad_mix = client.post("/api/kahaaniverse/mix", json={"suspense_mystery": 90})
        self.assertIn("100%", bad_mix.json()["error"])
