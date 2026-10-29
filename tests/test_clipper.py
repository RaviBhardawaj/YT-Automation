"""AI Shorts clipper: moment finding, snapping, ranking, platform texts, a real render, downloads and tenant isolation.
Run: .venv/bin/python -m unittest tests.test_clipper -v"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins
os.environ.setdefault("STUDIO_OFFLINE", "1")              # never call the free image services

from studio import db, pipeline, uploads  # noqa: E402
from studio.editing import clipper, engine  # noqa: E402

from tests.test_saas import account, client, make_video, save  # noqa: E402


def transcript(n: int, step: float = 3.5, text: str = "this is line number {i} of the talk.") -> list[dict]:
    out = []
    for i in range(n):
        s = round(0.5 + i * step, 2)
        words = text.format(i=i).split()
        dur = step - 0.4
        out.append({"start": s, "end": round(s + dur, 2), "text": " ".join(words),
                    "words": [{"start": round(s + k * dur / len(words), 2), "end": round(s + (k + 1) * dur / len(words), 2),
                               "word": w} for k, w in enumerate(words)]})
    return out


class SnapAndPickTests(unittest.TestCase):
    def test_snap_uses_whole_lines_and_fits_length(self):
        tr = transcript(40)
        s, e = clipper.snap(20.0, 47.0, tr, 30, 60, 200)
        starts = {x["start"] for x in tr}
        ends = {x["end"] for x in tr}
        self.assertIn(round(s + 0.12, 2), starts)              # begins where a line begins (small lead-in)
        self.assertIn(round(e - 0.25, 2), ends)                 # ends where a line ends (small tail)
        self.assertTrue(30 <= (e - s) - 0.37 <= 60)

    def test_snap_cuts_a_single_long_line_on_a_word(self):
        tr = transcript(1, step=80.0, text=" ".join(f"w{i}" for i in range(60)))
        s, e = clipper.snap(0, 80, tr, 15, 30, 100)
        self.assertLessEqual(e - s, 30.5)
        self.assertIn(round(e - 0.25, 2), {w["end"] for w in tr[0]["words"]})

    def test_snap_rejects_too_short_tail(self):
        tr = transcript(3)
        self.assertIsNone(clipper.snap(0, 10, tr, 30, 60, 11))

    def test_pick_is_best_first_without_overlap(self):
        cands = [{"start": 0, "end": 40, "score": 90}, {"start": 10, "end": 50, "score": 80},
                 {"start": 60, "end": 100, "score": 70}, {"start": 95, "end": 130, "score": 60}]
        got = clipper.pick(cands, 3)
        self.assertEqual([c["score"] for c in got], [90, 70, 60])

    def test_windows_cover_everything_and_overlap(self):
        tr = transcript(400, text="a fairly long spoken sentence with many words in it number {i} here")
        wins = clipper.windows(tr, max_chars=3000)
        self.assertGreater(len(wins), 3)
        self.assertEqual(wins[0][0], tr[0])
        self.assertEqual(wins[-1][-1], tr[-1])
        for a, b in zip(wins, wins[1:]):
            self.assertLess(b[0]["start"], a[-1]["end"])        # neighbouring windows overlap

    def test_auto_count_and_language(self):
        self.assertEqual(clipper.auto_count(60 * 60), 10)
        self.assertEqual(clipper.auto_count(10 * 60), 4)
        self.assertEqual(clipper.auto_count(40, "medium"), 1)
        self.assertEqual(clipper.spoken_language([{"text": "नमस्ते दोस्तों आज हम बात करेंगे"}]), "Hindi")
        self.assertEqual(clipper.spoken_language([{"text": "hello friends"}]), "English")

    def test_heuristic_prefers_hooks_and_handles_silence(self):
        tr = transcript(30)
        tr[10]["text"] = "Why do most people lose money in the first year?"
        best = max(clipper.heuristic(tr, 120, 15, 30), key=lambda c: c["score"])
        self.assertEqual(best["start"], round(tr[10]["start"] - 0.12, 2))
        silent = clipper.heuristic([], 120, 30, 60, cuts=[31.0, 62.5])
        self.assertTrue(silent and all(30 <= c["end"] - c["start"] <= 60 for c in silent))


class MomentAndCopyTests(unittest.TestCase):
    def test_ai_moments_are_snapped_ranked_and_topped_up(self):
        tr = transcript(60)
        media = {"id": 1, "original_name": "talk.mp4", "duration": 220.0, "transcript": tr}
        mk = lambda s, e, h: clipper.Moment(start=s, end=e, title=f"T{s}", hook="hook", reason="good",   # noqa: E731
                                            keywords=["line"], scores=clipper.Scores(hook=h, value=7, emotion=6, standalone=8))
        with mock.patch.object(clipper, "_ask", return_value=[mk(20.3, 55, 9), mk(21, 50, 5), mk(120, 160, 7)]):
            got, meta = clipper.find_moments(media, [], count=3, length="medium", language="English")
        self.assertEqual(len(got), 3)
        self.assertEqual([g["picked_by"] for g in got[:2]], ["ai", "ai"])
        self.assertGreater(got[0]["score"], got[1]["score"])
        self.assertEqual(got[2]["picked_by"], "rules")          # the overlapping AI pick was dropped; rules filled in
        self.assertEqual(meta["picked_by"], "ai+rules")

    def test_ai_offline_still_finds_moments(self):
        media = {"id": 1, "original_name": "talk.mp4", "duration": 220.0, "transcript": transcript(60)}
        with mock.patch("studio.ai.generate", side_effect=RuntimeError("offline")):
            got, meta = clipper.find_moments(media, [], count=4, length="short", language="English")
        self.assertEqual(len(got), 4)
        self.assertEqual(meta["picked_by"], "rules")
        self.assertTrue(meta["ai_errors"])

    def test_platform_texts_respect_limits(self):
        cp = clipper.PlatformCopy(youtube_title="A" * 150, youtube_description="Desc", instagram_caption="Cap " * 700,
                                  facebook_caption="FB", hashtags=[f"#tag{i}" for i in range(12)], tags=["x" * 40] * 30)
        f = clipper._final({"title": "t", "hook": "h", "reason": "r", "keywords": ["money"]}, cp)
        self.assertLessEqual(len(f["youtube"]["title"]), 100)
        self.assertIn("#Shorts", f["youtube"]["description"])
        self.assertLessEqual(f["instagram"]["caption"].count("#"), clipper.IG_HASHTAGS)
        self.assertLessEqual(len(f["instagram"]["caption"]), 2200)
        self.assertLessEqual(sum(len(t) + 1 for t in f["youtube"]["tags"]), 451)
        self.assertEqual(f["facebook"]["caption"].count("#"), clipper.FB_HASHTAGS)
        plain = clipper._final({"title": "Money tip", "hook": "Save more", "reason": "clear tip", "keywords": ["money"]}, None)
        self.assertEqual(plain["youtube"]["title"], "Money tip")
        self.assertIn("#money", plain["instagram"]["caption"])


class EngineHelperTests(unittest.TestCase):
    def test_split_at_shot_changes(self):
        self.assertEqual(engine.split_at([(0, 10)], [3.0, 3.5, 9.6]), [(0, 3.0), (3.0, 10)])

    def test_one_crop_per_shot(self):
        # shots: 0-10 (face at 0.3), 10-20 (no face found), 20-30 (face at 0.7); pauses split the pieces further
        xs = iter([0.3, None, 0.7])
        with mock.patch.object(engine, "face_x", side_effect=lambda *_: next(xs)):
            parts, crops = engine.shot_crops("v.mp4", 0, 30, [(0, 4), (5, 14), (15, 30)], [10.0, 20.0])
        self.assertEqual(parts, [(0, 4), (5, 10.0), (10.0, 14), (15, 20.0), (20.0, 30)])
        self.assertEqual(crops, [0.3, 0.3, 0.7, 0.7, 0.7])      # the face-less shot uses the usual spot (median)
        with mock.patch.object(engine, "face_x", return_value=None):
            self.assertEqual(engine.shot_crops("v.mp4", 0, 10, [(0, 10)], [])[1], [None])

    def test_download_names_keep_hindi(self):
        from studio.web.app import safe_filename
        self.assertEqual(safe_filename("Breakout और Rejection को कैसे समझें?", "x"), "Breakout और Rejection को कैसे समझें")
        self.assertEqual(safe_filename('../a/b:*?"<>|', "x"), "ab")
        self.assertEqual(safe_filename("", "video-9"), "video-9")

    def test_no_face_in_test_pattern(self):
        src = make_video(Path(tempfile.mkdtemp()) / "pattern.mp4", 3)
        self.assertIsNone(engine.face_x(str(src), 0, 3))


class ClipperJobTests(unittest.TestCase):
    """A real job: upload → transcript → clipper plan (AI offline) → face/fit crop render → clips, ZIP, pages."""

    def test_long_video_to_shorts_with_downloads(self):
        from studio.editing import workflow
        ws, tok = account("clipper@test.local")
        src = make_video(Path(tempfile.mkdtemp()) / "podcast.mp4", 70)
        m = save(ws, "podcast.mp4", src.read_bytes())
        db.update("media", m["id"], {"transcript": json.dumps(transcript(19))})
        c = client(tok)
        r = c.post("/api/create", json={"media_ids": [m["id"]], "channel_id": None, "no_upload": True, "request": "",
                                        "shorts": {"clips": 2, "length": "short", "language": "auto", "captions": True,
                                                   "headline": True, "face_crop": True, "remove_pauses": True}})
        self.assertTrue(r.json()["ok"], r.text)
        pid = r.json()["production_ids"][0]
        self.assertEqual(r.json()["next"], f"/shorts/{pid}")
        with mock.patch("studio.ai.generate", side_effect=RuntimeError("AI offline in tests")), \
                mock.patch("studio.worker.enqueue", return_value=1):
            for st in workflow.STAGES:
                pipeline.save(pid, stage=st)
                workflow.STAGE_FN[st](pipeline.load(pid))
        p = pipeline.load(pid)
        self.assertEqual(p["package"]["planner"]["planner"], "clipper")
        self.assertEqual(len(p["package"]["clips"]), 2)
        outs = p["episode"]["outputs"]
        self.assertTrue(all(o["passed"] for o in outs), [o["checks"] for o in outs])
        for o in outs:
            self.assertEqual((o["info"]["width"], o["info"]["height"]), (1080, 1920))
            self.assertTrue(15 <= o["seconds"] <= 31, o["seconds"])
            self.assertTrue(Path(o["details"]).is_file() and Path(o["clean_video"]).is_file())
        kid = db.q1("SELECT * FROM productions WHERE parent_id=? AND kind='edit_clip'", (pid,))
        self.assertEqual(kid["status"], "awaiting_approval")    # never auto-published
        self.assertIn("platforms", json.loads(kid["package"]))
        self.assertEqual(p["status"], "awaiting_approval")
        self.assertTrue(uploads.verify_intact(uploads.get(m["id"], ws)))

        page = c.get(f"/shorts/{pid}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Download all 2 (ZIP)", page.text)
        self.assertIn("Instagram Reels", page.text)
        self.assertEqual(c.get(f"/download/{kid['id']}/details").status_code, 200)
        z = c.get(f"/download/{pid}/pack")
        self.assertEqual(z.status_code, 200)
        names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
        self.assertIn("clips.csv", names)
        self.assertEqual(sum(n.endswith("/short.mp4") for n in names), 2)
        self.assertEqual(sum(n.endswith("/details.txt") for n in names), 2)
        self.assertFalse(list(Path(p["video_path"]).parent.parent.glob("shorts-*.zip")))   # temp ZIP removed
        self.assertEqual(c.get("/shorts").status_code, 200)

        _, other = account("clipper-other@test.local")
        o = client(other)
        self.assertEqual(o.get(f"/shorts/{pid}").status_code, 404)
        self.assertEqual(o.get(f"/download/{pid}/pack").status_code, 404)

    def test_needs_exactly_one_long_enough_video(self):
        ws, tok = account("clipper2@test.local")
        a = save(ws, "a.mp4", make_video(Path(tempfile.mkdtemp()) / "a.mp4", 25).read_bytes())
        b = save(ws, "b.mp4", make_video(Path(tempfile.mkdtemp()) / "b.mp4", 5).read_bytes())
        c = client(tok)
        two = c.post("/api/create", json={"media_ids": [a["id"], b["id"]], "shorts": {}}).json()
        self.assertIn("exactly one video", two["error"])
        short = c.post("/api/create", json={"media_ids": [b["id"]], "shorts": {}}).json()
        self.assertIn("shorter than 20 seconds", short["error"])


if __name__ == "__main__":
    unittest.main()
