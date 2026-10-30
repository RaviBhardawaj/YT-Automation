"""Lip-synced talking clips (LTX-2 on a free ZeroGPU Space): pieces chained from the last frame, trimmed to the
voice and joined; daily limit; quota shared with the Wan clips; honest fallbacks for news presenters and film
dialogue shots. The Space is faked in-process; nothing real is called.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins
os.environ.setdefault("STUDIO_OFFLINE", "1")              # never call the free image services

import httpx  # noqa: E402
from PIL import Image  # noqa: E402

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import channels, settings  # noqa: E402
from studio.creative import news  # noqa: E402
from studio.film_studio import sadtalker, talking, zerogpu  # noqa: E402

TMP = Path(tempfile.mkdtemp())
CLIP = TMP / "piece.mp4"
subprocess.run([config.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=448x256:rate=24:d=3.04",
                "-pix_fmt", "yuv420p", str(CLIP)], check=True)
PIC = TMP / "face.png"
Image.new("RGB", (896, 512), "navy").save(PIC)
INFO = {"named_endpoints": {"/generate_video": {"parameters": [{"parameter_name": n} for n in talking.KNOWN]}}}


def voice(seconds: float) -> Path:
    out = TMP / f"voice_{seconds}.wav"
    subprocess.run([config.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}",
                    str(out)], check=True)
    return out


def fake_space(stream: str = 'event: complete\ndata: [{"video": {"url": "https://s/f.mp4"}}]\n\n'):
    calls = {"call": 0, "uploads": []}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/info"):
            return httpx.Response(200, json=INFO)
        if path.endswith("/upload"):
            calls["uploads"].append(req.content[:20000])
            return httpx.Response(200, json=["/tmp/a.png", "/tmp/a.wav"])
        if path.endswith("/call/generate_video"):
            calls["call"] += 1
            return httpx.Response(200, json={"event_id": "e"})
        if "/call/generate_video/" in path:
            return httpx.Response(200, text=stream)
        return httpx.Response(200, content=CLIP.read_bytes())
    client = lambda: httpx.Client(transport=httpx.MockTransport(handler))  # noqa: E731
    return mock.patch.object(zerogpu, "_client", client), calls


def token(yes: bool = True):
    return mock.patch.object(settings, "has_secret", lambda name: yes if name == "hf_token" else False)


def reset() -> None:
    talking._contract.clear()
    settings.set("film_studio.zerogpu_quota_until", "")
    settings.set("film_studio.talking", True)
    settings.set("film_studio.talking_clips_per_day", 10)        # the tests use more than the free default
    db.ex("DELETE FROM ai_usage WHERE provider=?", (talking.PROVIDER,))


class TalkTests(unittest.TestCase):
    def setUp(self):
        reset()

    def test_a_7_second_line_is_three_chained_pieces_trimmed_to_the_voice(self):
        patch, calls = fake_space()
        out = TMP / "talk7.mp4"
        with token(), patch:
            info = talking.talk(PIC, voice(7.0), "speaks", out, hold=0.5)
        self.assertEqual((info["pieces"], calls["call"]), (3, 3))
        self.assertAlmostEqual(info["video"]["duration"], 7.5, delta=0.25)       # voice + hold, not 3 × 3.04
        self.assertEqual(talking.budget()["used"], 3)
        self.assertIn(b"last00.png", b"".join(calls["uploads"][1:2]))           # piece 2 starts from piece 1's end

    def test_limits_and_too_long_lines_are_refused_without_calling(self):
        patch, calls = fake_space()
        with token(), patch:
            with self.assertRaises(talking.TooLong):
                talking.talk(PIC, voice(13.0), "x", TMP / "long.mp4", max_pieces=4)
            settings.set("film_studio.talking_clips_per_day", 1)
            with self.assertRaises(zerogpu.SpaceError) as cm:
                talking.talk(PIC, voice(4.0), "x", TMP / "limit.mp4")
            self.assertEqual(cm.exception.kind, "limit")
        self.assertEqual(calls["call"], 0)

    def test_spent_gpu_minutes_are_remembered_for_wan_too(self):
        patch, _ = fake_space(stream='event: error\ndata: {"error": "You have exceeded your free ZeroGPU quota. '
                              'Try again in 19:05:15."}\n\n')
        with token(), patch, self.assertRaises(zerogpu.QuotaExceeded):
            talking.talk(PIC, voice(2.0), "x", TMP / "q.mp4")
        left = (zerogpu.quota_until() - db.utcnow()).total_seconds()
        self.assertGreater(left, 19 * 3600)                          # the Space's own reset time, not a guess
        with token():
            self.assertIn("used up", talking.available(2.0))


class PresenterTalkTests(unittest.TestCase):
    def setUp(self):
        reset()

    def _news(self):
        cid = channels.create({"name": f"Talk news {os.urandom(2).hex()}", "workflow": "factual", "styles": ["news"]}, 1)
        pid = db.insert("productions", {"format": "long", "title": "t", "status": "rendering", "stage": "render",
                                        "kind": "general", "workspace_id": 1, "channel_id": cid, "package": "{}",
                                        "created_at": db.iso(), "updated_at": db.iso()})
        work = TMP / f"p{pid}"
        (work / "visuals").mkdir(parents=True)
        for i, prov in enumerate((news.PRESENTER_PROVIDER, "pexels")):
            db.insert("assets", {"production_id": pid, "scene_index": i, "kind": "image", "provider": prov,
                                 "provider_id": f"x{i}", "local_path": str(PIC), "created_at": db.iso()})
        assets = {a["scene_index"]: a for a in db.q("SELECT * FROM assets WHERE production_id=?", (pid,))}
        tl = {"scenes": [{"wav": str(voice(4.0)), "duration": 4.0}, {"wav": str(voice(3.0)), "duration": 3.0}]}
        return {"id": pid, "format": "long", "channel_id": cid}, assets, tl, work

    def test_presenter_scene_becomes_a_talking_video_other_scenes_untouched(self):
        p, assets, tl, work = self._news()
        patch, _ = fake_space()
        with token(), patch:
            self.assertEqual(news.make_presenter_talk(p, assets, tl, 0.5, work), 1)
        row = db.q1("SELECT kind, local_path FROM assets WHERE production_id=? AND scene_index=0", (p["id"],))
        self.assertEqual(row["kind"], "video")
        self.assertTrue(Path(row["local_path"]).exists())
        self.assertEqual(assets[1]["kind"], "image")

    def test_no_token_and_no_sadtalker_keeps_the_still_picture(self):
        p, assets, tl, work = self._news()
        with token(False), mock.patch.object(sadtalker, "installed", lambda: False):
            self.assertEqual(news.make_presenter_talk(p, assets, tl, 0.5, work), 0)
        self.assertEqual(assets[0]["kind"], "image")

    def test_used_up_gpu_falls_back_to_sadtalker_on_the_mac(self):
        p, assets, tl, work = self._news()
        settings.set("film_studio.zerogpu_quota_until", db.iso_in(3600))
        calls = []

        def fake_sad(image, audio, out, *, size, hold=0.0):
            calls.append((size, hold))
            out.write_bytes(CLIP.read_bytes())
            return {"provider": sadtalker.PROVIDER, "model": "SadTalker 256 (on this Mac)", "pieces": 1,
                    "speech_seconds": 4.0, "wall_seconds": 1.0,
                    "video": {"width": 896, "height": 512, "duration": 4.5}}
        with token(), mock.patch.object(sadtalker, "talk", fake_sad):
            self.assertEqual(news.make_presenter_talk(p, assets, tl, 0.5, work), 1)
        row = db.q1("SELECT kind, license FROM assets WHERE production_id=? AND scene_index=0", (p["id"],))
        self.assertEqual((row["kind"], calls[0][0]), ("video", (896, 512)))
        self.assertIn("SadTalker", row["license"])


class SadTalkerTests(unittest.TestCase):
    def test_not_installed_is_refused_without_running_anything(self):
        settings.set("film_studio.sadtalker_dir", str(TMP / "nowhere"))
        wav = voice(2.0)
        try:
            with mock.patch.object(sadtalker.subprocess, "run") as run, self.assertRaises(sadtalker.NotInstalled):
                sadtalker.talk(PIC, wav, TMP / "s.mp4", size=(896, 512))
            run.assert_not_called()
        finally:
            settings.set("film_studio.sadtalker_dir", "")


if __name__ == "__main__":
    unittest.main()
