"""Character bible: persistent, per-channel characters with a fixed design and a fixed voice.

A design is a small set of parameters the renderer draws exactly the same way in every scene and
every episode — that is what keeps a recurring character recognisable. Voices are assigned once
and never change for that character.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import config, db

COLOURS = {
    "red": (220, 50, 47), "blue": (38, 120, 220), "navy": (30, 50, 110), "green": (46, 160, 67),
    "yellow": (245, 196, 40), "orange": (240, 130, 30), "purple": (130, 70, 180), "pink": (232, 110, 160),
    "white": (240, 240, 240), "black": (35, 35, 40), "grey": (130, 135, 145), "brown": (130, 85, 50),
    "maroon": (128, 30, 45), "teal": (20, 150, 150), "cream": (240, 228, 200), "khaki": (190, 170, 110),
    "saffron": (255, 153, 51), "gold": (212, 175, 55),
}
SKIN = {"light": (246, 214, 180), "medium": (222, 172, 130), "wheat": (205, 150, 105), "dark": (150, 100, 70),
        "pale": (220, 225, 230), "green": (150, 200, 120)}
HAIR_COLOUR = {"black": (30, 25, 25), "brown": (90, 60, 40), "grey": (170, 170, 175), "white": (235, 235, 235),
               "red": (160, 60, 30), "none": (30, 25, 25)}
OPTIONS = {
    "gender": ["male", "female"],
    "age": ["child", "young", "adult", "old"],
    "body": ["thin", "average", "round", "tall"],
    "skin": list(SKIN),
    "outfit": list(COLOURS),
    "outfit2": list(COLOURS),
    "hair": ["short", "long", "curly", "bun", "bald", "spiky", "ponytail"],
    "hair_colour": list(HAIR_COLOUR),
    "accessory": ["none", "cape", "glasses", "mustache", "turban", "cap", "mask", "tie", "hat", "bindi", "beard",
                  "cape_mask"],
}
DEFAULT_DESIGN = {"gender": "male", "age": "adult", "body": "average", "skin": "medium", "outfit": "blue",
                  "outfit2": "navy", "hair": "short", "hair_colour": "black", "accessory": "none"}
# Sarvam bulbul:v3 speakers by voice type (Kokoro fallback when Sarvam is not configured)
VOICES = {
    ("male", "adult"): ["amit", "rahul", "kabir", "varun", "aditya", "shubh"],
    ("male", "young"): ["aditya", "shubh", "varun", "rahul"],
    ("male", "old"): ["kabir", "amit"],
    ("male", "child"): ["shubh", "aditya"],
    ("female", "adult"): ["priya", "neha", "ritu", "kavya"],
    ("female", "young"): ["kavya", "neha", "priya"],
    ("female", "old"): ["ritu", "priya"],
    ("female", "child"): ["kavya", "neha"],
}
NARRATOR_KEY = "NARRATOR"


def normalise_design(d: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(DEFAULT_DESIGN)
    for k, opts in OPTIONS.items():
        v = str((d or {}).get(k, "")).lower().strip()
        if v in opts:
            out[k] = v
    return out


def _row(r: dict[str, Any]) -> dict[str, Any]:
    r["design"] = normalise_design(db.loads(r.get("design"), {}))
    r["voice"] = db.loads(r.get("voice"), {})
    r["catchphrases"] = db.loads(r.get("catchphrases"), [])
    return r


def for_channel(channel_id: int, include_draft: bool = True, include_all: bool = False) -> list[dict[str, Any]]:
    """The channel's recurring cast. Retired characters and one-video ('oneoff') characters are never offered to
    new episodes; include_all lists them too (for the Characters page)."""
    if include_all:
        cond = ""
    elif include_draft:
        cond = " AND status NOT IN ('retired','oneoff')"
    else:
        cond = " AND status='approved'"
    return [_row(r) for r in db.q("SELECT * FROM characters WHERE channel_id=?" + cond + " ORDER BY id", (channel_id,))]


def get(char_id: int) -> dict[str, Any] | None:
    r = db.q1("SELECT * FROM characters WHERE id=?", (char_id,))
    return _row(r) if r else None


def key_of(c: dict[str, Any]) -> str:
    return f"C{c['id']}"


def _pick_voice(channel_id: int, design: dict[str, Any]) -> dict[str, Any]:
    used = {(db.loads(r["voice"], {}) or {}).get("voice_id")
            for r in db.q("SELECT voice FROM characters WHERE channel_id=?", (channel_id,))}
    choices = VOICES.get((design["gender"], design["age"]), VOICES[(design["gender"], "adult")])
    # same voice type first, then any unused voice of that gender, so no two characters on a channel sound alike
    same_gender = [v for (g, _), vs in VOICES.items() if g == design["gender"] for v in vs]
    voice_id = next((v for v in choices + same_gender if v not in used), choices[0])
    pace = {"child": 1.12, "young": 1.05, "adult": 1.0, "old": 0.92}[design["age"]]
    return {"engine": "sarvam", "voice_id": voice_id, "pace": pace}


def create(channel_id: int, name: str, design: dict[str, Any], *, role: str = "", personality: str = "",
           catchphrases: list[str] | None = None, relationships: str = "", rules: str = "",
           voice: dict[str, Any] | None = None, status: str = "approved") -> int:
    d = normalise_design(design)
    cid = db.insert("characters", {
        "channel_id": channel_id, "name": name.strip()[:60], "role": role[:200], "design": json.dumps(d),
        "personality": personality[:600], "voice": json.dumps(voice or _pick_voice(channel_id, d)),
        "catchphrases": json.dumps((catchphrases or [])[:5], ensure_ascii=False), "relationships": relationships[:600],
        "rules": rules[:600], "status": status, "created_at": db.iso(), "updated_at": db.iso()})
    render_reference(cid)
    return cid


def render_reference(char_id: int) -> Path:
    """Approved reference sheet (the same drawing every episode uses)."""
    from . import cartoon
    c = get(char_id)
    out = config.DATA_DIR / "channels" / f"{c['channel_id']:04d}" / "characters" / f"{char_id:05d}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    cartoon.reference_sheet(c, out)
    db.update("characters", char_id, {"reference_path": str(out), "updated_at": db.iso()})
    return out


def bible_text(chars: list[dict[str, Any]]) -> str:
    """Character bible as given to the screenwriter."""
    lines = []
    for c in chars:
        d = c["design"]
        lines.append(f"- {key_of(c)} = {c['name']} ({d['gender']}, {d['age']}; {c['role'] or 'character'}). "
                     f"Personality: {c['personality'] or '-'}. Catchphrases: {', '.join(c['catchphrases']) or '-'}. "
                     f"Relationships: {c['relationships'] or '-'}. Rules: {c['rules'] or '-'}")
    return "\n".join(lines) or "(no recurring characters yet)"
