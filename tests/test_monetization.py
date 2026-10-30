"""Monetization readiness: honest status (unknown / in progress / requirements met / owner-recorded decision),
thresholds with their source and check date, policy checks from the channel's own uploads, and the weekly
decision report. YouTube is never called.
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
from studio import channels, learning  # noqa: E402
from studio.youtube import growth  # noqa: E402


def channel(**kw) -> dict:
    return channels.get(channels.create({"name": f"Money {os.urandom(2).hex()}", **kw}, 1))


def upload(cid: int, title: str, presenter: bool = False) -> int:
    pid = db.insert("productions", {"format": "long", "title": title, "status": "uploaded", "stage": "qa",
                                    "kind": "general", "workspace_id": 1, "channel_id": cid, "package": "{}",
                                    "qa": "{}", "youtube_video_id": f"v{os.urandom(3).hex()}", "uploaded_at": db.iso(),
                                    "created_at": db.iso(), "updated_at": db.iso()})
    if presenter:
        db.insert("assets", {"production_id": pid, "scene_index": 0, "kind": "image", "provider": "ai_presenter",
                             "provider_id": "p", "local_path": "/x.png", "created_at": db.iso()})
    return pid


class StatusTests(unittest.TestCase):
    def test_status_never_claims_more_than_the_evidence(self):
        ch = channel()
        self.assertEqual(growth.status(ch)["key"], "unknown")
        m = {"fetched_at": db.iso(), "subscribers": 1200, "watch_hours_12m": 4100, "shorts_views_90d": 0,
             "public_uploads_90d": 9}
        m["tiers"] = {k: growth.progress(m, t) for k, t in growth.TIERS.items()}
        db.update("channels", ch["id"], {"monetization": json.dumps(m)})
        st = growth.status(channels.get(ch["id"]))
        self.assertEqual(st["key"], "requirements_met")
        self.assertIn("not acceptance", st["label"])                     # eligibility is not acceptance
        growth.set_owner_status(ch["id"], "accepted", "owner@test")
        self.assertEqual(growth.status(channels.get(ch["id"]))["key"], "accepted")
        with self.assertRaises(ValueError):
            growth.set_owner_status(ch["id"], "monetized!", "owner@test")

    def test_thresholds_carry_their_source_and_check_date(self):
        self.assertTrue(growth.RULES["checked"])
        self.assertIn("support.google.com/youtube/answer/72851", growth.RULES["sources"]["ypp_overview"])
        self.assertTrue(growth.TIERS["full"]["verified"])
        self.assertFalse(growth.TIERS["early"]["verified"])               # not on the official page as checked


class PolicyTests(unittest.TestCase):
    def test_ai_presenter_on_a_finance_channel_is_flagged(self):
        ch = channel(niche="stock market news", styles=["news", "finance"])
        upload(ch["id"], "Markets fall after the rate decision", presenter=True)
        checks = {c["name"]: c for c in growth.policy_readiness(ch["id"])}
        self.assertEqual(checks["AI persona on a sensitive topic"]["state"], "warn")
        self.assertIn("support.google.com", checks["AI persona on a sensitive topic"]["policy"])

    def test_near_identical_titles_are_flagged_as_repetitive(self):
        ch = channel(niche="space facts")
        for _ in range(3):
            upload(ch["id"], "10 amazing facts about the planet Mars you never knew")
        checks = {c["name"]: c for c in growth.policy_readiness(ch["id"])}
        self.assertEqual(checks["inauthentic / repetitive content"]["state"], "warn")

    def test_no_uploads_is_not_checked_not_passed(self):
        self.assertEqual(growth.policy_readiness(channel()["id"])[0]["state"], "not_checked")


class WeeklyTests(unittest.TestCase):
    def test_decisions_follow_the_numbers(self):
        base = {"id": 1, "title": "t", "format": "long", "views": 0, "age_days": 10.0}
        self.assertEqual(learning._decide({**base, "age_days": 1.0, "views_per_day": 5, "avg_viewed_pct": 50}, 5)[0],
                         "too early")
        self.assertEqual(learning._decide({**base, "views_per_day": 20, "avg_viewed_pct": 55}, 5)[0], "continue")
        self.assertEqual(learning._decide({**base, "views_per_day": 5, "avg_viewed_pct": 15}, 5)[0], "improve")
        dec, why = learning._decide({**base, "views_per_day": 1, "avg_viewed_pct": 60}, 5)
        self.assertEqual(dec, "repackage")
        self.assertIn("hypothesis", why)                                   # CTR is not measured here
        self.assertEqual(learning._decide({**base, "views_per_day": 5, "avg_viewed_pct": None}, 5)[0], "no data")

    def test_report_is_stored_once_per_week(self):
        ch = channel()
        with mock.patch.object(learning, "results", lambda cid: []):
            learning.weekly_report(ch["id"])
            learning.weekly_report(ch["id"])
        self.assertEqual(db.q1("SELECT COUNT(*) AS n FROM channel_reviews WHERE channel_id=?", (ch["id"],))["n"], 1)
        self.assertIn("click-through rate", learning.latest_weekly(ch["id"])["not_collected"])


class PageTests(unittest.TestCase):
    def test_channel_page_shows_status_policy_and_weekly_review(self):
        from fastapi.testclient import TestClient

        from studio import auth
        from studio.web import app as webapp
        if not db.q1("SELECT id FROM users WHERE email='money-owner@test.local'"):
            auth.approve(auth.register("money-owner@test.local", "a long enough password")["user_id"], 1)
        tok, _ = auth.login("money-owner@test.local", "a long enough password")
        ws = auth.session(tok)["workspace"]["id"]
        cid = channels.create({"name": f"Page {os.urandom(2).hex()}"}, ws)
        c = TestClient(webapp.app, base_url=f"http://127.0.0.1:{config.PORT}", follow_redirects=False)
        c.cookies.set("sid", tok)
        html = c.get(f"/channels/{cid}?tab=youtube").text
        self.assertIn("Policy readiness", html)
        self.assertIn("Weekly review", html)


if __name__ == "__main__":
    unittest.main()
