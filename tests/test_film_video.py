"""Real-video generation in the Film Studio: budget semantics, shot selection, provider dispatch, the Hugging Face
Space adapter's error handling and output validation, resume, honest fallbacks and motion diagnostics.
External services are mocked (an in-process fake Space); nothing real is called.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
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

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import settings  # noqa: E402
from studio.film_studio import generate, providers, qa, zerogpu  # noqa: E402

TMP = Path(tempfile.mkdtemp())


def clip(name: str, src: str = "testsrc=size=320x180:rate=16", seconds: float = 2.0) -> Path:
    out = TMP / name
    subprocess.run([config.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"{src}:d={seconds}",
                    "-pix_fmt", "yuv420p", str(out)], check=True)
    return out


MOVING = clip("moving.mp4")
STILL = clip("still.mp4", "color=c=gray:size=320x180:rate=16")
INFO = {"named_endpoints": {"/generate_video": {"parameters": [{"parameter_name": n} for n in zerogpu.PARAMS]}}}


def space(stream: str = 'event: complete\ndata: [{"video": {"url": "https://space/file.mp4"}}, 7]\n\n',
          video: bytes | None = None, fail: dict[str, int] | None = None, info: dict | None = None):
    """A fake Gradio Space: fail={'upload': 401} makes that step answer with that status."""
    fail = fail or {}
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        seen.append(path)
        step = ("info" if path.endswith("/info") else "upload" if path.endswith("/upload") else
                "stream" if "/call/generate_video/" in path else "call" if path.endswith("/call/generate_video")
                else "download")
        if step in fail:
            return httpx.Response(fail[step], text="nope")
        if step == "info":
            return httpx.Response(200, json=info or INFO)
        if step == "upload":
            return httpx.Response(200, json=["/tmp/gradio/x.png"])
        if step == "call":
            return httpx.Response(200, json={"event_id": "e1"})
        if step == "stream":
            return httpx.Response(200, text=stream)
        return httpx.Response(200, content=video if video is not None else MOVING.read_bytes())
    client = lambda: httpx.Client(transport=httpx.MockTransport(handler))  # noqa: E731
    return mock.patch.object(zerogpu, "_client", client), seen


def has_token(yes: bool = True):
    return mock.patch.object(settings, "has_secret", lambda name: yes if name == "hf_token" else False)


def reset() -> None:
    zerogpu._contract.clear()
    settings.set("film_studio.zerogpu_quota_until", "")
    db.ex("DELETE FROM ai_usage WHERE provider='hf_zerogpu'")
    db.ex("DELETE FROM settings WHERE key='film_studio.video_clips_per_run'")
    settings._cache.pop("film_studio.video_clips_per_run", None)
    settings.set("film_studio.video_shots", "key")


def shot(**kw):
    s = {"id": 1, "scene_no": 2, "shot_no": 3, "shot_type": "medium", "characters": ["A"], "action": "",
         "dialogue": [], "camera": {"movement": "static"}, "lighting": "evening light", "target_seconds": 4.0,
         "continuity": {}}
    s.update(kw)
    return s


class BudgetTests(unittest.TestCase):
    def setUp(self):
        reset()

    def test_unset_uses_the_default_explicit_zero_means_none_positive_is_respected(self):
        b = providers.video_budget()
        self.assertEqual((b["limit"], b["source"]), (providers.DEFAULT_CLIPS_PER_DAY, "default"))
        settings.set("film_studio.video_clips_per_run", 0)
        b = providers.video_budget()
        self.assertEqual((b["limit"], b["left"], b["source"]), (0, 0, "set"))
        settings.set("film_studio.video_clips_per_run", 2)
        self.assertEqual(providers.video_budget()["limit"], 2)

    def test_invalid_value_warns_and_falls_back(self):
        settings.set("film_studio.video_clips_per_run", "lots")
        b = providers.video_budget()
        self.assertEqual(b["limit"], providers.DEFAULT_CLIPS_PER_DAY)
        self.assertIn("not a valid clip limit", b["warning"])

    def test_used_clips_are_counted_from_the_usage_log_across_runs(self):
        settings.set("film_studio.video_clips_per_run", 2)
        for _ in range(2):
            zerogpu._record(True, seconds=30)
        zerogpu._record(False, "error")                     # a failed call does not use a clip
        b = providers.video_budget()
        self.assertEqual((b["used"], b["left"]), (2, 0))
        with has_token():
            self.assertFalse(providers.health("hf_zerogpu")["available"])
            self.assertIn("limit reached", providers.health("hf_zerogpu")["detail"])


class SelectionTests(unittest.TestCase):
    def setUp(self):
        reset()

    def test_modes(self):
        talk = shot(dialogue=[{"name": "A", "text": "hi"}])
        settings.set("film_studio.video_shots", "none")
        self.assertFalse(generate.wants_video(shot(action="runs to the tractor")))
        settings.set("film_studio.video_shots", "all")
        self.assertTrue(generate.wants_video(talk))
        settings.set("film_studio.video_shots", "key")
        self.assertTrue(generate.wants_video(shot(action="jumps onto the tractor")))   # physical action
        self.assertTrue(generate.wants_video(shot(shot_no=1, dialogue=[{"name": "A", "text": "x"}])))  # opening
        self.assertTrue(generate.wants_video(shot(shot_type="insert", dialogue=[{"name": "A", "text": "x"}])))
        self.assertFalse(generate.wants_video(talk))                                    # ordinary talk shot

    def test_physical_actions_come_first_then_openings(self):
        order = sorted([shot(shot_type="insert", dialogue=[{"name": "A", "text": "x"}]),
                        shot(scene_no=1, shot_no=1, dialogue=[{"name": "A", "text": "x"}]),
                        shot(action="pushes the cart")], key=generate.video_priority)
        self.assertEqual(order[0]["action"], "pushes the cart")
        self.assertEqual((order[1]["scene_no"], order[1]["shot_no"]), (1, 1))

    def test_motion_prompt_is_specific(self):
        p = generate.motion_prompt(shot(action="Balbir slaps the cards on the table",
                                        camera={"movement": "slow push_in"}), {"location": "village courtyard"})
        self.assertIn("slaps the cards", p)
        self.assertIn("slow push in", p)
        self.assertIn("breeze", p)                                    # outdoor environment
        self.assertIn("same faces", p)
        locked = generate.motion_prompt(shot(dialogue=[{"name": "Pappi", "text": "x"}]), {"location": "kitchen"})
        self.assertIn("locked off", locked)
        self.assertIn("restrained", locked)


class DispatchTests(unittest.TestCase):
    def setUp(self):
        reset()

    def test_local_wan_is_never_available_even_with_a_folder(self):
        (config.DATA_DIR / "models" / "wan2.2-ti2v-5b").mkdir(parents=True, exist_ok=True)
        self.assertFalse(providers.health("wan22_local")["available"])
        with self.assertRaises(providers.NoFreeProvider):
            providers.run_video("wan22_local", MOVING, "x", TMP / "w.mp4", seconds=2)

    def test_paid_video_is_blocked_at_zero_budget(self):
        with self.assertRaises(providers.PaidBlocked):
            providers.run_video("gemini_omni", MOVING, "x", TMP / "g.mp4", seconds=2)

    def test_the_chosen_provider_is_the_one_executed(self):
        calls = []
        with mock.patch.object(zerogpu, "generate", lambda *a, **k: calls.append("hf") or
                               {"space": "s", "file": "f", "video": {}}):
            info = providers.run_video("hf_zerogpu", MOVING, "x", TMP / "h.mp4", seconds=2)
        self.assertEqual((calls, info["provider"]), (["hf"], "hf_zerogpu"))
        self.assertFalse(info["refs_supported"])                      # one start image only — said honestly


class SpaceAdapterTests(unittest.TestCase):
    def setUp(self):
        reset()

    def go(self, **kw):
        patch, seen = space(**kw)
        with has_token(), patch:
            return zerogpu.generate(MOVING, "walks", TMP / "out.mp4", seconds=2), seen

    def test_success_validates_the_file_and_logs_usage(self):
        info, seen = self.go()
        self.assertTrue(info["video"]["decodes"])
        self.assertGreater(info["video"]["duration"], 1)
        self.assertEqual(db.q1("SELECT COUNT(*) AS n FROM ai_usage WHERE provider='hf_zerogpu' AND ok=1")["n"], 1)
        self.assertIn("/gradio_api/info", seen[0])                    # the published contract was checked first

    def test_missing_token(self):
        with has_token(False), self.assertRaises(zerogpu.SpaceError) as cm:
            zerogpu.generate(MOVING, "x", TMP / "o.mp4")
        self.assertEqual(cm.exception.kind, "not_configured")

    def test_auth_rate_limit_and_server_errors_are_not_quota(self):
        for status, kind in ((401, "auth"), (403, "auth"), (429, "rate_limited"), (502, "server")):
            reset()
            with self.subTest(status=status), self.assertRaises(zerogpu.SpaceError) as cm:
                self.go(fail={"upload": status})
            self.assertEqual(cm.exception.kind, kind)
            self.assertIsNone(zerogpu.quota_until())

    def test_quota_is_detected_remembered_and_not_asked_again(self):
        with self.assertRaises(zerogpu.QuotaExceeded):
            self.go(stream="event: error\ndata: null\n\n")
        self.assertIsNotNone(zerogpu.quota_until())
        patch, seen = space()
        with has_token(), patch, self.assertRaises(zerogpu.QuotaExceeded):
            zerogpu.generate(MOVING, "x", TMP / "o2.mp4")
        self.assertEqual(seen, [])                                    # no network call while the quota is spent
        with has_token():
            self.assertFalse(providers.health("hf_zerogpu")["available"])

    def test_malformed_answers_and_changed_interface(self):
        with self.assertRaises(zerogpu.SpaceError) as cm:
            self.go(stream="event: complete\ndata: {not json\n\n")
        self.assertEqual(cm.exception.kind, "malformed")
        reset()
        with self.assertRaises(zerogpu.SpaceError) as cm:
            self.go(info={"named_endpoints": {"/generate_video": {"parameters": [{"parameter_name": "image_v2"}]}}})
        self.assertEqual(cm.exception.kind, "interface")

    def test_http_200_with_a_broken_file_is_not_a_video(self):
        with self.assertRaises(zerogpu.SpaceError) as cm:
            self.go(video=b"<html>not a video</html>" * 100)
        self.assertEqual(cm.exception.kind, "invalid_video")
        self.assertFalse((TMP / "out.mp4").exists())
        self.assertEqual(db.q1("SELECT COUNT(*) AS n FROM ai_usage WHERE provider='hf_zerogpu' AND ok=1")["n"], 0)


class MotionTests(unittest.TestCase):
    def test_static_clips_are_flagged_moving_ones_are_not(self):
        self.assertTrue(qa.motion_stats(STILL)["static"])
        m = qa.motion_stats(MOVING)
        self.assertTrue(m["checked"])
        self.assertFalse(m["static"])

    def test_resume_reuses_a_valid_downloaded_clip(self):
        out = TMP / "resume.mp4"
        out.write_bytes(MOVING.read_bytes())
        self.assertTrue(generate._resume_clip(out))
        broken = TMP / "broken.mp4"
        broken.write_bytes(b"x" * 4000)
        self.assertIsNone(generate._resume_clip(broken))
        self.assertFalse(broken.exists())                             # a broken leftover is removed, then redone


if __name__ == "__main__":
    unittest.main()
