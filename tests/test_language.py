"""Hindi/Hinglish channels: numbers are spoken in English (the way viewers say them) while captions keep digits;
scripts are conversational, not formal Hindi; scene fixes follow the same language checks; letters from other
scripts are rejected on Hindi/Hinglish/English channels only. The AI is mocked.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins
os.environ.setdefault("STUDIO_OFFLINE", "1")              # never call the free image services

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import profile, settings  # noqa: E402
from studio.content import package as content  # noqa: E402
from studio.media import tts  # noqa: E402


class NumberTests(unittest.TestCase):
    def test_numbers_become_english_words_in_devanagari(self):
        cases = {
            "S&P 500 इंडेक्स": "फाइव हंड्रेड",
            "नैस्डैक 2.5% गिरा": "टू पॉइंट फाइव परसेंट",
            "निफ्टी 24,591.41 पर": "ट्वेंटी फोर थाउज़ेंड फाइव हंड्रेड नाइंटी वन पॉइंट फोर वन",
            "₹1,200 का शेयर": "वन थाउज़ेंड टू हंड्रेड रुपये",
            "$10 बिलियन": "टेन बिलियन डॉलर",
            "2,50,000 लोग": "टू लाख फिफ्टी थाउज़ेंड",
            "साल 2026 में": "ट्वेंटी ट्वेंटी सिक्स",
            "१० बजे": "टेन बजे",
            "1st रैंक": "फर्स्ट रैंक",
        }
        for text, want in cases.items():
            self.assertIn(want, tts.numbers_in_english(text), text)
        self.assertNotRegex(tts.numbers_in_english("कुल 37 लोग"), r"\d")

    def test_hindi_voices_say_english_numbers_unless_switched_off_english_voices_untouched(self):
        self.assertIn("टू परसेंट", tts.say_numbers("2% गिरा", "hi"))
        self.assertIn("एंड", tts.say_numbers("S&P 500 इंडेक्स आज गिरा", "hi"))
        self.assertEqual(tts.say_numbers("It fell 2%", "en-us"), "It fell 2%")
        self.assertEqual(tts.say_numbers("It fell 2%", "hi"), "It fell 2%")      # English sentence on a Hindi voice
        with mock.patch.object(settings, "get", lambda k, *d: {"numbers": "native"} if k == "voice" else None):
            self.assertIn("2", tts.say_numbers("2% गिरा", "hi"))

    def test_the_voice_gets_the_words_and_the_caption_keeps_the_digits(self):
        import numpy as np
        spoken = []

        class FakeKokoro:
            def create(self, text, voice, speed, lang, trim=True):
                spoken.append(text)
                return np.zeros(240, dtype=np.float32), 24000
        with mock.patch.object(tts, "_kokoro", lambda: FakeKokoro()):
            tts._synth_sentence("नैस्डैक 2.5% गिरा", "kokoro", "hf_alpha", "hi", 1.0)
        self.assertEqual(len(spoken), 1)
        self.assertIn("टू पॉइंट फाइव परसेंट", spoken[0])
        self.assertNotRegex(spoken[0], r"\d")


class StyleTests(unittest.TestCase):
    def test_hindi_and_hinglish_rules_ask_for_spoken_style_and_digits(self):
        for lang in ("Hindi", "Hinglish"):
            with mock.patch.object(profile, "language", lambda lang=lang: lang):
                rules = profile.language_rules()
            self.assertIn("NOT formal", rules)
            self.assertIn("digits", rules)
            self.assertIn("तकनीकी", rules)                         # the example of what to avoid

    def _scene(self, narration, caption):
        return content.Scene(narration=narration, caption_text=caption, visual_description="A wide shot of a market",
                             visual_query="stock market screen", visual_type="photo", on_screen_text="")

    def test_letters_from_another_script_are_rejected_on_hinglish(self):
        bad = self._scene("बाज़ार में भूचाल आ गया।", "Bazaar mein bhuچال aa gaya.")
        with mock.patch.object(profile, "language", lambda: "Hinglish"), self.assertRaises(ValueError) as cm:
            content.validate_scenes([bad], "short", check_totals=False)
        self.assertIn("another script", str(cm.exception))

    def test_a_tamil_channel_is_not_rejected_for_its_own_script(self):
        ok = self._scene("சந்தை இன்று சரிந்தது", "சந்தை இன்று சரிந்தது")
        with mock.patch.object(profile, "language", lambda: "Tamil"):
            try:
                content.validate_scenes([ok], "short", check_totals=False)
            except ValueError as e:
                self.assertNotIn("another script", str(e))

    def test_scene_fix_must_keep_devanagari_narration_and_roman_captions(self):
        pkg = {"scenes": [{"narration": "पुरानी लाइन", "caption_text": "Purani line", "on_screen_text": ""}]}
        issues = [{"scene": 1, "detail": "unsupported number", "suggestion": "say it generally"}]
        seen = {}

        def fake(task, system, prompt, model_cls, validate=None, **kw):
            seen["prompt"] = prompt
            bad = model_cls.model_validate({"fixes": [{"scene": 1, "narration": "Market aaj gira hai",
                                                       "caption_text": "Market aaj gira hai", "on_screen_text": ""}]})
            with self.assertRaises(ValueError):
                validate(bad)                                        # Roman narration is refused
            good = model_cls.model_validate({"fixes": [{"scene": 1, "narration": "मार्केट आज गिरा है",
                                                        "caption_text": "Market aaj gira hai", "on_screen_text": ""}]})
            validate(good)
            return good, {"provider": "fake", "model": "m"}
        with mock.patch.object(profile, "language", lambda: "Hinglish"), \
                mock.patch.object(content.ai, "generate", fake):
            out, _ = content.fix_scenes(pkg, issues, {"title": "t"}, [])
        self.assertEqual(out["scenes"][0]["caption_text"], "Market aaj gira hai")   # a real Roman caption
        self.assertIn("digits", seen["prompt"])


if __name__ == "__main__":
    unittest.main()
