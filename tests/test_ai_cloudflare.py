"""Cloudflare Workers AI as the free last-resort writer: in the free chain, answers parsed, neurons counted against
the shared daily allowance, and skipped once that allowance is used up. Nothing real is called.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins
os.environ.setdefault("STUDIO_OFFLINE", "1")              # never call the free image services

import httpx  # noqa: E402

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio.ai import providers, router  # noqa: E402
from studio.media import visuals  # noqa: E402

ANSWER = {"result": {"choices": [{"message": {"content": '{"title": "Aaj ki khabar"}'}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 1000, "completion_tokens": 1000}}}


def ready(yes: bool = True):
    return mock.patch.object(visuals, "cloudflare_ready", lambda: yes)


class CloudflareTextTests(unittest.TestCase):
    def setUp(self):
        db.ex("DELETE FROM ai_usage WHERE provider='cloudflare'")

    def test_in_the_free_chain_only_when_set_up(self):
        with ready():
            self.assertIn("cloudflare_ai", router.provider_chain("script"))
            self.assertTrue(router.is_free("cloudflare_ai"))
        with ready(False):
            self.assertNotIn("cloudflare_ai", router.provider_chain("script"))

    def test_answer_is_parsed_and_neurons_are_counted(self):
        p = providers.get("cloudflare_ai")
        with ready(), mock.patch.object(httpx, "post", lambda *a, **k: httpx.Response(200, json=ANSWER)):
            r = p.complete_json("sys", "prompt", {"type": "object"}, 500)
        self.assertEqual(r.text, '{"title": "Aaj ki khabar"}')
        self.assertEqual(visuals.cloudflare_used_today(), int((1000 * 26_668 + 1000 * 204_805) / 1e6) + 1)

    def test_used_up_allowance_skips_without_calling(self):
        db.insert("ai_usage", {"task": "image", "provider": "cloudflare", "model": "flux", "input_tokens": 0,
                               "output_tokens": visuals.CF_FREE_NEURONS, "cost_usd": 0, "ok": 1, "error": "",
                               "duration_s": 0, "created_at": db.iso()})
        calls = []
        with ready(), mock.patch.object(httpx, "post", lambda *a, **k: calls.append(1)), \
                self.assertRaises(providers.ProviderError) as cm:
            providers.get("cloudflare_ai").complete_json("s", "p", {}, 100)
        self.assertTrue(cm.exception.skip_provider)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()


class GeminiRotationTests(unittest.TestCase):
    def setUp(self):
        providers._gemini_spent.clear()

    def test_a_used_up_model_moves_on_to_the_next_free_one_and_is_skipped_afterwards(self):
        seen = []

        def post(url, **kw):
            model = url.split("/models/")[1].split(":")[0]
            seen.append(model)
            if model in ("gemini-flash-latest", "gemini-3.5-flash"):
                return httpx.Response(429, json={"error": {"message": "quota"}})
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"a": 1}'}]}}]})
        g = providers.get("gemini")
        with mock.patch.object(httpx, "post", post):
            r1 = g.complete_json("s", "p", {"type": "object"}, 100)
            r2 = g.complete_json("s", "p", {"type": "object"}, 100)
        self.assertEqual((r1.model, r2.model), ("gemini-3-flash-preview", "gemini-3-flash-preview"))
        self.assertEqual(seen.count("gemini-flash-latest"), 1)              # remembered as spent for today

    def test_gemma_gets_the_schema_in_the_text_and_no_json_mode(self):
        bodies = []
        with mock.patch.object(httpx, "post", lambda url, **kw: bodies.append(kw["json"]) or httpx.Response(
                200, json={"candidates": [{"content": {"parts": [{"text": 'ok {"a": 1}'}]}}]})):
            providers.get("gemini")._call("gemma-4-31b-it", "sys", "p", {"type": "object"}, 100)
        self.assertNotIn("systemInstruction", bodies[0])
        self.assertNotIn("generationConfig", bodies[0])
        self.assertIn("JSON schema", bodies[0]["contents"][0]["parts"][0]["text"])

    def test_every_writer_used_up_is_a_wait_not_a_failure(self):
        self.assertTrue(router.AllProvidersFailed.temporary)
        self.assertGreater(router.AllProvidersFailed.retry_after, 0)
