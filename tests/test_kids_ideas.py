"""Kids channels get kid formats (rhymes, learning songs, moral stories) and COPPA-safe rules in the idea prompt.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import auth, channels  # noqa: E402
from studio.creative import ideas  # noqa: E402

auth.ensure_default_workspace()
channels.ensure_default()


class KidsIdeasTest(unittest.TestCase):
    def _prompt_for(self, ch: dict) -> str:
        seen = {}

        def fake(task, system, prompt, schema, **kw):
            seen["prompt"] = prompt
            return ideas.StoryIdeas(ideas=[]), {"provider": "test", "model": "test"}
        base = {"id": 1, "name": "Test", "niche": "cartoons", "sub_niche": "", "audience": "", "language": "Hindi",
                "tone": "", "instructions": "", "restrictions": "", "workspace_id": 1, "formats": "both"}
        with mock.patch.object(channels, "get", return_value={**base, **ch}), \
                mock.patch.object(ideas.ai, "generate", side_effect=fake):
            ideas.generate(1, count=0)
        return seen["prompt"]

    def test_kids_channel_gets_kid_formats_and_coppa_rules(self):
        p = self._prompt_for({"made_for_kids": 1, "styles": ["kids"]})
        self.assertIn("nursery rhymes", p)
        self.assertIn("moral stories", p)
        self.assertIn("Never ask viewers to comment", p)
        self.assertNotIn("not lessons", p)

    def test_kids_style_without_flag_counts_as_kids(self):
        self.assertTrue(ideas.is_kids({"made_for_kids": 0, "styles": ["kids"]}))

    def test_other_channels_stay_entertainment(self):
        p = self._prompt_for({"made_for_kids": 0, "styles": ["comedy"]})
        self.assertIn("not lessons, tips or explainers", p)
        self.assertNotIn("nursery rhymes", p)


if __name__ == "__main__":
    unittest.main()
