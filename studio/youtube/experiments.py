"""Daily experiments: every scheduled video tries ONE new thing, and the channel keeps what gets more views.

Each video tests one dimension (hook style, title style, commentary angle — and for news channels, US vs India
stories); the other dimensions use their current best version. Results come from the channel's own YouTube numbers:
views per day of each version, once a video is at least a day old. A version wins when it has at least
MIN_VIDEOS videos and beats the next best by WIN_RATIO; after that it's used most of the time (EXPLOIT), and the
others still get an occasional try so a change in taste is noticed. One variable per video, so a result can be read.
Nothing here touches YouTube itself — only how our next video is written or which story it covers.
"""
from __future__ import annotations

import random
from typing import Any

from .. import db

MIN_VIDEOS = 3
WIN_RATIO = 1.3
EXPLOIT = 0.7
MIN_AGE_DAYS = 1.0

DIMENSIONS: dict[str, dict[str, str]] = {
    "hook": {
        "question": "OPENING: start with a sharp question the viewer wants answered (answer it within the video).",
        "shock_fact": "OPENING: start with the single most surprising fact or number from the sources, said plainly.",
        "countdown": "OPENING: start by promising a short list ('3 things you need to know…') and deliver exactly it.",
        "story": "OPENING: start with one vivid human moment or scene from the story, then the facts.",
    },
    "title": {
        "question": "TITLE STYLE: a direct question (honest — the video answers it).",
        "number": "TITLE STYLE: lead with the key number or a count ('3 reasons…', '₹50,000…').",
        "explained": "TITLE STYLE: 'X explained' / 'What X means for you' — clear and searchable.",
        "breaking": "TITLE STYLE: news-desk style — what happened, in under 60 characters, no question.",
    },
    "angle": {
        "what_happened": "COMMENTARY ANGLE: a clear, fast account of what happened and the key facts.",
        "why_it_matters": "COMMENTARY ANGLE: focus on why it matters to ordinary viewers — money, jobs, daily life.",
        "what_next": "COMMENTARY ANGLE: focus on what happens next and what to watch for (attributed, no guessing).",
    },
}
NEWS_ONLY = {"region": {"US": "", "IN": ""}}             # applied when the story is picked, not in the script


def dimensions(news: bool) -> dict[str, dict[str, str]]:
    return {**DIMENSIONS, **(NEWS_ONLY if news else {})}


def _history(channel_id: int) -> list[dict[str, Any]]:
    """This channel's experiment videos with their views per day (only videos at least a day old)."""
    rows = db.q("SELECT p.id, p.request, p.uploaded_at, a.metrics FROM productions p LEFT JOIN analytics a ON "
                "a.production_id=p.id AND a.id=(SELECT MAX(id) FROM analytics WHERE production_id=p.id) "
                "WHERE p.channel_id=? AND json_extract(p.request, '$.experiment') IS NOT NULL", (channel_id,))
    out = []
    for r in rows:
        exp = (db.loads(r["request"], {}) or {}).get("experiment") or {}
        m = db.loads(r["metrics"], {}) or {}
        when = db.parse_iso(r["uploaded_at"])
        age = (db.utcnow() - when).total_seconds() / 86400 if when else 0.0
        out.append({"id": r["id"], "arms": exp.get("arms") or {}, "test": exp.get("test"),
                    "measured": bool(m) and age >= MIN_AGE_DAYS,
                    "views_per_day": round(int(m.get("views") or 0) / max(age, 1.0), 2) if m else None})
    return out


def results(channel_id: int, news: bool = False) -> dict[str, dict[str, Any]]:
    """{dimension: {arms: {arm: {videos, measured, views_per_day}}, winner, tests}} from real numbers."""
    hist = _history(channel_id)
    out: dict[str, dict[str, Any]] = {}
    for dim, arms in dimensions(news).items():
        stats = {}
        for arm in arms:
            vids = [h for h in hist if h["arms"].get(dim) == arm and h["test"] == dim]
            done = [h["views_per_day"] for h in vids if h["measured"]]
            stats[arm] = {"videos": len(vids), "measured": len(done),
                          "views_per_day": round(sum(done) / len(done), 2) if done else None}
        ranked = sorted((a for a, s in stats.items() if s["measured"] >= MIN_VIDEOS), key=lambda a: -stats[a]["views_per_day"])
        winner = None                                # needs two measured versions and a clear gap between them
        if len(ranked) >= 2 and stats[ranked[0]]["views_per_day"] > 0 and \
                stats[ranked[0]]["views_per_day"] >= WIN_RATIO * stats[ranked[1]]["views_per_day"]:
            winner = ranked[0]
        out[dim] = {"arms": stats, "winner": winner, "tests": sum(s["videos"] for s in stats.values())}
    return out


def assign(channel_id: int, news: bool = False, rng: random.Random | None = None) -> dict[str, Any]:
    """The plan for the next video: which dimension it tests and which version of every dimension it uses."""
    rng = rng or random.Random()
    res = results(channel_id, news)
    dims = dimensions(news)
    test = min(dims, key=lambda d: (res[d]["tests"], list(dims).index(d)))      # the least-tested dimension
    arms = {}
    for dim, options in dims.items():
        winner = res[dim]["winner"]
        if dim != test:
            arms[dim] = winner or (rng.choice(list(options)) if dim == "region" else None)
            continue
        if winner and rng.random() < EXPLOIT:
            arms[dim] = winner                                               # keep doing what works
        else:                                                                # try what has been tried least
            arms[dim] = min(options, key=lambda a: (res[dim]["arms"][a]["videos"], rng.random()))
    return {"test": test, "arms": {k: v for k, v in arms.items() if v}}


def instructions(exp: dict[str, Any] | None) -> str:
    """The script-writer lines for this video's versions (hook, title, angle)."""
    arms = (exp or {}).get("arms") or {}
    lines = [DIMENSIONS[d][a] for d, a in arms.items() if d in DIMENSIONS and a in DIMENSIONS[d]]
    return ("THIS VIDEO'S STYLE (follow exactly):\n" + "\n".join(f"- {x}" for x in lines)) if lines else ""


def record(production_ids: list[int], exp: dict[str, Any]) -> None:
    for pid in production_ids or []:
        row = db.q1("SELECT request FROM productions WHERE id=?", (pid,))
        if row is not None:
            req = db.loads(row["request"], {}) or {}
            req["experiment"] = exp
            db.update("productions", pid, {"request": db.dumps(req)})
