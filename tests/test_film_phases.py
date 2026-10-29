"""Film Studio phases 6–9: shot generation with takes and free video, dialogue sync, timeline + export, QA.
Video service, images, voices and AI are mocked — no free allowance or GPU quota is spent.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins
os.environ.setdefault("STUDIO_OFFLINE", "1")              # never call the free image services

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import auth, channels, jobs, pipeline, settings, worker  # noqa: E402
from studio.creative import film  # noqa: E402
from studio.film_studio import audio, edit, generate, qa, store, zerogpu  # noqa: E402

auth.ensure_default_workspace()
channels.ensure_default()

SCRIPT = """Title: Coin Trick
Visual style: Photorealistic live-action cinematic realism.
CHARACTER 1: RAMU — THE FARMER
Appearance: Grey moustache.
Clothing: White kurta.
Voice: Deep, mature male voice.
CHARACTER 2: SHAMU — THE JOKER
Appearance: Round face.
Clothing: Blue shirt.
4. ENVIRONMENT AND SET DESIGN
* A village courtyard with a neem tree.
SCENE 1 — THE BET
Two friends sit at a table.
RAMU:
"Do rupaye!"
He places two coins on the table.
Everyone laughs.
SHAMU:
"Bas itna hi?"
SCENE 2 — THE END
SHAMU:
"Main chala."
RAMU:
"Ruk ja!"
"""
CALLS = {"video": 0}


def fake_image(prompt, fmt, out, look="real", *, refs=None, size=None):
    Image.new("RGB", size or (1024, 576), (90, 120, 60)).save(out)


def fake_tts(text, engine, voice_id, lang, speed, allow_fallback=True):
    t = np.arange(int(24000 * 1.2)) / 24000
    return (np.sin(2 * np.pi * 200 * t) * 7000).astype(np.int16), 24000


def tiny_video(out: Path, seconds: float = 1.5) -> None:
    subprocess.run([config.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=832x480:rate=16:d={seconds}",
                    "-pix_fmt", "yuv420p", str(out)], check=True)


def fake_zerogpu(image, prompt, out, *, seconds=3.5, seed=42):
    CALLS["video"] += 1
    tiny_video(out, 1.5)                                    # shorter than its shot on purpose
    return {"file": str(out), "space": "test/space", "seconds_requested": seconds, "wall_seconds": 1.0}


def health(*ok):
    return mock.patch("studio.film_studio.providers.health", lambda pid: {"available": pid in ok, "detail": "test"})


def studio_on():
    settings.set("film_studio.enabled", True)
    settings.set("film_studio.auto_approve_refs", True)
    perms = db.loads(db.q1("SELECT permissions FROM workspaces WHERE id=1")["permissions"], {}) or {}
    db.ex("UPDATE workspaces SET permissions=? WHERE id=1", (json.dumps({**perms, "film_studio": True}),))


def new_film() -> int:
    studio_on()
    with mock.patch.object(worker, "enqueue", return_value=1):
        return jobs.create(channels.holder(1), SCRIPT, no_upload=True)["production_ids"][0]


def run_all(pid, video=True, zgen=fake_zerogpu):
    ok = ("cloudflare_flux", "ffmpeg_camera") + (("hf_zerogpu",) if video else ())
    with health(*ok), mock.patch("studio.media.visuals.generate_cloudflare", fake_image), \
            mock.patch("studio.media.tts._synth_sentence", fake_tts), \
            mock.patch("studio.media.tts.kokoro_installed", lambda: True), \
            mock.patch.object(film, "_devanagari", lambda lines: ["देव " + x for x in lines]), \
            mock.patch.object(zerogpu, "generate", zgen):
        pipeline.run_production({"production_id": pid}, {})
    return pipeline.load(pid)


class GenerationTests(unittest.TestCase):
    def setUp(self):
        CALLS["video"] = 0
        settings.set("film_studio.video_shots", "key")
        settings.set("film_studio.video_clips_per_run", 4)

    def test_key_shots_get_real_video_and_the_rest_are_labelled(self):
        p = run_all(new_film())
        self.assertEqual(p["status"], "awaiting_approval", p.get("error"))
        shots = store.shots(p["id"], 1)
        kinds = {s["shot_type"]: s["motion_type"] for s in shots}
        self.assertEqual(kinds["insert"], "video")                       # the coins: real motion
        self.assertTrue(any(s["motion_type"] == "camera_move" for s in shots))
        self.assertFalse(any(s["motion_type"] in ("planned", "static") for s in shots))
        self.assertLessEqual(CALLS["video"], 4)
        exp = edit.export_dir(p["id"])
        for f in ("film.mp4", "film.srt", "timeline.json", "shot_manifest.json", "metadata.json", "licences.json",
                  "quality_report.json"):
            self.assertTrue((exp / f).exists(), f)
        manifest = json.loads((exp / "shot_manifest.json").read_text())
        self.assertTrue(any(m["motion"] == "generated video (continuous motion)" for m in manifest))
        rep = json.loads((exp / "quality_report.json").read_text())
        self.assertIn("lip-sync", rep["unverified"])
        self.assertIn("character consistency", rep["unverified"])

    def test_short_clip_is_never_looped_or_slowed(self):
        p = run_all(new_film())
        tl = json.loads((edit.export_dir(p["id"]) / "timeline.json").read_text())
        insert = next(c for c in tl if c["kind"] == "shot" and c["shot_type"] == "insert")
        clip = Path(film.workdir(p["id"]) / "edit" / f"c{tl.index(insert):04d}_b.mp4")
        self.assertTrue(clip.exists())                                    # the rest of the shot is its key picture
        dur = float(subprocess.run([config.FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of",
                                    "csv=p=0", str(edit.export_dir(p["id"]) / "film.mp4")],
                                   capture_output=True, text=True).stdout)
        self.assertAlmostEqual(dur, tl[-1]["end"], delta=1.0)

    def test_quota_used_up_key_mode_carries_on_all_mode_waits(self):
        def quota(*a, **k):
            raise zerogpu.QuotaExceeded("used up")
        p = run_all(new_film(), zgen=quota)
        self.assertEqual(p["status"], "awaiting_approval", p.get("error"))
        notes = [s["continuity"].get("video_wanted", "") for s in store.shots(p["id"], 1)]
        self.assertTrue(any("free GPU minutes are used up" in n for n in notes))
        settings.set("film_studio.video_shots", "all")
        pid = new_film()
        with self.assertRaises(zerogpu.QuotaExceeded) as cm:
            run_all(pid, zgen=quota)
        self.assertTrue(cm.exception.temporary)                           # the worker waits, no attempt used

    def test_failures_are_recorded_and_earlier_takes_kept(self):
        def broken(*a, **k):
            raise zerogpu.SpaceError("bad gateway")
        p = run_all(new_film(), zgen=broken)
        insert = next(s for s in store.shots(p["id"], 1) if s["shot_type"] == "insert")
        takes = store.takes(insert["id"], 1)
        self.assertEqual([t["status"] for t in takes], ["failed", "ready"])
        self.assertEqual(insert["motion_type"], "camera_move")
        self.assertIn("real video failed", insert["continuity"]["video_wanted"])

    def test_resume_skips_finished_shots(self):
        pid = new_film()
        p = run_all(pid, video=False)
        before = {s["id"]: s["current_take"] for s in store.shots(pid, 1)}
        first = next(iter(before))
        store.set_shot(first, 1, current_take=None, status="queued")
        made = {}
        with health("cloudflare_flux", "ffmpeg_camera"), mock.patch("studio.media.visuals.generate_cloudflare",
                                                                      fake_image):
            made = generate.run(pipeline.load(pid), film._gen, film.workdir(pid))
        self.assertEqual(made["camera_move"], 1)                          # only the reopened shot
        self.assertEqual(p["status"], "awaiting_approval")

    def test_upgrade_to_video_keeps_the_old_take_until_video_succeeds(self):
        pid = new_film()
        run_all(pid, video=False)
        wide = next(s for s in store.shots(pid, 1) if s["shot_type"] in ("wide", "establishing"))
        generate.upgrade_to_video(wide["id"], 1)
        with health("cloudflare_flux", "ffmpeg_camera", "hf_zerogpu"), \
                mock.patch.object(zerogpu, "generate", fake_zerogpu):
            res = generate.run(pipeline.load(pid), film._gen, film.workdir(pid))
        self.assertEqual(res["upgraded"], 1)
        self.assertEqual(store.shot(wide["id"], 1)["motion_type"], "video")
        self.assertEqual(len(store.takes(wide["id"], 1)), 2)


class SyncAndQaTests(unittest.TestCase):
    def test_lines_attach_to_their_speaker_and_set_shot_length(self):
        pid = new_film()
        p = run_all(pid, video=False)
        for s in store.shots(pid, 1):
            if s["dialogue"]:
                d = s["dialogue"][0]
                self.assertIn(d["speaker"], s["characters"])
                self.assertAlmostEqual(s["target_seconds"], d["seconds"] + audio.GAP, places=2)
                self.assertIn("not supported", s["continuity"]["audio"]["lipsync"])
        self.assertEqual(p["package"]["audio_sync"]["warnings"], [])

    def test_off_script_shots_retry_then_go_to_review(self):
        pid = new_film()
        run_all(pid, video=False)
        sid = store.shots(pid, 1)[0]["id"]
        settings.set("film_studio.max_retries_per_shot", 1)
        try:
            first = qa.regenerate(pipeline.load(pid), [sid])
            self.assertEqual(first["retried"], [sid])
            store.add_take(sid, 1, provider="ffmpeg_camera", motion_type="camera_move", file_path="/x.png")
            second = qa.regenerate(pipeline.load(pid), [sid])
            self.assertEqual(second["escalated"], [sid])
            self.assertEqual(store.shot(sid, 1)["status"], "needs_review")
        finally:
            settings.set("film_studio.max_retries_per_shot", 2)

    def test_technical_checks_catch_a_missing_file(self):
        checks = qa.technical("/nope/film.mp4", None, 10.0, ["1.2"])
        self.assertFalse(checks[0]["ok"])
        self.assertFalse(checks[1]["ok"])


if __name__ == "__main__":
    unittest.main()
