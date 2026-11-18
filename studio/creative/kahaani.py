"""KahaaniVerse — original Indian cinematic 3D-animated stories (Hindi + Hinglish), on the studio's channel #1.
"Har Kahaani Mein Ek Raaz. Har Raaz Ke Peeche Ek Sach."

This module is the channel's creative identity and its permanent character library:
  * GENRES and the editorial mix (configurable per channel; later tuned from the channel's own analytics);
  * UNIVERSE — the recurring main characters, each with a full bible (look, clothing, accessories, personality,
    gestures, voice) that every film reuses: a recurring character is never re-invented for a new scene;
  * SHEETS — the reference library made once per character: front / side / back turnarounds, expression, emotion,
    gesture and pose sheets and a clothing reference, all drawn from the approved front view so the face and
    proportions stay the same; plus the voice profile and identity metadata (data, not pictures);
  * convert_channel() — turns a channel into KahaaniVerse (the previous settings go on the audit trail).
Pictures come from the free image providers in the order the film studio uses; a sheet that would have to be made
WITHOUT the front view as a reference is left for later instead of drawn inconsistently.
"""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from .. import channels, config, db, logs

NAME = "KahaaniVerse"
TAGLINE = "Har Kahaani Mein Ek Raaz. Har Raaz Ke Peeche Ek Sach."
LOOK = "animated3d"

GENRES: dict[str, dict[str, Any]] = {
    "suspense_mystery": {"label": "Suspense & mystery", "share": 30,
                         "note": "a puzzle the viewer tries to solve with the hero; fair clues; a reveal that "
                                 "re-frames earlier scenes"},
    "emotional_drama": {"label": "Emotional family drama", "share": 25,
                        "note": "an Indian family under one pressure; small, true moments; a payoff that earns tears"},
    "psychological_thriller": {"label": "Psychological thriller", "share": 20,
                               "note": "an unreliable perception, rising doubt, a twist rooted in character"},
    "village_mystery": {"label": "Village mystery", "share": 15,
                        "note": "a village secret, a local legend tested against facts, a human truth underneath"},
    "scifi_supernatural": {"label": "Sci-fi & supernatural", "share": 10,
                           "note": "one bold idea (time, memory, a strange device or presence) explored through people"},
}
LATER_GENRES = {"horror": "Horror & supernatural fiction", "crime_detective": "Crime & detective fiction",
                "adventure_fantasy": "Adventure & fantasy", "twist": "Unexpected-twist stories"}

STYLE_3D = ("stylised 3D animated feature-film character, high-end CG render, expressive large eyes, detailed "
            "hair strands, subtle skin texture with pores and soft subsurface scattering, believable proportions, "
            "cinematic three-point lighting, rich colour grade, original character design")

UNIVERSE: list[dict[str, Any]] = [
    {"key": "AARAV", "name": "Aarav", "gender": "male", "role": "protagonist",
     "age": "24",
     "appearance": "lean build, 5 ft 9; warm wheat-brown skin; sharp curious dark-brown eyes; thick slightly messy "
                   "wavy black hair with a cowlick at the crown; light stubble; a small scar on the left knuckle",
     "clothing": "rust-orange cotton overshirt open over a charcoal T-shirt, dark-blue jeans, worn brown leather "
                 "boots",
     "accessories": "steel kada on the right wrist, a pocket notebook with a pen clipped to it, a phone with a "
                    "cracked corner",
     "personality": "logical and quick-witted; hides fear behind humour; cannot leave a puzzle alone; impulsive — "
                    "acts before he has all the facts; fiercely loyal to Dadaji",
     "backstory": "a software tester from Lucknow who lost his job after reporting a cover-up nobody believed; "
                  "now follows strange stories to prove that small details matter",
     "gestures": "taps the pen against his chin while thinking; pushes his hair back when stressed; half-grin "
                 "when he spots a clue",
     "voice": {"engine": "kokoro", "voice_id": "hm_psi", "pitch": 1.0, "speed": 1.02,
               "note": "young, quick Hinglish; drops to a whisper when he notices something"}},
    {"key": "SIYA", "name": "Siya", "gender": "female", "role": "protagonist",
     "age": "25",
     "appearance": "medium height; medium-brown skin; intelligent dark-brown eyes with a steady gaze; long black "
                   "hair in a loose side braid with a few strands escaping; a small mole above the left eyebrow",
     "clothing": "teal block-print cotton kurta, olive utility jacket with rolled sleeves, dark trousers, tan "
                 "kolhapuri sandals",
     "accessories": "small silver jhumkas, a crossbody leather satchel, her late mother's old film camera on a "
                    "strap",
     "personality": "confident, empathetic, stubborn about the truth; notices people's feelings before their words; "
                    "trusts very few people; brave without being reckless",
     "backstory": "a Bhopal newspaper reporter turned documentary researcher, finishing the last unfinished story "
                  "her mother was working on",
     "gestures": "tucks the loose strand of hair behind her ear; raises one eyebrow when she doubts a claim; "
                 "lifts the camera before she speaks",
     "voice": {"engine": "kokoro", "voice_id": "hf_alpha", "pitch": 1.0, "speed": 1.0,
               "note": "clear, warm, confident; softens in emotional moments"}},
    {"key": "VEER", "name": "Veer", "gender": "male", "role": "mysterious ally / antagonist",
     "age": "about 35",
     "appearance": "tall and lean; angular face; a thin scar through the right eyebrow; rare grey-green eyes; "
                   "shoulder-length black hair tied half up; short neat beard",
     "clothing": "long charcoal-black wool coat, maroon scarf, dark shirt, black trousers, scuffed boots",
     "accessories": "black fingerless gloves, an old brass pocket watch stopped at 3:17 on a chain",
     "personality": "calm, unreadable, always one step ahead; helps and misleads in the same breath; his motives "
                    "change with what he wants to protect",
     "backstory": "appears wherever an old secret is about to surface; nobody knows where he lives or why the "
                  "watch stopped",
     "gestures": "flips the pocket watch open and shut; tilts his head before answering; a slow half-smile",
     "voice": {"engine": "kokoro", "voice_id": "hm_omega", "pitch": 0.9, "speed": 0.94,
               "note": "low, unhurried, every word placed; never shouts"}},
    {"key": "DADAJI", "name": "Dadaji", "gender": "male", "role": "mentor with a secret",
     "age": "78",
     "appearance": "slightly stooped but strong; deep laugh lines; white hair swept back; thick white moustache; "
                   "round wire-rim spectacles; kind, watchful eyes",
     "clothing": "cream cotton kurta-pyjama, brown wool Nehru-cut waistcoat, a grey shawl over one shoulder",
     "accessories": "a wooden walking stick carved with a coiled serpent, an old railway signal lantern",
     "personality": "wise, gentle, funny in a dry way; tells stories that hide more than they reveal; protective "
                    "of Aarav; carries a guilt from long ago",
     "backstory": "a retired railway station master from a hill village; he was on duty the night of a train "
                  "incident in 1971 that he has never spoken about",
     "gestures": "taps the stick twice before beginning a story; strokes his moustache; looks over the top of "
                 "his glasses",
     "voice": {"engine": "kokoro", "voice_id": "hm_omega", "pitch": 0.8, "speed": 0.86,
               "note": "old, slow, warm; long pauses before the important line"}},
]

# Performance profiles: the one voice each character always speaks with (logical id → Kokoro voice + pitch + speed),
# how names are said, the emotions the character plays, and how the body and face move. Never chosen at random.
PERFORMANCE: dict[str, dict[str, Any]] = {
    "AARAV": {"logical_id": "AARAV_VOICE", "voice_presentation": "young adult male, bright, quick",
              "age_presentation": "mid-20s", "accent": "Lucknow Hindi with everyday English",
              "emotional_range": ["curiosity", "excitement", "fear", "confusion", "determination", "humour"],
              "expressions": {"thinking": "eyes narrowed, pen at chin", "clue": "half-grin, eyebrows up",
                              "afraid": "eyes wide, breath held", "lost": "blank stare, slack jaw"},
              "animation": "quick, restless; leans in when curious; shifts weight often",
              "gestures": ["taps pen on chin", "pushes hair back", "points at the clue"]},
    "SIYA": {"logical_id": "SIYA_VOICE", "voice_presentation": "young adult female, clear, warm, confident",
             "age_presentation": "mid-20s", "accent": "Bhopal Hindi with everyday English",
             "emotional_range": ["confidence", "empathy", "doubt", "alarm", "tenderness", "anger"],
             "expressions": {"doubt": "one eyebrow raised", "alarm": "eyes wide, lips parted",
                             "tender": "soft eyes, slight smile"},
             "animation": "steady and purposeful; still when listening, decisive when acting",
             "gestures": ["tucks hair strand behind ear", "lifts the old camera", "takes Aarav's arm"]},
    "VEER": {"logical_id": "VEER_VOICE", "voice_presentation": "adult male, low, slow, controlled",
             "age_presentation": "mid-30s", "accent": "neutral Hindi, precise",
             "emotional_range": ["calm", "menace", "regret", "amusement"],
             "expressions": {"calm": "still face, slow blink", "amused": "slow half-smile",
                             "menace": "head tilted, eyes fixed"},
             "animation": "economical: few movements, all deliberate; never hurries",
             "gestures": ["flips the pocket watch open and shut", "tilts head before answering"]},
    "DADAJI": {"logical_id": "DADAJI_VOICE", "voice_presentation": "elderly male, warm, unhurried",
               "age_presentation": "late 70s", "accent": "hill-village Hindi",
               "emotional_range": ["warmth", "humour", "guilt", "sorrow", "sternness"],
               "expressions": {"story": "looks over his glasses", "guilt": "eyes down, jaw tight"},
               "animation": "slow, slightly stooped; leans on the serpent stick",
               "gestures": ["taps the stick twice", "strokes his moustache"]},
}
PRONUNCIATIONS = {"Aarav": "आरव", "Siya": "सिया", "Veer": "वीर", "Dadaji": "दादाजी", "KahaaniVerse": "कहानीवर्स"}


def performance(key: str) -> dict[str, Any]:
    """The full performance profile of a recurring character (voice, delivery, face, body)."""
    c = next(x for x in UNIVERSE if x["key"] == key)
    return {"character_id": key, **PERFORMANCE[key], **{k: c["voice"][k] for k in ("engine", "voice_id", "pitch",
                                                                                     "speed")},
            "voice_provider": c["voice"]["engine"], "gender_presentation": c["gender"],
            "pronunciations": PRONUNCIATIONS, "gestures_bible": c["gestures"]}


# The reference library made for every main character (pictures) — voice profile and identity are data.
SHEETS: dict[str, dict[str, Any]] = {
    "front": {"size": (768, 1024), "refs": False,
              "prompt": "character turnaround reference, full body, FRONT view, standing straight in a relaxed "
                        "neutral pose, arms slightly away from the body, plain light-grey studio background"},
    "side": {"size": (768, 1024), "prompt": "same character, full body, exact SIDE profile view (facing left), same "
                                            "clothes and proportions, plain light-grey studio background"},
    "back": {"size": (768, 1024), "prompt": "same character, full body, BACK view, same hair, clothes and "
                                            "accessories, plain light-grey studio background"},
    "expressions": {"size": (1344, 768), "prompt": "facial expression sheet of the same character: six head-and-"
                                                   "shoulders views in a grid — neutral, smiling, laughing, "
                                                   "surprised, suspicious, determined; same face in every view"},
    "emotions": {"size": (1344, 768), "prompt": "emotion sheet of the same character: six head-and-shoulders views "
                                                "in a grid — joy, sadness with tears, fear, anger, shock, quiet "
                                                "relief; same face in every view"},
    "gestures": {"size": (1344, 768), "prompt": "gesture sheet of the same character: four waist-up views showing "
                                                "{gestures}; same clothes in every view"},
    "poses": {"size": (1344, 768), "prompt": "pose sheet of the same character: four full-body action poses — "
                                             "walking, running, crouching to look closely, pointing at something "
                                             "off-screen; same clothes in every view"},
    "clothing": {"size": (1344, 768), "prompt": "costume reference sheet for the same character: the full outfit "
                                                "and accessories laid out and labelled by shape only (no text): "
                                                "{clothing}; {accessories}"},
}
PICTURES = list(SHEETS)


def mix(ch: dict[str, Any]) -> dict[str, int]:
    """The channel's genre shares (percent); the defaults until the owner or the analytics change them."""
    saved = ((ch.get("branding") or {}).get("kahaani") or {}).get("genre_mix") or {}
    out = {g: int(saved.get(g, v["share"])) for g, v in GENRES.items()}
    return out if sum(out.values()) > 0 else {g: v["share"] for g, v in GENRES.items()}


def pick_genre(ch: dict[str, Any], rng: random.Random | None = None) -> str:
    """Today's genre: weighted by the mix, nudged away from the genre of the last two videos for variety."""
    rng = rng or random.Random()
    weights = dict(mix(ch))
    recent = db.q("SELECT json_extract(request, '$.genre') AS g FROM productions WHERE channel_id=? AND kind='film' "
                  "ORDER BY id DESC LIMIT 2", (ch["id"],))
    for r in recent:
        if r["g"] in weights:
            weights[r["g"]] = max(1, weights[r["g"]] // 2)
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def bible_text(c: dict[str, Any]) -> str:
    return (f"{c['name']} ({c['role']}, {c['gender']}, {c['age']}): {c['appearance']}. Wears {c['clothing']}; "
            f"{c['accessories']}. Personality: {c['personality']}. Backstory: {c['backstory']}. Gestures: "
            f"{c['gestures']}. Voice: {c['voice']['note']}.")


def universe_text() -> str:
    """The recurring cast for the screenwriter (they may appear; one-off characters may be added per story)."""
    return "KAHAANIVERSE RECURRING CHARACTERS (keep their looks, voices and personalities exactly):\n" + \
        "\n".join(f"- {bible_text(c)}" for c in UNIVERSE)


def look_prompt(c: dict[str, Any]) -> str:
    return (f"{c['name']}, an original Indian {('man' if c['gender'] == 'male' else 'woman')}, age {c['age']}: "
            f"{c['appearance']}; wearing {c['clothing']}; {c['accessories']}")


def sheet_prompt(c: dict[str, Any], sheet: str) -> str:
    spec = SHEETS[sheet]
    return f"{spec['prompt'].format(**c)}. {look_prompt(c)}. {STYLE_3D}"


# ------------------------------------------------------------------ the library (characters table, per channel)

def library_dir(channel_id: int) -> Path:
    d = config.DATA_DIR / "characters" / f"channel_{channel_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_universe(channel_id: int) -> list[dict[str, Any]]:
    """Every main character exists once in the channel's character bible (rows are never duplicated)."""
    out = []
    for c in UNIVERSE:
        row = db.q1("SELECT * FROM characters WHERE channel_id=? AND json_extract(design, '$.universe_key')=?",
                    (channel_id, c["key"]))
        identity = {k: c[k] for k in ("key", "name", "gender", "role", "age", "appearance", "clothing",
                                      "accessories", "backstory", "gestures")}
        design = {"universe_key": c["key"], "look": LOOK, "gender": c["gender"], "identity": identity,
                  "performance": performance(c["key"]), "refs": {}, "refs_meta": {}}
        if row:
            old = db.loads(row["design"], {}) or {}
            design["refs"], design["refs_meta"] = old.get("refs") or {}, old.get("refs_meta") or {}
            db.update("characters", row["id"], {"design": db.dumps(design), "personality": c["personality"],
                                                "voice": db.dumps({**c["voice"], "logical_id":
                                                                   PERFORMANCE[c["key"]]["logical_id"]}),
                                                "updated_at": db.iso()})
            out.append(dict(db.q1("SELECT * FROM characters WHERE id=?", (row["id"],))))
            continue
        cid = db.insert("characters", {"channel_id": channel_id, "name": c["name"], "role": c["role"],
                                       "design": db.dumps(design), "personality": c["personality"],
                                       "voice": db.dumps(c["voice"]), "relationships": "", "rules":
                                       "Never redesign: always made from the approved reference sheets.",
                                       "status": "approved", "created_at": db.iso(), "updated_at": db.iso()})
        out.append(dict(db.q1("SELECT * FROM characters WHERE id=?", (cid,))))
    return out


def library(channel_id: int) -> list[dict[str, Any]]:
    """The channel's main characters with their reference pictures, voice profile and identity metadata."""
    rows = db.q("SELECT * FROM characters WHERE channel_id=? AND json_extract(design, '$.universe_key') IS NOT NULL "
                "ORDER BY id", (channel_id,))
    out = []
    for r in rows:
        d = db.loads(r["design"], {}) or {}
        refs = {k: v for k, v in (d.get("refs") or {}).items() if v and Path(v).exists()}
        out.append({"id": r["id"], "key": d["universe_key"], "name": r["name"], "role": r["role"],
                    "identity": d.get("identity") or {}, "personality": r["personality"],
                    "voice": db.loads(r["voice"], {}), "refs": refs, "refs_meta": d.get("refs_meta") or {},
                    "missing": [s for s in PICTURES if s not in refs], "reference_path": r["reference_path"]})
    return out


def build_sheets(channel_id: int, gen, *, limit: int | None = None) -> dict[str, Any]:
    """Make the missing reference pictures (front first; every other sheet is drawn FROM the front view).
    gen(prompt, out, refs=..., size=...) is the film studio's free image function. Stops at the first provider
    wait (allowance used up) — the next run continues where this one stopped."""
    from . import film
    ensure_universe(channel_id)
    made, waiting = [], None
    for c in UNIVERSE:
        row = db.q1("SELECT * FROM characters WHERE channel_id=? AND json_extract(design, '$.universe_key')=?",
                    (channel_id, c["key"]))
        design = db.loads(row["design"], {}) or {}
        refs, meta = design.setdefault("refs", {}), design.setdefault("refs_meta", {})
        for sheet in PICTURES:
            if refs.get(sheet) and Path(refs[sheet]).exists():
                continue
            if limit is not None and len(made) >= limit:
                return {"made": made, "waiting": "limit"}
            spec = SHEETS[sheet]
            front = refs.get("front")
            if spec.get("refs", True) and not (front and Path(front).exists()):
                break                                        # front first; the rest are drawn from it
            out = library_dir(channel_id) / f"{c['key'].lower()}_{sheet}.png"
            try:
                with film.look_as(LOOK):
                    gen(sheet_prompt(c, sheet), out, refs=[Path(front)] if spec.get("refs", True) else None,
                        size=spec["size"])
            except film.WaitForAllowance:
                waiting = "free image allowance used up — continues after the reset"
                break
            except Exception as e:                           # network, memory guard: this character waits
                waiting = f"{c['name']} {sheet}: {type(e).__name__} — continues on the next run"
                break
            info = {}
            try:
                info = json.loads(out.with_suffix(".json").read_text())
            except (OSError, ValueError):
                pass
            if spec.get("refs", True) and not info.get("refs_used"):
                out.unlink(missing_ok=True)                  # drawn without the face: inconsistent, don't keep it
                out.with_suffix(".json").unlink(missing_ok=True)
                waiting = "the reference-capable image service didn't answer — the rest continues on the next run"
                break                                        # this character waits; the others still get a front
            refs[sheet] = str(out)
            meta[sheet] = {"provider": info.get("provider"), "from_front": bool(info.get("refs_used")),
                           "made_at": db.iso()}
            made.append(f"{c['name']} {sheet}")
            db.update("characters", row["id"], {"design": db.dumps(design), "updated_at": db.iso(),
                                                **({"reference_path": str(out)} if sheet == "front" else {})})
        if waiting and "allowance" in waiting:
            break                                            # nothing more can be drawn today
    if made:
        logs.event("info", "kahaani", f"Character library: made {len(made)} reference pictures "
                   f"({', '.join(made[:6])}{'…' if len(made) > 6 else ''})")
    return {"made": made, "waiting": waiting}


def cast_for(channel_id: int, keys: list[str]) -> dict[str, dict[str, Any]]:
    """Library entries for the recurring characters a story uses: approved front picture, bible and fixed voice."""
    by_key = {c["key"]: c for c in library(channel_id)}
    return {k: by_key[k] for k in keys if k in by_key and by_key[k]["refs"].get("front")}


# ------------------------------------------------------------------ the channel

def convert_channel(channel_id: int, actor: str) -> dict[str, Any]:
    """Make this channel KahaaniVerse: niche, language, workflow, schedule times; finance schedules switched off.
    The previous settings are kept on the audit trail (and nothing already made or uploaded is touched)."""
    from .. import ops, scheduler
    ch = channels.get(channel_id)
    before = {k: ch.get(k) for k in ("name", "niche", "sub_niche", "audience", "language", "tone", "styles", "workflow",
                                     "long_minutes", "short_seconds", "schedule_enabled", "schedule_time",
                                     "publish_time", "instructions", "restrictions", "branding", "made_for_kids")}
    branding = dict(ch.get("branding") or {})
    branding.pop("mascot", None)                   # Fibbu stays with the Kids and AI News channels
    branding["kahaani"] = {"tagline": TAGLINE, "genre_mix": {g: v["share"] for g, v in GENRES.items()},
                           "short_publish_time": "13:00", "long_publish_time": "19:00", "look": LOOK}
    channels.update(channel_id, {
        "name": NAME, "niche": "Original Indian cinematic 3D-animated stories — suspense, mystery, family drama, "
                               "psychological thrillers, village mysteries, sci-fi",
        "sub_niche": "connected story universe with recurring characters (Aarav, Siya, Veer, Dadaji)",
        "audience": "Hindi and Hinglish speaking viewers, 16–45, who love suspense, twists and emotional stories",
        "language": "Hinglish", "tone": "cinematic, gripping, emotional; natural spoken Hindi with everyday English",
        "styles": ["storytelling", "mystery", "thriller", "drama", "horror"], "workflow": "cinematic_film",
        "formats": "both", "long_minutes": 10, "short_seconds": 45, "schedule_enabled": False,
        "schedule_time": "09:00", "publish_time": "19:00", "made_for_kids": False,
        "instructions": f"{TAGLINE} Original stories only; recurring characters keep their exact looks and voices.",
        "restrictions": "No copying other creators' stories, characters or titles; no real people; no gore; "
                        "never publish a defective film to keep the schedule.",
        "branding": branding}, actor=actor)
    off = []
    for t in ("nifty_daily_episode", "fib_lesson_daily"):
        for s in db.q("SELECT id, name FROM schedules WHERE task_type=? AND enabled=1", (t,)):
            db.update("schedules", s["id"], {"enabled": 0})
            off.append(s["name"])
    ops.audit(actor, "channel_converted", channel_id=channel_id,
              detail={"to": NAME, "before": before, "schedules_switched_off": off})
    scheduler.sync_channels()
    from .. import kahaani_tasks
    kahaani_tasks.ensure_schedule(enabled=False)
    ensure_universe(channel_id)
    logs.event("info", "kahaani", f"Channel #{channel_id} is now {NAME}; finance schedules switched off: "
               f"{', '.join(off) or 'none were on'}")
    return {"channel_id": channel_id, "schedules_off": off}
