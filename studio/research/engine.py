"""Niche research, topic ideation & ranking, and per-topic deep research.

Separation of concerns:
  * measured numbers  -> computed here from evidence returned by source APIs ("verified")
  * interpretation    -> produced by the AI router, stored separately and labelled
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from .. import ai, db, logs, net, profile, settings
from ..ai import prompts
from . import dedup, sources

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ schemas

class Claim(BaseModel):
    text: str = Field(..., max_length=400)
    kind: Literal["fact", "estimate", "hypothesis"]
    evidence_id: int = Field(..., description="Supporting evidence id, or 0 if none")


class NicheIdea(BaseModel):
    name: str = Field(..., min_length=3, max_length=80)
    description: str = Field(..., max_length=500)
    search_queries: list[str] = Field(..., min_length=2, max_length=4,
                                      description="2-4 short search queries (mix English and the channel language)")
    example_topics: list[str] = Field(..., min_length=3, max_length=6)
    why_it_fits: str = Field(..., max_length=500)


class NicheIdeas(BaseModel):
    niches: list[NicheIdea] = Field(..., min_length=3, max_length=6)


class NicheAssessment(BaseModel):
    summary: str = Field(..., max_length=900)
    demand_reading: str = Field(..., max_length=900, description="Interpretation of the MEASURED signals; cite evidence ids")
    competition_reading: str = Field(..., max_length=900)
    example_topics: list[str] = Field(..., min_length=3, max_length=8)
    production_cost: Literal["low", "medium", "high"]
    production_cost_reason: str = Field(..., max_length=500)
    monetization_potential: Literal["low", "medium", "high", "unknown"]
    monetization_reason: str = Field(..., max_length=600)
    uncertainty: Literal["low", "medium", "high"]
    uncertainty_reason: str = Field(..., max_length=500)
    risks: list[str] = Field(..., max_length=6)
    claims: list[Claim] = Field(..., max_length=8)


class TopicIdea(BaseModel):
    title: str = Field(..., min_length=8, max_length=100)
    search_query: str = Field(..., min_length=3, max_length=80, description="Short ENGLISH search query for research")
    angle: str = Field(..., max_length=400, description="What makes our take original")
    format: Literal["short", "long"]
    hook_idea: str = Field(..., max_length=300)
    why_now: str = Field(..., max_length=400)
    evidence_ids: list[int] = Field(..., max_length=8)
    audience_fit: int = Field(..., ge=1, le=5)
    claims: list[Claim] = Field(..., max_length=5)


class TopicIdeas(BaseModel):
    ideas: list[TopicIdea] = Field(..., min_length=1, max_length=20)


# ------------------------------------------------------------------ runs & evidence

def start_run(kind: str, subject: str) -> int:
    return db.insert("research_runs", {"kind": kind, "subject": subject, "status": "running", "created_at": db.iso()})


def finish_run(run_id: int, notes: str, sources_used: dict[str, Any], status: str = "done") -> None:
    db.update("research_runs", run_id, {"status": status, "notes": notes, "sources_used": db.dumps(sources_used),
                                        "finished_at": db.iso()})


def save_evidence(run_id: int, items: list[dict[str, Any]], niche_id: int | None = None,
                  topic_id: int | None = None) -> list[dict[str, Any]]:
    seen: set[str] = set()
    saved = []
    for e in items:
        url = (e.get("url") or "").strip()
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        row = {"run_id": run_id, "niche_id": niche_id, "topic_id": topic_id, "source_type": e["source_type"],
               "url": url, "title": (e.get("title") or "")[:300], "published_at": e.get("published_at") or "",
               "snippet": (e.get("snippet") or "")[:1000], "content": (e.get("content") or "")[:8000],
               "metrics": db.dumps(e.get("metrics") or {}), "fetched_at": db.iso()}
        row["id"] = db.insert("evidence", row)
        row["metrics"] = e.get("metrics") or {}
        saved.append(row)
    return saved


def load_evidence(where: str, params: tuple) -> list[dict[str, Any]]:
    rows = db.q(f"SELECT * FROM evidence WHERE {where} ORDER BY id", params)
    for r in rows:
        r["metrics"] = db.loads(r["metrics"], {})
    return rows


def collect(queries: list[str], *, purpose: str, run_id: int, niche_id: int | None = None,
            topic_id: int | None = None, fmt: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Query every enabled source. Failures are recorded, never fatal."""
    enabled = settings.get("research.sources")
    language, region = profile.language(), settings.get("research.region")
    used: dict[str, Any] = {}
    items: list[dict[str, Any]] = []

    def attempt(name: str, fn, *args, **kwargs) -> None:
        if not enabled.get(name, False):
            used.setdefault(name, "disabled")
            return
        try:
            got = fn(*args, **kwargs)
            items.extend(got)
            used[name] = (used.get(name, 0) if isinstance(used.get(name), int) else 0) + len(got)
        except net.FetchError as e:
            used[name] = f"unavailable: {e}"[:200]
        except Exception as e:  # a broken source must not stop research
            log.exception("source %s failed", name)
            used[name] = f"error: {e}"[:200]

    queries = [q.strip() for q in queries if q and q.strip()][:3]
    for i, query in enumerate(queries):
        attempt("google_news", sources.google_news, query, language, region, days=7 if purpose != "topic" else 30)
        attempt("ollama_web", sources.ollama_web_search, query, 6 if purpose == "topic" else 5)
        if i == 0 or purpose == "niche":
            attempt("wikipedia", sources.wikipedia, query, language, 2, with_text=(purpose == "topic"))
        if i == 0:
            if purpose != "topic":
                duration = {"short": "short", "long": "medium"}.get(fmt or "", None)
                attempt("youtube", sources.youtube_search, query, language, region,
                        days=180, limit=15, order="viewCount" if purpose == "ideation" else "relevance", duration=duration)
            attempt("hackernews", sources.hackernews, query)
            attempt("rss", sources.rss, query.split())

    if purpose == "topic":
        fetched = 0
        for e in list(items):
            if e["source_type"] != "web" or fetched >= int(settings.get("research.max_page_fetch")):
                continue
            try:
                title, text = sources.page_text(e["url"])
                if len(text) > 400:
                    e["content"] = text
                    e["title"] = e.get("title") or title
                    fetched += 1
            except net.FetchError as err:
                e["snippet"] = (e.get("snippet") or "") + f" [full text not fetched: {err}]"[:200]
        used["pages_fetched"] = fetched

    saved = save_evidence(run_id, items, niche_id=niche_id, topic_id=topic_id)
    return saved, used


# ------------------------------------------------------------------ niche research

def niche_metrics(ev: list[dict[str, Any]], used: dict[str, Any]) -> dict[str, Any]:
    yt = [e for e in ev if e["source_type"] == "youtube"]
    now = datetime.now(timezone.utc)
    views = [e["metrics"].get("views", 0) for e in yt]
    vps = [e["metrics"]["views_per_subscriber"] for e in yt if e["metrics"].get("views_per_subscriber")]
    recent = [e for e in yt if (db.parse_iso(e["published_at"]) or now - timedelta(days=999)) > now - timedelta(days=90)]
    channels: dict[str, dict[str, Any]] = {}
    for e in yt:
        m = e["metrics"]
        c = channels.setdefault(m.get("channel_id", ""), {"name": m.get("channel"), "url": m.get("channel_url"),
                                                          "subscribers": m.get("channel_subscribers"), "sample_videos": 0,
                                                          "sample_views": 0})
        c["sample_videos"] += 1
        c["sample_views"] += m.get("views", 0)
    wiki = [e for e in ev if e["source_type"] == "wikipedia"]
    news = [e for e in ev if e["source_type"] == "news"]
    return {
        "youtube_sampled": len(yt),
        "youtube_median_views": int(statistics.median(views)) if views else None,
        "youtube_max_views": max(views) if views else None,
        "youtube_recent_90d": len(recent),
        "youtube_median_views_per_sub": round(statistics.median(vps), 2) if vps else None,
        "competitors": sorted(channels.values(), key=lambda c: c["sample_views"], reverse=True)[:6],
        "wiki_views_30d": sum(e["metrics"].get("wiki_views_30d", 0) or 0 for e in wiki) or None,
        "wiki_trend_pct": next((e["metrics"].get("wiki_trend_pct") for e in wiki if e["metrics"].get("wiki_trend_pct") is not None), None),
        "news_items_7d": max((e["metrics"].get("results_in_window", 0) for e in news), default=None),
        "web_results": len([e for e in ev if e["source_type"] == "web"]),
        "sources": used,
    }


def run_niche_research(payload: dict[str, Any]) -> dict[str, Any]:
    language = profile.language()
    run_id = start_run("niche", "niche discovery")
    candidates: list[NicheIdea]
    user_given = [c for c in payload.get("candidates", []) if c.strip()]
    if user_given:
        candidates = [NicheIdea(name=c[:80], description=c, search_queries=[c, f"{c} {language}"],
                                example_topics=["-", "-", "-"], why_it_fits="Provided by you") for c in user_given]
    else:
        prompt = (f"CHANNEL PROFILE: {profile.summary_text()}\n\n"
                  f"Propose 5 distinct YouTube niches that suit this profile and can be produced with narration, "
                  f"stock footage, licensed images and motion graphics (no on-camera presenter). Prefer niches with "
                  f"evergreen interest plus a steady stream of new developments. For each give 2-4 search queries "
                  f"(at least one in English) that would reveal audience interest, and 3-6 example video topics. "
                  f"Do not claim any numbers — we will measure demand ourselves.")
        ideas, _meta = ai.generate("niche_research", prompts.system("a YouTube strategy researcher"), prompt, NicheIdeas,
                                   max_tokens=4000)
        candidates = ideas.niches

    results = []
    all_used: dict[str, Any] = {}
    for cand in candidates:
        niche_id = db.insert("niches", {"name": cand.name, "description": cand.description,
                                        "status": "candidate", "source": "user" if user_given else "ai",
                                        "search_queries": db.dumps(cand.search_queries),
                                        "research_run_id": run_id, "created_at": db.iso()})
        ev, used = collect(cand.search_queries, purpose="niche", run_id=run_id, niche_id=niche_id)
        all_used[cand.name] = used
        metrics = niche_metrics(ev, used)
        valid_ids = {e["id"] for e in ev}
        prompt = (f"CHANNEL PROFILE: {profile.summary_text()}\n\nNICHE: {cand.name} — {cand.description}\n"
                  f"Example topics proposed earlier: {', '.join(cand.example_topics)}\n\n"
                  f"MEASURED SIGNALS (computed by our software from the evidence; treat as facts): "
                  f"{db.dumps({k: v for k, v in metrics.items() if k != 'sources'})}\n"
                  f"Sources that were unavailable: {db.dumps({k: v for k, v in used.items() if isinstance(v, str)})}\n\n"
                  f"{prompts.evidence_block(ev, max_chars=9000, content_chars=300)}\n\n"
                  "Assess this niche for the channel. Interpret the measured signals (do not invent new numbers). "
                  "Production cost: judge for a narration + stock footage + motion graphics workflow. Monetization: "
                  "qualitative only — explain drivers (advertiser appeal, audience geography, sponsorship fit) and "
                  "say what is unknown; never quote RPM/CPM figures. If key sources were unavailable, raise uncertainty.")

        def check(a: NicheAssessment) -> None:
            for c in a.claims:
                if c.evidence_id and c.evidence_id not in valid_ids:
                    raise ValueError(f"claim cites evidence id {c.evidence_id} which does not exist; use 0 or a listed id")

        try:
            assessment, meta = ai.generate("niche_research", prompts.system("a careful YouTube market analyst"),
                                           prompt, NicheAssessment, max_tokens=5000, validate=check)
            data = assessment.model_dump()
            data["claims"] = _normalise_claims(data["claims"], valid_ids)
            data["_generated_by"] = meta
        except (ai.AllProvidersFailed, ai.BudgetExceeded) as e:
            data = {"error": str(e)}
        if not user_given:
            data.setdefault("example_topics", cand.example_topics)
            data["why_it_fits"] = cand.why_it_fits
        db.update("niches", niche_id, {"assessment": db.dumps(data), "metrics": db.dumps(metrics)})
        results.append({"niche_id": niche_id, "name": cand.name})
        logs.event("info", "research", f"Niche assessed: {cand.name} ({len(ev)} evidence items)")
    finish_run(run_id, f"{len(results)} niches assessed", all_used)
    return {"run_id": run_id, "niches": results}


def _normalise_claims(claims: list[dict[str, Any]], valid_ids: set[int]) -> list[dict[str, Any]]:
    out = []
    for c in claims:
        c = dict(c)
        if c["kind"] in ("fact", "estimate") and c.get("evidence_id") not in valid_ids:
            c["kind"] = "hypothesis"
            c["note"] = "downgraded: no supporting evidence id"
        out.append(c)
    return out


# ------------------------------------------------------------------ ideation & ranking

def _existing_titles() -> list[tuple[int, str]]:
    return [(r["id"], r["title"]) for r in db.q("SELECT id, title FROM topics WHERE status != 'rejected' ORDER BY id DESC LIMIT 500")]


def _performance_context() -> str:
    rows = db.q("""SELECT p.title, p.format, a.metrics FROM analytics a JOIN productions p ON p.id=a.production_id
                   WHERE a.id IN (SELECT MAX(id) FROM analytics GROUP BY video_id)""")
    if not rows:
        return "CHANNEL PERFORMANCE: no published videos with analytics yet."
    scored = []
    for r in rows:
        m = db.loads(r["metrics"], {})
        scored.append((m.get("views", 0), m.get("averageViewPercentage"), r["title"], r["format"]))
    scored.sort(reverse=True)
    best = "; ".join(f"{t} ({f}, {v} views, avg viewed {p}%)" for v, p, t, f in scored[:5])
    worst = "; ".join(f"{t} ({f}, {v} views)" for v, p, t, f in scored[-3:])
    return (f"CHANNEL PERFORMANCE (our own analytics; small samples are noisy, treat as weak signals): "
            f"best: {best}. weakest: {worst}.")


def _gap_check(idea: TopicIdea, language: str, region: str) -> dict[str, Any]:
    try:
        vids = sources.youtube_search(idea.search_query, language, region, days=365, limit=10, order="relevance",
                                      duration="short" if idea.format == "short" else "medium")
    except net.FetchError as e:
        return {"measured": False, "note": str(e)[:200]}
    now = datetime.now(timezone.utc)
    views = [v["metrics"]["views"] for v in vids]
    recent = [v for v in vids if (db.parse_iso(v["published_at"]) or now - timedelta(days=999)) > now - timedelta(days=90)]
    med = int(statistics.median(views)) if views else 0
    if not vids:
        score, reading = 9, "no comparable videos found in the last year (untested demand)"
    elif med >= 100_000 and len(recent) <= 2:
        score, reading = 15, "high views on existing videos but little fresh supply"
    elif med >= 100_000:
        score, reading = 7, "high demand but crowded with recent videos"
    elif med < 5_000:
        score, reading = 4, "existing videos get few views"
    else:
        score, reading = 10, "moderate demand and competition"
    closest = max((dedup.similarity(idea.title, v["title"]) for v in vids), default=0.0)
    return {"measured": True, "sampled": len(vids), "median_views": med, "recent_90d": len(recent),
            "score": score, "reading": reading, "closest_title_similarity": round(closest, 2),
            "examples": [{"title": v["title"], "url": v["url"], "views": v["metrics"]["views"]} for v in vids[:5]]}


def score_topic(idea: TopicIdea, ev_by_id: dict[int, dict[str, Any]], gap: dict[str, Any] | None,
                novelty: float) -> tuple[float, dict[str, Any]]:
    refs = [ev_by_id[i] for i in dict.fromkeys(idea.evidence_ids) if i in ev_by_id]
    now = datetime.now(timezone.utc)
    breakdown: dict[str, Any] = {}

    n = len(refs)
    breakdown["evidence"] = {"points": min(25, n * 7), "max": 25, "basis": f"{n} cited evidence item(s)", "kind": "measured"}

    ages = [(now - d).days for d in (db.parse_iso(e["published_at"]) for e in refs) if d]
    if ages:
        newest = min(ages)
        pts = 20 if newest <= 3 else 15 if newest <= 7 else 9 if newest <= 30 else 4
        breakdown["freshness"] = {"points": pts, "max": 20, "basis": f"newest cited item is {newest} day(s) old", "kind": "measured"}
    else:
        breakdown["freshness"] = {"points": 4, "max": 20, "basis": "no dated evidence (evergreen or unknown)", "kind": "measured"}

    signals = [s for s in (sources.demand_points(e["metrics"]) for e in refs) if s is not None]
    if signals:
        breakdown["demand"] = {"points": round(max(signals) * 25, 1), "max": 25,
                               "basis": "strongest real metric among cited items (views / pageviews / points)", "kind": "measured"}
    else:
        breakdown["demand"] = {"points": 5, "max": 25, "basis": "no measured demand signal", "kind": "unknown"}

    if gap and gap.get("measured"):
        pts = gap["score"] - (6 if gap.get("closest_title_similarity", 0) >= 0.8 else 0)
        breakdown["gap"] = {"points": max(0, pts), "max": 15, "basis": gap["reading"], "kind": "estimate"}
    else:
        breakdown["gap"] = {"points": 7, "max": 15, "basis": "competition not measured (needs YouTube API key)", "kind": "unknown"}

    breakdown["audience_fit"] = {"points": idea.audience_fit * 3, "max": 15, "basis": "AI judgement of fit with your profile",
                                 "kind": "hypothesis"}
    if novelty >= dedup.NEAR:
        breakdown["novelty_penalty"] = {"points": -10, "max": 0, "basis": f"similar to an earlier topic ({novelty:.2f})",
                                        "kind": "measured"}
    total = round(sum(v["points"] for v in breakdown.values()), 1)
    return total, breakdown


def _channel_fields(ch: dict[str, Any] | None, idea: Any, ev_by_id: dict[int, Any], language: str) -> dict[str, Any]:
    if not ch:
        return {}
    cited = [ev_by_id[i] for i in idea.evidence_ids if i in ev_by_id]
    dated = sorted((e for e in cited if e.get("published_at")), key=lambda e: e["published_at"], reverse=True)
    fresh = bool(dated) and (db.parse_iso(dated[0]["published_at"]) or datetime.min.replace(tzinfo=timezone.utc)) > \
        datetime.now(timezone.utc) - timedelta(days=21)
    src = (dated or cited or [{}])[0]
    return {"channel_id": ch["id"], "workspace_id": ch["workspace_id"], "idea_kind": "trend" if fresh else "inspired" if cited else "evergreen",
            "style": next(iter(ch.get("styles") or []), None), "language": language, "summary": idea.angle,
            "source_url": src.get("url"), "source_date": (src.get("published_at") or "")[:10] or None}


def run_ideation(payload: dict[str, Any]) -> dict[str, Any]:
    ch = None
    if payload.get("channel_id"):                     # studio channel: its own niche, language and audience
        from .. import channels
        ch = channels.get(int(payload["channel_id"]))
        niche = {"id": None, "name": ch["niche"] or ch["name"],
                 "description": "; ".join(x for x in (ch["sub_niche"], ch["audience"] and f"audience: {ch['audience']}",
                                                      ch["instructions"]) if x),
                 "search_queries": db.dumps([q for q in (ch["niche"], ch["sub_niche"], payload.get("focus")) if q])}
    else:
        niche = db.q1("SELECT * FROM niches WHERE id=?", (payload["niche_id"],)) if payload.get("niche_id") else \
            db.q1("SELECT * FROM niches WHERE status='approved' ORDER BY approved_at DESC LIMIT 1")
    if not niche:
        raise ValueError("No approved niche yet — approve one on the Niches page first.")
    count = int(payload.get("count", 10))
    fmts = payload.get("formats") or profile.formats()
    language, region = profile.language(), settings.get("research.region")
    run_id = start_run("ideation", niche["name"])
    queries = db.loads(niche["search_queries"], []) or [niche["name"]]
    ev, used = collect(queries, purpose="ideation", run_id=run_id, niche_id=niche["id"],
                       fmt=fmts[0] if len(fmts) == 1 else None)
    ev_by_id = {e["id"]: e for e in ev}
    existing = _existing_titles()
    prompt = (f"CHANNEL PROFILE: {profile.summary_text()}\nNICHE: {niche['name']} — {niche['description']}\n"
              f"{_performance_context()}\n\n"
              f"ALREADY COVERED OR PLANNED (do not repeat or closely paraphrase): "
              f"{'; '.join(t for _, t in existing[:80]) or 'nothing yet'}\n\n"
              f"{prompts.evidence_block(ev)}\n\n"
              f"Propose {count} original video topics. Allowed formats: {', '.join(fmts)}. "
              f"Ground each idea in the evidence (cite ids) — prefer fresh developments, real questions people are "
              f"asking, and gaps where existing videos are old or shallow. Each title must be a working title in the "
              f"channel language, specific and honest (no clickbait lies). Give each a distinct original angle. "
              f"{profile.language_rules()}")

    def check(ideas: TopicIdeas) -> None:
        bad = [i.format for i in ideas.ideas if i.format not in fmts]
        if bad:
            raise ValueError(f"format must be one of {fmts}")

    ideas, meta = ai.generate("ideation", prompts.system("a YouTube content strategist"), prompt, TopicIdeas,
                              max_tokens=7000, validate=check)
    gap_budget = 5 if settings.has_secret("youtube_api_key") and settings.get("research.sources").get("youtube") else 0
    created, dupes = [], 0
    batch: list[tuple[int, str]] = []
    # Gap checks cost YouTube quota: spend them on the ideas with the best evidence first.
    ordered = sorted(ideas.ideas, key=lambda i: -len([x for x in i.evidence_ids if x in ev_by_id]))
    for idea in ordered:
        dup_id, sim = dedup.best_match(idea.title, existing + batch)
        if sim >= dedup.DUPLICATE:
            dupes += 1
            continue
        gap = None
        if gap_budget > 0:
            gap = _gap_check(idea, language, region)
            gap_budget -= 1
        score, breakdown = score_topic(idea, ev_by_id, gap, sim)
        claims = _normalise_claims([c.model_dump() for c in idea.claims], set(ev_by_id))
        tid = db.insert("topics", {
            "niche_id": niche["id"], "run_id": run_id, "title": idea.title.strip(), "angle": idea.angle,
            "format": idea.format, "hook_idea": idea.hook_idea, "why_now": idea.why_now, "status": "idea",
            "score": score, "score_breakdown": db.dumps(breakdown), "claims": db.dumps(claims),
            "evidence_ids": db.dumps([i for i in dict.fromkeys(idea.evidence_ids) if i in ev_by_id]),
            "gap": db.dumps(gap or {}), "search_query": idea.search_query, "norm_title": dedup.normalize(idea.title),
            "created_at": db.iso(), "updated_at": db.iso(), **_channel_fields(ch, idea, ev_by_id, language)})
        batch.append((tid, idea.title))
        created.append(tid)
    finish_run(run_id, f"{len(created)} ideas, {dupes} duplicates skipped (by {meta['provider']}/{meta['model']})", used)
    logs.event("info", "research", f"Ideation for '{niche['name']}': {len(created)} new ideas, {dupes} duplicates skipped")
    return {"run_id": run_id, "created": created, "duplicates_skipped": dupes, "sources": used}


# ------------------------------------------------------------------ per-topic research

def research_topic(topic: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect deeper evidence for one topic. Returns all evidence linked to it."""
    run_id = start_run("topic", topic["title"])
    queries = [topic.get("search_query") or topic["title"]]
    if topic.get("search_query") and topic["title"] != topic["search_query"]:
        queries.append(topic["title"])
    _, used = collect(queries, purpose="topic", run_id=run_id, topic_id=topic["id"])
    finish_run(run_id, "topic research", used)
    original = db.loads(topic.get("evidence_ids"), []) or []
    linked = load_evidence("topic_id=?", (topic["id"],))
    if original:
        marks = ",".join("?" for _ in original)
        linked = load_evidence(f"id IN ({marks})", tuple(original)) + linked
    return linked
