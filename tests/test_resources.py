"""Keeping the Mac usable: heavy local models wait when memory or disk is short (never start and swap), heavy tasks
share one lane, FFmpeg leaves cores free, the service runs at lower CPU priority.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins
os.environ.setdefault("STUDIO_OFFLINE", "1")              # never call the free image services

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import resources, worker  # noqa: E402
from studio.media import render  # noqa: E402


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"STUDIO_RESOURCE_GUARD": "1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_low_memory_makes_heavy_work_wait_not_fail(self):
        with mock.patch.object(resources, "available_memory_gb", lambda: 1.5), \
                mock.patch.object(resources, "free_disk_gb", lambda: 50.0):
            with self.assertRaises(resources.NotEnoughRoom) as cm:
                resources.ensure(4.0, "SadTalker")
        self.assertTrue(cm.exception.temporary)                  # the worker waits; no attempt is used up
        self.assertGreater(cm.exception.retry_after, 0)
        self.assertIn("1.5 GB", str(cm.exception))

    def test_a_nearly_full_disk_makes_heavy_work_wait(self):
        with mock.patch.object(resources, "available_memory_gb", lambda: 12.0), \
                mock.patch.object(resources, "free_disk_gb", lambda: 2.0), \
                self.assertRaises(resources.NotEnoughRoom) as cm:
            resources.ensure(1.0, "Rendering")
        self.assertIn("Storage page", str(cm.exception))

    def test_enough_room_starts_at_once(self):
        with mock.patch.object(resources, "available_memory_gb", lambda: 9.0), \
                mock.patch.object(resources, "free_disk_gb", lambda: 40.0):
            resources.ensure(4.0, "SadTalker")

    def test_sadtalker_is_guarded_before_it_starts(self):
        from studio.film_studio import sadtalker
        started = []
        with mock.patch.object(sadtalker, "enabled", lambda: True), \
                mock.patch.object(sadtalker, "_duration", lambda p: 3.0), \
                mock.patch.object(resources, "available_memory_gb", lambda: 1.0), \
                mock.patch.object(resources, "free_disk_gb", lambda: 40.0), \
                mock.patch.object(sadtalker.subprocess, "run", side_effect=lambda *a, **k: started.append(1)):
            with self.assertRaises(resources.NotEnoughRoom):
                sadtalker.talk(Path("/x.png"), Path("/x.wav"), Path(tempfile.mkdtemp()) / "o.mp4", size=(64, 64))
        self.assertEqual(started, [])

    def test_memory_reading_is_a_number(self):
        self.assertGreater(resources.available_memory_gb(), 0)


class SharingTests(unittest.TestCase):
    def test_heavy_task_types_run_one_at_a_time(self):
        for t in ("production", "film_shots_run", "film_ref", "film_short_make", "local_flux_setup", "voiceclone_setup"):
            self.assertEqual(worker.LANE_FOR.get(t), "media", t)

    def test_ffmpeg_leaves_cores_free(self):
        args = render._encode_args()
        threads = int(args[args.index("-threads") + 1])
        self.assertLess(threads, os.cpu_count() or 8)

    def test_the_service_runs_at_lower_priority(self):
        script = (config.ROOT / "scripts" / "install_launch_agent.sh").read_text()
        self.assertIn("<key>Nice</key><integer>10</integer>", script)


if __name__ == "__main__":
    unittest.main()
