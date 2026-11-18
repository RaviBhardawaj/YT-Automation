"""Performance engine: fixed character voices with emotion-shaped delivery, voice-identity measurement, one
speaker per dialogue shot, scene-emotion music with dialogue ducking, sync checks and honest QC statuses.
Providers are not called (synthetic audio stands in for voices).
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")
os.environ.setdefault("STUDIO_OFFLINE", "1")

import numpy as np  # noqa: E402

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio.creative import director, kahaani  # noqa: E402
from studio.film_studio import performance as perf  # noqa: E402
from studio.film_studio import score  # noqa: E402

TMP = Path(tempfile.mkdtemp())


def voice(hz: float, seconds: float = 1.5, name: str = "v.wav", sr: int = 24000) -> str:
    t = np.arange(int(sr * seconds)) / sr
    sig = 0.5 * np.sin(2 * np.pi * hz * t) + 0.2 * np.sin(4 * np.pi * hz * t)
    path = TMP / name
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((sig * 32767).astype(np.int16).tobytes())
    return str(path)


class VoiceTests(unittest.TestCase):
    def test_every_main_character_has_a_fixed_distinct_voice_profile(self):
        ids = {kahaani.performance(c["key"])["logical_id"] for c in kahaani.UNIVERSE}
        self.assertEqual(ids, {"AARAV_VOICE", "SIYA_VOICE", "VEER_VOICE", "DADAJI_VOICE"})
        combos = {(c["voice"]["voice_id"], c["voice"]["pitch"]) for c in kahaani.UNIVERSE}
        self.assertEqual(len(combos), len(kahaani.UNIVERSE))         # no two characters share a voice
        self.assertNotEqual(kahaani.performance("AARAV")["voice_id"], kahaani.performance("DADAJI")["voice_id"])

    def test_delivery_follows_emotion_and_keeps_the_voice(self):
        prof = kahaani.performance("SIYA")
        calm = perf.deliver({"text": "सिया यहाँ है", "manner": ""}, prof)
        scared = perf.deliver({"text": "Siya भागो!", "manner": "trembling, scared"}, prof)
        whisper = perf.deliver({"text": "धीरे बोलो", "manner": "whisper"}, prof)
        self.assertEqual({calm["voice_id"], scared["voice_id"], whisper["voice_id"]}, {"hf_alpha"})
        self.assertEqual(scared["emotion"], "fear")
        self.assertGreater(scared["speed"], calm["speed"])
        self.assertLess(whisper["gain"], calm["gain"])
        self.assertIn("सिया", scared["text"])                     # the name is said the profile's way

    def test_lines_are_levelled_to_one_loudness(self):
        quiet = (np.sin(np.arange(24000) / 10) * 2000).astype(np.int16)
        loud = (np.sin(np.arange(24000) / 10) * 20000).astype(np.int16)
        rq, rl = (float(np.sqrt(np.mean(perf.level(x).astype(float) ** 2))) for x in (quiet, loud))
        self.assertAlmostEqual(rq, rl, delta=rl * 0.05)

    def test_a_whispered_line_drives_the_face_at_full_level(self):
        from studio.film_studio import sadtalker
        import wave
        src = voice(180, seconds=1.0, name="whisper.wav")
        with wave.open(src, "rb") as w:
            quiet = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16) * 0.05
        perf._write(TMP / "whisper.wav", quiet / 32768)
        out = sadtalker._drive(TMP / "whisper.wav", TMP / "drive.wav")
        self.assertEqual(out, TMP / "drive.wav")
        rms = lambda p: float(np.sqrt(np.mean(perf._read(p)[0].astype(float) ** 2)))  # noqa: E731
        self.assertGreater(20 * np.log10(rms(out) / rms(TMP / "whisper.wav")), 10)   # far louder than the whisper

    def test_identity_check_measures_pitch_and_flags_drift_and_lookalikes(self):
        lines = [{"speaker": "AARAV", "wav": voice(140, name="a1.wav")},
                 {"speaker": "AARAV", "wav": voice(142, name="a2.wav")},
                 {"speaker": "AARAV", "wav": voice(200, name="a3.wav")},        # sounds like someone else
                 {"speaker": "VEER", "wav": voice(110, name="v1.wav")},
                 {"speaker": "DADAJI", "wav": voice(112, name="d1.wav")}]       # too close to Veer
        rep = perf.voice_identity(lines)
        self.assertAlmostEqual(rep["median_hz"]["VEER"], 110, delta=4)
        self.assertEqual([x["wav"].split("/")[-1] for x in rep["drifting_lines"]], ["a3.wav"])
        self.assertIn(("DADAJI", "VEER"), rep["too_alike"])
        self.assertFalse(rep["ok"])


class FramingTests(unittest.TestCase):
    def test_a_two_shot_with_two_speakers_becomes_one_single_per_speaker(self):
        lines = [{"speaker": "AARAV", "text": "दादाजी, यह चिट्ठी किसकी है?"},
                 {"speaker": "DADAJI", "text": "उसे वहीं रख दो, आरव।"}]
        board = director.Shot(shot_type="over_shoulder", characters=["AARAV", "DADAJI"], line_indices=[0, 1],
                              seconds=4, image_prompt="Aarav and Dadaji talk across the table", lighting="lamp")
        runs = director._speaker_runs(board.line_indices, lines)
        self.assertEqual(runs, [[0], [1]])
        framed = [director._frame_speaker(board, [{"speaker": lines[r[0]]["speaker"], "line_index": r[0]}], lines,
                                          {"AARAV": "Aarav look", "DADAJI": "Dadaji look"}, "a hill house")
                  for r in runs]
        self.assertEqual([f.characters for f in framed], [["AARAV"], ["DADAJI"]])
        self.assertEqual({f.shot_type for f in framed}, {"close_up"})
        self.assertIn("Dadaji look", framed[1].image_prompt)
        self.assertIn("alone in frame", framed[1].image_prompt)


class MusicTests(unittest.TestCase):
    def test_scene_emotions_pick_different_cues(self):
        self.assertEqual(score.profile_for({"purpose": "twist"}), "emotional_reveal")
        self.assertEqual(score.profile_for({"purpose": "setup", "action": "rain; she cries alone"}), "sadness")
        self.assertEqual(score.profile_for({"purpose": "climax"}, last=True, genre="psychological_thriller"),
                         "dramatic_ending")
        a, b = score.cue("suspense", 4, 1), score.cue("happiness", 4, 1)
        self.assertEqual(len(a), 4 * score.SR)
        self.assertGreater(float(np.abs(a - b).mean()), 0.01)
        self.assertEqual(float(np.abs(score.cue("silence", 2)).max()), 0.0)

    def test_plan_has_a_cue_per_emotion_change_with_ducking(self):
        film = {"genre": "psychological_thriller", "scenes": [{"purpose": "hook"}, {"purpose": "setup"},
                                                              {"purpose": "twist"}, {"purpose": "resolution"}]}
        tl = [{"kind": "shot", "scene": i + 1, "start": i * 10.0, "end": i * 10.0 + 10} for i in range(4)]
        plan = perf.music_plan(film, tl)
        self.assertEqual([c["emotion"] for c in plan], ["suspense", "mystery", "emotional_reveal", "dramatic_ending"])
        self.assertTrue(all(c["ducking"]["under_dialogue_db"] <= -10 for c in plan))
        self.assertIn("original", plan[0]["provenance"])

    def test_music_ducks_under_every_line_and_recovers(self):
        env = perf.duck_envelope(10.0, [(2.0, 4.0)])
        at = lambda s: float(env[int(s * perf.SR)])  # noqa: E731
        self.assertAlmostEqual(at(1.0), 1.0, places=2)
        self.assertLess(at(3.0), 0.3)                                  # -12 dB ≈ 0.25
        self.assertGreater(at(6.0), 0.95)                             # back up after the release

    def test_dialogue_over_music_is_measured(self):
        n = perf.SR * 6
        d = np.zeros(n)
        d[perf.SR:perf.SR * 3] = 0.3 * np.sin(np.arange(perf.SR * 2) / 7)
        m = 0.3 * np.sin(np.arange(n) / 11) * perf.duck_envelope(6.0, [(1.0, 3.0)])[:n] * 10 ** (-22 / 20)
        perf._write(TMP / "d.wav", d)
        perf._write(TMP / "m.wav", m)
        rep = perf.dialogue_vs_music(TMP / "d.wav", TMP / "m.wav", [(1.0, 3.0)])
        self.assertTrue(rep["ok"])
        self.assertGreater(rep["min_db"], 25)


class CheckTests(unittest.TestCase):
    def test_sync_checks_catch_wrong_speaker_spill_and_stills(self):
        dlg = [{"dialogue_id": "s01.02", "character": "AARAV", "on_screen": ["SIYA"], "end": 5.0, "shot_end": 4.0,
                "motion_type": "camera_move"},
               {"dialogue_id": "s01.03", "character": "SIYA", "on_screen": ["SIYA"], "end": 7.0, "shot_end": 7.5,
                "motion_type": "dialogue_video"}]
        res = {c["name"]: c["ok"] for c in perf.sync_checks(dlg, [])}
        self.assertFalse(res["speaker is on screen when their line plays"])
        self.assertFalse(res["dialogue ends before the shot changes"])
        self.assertFalse(res["every line has a talking face"])

    def test_report_never_passes_what_was_not_measured(self):
        rep = perf.report({"lipsync": [{"name": "a", "ok": True}, {"name": "b", "ok": None}],
                           "music": [{"name": "c", "ok": True}]})
        self.assertEqual((rep["lipsync"]["status"], rep["music"]["status"], rep["overall"]),
                         ("unverified", "passed", "unverified"))
        self.assertEqual(perf.report({"movement": [{"name": "x", "ok": False}]})["overall"], "failed")


if __name__ == "__main__":
    unittest.main()
