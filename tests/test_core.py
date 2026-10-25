"""Core unit tests. Run: .venv/bin/python -m unittest discover -s tests -v

Uses a throwaway data directory and never touches the Keychain or the network
(except the localhost check in URL safety, which resolves names locally)."""
from __future__ import annotations

import os
import tempfile
import unittest
from typing import Literal

_TMP = tempfile.mkdtemp(prefix="studio-test-")
os.environ["STUDIO_DATA_DIR"] = _TMP
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins

from pydantic import BaseModel, Field  # noqa: E402

from studio import config, db, logs, settings  # noqa: E402

config.ensure_dirs()
db.migrate()


class SchemaTests(unittest.TestCase):
    def test_provider_schema_keeps_field_named_title_and_strips_limits(self):
        from studio.ai.schema import provider_schema

        class Item(BaseModel):
            title: str = Field(..., max_length=100)
            kind: Literal["a", "b"]

        class Wrap(BaseModel):
            items: list[Item] = Field(..., min_length=1)

        s = provider_schema(Wrap)
        item = s["properties"]["items"]["items"]
        self.assertIn("title", item["properties"])
        self.assertEqual(item["required"], ["title", "kind"])
        self.assertFalse(item["additionalProperties"])
        self.assertNotIn("maxLength", item["properties"]["title"])
        self.assertNotIn("minItems", s["properties"]["items"])

    def test_extract_json_tolerates_fences_and_trailing_text(self):
        from studio.ai.schema import extract_json
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('Sure! {"a": {"b": 2}} hope that helps {"c": 3}'), {"a": {"b": 2}})
        with self.assertRaises(ValueError):
            extract_json("no json here")


class BudgetTests(unittest.TestCase):
    def setUp(self):
        from studio.ai import providers, router
        self.providers, self.router = providers, router
        self._orig_get = providers.get
        db.ex("DELETE FROM ai_usage")

        class Ok(BaseModel):
            reply: str
        self.Ok = Ok

        class Fake(providers.Provider):
            def __init__(self, pid, paid, est):
                self.id, self.paid, self.est, self.calls, self.label = pid, paid, est, 0, pid

            def model(self):
                return "fake"

            def available(self):
                return True, "ok"

            def estimate_cost(self, *a, **k):
                return self.est

            def complete_json(self, system, prompt, schema, max_tokens):
                self.calls += 1
                return providers.LLMResult('{"reply": "hi"}', self.id, "fake", 10, 5, 0.5 if self.paid else 0.0)

        self.paid = Fake("anthropic", True, 10.0)
        self.free = Fake("ollama_local", False, 0.0)
        fakes = {"anthropic": self.paid, "ollama_local": self.free, "ollama_cloud": Fake("ollama_cloud", False, 0.0),
                 "gemini": Fake("gemini", False, 0.0)}
        providers.get = lambda pid: fakes[pid]
        settings.set("ai.default_provider", "anthropic")
        settings.set("ai.fallback_provider", "ollama_local")
        settings.set("ai.monthly_budget_usd", 5.0)

    def tearDown(self):
        self.providers.get = self._orig_get

    def test_pause_policy_blocks_paid_call(self):
        settings.set("ai.on_budget_exceeded", "pause")
        with self.assertRaises(self.router.BudgetExceeded):
            self.router.generate("script", "s", "p", self.Ok)
        self.assertEqual(self.paid.calls, 0)

    def test_fallback_policy_uses_local_provider(self):
        settings.set("ai.on_budget_exceeded", "fallback_local")
        obj, meta = self.router.generate("script", "s", "p", self.Ok)
        self.assertEqual(meta["provider"], "ollama_local")
        self.assertEqual(self.paid.calls, 0)
        self.assertEqual(obj.reply, "hi")

    def test_paid_call_allowed_under_budget_and_recorded(self):
        self.paid.est = 0.5
        obj, meta = self.router.generate("script", "s", "p", self.Ok)
        self.assertEqual(meta["provider"], "anthropic")
        self.assertAlmostEqual(self.router.month_spend(), 0.5)


class DedupTests(unittest.TestCase):
    def test_similar_titles_detected(self):
        from studio.research import dedup
        self.assertGreaterEqual(dedup.similarity("What is the biggest star in the universe?",
                                                 "The biggest star in the universe"), dedup.DUPLICATE)
        self.assertGreaterEqual(dedup.similarity("ब्रह्मांड का सबसे बड़ा तारा कौन सा है?", "ब्रह्मांड का सबसे बड़ा तारा"), dedup.DUPLICATE)
        self.assertLess(dedup.similarity("How volcanoes form", "Why cats purr"), dedup.NEAR)

    def test_overlap_ratio(self):
        from studio.research import dedup
        src = "the quick brown fox jumps over the lazy dog near the river bank today"
        self.assertGreater(dedup.overlap_ratio(src, [src]), 0.9)
        self.assertEqual(dedup.overlap_ratio("completely different words in my own original script here now", [src]), 0.0)


class CaptionTests(unittest.TestCase):
    def test_chunking_and_timing_cover_sentences(self):
        from studio.media import captions
        scene = {"narration": "Sunlight carries every colour. Blue scatters the most in air.", "caption_text": ""}
        timing = {"duration": 5.0, "sentences": [{"text": "Sunlight carries every colour.", "start": 0.0, "end": 2.0},
                                                {"text": "Blue scatters the most in air.", "start": 2.2, "end": 4.6}]}
        cues = captions.scene_captions(scene, timing, "bold", "short")
        self.assertTrue(all(len(c["text"].split()) <= 4 for c in cues))
        self.assertAlmostEqual(cues[0]["start"], 0.0)
        self.assertAlmostEqual(cues[-1]["end"], 4.6, places=3)
        self.assertTrue(all(b["start"] >= a["start"] for a, b in zip(cues, cues[1:])))
        srt = captions.to_srt(cues)
        self.assertIn("00:00:00,000 -->", srt)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        settings.set("interview", {"language": "Hindi", "formats": "shorts"})

    def _scene(self, **kw):
        from studio.content.package import Scene
        base = dict(narration="यह एक छोटा सा वाक्य है।", caption_text="", visual_description="night sky",
                    visual_query="night sky stars", visual_type="photo", on_screen_text="")
        base.update(kw)
        return Scene(**base)

    def test_rejects_wrong_script_and_bad_query(self):
        from studio.content import package
        with self.assertRaises(ValueError) as ctx:
            package.validate_scenes([self._scene(narration="This is English, not Hindi."),
                                     self._scene(visual_query="तारे")], "short", check_totals=False)
        msg = str(ctx.exception)
        self.assertIn("Devanagari", msg)
        self.assertIn("ENGLISH", msg)

    def test_rejects_english_overlay_in_hindi_video(self):
        from studio.content import package
        with self.assertRaises(ValueError) as ctx:
            package.validate_scenes([self._scene(on_screen_text="Safe Return")], "short", check_totals=False)
        self.assertIn("on_screen_text", str(ctx.exception))
        package.validate_scenes([self._scene(on_screen_text="सुरक्षित वापसी")], "short", check_totals=False)

    def test_rejects_people_in_visual_queries(self):
        from studio.content import package
        with self.assertRaises(ValueError):
            package.validate_scenes([self._scene(visual_query="aerospace engineer satellite lab")], "short", check_totals=False)
        package.validate_scenes([self._scene(visual_query="satellite clean room", visual_type="photo")], "short", check_totals=False)
        package.validate_scenes([self._scene(visual_query="project director", visual_type="text_card")], "short", check_totals=False)

    def test_archive_relevance_filter(self):
        from studio.media import visuals
        self.assertFalse(visuals.relevant("File:Lena River Delta - Landsat 2000.jpg", "satellite sensors technology"))
        self.assertTrue(visuals.relevant("File:Solar flare SDO.jpg", "sun solar flares space"))
        self.assertTrue(visuals.relevant("anything", "space animation"))

    def test_accepts_valid_scenes(self):
        from studio.content import package
        package.validate_scenes([self._scene() for _ in range(3)], "short", check_totals=False)

    def test_metadata_rejects_competitor_title_copy(self):
        from studio.content import package
        with self.assertRaises(ValueError):
            package.validate_metadata("Why is the sky blue explained", ["tag"], ["Why is the sky blue? Explained"])

    def test_all_caps_rule_ignores_uncased_scripts(self):
        from studio.content import package
        package.validate_metadata("आदित्य L1: सूरज की जासूसी", ["tag"], [])
        with self.assertRaises(ValueError):
            package.validate_metadata("THIS TITLE IS SHOUTING", ["tag"], [])

    def test_sanitize_strips_angle_brackets_and_limits_tags(self):
        from studio.content import package
        pkg = {"title": "<b>Hi</b> there", "description": "x <script>", "tags": ["#a" * 40, "b", "B"],
               "scenes": [{"narration": " a  b ", "caption_text": "", "on_screen_text": "", "visual_query": "sky; rm -rf"}],
               "thumbnail": {"text": "<x>"}}
        out = package.sanitize(pkg)
        self.assertNotIn("<", out["title"] + out["description"])
        self.assertEqual(out["scenes"][0]["visual_query"], "sky rm -rf")
        self.assertEqual(len([t for t in out["tags"] if t.lower() == "b"]), 1)
        self.assertTrue(all(len(t) <= 30 for t in out["tags"]))


class SafetyTests(unittest.TestCase):
    def test_non_public_urls_blocked(self):
        from studio import net
        for url in ("http://localhost:8765/", "http://127.0.0.1/", "http://10.0.0.5/", "file:///etc/passwd",
                    "http://169.254.169.254/latest/meta-data"):
            self.assertFalse(net.is_public_url(url), url)

    def test_redaction(self):
        text = logs.redact("key=sk-ant-api03-abcdefghijklmnopqrstuvwxyz token: ya29.a0AfH6SMBxyz Authorization: Bearer abcdef123456789")
        self.assertNotIn("sk-ant-api03-abcdefghijklmnop", text)
        self.assertNotIn("ya29.a0AfH6SMBxyz", text)
        self.assertNotIn("abcdef123456789", text)


class SchedulerAndPublishTests(unittest.TestCase):
    def test_next_run_daily_is_in_future(self):
        from datetime import datetime
        from studio import scheduler
        nxt = db.parse_iso(scheduler.next_run({"kind": "daily", "time_of_day": "07:30"}))
        self.assertGreater(nxt, datetime.now().astimezone())

    def test_unaudited_project_always_private(self):
        from studio.youtube import upload
        settings.set("youtube.audit_passed", False)
        res = upload.privacy_for({"privacy": "public", "publish_at": None})
        self.assertEqual(res["status"]["privacyStatus"], "private")
        settings.set("youtube.audit_passed", True)
        res = upload.privacy_for({"privacy": "scheduled", "publish_at": "2030-01-01T10:00:00Z"})
        self.assertEqual(res["status"], {"privacyStatus": "private", "publishAt": "2030-01-01T10:00:00Z"})
        settings.set("youtube.audit_passed", False)


if __name__ == "__main__":
    unittest.main()
