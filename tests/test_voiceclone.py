"""Cloned voices: consent, per-account isolation, use in videos and re-voicing uploads (engine mocked).
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import auth, channels, jobs, pipeline  # noqa: E402
from studio.editing import engine as edit_engine  # noqa: E402
from studio.media import voiceclone  # noqa: E402

auth.ensure_default_workspace()
channels.ensure_default()


def wav_bytes(seconds: float, sr: int = 24000, loud: bool = True) -> bytes:
    t = np.arange(int(seconds * sr)) / sr
    a = (np.sin(2 * np.pi * 220 * t) * (9000 if loud else 10)).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(a.tobytes())
    return buf.getvalue()


def ws(name: str) -> int:
    return db.insert("workspaces", {"name": name, "plan": "studio", "created_at": db.iso()})


class VoiceTests(unittest.TestCase):
    def test_consent_length_and_silence_are_checked(self):
        w = ws("voice-a")
        with self.assertRaises(voiceclone.VoiceError):
            voiceclone.add(w, None, "Me", "Hindi", wav_bytes(15), "a.wav", consent=False)
        with self.assertRaises(voiceclone.VoiceError):
            voiceclone.add(w, None, "Me", "Hindi", wav_bytes(4), "a.wav", consent=True)
        with self.assertRaises(voiceclone.VoiceError):
            voiceclone.add(w, None, "Me", "Hindi", wav_bytes(15, loud=False), "a.wav", consent=True)
        vid = voiceclone.add(w, None, "Me", "Hindi", wav_bytes(40), "a.wav", consent=True)
        v = voiceclone.get(vid, w)
        self.assertTrue(Path(v["sample_path"]).is_file())
        self.assertLessEqual(v["sample_seconds"], voiceclone.MAX_SECONDS + 0.5)
        self.assertEqual(v["consent"], voiceclone.CONSENT)

    def test_a_voice_is_only_usable_by_its_own_account(self):
        a, b = ws("voice-own"), ws("voice-other")
        vid = voiceclone.add(a, None, "Mine", "English", wav_bytes(12), "a.wav", consent=True)
        self.assertIsNone(voiceclone.get(vid, b))
        ch_b = channels.create({"name": "B channel", "workflow": "factual"}, b)
        with self.assertRaises(ValueError):
            jobs.create(ch_b, "a video about tea", voice_id=vid)
        voiceclone.delete(a, vid)
        self.assertIsNone(voiceclone.get(vid, a))
        self.assertFalse(voiceclone.folder(a, vid).exists())

    def test_chosen_voice_narrates_the_video(self):
        a = ws("voice-video")
        vid = voiceclone.add(a, None, "Narrator", "English", wav_bytes(12), "a.wav", consent=True)
        ch = channels.create({"name": "Explainers", "workflow": "factual"}, a)
        res = jobs.create(ch, "explain how tea is grown", voice_id=vid)
        p = pipeline.load(res["production_ids"][0])
        self.assertEqual(json.loads(p["request"])["voice"]["voice_id"], str(vid))
        self.assertEqual(pipeline.video_voice(p)["engine"], "clone")
        voiceclone.delete(a, vid)
        with self.assertRaises(pipeline.NeedsAttention):
            pipeline.video_voice(p)

    def test_speaking_language_follows_the_line(self):
        a = ws("voice-lang")
        vid = voiceclone.add(a, None, "Bilingual", "Hindi", wav_bytes(12), "a.wav", consent=True)
        sent = []

        class FakeProc:
            def poll(self):
                return None

            class stdin:
                @staticmethod
                def write(s):
                    req = json.loads(s)
                    sent.append(req["lang"])
                    Path(req["out"]).write_bytes(wav_bytes(1))

                @staticmethod
                def flush():
                    pass

            class stdout:
                @staticmethod
                def readline():
                    return json.dumps({"ok": True}) + "\n"
        with mock.patch.object(voiceclone, "_worker", return_value=FakeProc()):
            voiceclone.say("नमस्ते दोस्तों", str(vid), "")
            voiceclone.say("Hello friends", str(vid), "")
        self.assertEqual(sent, ["hi", "en"])

    def test_revoice_replaces_the_soundtrack_at_the_same_times(self):
        work = Path(tempfile.mkdtemp())
        src = work / "in.mp4"
        subprocess.run([config.FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=6",
                        "-f", "lavfi", "-i", "sine=f=440:d=6", "-shortest", "-c:v", "libx264", "-c:a", "aac", str(src)],
                       check=True)
        lines = edit_engine.speech_lines(
            [{"start": 0.5, "end": 2.0, "text": "first line"}, {"start": 3.0, "end": 4.0, "text": "second"}],
            [{"media_id": 1, "src_start": 0.0, "src_end": 6.0, "out_start": 0.0, "speed": 1.0}])
        self.assertEqual([round(x["start"], 1) for x in lines], [0.5, 3.0])
        with mock.patch.object(voiceclone, "say", return_value=(np.full(24000 * 4, 3000, np.int16), 24000)):
            out = edit_engine.revoice(src, lines, "1", 6.0, work / "v")
        info = edit_engine.probe(out)
        self.assertAlmostEqual(info["duration"], 6.0, delta=0.3)
        self.assertTrue((work / "v" / "l0000_fit.wav").exists())     # 4 s line squeezed into its 2.5 s slot


if __name__ == "__main__":
    unittest.main()
