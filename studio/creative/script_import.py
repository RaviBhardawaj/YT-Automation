"""Read a screenplay the user wrote, as written: scenes, who says what, the action around each line, each
character's look, the location and the title. Nothing is rewritten — the film is made from the user's own words.

Understands the common format:
    SCENE 3 — THE FIRST SUSPICIOUS MOVE
    The card game becomes more competitive.          (action)
    BALBIR:                                            (speaker, optionally ", WHISPERING")
    "Chal Jagga, teri baari."                          (the line, quotes optional)
and a character section such as
    CHARACTER 1: BALBIR — THE FARMER
    Age: 45.  Appearance: ...  Clothing: ...  Voice: ...
"""
from __future__ import annotations

import re
from typing import Any

SCENE_RX = re.compile(r"^\s*(?:SCENE|SCENE NO\.?|दृश्य)\s*(\d+)\s*[—\-–:.]*\s*(.*)$", re.I)
SPEAKER_RX = re.compile(r"^\s*([A-Z][A-Z .'\-]{1,30}?)(?:\s*,\s*([A-Z][A-Z ,'\-]{0,30}))?\s*:\s*(.*)$")
CHAR_RX = re.compile(r"^\s*CHARACTER\s*\d+\s*[:.\-]\s*([A-Za-z .'\-]+?)\s*(?:[—\-–]\s*(.*))?$", re.I)
FIELD_RX = re.compile(r"^\s*(Age|Appearance|Clothing|Personality|Voice|Acting style|Role in story)\s*:\s*(.+)$", re.I)
NOT_SPEAKERS = {"TIME", "CAMERA DIRECTION", "OPENING SHOT", "END TITLE", "TITLE", "LOCATION", "GENRE", "DURATION",
                "NOTE", "INT", "EXT", "CUT TO", "FADE OUT", "FADE IN", "MUSIC", "SFX", "OPTIONAL CLOSING LINE",
                "AGE", "APPEARANCE", "CLOTHING", "PERSONALITY", "VOICE", "ACTING STYLE", "ROLE IN STORY", "INCLUDE"}
SECTION_RX = re.compile(r"^\s*\d+\.\s+[A-Z][A-Z &'\-]+$")
FEMALE_RX = re.compile(r"\b(woman|women|female|girl|she|her|mother|wife|aunty|didi|lady)\b", re.I)


def _clean(s: str) -> str:
    return s.strip().strip('"“”').strip()


def looks_like_screenplay(text: str) -> bool:
    """A real script: at least two scene headings and a few 'NAME:' dialogue lines."""
    lines = (text or "").splitlines()
    scenes = sum(1 for ln in lines if SCENE_RX.match(ln))
    speakers = sum(1 for ln in lines if (m := SPEAKER_RX.match(ln)) and m.group(1).strip().upper() not in NOT_SPEAKERS)
    return scenes >= 2 and speakers >= 4


def _title(lines: list[str]) -> str:
    for ln in lines:
        m = re.match(r"^\s*Title\s*:\s*(.+)$", ln, re.I)
        if m:
            return _clean(m.group(1))[:90]
    for ln in lines[:6]:                                   # an all-caps title line near the top
        s = ln.strip()
        if s and s.upper() == s and 6 <= len(s) <= 80 and "PROMPT" not in s and not SCENE_RX.match(s):
            return s.title()[:90]
    return "Untitled film"


def _characters(lines: list[str]) -> list[dict[str, Any]]:
    chars: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for ln in lines:
        m = CHAR_RX.match(ln)
        if m:
            cur = {"name": m.group(1).strip().title(), "role": _clean(m.group(2) or "").title(), "fields": {}}
            chars.append(cur)
            continue
        if cur is not None:
            f = FIELD_RX.match(ln)
            if f:
                cur["fields"][f.group(1).lower()] = f.group(2).strip()
            elif SECTION_RX.match(ln) or SCENE_RX.match(ln):
                cur = None
    return chars


def _section(lines: list[str], heading_words: tuple[str, ...]) -> str:
    out, on = [], False
    for ln in lines:
        if SECTION_RX.match(ln):
            on = any(w in ln.upper() for w in heading_words)
            continue
        if on and ln.strip():
            out.append(ln.strip().lstrip("*•- ").strip())
    return " ".join(out)[:900]


def parse(text: str) -> dict[str, Any]:
    lines = (text or "").splitlines()
    title = _title(lines)
    declared = _characters(lines)
    group = " ".join(ln for ln in lines if re.search(r"all (five|four|three|the) characters", ln, re.I))
    scenes: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    speaker: str | None = None
    action: list[str] = []
    for raw in lines:
        ln = raw.strip()
        if not ln:
            continue
        m = SCENE_RX.match(ln)
        if m:
            cur = {"number": int(m.group(1)), "heading": _clean(m.group(2)).title() or f"Scene {m.group(1)}",
                   "action": [], "lines": [], "beats": []}
            scenes.append(cur)
            speaker, action = None, []
            continue
        if cur is None:
            continue
        if SECTION_RX.match(ln) or re.match(r"^\s*(END TITLE|END CREDITS|Optional closing line|THE END)\b", ln, re.I):
            cur, speaker = None, None                      # the screenplay (or its last scene) ended
            continue
        sm = SPEAKER_RX.match(ln)
        if sm and sm.group(1).strip().upper() not in NOT_SPEAKERS:
            speaker = sm.group(1).strip().title()
            manner = (sm.group(2) or "").strip().lower()
            rest = _clean(sm.group(3))
            if rest:                                       # "NAME: line" on one line
                cur["lines"].append({"speaker": speaker, "text": rest, "manner": manner,
                                     "action": " ".join(action)[-200:]})
                cur["beats"].append({"kind": "line", "line": len(cur["lines"]) - 1})
                speaker, action = None, []
            else:
                cur["_manner"] = manner
            continue
        if speaker:                                        # the line under "NAME:"
            cur["lines"].append({"speaker": speaker, "text": _clean(ln), "manner": cur.pop("_manner", ""),
                                 "action": " ".join(action)[-200:]})
            cur["beats"].append({"kind": "line", "line": len(cur["lines"]) - 1})
            speaker, action = None, []
            continue
        if re.match(r"^\s*(Time|CAMERA DIRECTION|OPENING SHOT)\s*:", ln, re.I):
            continue
        action.append(ln)
        cur["action"].append(ln)
        cur["beats"].append({"kind": "action", "text": ln})
    scenes = [s for s in scenes if s["lines"]]
    for s in scenes:
        s.pop("_manner", None)
        s["action"] = " ".join(s["action"])[:600]
    names = list(dict.fromkeys(ln["speaker"] for s in scenes for ln in s["lines"]))
    by_name = {c["name"].lower(): c for c in declared}
    cast = []
    for i, name in enumerate(names):
        c = by_name.get(name.lower(), {"name": name, "role": "", "fields": {}})
        f = c["fields"]
        look = "; ".join(x for x in (f.get("age") and f"age {f['age'].rstrip('.')}", f.get("appearance"),
                                     f.get("clothing")) if x)
        about = " ".join(x for x in (look, f.get("personality"), group) if x)
        cast.append({"key": f"F{i + 1}", "name": c["name"], "role": c.get("role", ""), "look": look[:500],
                     "bible": {k: v[:400] for k, v in f.items()},
                     "personality": (f.get("personality") or "")[:300], "voice_note": (f.get("voice") or "")[:200],
                     "gender": "female" if FEMALE_RX.search(about) else "male"})
    keys = {c["name"].lower(): c["key"] for c in cast}
    for s in scenes:
        for ln in s["lines"]:
            ln["speaker"] = keys[ln["speaker"].lower()]
    end = next((_clean(lines[i + 1]) for i, ln in enumerate(lines[:-1])
                if re.match(r"^\s*Optional closing line\s*:", ln, re.I)), "")
    return {"title": title, "cast": cast, "scenes": scenes,
            "location": _section(lines, ("ENVIRONMENT", "SET DESIGN", "LOCATION")) or
            next((ln.split(":", 1)[1].strip() for ln in lines if re.match(r"^\s*Location\s*:", ln, re.I)), ""),
            "lighting": next((ln.split(":", 1)[1].strip() for ln in lines if re.match(r"^\s*Lighting\s*:", ln, re.I)), ""),
            "closing_line": end, "target_seconds": _duration(lines)}


def _duration(lines: list[str]) -> float | None:
    """'Duration: Exactly 10 minutes' / '90 seconds' → seconds (None when the script doesn't say)."""
    for ln in lines:
        m = re.match(r"^\s*Duration\s*:\s*(.+)$", ln, re.I)
        if not m:
            continue
        n = re.search(r"(\d+(?:\.\d+)?)\s*(minutes?|mins?|seconds?|secs?|s\b)", m.group(1), re.I)
        if n:
            v = float(n.group(1))
            return v * 60 if n.group(2).lower().startswith("m") else v
    return None
