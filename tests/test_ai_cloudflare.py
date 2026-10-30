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
