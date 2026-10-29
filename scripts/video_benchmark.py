"""Opt-in real-video benchmark for the Film Studio (uses the free Hugging Face ZeroGPU quota — nothing paid).

Six fixed scenes cover the hard cases: walking, speaking, a car, a tractor, a close-up reaction, wind/water.
For each one a start picture is made with the free picture chain, then a short clip with exactly the provider the
registry chooses; the clip's technical facts and motion diagnostics are recorded. Realism needs human eyes, so
the report leaves a 'manual_realism' field to fill in after watching the clips.

  .venv/bin/python scripts/video_benchmark.py              # show the plan only — no calls
  .venv/bin/python scripts/video_benchmark.py --run --limit 1
  .venv/bin/python scripts/video_benchmark.py --run --only tractor,walking

Results: data/benchmarks/video-<timestamp>/report.json (+ the clips). Never fabricates a result: a scene that was
not run, or failed, says so with the reason.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from studio import config, db  # noqa: E402

SCENES = [
    {"id": "walking", "picture": "A man in his forties in a white kurta standing on a dusty village lane in late "
     "afternoon light, full body, photorealistic film still",
     "shot": {"action": "He walks forward along the lane at a relaxed pace, arms swinging naturally",
              "camera": {"movement": "slow tracking"}, "shot_type": "wide"}},
    {"id": "speaking", "picture": "Close-up of a middle-aged Indian woman at a kitchen doorway, warm window light, "
     "photorealistic film still",
     "shot": {"dialogue": [{"name": "She", "text": "..."}], "camera": {"movement": "static"}, "shot_type": "close_up"}},
    {"id": "car", "picture": "An old white car parked on an empty rural road between green fields, daylight, "
     "photorealistic film still",
     "shot": {"action": "The car pulls away and drives down the road away from the camera",
              "camera": {"movement": "static"}, "shot_type": "wide"}},
    {"id": "tractor", "picture": "A red tractor at the edge of a ploughed field at sunrise, photorealistic film still",
     "shot": {"action": "The tractor rolls forward across the field, wheels turning, a little dust behind it",
              "camera": {"movement": "slow pan"}, "shot_type": "wide"}},
    {"id": "reaction", "picture": "Close-up portrait of an elderly man with glasses looking at something off-screen, "
     "soft evening light, photorealistic film still",
     "shot": {"shot_type": "reaction", "camera": {"movement": "static"}}},
    {"id": "wind_water", "picture": "A village pond with reeds and a neem tree on the bank, morning light, no people, "
     "photorealistic film still",
     "shot": {"action": "Wind ripples the water and sways the reeds and leaves", "camera": {"movement": "static"},
              "shot_type": "establishing"}},
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="store_true", help="really call the free providers (uses free quota)")
    ap.add_argument("--limit", type=int, default=2, help="at most this many clips (default 2, to save quota)")
    ap.add_argument("--only", default="", help="comma-separated scene ids")
    ap.add_argument("--seconds", type=float, default=2.0, help="clip length (short clips save quota)")
    a = ap.parse_args()
    scenes = [s for s in SCENES if not a.only or s["id"] in a.only.split(",")][: max(0, a.limit)]
    if not a.run:
        print("Plan (nothing called — add --run):")
        for s in scenes:
            print(f"  {s['id']:<11} {s['shot'].get('action') or s['shot'].get('shot_type')}")
        return 0
    config.ensure_dirs()
    db.migrate()
    from studio.creative import film
    from studio.film_studio import generate, providers, qa
    out_dir = config.DATA_DIR / "benchmarks" / f"video-{datetime.now():%Y%m%d-%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {"started": db.iso(), "seconds": a.seconds, "results": []}
    for s in scenes:
        row = {"id": s["id"], "picture_prompt": s["picture"], "status": "not run", "manual_realism": None}
        shot = {"id": 0, "scene_no": 1, "shot_no": 2, "characters": [], "action": "", "dialogue": [],
                "lighting": "", "target_seconds": a.seconds, "continuity": {}, **s["shot"]}
        row["motion_prompt"] = generate.motion_prompt(shot, {"location": s["picture"]})
        start = out_dir / f"{s['id']}_start.png"
        try:
            film._gen(s["picture"], start, size=(832, 480))
            row["start_image"] = str(start)
            pick = providers.choose("video_i2v")
            row["provider_chosen"] = pick["provider"]
            if pick["motion_type"] != "video":
                row.update(status="skipped", reason=pick.get("limitation") or "no free video provider")
            else:
                t0 = time.time()
                clip = out_dir / f"{s['id']}.mp4"
                info = providers.run_video(pick["provider"], start, row["motion_prompt"], clip, seconds=a.seconds)
                row.update(status="ok", provider=info["provider"], model=info["model"],
                           runtime_s=round(time.time() - t0, 1), video=info.get("video"),
                           motion=qa.motion_stats(clip), clip=str(clip))
        except Exception as e:  # noqa: BLE001 - every failure is reported as it is
            row.update(status="failed", reason=f"{type(e).__name__}: {str(e)[:300]}",
                       error_kind=getattr(e, "kind", None))
        report["results"].append(row)
        print(f"{s['id']:<11} {row['status']:<8} {row.get('reason') or row.get('video') or ''}")
        if row["status"] == "failed" and "QuotaExceeded" in row.get("reason", ""):
            break                                           # no point asking again before the reset
    report["finished"] = db.iso()
    (out_dir / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))
    print("report:", out_dir / "report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
