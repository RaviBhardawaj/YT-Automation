"""Ideas for creative channels (comedy, horror, kids, drama …): original story concepts, not research topics.

Each idea is written for one channel — its niche, language, tone and recurring characters — and checked
against everything the channel already made or planned, so the list never repeats itself.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .. import ai, channels, db, logs
from ..research import dedup
from . import characters


SYSTEM = ("You are the head writer of an animated YouTube series. Invent original situations and characters; never "
          "copy other creators' titles, formats or catchphrases, and never imitate real people. Keep it safe for the "
          "stated audience. Output exactly one JSON object matching the schema, no commentary.")


class StoryIdea(BaseModel):
    title: str = Field(description="working title in the channel language, specific and catchy")
    logline: str = Field(description="one or two sentences: who wants what, what goes wrong, the payoff")
    hook: str = Field(description="what happens in the first 5 seconds")
    style: str = Field(description="one of the channel's styles, e.g. comedy, horror, kids, drama")
    format: Literal["long", "short"]
    kind: Literal["ai_original", "evergreen"] = Field(description="ai_original = new concept; evergreen = a timeless "
                                                      "situation (festival, school, family) the audience always enjoys")
    characters: list[str] = Field(default_factory=list, description="names of recurring characters used, if any")
    appeal: int = Field(ge=1, le=10, description="your honest estimate of audience appeal")
    originality: int = Field(ge=1, le=10, description="how different it is from typical videos in this niche")


class StoryIdeas(BaseModel):
    ideas: list[StoryIdea]


def generate(channel_id: int, count: int = 10, formats: list[str] | None = None, focus: str = "") -> dict:
    ch = channels.get(channel_id)
    fmts = formats or channels.formats(ch)
    cast = characters.for_channel(channel_id, include_draft=False)
    existing = [(r["id"], r["title"]) for r in db.q(
        "SELECT id, title FROM topics WHERE channel_id=? UNION ALL SELECT id, title FROM productions WHERE channel_id=? "
        "AND kind='story'", (channel_id, channel_id))]
    styles = channels.styles(ch) or ["comedy"]
    prompt = (f"CHANNEL: {ch['name']}\nNICHE: {ch['niche']} {('— ' + ch['sub_niche']) if ch['sub_niche'] else ''}\n"
              f"AUDIENCE: {ch['audience'] or 'general YouTube audience'}\nLANGUAGE: {ch['language']}\n"
              f"TONE: {ch['tone'] or '-'}\nSTYLES: {', '.join(styles)}\n"
              f"CHANNEL INSTRUCTIONS: {ch['instructions'] or '-'}\nRESTRICTIONS: {ch['restrictions'] or '-'}\n"
              f"RECURRING CHARACTERS:\n{characters.bible_text(cast)}\n"
              f"{('FOCUS FOR THIS BATCH: ' + focus) if focus else ''}\n\n"
              f"ALREADY MADE OR PLANNED (never repeat or closely paraphrase): "
              f"{'; '.join(t for _, t in existing[-80:]) or 'nothing yet'}\n\n"
              f"Propose {count} original episode ideas for this channel. Allowed formats: {', '.join(fmts)} "
              f"(short = one punchy situation under a minute; long = a full story with a beginning, twist and payoff). "
              f"This is ENTERTAINMENT: stories, situations and characters — not lessons, tips or explainers. "
              f"Use the recurring characters where they fit. Titles and loglines in {ch['language']}.")

    def check(out: StoryIdeas) -> None:
        if [i.format for i in out.ideas if i.format not in fmts]:
            raise ValueError(f"format must be one of {fmts}")
        if len(out.ideas) < max(1, count // 2):
            raise ValueError(f"give {count} ideas")

    out, meta = ai.generate("ideation", SYSTEM, prompt,
                            StoryIdeas, max_tokens=6000, validate=check)
    created, dupes, batch = [], 0, []
    for idea in out.ideas:
        _, sim = dedup.best_match(idea.title, existing + batch)
        if sim >= dedup.DUPLICATE:
            dupes += 1
            continue
        score = round(idea.appeal * 5.5 + idea.originality * 4.5 - sim * 10, 1)
        breakdown = {"appeal": {"points": idea.appeal * 5.5, "max": 55, "kind": "ai judgement",
                                "basis": "writer's estimate of audience appeal"},
                     "originality": {"points": idea.originality * 4.5, "max": 45, "kind": "ai judgement",
                                     "basis": f"closest existing title similarity {sim:.2f}"}}
        style = idea.style.lower().strip() if idea.style.lower().strip() in channels.STYLES else styles[0]
        tid = db.insert("topics", {
            "channel_id": channel_id, "title": idea.title.strip()[:200], "angle": idea.logline, "format": idea.format,
            "hook_idea": idea.hook, "why_now": "original concept" if idea.kind == "ai_original" else "evergreen",
            "status": "idea", "score": max(0, min(100, score)), "score_breakdown": db.dumps(breakdown),
            "claims": "[]", "evidence_ids": "[]", "gap": db.dumps({"characters": idea.characters}),
            "search_query": idea.title, "norm_title": dedup.normalize(idea.title), "idea_kind": idea.kind,
            "style": style, "language": ch["language"], "summary": idea.logline, "workspace_id": ch["workspace_id"],
            "created_at": db.iso(), "updated_at": db.iso()})
        batch.append((tid, idea.title))
        created.append(tid)
    logs.event("info", "ideas", f"'{ch['name']}': {len(created)} new story ideas, {dupes} duplicates skipped "
               f"(by {meta['provider']}/{meta['model']})")
    return {"created": created, "duplicates_skipped": dupes}


def variations(topic_id: int, count: int = 4) -> dict:
    t = db.q1("SELECT * FROM topics WHERE id=?", (topic_id,))
    if not t or not t.get("channel_id"):
        raise ValueError("idea not found")
    return generate(t["channel_id"], count, [t["format"]],
                    focus=f"variations on this idea — same spirit, clearly different situations: {t['title']} — "
                          f"{t.get('summary') or t.get('angle') or ''}")
