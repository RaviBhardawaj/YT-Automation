"""Database backups: a checked consistent copy, retention that never deletes the last good copy, corruption
detection, a restore drill that never touches the live database, and restore refused while the studio runs.
Everything happens in the test data folder.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
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
from studio import backup  # noqa: E402


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.patch = mock.patch.object(backup, "AUTO_DIR", self.dir)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_a_backup_is_checked_and_holds_the_data(self):
        res = backup.make()
        self.assertTrue(res["ok"])
        self.assertEqual(res["integrity"], "ok")
        self.assertTrue(Path(self.dir / res["file"]).exists())
        self.assertTrue(backup.verify(self.dir / res["file"])["ok"])
        self.assertEqual(backup.status()["last"]["file"], res["file"])

    def test_a_damaged_backup_is_detected(self):
        res = backup.make()
        f = self.dir / res["file"]
        data = bytearray(f.read_bytes())
        data[len(data) // 2: len(data) // 2 + 4096] = b"\\0" * 4096
        f.write_bytes(bytes(data))
        self.assertFalse(backup.verify(f)["ok"])                    # checksum no longer matches

    def test_retention_never_deletes_the_newest_good_copy(self):
        for i in range(12):                                         # 12 copies on 12 days
            f = self.dir / f"studio-202610{i + 1:02d}-030000.db"
            f.write_bytes(b"x")
            f.with_suffix(".json").write_text(json.dumps({"ok": True}))
        bad = self.dir / "studio-20261020-030000.db"                # newest, but it failed its check
        bad.write_bytes(b"x")
        bad.with_suffix(".json").write_text(json.dumps({"ok": False}))
        backup.prune()
        left = sorted(p.name for p in self.dir.glob("*.db"))
        self.assertIn("studio-20261012-030000.db", left)            # last known good kept
        self.assertLessEqual(len(left), backup.KEEP_DAILY + backup.KEEP_WEEKLY)
        self.assertNotIn("studio-20261020-030000.db", left)          # a failed copy is not kept

    def test_restore_drill_never_touches_the_live_database(self):
        backup.make()
        before = config.DB_PATH.stat().st_mtime_ns
        res = backup.restore_drill()
        self.assertTrue(res["ok"] and res["same_schema"])
        self.assertEqual(config.DB_PATH.stat().st_mtime_ns, before)

    def test_restore_is_refused_while_the_studio_runs(self):
        res = backup.make()
        with mock.patch.object(backup, "_studio_running", lambda: True), self.assertRaises(RuntimeError):
            backup.restore(self.dir / res["file"])


if __name__ == "__main__":
    unittest.main()
