"""Story backgrounds for animated episodes: one free AI-drawn cartoon set per scene, showing what the story is about.

The writer's scene summary ("the diya sends a rainbow pulse and the room's lights flicker like a disco") becomes an
EMPTY set — location, time of day, the story's props and light effects, no people — drawn in a flat 2D cartoon style
by the free image models (Cloudflare FLUX.2, then the Hugging Face FLUX Space). The channel's own characters are then
drawn on top by the cartoon renderer, so faces and outfits stay exactly the same in every scene and episode.
Scenes in the same location are drawn from the first picture of that place (as a reference), so the room stays the
same room. When every free allowance is spent a scene keeps the built-in drawn set — the video is never held up.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .. import ai, config, db, logs
from ..media import vision, visuals

STYLE = ("Flat 2D vector cartoon background art for a children's TV animation, bold clean outlines, bright cheerful "
         "colours, simple rounded shapes, soft even lighting, wide shot at eye level. The story's key object stands "
         "in the CENTRE of the room against the back wall, clearly visible; the floor in front, left and right is "
         "open and empty (characters will stand there). EMPTY SET: no people, no characters, no faces, no hands, no animals, no text, no letters, "
         "no logos, no watermark.")


class SceneSet(BaseModel):
    scene: int = Field(description="scene number, starting at 1")
    background: str = Field(description="the empty set in English, max 60 words: place, time of day, furniture, and "
                                        "the story's props and visual effects in this scene — never people")
    special: bool = Field(description="true only when this scene needs an object or effect the place doesn't "
                                      "normally have (a glowing magic lamp, lights dancing, a big mess, a party); "
                                      "false when the usual place is enough")


class SetDesign(BaseModel):
    place: str = Field(description="the shared look of the main location in English (layout, colours, furniture), "
                                   "max 40 words, so every scene there is the same place")
    scenes: list[SceneSet]


def design(pkg: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Per scene: {"text": the set to draw, "special": needs story props, "place": the usual place} (English)."""
    listing = "\n".join(f"Scene {i + 1}: {s.get('location')}, {s.get('time')}, mood {s.get('mood')} — {s.get('summary', '')}"
                        for i, s in enumerate(pkg["scenes"]))
    try:
        out, _ = ai.generate(
            "script", "You are the background artist of a children's cartoon. You describe EMPTY sets — the place, "
            "its props and light effects — never people or characters. Output exactly one JSON object.",
            f"EPISODE: {pkg.get('title')}\n{listing}\n\nDescribe the background set for each scene. Show the story's "
            "important objects and effects (for example a glowing magic lamp, colourful lights dancing on the walls) "
            "so a child understands the scene from the picture alone.", SetDesign, max_tokens=2500)
        sets = {s.scene - 1: {"text": f"{out.place} {s.background}", "special": s.special, "place": out.place}
                for s in out.scenes if 0 < s.scene <= len(pkg["scenes"])}
    except Exception as e:  # the writer's summary is still a usable description
        logs.event("warning", "creative", f"set design fallback: {e}"[:200])
        sets = {}
    return {i: sets.get(i) or {"text": f"{s.get('location', 'home')} interior, {s.get('time', 'day')}, "
                                       f"{s.get('summary', '')[:200]}", "special": True,
                               "place": f"{s.get('location', 'home')} interior"}
            for i, s in enumerate(pkg["scenes"])}


# ------------------------------------------------------------------ the channel's set library

def library_dir(channel_id: int) -> Path:
    return config.DATA_DIR / "channels" / str(channel_id) / "sets"


def library_get(channel_id: int, location: str, time: str) -> dict[str, Any] | None:
    for r in db.q("SELECT * FROM set_library WHERE channel_id=? AND location=? AND time=? AND status='active' "
                  "ORDER BY COALESCE(score, 0) DESC, id", (channel_id, location, time)):
        if Path(r["file_path"]).exists():
            return r
    return None


def library_use(row: dict[str, Any]) -> None:
    db.ex("UPDATE set_library SET uses=uses+1, last_used_at=? WHERE id=?", (db.iso(), row["id"]))


def library_add(channel_id: int, workspace_id: int, location: str, time: str, file: Path, description: str,
                provider: str, check: dict[str, Any] | None) -> dict[str, Any]:
    rid = db.insert("set_library", {"channel_id": channel_id, "workspace_id": workspace_id, "location": location,
                                    "time": time, "description": description[:600], "file_path": str(file),
                                    "provider": provider, "score": (check or {}).get("score"),
                                    "created_at": db.iso()})
    return db.q1("SELECT * FROM set_library WHERE id=?", (rid,))


def _draw(prompt: str, out: Path, ref: Path | None) -> str | None:
    """One background from the first free model with allowance left; None when all are spent."""
    for name, make in (("cloudflare", lambda: visuals.generate_cloudflare(prompt, "long", out, "cartoon",
                                                                          refs=[ref] if ref else None)),
                       ("hf_flux", lambda: visuals.generate_hf_flux(prompt, "long", out, "cartoon")),
                       ("local_flux", lambda: visuals.generate_local_flux(prompt, "long", out, "cartoon"))):
        if (name == "cloudflare" and not visuals.cloudflare_ready()) or \
                (name == "local_flux" and not visuals.local_flux_installed()):
            continue
        try:
            make()
            from PIL import Image
            with Image.open(out) as im:
                visuals.trim_bars(im.convert("RGB")).save(out, "PNG")
            return name
        except Exception as e:                     # used up, filtered or offline: try the next free model
            logs.event("info", "creative", f"background by {name} not made: {e}"[:200])
    return None


MAX_FIXES = 2                   # automatic re-prompts per background when the check finds a problem


def _quality(check: dict[str, Any] | None) -> int:
    return -1 if check is None else (0 if check.get("people") else int(check.get("score", 0)))


def draw_checked(want: str, target: Path, ref: Path | None) -> dict[str, Any] | None:
    """Draw a background, look at it, and when it isn't good (people in it, or not showing the story) rewrite the
    prompt from what the check saw and draw again — up to MAX_FIXES times. Keeps the best picture.
    Returns {"by", "check", "attempts", "prompt"} or None when every free image allowance is spent."""
    prompt, best, attempts = f"{want}. {STYLE}", None, []
    for n in range(MAX_FIXES + 1):
        out = target.with_name(f"{target.stem}_try{n + 1}.png")
        by = _draw(prompt, out, ref)
        if not by:
            break
        check = vision.check_art(out.read_bytes(), want=want) if vision.available() else None
        attempts.append({"prompt": prompt[:400], "by": by, "check": check})
        if best is None or _quality(check) > _quality(best["check"]):
            best = {"file": out, "by": by, "check": check, "prompt": prompt}
        if check is None or (not check.get("people") and check.get("score", 0) >= vision.PASS + 1):
            break                                       # good (or nobody can look): stop spending allowance
        fix = check.get("fix") or ("show " + want[:120])
        prompt = (f"{want}. Fix from the last attempt: {fix}. "
                  f"{'There must be NO people, characters, faces, hands or animals anywhere. ' if check.get('people') else ''}"
                  f"{STYLE}")
        logs.event("info", "creative", f"{target.name}: redrawing ({check.get('shows', '')[:60]} → {fix[:80]})")
    if best is None:
        return None
    best["file"].replace(target)
    for extra in target.parent.glob(f"{target.stem}_try*.png"):
        extra.unlink(missing_ok=True)
    return {"by": best["by"], "check": best["check"], "attempts": len(attempts), "prompt": best["prompt"]}


def ensure(pkg: dict[str, Any], out_dir: Path, *, channel_id: int | None = None,
           workspace_id: int | None = None) -> int:
    """Give every scene a drawn story background (kept between renders). Returns how many scenes have one.
    With a channel, plain scenes reuse the channel's set library (drawn once, used in every later episode); only
    scenes with special story props get a new picture, drawn from the library room so it stays the same house."""
    out_dir.mkdir(parents=True, exist_ok=True)
    scenes = pkg["scenes"]
    missing = [i for i, s in enumerate(scenes) if not (s.get("bg_image") and Path(s["bg_image"]).exists())]
    sets = design(pkg) if missing else {}
    first_at: dict[str, Path] = {}
    for i, s in enumerate(scenes):
        if s.get("bg_image") and Path(s["bg_image"]).exists():
            first_at.setdefault(s.get("location", "home"), Path(s["bg_image"]))
    for i in missing:
        s = scenes[i]
        d = sets[i] if isinstance(sets[i], dict) else {"text": sets[i], "special": True, "place": sets[i]}
        loc, tod = s.get("location", "home"), s.get("time", "day")
        lib = library_get(channel_id, loc, tod) if channel_id else None
        if channel_id and not d["special"]:
            if lib is None:                             # first time this place is needed: draw it for the library
                f = library_dir(channel_id) / f"{loc}_{tod}_{db.utcnow():%Y%m%d%H%M%S}_{i + 1}.png"
                f.parent.mkdir(parents=True, exist_ok=True)
                res = draw_checked(f"{d['place']} ({loc}, {tod}), the usual everyday look", f, None)
                if res:
                    lib = library_add(channel_id, workspace_id or 1, loc, tod, f, d["place"], res["by"],
                                      res["check"])
            if lib:
                library_use(lib)
                s["bg_image"], s["bg_by"] = lib["file_path"], f"set library #{lib['id']}"
                first_at.setdefault(loc, Path(lib["file_path"]))
                continue
        target = out_dir / f"scene_{i + 1:02d}.png"
        ref = Path(lib["file_path"]) if lib else first_at.get(loc)
        res = draw_checked(d["text"], target, ref)
        if res:
            s["bg_image"], s["bg_by"], s["bg_prompt"] = str(target), res["by"], d["text"][:600]
            s["bg_check"] = {**(res["check"] or {"note": "no vision model available — not checked"}),
                             "attempts": res["attempts"]}
            first_at.setdefault(loc, target)
    # a scene that couldn't get its own picture borrows the nearest drawn one of the same place (the earlier one
    # first), so an episode never switches between drawn and built-in sets halfway through
    drawn = [i for i, s in enumerate(scenes) if s.get("bg_image") and Path(s["bg_image"]).exists()]
    for i, s in enumerate(scenes):
        if i in drawn:
            continue
        same = [j for j in drawn if scenes[j].get("location") == s.get("location")]
        if same:
            j = min(same, key=lambda j: (abs(j - i), j > i))
            s["bg_image"], s["bg_by"] = scenes[j]["bg_image"], f"reused from scene {j + 1} (free allowance spent)"
    return sum(1 for s in scenes if s.get("bg_image") and Path(s["bg_image"]).exists())
