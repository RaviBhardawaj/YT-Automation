"""KahaaniVerse creative director: research → 10+ scored concepts → backlog → today's story → screenplay → storyboard.

research()    what audiences are watching and searching, from several permitted sources (each one optional — the day
              never depends on one): the official YouTube Data API (titles, views, lengths only; nothing downloaded),
              Google Trends' public daily feed for India, and this channel's own results per genre. Sources that
              are not used, and why, are recorded with the research.
concepts()    at least 10 original story concepts, each answering the director's questions (why it's interesting,
              why viewers care, protagonist, want, conflict, curiosity, emotional core, twist, payoff, what's visually
              memorable) and scored on ten dimensions. Concepts too close to anything the channel made, or to a
              trending title, are dropped. The rest go to the backlog (topics) with their full brief.
pick()        the strongest backlog concept for today's genre (the editorial mix), else the strongest overall.
screenplay()  the chosen concept as an original Hindi/Hinglish screenplay (Devanagari dialogue for the voices): a
              5-second hook, locations, scenes with purposes (hook → setup → conflict → twist → climax → resolution),
              recurring characters from the library with their fixed bibles, and one-off characters with full looks.
storyboard()  the cinematography pass: every line of dialogue is covered, in order, by shots with type, camera angle
              and movement, composition, depth of field, lighting, expression, duration, image / video / negative
              prompts and continuity notes.
The output is the film studio's own film format, so casting, voices, shots, motion, edit and QA are reused.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field

from .. import ai, channels, db, logs
from ..research import dedup
from . import kahaani

GenreKey = Literal["suspense_mystery", "emotional_drama", "psychological_thriller", "village_mystery",
                   "scifi_supernatural"]
UniverseKey = Literal["AARAV", "SIYA", "VEER", "DADAJI"]
SCORE_WEIGHTS = {"curiosity": 1.4, "emotion": 1.3, "originality": 1.3, "trend": 0.8, "visual": 1.0, "feasibility": 1.2,
                 "development": 0.6, "competition_gap": 0.7, "evergreen": 0.7, "relevance": 1.0}
QUERIES = {"suspense_mystery": "hindi suspense mystery story animation", "emotional_drama": "emotional hindi kahani",
           "psychological_thriller": "psychological thriller hindi story", "village_mystery": "gaon ki rahasyamayi kahani",
           "scifi_supernatural": "hindi sci fi animated story"}
WORDS_PER_MINUTE = 105          # spoken dialogue per film minute (Hindi ~2.25 words/s, with room for silent shots)
SYSTEM = ("You are the creative director and head screenwriter of KahaaniVerse, an Indian studio making original "
          "cinematic 3D-animated films in Hindi/Hinglish. You think like a great film director: every choice serves "
          "the viewer's experience. Never copy or closely adapt other creators' stories, titles, characters or real "
          "people. Output exactly one JSON object matching the schema, no markdown.")
FEASIBLE = ("PRODUCTION LIMITS (design stories that look great within them): 2–4 locations, at most 5 speaking "
            "characters, small casts per scene, no crowds or battles; drama carried by faces, looks, silence, light "
            "and a few strong visual moments (an object, a door, a reflection, rain, a lamp).")


# ------------------------------------------------------------------ research

def research(ch: dict[str, Any], *, youtube: bool = True) -> dict[str, Any]:
    from ..research import sources
    from . import news
    out: dict[str, Any] = {"sources": {}, "videos": [], "trends": [], "own": {}}
    if youtube:
        for g in kahaani.mix(ch):
            try:
                vids = sources.youtube_search(QUERIES[g], "Hindi", "IN", days=90, limit=8)
                out["videos"] += [{"genre": g, "title": v["title"], "views": v["metrics"]["views"],
                                   "minutes": round(v["metrics"]["duration_s"] / 60, 1)} for v in vids]
                out["sources"][f"youtube:{g}"] = f"{len(vids)} videos"
            except Exception as e:                          # quota, network: the other sources still count
                out["sources"][f"youtube:{g}"] = f"not used — {type(e).__name__}: {str(e)[:120]}"
    try:
        out["trends"] = [t["keyword"] for t in news.trending("IN")[:20]]
        out["sources"]["google_trends_in"] = f"{len(out['trends'])} searches"
    except Exception as e:
        out["sources"]["google_trends_in"] = f"not used — {type(e).__name__}"
    out["own"] = own_results(ch["id"])
    out["sources"]["own_analytics"] = f"{sum(v['videos'] for v in out['own'].values())} measured films"
    out["sources"]["reddit"] = "not used — needs a registered Reddit API app (not set up)"
    out["sources"]["youtube_suggest"] = "not used — no official API for search suggestions"
    return out


def own_results(channel_id: int) -> dict[str, dict[str, Any]]:
    """Views per day of this channel's films by genre (real YouTube numbers; empty until there are some)."""
    rows = db.q("SELECT p.request, p.uploaded_at, a.metrics FROM productions p JOIN analytics a ON a.production_id=p.id "
                "AND a.id=(SELECT MAX(id) FROM analytics WHERE production_id=p.id) WHERE p.channel_id=? AND "
                "p.kind='film' AND p.uploaded_at IS NOT NULL", (channel_id,))
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        g = (db.loads(r["request"], {}) or {}).get("genre")
        m = db.loads(r["metrics"], {}) or {}
        when = db.parse_iso(r["uploaded_at"])
        if not g or not when:
            continue
        age = max(1.0, (db.utcnow() - when).total_seconds() / 86400)
        s = out.setdefault(g, {"videos": 0, "views_per_day": 0.0})
        s["views_per_day"] = round((s["views_per_day"] * s["videos"] + int(m.get("views") or 0) / age) /
                                   (s["videos"] + 1), 2)
        s["videos"] += 1
    return out


# ------------------------------------------------------------------ concepts

class Scores(BaseModel):
    curiosity: int = Field(..., ge=1, le=10)
    emotion: int = Field(..., ge=1, le=10)
    originality: int = Field(..., ge=1, le=10)
    trend: int = Field(..., ge=1, le=10, description="fit with what audiences watch/search now (from the research)")
    visual: int = Field(..., ge=1, le=10)
    feasibility: int = Field(..., ge=1, le=10, description="can be animated well within the production limits")
    development: int = Field(..., ge=1, le=10, description="room for sequels / the connected universe")
    competition_gap: int = Field(..., ge=1, le=10, description="10 = few similar videos exist")
    evergreen: int = Field(..., ge=1, le=10)
    relevance: int = Field(..., ge=1, le=10, description="fit with KahaaniVerse and its characters")


class Concept(BaseModel):
    title: str = Field(..., min_length=3, max_length=90, description="working title, Hinglish (Roman) or Hindi")
    genre: GenreKey
    logline: str = Field(..., min_length=20, max_length=400)
    why_interesting: str = Field(..., max_length=300)
    why_care: str = Field(..., max_length=300)
    protagonist: str = Field(..., max_length=200)
    want: str = Field(..., max_length=200)
    conflict: str = Field(..., max_length=300)
    curiosity: str = Field(..., max_length=300, description="the question that keeps viewers watching")
    emotional_core: str = Field(..., max_length=300)
    twist: str = Field(..., max_length=300)
    payoff: str = Field(..., max_length=300)
    visual_memorable: str = Field(..., max_length=300)
    hook_5s: str = Field(..., max_length=240, description="what is seen and heard in the first 5 seconds")
    structure: Literal["linear", "flashback", "two_timelines", "countdown", "unreliable_narrator", "bookend"]
    recurring: list[UniverseKey] = Field(default_factory=list, max_length=4)
    one_off_characters: list[str] = Field(default_factory=list, max_length=4)
    scores: Scores


class Concepts(BaseModel):
    concepts: list[Concept] = Field(..., min_length=10, max_length=14)


def score(c: dict[str, Any], own: dict[str, dict[str, Any]] | None = None) -> float:
    """0–100 from the ten dimensions, plus a small bonus for a genre this channel's viewers already reward."""
    s = c["scores"]
    base = sum(s[k] * w for k, w in SCORE_WEIGHTS.items()) / (10 * sum(SCORE_WEIGHTS.values())) * 100
    vpd = {g: v["views_per_day"] for g, v in (own or {}).items() if v["videos"] >= 3}
    if vpd and c["genre"] in vpd and max(vpd.values()) > 0:
        base += 8 * vpd[c["genre"]] / max(vpd.values())
    return round(min(100.0, base), 1)


def _taken(channel_id: int) -> list[tuple[int, str]]:
    return [(r["id"], r["title"]) for r in db.q(
        "SELECT id, title FROM topics WHERE channel_id=? AND why_now='kahaani concept' UNION ALL "
        "SELECT id, title FROM productions WHERE channel_id=? AND kind='film'", (channel_id, channel_id))]


def concepts(channel_id: int, research_data: dict[str, Any], count: int = 10) -> dict[str, Any]:
    ch = channels.get(channel_id)
    taken = _taken(channel_id)
    hot = [v["title"] for v in sorted(research_data.get("videos", []), key=lambda v: -v["views"])[:25]]
    prompt = (f"{kahaani.universe_text()}\n\nGENRES AND TODAY'S EDITORIAL MIX (%): {kahaani.mix(ch)}\n"
              f"WHAT AUDIENCES WATCH NOW (titles only — for themes and curiosity patterns; never copy): "
              f"{'; '.join(hot) or 'no data today'}\n"
              f"WHAT INDIA IS SEARCHING TODAY (Google Trends): {', '.join(research_data.get('trends') or []) or '-'}\n"
              f"OUR OWN RESULTS BY GENRE (views/day): {research_data.get('own') or 'no films measured yet'}\n"
              f"ALREADY MADE OR PLANNED (never repeat or re-skin): {'; '.join(t for _, t in taken[-60:]) or 'none'}\n"
              f"{FEASIBLE}\n\n"
              f"Create {count} ORIGINAL story concepts for 8–12 minute films, spread across the genres roughly in "
              "the mix. Vary the narrative structure; avoid clichés (no 'it was all a dream', no swapped names on a "
              "known plot). Use the recurring characters where they fit (they are a connected universe), plus "
              "one-off characters when the story needs them. Score each concept honestly on every dimension (most "
              "concepts are 5–8, not 10).")

    def check(out: Concepts) -> None:
        if len(out.concepts) < count:
            raise ValueError(f"give at least {count} concepts")
        if len({c.genre for c in out.concepts}) < 3:
            raise ValueError("spread the concepts over at least 3 genres")

    out, meta = ai.generate("ideation", SYSTEM, prompt, Concepts, max_tokens=12000, validate=check)
    created, dropped = [], []
    for c in out.concepts:
        d = c.model_dump()
        _, own_sim = dedup.best_match(c.title + " " + c.logline[:80], taken)
        hot_sim = max((dedup.similarity(c.title, t) for t in hot), default=0.0)
        if own_sim >= dedup.DUPLICATE or hot_sim >= 0.5:
            dropped.append({"title": c.title, "why": f"too close (own {own_sim:.2f}, trending {hot_sim:.2f})"})
            continue
        d["score"] = score(d, research_data.get("own"))
        tid = db.insert("topics", {
            "channel_id": channel_id, "title": c.title[:200], "angle": c.logline, "format": "long",
            "hook_idea": c.hook_5s, "why_now": "kahaani concept", "status": "idea", "score": d["score"],
            "score_breakdown": db.dumps({k: {"points": v, "max": 10, "kind": "ai judgement"}
                                         for k, v in d["scores"].items()}),
            "claims": "[]", "evidence_ids": "[]", "gap": db.dumps({"kahaani": d}), "search_query": c.title,
            "norm_title": dedup.normalize(c.title), "idea_kind": "ai_original", "style": "storytelling",
            "language": ch["language"], "summary": c.logline, "workspace_id": ch["workspace_id"],
            "created_at": db.iso(), "updated_at": db.iso()})
        taken.append((tid, c.title))
        created.append(tid)
    logs.event("info", "kahaani", f"Story research: {len(created)} concepts added to the backlog, {len(dropped)} "
               f"dropped as too close ({meta['provider']}/{meta['model']})")
    return {"created": created, "dropped": dropped, "model": meta}


def backlog(channel_id: int) -> list[dict[str, Any]]:
    rows = db.q("SELECT id, title, score, gap, status FROM topics WHERE channel_id=? AND why_now='kahaani concept' "
                "AND status IN ('idea','approved') ORDER BY score DESC", (channel_id,))
    return [{"id": r["id"], "title": r["title"], "score": r["score"], "status": r["status"],
             **((db.loads(r["gap"], {}) or {}).get("kahaani") or {})} for r in rows]


def pick(channel_id: int, genre: str | None = None) -> dict[str, Any] | None:
    """Today's story: an owner-approved concept first, then the best of today's genre, then the best overall."""
    items = backlog(channel_id)
    if not items:
        return None
    approved = [c for c in items if c["status"] == "approved"]
    same = [c for c in items if c.get("genre") == genre]
    return (approved or same or items)[0]


# ------------------------------------------------------------------ screenplay

class OneOff(BaseModel):
    key: str = Field(..., pattern=r"^N[1-9]$")
    name: str = Field(..., max_length=40)
    gender: Literal["male", "female"]
    age: str = Field(..., max_length=20)
    look: str = Field(..., min_length=20, max_length=400, description="face, hair, build, clothing, accessories")
    personality: str = Field(..., max_length=200)
    voice_note: str = Field("", max_length=120)


class Location(BaseModel):
    key: str = Field(..., pattern=r"^L[1-9]$")
    description: str = Field(..., min_length=20, max_length=400, description="a specific place, its details, its feel")


class SceneBeat(BaseModel):
    location: str = Field(..., description="a location key L1..")
    time: Literal["dawn", "day", "dusk", "night"]
    mood: str = Field(..., max_length=60)
    purpose: Literal["hook", "setup", "conflict", "escalation", "twist", "climax", "resolution", "flashback"]
    characters: list[str] = Field(..., min_length=1, max_length=5)
    summary: str = Field(..., min_length=20, max_length=500)


class FilmOutline(BaseModel):
    title: str = Field(..., min_length=3, max_length=90)
    hook_line: str = Field(..., max_length=200, description="the first spoken line (within 5 seconds), Devanagari")
    one_offs: list[OneOff] = Field(default_factory=list, max_length=4)
    locations: list[Location] = Field(..., min_length=1, max_length=5)
    scenes: list[SceneBeat] = Field(..., min_length=6, max_length=16)
    thumbnail_text: str = Field(..., max_length=40)
    description: str = Field(..., min_length=40, max_length=1500)
    tags: list[str] = Field(..., min_length=5, max_length=15)
    short_scene: int = Field(..., ge=1, description="the scene whose moment makes the best stand-alone Short")


class FilmLine(BaseModel):
    speaker: str
    text: str = Field(..., min_length=1, max_length=260, description="Devanagari")
    manner: str = Field("", max_length=60, description="how it is said: whisper, angry, trembling…")
    action: str = Field("", max_length=160)


class FilmScene(BaseModel):
    action: str = Field(..., min_length=20, max_length=700, description="what we SEE before and during the scene")
    lines: list[FilmLine] = Field(..., min_length=1, max_length=22)


class FilmScenes(BaseModel):
    scenes: list[FilmScene]


def _devanagari_ratio(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    return sum("ऀ" <= ch <= "ॿ" for ch in letters) / max(1, len(letters))


def screenplay(channel_id: int, concept: dict[str, Any], minutes: float = 10.0) -> dict[str, Any]:
    words = int(minutes * WORDS_PER_MINUTE)
    n = max(6, min(14, round(minutes * 1.2)))
    lib = {c["key"]: c for c in kahaani.UNIVERSE}
    head = (f"{kahaani.universe_text()}\n\nDIRECTOR'S BRIEF (follow it): {concept['title']} — {concept['logline']}\n"
            f"Why it's interesting: {concept['why_interesting']}. Why viewers care: {concept['why_care']}.\n"
            f"Protagonist: {concept['protagonist']}; wants: {concept['want']}; conflict: {concept['conflict']}.\n"
            f"Curiosity: {concept['curiosity']}. Emotional core: {concept['emotional_core']}.\n"
            f"Twist: {concept['twist']}. Payoff: {concept['payoff']}. Visual signature: {concept['visual_memorable']}.\n"
            f"First 5 seconds: {concept['hook_5s']}. Structure: {concept['structure']}. "
            f"Genre: {kahaani.GENRES[concept['genre']]['label']} — {kahaani.GENRES[concept['genre']]['note']}.\n"
            f"{FEASIBLE}\nLANGUAGE: natural spoken Hindi with everyday English words (Hinglish) — ALL dialogue in "
            "DEVANAGARI script (English words transliterated). Numbers as digits. People talk like real Indians: "
            "short lines, interruptions, silences, subtext; no speeches, no narration explaining feelings.\n")
    prompt = (head + f"\nPlan the film: exactly {n} scenes in order, with the hook first and a satisfying resolution "
              "last. Recurring characters by key (AARAV, SIYA, VEER, DADAJI); one-off characters N1.. with full looks. "
              "2–4 locations (L1..), each a specific, visual place. thumbnail_text: 2–4 strong words.")
    keys_ok = set(lib)

    def check_outline(o: FilmOutline) -> None:
        problems = []
        locs = {loc.key for loc in o.locations}
        people = keys_ok | {x.key for x in o.one_offs}
        by_name = {c["name"].lower(): k for k, c in lib.items()} | {x.name.lower(): x.key for x in o.one_offs} | \
            {k.lower(): k for k in people}
        for sc in o.scenes:                             # names written where keys belong
            sc.characters = [by_name.get(k.strip().lower(), k) for k in sc.characters]
        if abs(len(o.scenes) - n) > 1:
            problems.append(f"write exactly {n} scenes")
        for i, s in enumerate(o.scenes, 1):
            if s.location not in locs:
                problems.append(f"scene {i}: unknown location {s.location}")
            bad = [k for k in s.characters if k not in people]
            if bad:
                problems.append(f"scene {i}: unknown characters {bad}")
        if o.scenes and o.scenes[0].purpose != "hook":
            problems.append("scene 1 must be the hook")
        if _devanagari_ratio(o.hook_line) < 0.6:
            problems.append("hook_line must be in Devanagari")
        if problems:
            raise ValueError("; ".join(problems[:8]))

    outline, meta = ai.generate("script", SYSTEM, prompt, FilmOutline, max_tokens=8000, validate=check_outline)
    people = {**{k: v["name"] for k, v in lib.items()}, **{x.key: x.name for x in outline.one_offs}}
    per_scene = max(50, words // len(outline.scenes))
    scenes: list[dict[str, Any]] = []
    for start in range(0, len(outline.scenes), 2):
        chunk = outline.scenes[start:start + 2]
        so_far = " ".join(f"[{i + 1} {b.purpose}] {b.summary}" for i, b in enumerate(outline.scenes))
        prev = scenes[-1]["lines"][-3:] if scenes else []
        locs = {loc.key: loc.description for loc in outline.locations}
        p2 = (head + f"\nFILM: {outline.title}\nFULL OUTLINE: {so_far}\nSPEAKER KEYS: "
              + ", ".join(f"{k}={v}" for k, v in people.items())
              + (f"\nLAST LINES SO FAR (continue AFTER them — never repeat them): "
                 f"{' / '.join(x['text'] for x in prev)}" if prev else "")
              + f"\n\nWrite scenes {start + 1}-{start + len(chunk)} only:\n"
              + "\n".join(f"Scene {start + i + 1} ({b.purpose}, {b.time}, {b.mood}) at {locs.get(b.location, '')}: "
                          f"{', '.join(b.characters)} — {b.summary}" for i, b in enumerate(chunk))
              + f"\nAbout {per_scene} spoken words per scene. 'action' = what the camera sees (faces, hands, light, "
              "objects, movement), in English. Every line: speaker key, Devanagari text, manner, a small action. "
              + (f"Scene 1 opens with exactly this line: {outline.hook_line}" if start == 0 else ""))

        def check_scenes(out: FilmScenes, chunk=chunk) -> None:
            problems = []
            by_name = {v.lower(): k for k, v in people.items()} | {k.lower(): k for k in people}
            for sc in out.scenes:                       # writers often use the name ("Aarav") for the key ("AARAV")
                for ln in sc.lines:
                    ln.speaker = by_name.get(ln.speaker.strip().lower(), ln.speaker)
            if len(out.scenes) != len(chunk):
                problems.append(f"return exactly {len(chunk)} scenes")
            for si, sc in enumerate(out.scenes, 1):
                bad = {ln.speaker for ln in sc.lines} - set(people)
                if bad:
                    problems.append(f"scene {si}: unknown speakers {sorted(bad)}")
                if _devanagari_ratio(" ".join(ln.text for ln in sc.lines)) < 0.6:
                    problems.append(f"scene {si}: dialogue must be in Devanagari")
            said = {ln["text"].strip() for x in scenes for ln in x["lines"]}
            for sc in out.scenes:                       # writers echo the "last lines" they were given: drop echoes
                seen: set[str] = set()
                kept = []
                for ln in sc.lines:
                    t = ln.text.strip()
                    if len(t.split()) > 3 and (t in said or t in seen):
                        continue
                    seen.add(t)
                    kept.append(ln)
                sc.lines = kept or sc.lines
                said |= seen
            lines = [ln.text.strip() for sc in out.scenes for ln in sc.lines]
            spoken = sum(len(t.split()) for t in lines)
            if spoken < 0.5 * per_scene * len(chunk):
                problems.append(f"too short: {spoken} words for {len(chunk)} scenes — write about {per_scene} words "
                                "per scene: more exchanges, reactions, interruptions and specific details")
            if problems:
                raise ValueError("; ".join(problems[:8]))

        out, _ = ai.generate("script", SYSTEM, p2, FilmScenes, max_tokens=8000, validate=check_scenes)
        for b, sc in zip(chunk, out.scenes):
            scenes.append({**b.model_dump(), "action": sc.action, "lines": [ln.model_dump() for ln in sc.lines]})
    return {"concept": concept, "outline": outline.model_dump(), "scenes": scenes, "people": people,
            "words": sum(len(ln["text"].split()) for s in scenes for ln in s["lines"]), "generated_by": meta}


# ------------------------------------------------------------------ storyboard (cinematography)

ShotType = Literal["establishing", "wide", "medium", "close_up", "extreme_close_up", "over_shoulder", "tracking",
                   "insert", "reaction", "pov"]


def _trim(n: int):
    """Over-long text from a writer is cut to size rather than rejected (only the meaning matters here)."""
    return BeforeValidator(lambda v: str(v or "")[:n])


class Shot(BaseModel):
    shot_type: ShotType
    characters: list[str] = Field(default_factory=list)
    line_indices: list[int] = Field(default_factory=list, description="dialogue lines (0-based) heard in this shot")
    action: Annotated[str, _trim(300)] = Field("", description="what the character does")
    expression: Annotated[str, _trim(100)] = ""
    angle: Annotated[str, _trim(60)] = Field("eye level", description="eye level, low angle, high angle, dutch…")
    movement: Annotated[str, _trim(100)] = Field("static", description="static, slow dolly in, pan left, tracking…")
    composition: Annotated[str, _trim(160)] = ""
    depth_of_field: Literal["shallow", "medium", "deep"] = "shallow"
    lighting: Annotated[str, _trim(160)] = ""
    seconds: float = Field(..., ge=1.0, le=15.0)
    image_prompt: Annotated[str, _trim(900)] = Field(..., min_length=10, description="the keyframe, in English")
    video_prompt: Annotated[str, _trim(600)] = Field("", description="what moves, in English")
    negative_prompt: Annotated[str, _trim(240)] = ""
    continuity: Annotated[str, _trim(240)] = ""
    transition: Literal["cut", "match_cut", "dissolve", "fade_to_black", "whip"] = "cut"
    # animation instruction (Animation Director): what the body, head and hands do from first to last frame
    start_pose: Annotated[str, _trim(160)] = ""
    end_pose: Annotated[str, _trim(160)] = ""
    body_action: Annotated[str, _trim(200)] = ""
    head: Annotated[str, _trim(120)] = ""
    hands: Annotated[str, _trim(120)] = ""
    environment_motion: Annotated[str, _trim(160)] = ""


class SceneBoard(BaseModel):
    shots: list[Shot] = Field(..., min_length=2, max_length=30)


NEGATIVE = ("extra fingers, deformed hands, different face, changed clothes, text, letters, watermark, logo, "
            "photorealistic live action, flat 2D cartoon")


def storyboard(sp: dict[str, Any], cache: Path | None = None) -> list[dict[str, Any]]:
    """Shots for every scene; every dialogue line covered once, in order, with time to be heard.
    cache: a folder where each finished scene's board is kept, so a later failure never redoes it."""
    from ..film_studio import shotlist
    outline = sp["outline"]
    locs = {loc["key"]: loc["description"] for loc in outline["locations"]}
    looks = {c["key"]: kahaani.look_prompt(c) for c in kahaani.UNIVERSE} | \
        {x["key"]: f"{x['name']}: {x['look']}" for x in outline["one_offs"]}
    shots: list[dict[str, Any]] = []
    by_name = {v.lower(): k for k, v in sp["people"].items()} | {k.lower(): k for k in sp["people"]}
    for si, s in enumerate(sp["scenes"]):
        lines = s["lines"]
        prompt = (f"You are the cinematographer. Film: {outline['title']}. Scene {si + 1} of {len(sp['scenes'])} "
                  f"({s['purpose']}, {s['time']}, mood {s['mood']}). Place: {locs.get(s['location'], '')}.\n"
                  f"What we see: {s['action']}\nCHARACTERS: " + "; ".join(f"{k} = {looks.get(k, k)}"
                                                                      for k in s["characters"])
                  + "\nDIALOGUE (index: speaker — text):\n"
                  + "\n".join(f"{i}: {ln['speaker']} — {ln['text']} ({ln.get('manner') or ''}; {ln.get('action') or ''})"
                              for i, ln in enumerate(lines))
                  + "\n\nDesign the shots like a great film's director of photography: open with an establishing or "
                    "wide shot when the place is new, then medium shots, close-ups on emotion, extreme close-ups on "
                    "key objects or eyes, over-the-shoulder for exchanges, reaction shots on reveals, movement only "
                    "with a reason. Every dialogue index appears in exactly one shot, in order. A DIALOGUE SHOT SHOWS "
                    "ONLY ITS SPEAKER (close-up or medium single, face to camera, three-quarter view) and holds that "
                    "speaker's lines only — listeners get their own silent reaction shots. For every shot also give the "
                    "animation: start_pose, end_pose, body_action, head, hands and environment_motion (rain, fan, "
                    "steam, lamp flicker…) — real, specific movement, never 'stands still'. Seconds must leave time to say its lines (about 2.2 words per second). "
                    "image_prompt: one frame of a stylised 3D animated feature film — who is where, doing what, "
                    "expression, camera, light; describe recurring characters by their exact look. video_prompt: "
                    "the one movement in the shot (beginning → end), camera move included.")

        def check(b: SceneBoard, n=len(lines), lines=lines) -> None:
            seen = [i for sh in b.shots for i in sh.line_indices]
            if seen != list(range(n)):
                raise ValueError(f"dialogue indices must be 0..{n - 1}, each once, in order (got {seen})")
            for k, sh in enumerate(b.shots, 1):
                need = sum(shotlist.speech_seconds(lines[i]["text"]) for i in sh.line_indices)
                sh.seconds = round(max(sh.seconds, need + 0.4), 2)    # a shot always lasts as long as its lines

        saved = cache / f"board_{si + 1:02d}.json" if cache else None
        if saved and saved.exists():
            board = SceneBoard.model_validate_json(saved.read_text())
        else:
            board, _ = ai.generate("script", SYSTEM, prompt, SceneBoard, max_tokens=9000, validate=check)
            if saved:
                saved.write_text(board.model_dump_json())
        k = 0
        for board_shot in board.shots:
            for idx in (_speaker_runs(board_shot.line_indices, lines) or [[]]):
                k += 1
                dlg = [{"speaker": lines[i]["speaker"], "name": sp["people"].get(lines[i]["speaker"], lines[i]["speaker"]),
                        "text": lines[i]["text"], "line_index": i, "manner": lines[i].get("manner", "")}
                       for i in idx]
                sh = _frame_speaker(board_shot, dlg, lines, looks, locs.get(s["location"], "")) if dlg else board_shot
                shots.append({
                    "scene_no": si + 1, "shot_no": k, "shot_type": sh.shot_type if sh.shot_type in shotlist_types()
                    else "medium", "target_seconds": round(sh.seconds, 2),
                    "camera": {"angle": sh.angle, "movement": sh.movement, "composition": sh.composition,
                               "depth_of_field": sh.depth_of_field, "transition": sh.transition,
                               "expression": sh.expression, "board_type": sh.shot_type, "environment": s["location"],
                               "image_prompt": sh.image_prompt, "video_prompt": sh.video_prompt,
                               "negative_prompt": ", ".join(x for x in (sh.negative_prompt, NEGATIVE) if x),
                               "animation": {"start_pose": sh.start_pose, "end_pose": sh.end_pose,
                                             "body_action": sh.body_action or sh.action, "head": sh.head,
                                             "hands": sh.hands, "environment_motion": sh.environment_motion,
                                             "expression": sh.expression, "duration": round(sh.seconds, 2)}},
                    "characters": list(dict.fromkeys(key for key in (by_name.get(c.strip().lower())
                                                                     for c in sh.characters) if key)),
                    "action": sh.action,
                    "dialogue": dlg, "props": [], "lighting": sh.lighting,
                    "continuity": {"notes": sh.continuity, "voice": dlg[0]["speaker"] if dlg else None}})
    for i, sh in enumerate(shots):
        prev, nxt = (shots[i - 1] if i else None), (shots[i + 1] if i + 1 < len(shots) else None)
        sh["continuity"].update({"prev": f"{prev['scene_no']}.{prev['shot_no']}" if prev else None,
                                 "next": f"{nxt['scene_no']}.{nxt['shot_no']}" if nxt else None})
    return shots


def _speaker_runs(indices: list[int], lines: list[dict[str, Any]]) -> list[list[int]]:
    """A shot's lines split by speaker: one speaker per dialogue shot."""
    runs: list[list[int]] = []
    for i in indices:
        if runs and lines[runs[-1][-1]]["speaker"] == lines[i]["speaker"]:
            runs[-1].append(i)
        else:
            runs.append([i])
    return runs


def _frame_speaker(sh: "Shot", dlg: list[dict[str, Any]], lines: list[dict[str, Any]], looks: dict[str, str],
                   place: str) -> "Shot":
    """A dialogue shot framed on its speaker alone, face to camera: the voice heard is the face seen, and the
    lip-sync model animates the right person (two-shots and over-the-shoulders are kept for silent shots)."""
    from ..film_studio import shotlist
    who = dlg[0]["speaker"]
    kind = sh.shot_type if sh.shot_type in ("close_up", "medium", "extreme_close_up") and sh.characters == [who] \
        else "close_up" if sh.shot_type in ("close_up", "extreme_close_up", "reaction", "over_shoulder") else "medium"
    need = sum(shotlist.speech_seconds(lines[i]["text"]) for i in (d["line_index"] for d in dlg)) + 0.4
    framing = {"close_up": "Close-up", "medium": "Medium single shot", "extreme_close_up": "Close-up"}[kind]
    prompt = (f"{framing} of {looks.get(who, who)}, alone in frame, speaking, face toward the camera in a "
              f"three-quarter view, mouth clearly visible, {sh.expression or 'natural expression'}. {sh.lighting}. "
              f"Background, softly out of focus: {place[:200]}. Frame from a stylised 3D animated feature film.")
    return sh.model_copy(update={"shot_type": kind, "characters": [who], "image_prompt": prompt,
                                 "seconds": round(max(sh.seconds if len(dlg) == len(sh.line_indices) else 0, need), 2)})


def shotlist_types() -> set[str]:
    """Shot types the generator and editor know; others (tracking, pov) are kept in camera.board_type."""
    from ..film_studio.generate import TYPE_LABEL
    return set(TYPE_LABEL)


# ------------------------------------------------------------------ film format

def to_film(channel_id: int, sp: dict[str, Any], shots: list[dict[str, Any]]) -> dict[str, Any]:
    """The film studio's film dict: library characters keep their pictures and fixed voices."""
    outline, concept = sp["outline"], sp["concept"]
    lib = {c["key"]: c for c in kahaani.library(channel_id)}
    bibles = {c["key"]: c for c in kahaani.UNIVERSE}
    used = list(dict.fromkeys(k for s in sp["scenes"] for k in s["characters"] + [ln["speaker"] for ln in s["lines"]]))
    cast = []
    for k in used:
        if k in bibles:
            b = bibles[k]
            entry = {"key": k, "name": b["name"], "role": b["role"], "gender": b["gender"], "look": kahaani.look_prompt(b),
                     "personality": b["personality"], "performance": kahaani.performance(k),
                     "voice": {**{k2: b["voice"][k2] for k2 in ("engine", "voice_id", "pitch", "speed")},
                               "logical_id": kahaani.PERFORMANCE[k]["logical_id"]},
                     "bible": {"age": b["age"], "appearance": b["appearance"], "clothing": b["clothing"]}}
            front = (lib.get(k) or {}).get("refs", {}).get("front")
            if front:
                entry["library_ref"] = front
            cast.append(entry)
            continue
        x = next((o for o in outline["one_offs"] if o["key"] == k), None)
        if x:
            cast.append({"key": k, "name": x["name"], "role": "", "gender": x["gender"], "look": x["look"],
                         "personality": x["personality"], "voice_note": x.get("voice_note", ""),
                         "bible": {"age": x["age"], "appearance": x["look"]}})
    scenes = [{"number": i + 1, "heading": f"{s['purpose'].title()} — {s['time']}", "action": s["action"],
               "location": s["location"], "purpose": s["purpose"], "mood": s.get("mood", ""), "time": s["time"],
               "lines": [{"speaker": ln["speaker"], "text": ln["text"], "tts": ln["text"], "manner": ln.get("manner", ""),
                          "action": ln.get("action", "")} for ln in s["lines"]]} for i, s in enumerate(sp["scenes"])]
    main = max(outline["locations"], key=lambda loc: sum(s["location"] == loc["key"] for s in sp["scenes"]))
    return {"title": outline["title"], "cast": cast, "scenes": scenes, "location": main["description"],
            "environments": {loc["key"]: {"description": loc["description"]} for loc in outline["locations"]},
            "lighting": "", "closing_line": kahaani.TAGLINE, "description": outline["description"],
            "tags": outline["tags"], "thumbnail_text": outline["thumbnail_text"], "short_scene": outline["short_scene"],
            "genre": concept["genre"], "concept": concept, "storyboard": shots,
            "target_seconds": round(sum(sh["target_seconds"] for sh in shots), 1)}


def check_story(sp: dict[str, Any], minutes: float) -> list[str]:
    """Story QC before anything is drawn: length, hook, Devanagari, repeats, every scene alive."""
    problems = []
    if sp["words"] < minutes * WORDS_PER_MINUTE * 0.45:   # a little short is fine (silent shots carry time)
        problems.append(f"too little dialogue for {minutes:.0f} min ({sp['words']} words)")
    if not sp["scenes"] or sp["scenes"][0]["purpose"] != "hook":
        problems.append("the film doesn't open on its hook")
    texts = [ln["text"].strip() for s in sp["scenes"] for ln in s["lines"]]
    dupes = {t for t in texts if texts.count(t) > 1 and len(t.split()) > 3}
    if len(dupes) > 2:                                  # one or two can be a deliberate callback; more is filler
        problems.append(f"repeated lines: {list(dupes)[:3]}")
    roman = [t for t in texts if re.search(r"[A-Za-zऀ-ॿ]", t) and _devanagari_ratio(t) < 0.5]
    if len(roman) > max(2, len(texts) // 10):           # a name or an exclamation in Roman letters is fine
        problems.append(f"{len(roman)} lines aren't in Devanagari")
    return problems


# ------------------------------------------------------------------ today's film

def start_film(channel_id: int, *, minutes: float = 10.0, topic_id: int | None = None, youtube: bool = True,
               actor: str = "scheduler") -> int:
    """Research (when the backlog is thin) → pick today's story → queue its film. Returns the production id."""
    from . import film
    ch = channels.get(channel_id)
    if topic_id is None and len(backlog(channel_id)) < 3:
        concepts(channel_id, research(ch, youtube=youtube))
    genre = kahaani.pick_genre(ch)
    c = next((x for x in backlog(channel_id) if x["id"] == topic_id), None) if topic_id else pick(channel_id, genre)
    if not c:
        raise ValueError("no story concept in the backlog")
    plan = {"kahaani": True, "concept": {k: v for k, v in c.items() if k not in ("id", "status")},
            "topic_id": c["id"], "genre": c.get("genre"), "minutes": minutes, "look": kahaani.LOOK,
            "language": "Hinglish", "request": c["title"], "format": "long",
            # until a film has been published, each one waits for the owner's look (self-review respects this)
            "hold_for_review": not db.q1("SELECT 1 FROM productions WHERE channel_id=? AND kind='film' AND "
                                         "youtube_video_id IS NOT NULL", (channel_id,))}
    pid = film.create(channel_id, c["title"], plan=plan, actor=actor)
    db.update("topics", c["id"], {"status": "in_production", "updated_at": db.iso()})
    logs.event("info", "kahaani", f"Today's film: '{c['title']}' ({c.get('genre')}, score {c['score']})",
               production_id=pid)
    return pid
