"""Researched niches give ideas about that niche only, owned by the account, and channel-less videos stay neutral.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import auth, channels, jobs  # noqa: E402
from studio.research import engine  # noqa: E402

auth.ensure_default_workspace()
channels.ensure_default()
PW = "a long enough password"
if not auth.has_users():
    auth.register("first-admin@test.local", PW)            # same first account as the other suites


def client(email: str):
    from fastapi.testclient import TestClient
    from studio.web import app as webapp
    if not db.q1("SELECT id FROM users WHERE email=?", (email,)):
        auth.approve(auth.register(email, PW)["user_id"], 1)
    tok, _ = auth.login(email, PW)
    c = TestClient(webapp.app, base_url=f"http://127.0.0.1:{config.PORT}", follow_redirects=False,
                   raise_server_exceptions=False)
    c.cookies.set("sid", tok)
    c.headers["X-CSRF-Token"] = webapp.csrf_for_seed(tok)
    return c, auth.session(tok)["workspace"]["id"]


class NicheIdeaTests(unittest.TestCase):
    def test_performance_context_survives_missing_metrics(self):
        pid = db.insert("productions", {"format": "long", "title": "No stats yet", "status": "uploaded", "stage": "done",
                                        "kind": "general", "workspace_id": 1, "created_at": db.iso(), "updated_at": db.iso()})
        for i, m in enumerate(({"views": 10, "averageViewPercentage": None}, {"views": None},
                               {"views": 5, "averageViewPercentage": 40.0})):
            db.insert("analytics", {"production_id": pid, "video_id": f"v{pid}-{i}", "metrics": json.dumps(m),
                                    "fetched_at": db.iso()})
        self.assertIn("CHANNEL PERFORMANCE", engine._performance_context())

    def test_niche_ideas_belong_to_the_workspace_not_a_channel(self):
        idea = SimpleNamespace(evidence_ids=[], angle="Low-sugar breakfasts explained")
        f = engine._niche_fields({"workspace_id": 7}, idea, {}, "English")
        self.assertEqual(f["workspace_id"], 7)
        self.assertNotIn("channel_id", f)
        self.assertEqual(f["language"], "English")

    def test_channel_less_video_ignores_the_borrowed_channels_style(self):
        kids = channels.create({"name": "Kids test", "workflow": "animated_story", "styles": ["kids"]}, 1)
        idea = {"id": 1, "title": "5 breakfast mistakes that spike blood sugar", "style": None, "language": "English"}
        neutral = jobs.plan_for(kids, "", idea, standalone=True)
        self.assertEqual(neutral["workflow"], "factual")
        self.assertEqual(neutral["language"], "English")
        self.assertEqual(jobs.plan_for(kids, "", idea)["workflow"], "animated_story")


class NicheIdeasPageTests(unittest.TestCase):
    def test_niche_ideas_show_only_to_their_owner_and_can_be_made_without_a_channel(self):
        owner, ws = client("niche-owner@test.local")
        other, _ = client("niche-other@test.local")
        nid = db.insert("niches", {"name": "Diabetes care", "description": "", "status": "approved",
                                   "created_at": db.iso()})
        tid = db.insert("topics", {"niche_id": nid, "workspace_id": ws, "title": "Foot care with diabetes",
                                   "angle": "a", "format": "short", "hook_idea": "", "why_now": "", "status": "idea",
                                   "score": 50, "norm_title": "foot care with diabetes", "language": "English",
                                   "created_at": db.iso(), "updated_at": db.iso()})
        r = owner.get(f"/ideas?channel=all&nid={nid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Foot care with diabetes", r.text)
        self.assertIn("No channel — just make it", r.text)
        self.assertNotIn("Foot care with diabetes", other.get(f"/ideas?channel=all&nid={nid}").text)
        self.assertEqual(other.post(f"/api/topics/{tid}/video", json={"channel_id": "none"}).status_code, 404)
        r = owner.post(f"/api/topics/{tid}/video", json={"channel_id": "none"})
        self.assertEqual(r.status_code, 200, r.text)
        p = db.q1("SELECT * FROM productions WHERE topic_id=? ORDER BY id DESC", (tid,))
        self.assertTrue(json.loads(p["request"])["no_upload"])
        self.assertIsNone(db.q1("SELECT channel_id FROM topics WHERE id=?", (tid,))["channel_id"])


class MyVoicePageTests(unittest.TestCase):
    def test_voice_page_upload_and_other_accounts_cannot_touch_it(self):
        import base64
        import io
        import wave
        import numpy as np
        owner, ws = client("voice-owner@test.local")
        other, _ = client("voice-other@test.local")
        db.ex("UPDATE workspaces SET plan='studio' WHERE id=?", (ws,))
        t = np.arange(24000 * 12) / 24000
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
            w.writeframes((np.sin(2 * np.pi * 200 * t) * 8000).astype(np.int16).tobytes())
        data = "data:audio/wav;base64," + base64.b64encode(buf.getvalue()).decode()
        self.assertEqual(owner.post("/api/voices", json={"name": "Me", "language": "Hindi", "filename": "a.wav",
                                                         "data": data}).status_code, 400)   # no consent
        r = owner.post("/api/voices", json={"name": "Me", "language": "Hindi", "filename": "a.wav", "data": data,
                                            "consent": True})
        self.assertEqual(r.status_code, 200, r.text)
        vid = r.json()["voice_id"]
        page = owner.get("/voices").text
        self.assertIn("Me", page)
        self.assertIn("/media/workspaces/", page)
        self.assertIn("(my voice)", owner.get("/create").text)
        self.assertNotIn("(my voice)", other.get("/create").text)
        self.assertEqual(other.post(f"/api/voices/{vid}/delete").status_code, 404)
        sample = db.q1("SELECT sample_path FROM voices WHERE id=?", (vid,))["sample_path"]
        rel = "/media/" + str(Path(sample).resolve().relative_to(config.DATA_DIR.resolve()))
        self.assertEqual(owner.get(rel).status_code, 200)
        self.assertEqual(other.get(rel).status_code, 404)


if __name__ == "__main__":
    unittest.main()
