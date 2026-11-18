"""KahaaniVerse director: scored concepts go to the backlog (copies dropped), today's pick follows the genre mix,
the screenplay and storyboard are validated (every line covered once, in order, with time to be heard) and become
the film studio's film format with library characters' fixed voices. The AI is replaced by canned answers.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")
os.environ.setdefault("STUDIO_OFFLINE", "1")

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import channels  # noqa: E402
from studio.creative import director, film  # noqa: E402

SCORES = {k: 7 for k in director.SCORE_WEIGHTS}


def concept(i: int, genre: str = "suspense_mystery", **kw) -> dict:
    return {"title": f"Raaz number {i} ki kahani {os.urandom(2).hex()}", "genre": genre,
            "logline": "A young man finds a letter that should not exist and follows it home.",
            "why_interesting": "x", "why_care": "x", "protagonist": "Aarav", "want": "truth", "conflict": "family",
            "curiosity": "who wrote it", "emotional_core": "loss", "twist": "his grandfather", "payoff": "forgiveness",
            "visual_memorable": "a lantern in rain", "hook_5s": "a letter falls", "structure": "flashback",
            "recurring": ["AARAV", "DADAJI"], "one_off_characters": [], "scores": dict(SCORES), **kw}


def fake_generate(answers: dict):
    def gen(task, system, prompt, model, max_tokens=0, validate=None):
        out = model.model_validate(answers[model.__name__](prompt))
        if validate:
            validate(out)
        return out, {"provider": "test", "model": "canned"}
    return gen


LINES = [{"speaker": "AARAV", "text": "दादाजी, यह चिट्ठी किसकी है?", "manner": "whisper", "action": ""},
         {"speaker": "DADAJI", "text": "उसे वहीं रख दो, आरव।", "manner": "slow", "action": ""}]


_n = [0]


def fresh_lines(k: int) -> list[dict]:
    """k different lines of eight words each (writers must never repeat themselves)."""
    out = []
    for _ in range(k):
        _n[0] += 1
        out.append({"speaker": "DADAJI", "text": f"बेटा यह पुरानी बात है, पंक्ति {_n[0]} सुनो ध्यान से", "manner": "",
                    "action": ""})
    return out


class ConceptTests(unittest.TestCase):
    def setUp(self):
        self.ch = channels.get(channels.create({"name": f"KV {os.urandom(2).hex()}"}, 1))

    def test_concepts_scored_into_backlog_and_copies_dropped(self):
        genres = ["suspense_mystery", "emotional_drama", "village_mystery"]
        items = [concept(i, genres[i % 3]) for i in range(10)]
        items[0]["title"] = "Trending Mystery Of The Old Haveli"
        with mock.patch("studio.ai.generate", fake_generate({"Concepts": lambda p: {"concepts": items}})):
            res = director.concepts(self.ch["id"], {"videos": [{"genre": "x", "title": "Trending Mystery Of The Old "
                                                                "Haveli", "views": 9, "minutes": 9}]})
        self.assertEqual(len(res["created"]), 9)
        self.assertEqual(len(res["dropped"]), 1)
        b = director.backlog(self.ch["id"])
        self.assertEqual(b[0]["twist"], "his grandfather")
        self.assertEqual(b[0]["score"], 70.0)
        self.assertEqual(director.pick(self.ch["id"], "village_mystery")["genre"], "village_mystery")

    def test_genre_this_channel_already_wins_gets_a_bonus(self):
        own = {"emotional_drama": {"videos": 4, "views_per_day": 50.0}}
        self.assertGreater(director.score(concept(1, "emotional_drama"), own), director.score(concept(2), own))


class ScreenplayTests(unittest.TestCase):
    def outline(self, prompt):
        purposes = ["hook", "setup", "conflict", "escalation", "twist", "escalation", "climax", "resolution"]
        return {"title": "Lantern", "hook_line": "दादाजी, यह चिट्ठी किसकी है?", "one_offs": [],
                "locations": [{"key": "L1", "description": "an old railway quarters house in the hills, rain"}],
                "scenes": [{"location": "L1", "time": "night", "mood": "tense", "purpose": p,
                            "characters": ["AARAV", "DADAJI"], "summary": "Aarav asks; Dadaji avoids the question."}
                           for p in purposes],
                "thumbnail_text": "किसकी चिट्ठी?", "description": "A letter from 1971 opens an old wound." * 2,
                "tags": ["a", "b", "c", "d", "e"], "short_scene": 5}

    def test_screenplay_storyboard_and_film_format(self):
        answers = {"FilmOutline": self.outline,
                   "FilmScenes": lambda p: {"scenes": [{"action": "Rain on the window; a lantern flickers.",
                                                        "lines": fresh_lines(12)} for _ in range(2)]},
                   "SceneBoard": lambda p: {"shots": [
                       {"shot_type": "establishing", "characters": [], "line_indices": [], "action": "rain",
                        "angle": "high angle", "movement": "slow crane down", "lighting": "blue night",
                        "seconds": 3, "image_prompt": "an old hill house at night in rain, a lit window, 3D film",
                        "video_prompt": "rain falls, the camera cranes down"},
                       *[{"shot_type": "close_up", "characters": ["AARAV"], "line_indices": [i],
                          "action": "holds the letter", "angle": "eye level", "movement": "slow dolly in",
                          "lighting": "lantern light", "seconds": 10, "transition": "dissolve",
                          "image_prompt": "Aarav holds a yellowed letter, lantern light on his face, 3D film",
                          "video_prompt": "his hand trembles slightly"} for i in range(12)]]}}
        with mock.patch("studio.ai.generate", fake_generate(answers)):
            sp = director.screenplay(1, concept(1), minutes=6.5)
            shots = director.storyboard(sp)
        self.assertEqual(len(sp["scenes"]), 8)
        self.assertEqual(shots[1]["camera"]["transition"], "dissolve")
        self.assertIn("extra fingers", shots[1]["camera"]["negative_prompt"])
        self.assertEqual([d["line_index"] for d in shots[2]["dialogue"]], [1])
        f = director.to_film(1, sp, shots)
        aarav = next(c for c in f["cast"] if c["key"] == "AARAV")
        self.assertEqual(aarav["voice"]["voice_id"], "hm_psi")
        self.assertEqual(f["environments"]["L1"]["description"][:10], "an old rai")
        self.assertEqual(director.check_story(sp, 6.5), [])
        sp["scenes"][1]["lines"] = sp["scenes"][1]["lines"] + [
            {"speaker": "AARAV", "text": f"यह लाइन नंबर {i} बार बार आती है", "manner": ""} for i in range(3)] * 2
        self.assertIn("repeated lines", " ".join(director.check_story(sp, 6.5)))   # three lines said twice

    def test_storyboard_must_cover_every_line_in_order(self):
        board = director.SceneBoard.model_validate({"shots": [
            {"shot_type": "medium", "line_indices": [1], "action": "x", "angle": "eye level", "movement": "static",
             "lighting": "x", "seconds": 3, "image_prompt": "y" * 40, "video_prompt": "z" * 25},
            {"shot_type": "medium", "line_indices": [0], "action": "x", "angle": "eye level", "movement": "static",
             "lighting": "x", "seconds": 3, "image_prompt": "y" * 40, "video_prompt": "z" * 25}]})
        sp = {"outline": {"title": "t", "locations": [], "one_offs": []}, "people": {"AARAV": "Aarav", "DADAJI": "D"},
              "scenes": [{"purpose": "hook", "time": "night", "mood": "x", "location": "L1", "action": "x",
                          "characters": ["AARAV"], "lines": LINES}]}
        calls = []

        def gen(task, system, prompt, model, max_tokens=0, validate=None):
            calls.append(1)
            validate(board)
        with mock.patch("studio.ai.generate", gen):
            with self.assertRaisesRegex(ValueError, "in order"):
                director.storyboard(sp)


class FilmVoiceTests(unittest.TestCase):
    def test_library_voices_are_kept_and_never_reused(self):
        cast = [{"key": "AARAV", "gender": "male", "voice": {"engine": "kokoro", "voice_id": "hm_psi", "pitch": 1.0}},
                {"key": "N1", "gender": "male"}]
        film._assign_voices(cast)
        self.assertEqual(cast[0]["voice"]["voice_id"], "hm_psi")
        self.assertNotEqual((cast[1]["voice"]["voice_id"], cast[1]["voice"]["pitch"]), ("hm_psi", 1.0))


if __name__ == "__main__":
    unittest.main()
