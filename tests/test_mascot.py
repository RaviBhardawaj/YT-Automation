"""Channel mascot (Fibbu on the NIFTY channel): off unless the channel turns it on; same-length pitched voice; the
talking overlay keeps the video's length, size and sound; the script rule adds humour but forbids tips.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins
os.environ.setdefault("STUDIO_OFFLINE", "1")              # never call the free image services

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import channels  # noqa: E402
from studio.creative import mascot  # noqa: E402

TMP = Path(tempfile.mkdtemp())


def speech(path: Path, seconds: float = 2.0, sr: int = 24000) -> Path:
    t = np.arange(int(sr * seconds)) / sr
    burst = (np.sin(2 * np.pi * 220 * t) * (np.sin(2 * np.pi * 3 * t) > 0) * 9000).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(burst.tobytes())
    return path


def probe(path: Path) -> dict:
    out = subprocess.run([config.FFPROBE, "-v", "error", "-show_entries",
                          "stream=codec_type,width,height:format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True).stdout
    return json.loads(out)


class MascotTests(unittest.TestCase):
    def test_only_a_channel_that_turns_it_on_gets_the_mascot(self):
        off = channels.create({"name": f"Plain {os.urandom(2).hex()}"}, 1)
        on = channels.create({"name": f"Nifty {os.urandom(2).hex()}", "branding": {"mascot": "fibbu"}}, 1)
        self.assertIsNone(mascot.for_production({"channel_id": off}))
        self.assertEqual(mascot.for_production({"channel_id": on})["name"], "Fibbu")
        rule = mascot.script_rule({"channel_id": on})
        self.assertIn("NEVER gives tips", rule)
        self.assertEqual(mascot.script_rule({"channel_id": off}), "")

    def test_the_voice_is_higher_but_exactly_as_long(self):
        wav = speech(TMP / "line.wav", 3.0)
        before = wave.open(str(wav)).getnframes()
        mascot.pitch_voice(wav, 1.32)
        with wave.open(str(wav)) as w:
            self.assertAlmostEqual(w.getnframes() / w.getframerate(), before / 24000, delta=0.05)

    def test_overlay_keeps_length_size_and_sound_and_draws_the_character(self):
        video = TMP / "in.mp4"
        subprocess.run([config.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x0a1020:s=1920x1080:r=30:d=2",
                        "-f", "lavfi", "-i", "sine=f=300:d=2", "-shortest", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        str(video)], check=True)
        out = TMP / "out.mp4"
        frames = mascot.overlay(video, speech(TMP / "n.wav", 2.0), out, "long", 2.0)
        self.assertEqual(frames, 60)
        info = probe(out)
        self.assertAlmostEqual(float(info["format"]["duration"]), 2.0, delta=0.15)
        kinds = {s["codec_type"]: s for s in info["streams"]}
        self.assertEqual((kinds["video"]["width"], kinds["video"]["height"]), (1920, 1080))
        self.assertIn("audio", kinds)
        frame = TMP / "f.png"
        subprocess.run([config.FFMPEG, "-v", "error", "-y", "-ss", "1", "-i", str(out), "-frames:v", "1", str(frame)],
                       check=True)
        from PIL import Image
        size, left, bottom = mascot.PLACE["long"]
        crop = Image.open(frame).convert("RGB").crop((left, 1080 - bottom - size, left + size, 1080 - bottom))
        greens = sum(1 for r, g, b in crop.getdata() if g > 150 and r < 100 and b < 160)
        self.assertGreater(greens, 2000)                                    # Fibbu's green body is there

    def test_every_pose_draws(self):
        for amp, blink, wave_ in ((0.0, False, 0.0), (0.9, False, 0.9), (0.4, True, 0.3)):
            img = mascot.draw_fibbu(0.3, amp, blink=blink, wave=wave_, size=200)
            self.assertEqual((img.size, img.mode), ((200, 200), "RGBA"))


class RolesTests(unittest.TestCase):
    def test_the_anchor_looks_different_and_speaks_lower(self):
        classic = mascot.draw_fibbu(0.2, 0.0, blink=False, size=200)
        anchor = mascot.draw_fibbu(0.2, 0.0, blink=False, size=200, variant="anchor")
        self.assertNotEqual(classic.tobytes(), anchor.tobytes())          # glasses, grey brows, moustache, mic
        self.assertLess(mascot.MASCOTS["fibbu_anchor"]["voice"]["pitch"], 1.0)
        self.assertIn("neutral and factual", mascot.MASCOTS["fibbu_anchor"]["script_rule"])

    def test_fibbu_can_be_a_story_character(self):
        from PIL import Image

        from studio.creative import cartoon, characters
        cid = channels.create({"name": f"Kids {os.urandom(2).hex()}", "workflow": "animated_story"}, 1)
        char = characters.get(characters.create(cid, "फिब्बू", {"mascot": "fibbu", "age": "child"},
                                                voice={"engine": "kokoro", "voice_id": "hm_omega", "pitch": 1.32}))
        self.assertEqual(char["design"].get("mascot"), "fibbu")              # kept by the character bible
        img = Image.new("RGB", (640, 360), (240, 230, 200))
        cartoon.draw_character(img, char["design"], 320, 300, 200, {"t": 0.1, "amp": 0.6, "talking": True})
        greens = sum(1 for r, g, b in img.getdata() if g > 150 and r < 100 and b < 160)
        self.assertGreater(greens, 1000)

    def test_a_news_channel_with_an_anchor_mascot_skips_the_photoreal_presenter(self):
        from studio.creative import news
        cid = channels.create({"name": f"News {os.urandom(2).hex()}", "workflow": "factual", "styles": ["news"],
                               "branding": {"mascot": "fibbu_anchor"}}, 1)
        prod = {"channel_id": cid, "format": "short", "package": {"scenes": [{}, {}, {}]}}
        self.assertIsNone(news.presenter_asset(prod, 0, TMP))


if __name__ == "__main__":
    unittest.main()
