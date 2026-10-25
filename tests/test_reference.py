"""YouTube-link requests. Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import auth, channels, jobs, worker  # noqa: E402
from studio.creative import reference  # noqa: E402

auth.ensure_default_workspace()
channels.ensure_default()


def fake(title, desc="", tags=(), cat=None, dur=600):
    return {"id": "abcdefghijk", "url": "https://www.youtube.com/watch?v=abcdefghijk", "title": title,
            "channel": "Someone", "description": desc, "tags": list(tags), "category_id": cat, "language": "en",
            "duration_s": dur, "via": "test"}


class LinkTests(unittest.TestCase):
    def test_finds_all_link_shapes(self):
        for u in ("https://www.youtube.com/watch?v=abcdefghijk&t=10s", "https://youtu.be/abcdefghijk?si=x",
                  "https://youtube.com/shorts/abcdefghijk", "https://m.youtube.com/watch?feature=share&v=abcdefghijk"):
            text, ids = reference.find(f"make this {u} please")
            self.assertEqual(ids, ["abcdefghijk"], u)
            self.assertNotIn("youtu", text)

    def test_link_subject_beats_the_nifty_channel(self):
        with mock.patch.object(reference, "fetch", return_value=fake("Haunted house horror short film", cat="1")), \
                mock.patch.object(worker, "enqueue", return_value=1):
            res = jobs.create(1, "https://youtu.be/abcdefghijk")
        p = res["plan"]
        self.assertEqual((p["content_type"], p["workflow"], p["decided_by"]), ("horror", "animated_story", "reference"))
        self.assertEqual(p["tone"], "")                        # not the NIFTY news-anchor tone
        self.assertIn("Haunted house horror short film", p["request"])
        self.assertEqual(db.q1("SELECT kind FROM productions WHERE id=?", (res["production_ids"][0],))["kind"], "story")

    def test_factual_link_researches_the_subject_not_the_url(self):
        with mock.patch.object(reference, "fetch", return_value=fake("How a Car Engine Works", cat="28")), \
                mock.patch.object(worker, "enqueue", return_value=1):
            res = jobs.create(1, "https://www.youtube.com/watch?v=abcdefghijk", fmt="long")
        t = db.q1("SELECT t.* FROM topics t JOIN productions p ON p.topic_id=t.id WHERE p.id=?", (res["production_ids"][0],))
        self.assertEqual((t["title"], t["search_query"], t["style"]), ("How a Car Engine Works",) * 2 + ("tech",))

    def test_creator_words_win_over_the_link(self):
        with mock.patch.object(reference, "fetch", return_value=fake("Nifty prediction for Monday", cat="27")):
            ref = reference.resolve("Make a funny cartoon about this https://youtu.be/abcdefghijk")
        plan = jobs.plan_for(1, ref["user_text"], None, hint=ref["content_type"])
        self.assertEqual((plan["content_type"], plan["decided_by"]), ("comedy", "request"))

    def test_category_used_when_text_has_no_clue(self):
        self.assertEqual(reference.content_type("", fake("Vlog #12", cat="23")), "comedy")
        self.assertEqual(reference.content_type("", fake("Episode 4", cat=None)), "documentary")

    def test_short_reference_makes_a_short(self):
        with mock.patch.object(reference, "fetch", return_value=fake("Ice cream prank funny", cat="24", dur=40)), \
                mock.patch.object(worker, "enqueue", return_value=1):
            res = jobs.create(1, "https://youtube.com/shorts/abcdefghijk")
        self.assertEqual(res["plan"]["format"], "short")

    def test_unreadable_link_is_reported(self):
        with mock.patch.object(reference, "fetch", side_effect=ValueError("that YouTube video doesn't exist")):
            with self.assertRaises(ValueError):
                jobs.create(1, "https://youtu.be/abcdefghijk")


if __name__ == "__main__":
    unittest.main()
