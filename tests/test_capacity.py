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


if __name__ == "__main__":
    unittest.main()
