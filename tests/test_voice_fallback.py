"""Sarvam → Kokoro fallback. Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))

import numpy as np  # noqa: E402

from studio import config, db, settings  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio.media import tts  # noqa: E402


class FakeKokoro:
    def __init__(self):
        self.calls = []

    def create(self, text, voice, speed, lang, trim):
        self.calls.append((voice, lang))
        return np.zeros(2400, dtype=np.float32), 24000


def sarvam_ok(*a, **k):
    return np.ones(2400, dtype=np.int16), 24000


class FallbackTests(unittest.TestCase):
    def setUp(self):
        settings.set(tts.SARVAM_STATUS, {"ok": True})
        self.k = FakeKokoro()
        self.patches = [mock.patch.object(tts, "_kokoro", return_value=self.k),
                        mock.patch.object(tts, "kokoro_installed", return_value=True)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_no_credits_uses_kokoro_and_warns(self):
        err = RuntimeError("Sarvam TTS HTTP 402: insufficient credits")
        with mock.patch.object(tts, "_sarvam_say", side_effect=err) as s:
            audio, sr = tts._synth_sentence("आज निफ्टी ने अच्छा प्रदर्शन किया।", "sarvam", "ritu", "hi", 1.0)
            tts._synth_sentence("Hello there friends.", "sarvam", "amit", "en", 1.0)
        self.assertEqual(s.call_count, 1)                      # not retried for every sentence
        self.assertEqual(self.k.calls, [("hf_alpha", "hi"), ("am_michael", "en-us")])
        st = tts.sarvam_status()
        self.assertFalse(st["ok"])
        self.assertIn("insufficient credits", st["error"])
        self.assertTrue(db.q1("SELECT 1 FROM logs WHERE source='voice' AND level='warning'"))

    def test_retries_after_30_minutes_and_recovers(self):
        settings.set(tts.SARVAM_STATUS, {"ok": False, "since": db.iso_in(-3600), "error": "x",
                                         "last_try": db.iso_in(-31 * 60)})
        with mock.patch.object(tts, "_sarvam_say", side_effect=sarvam_ok):
            tts._synth_sentence("नमस्ते", "sarvam", "ritu", "hi", 1.0)
        self.assertTrue(tts.sarvam_status()["ok"])
        self.assertEqual(self.k.calls, [])

    def test_waits_between_retries(self):
        settings.set(tts.SARVAM_STATUS, {"ok": False, "since": db.iso(), "error": "x", "last_try": db.iso()})
        with mock.patch.object(tts, "_sarvam_say", side_effect=sarvam_ok) as s:
            tts._synth_sentence("नमस्ते", "sarvam", "ritu", "hi", 1.0)
        self.assertEqual(s.call_count, 0)
        self.assertEqual(len(self.k.calls), 1)

    def test_failure_mid_video_restarts_with_one_voice(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] > 2:
                raise RuntimeError("Sarvam TTS HTTP 402: credits exhausted")
            return sarvam_ok()
        with mock.patch.object(tts, "_sarvam_say", side_effect=flaky):
            with self.assertRaises(tts.VoiceSwitched):
                with tts.track_engines():
                    for t in ("एक।", "दो।", "तीन।"):
                        tts._synth_sentence(t, "sarvam", "ritu", "hi", 1.0)
            with tts.track_engines() as used:                 # the stage's second pass: all fallback
                for t in ("एक।", "दो।", "तीन।"):
                    tts._synth_sentence(t, "sarvam", "ritu", "hi", 1.0)
        self.assertEqual(used, {"fallback"})

    def test_voice_samples_never_fall_back(self):
        with mock.patch.object(tts, "_sarvam_say", side_effect=RuntimeError("HTTP 402")):
            with self.assertRaises(RuntimeError):
                tts._synth_sentence("नमस्ते", "sarvam", "ritu", "hi", 1.0, allow_fallback=False)


if __name__ == "__main__":
    unittest.main()
