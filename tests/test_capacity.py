"""Smart timing: making windows never overlap (one heavy job at a time), avoid unreliable-internet hours, finish
before the audience slot or are made the evening before, keep the market channel fixed, and small changes are skipped.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

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
from studio import capacity, channels  # noqa: E402


def mins(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


class PlanTests(unittest.TestCase):
    def setUp(self):
        tag = os.urandom(2).hex()
        self.kids = channels.create({"name": f"K{tag}", "workflow": "animated_story", "made_for_kids": True,
                                     "schedule_enabled": True, "schedule_time": "18:00"}, 1)
        self.news = channels.create({"name": f"N{tag}", "workflow": "factual", "styles": ["news"],
                                     "branding": {"news_regions": ["US", "IN"]}, "schedule_enabled": True,
                                     "schedule_time": "18:00"}, 1)
        self.nifty = channels.create({"name": f"F{tag}", "workflow": "nifty_daily"}, 1)
        mine = {self.kids, self.news, self.nifty}
        real = channels.all_channels
        self.p = [mock.patch.object(channels, "all_channels", lambda *a: [c for c in real() if c["id"] in mine]),
                  mock.patch.object(capacity, "make_minutes", lambda cid, kind, days=21:
                                    {"story": 240, "general": 120, "nifty_pivot": 80}[kind]),
                  mock.patch.object(capacity, "offline_rate", lambda days=14: {h: (0.8 if h < 8 else 0.1)
                                                                                for h in range(24)})]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def test_windows_never_overlap_and_avoid_offline_hours(self):
        plan = capacity.plan()
        spans = sorted((mins(a), mins(b) if mins(b) > mins(a) else mins(b) + 1440) for a, b, _ in plan["lane"])
        for (a1, b1), (a2, b2) in zip(spans, spans[1:]):
            self.assertLessEqual(b1, a2)
        for r in plan["channels"]:
            if r["channel_id"] != self.nifty:
                self.assertGreaterEqual(mins(r["make_at"]), 8 * 60)          # not in the offline night hours

    def test_market_channel_is_fixed_and_audience_slots_are_kept(self):
        rows = {r["channel_id"]: r for r in capacity.plan()["channels"]}
        self.assertEqual(rows[self.nifty]["make_at"], "15:40")
        self.assertEqual(rows[self.kids]["publish_at"], "18:00")
        self.assertEqual(rows[self.news]["publish_at"], "18:30")

    def test_apply_moves_only_meaningful_changes(self):
        first = capacity.apply()
        self.assertTrue(first)
        self.assertEqual(capacity.apply(), [])                                 # same plan: nothing to move
        ch = channels.get(self.news)
        self.assertEqual(ch["publish_time"], "18:30")


class FilmAndOrderTests(PlanTests):
    def setUp(self):
        super().setUp()
        self.film = channels.create({"name": f"Film{os.urandom(2).hex()}", "workflow": "cinematic_film",
                                     "schedule_enabled": True, "schedule_time": "09:00", "long_minutes": 10}, 1)
        mine = {self.kids, self.news, self.nifty, self.film}
        self.p[0].stop()
        from studio import channels as ch_mod
        self.p[0] = mock.patch.object(ch_mod, "all_channels",
                                      lambda *a: [ch_mod.get(i) for i in mine])
        self.p[0].start()

    def test_film_is_sized_to_fit_the_day_and_goes_first(self):
        plan = capacity.plan()
        rows = {r["channel_id"]: r for r in plan["channels"]}
        film = rows[self.film]
        self.assertLess(film["film_minutes"], 10)                          # a 10-minute film can't be made in a day
        busy = sum((mins(b) - mins(a)) % 1440 for a, b, _ in plan["lane"])
        self.assertLessEqual(busy, capacity.RELIABLE_DAY + 80 + 30)       # fits (nifty is fixed outside the budget)
        firsts = sorted((mins(r["make_at"]), cid) for cid, r in rows.items() if cid != self.nifty)
        self.assertEqual(firsts[0][1], self.film)                          # first in the allowance day
        self.assertLess(mins(rows[self.news]["make_at"]), mins(rows[self.kids]["make_at"]))   # news made fresh
        capacity.apply(plan)
        self.assertEqual(channels.get(self.film)["long_minutes"], film["film_minutes"])


class UploadPacingTests(unittest.TestCase):
    def setUp(self):
        self.ch = channels.create({"name": f"U{os.urandom(2).hex()}"}, 1)

    def up(self, minutes_ago: float) -> None:
        db.insert("productions", {"format": "short", "title": "x", "status": "uploaded", "stage": "qa", "kind": "general",
                                  "workspace_id": 1, "channel_id": self.ch, "youtube_video_id": os.urandom(4).hex(),
                                  "uploaded_at": db.iso_in(-minutes_ago * 60), "package": "{}",
                                  "created_at": db.iso(), "updated_at": db.iso()})

    def test_daily_cap_gap_and_quiet_hours(self):
        from datetime import datetime
        noon = datetime.now().astimezone().replace(hour=12, minute=0)
        with mock.patch.object(capacity, "offline_rate", lambda days=14: {h: (0.9 if 3 <= h < 7 else 0.1)
                                                                           for h in range(24)}):
            self.assertEqual(capacity.upload_wait(self.ch, noon), (0, ""))
            self.up(20)                                                    # 20 min ago: wait ~40 min
            wait, why = capacity.upload_wait(self.ch)
            self.assertTrue(30 * 60 < wait <= 41 * 60, wait)
            self.assertIn("apart", why)
            for m in (300, 600, 900):
                self.up(m)
            wait, why = capacity.upload_wait(self.ch)                      # 4 in 24 h: next slot when the oldest ages out
            self.assertGreater(wait, 8 * 3600)
            self.assertIn("daily limit", why)
            night = datetime.now().astimezone().replace(hour=4, minute=10)
            other = channels.create({"name": f"V{os.urandom(2).hex()}"}, 1)
            wait, why = capacity.upload_wait(other, night)
            self.assertAlmostEqual(wait / 60, 171, delta=1)               # 04:10 → 07:00, plus a minute
            self.assertIn("usually down", why)


if __name__ == "__main__":
    unittest.main()
