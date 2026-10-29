"""Phase 10 — Shorts from a finished film. Proposals only; the owner chooses which to make, and a made Short is a
separate download-only item that is never published automatically.

A proposal starts where a line starts and ends after a laugh (the payoff) or at the end of a line, so dialogue is
never cut mid-sentence; it is 15–60 s and keeps enough of the exchange for context. The vertical version keeps the
whole 16:9 picture (with its subtitles) centred over a soft, blurred fill — nothing important is cropped away.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import config, db, logs
from ..media import render

MIN_S, MAX_S, MAX_PROPOSALS = 15.0, 60.0, 3


def propose(tl: list[dict[str, Any]]) -> list[dict[str, Any]]:
    shots = [c for c in tl if c["kind"] == "shot"]
    cands = []
    for i, start in enumerate(shots):
        if not start.get("subtitle"):
            continue                                        # begin on a spoken line
        lines, video = 0, 0
        for j in range(i, len(shots)):
            c = shots[j]
            if c["scene"] != start["scene"]:
                break
            lines += 1 if c.get("subtitle") else 0
            video += 1 if c.get("motion_type") in ("video", "dialogue_video") else 0
            dur = c["end"] - start["start"]
            if dur > MAX_S:
                break
            payoff = c["shot_type"] == "reaction" or (c.get("subtitle") and j + 1 < len(shots) and
                                                      shots[j + 1]["shot_type"] == "reaction")
            ends_ok = payoff or (c.get("subtitle") and (j + 1 == len(shots) or shots[j + 1]["scene"] != c["scene"]))
            if dur >= MIN_S and lines >= 2 and ends_ok:
                end = shots[j + 1]["end"] if (c.get("subtitle") and j + 1 < len(shots) and
                                              shots[j + 1]["shot_type"] == "reaction") else c["end"]
                score = (3 if payoff else 0) + min(lines, 4) + (2 if video else 0)
                cands.append({"start": start["start"], "end": round(min(end, start["start"] + MAX_S), 3),
                              "scene": start["scene"], "lines": lines, "video_shots": video, "score": score,
                              "first_line": start["subtitle"][:80], "payoff": bool(payoff)})
    cands.sort(key=lambda c: (-c["score"], c["start"]))
    chosen: list[dict[str, Any]] = []
    for c in cands:
        if all(c["end"] <= o["start"] or c["start"] >= o["end"] for o in chosen):
            chosen.append(c)
        if len(chosen) == MAX_PROPOSALS:
            break
    return sorted(chosen, key=lambda c: c["start"])


def make(parent: dict[str, Any], index: int) -> int:
    """Render one proposal as a vertical Short; returns the new (download-only, unpublished) production id."""
    from . import edit
    props = (parent["package"] or {}).get("shorts_proposals") or []
    if not 0 <= index < len(props):
        raise ValueError("no such Shorts proposal")
    pr = props[index]
    src = edit.export_dir(parent["id"]) / "film.mp4"
    if not src.exists():
        raise ValueError("the film isn't finished yet")
    out = edit.export_dir(parent["id"]) / f"short_{index + 1}.mp4"
    w, h = 1080, 1920
    graph = (f"[0:v]split=2[bg][fg];[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
             f"gblur=sigma=28,eq=brightness=-0.08[b];[fg]scale={w}:-2[f];[b][f]overlay=(W-w)/2:(H-h)/2,"
             f"fps={config.FPS},format=yuv420p[v]")
    render.ffmpeg(["-ss", f"{pr['start']:.3f}", "-to", f"{pr['end']:.3f}", "-i", str(src), "-filter_complex", graph,
                   "-map", "[v]", "-map", "0:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a",
                   "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)])
    title = f"{parent['title'][:70]} — {pr['first_line'][:24]}".strip()
    pkg = {"title": title, "description": f"From the short film “{parent['title']}”.\n\nMade with AI-generated images "
                                          "and voices.", "tags": (parent["package"].get("tags") or [])[:10] + ["shorts"],
           "short_of": parent["id"], "proposal": pr}
    pid = db.insert("productions", {
        "format": "short", "title": title, "status": "awaiting_approval", "stage": "qa", "kind": "film_short",
        "parent_id": parent["id"], "channel_id": parent.get("channel_id"), "workspace_id": parent["workspace_id"],
        "language": parent.get("language"), "video_path": str(out), "thumbnail_path": parent.get("thumbnail_path"),
        "request": json.dumps({"no_upload": True, "source": "film shorts proposal"}),
        "package": json.dumps(pkg, ensure_ascii=False), "timeline": json.dumps({"total_duration": pr["end"] - pr["start"]}),
        "qa": json.dumps({"passed": Path(out).exists(), "auto_publish_ok": False,
                          "checks": [{"name": "rendered", "ok": Path(out).exists(), "severity": "fail",
                                      "detail": "vertical 1080×1920"}]}),
        "error": "Ready — watch it, then download it or choose a channel to publish it to.",
        "episode": "{}", "test_mode": 0, "created_at": db.iso(), "updated_at": db.iso()})
    logs.event("info", "film", f"Short made from {pr['start']:.0f}–{pr['end']:.0f}s (not published)",
               production_id=parent["id"])
    return pid
