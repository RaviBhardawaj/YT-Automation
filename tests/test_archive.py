"""Archiving published videos: after N days on YouTube the video files leave this Mac; the record, YouTube link,
thumbnail, subtitles and reports stay. Unpublished videos, ones still in use, and long videos whose Shorts aren't
finished are never touched. Nothing external is called.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")
os.environ.setdefault("STUDIO_OFFLINE", "1")

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import storage  # noqa: E402


def prod(days_ago: float = 5, **kw) -> int:
    pid = db.insert("productions", {"format": "long", "title": "Old video", "status": "uploaded", "stage": "qa",
                                    "kind": "general", "workspace_id": 1, "youtube_video_id": "abc123",
                                    "uploaded_at": db.iso_in(-days_ago * 86400), "package": "{}",
                                    "created_at": db.iso(), "updated_at": db.iso(), **kw})
    d = storage.folder(pid)
    (d / "clips").mkdir(parents=True, exist_ok=True)
    for name, size in (("final.mp4", 5000), ("clips/c1.mp4", 3000), ("voice.wav", 2000), ("thumbnail.jpg", 300),
                       ("captions.srt", 50), ("report.md", 40), ("timeline.json", 60)):
        (d / name).write_bytes(b"x" * size)
    return pid


class ArchiveTests(unittest.TestCase):
    def test_files_go_records_stay(self):
        pid = prod()
        self.assertIn(pid, storage.archivable(3))
        freed = storage.archive(pid)
        self.assertEqual(freed, 10000)
        left = sorted(str(f.relative_to(storage.folder(pid))) for f in storage.folder(pid).rglob("*") if f.is_file())
        self.assertEqual(left, ["captions.srt", "report.md", "thumbnail.jpg", "timeline.json"])
        row = db.q1("SELECT archived_at, youtube_video_id, title FROM productions WHERE id=?", (pid,))
        self.assertTrue(row["archived_at"])
        self.assertEqual((row["youtube_video_id"], row["title"]), ("abc123", "Old video"))
        self.assertEqual(storage.archive(pid), 0)              # once only
        self.assertNotIn(pid, storage.archivable(3))

    def test_recent_unpublished_and_unfinished_shorts_are_left_alone(self):
        recent = prod(days_ago=1)
        unpublished = prod(status="awaiting_approval", youtube_video_id=None)
        parent = prod()
        prod(parent_id=parent, status="queued", youtube_video_id=None)          # its Short is still being made
        done_parent = prod()
        prod(parent_id=done_parent, status="uploaded")
        ids = storage.archivable(3)
        for pid in (recent, unpublished, parent):
            self.assertNotIn(pid, ids)
        self.assertIn(done_parent, ids)
        self.assertEqual(storage.archive(unpublished), 0)
        self.assertTrue((storage.folder(unpublished) / "final.mp4").exists())

    def test_housekeeping_archives_when_switched_on(self):
        from studio import settings, tasks
        pid = prod(days_ago=10)
        settings.set("storage.archive_after_days", 3)
        try:
            res = tasks._housekeeping({}, {})
        finally:
            settings.set("storage.archive_after_days", 0)
        self.assertGreaterEqual(res["archived"], 1)
        self.assertFalse((storage.folder(pid) / "final.mp4").exists())
        self.assertTrue(json.loads(json.dumps(res))["freed_mb"] >= 0)

    def test_reclaim_clears_published_and_rejected_of_any_age(self):
        published = prod(days_ago=0.1)
        rejected = prod(status="rejected", youtube_video_id=None)
        waiting = prod(status="awaiting_approval", youtube_video_id=None)
        parent = prod(status="rejected", youtube_video_id=None)
        prod(parent_id=parent, status="queued", youtube_video_id=None)          # its Short is still being made
        rows = {r["id"]: r for r in storage.reclaimable(1)}
        self.assertEqual(rows[published]["bytes"], 10000)
        self.assertIn(rejected, rows)
        for pid in (waiting, parent):
            self.assertNotIn(pid, rows)
        n, freed = storage.reclaim(1)
        self.assertGreaterEqual(n, 2)
        self.assertGreaterEqual(freed, 20000)
        for pid in (published, rejected):
            left = sorted(f.name for f in storage.folder(pid).rglob("*") if f.is_file())
            self.assertEqual(left, ["captions.srt", "report.md", "thumbnail.jpg", "timeline.json"])
            self.assertTrue(db.q1("SELECT archived_at FROM productions WHERE id=?", (pid,))["archived_at"])
        self.assertTrue((storage.folder(waiting) / "final.mp4").exists())
        self.assertTrue((storage.folder(parent) / "final.mp4").exists())
        self.assertEqual(storage.reclaimable(1), [])

    def test_storage_page_offers_the_button(self):
        from unittest import mock

        from fastapi.testclient import TestClient

        from studio import auth
        from studio.web import app as webapp
        pid = prod(status="rejected", youtube_video_id=None)
        if not db.q1("SELECT id FROM users WHERE email='reclaim-owner@test.local'"):
            auth.approve(auth.register("reclaim-owner@test.local", "a long enough password")["user_id"], 1)
        tok, _ = auth.login("reclaim-owner@test.local", "a long enough password")
        c = TestClient(webapp.app, base_url=f"http://127.0.0.1:{config.PORT}", follow_redirects=False)
        c.cookies.set("sid", tok)
        with mock.patch.object(webapp, "is_admin", lambda request: True):
            html = c.get("/storage").text
        self.assertIn("Published and rejected videos", html)
        self.assertIn("/api/storage/reclaim", html)
        storage.archive(pid)


if __name__ == "__main__":
    unittest.main()
