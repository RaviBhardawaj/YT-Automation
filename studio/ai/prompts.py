"""Shared prompt text. Kept in one place so the rules are identical for every provider."""
from __future__ import annotations

from typing import Any

GROUNDING_RULES = """\
GROUND RULES (apply to every answer):
1. You have NO internet access. The EVIDENCE list in the prompt is everything you know about recent events.
   Use your general background knowledge only for well-established, non-numeric facts.
2. Never invent statistics, view counts, subscriber counts, search volumes, rankings, dates, quotes or URLs.
   Any number you state must appear in the EVIDENCE; otherwise omit it or describe it qualitatively.
3. Refer to evidence only by its numeric id (e.g. 12). Never write URLs yourself.
4. Label uncertainty honestly. "fact" = directly supported by a cited evidence item; "estimate" = derived
   from cited numbers; "hypothesis" = your judgement.
5. Be original. Do not reuse sentences, hooks, titles, thumbnail ideas or signature formats of other
   creators — including the YouTube videos in the evidence. Paraphrase facts in your own words and structure.
6. Never imitate or name-drop a real person's voice, catchphrases or persona. No medical, legal or
   financial advice; no defamation; nothing unsafe for the stated audience.
7. Output exactly one JSON object that matches the requested schema. No markdown, no commentary."""


def system(role: str) -> str:
    return f"You are {role} for a YouTube channel.\n\n{GROUNDING_RULES}"


def evidence_block(items: list[dict[str, Any]], max_chars: int = 14000, content_chars: int = 900) -> str:
    """Numbered evidence list using database ids, trimmed to a character budget."""
    lines: list[str] = []
    used = 0
    for e in items:
        m = e.get("metrics") or {}
        facts = []
        for key in ("views", "channel", "channel_subscribers", "views_per_subscriber", "wiki_views_30d",
                    "wiki_trend_pct", "points", "comments", "duration_s"):
            if m.get(key) not in (None, "", 0):
                facts.append(f"{key}={m[key]}")
        body = (e.get("content") or e.get("snippet") or "").replace("\n", " ").strip()[:content_chars]
        line = (f"[{e['id']}] ({e['source_type']}; published {e.get('published_at') or 'unknown'}) {e.get('title', '')}"
                + (f" | metrics: {', '.join(facts)}" if facts else "")
                + (f"\n    {body}" if body else ""))
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    return "EVIDENCE:\n" + ("\n".join(lines) if lines else "(none collected — rely on caution; mark claims as hypothesis)")
