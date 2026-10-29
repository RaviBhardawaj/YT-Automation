"""Casting: a character bible and approved reference pictures for every character and location.

Each character and the location get a *proposed* reference picture (free FLUX). Shots are only filmed from
*approved* references — the owner approves them on the Cast review page (or the account opts in to auto-approval).
Regenerating or editing a reference makes a new version; earlier versions stay, and an approved picture is never
replaced without a new approval.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import db, logs, settings
from . import store

BIBLE_FIELDS = ("age", "appearance", "clothing", "personality", "acting style", "voice", "role in story")


def _bible(c: dict[str, Any]) -> dict[str, Any]:
    b = dict(c.get("bible") or {})
    b.setdefault("appearance", c.get("look", ""))
    b["gender"] = c.get("gender", "male")
    if c.get("role"):
        b.setdefault("role", c["role"])
    return {k: v for k, v in b.items() if v}


def character_prompt(name: str, bible: dict[str, Any], notes: str = "") -> str:
    who = "man" if bible.get("gender", "male") == "male" else "woman"
    looks = "; ".join(str(bible[k]) for k in ("age", "appearance", "clothing") if bible.get(k))
    return (f"Photorealistic waist-up portrait photograph of {name}, an ordinary North Indian {who}"
            + (f" ({bible['age']} years)" if str(bible.get("age", "")).rstrip(".").isdigit() else "")
            + f": {looks}. {notes} Real skin texture and natural imperfections, neutral expression, looking at the "
              "camera, plain village wall behind, soft daylight").replace("  ", " ")


def location_prompt(description: str, notes: str = "") -> str:
    loc = re.sub(r"(?i)^the entire film takes place primarily in one\s*", "", description or "").strip()
    return (f"Cinematic wide establishing shot, no people: {loc[:420] or 'a village courtyard in North India'}. "
            f"{notes} Natural afternoon sunlight, real place, lived-in details").replace("  ", " ")


def propose(production_id: int, ws: int, film: dict[str, Any]) -> int:
    """Create the bible entries (once). Returns how many references were added."""
    have = {(r["kind"], r["ref_key"]) for r in store.refs(production_id, ws)}
    added = 0
    for c in film.get("cast") or []:
        if ("character", c["key"]) not in have:
            store.add_ref(production_id, ws, "character", c["key"], c["name"], c.get("look", ""),
                          meta={"bible": _bible(c)})
            added += 1
    if ("location", "LOC") not in have:
        store.add_ref(production_id, ws, "location", "LOC", "Main location", film.get("location", ""),
                      meta={"lighting": film.get("lighting", "")})
        added += 1
    return added


def latest(production_id: int, ws: int) -> dict[tuple[str, str], dict[str, Any]]:
    """Per reference: its newest version (what the review page shows) with "approved" = the approved version."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in store.refs(production_id, ws):
        r["meta"] = db.loads(r.get("meta"), {}) or {}
        groups.setdefault((r["kind"], r["ref_key"]), []).append(r)
    out = {}
    for key, rows in groups.items():
        newest = max(rows, key=lambda r: r["version"])
        newest["approved"] = next((r for r in rows if r["status"] == "approved"), None)
        newest["versions"] = len(rows)
        out[key] = newest
    return out


def generate(ref_id: int, ws: int, out_dir: Path, gen) -> dict[str, Any]:
    """Make the picture for one reference version (gen = the film's free image function)."""
    r = db.q1("SELECT * FROM film_refs WHERE id=? AND workspace_id=?", (ref_id, ws))
    if not r:
        raise store.NotFound(f"reference #{ref_id} not found")
    meta = db.loads(r["meta"], {}) or {}
    notes = meta.get("notes", "")
    out = out_dir / f"{r['kind']}_{r['ref_key']}_v{r['version']}.png"
    if r["kind"] == "character":
        gen(character_prompt(r["name"], meta.get("bible") or {"appearance": r["description"]}, notes), out,
            size=(768, 768))
    else:
        gen(location_prompt(r["description"], notes), out, size=(1344, 768))
    db.update("film_refs", ref_id, {"file_path": str(out), "updated_at": db.iso()})
    if settings.get("film_studio.auto_approve_refs"):
        store.set_ref_status(ref_id, ws, "approved")
    return db.q1("SELECT * FROM film_refs WHERE id=?", (ref_id,))


def new_version(ref_id: int, ws: int, *, description: str | None = None, notes: str = "",
                bible: dict[str, Any] | None = None) -> int:
    """A fresh version to regenerate (optionally with an edited description, bible or extra direction)."""
    r = db.q1("SELECT * FROM film_refs WHERE id=? AND workspace_id=?", (ref_id, ws))
    if not r:
        raise store.NotFound(f"reference #{ref_id} not found")
    meta = db.loads(r["meta"], {}) or {}
    if bible:
        meta["bible"] = {**(meta.get("bible") or {}), **{k: str(v)[:400] for k, v in bible.items()
                                                         if k in BIBLE_FIELDS}}
    meta["notes"] = notes[:400]
    return store.add_ref(r["production_id"], ws, r["kind"], r["ref_key"], r["name"],
                         r["description"] if description is None else description[:2000], meta=meta)


def missing(production_id: int, ws: int) -> list[str]:
    """Names of references that still need an approved picture."""
    out = []
    for (kind, key), r in latest(production_id, ws).items():
        ok = r.get("approved") and r["approved"].get("file_path") and Path(r["approved"]["file_path"]).exists()
        if not ok:
            out.append(r["name"])
    return out


def apply_to_film(production_id: int, ws: int, film: dict[str, Any]) -> None:
    """Point the film at the approved pictures (shots are made from these)."""
    refs = latest(production_id, ws)
    for c in film.get("cast") or []:
        a = (refs.get(("character", c["key"])) or {}).get("approved")
        if a:
            c["portrait"] = a["file_path"]
            c["bible"] = (db.loads(a.get("meta"), {}) or {}).get("bible") or c.get("bible")
    loc = (refs.get(("location", "LOC")) or {}).get("approved")
    if loc:
        film["location_ref"] = loc["file_path"]


def log(production_id: int, msg: str) -> None:
    logs.event("info", "film", msg, production_id=production_id)
