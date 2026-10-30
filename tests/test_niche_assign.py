"""Approving a niche hands it to a channel (existing or new) with an optional daily video; several niches can be
approved at once, one per channel; the daily video prefers today's trend-based idea. Nothing external is called.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins
os.environ.setdefault("STUDIO_OFFLINE", "1")              # never call the free image services

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import auth, channels, tasks  # noqa: E402


def niche(name: str) -> int:
    return db.insert("niches", {"name": name, "description": f"{name} explained simply", "status": "candidate",
                                "source": "ai", "created_at": db.iso()})


def client():
    from fastapi.testclient import TestClient

    from studio.web import app as webapp
    if not db.q1("SELECT id FROM users WHERE email='niche-owner@test.local'"):
        auth.approve(auth.register("niche-owner@test.local", "a long enough password")["user_id"], 1)
    db.ex("UPDATE users SET is_admin=1 WHERE email='niche-owner@test.local'")    # niche research is owner-only
    tok, _ = auth.login("niche-owner@test.local", "a long enough password")
    c = TestClient(webapp.app, base_url=f"http://127.0.0.1:{config.PORT}", follow_redirects=False)
    c.cookies.set("sid", tok)
    c.headers["X-CSRF-Token"] = webapp.csrf_for_seed(tok)
    return c, auth.session(tok)["workspace"]["id"]


class AssignTests(unittest.TestCase):
    def test_existing_channel_gets_the_niche_and_a_daily_video(self):
        c, ws = client()
        cid = channels.create({"name": f"Space {os.urandom(2).hex()}"}, ws)
        nid = niche("Space facts")
        r = c.post(f"/api/niches/{nid}/assign", json={"channel": str(cid), "daily": True, "time": "07:30",
                                                        "formats": "short"})
        self.assertTrue(r.json()["ok"], r.text)
        ch = channels.get(cid)
        self.assertEqual((ch["niche"], ch["schedule_enabled"], ch["schedule_time"], ch["formats"]),
                         ("Space facts", 1, "07:30", "short"))
        self.assertEqual(db.q1("SELECT status, channel_id FROM niches WHERE id=?", (nid,)),
                         {"status": "approved", "channel_id": cid})
        self.assertTrue(db.q1("SELECT 1 FROM tasks WHERE type='trend_research' AND payload LIKE ?",
                              (f'%"channel_id": {cid}%',)))
        self.assertTrue(db.q1("SELECT 1 FROM schedules WHERE channel_id=? AND time_of_day='07:30'", (cid,)))

    def test_a_new_channel_can_be_made_for_the_niche_and_two_niches_stay_approved(self):
        c, ws = client()
        first, second = niche("Cooking hacks"), niche("Car repair basics")
        r = c.post(f"/api/niches/{first}/assign", json={"channel": "new", "daily": False})
        new_id = db.q1("SELECT channel_id FROM niches WHERE id=?", (first,))["channel_id"]
        self.assertTrue(r.json()["ok"])
        self.assertEqual(channels.get(new_id)["niche"], "Cooking hacks")
        self.assertFalse(channels.get(new_id)["auto_publish"])                 # a new channel never auto-publishes
        c.post(f"/api/niches/{second}/assign", json={"channel": "new"})
        self.assertEqual({r["status"] for r in db.q("SELECT status FROM niches WHERE id IN (?,?)", (first, second))},
                         {"approved"})

    def test_another_accounts_channel_is_refused(self):
        c, _ = client()
        other = channels.create({"name": "Someone else's"}, 999)
        r = c.post(f"/api/niches/{niche('Gardening')}/assign", json={"channel": str(other)})
        self.assertFalse(r.json()["ok"])

    def test_the_niche_page_asks_which_channel(self):
        c, _ = client()
        niche("Photography tips")
        html = c.get("/niches").text
        self.assertIn("Which channel handles", html)
        self.assertIn("Publish a new video every day", html)


class DailyPickTests(unittest.TestCase):
    def test_todays_trend_idea_beats_an_older_higher_scored_idea(self):
        cid = channels.create({"name": f"Pick {os.urandom(2).hex()}", "workflow": "factual"}, 1)
        base = {"channel_id": cid, "workspace_id": 1, "status": "idea", "format": "long", "created_at": db.iso(),
                "updated_at": db.iso()}
        cols = {r["name"] for r in db.q("PRAGMA table_info(topics)")}
        row = lambda **kw: {k: v for k, v in {**base, **kw}.items() if k in cols}  # noqa: E731
        db.insert("topics", row(title="Old evergreen idea", score=95, why_now="evergreen"))
        trend = db.insert("topics", row(title="Today's popular theme idea", score=60,
                                        why_now="this week's popular theme (original story)"))
        made = {}

        def fake_create(cid_, request, idea_id=None, actor="", scheduled_for=None):
            made["idea"] = idea_id
            return {"production_ids": [1]}
        with mock.patch("studio.jobs.create", fake_create), mock.patch("studio.plans.can", lambda *a: True):
            tasks._channel_daily({"channel_id": cid}, {})
        self.assertEqual(made["idea"], trend)


if __name__ == "__main__":
    unittest.main()
