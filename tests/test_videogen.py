"""AI text-to-video (Gemini Omni Flash) — Google is mocked: no money is spent in tests.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import base64
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins

from studio import config, db, settings  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import pipeline  # noqa: E402
from studio.ai import videogen  # noqa: E402

TMP = Path(tempfile.mkdtemp())
CLIP = TMP / "clip.mp4"
subprocess.run([config.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                "testsrc2=s=640x360:r=24:d=3", "-f", "lavfi", "-i", "sine=d=3", "-shortest", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", str(CLIP)], check=True)


def google_ok(tokens: int | None = 100_000):
    body = {"status": "completed", "object": "interaction",
            "steps": [{"type": "model_output", "content": [{"type": "video", "mime_type": "video/mp4",
                                                            "data": base64.b64encode(CLIP.read_bytes()).decode()}]}]}
    if tokens:
        body["usage"] = {"total_output_tokens": tokens}
    return mock.Mock(status_code=200, json=lambda: body, text="")


class VideoGenTests(unittest.TestCase):
    def setUp(self):
        db.ex("DELETE FROM ai_usage WHERE provider=?", (videogen.PROVIDER,))
        settings.set("videogen.enabled", True)
        settings.set("videogen.monthly_cap_usd", 5.0)
        self.key = mock.patch.object(settings, "secret", side_effect=lambda n: "test-key" if n == "gemini_api_key" else None)
        self.key.start()

    def tearDown(self):
        self.key.stop()
        settings.set("videogen.enabled", False)

    def test_off_by_default_and_refuses_when_off(self):
        self.assertFalse(settings.DEFAULTS["videogen.enabled"])
        settings.set("videogen.enabled", False)
        with mock.patch("httpx.post") as post, self.assertRaises(videogen.VideoGenError):
            videogen.generate("a cat", "16:9", TMP / "off.mp4")
        post.assert_not_called()

    def test_generates_and_records_actual_cost_from_tokens(self):
        out = TMP / "a.mp4"
        with mock.patch("httpx.post", return_value=google_ok(100_000)) as post:
            res = videogen.generate("a rocket launch at dawn", "9:16", out)
        sent = post.call_args.kwargs["json"]
        self.assertEqual((sent["model"], sent["response_format"]["aspect_ratio"]), ("gemini-omni-1.1-flash", "9:16"))
        self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], "test-key")
        self.assertTrue(out.is_file())
        self.assertEqual((res["basis"], res["cost_usd"]), ("actual", 1.75))     # 100k tokens × $17.50/M
        self.assertEqual(videogen.month_spent(), 1.75)

    def test_estimate_basis_when_google_reports_no_usage(self):
        with mock.patch("httpx.post", return_value=google_ok(None)):
            res = videogen.generate("waves", "16:9", TMP / "b.mp4")
        self.assertEqual(res["basis"], "estimate")
        self.assertAlmostEqual(res["cost_usd"], round(res["duration"] * 0.10, 4), places=3)

    def test_retry_reuses_the_file_no_second_charge(self):
        out = TMP / "c.mp4"
        with mock.patch("httpx.post", return_value=google_ok()) as post:
            videogen.generate("x", "16:9", out)
            again = videogen.generate("x", "16:9", out)
        self.assertEqual(post.call_count, 1)
        self.assertTrue(again["reused"])

    def test_monthly_cap_blocks_before_calling_google(self):
        settings.set("videogen.monthly_cap_usd", 0.5)                      # one clip ≈ $0.80
        with mock.patch("httpx.post") as post, self.assertRaises(videogen.VideoGenError) as e:
            videogen.generate("x", "16:9", TMP / "d.mp4")
        post.assert_not_called()
        self.assertIn("cap", str(e.exception))

    def test_blocked_prompt_reports_clearly(self):
        empty = mock.Mock(status_code=200, json=lambda: {"steps": []}, text="")
        with mock.patch("httpx.post", return_value=empty), self.assertRaises(videogen.VideoGenError) as e:
            videogen.generate("x", "16:9", TMP / "e.mp4")
        self.assertIn("no video", str(e.exception))

    def test_estimate(self):
        est = videogen.estimate(3)
        self.assertEqual((est["clips"], est["seconds"], est["usd"]), (3, 24, 2.4))
        self.assertTrue(est["fits_cap"])

    def test_check_access_explains_billing(self):
        listing = mock.Mock(status_code=200, json=lambda: {"models": [{"name": "models/gemini-2.5-flash"}]})
        with mock.patch("httpx.get", return_value=listing):
            r = videogen.check_access()
        self.assertFalse(r["available"])
        self.assertIn("billing", r["detail"])

    def test_pipeline_only_uses_ai_clips_when_job_opted_in(self):
        pid = db.insert("productions", {"format": "long", "title": "t", "status": "queued", "stage": "assets",
                                        "kind": "general", "request": '{"ai_video": false}', "created_at": db.iso(),
                                        "updated_at": db.iso()})
        self.assertEqual(pipeline._ai_clip_budget(pipeline.load(pid), set()), 0)
        pipeline.save(pid, request='{"ai_video": true}')
        self.assertEqual(pipeline._ai_clip_budget(pipeline.load(pid), set()), 3)
        settings.set("videogen.enabled", False)
        self.assertEqual(pipeline._ai_clip_budget(pipeline.load(pid), set()), 0)


if __name__ == "__main__":
    unittest.main()
