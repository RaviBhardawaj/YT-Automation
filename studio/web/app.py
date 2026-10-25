"""Local web dashboard (FastAPI + Jinja2). Binds to 127.0.0.1 only."""
from __future__ import annotations

import base64
import logging
import mimetypes
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import auth, bootstrap, config, db, logs, net, pipeline, plans, profile, settings, sysinfo, worker
from ..ai import providers, router
from ..media import tts
from ..research import engine
from ..youtube import analytics, oauth

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
MODEL_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,60}(:[A-Za-z0-9._-]{1,40})?")
CSRF_SECRET = secrets.token_bytes(32)
ALLOWED_HOSTS = {f"127.0.0.1:{config.PORT}", f"localhost:{config.PORT}"}
ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOSTS}
if config.PUBLIC_URL:                                   # online: the public address is also accepted
    from urllib.parse import urlsplit as _us
    ALLOWED_HOSTS.add(_us(config.PUBLIC_URL).netloc)
    ALLOWED_ORIGINS.add(f"{_us(config.PUBLIC_URL).scheme}://{_us(config.PUBLIC_URL).netloc}")


def csrf_token(request: Request) -> str:
    """Per-browser token: derived from the session (or a pre-login cookie), never shared between users."""
    seed = request.cookies.get("sid") or request.cookies.get("pre") or getattr(request.state, "pre", "")
    return csrf_for_seed(seed)


def csrf_for_seed(seed: str) -> str:
    import hashlib
    import hmac
    return hmac.new(CSRF_SECRET, seed.encode(), hashlib.sha256).hexdigest()


def set_cookie(resp, name: str, value: str, max_age: int) -> None:
    resp.set_cookie(name, value, httponly=True, samesite="lax", max_age=max_age, secure=config.SECURE_COOKIES)


@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap.init(start_background=True)
    logs.event("info", "app", f"{config.APP_NAME} started on {config.BASE_URL}")
    yield
    worker.stop()


app = FastAPI(title=config.APP_NAME, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
# Productions of different kinds store different optional details: a missing one renders blank, never a 500.
import jinja2  # noqa: E402
templates.env.undefined = jinja2.ChainableUndefined


PUBLIC = ("/login", "/signup", "/api/auth/login", "/api/auth/signup", "/static/", "/oauth/google/callback", "/favicon.ico")
ADMIN_ONLY = ("/analytics", "/settings", "/compliance", "/niches", "/setup", "/admin", "/api/settings", "/api/secrets/",
              "/api/providers/", "/api/ollama/", "/api/voice/", "/api/youtube/", "/api/compliance/", "/api/datasources/",
              "/api/datasets/", "/api/niches/", "/api/interview/", "/api/feeds", "/api/upload/", "/api/lessons/",
              "/api/episodes", "/api/productions/selftest", "/api/analytics/", "/api/ideas/generate", "/api/admin/")
# /<prefix>/<id>: the id must belong to the signed-in workspace (404 otherwise — never reveal other tenants' data)
OWNED_PATHS = [(re.compile(r"^/(?:api/)?productions/(\d+)"), "productions"),
               (re.compile(r"^/download/(\d+)"), "productions"),
               (re.compile(r"^/(?:api/)?(?:ideas|topics)/(\d+)"), "topics"),
               (re.compile(r"^/(?:api/)?channels/(\d+)"), "channels"),
               (re.compile(r"^/api/characters/(\d+)"), "characters"),
               (re.compile(r"^/api/tasks/(\d+)"), "tasks"),
               (re.compile(r"^/(?:api/)?media-files/(\d+)"), "media"),
               (re.compile(r"^/media/productions/(\d+)/"), "productions"),
               (re.compile(r"^/media/channels/(\d+)/"), "channels")]


_attempts: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    # behind Cloudflare / a reverse proxy the real visitor address arrives in a header
    if os.environ.get("STUDIO_TRUST_PROXY") == "1":
        return (request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",")[0]
                or (request.client.host if request.client else "?")).strip()
    return request.client.host if request.client else "?"


def _rate_ok(request: Request, path: str) -> bool:
    """Brute-force guard: 10 sign-in or 5 sign-up attempts per address per 15 minutes."""
    import time
    key, limit = f"{path}:{_client_ip(request)}", (10 if path.endswith("login") else 5)
    now = time.time()
    hits = [t for t in _attempts.get(key, []) if now - t < 900]
    hits.append(now)
    _attempts[key] = hits
    return len(hits) <= limit


def _deny(request: Request, code: int, msg: str):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"ok": False, "error": msg}, status_code=code)
    if code == 401:
        return RedirectResponse("/login?next=" + request.url.path, status_code=303)
    return HTMLResponse(f"<h1>{msg}</h1><p><a href='/'>Back</a></p>", status_code=code)


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.headers.get("host", "") not in ALLOWED_HOSTS:
        return PlainTextResponse("Forbidden host", status_code=403)
    path = request.url.path
    request.state.sess = None
    if not path.startswith(PUBLIC):
        if not auth.has_users():
            return _deny(request, 401, "Create the owner account first") if path.startswith("/api/") else \
                RedirectResponse("/signup", status_code=303)
        sess = auth.session(request.cookies.get("sid"))
        if not sess:
            return _deny(request, 401, "Sign in first")
        request.state.sess = sess
        ws_id = sess["workspace"]["id"]
        if path.startswith(ADMIN_ONLY) and not sess["user"]["is_admin"]:
            return _deny(request, 403, "Only the platform admin can open this")
        for rx, table in OWNED_PATHS:
            m = rx.match(path)
            if m and table == "tasks" and sess["user"]["is_admin"] and \
                    db.q1("SELECT 1 FROM tasks WHERE id=? AND production_id IS NULL", (int(m.group(1)),)):
                continue                                   # system tasks (research, schedules): platform admin
            if m and not auth.owns(ws_id, table, int(m.group(1))):
                return _deny(request, 404, "Not found")
        if path.startswith("/media/samples/") and not sess["user"]["is_admin"]:
            return _deny(request, 404, "Not found")
        if path.startswith("/media/workspaces/") and not path.startswith(f"/media/workspaces/{ws_id:05d}/"):
            return _deny(request, 404, "Not found")
        if request.method not in ("GET", "HEAD", "OPTIONS") and sess["role"] == "viewer":
            return _deny(request, 403, "Viewers can't change anything in this workspace")
        plans._current.set(ws_id)
    new_pre = None
    if not request.cookies.get("sid") and not request.cookies.get("pre"):
        new_pre = request.state.pre = secrets.token_urlsafe(24)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin not in ALLOWED_ORIGINS:
            return PlainTextResponse("Forbidden origin", status_code=403)
        if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), csrf_token(request)):
            return PlainTextResponse("Missing or invalid CSRF token — reload the page", status_code=403)
        if path in ("/api/auth/login", "/api/auth/signup") and not _rate_ok(request, path):
            return JSONResponse({"ok": False, "error": "Too many attempts — wait 15 minutes and try again."},
                                status_code=429)
    response = await call_next(request)
    if new_pre:
        set_cookie(response, "pre", new_pre, 86400)
    if config.SECURE_COOKIES:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    return response


@app.exception_handler(Exception)
async def on_error(request: Request, exc: Exception):
    log.exception("request failed: %s", request.url.path)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"ok": False, "error": logs.redact(f"{type(exc).__name__}: {exc}")[:600]}, status_code=500)
    return HTMLResponse(f"<h1>Something went wrong</h1><p>{type(exc).__name__}</p><p><a href='/'>Back</a></p>", status_code=500)


# ------------------------------------------------------------------ helpers

def ok(**data: Any) -> JSONResponse:
    return JSONResponse({"ok": True, **data})


def bad(msg: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=code)


async def body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def media_url(path: str | None) -> str | None:
    if not path:
        return None
    try:
        rel = Path(path).resolve().relative_to(config.DATA_DIR)
    except ValueError:
        return None
    return "/media/" + str(rel)


def ws_id(request: Request) -> int:
    return request.state.sess["workspace"]["id"]


def is_admin(request: Request) -> bool:
    return bool(request.state.sess and request.state.sess["user"]["is_admin"])


def _my_usage(ws: int) -> dict[str, Any]:
    lim, use = plans.limits(ws), plans.used(ws)
    return {"videos": use["videos"], "limit": lim["videos_per_month"],
            "pct": min(100, int(use["videos"] / lim["videos_per_month"] * 100)) if lim["videos_per_month"] else 0}


def common(request: Request | None = None) -> dict[str, Any]:
    spend = router.month_spend()
    budget = float(settings.get("ai.monthly_budget_usd"))
    sess = getattr(request.state, "sess", None) if request else None
    ws = sess["workspace"]["id"] if sess else 0
    counts = {r["status"]: r["n"] for r in db.q(
        "SELECT t.status, COUNT(*) AS n FROM tasks t LEFT JOIN productions p ON p.id=t.production_id "
        "WHERE p.workspace_id=? OR (? AND t.production_id IS NULL) GROUP BY t.status",
        (ws, int(bool(sess and sess["user"]["is_admin"]))))}
    return {"csrf": csrf_token(request) if request else "", "app_name": config.APP_NAME, "spend": spend, "budget": budget,
            "spend_pct": min(100, int(spend / budget * 100)) if budget else 0,
            "brand": settings.get("brand"), "interview_done": profile.complete(), "task_counts": counts,
            "awaiting": db.q1("SELECT COUNT(*) AS n FROM productions WHERE status='awaiting_approval' AND workspace_id=?",
                              (ws,))["n"],
            "media_url": media_url, "loads": db.loads, "me": sess["user"] if sess else None,
            "workspace": sess["workspace"] if sess else None, "role": sess["role"] if sess else None,
            "is_admin": bool(sess and sess["user"]["is_admin"]),
            "sarvam": tts.sarvam_status() if settings.has_secret("sarvam_api_key") else {"ok": True},
            "my_usage": _my_usage(ws) if sess and not sess["user"]["is_admin"] else None}


def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {**common(request), **ctx, "page": name.removesuffix(".html")})


def _fmt_dt(value: str | None) -> str:
    dt = db.parse_iso(value)
    return dt.astimezone().strftime("%d %b %H:%M") if dt else "—"


templates.env.filters["dt"] = _fmt_dt
templates.env.filters["loads"] = lambda s, d=None: db.loads(s, d)
templates.env.globals["media_url"] = media_url


def _static_version() -> str:
    """Cache-buster derived from the static files' modification times."""
    return str(int(max(p.stat().st_mtime for p in (HERE / "static").iterdir())))


templates.env.globals["static_v"] = _static_version()


# ------------------------------------------------------------------ accounts

def _login_response(token: str, next_url: str) -> RedirectResponse:
    resp = RedirectResponse(next_url if next_url.startswith("/") and not next_url.startswith("//") else "/",
                            status_code=303)
    # Lax (not Strict) so the session survives the redirect back from Google's YouTube authorization
    set_cookie(resp, "sid", token, auth.SESSION_DAYS * 86400)
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", error: str = ""):
    if not auth.has_users():
        return RedirectResponse("/signup", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"csrf": csrf_token(request), "app_name": config.APP_NAME,
                                      "static_v": templates.env.globals["static_v"], "next": next, "error": error,
                                      "mode": "login", "allow_signup": settings.get("auth.allow_signup")})


@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request, error: str = ""):
    first = not auth.has_users()
    if not first and not settings.get("auth.allow_signup"):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"csrf": csrf_token(request), "app_name": config.APP_NAME,
                                      "static_v": templates.env.globals["static_v"], "next": "/", "error": error,
                                      "mode": "signup", "first": first, "allow_signup": True})


@app.post("/api/auth/{action}")
async def api_auth(request: Request, action: str):
    data = await body(request)
    try:
        if action == "signup":
            if auth.has_users() and not settings.get("auth.allow_signup"):
                return bad("Sign-ups are closed.")
            auth.register(str(data.get("email", "")), str(data.get("password", "")), str(data.get("name", "")),
                          str(data.get("workspace", "")))
            action = "login"
        if action == "login":
            token, _ = auth.login(str(data.get("email", "")), str(data.get("password", "")))
            resp = JSONResponse({"ok": True, "next": str(data.get("next") or "/")})
            set_cookie(resp, "sid", token, auth.SESSION_DAYS * 86400)
            return resp
    except auth.AuthError as e:
        return bad(str(e))
    if action == "logout":
        auth.logout(request.cookies.get("sid"))
        resp = JSONResponse({"ok": True, "next": "/login"})
        resp.delete_cookie("sid")
        return resp
    return bad("unknown action")


# ------------------------------------------------------------------ pages

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not db.q1("SELECT 1 FROM channels WHERE workspace_id=?", (ws_id(request),)) and not is_admin(request):
        return RedirectResponse("/welcome", status_code=303)
    from .. import channels as chmod
    cid = _channel_arg(request, request.query_params.get("channel"))
    ws = ws_id(request)
    w, a = ("WHERE p.channel_id=?", (cid,)) if cid else ("WHERE p.workspace_id=?", (ws,))
    prods = db.q(f"SELECT p.id, p.title, p.format, p.status, p.stage, p.updated_at, p.test_mode, p.parent_id, "
                 f"c.name AS channel_name FROM productions p LEFT JOIN channels c ON c.id=p.channel_id {w} "
                 f"ORDER BY p.id DESC LIMIT 10", a)
    ideas = db.q(f"SELECT id, title, format, score FROM topics WHERE status IN ('idea','approved') "
                 f"{'AND channel_id=?' if cid else 'AND workspace_id=? AND channel_id IS NOT NULL'} "
                 f"ORDER BY saved DESC, score DESC LIMIT 6", a)
    chans = chmod.all_channels(ws)
    for c in chans:
        c["counts"] = chmod.counts(c["id"])
        c["connected"] = oauth.connected(c["id"])
        sched = db.q1("SELECT next_run_at, enabled FROM schedules WHERE channel_id=? AND task_type IN "
                      "('channel_daily','nifty_daily_episode') AND enabled=1 ORDER BY next_run_at LIMIT 1", (c["id"],))
        c["next_run"] = sched["next_run_at"] if sched else None
    ch = chmod.get(cid)
    resp = page(request, "dashboard.html", productions=prods, ideas=ideas, channels=chans, ch=ch,
                totals=chmod.counts(cid, ws), providers=providers.status_all(),
                schedules=db.q("SELECT s.* FROM schedules s JOIN channels c ON c.id=s.channel_id WHERE s.enabled=1 "
                               "AND c.workspace_id=? ORDER BY s.next_run_at", (ws,)),
                uploads_recent=db.q("SELECT * FROM media WHERE workspace_id=? AND status='ready' ORDER BY id DESC "
                                    "LIMIT 5", (ws,)),
                unfinished=db.q("SELECT id, title, status, kind FROM productions WHERE workspace_id=? AND parent_id IS "
                                "NULL AND status IN ('failed','needs_attention','paused','queued') ORDER BY id DESC "
                                "LIMIT 5", (ws,)))
    return _with_cookie(resp, cid) if request.query_params.get("channel") is not None else resp


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    return page(request, "setup.html", sys=sysinfo.summary(), questions=profile.QUESTIONS, answers=profile.answers(),
                secrets=settings.secret_status())


@app.get("/niches", response_class=HTMLResponse)
def niches_page(request: Request):
    rows = db.q("SELECT * FROM niches ORDER BY CASE status WHEN 'approved' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END, id DESC")
    for r in rows:
        r["assessment"] = db.loads(r["assessment"], {}) or {}
        r["metrics"] = db.loads(r["metrics"], {}) or {}
        r["queries"] = db.loads(r["search_queries"], [])
        r["evidence"] = engine.load_evidence("niche_id=?", (r["id"],))[:40]
    running = db.q1("SELECT id FROM tasks WHERE type='niche_research' AND status IN ('pending','running')")
    return page(request, "niches.html", niches=rows, running=bool(running), answers=profile.answers())


IDEA_STATUS = {"open": "t.status IN ('idea','approved')", "saved": "t.saved=1 AND t.status NOT IN ('rejected')",
               "approved": "t.status='approved'", "used": "t.status IN ('producing','in_production','produced')",
               "rejected": "t.status='rejected'", "all": "1=1"}
IDEA_SORT = {"score": "t.score DESC, t.id DESC", "new": "t.id DESC", "old": "t.id ASC", "title": "t.title COLLATE NOCASE"}
PAGE_SIZE = 20


@app.get("/ideas", response_class=HTMLResponse)
def ideas_page(request: Request, channel: str = "", niche: str = "", format: str = "", language: str = "",
               style: str = "", kind: str = "", fresh: str = "", status: str = "open", sort: str = "score",
               q: str = "", page_no: int = 1):
    from .. import channels as chmod
    cid = _channel_arg(request, channel)
    where, args = [IDEA_STATUS.get(status, IDEA_STATUS["open"]), "t.workspace_id=?"], [ws_id(request)]
    if cid:
        where.append("t.channel_id=?"); args.append(cid)
    if niche:
        where.append("c.niche=?"); args.append(niche)
    if format in ("long", "short"):
        where.append("t.format=?"); args.append(format)
    if language:
        where.append("COALESCE(t.language, c.language)=?"); args.append(language)
    if style:
        where.append("t.style=?"); args.append(style)
    if kind:
        where.append("t.idea_kind=?"); args.append(kind)
    if fresh in ("1", "7", "30", "90"):
        where.append("t.created_at>=?"); args.append(db.iso_in(-int(fresh) * 86400))
    if q.strip():
        where.append("(t.title LIKE ? OR t.angle LIKE ? OR t.summary LIKE ?)"); args += [f"%{q.strip()}%"] * 3
    sql = f"FROM topics t LEFT JOIN channels c ON c.id=t.channel_id WHERE {' AND '.join(where)}"
    total = db.q1(f"SELECT COUNT(*) AS n {sql}", tuple(args))["n"]
    pages = max(1, -(-total // PAGE_SIZE))
    page_no = max(1, min(page_no, pages))
    rows = db.q(f"SELECT t.*, c.name AS channel_name, c.niche AS channel_niche, c.language AS channel_language {sql} "
                f"ORDER BY {IDEA_SORT.get(sort, IDEA_SORT['score'])} LIMIT ? OFFSET ?",
                tuple(args) + (PAGE_SIZE, (page_no - 1) * PAGE_SIZE))
    for r in rows:
        r["breakdown"] = db.loads(r["score_breakdown"], {}) or {}
        r["claims"] = db.loads(r["claims"], []) or []
        r["gap"] = db.loads(r["gap"], {}) or {}
        ids = db.loads(r["evidence_ids"], []) or []
        r["evidence"] = engine.load_evidence(f"id IN ({','.join('?' for _ in ids)})", tuple(ids)) if ids else []
    all_ch = chmod.all_channels(ws_id(request))
    running = [t for t in db.q("SELECT id, payload FROM tasks WHERE type='channel_ideas' AND status IN ('pending','running')")
               if (db.loads(t["payload"], {}) or {}).get("channel_id") in {c["id"] for c in all_ch}]
    filters = {"channel": str(cid or ""), "niche": niche, "format": format, "language": language, "style": style,
               "kind": kind, "fresh": fresh, "status": status, "sort": sort, "q": q}
    from urllib.parse import urlencode
    qs = urlencode({k: v for k, v in filters.items() if v})
    resp = page(request, "ideas.html", topics=rows, running=bool(running), channels=all_ch, ch=chmod.get(cid),
                filters=filters, total=total, pages=pages, page_no=page_no, qs=qs,
                niches=sorted({c["niche"] for c in all_ch if c["niche"]}),
                languages=sorted({r["l"] for r in db.q("SELECT DISTINCT COALESCE(t.language, c.language) AS l FROM topics t "
                                                       "LEFT JOIN channels c ON c.id=t.channel_id WHERE t.workspace_id=?",
                                                       (ws_id(request),)) if r["l"]}),
                styles=chmod.STYLES)
    return _with_cookie(resp, cid) if channel != "" else resp


@app.get("/ideas/{tid}", response_class=HTMLResponse)
def idea_page(request: Request, tid: int):
    from .. import channels as chmod, jobs
    t = db.q1("SELECT * FROM topics WHERE id=?", (tid,))
    if not t:
        raise HTTPException(404)
    t["breakdown"] = db.loads(t["score_breakdown"], {}) or {}
    t["claims"] = db.loads(t["claims"], []) or []
    t["gap"] = db.loads(t["gap"], {}) or {}
    ids = db.loads(t["evidence_ids"], []) or []
    t["evidence"] = engine.load_evidence(f"id IN ({','.join('?' for _ in ids)})", tuple(ids)) if ids else []
    ch = chmod.get(t.get("channel_id"))
    plan = jobs.plan_for(ch["id"], "", t) if ch else None
    made = db.q("SELECT id, title, format, status, kind, youtube_video_id FROM productions WHERE topic_id=? ORDER BY id DESC",
                (tid,))
    return page(request, "idea.html", t=t, ch=ch, plan=plan, made=made)


LIB_STATUS = {
    "active": "p.status NOT IN ('awaiting_approval','uploaded','approved','failed','needs_attention','needs_review',"
              "'rejected','test_passed','paused')",
    "review": "p.status IN ('awaiting_approval','needs_review')", "published": "p.status='uploaded'",
    "approved": "p.status IN ('approved','uploading')", "failed": "p.status IN ('failed','needs_attention','paused_budget')",
    "paused": "p.status='paused'", "rejected": "p.status='rejected'", "all": "1=1"}


@app.get("/productions", response_class=HTMLResponse)
def productions_page(request: Request, channel: str = "", status: str = "all", format: str = "", kind: str = "",
                     q: str = "", page_no: int = 1):
    from .. import channels as chmod
    cid = _channel_arg(request, channel)
    where, args = [LIB_STATUS.get(status, "1=1"), "p.workspace_id=?"], [ws_id(request)]
    if cid:
        where.append("p.channel_id=?"); args.append(cid)
    if format in ("long", "short"):
        where.append("p.format=?"); args.append(format)
    if kind == "episodes":
        where.append("p.parent_id IS NULL")
    elif kind == "shorts_of":
        where.append("p.parent_id IS NOT NULL")
    if q.strip():
        where.append("(p.title LIKE ? OR CAST(p.id AS TEXT)=?)"); args += [f"%{q.strip()}%", q.strip()]
    sql = f"FROM productions p LEFT JOIN channels c ON c.id=p.channel_id WHERE {' AND '.join(where)}"
    total = db.q1(f"SELECT COUNT(*) AS n {sql}", tuple(args))["n"]
    pages = max(1, -(-total // 30))
    page_no = max(1, min(page_no, pages))
    rows = db.q(f"SELECT p.*, c.name AS channel_name {sql} ORDER BY p.id DESC LIMIT 30 OFFSET ?",
                tuple(args) + ((page_no - 1) * 30,))
    for r in rows:
        r["qa"] = db.loads(r["qa"], {}) or {}
        r["tl"] = db.loads(r["timeline"], {}) or {}
        r["has_video"] = bool(r["video_path"]) and Path(r["video_path"]).is_file()
        r["has_thumb"] = bool(r["thumbnail_path"]) and Path(r["thumbnail_path"]).is_file()
    from urllib.parse import urlencode
    filters = {"channel": str(cid or ""), "status": status, "format": format, "kind": kind, "q": q}
    resp = page(request, "productions.html", productions=rows, formats=profile.formats(),
                channels=chmod.all_channels(ws_id(request)),
                ch=chmod.get(cid), filters=filters, total=total, pages=pages, page_no=page_no,
                qs=urlencode({k: v for k, v in filters.items() if v}))
    return _with_cookie(resp, cid) if channel != "" else resp


@app.get("/productions/{pid}", response_class=HTMLResponse)
def production_page(request: Request, pid: int):
    p = pipeline.load(pid)
    topic = db.q1("SELECT * FROM topics WHERE id=?", (p["topic_id"],)) if p["topic_id"] else None
    ev = pipeline._evidence(p)
    events = db.q("SELECT * FROM logs WHERE production_id=? ORDER BY id DESC LIMIT 60", (pid,))
    tasks = db.q("SELECT * FROM tasks WHERE production_id=? ORDER BY id DESC LIMIT 10", (pid,))
    ev_by_id = {e["id"]: e for e in ev}
    stages, _, _ = pipeline.stages_for(p.get("kind") or "general")
    fin_audit_entries = []
    if p.get("kind") in pipeline.FINANCE_KINDS:
        from ..finance import audit as fin_audit
        fin_audit_entries = fin_audit.entries(production_id=pid, limit=60)
    from .. import channels as chmod
    ws = ws_id(request)
    chans = chmod.all_channels(ws)
    for c in chans:
        c["connected"] = oauth.connected(c["id"])
    files = outputs_of(p)
    info = None
    if p.get("video_path") and Path(p["video_path"]).is_file():
        try:
            from ..uploads import probe
            info = {**probe(Path(p["video_path"])), "size_bytes": Path(p["video_path"]).stat().st_size}
        except Exception:
            info = None
    if p.get("video_path") and not info and p["status"] in ("awaiting_approval", "approved"):
        p["status"] = "needs_attention"               # shown only: never call a missing file ready
        p["error"] = "The rendered video file is missing. Retry the job to render it again."
    sources = [m for m in (uploads_mod().get(i, ws) for i in db.loads(p.get("source_media"), []) or []) if m]
    job_outputs = db.q("SELECT id, title, status, format, video_path, youtube_video_id FROM productions WHERE parent_id=? "
                       "AND kind='edit_clip' ORDER BY id", (pid,)) if p.get("kind") == "edit" else []
    return page(request, "production.html", p=p, pkg=p["package"], tl=p["timeline"], topic=topic, evidence=ev,
                ev_by_id=ev_by_id, assets=pipeline.assets_for(pid), events=events, tasks=tasks,
                files=files, info=info, sources=sources, job_outputs=job_outputs, my_channels=chans,
                stages=stages, connected=oauth.connected(p["channel_id"]) if p.get("channel_id") else False,
                audit=settings.get("youtube.audit_passed"),
                active=p["status"] not in pipeline.TERMINAL, gate=p.get("compliance") or {},
                facts=(p.get("episode") or {}).get("facts") or {}, fin_audit=fin_audit_entries,
                shorts=db.q("SELECT id, title, status, privacy, youtube_video_id, video_path, compliance, "
                            "json_extract(episode,'$.segment') AS segment FROM productions WHERE parent_id=? ORDER BY id", (pid,)),
                parent=db.q1("SELECT id, title, status, youtube_video_id FROM productions WHERE id=?", (p["parent_id"],))
                if p.get("parent_id") else None, s_shorts_max=settings.get("shorts.per_episode", 4))


@app.get("/queue", response_class=HTMLResponse)
def queue_page(request: Request, level: str = "all"):
    ws, admin = ws_id(request), int(is_admin(request))
    tasks = db.q("SELECT t.* FROM tasks t LEFT JOIN productions p ON p.id=t.production_id "
                 "WHERE p.workspace_id=? OR (? AND t.production_id IS NULL) "
                 "ORDER BY CASE t.status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 WHEN 'paused' THEN 2 "
                 "WHEN 'failed' THEN 3 ELSE 4 END, t.id DESC LIMIT 150", (ws, admin))
    where = "" if level == "all" else "AND l.level IN ('warning','error')"
    events = db.q(f"SELECT l.* FROM logs l LEFT JOIN productions p ON p.id=l.production_id "
                  f"WHERE (p.workspace_id=? OR (? AND l.production_id IS NULL)) {where} ORDER BY l.id DESC LIMIT 300",
                  (ws, admin))
    return page(request, "queue.html", tasks=tasks, events=events, level=level)


@app.get("/schedule", response_class=HTMLResponse)
def schedule_page(request: Request):
    rows = db.q("SELECT s.* FROM schedules s LEFT JOIN channels c ON c.id=s.channel_id "
                "WHERE c.workspace_id=? OR (? AND s.channel_id IS NULL) ORDER BY s.id",
                (ws_id(request), int(is_admin(request))))
    return page(request, "schedule.html", schedules=rows,
                cadence=profile.cadence(), slots=settings.get("publish.slots"))


@app.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request):
    rows = analytics.latest()
    usage = router.spend_by_provider()
    daily = db.q("SELECT substr(created_at,1,10) AS day, ROUND(SUM(cost_usd),4) AS cost, COUNT(*) AS calls "
                 "FROM ai_usage GROUP BY day ORDER BY day DESC LIMIT 30")
    yt_units = db.q("SELECT day, SUM(units) AS units FROM api_quota GROUP BY day ORDER BY day DESC LIMIT 14")
    return page(request, "analytics.html", videos=rows, usage=usage, daily=daily, yt_units=yt_units,
                connected=oauth.connected())


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, tab: str = "ai"):
    voice = tts.current_voice()
    lang = profile.tts_language()
    all_voices = tts.all_voices() if tab == "voice" else []
    main_voices = [v for v in all_voices if v["lang"].startswith(lang) or v["id"] == voice.get("voice_id")]
    other_voices = [v for v in all_voices if v not in main_voices]
    return page(request, "settings.html", tab=tab, s=settings.all_settings(), secrets=settings.secret_status(),
                providers=providers.status_all() if tab == "ai" else [], provider_names=settings.PROVIDERS,
                ai_tasks=settings.AI_TASKS, voices=main_voices, other_voices=other_voices, channel_lang=profile.language(),
                voice=voice, kokoro_installed=tts.kokoro_installed(), voice_hi=tts.current_voice("Hindi"),
                voice_en=tts.current_voice("English"), audience_languages=settings.get("channel.audience_languages"),
                yt_configured=oauth.configured(), yt_connected=oauth.connected(), redirect_uri=oauth.REDIRECT_URI,
                feeds=db.q("SELECT * FROM feeds ORDER BY id"), music=sorted(p.name for p in config.MUSIC_DIR.glob("*")
                                                                            if p.suffix.lower() in (".mp3", ".wav", ".m4a", ".aac", ".flac")),
                ollama=sysinfo.ollama_local(settings.get("ai.ollama_host")), hw=sysinfo.hardware(),
                disk=sysinfo.disk_free_gb())


@app.get("/media/{path:path}")
def media(path: str):
    target = (config.DATA_DIR / path).resolve()
    allowed = [config.PRODUCTIONS_DIR, config.SAMPLES_DIR, config.ASSET_CACHE_DIR, config.BRAND_DIR,
               config.DATA_DIR / "workspaces", config.DATA_DIR / "channels"]
    if not any(target.is_relative_to(a.resolve()) for a in allowed) or not target.is_file():
        raise HTTPException(404)
    mt = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=mt)


@app.get("/oauth/google/callback", response_class=HTMLResponse)
def oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return page(request, "message.html", title="YouTube not connected", message=f"Google returned: {error}")
    try:
        ch = oauth.handle_callback(code, state)
    except oauth.AuthError as e:
        return page(request, "message.html", title="YouTube not connected", message=str(e), link="/channels")
    return page(request, "message.html", title="YouTube connected",
                message=f"Connected to channel “{ch.get('title')}”. Uploads for this studio channel now go there.",
                link=f"/channels/{ch.get('studio_channel_id')}?tab=youtube")


# ------------------------------------------------------------------ compliance (finance content)

@app.get("/compliance", response_class=HTMLResponse)
def compliance_page(request: Request, dataset: int | None = None):
    from ..finance import analysis as fin_analysis, audit as fin_audit, marketdata, rules as fin_rules
    datasets = marketdata.all_datasets()
    chosen = next((d for d in datasets if d["id"] == dataset), datasets[0] if datasets else None)
    dates = [b["trade_date"] for b in marketdata.daily_bars(chosen["id"])] if chosen and chosen["timeframe"] == "1d" else []
    episodes = db.q("SELECT id, kind, parent_id, privacy, title, status, compliance, created_at FROM productions "
                    "WHERE kind IN ('nifty_pivot','fib_lesson','nifty_short') ORDER BY id DESC LIMIT 40")
    for e in episodes:
        e["compliance"] = db.loads(e["compliance"], {}) or {}
    from ..finance import lessons as fin_lessons
    made = {}
    for r in db.q("SELECT id, status, audience, CAST(json_extract(episode, '$.facts.lesson_no') AS INTEGER) AS n "
                  "FROM productions WHERE kind='fib_lesson' ORDER BY id"):
        made.setdefault(r["n"], []).append(r)
    lesson_sched = db.q1("SELECT * FROM schedules WHERE task_type='fib_lesson_daily'")
    from ..finance import calendar as fin_cal
    now = marketdata.ist_now()
    session = fin_cal.session_status(now.date(), after_close=(now.hour, now.minute) >= marketdata.SESSION_CLOSE)
    freshness = []
    for tf in ("1d", "15m", "1h"):
        d = next((x for x in datasets if x["timeframe"] == tf and x["status"] != "invalid"), None)
        freshness.append({"tf": tf, "ds": d, "covers": bool(d and d["last_date"] and d["last_date"][:10] >= session["basis_session"])})
    upcoming = [(d.isoformat(), n) for d, n in sorted(fin_cal.HOLIDAYS.items()) if d >= now.date()][:4]
    daily_sched = db.q1("SELECT * FROM schedules WHERE task_type='nifty_daily_episode'")
    return page(request, "compliance.html", lessons=fin_lessons.LESSONS, lessons_made=made, lesson_sched=lesson_sched,
                session=session, freshness=freshness, upcoming=upcoming, daily_sched=daily_sched,
                audiences=profile.AUDIENCES, rules=fin_rules.all_rules(), sources=db.q("SELECT * FROM data_sources ORDER BY id"),
                datasets=datasets, chosen=chosen, dates=dates, modes=fin_analysis.MODES, episodes=episodes,
                audit_entries=fin_audit.entries(limit=80), chain=fin_audit.verify_chain(),
                lag_days=marketdata.EDUCATION_LAG_DAYS, s=settings.all_settings(),
                lag_cutoff=marketdata.add_days(marketdata.ist_now().date().isoformat(), -marketdata.EDUCATION_LAG_DAYS))


@app.post("/api/compliance/review")
async def api_compliance_review(request: Request):
    from ..finance import rules as fin_rules
    data = await body(request)
    try:
        fin_rules.add_review(str(data.get("rule_id")), str(data.get("reviewer", "")), str(data.get("decision", "")),
                             qualification=str(data.get("qualification", "")), conditions=str(data.get("conditions", "")),
                             notes=str(data.get("notes", "")), evidence_url=str(data.get("evidence_url", "")),
                             reviewed_on=str(data.get("reviewed_on", "")))
    except ValueError as e:
        return bad(str(e))
    return ok()


@app.post("/api/compliance/override")
async def api_owner_override(request: Request):
    """Record (or revoke) the owner's acceptance of open review-level risks. Critical checks are never overridden."""
    from ..finance import audit as fin_audit
    data = await body(request)
    if data.get("action") == "revoke":
        settings.set("compliance.owner_override", None)
        settings.set("compliance.unattended_publishing", False)
        fin_audit.record("owner_override_revoked", {}, actor="user")
        return {"ok": True, "message": "Override revoked; unattended public publishing is off"}
    statement = str(data.get("statement", "")).strip()
    if len(statement) < 10:
        return bad("Write a short statement of the risk you accept")
    override = {"active": True, "accepted_at": db.iso(), "accepted_by": "channel owner", "statement": statement[:500]}
    settings.set("compliance.owner_override", override)
    fin_audit.record("owner_override_accepted", override, actor="user")
    return {"ok": True, "message": "Override recorded in the audit log"}


@app.post("/api/compliance/recheck")
async def api_compliance_recheck(request: Request):
    from ..finance import rules as fin_rules
    data = await body(request)
    if not str(data.get("notes", "")).strip():
        return bad("Describe what you checked (source, date, any change).")
    fin_rules.mark_rechecked(str(data.get("rule_id")), str(data["notes"]), str(data.get("actor") or "user"))
    return ok()


@app.post("/api/datasources/{source_id}/licence")
async def api_licence(request: Request, source_id: str):
    from ..finance import marketdata
    data = await body(request)
    try:
        marketdata.record_licence(source_id, reference=str(data.get("reference", "")), holder=str(data.get("holder", "")),
                                  expiry=str(data.get("expiry", "")), evidence=str(data.get("evidence", "")),
                                  permits_public_video=bool(data.get("permits_public_video")),
                                  permits_monetization=bool(data.get("permits_monetization")),
                                  actor=str(data.get("holder") or "user"))
    except marketdata.DataError as e:
        return bad(str(e))
    return ok()


@app.post("/api/datasets/import")
async def api_dataset_import(request: Request):
    from ..finance import marketdata
    data = await body(request)
    raw = str(data.get("data", ""))
    try:
        text = base64.b64decode(raw.split(",", 1)[-1]).decode("utf-8-sig") if raw.startswith("data:") else raw
    except (ValueError, UnicodeDecodeError):
        return bad("Could not read the file as UTF-8 CSV.")
    if len(text) > 20_000_000:
        return bad("File too large.")
    try:
        res = marketdata.import_file(None, text=text, original_name=Path(str(data.get("name", "import.csv"))).name,
                                     source_id=str(data.get("source_id")), timeframe=str(data.get("timeframe", "1d")),
                                     data_nature=str(data.get("data_nature", "end_of_day")),
                                     retrieved_at_local=str(data.get("retrieved_at_local", "")),
                                     retrieved_by=str(data.get("retrieved_by", "")), notes=str(data.get("notes", "")))
    except marketdata.DataError as e:
        return bad(str(e))
    return ok(next=f"/compliance?dataset={res['dataset_id']}", detail=f"Imported: {res['status']} ({res['rows']} rows)")


@app.post("/api/lessons/next")
async def api_lesson_next(request: Request):
    from ..finance import lessons
    data = await body(request)
    audience = data.get("audience") if data.get("audience") in profile.AUDIENCES else "india"
    try:
        pid = lessons.create(audience=audience, fmt="short" if data.get("format") == "short" else "long", actor="user")
    except ValueError as e:
        return bad(str(e))
    return ok(next=f"/productions/{pid}")


@app.post("/api/episodes")
async def api_episode_create(request: Request):
    from ..finance import episode, marketdata
    data = await body(request)
    try:
        pid = episode.create(int(data.get("dataset_id")), str(data.get("basis_date")), str(data.get("mode")),
                             "short" if data.get("format") == "short" else "long",
                             audience=data.get("audience") if data.get("audience") in profile.AUDIENCES else "india")
    except (marketdata.DataError, ValueError, TypeError) as e:
        return bad(str(e))
    return ok(next=f"/productions/{pid}")


# ------------------------------------------------------------------ API: status

@app.get("/api/status")
def api_status(request: Request):
    ws, admin = ws_id(request), int(is_admin(request))
    running = db.q("SELECT t.id, t.type, t.production_id, t.locked_at, p.title, p.status AS pstatus FROM tasks t "
                   "LEFT JOIN productions p ON p.id=t.production_id WHERE t.status='running' AND "
                   "(p.workspace_id=? OR (? AND t.production_id IS NULL))", (ws, admin))
    pending = db.q1("SELECT COUNT(*) AS n FROM tasks t LEFT JOIN productions p ON p.id=t.production_id "
                    "WHERE t.status='pending' AND (p.workspace_id=? OR (? AND t.production_id IS NULL))",
                    (ws, admin))["n"]
    events = db.q("SELECT l.level, l.source, l.message, l.created_at, l.production_id FROM logs l "
                  "LEFT JOIN productions p ON p.id=l.production_id WHERE p.workspace_id=? OR "
                  "(? AND l.production_id IS NULL) ORDER BY l.id DESC LIMIT 15", (ws, admin))
    for e in events:
        e["when"] = _fmt_dt(e["created_at"])
    spend = router.month_spend()
    return {"running": running, "pending": pending, "events": events, "spend": round(spend, 4),
            "budget": settings.get("ai.monthly_budget_usd"),
            "awaiting": db.q1("SELECT COUNT(*) AS n FROM productions WHERE status='awaiting_approval' AND "
                              "workspace_id=?", (ws,))["n"]}


@app.get("/api/productions/{pid}/status")
def api_production_status(pid: int):
    p = db.q1("SELECT status, stage, error, updated_at FROM productions WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404)
    return p


# ------------------------------------------------------------------ API: interview

@app.post("/api/interview/answer")
async def api_answer(request: Request):
    data = await body(request)
    keys = {q["key"] for q in profile.QUESTIONS} | {q["followup"]["key"] for q in profile.QUESTIONS if q.get("followup")} \
        | {"audience_age"}
    answers = profile.answers()
    for k, v in (data.get("answers") or {}).items():
        if k in keys:
            answers[k] = v
    settings.set("interview", answers)
    return ok(answers=answers)


VOICE_DEFAULTS = {"Hindi": ("kokoro", "hf_alpha"), "Hinglish": ("kokoro", "hf_alpha"), "English": ("kokoro", "am_michael")}


@app.post("/api/interview/complete")
async def api_complete(request: Request):
    a = profile.answers()
    missing = [q["key"] for q in profile.QUESTIONS if q["key"] not in a]
    if missing:
        return bad(f"Please answer: {', '.join(missing)}")
    settings.set("publish.mode", a.get("publish_mode", "approve_first"))
    settings.set("youtube.made_for_kids", a.get("audience_age") == "kids")
    styles = a.get("styles", [])
    settings.set("youtube.category_id", "23" if "funny" in styles else "27" if {"educational", "tutorials", "documentary"} & set(styles) else "22")
    lang = a.get("language_other") if a.get("language") == "Other" else a.get("language", "Hindi")
    settings.merge("channel.audience_languages", {"india": lang or "Hindi"})
    by_lang = settings.get("voice.by_language") or {}
    if lang in VOICE_DEFAULTS and lang not in by_lang:          # never overwrite a voice the user picked
        engine_id, voice_id = VOICE_DEFAULTS[lang]
        settings.merge("voice.by_language", {lang: {"engine": engine_id, "voice_id": voice_id}})
    settings.set("research.region", "IN" if lang in ("Hindi", "Hinglish") else settings.get("research.region"))
    if a.get("niche_mode") == "have_one" and a.get("niche") and not db.q1("SELECT id FROM niches WHERE source='user' AND name=?", (a["niche"][:80],)):
        db.insert("niches", {"name": a["niche"][:80], "description": a["niche"], "status": "approved", "source": "user",
                             "search_queries": db.dumps([a["niche"][:80]]), "created_at": db.iso(), "approved_at": db.iso()})
    settings.set("interview_complete", True)
    logs.event("info", "setup", "First-run interview completed: " + profile.summary_text())
    return ok(next="/niches")


# ------------------------------------------------------------------ API: niches & ideas

@app.post("/api/niches/research")
async def api_niche_research(request: Request):
    data = await body(request)
    text = str(data.get("candidates_raw", ""))
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # A pasted brief or prompt (long, markdown headings, long sentences) is ONE niche, not one niche per line —
    # splitting it turned prompt headings like "## YOUR ROLE" into fake niches.
    if not data.get("candidates") and (len(text) > 400 or any(ln.startswith("#") or len(ln) > 120 for ln in lines)):
        title = next((ln.lstrip("#* ").strip() for ln in lines if ln.lstrip("#* ").strip()), "Custom niche")[:120]
        cands = [title]
    else:
        raw = data.get("candidates") or lines
        cands = [str(c).strip()[:120] for c in raw if str(c).strip()][:6]
    tid = worker.enqueue("niche_research", {"candidates": cands}, dedupe=True, max_attempts=2)
    return ok(task_id=tid)


@app.post("/api/niches/{nid}/{action}")
def api_niche_action(nid: int, action: str):
    if action == "approve":
        db.ex("UPDATE niches SET status='candidate' WHERE status='approved'")
        db.update("niches", nid, {"status": "approved", "approved_at": db.iso()})
        n = db.q1("SELECT name FROM niches WHERE id=?", (nid,))
        logs.event("info", "research", f"Niche approved: {n['name'] if n else nid}")
        worker.enqueue("ideation", {"niche_id": nid, "count": 10}, dedupe=True)
        return ok(next="/ideas")
    if action == "reject":
        db.update("niches", nid, {"status": "rejected"})
        return ok()
    return bad("unknown action")


@app.post("/api/ideas/generate")
async def api_ideas(request: Request):
    data = await body(request)
    niche = db.q1("SELECT id FROM niches WHERE status='approved' ORDER BY approved_at DESC LIMIT 1")
    if not niche:
        return bad("Approve a niche first.")
    fmts = [f for f in ("short", "long") if data.get(f"fmt_{f}")] or \
        [f for f in data.get("formats", profile.formats()) if f in ("short", "long")] or profile.formats()
    tid = worker.enqueue("ideation", {"niche_id": niche["id"], "count": max(3, min(20, int(data.get("count", 10)))),
                                      "formats": fmts}, dedupe=True, max_attempts=2)
    return ok(task_id=tid)


@app.post("/api/channels/{cid}/ideas")
async def api_channel_ideas(request: Request, cid: int):
    from .. import channels as chmod
    ch = chmod.get(cid)
    if not ch:
        return bad("not found", 404)
    data = await body(request)
    fmts = [f for f in ("long", "short") if data.get(f"fmt_{f}")] or chmod.formats(ch)
    tid = worker.enqueue("channel_ideas", {"channel_id": cid, "count": max(3, min(20, int(data.get("count") or 10))),
                                           "formats": fmts, "focus": str(data.get("focus") or "")[:300]},
                         dedupe=True, max_attempts=2)
    return ok(task_id=tid, detail=f"Generating ideas for {ch['name']}…")


@app.post("/api/topics/{tid}/{action}")
async def api_topic(request: Request, tid: int, action: str):
    t = db.q1("SELECT * FROM topics WHERE id=?", (tid,))
    if not t:
        return bad("not found", 404)
    if action in ("video", "long", "short", "both"):
        from .. import jobs
        if not t.get("channel_id"):
            return bad("This idea is not assigned to a channel.")
        try:
            res = jobs.create(t["channel_id"], "", idea_id=tid, fmt=None if action == "video" else action)
        except ValueError as e:
            return bad(str(e))
        pids = res.get("production_ids") or []
        return ok(next=f"/productions/{pids[0]}" if pids else "/queue")
    if action in ("save", "unsave"):
        db.update("topics", tid, {"saved": int(action == "save"), "updated_at": db.iso()})
        return ok(detail="Saved" if action == "save" else "Removed from saved")
    if action == "variations":
        if not t.get("channel_id"):
            return bad("This idea is not assigned to a channel.")
        worker.enqueue("channel_ideas", {"channel_id": t["channel_id"], "count": 4, "formats": [t["format"]],
                                         "focus": f"variations of: {t['title']} — {(t.get('summary') or t.get('angle') or '')[:200]}"},
                       max_attempts=2)
        return ok(detail="Generating 4 variations…")
    if action == "restore":
        db.update("topics", tid, {"status": "idea", "updated_at": db.iso()})
        return ok()
    if action in ("approve", "reject"):
        db.update("topics", tid, {"status": "approved" if action == "approve" else "rejected", "updated_at": db.iso()})
        return ok()
    if action in ("produce", "produce_international"):
        pid = pipeline.create(tid, audience="international" if action == "produce_international" else "india")
        return ok(next=f"/productions/{pid}")
    return bad("unknown action")


# ------------------------------------------------------------------ API: productions

@app.post("/api/productions/selftest")
async def api_selftest(request: Request):
    data = await body(request)
    fmt = data.get("format") if data.get("format") in ("short", "long") else "short"
    pid = pipeline.create(fmt=fmt, sample=True,
                          audience=data.get("audience") if data.get("audience") in profile.AUDIENCES else "india")
    return ok(next=f"/productions/{pid}")


@app.post("/api/productions/{pid}/approve")
async def api_approve(request: Request, pid: int):
    data = await body(request)
    return _approve(pid, data.get("privacy", "private"), data.get("publish_at_local"))


def _approve(pid: int, privacy: str, publish_at_local: Any) -> JSONResponse:
    if privacy not in ("private", "unlisted", "public", "scheduled"):
        return bad("invalid privacy")
    publish_at = None
    if privacy == "scheduled":
        try:
            local = datetime.fromisoformat(str(publish_at_local))
            publish_at = db.iso(local.astimezone())
        except (TypeError, ValueError):
            return bad("Pick a valid date and time for scheduled publishing.")
        if publish_at <= db.iso_in(15 * 60):
            return bad("Scheduled time must be at least 15 minutes in the future.")
    if privacy in ("public", "scheduled", "unlisted") and not settings.get("youtube.audit_passed"):
        return bad("Your Google Cloud project must pass the YouTube API audit before public/scheduled/unlisted uploads "
                   "(otherwise YouTube locks them private). Choose 'private' for now, or update Settings › YouTube.")
    try:
        pipeline.approve(pid, privacy=privacy, publish_at=publish_at)
    except pipeline.NeedsAttention as e:
        return bad(str(e))
    return ok()


@app.post("/api/productions/{pid}/reject")
def api_reject(pid: int):
    p = db.q1("SELECT topic_id FROM productions WHERE id=?", (pid,))
    pipeline.save(pid, status="rejected")
    if p and p["topic_id"]:
        db.update("topics", p["topic_id"], {"status": "idea", "updated_at": db.iso()})
    return ok()


@app.post("/api/productions/{pid}/shorts")
async def api_make_shorts(request: Request, pid: int):
    from ..finance import shorts as fin_shorts
    await body(request)
    try:
        made = fin_shorts.create_for(pid, actor="user")
    except ValueError as e:
        return bad(str(e))
    return {"ok": True, "created": made,
            "message": f"{len(made)} Short(s) queued" if made else "No new Shorts: every eligible segment already has one, "
                                                                     "or no self-contained verified segment fits 60 seconds"}


@app.get("/api/productions/{pid}/shorts/manifest")
def api_shorts_manifest(pid: int):
    from ..finance import shorts as fin_shorts
    fin_shorts.write_manifest(pid)
    return fin_shorts.manifest(pid)


@app.post("/api/productions/{pid}/retry")
async def api_retry(request: Request, pid: int):
    data = await body(request)
    stage = data.get("stage")
    p = pipeline.load(pid)
    if p["status"] in ("uploading",):
        return bad("Upload in progress.")
    if p["status"] in ("approved",) and not stage:
        worker.enqueue("upload", {"production_id": pid}, production_id=pid, max_attempts=8, dedupe=True)
        return ok()
    pipeline.reset_to_stage(pid, stage if stage in pipeline.STAGES else (p["stage"] or "research"))
    return ok()


@app.post("/api/productions/{pid}/metadata")
async def api_metadata(request: Request, pid: int):
    data = await body(request)
    p = pipeline.load(pid)
    if p["status"] in ("uploading", "uploaded"):
        return bad("Already uploaded — edit metadata in YouTube Studio.")
    pkg = p["package"]
    title = str(data.get("title", pkg["title"])).replace("<", "").replace(">", "").strip()[:100]
    if len(title) < 5:
        return bad("Title is too short.")
    tags = [t.strip().replace("<", "").replace(">", "")[:30] for t in str(data.get("tags", "")).split(",") if t.strip()]
    desc = str(data.get("final_description", pkg.get("final_description", ""))).replace("<", "").replace(">", "")
    if len(desc.encode()) > 5000:
        return bad("Description exceeds YouTube's 5000-byte limit.")
    pkg.update({"title": title, "tags": tags[:20], "final_description": desc})
    pipeline.save(pid, package=pkg, title=title)
    return ok()


# ------------------------------------------------------------------ API: queue & schedules

@app.post("/api/tasks/{tid}/{action}")
def api_task(tid: int, action: str):
    if action == "retry":
        worker.retry(tid)
    elif action == "cancel":
        worker.cancel(tid)
    else:
        return bad("unknown action")
    return ok()


@app.post("/api/schedules/{sid}")
async def api_schedule(request: Request, sid: int):
    from .. import scheduler
    data = await body(request)
    s = db.q1("SELECT s.*, c.workspace_id AS ws FROM schedules s LEFT JOIN channels c ON c.id=s.channel_id "
              "WHERE s.id=?", (sid,))
    if not s or (s["ws"] != ws_id(request) and not (s["channel_id"] is None and is_admin(request))):
        return bad("not found", 404)
    s.pop("ws", None)
    if data.get("run_now"):
        worker.enqueue(s["task_type"], db.loads(s["payload"], {}), schedule_id=sid, dedupe=True)
        return ok()
    fields: dict[str, Any] = {}
    if "enabled" in data:
        fields["enabled"] = 1 if data["enabled"] else 0
    if s["kind"] == "daily" and data.get("time_of_day"):
        hh, _, mm = str(data["time_of_day"]).partition(":")
        if not (hh.isdigit() and mm.isdigit() and int(hh) < 24 and int(mm) < 60):
            return bad("time must be HH:MM")
        fields["time_of_day"] = f"{int(hh):02d}:{int(mm):02d}"
    if s["kind"] == "interval" and data.get("interval_minutes"):
        fields["interval_minutes"] = max(15, int(data["interval_minutes"]))
    s.update(fields)
    fields["next_run_at"] = scheduler.next_run(s)
    db.update("schedules", sid, fields)
    return ok()


# ------------------------------------------------------------------ API: settings & secrets

SETTABLE = {
    "ai.default_provider", "ai.fallback_provider", "ai.task_providers", "ai.models", "ai.anthropic_effort",
    "ai.monthly_budget_usd", "ai.on_budget_exceeded", "ai.prices", "ai.gemini_tier", "ai.ollama_host", "ai.ollama_num_ctx",
    "research.sources", "research.region", "research.max_page_fetch", "research.youtube_daily_unit_budget",
    "research.contact",
    "visuals.order", "visuals.enabled", "visuals.prefer_video", "visuals.local_sd_url", "visuals.gemini_image_model",
    "visuals.gemini_image_price", "brand", "render.preset", "render.crf", "render.transition", "render.music_file",
    "render.music_license", "render.music_volume", "youtube.privacy_default", "youtube.category_id",
    "youtube.upload_captions", "youtube.made_for_kids", "youtube.audit_passed", "youtube.disclose_tts", "publish.mode",
    "publish.slots", "production.max_active", "production.short_seconds", "production.long_minutes",
    "production.min_topic_score", "production.auto_pick_topics",
    "compliance.public_publishing_enabled", "compliance.unattended_publishing", "compliance.finance_lint_all",
    "shorts.enabled", "shorts.per_episode", "nifty.daily_episode", "nifty.daily_audience", "nifty.require_intraday",
    "compliance.auto_private_upload",
}


@app.post("/api/settings")
async def api_settings(request: Request):
    data = await body(request)
    changed = []
    for k, v in data.items():
        if k not in SETTABLE:
            return bad(f"setting {k!r} cannot be changed here")
        if k == "ai.monthly_budget_usd":
            v = max(0.0, float(v))
        if k in ("ai.default_provider", "ai.fallback_provider") and v not in settings.PROVIDERS:
            return bad("unknown provider")
        if k == "shorts.per_episode":
            try:
                v = int(v)
            except (TypeError, ValueError):
                return bad("Shorts per episode must be a number from 0 to 6")
            if not 0 <= v <= 6:
                return bad("Shorts per episode must be a number from 0 to 6")
        if k == "nifty.daily_audience" and v not in profile.AUDIENCES:
            return bad("unknown audience")
        if k == "publish.mode" and v not in ("approve_first", "auto_after_validation"):
            return bad("invalid publish mode")
        if k == "publish.slots":
            if not isinstance(v, dict):
                return bad("publishing times must be lists of HH:MM")
            for fmt_key, times in v.items():
                if fmt_key not in ("short", "long") or not isinstance(times, list) or not times or \
                        not all(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(t)) for t in times):
                    return bad(f"{'Shorts' if fmt_key == 'short' else 'Long-form'} times must be HH:MM, e.g. 18:30, 20:00")
        if k == "compliance.unattended_publishing" and v and not (data.get("compliance.public_publishing_enabled")
                                                                   or settings.get("compliance.public_publishing_enabled")):
            return bad("Enable public publishing (after review) before unattended publishing.")
        if k.startswith("compliance.") and k != "compliance.finance_lint_all":
            from ..finance import audit as fin_audit
            fin_audit.record("compliance_setting_changed", {"setting": k, "value": v}, actor="user")
        if k == "render.music_file" and v:
            p = (config.MUSIC_DIR / Path(str(v)).name)
            if not p.exists():
                return bad("music file not found in data/music")
            v = str(p)
        if isinstance(v, dict) and isinstance(settings.get(k), dict) and k not in ("ai.prices",):
            merged = settings.get(k)
            merged.update(v)
            v = merged
        settings.set(k, v)
        changed.append(k)
    return ok(changed=changed)


@app.post("/api/secrets/{name}")
async def api_secret(request: Request, name: str):
    if name not in settings.SECRETS or name in ("google_refresh_token",):
        return bad("unknown secret")
    data = await body(request)
    value = str(data.get("value", "")).strip()
    if not value:
        settings.delete_secret(name)
        return ok(set=False)
    if len(value) > 500 or any(c.isspace() for c in value):
        return bad("That doesn't look like a valid key.")
    settings.set_secret(name, value)
    logs.event("info", "settings", f"Credential saved to Keychain: {settings.SECRETS[name]['label']}")
    return ok(set=True)


@app.post("/api/providers/{pid}/test")
def api_provider_test(pid: str):
    from pydantic import BaseModel

    class Ping(BaseModel):
        reply: str

    if pid not in settings.PROVIDERS:
        return bad("unknown provider")
    prov = providers.get(pid)
    available, why = prov.available()
    if not available:
        return bad(why)
    from ..ai.schema import provider_schema
    try:
        res = prov.complete_json("Reply with JSON only.", 'Return {"reply": "ok"}.', provider_schema(Ping), 400)
        router._record("connection_test", res, pid, prov.model(), True, None, 0)
    except providers.ProviderError as e:
        return bad(str(e))
    return ok(detail=f"{res.model} replied ({res.input_tokens} in / {res.output_tokens} out tokens, ${res.cost_usd:.5f})")


@app.get("/api/providers/{pid}/models")
def api_provider_models(pid: str):
    if pid not in settings.PROVIDERS:
        return bad("unknown provider")
    return ok(models=providers.get(pid).list_models())


@app.post("/api/ollama/check")
async def api_ollama_check(request: Request):
    data = await body(request)
    model = str(data.get("model", "")).strip()
    if not MODEL_NAME.fullmatch(model):
        return bad("Enter a model name like qwen3.5:9b")
    name, _, tag = model.partition(":")
    tag = tag or "latest"
    try:
        man = net.get_json(f"https://registry.ollama.ai/v2/library/{name}/manifests/{tag}",
                           headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"})
    except net.FetchError:
        return bad(f"Model {model!r} was not found in the Ollama library.")
    size = sum(l.get("size", 0) for l in man.get("layers", []))
    return ok(model=f"{name}:{tag}", size_gb=round(size / 1e9, 2), fit=sysinfo.model_fits(size))


@app.post("/api/ollama/pull")
async def api_ollama_pull(request: Request):
    data = await body(request)
    model = str(data.get("model", "")).strip()
    if not MODEL_NAME.fullmatch(model):
        return bad("invalid model name")
    host = settings.get("ai.ollama_host").rstrip("/")
    try:
        httpx.get(host + "/api/tags", timeout=3)
    except httpx.HTTPError:
        return bad("Ollama is not running. Install it (brew install ollama, or the app from ollama.com) and start it.")
    tid = worker.enqueue("ollama_pull", {"model": model}, dedupe=True, max_attempts=2)
    return ok(task_id=tid)


# ------------------------------------------------------------------ API: voice

@app.post("/api/voice/kokoro")
async def api_kokoro(request: Request):
    data = await body(request)
    variant = data.get("variant", "int8")
    if variant not in tts.KOKORO_MODELS:
        return bad("unknown variant")
    tid = worker.enqueue("kokoro_download", {"variant": variant}, dedupe=True, max_attempts=2)
    return ok(task_id=tid)


@app.get("/api/voice/piper")
def api_piper_catalog(lang: str = ""):
    cat = tts.piper_catalog()
    out = []
    for key, v in sorted(cat.items()):
        code = v["language"]["code"]
        if lang and not code.lower().startswith(lang.lower()):
            continue
        size = sum(f["size_bytes"] for f in v["files"].values())
        out.append({"key": key, "language": f"{v['language']['name_english']} ({code})", "quality": v["quality"],
                    "speakers": v["num_speakers"], "size_mb": round(size / 1e6)})
    return ok(voices=out)


@app.get("/api/voice/piper/{key}/card")
def api_piper_card(key: str):
    try:
        return ok(**tts.piper_card(key))
    except (KeyError, net.FetchError) as e:
        return bad(str(e))


@app.post("/api/voice/piper/{key}/download")
def api_piper_download(key: str):
    if key not in tts.piper_catalog():
        return bad("unknown voice")
    tid = worker.enqueue("piper_download", {"voice": key}, dedupe=True, max_attempts=2)
    return ok(task_id=tid)


@app.post("/api/voice/select")
async def api_voice_select(request: Request):
    data = await body(request)
    match = next((v for v in tts.all_voices() if v["engine"] == data.get("engine") and v["id"] == data.get("voice_id")), None)
    if not match:
        return bad("voice not installed")
    speed = max(0.7, min(1.4, float(data.get("speed", 1.0))))
    lang = data.get("language")
    if lang in ("Hindi", "English"):
        settings.merge("voice.by_language", {lang: {"engine": match["engine"], "voice_id": match["id"], "speed": speed}})
    else:
        settings.merge("voice", {"engine": match["engine"], "voice_id": match["id"], "speed": speed})
    return ok(voice=tts.current_voice(lang))


@app.post("/api/voice/sample")
async def api_voice_sample(request: Request):
    data = await body(request)
    try:
        path = tts.sample(str(data.get("engine")), str(data.get("voice_id")))
    except KeyError as e:
        return bad(str(e))
    return ok(url=media_url(str(path)) + f"?t={int(datetime.now().timestamp())}")


# ------------------------------------------------------------------ API: YouTube, analytics, feeds, brand

@app.post("/api/youtube/client")
async def api_yt_client(request: Request):
    data = await body(request)
    try:
        info = oauth.save_client(str(data.get("json", "")))
    except oauth.AuthError as e:
        return bad(str(e))
    return ok(**info)


@app.post("/api/youtube/connect")
def api_yt_connect():
    try:
        return ok(url=oauth.auth_url())
    except oauth.AuthError as e:
        return bad(str(e))


@app.post("/api/youtube/disconnect")
def api_yt_disconnect():
    oauth.disconnect()
    return ok()


@app.post("/api/analytics/sync")
def api_analytics_sync():
    return ok(task_id=worker.enqueue("analytics_sync", {}, dedupe=True))


@app.post("/api/feeds")
async def api_feeds(request: Request):
    data = await body(request)
    if data.get("delete"):
        db.ex("DELETE FROM feeds WHERE id=?", (int(data["delete"]),))
        return ok()
    url = str(data.get("url", "")).strip()
    if not url.startswith(("http://", "https://")) or not net.is_public_url(url):
        return bad("Enter a public http(s) feed URL.")
    try:
        _, text = net.get_text(url, respect_robots=False)
        import feedparser
        feed = feedparser.parse(text)
        if not feed.entries:
            return bad("No feed items found at that URL.")
    except net.FetchError as e:
        return bad(str(e))
    db.ex("INSERT OR IGNORE INTO feeds(url,title,enabled,created_at) VALUES(?,?,1,?)",
          (url, feed.feed.get("title", url)[:200], db.iso()))
    return ok()


@app.post("/api/upload/{kind}")
async def api_upload(request: Request, kind: str):
    """Small file uploads (logo / music) sent as base64 JSON."""
    data = await body(request)
    name = Path(str(data.get("name", "file"))).name
    raw = base64.b64decode(str(data.get("data", "")).split(",")[-1] or b"", validate=False)
    if kind == "logo":
        if len(raw) > 5_000_000:
            return bad("Logo must be under 5 MB.")
        from io import BytesIO
        from PIL import Image
        try:
            img = Image.open(BytesIO(raw)).convert("RGBA")
        except Exception:
            return bad("Not an image.")
        dest = config.BRAND_DIR / "logo.png"
        img.save(dest)
        settings.merge("brand", {"logo_path": str(dest)})
        return ok(url=media_url(str(dest)))
    if kind == "music":
        if len(raw) > 40_000_000 or Path(name).suffix.lower() not in (".mp3", ".wav", ".m4a", ".aac", ".flac"):
            return bad("Music must be mp3/wav/m4a/aac/flac under 40 MB.")
        dest = config.MUSIC_DIR / name
        dest.write_bytes(raw)
        return ok(name=name)
    return bad("unknown upload kind")


# ------------------------------------------------------------------ channels (multi-channel studio)

def _channel_arg(request: Request, channel: str | int | None) -> int | None:
    """?channel=N wins; otherwise the channel last chosen in the selector (cookie); 0/'all' = every channel."""
    from .. import channels
    raw = channel if channel not in (None, "") else request.cookies.get("channel", "")
    if str(raw) in ("0", "all", ""):
        return None
    try:
        cid = int(raw)
    except ValueError:
        return None
    ch = channels.get(cid)
    return cid if ch and ch["workspace_id"] == ws_id(request) else None


def _with_cookie(resp: HTMLResponse, cid: int | None) -> HTMLResponse:
    resp.set_cookie("channel", str(cid or 0), samesite="strict", httponly=True, max_age=365 * 86400,
                    secure=config.SECURE_COOKIES)
    return resp


@app.get("/channels", response_class=HTMLResponse)
def channels_page(request: Request):
    from .. import channels
    rows = channels.all_channels(ws_id(request))
    for c in rows:
        c["counts"] = channels.counts(c["id"])
        c["connected"] = oauth.connected(c["id"])
        steps = [s for s in channel_checklist(c) if not s.get("optional")]
        c["setup_done"], c["setup_total"] = sum(s["done"] for s in steps), len(steps)
    return page(request, "channels.html", channels=rows, workflows=channels.WORKFLOWS, styles=channels.STYLES,
                languages=channels.LANGUAGES)


def _channel_art(cid: int) -> dict[str, str]:
    folder = config.DATA_DIR / "channels" / f"{cid:04d}" / "branding"
    return {k: media_url(str(folder / f)) + f"?v={int((folder / f).stat().st_mtime)}"
            for k, f in (("banner", "banner_2560x1440.jpg"), ("avatar", "profile_800x800.png")) if (folder / f).exists()}


def channel_checklist(ch: dict[str, Any]) -> list[dict[str, Any]]:
    """The setup steps of one channel, each with whether it's done and where to do it."""
    from ..creative import characters
    cid = ch["id"]
    connected = oauth.connected(cid)
    sched_on = bool(ch.get("schedule_enabled")) if ch["workflow"] != "nifty_daily" else bool(db.q1(
        "SELECT 1 FROM schedules WHERE task_type='nifty_daily_episode' AND enabled=1 AND channel_id=?", (cid,)))
    made = db.q1("SELECT COUNT(*) AS n FROM productions WHERE channel_id=?", (cid,))["n"]
    published = db.q1("SELECT COUNT(*) AS n FROM productions WHERE channel_id=? AND youtube_video_id IS NOT NULL",
                      (cid,))["n"]
    ys = ch.get("youtube_status") or {}
    steps = [
        {"name": "Link your YouTube channel", "done": bool(ch.get("youtube_expected_id") or ch.get("youtube_channel_id")),
         "detail": "Paste the channel's @handle or link so uploads can only ever go to that channel.",
         "href": f"/channels/{cid}?tab=youtube", "action": "Link channel"},
        {"name": "Connect YouTube (one Google sign-in)", "done": connected,
         "detail": "Google requires the owner to approve uploads once: click Connect and choose this channel. "
                   "After that, uploads are automatic.", "href": f"/channels/{cid}?tab=youtube", "action": "Connect"},
        {"name": "Niche, style and language", "done": bool(ch.get("niche") and ch.get("styles") and ch.get("language")),
         "detail": f"{ch.get('niche') or '—'} · {', '.join(ch.get('styles') or []) or 'no styles'} · {ch.get('language')}",
         "href": f"/channels/{cid}?tab=settings", "action": "Edit"},
        {"name": "Audience" + (" (made for kids)" if ch.get("made_for_kids") else ""), "done": bool(ch.get("audience")),
         "detail": ch.get("audience") or "Who watches it — used to pitch every script.",
         "href": f"/channels/{cid}?tab=settings", "action": "Edit"},
        {"name": "Daily video schedule", "done": sched_on,
         "detail": (f"New video at {ch.get('schedule_time')} on the chosen days" if sched_on and ch["workflow"] != "nifty_daily"
                    else "NIFTY episode at 3:40 PM on trading days" if sched_on else "Off — videos are only made when you ask."),
         "href": "/schedule" if ch["workflow"] == "nifty_daily" else f"/channels/{cid}?tab=settings", "action": "Set schedule"},
        {"name": "Publishing", "done": bool(ch.get("auto_publish")) or not ch.get("uploads_enabled"),
         "detail": ("Uploads switched off" if not ch.get("uploads_enabled") else
                    f"Automatic, {ch.get('publish_privacy')}" + (f", goes public at {ch['publish_time']}" if ch.get("publish_time") else "")
                    if ch.get("auto_publish") else "You approve each video before it's uploaded."),
         "href": f"/channels/{cid}?tab=youtube", "action": "Choose"},
    ]
    if ch["workflow"] == "animated_story":
        n = len(characters.for_channel(cid, include_draft=False))
        steps.append({"name": "Characters", "done": n > 0, "detail": f"{n} recurring character(s)",
                      "href": f"/channels/{cid}?tab=characters", "action": "Characters"})
    steps += [
        {"name": "First video", "done": made > 0, "detail": f"{made} made, {published} on YouTube",
         "href": f"/channels/{cid}", "action": "Create video"},
        {"name": "Phone-verify the YouTube channel (optional)", "done": ys.get("long_uploads") == "allowed",
         "detail": "Needed for videos over 15 minutes and custom thumbnails.", "href": "https://www.youtube.com/verify",
         "action": "Verify", "optional": True},
    ]
    return steps


@app.get("/channels/{cid:int}", response_class=HTMLResponse)
def channel_page(request: Request, cid: int, tab: str = "overview"):
    from .. import channels
    from ..creative import characters
    ch = channels.get(cid)
    if not ch:
        raise HTTPException(404)
    chars = characters.for_channel(cid)
    recent = db.q("SELECT id, title, format, kind, status, stage, updated_at, youtube_video_id, parent_id, error "
                  "FROM productions WHERE channel_id=? ORDER BY id DESC LIMIT 12", (cid,))
    from ..youtube import growth
    resp = page(request, "channel.html", ch=ch, counts=channels.counts(cid), connected=oauth.connected(cid), tiers=growth.TIERS,
                oauth_ready=oauth.configured(), characters=chars, productions=recent, tab=tab,
                workflows=channels.WORKFLOWS, styles=channels.STYLES, languages=channels.LANGUAGES,
                options=characters.OPTIONS, voices=sorted({v for vs in characters.VOICES.values() for v in vs}),
                ideas_n=db.q1("SELECT COUNT(*) AS n FROM topics WHERE channel_id=? AND status IN ('idea','approved')",
                              (cid,))["n"], checklist=channel_checklist(ch), art=_channel_art(cid))
    return _with_cookie(resp, cid)


@app.post("/api/channels")
async def api_channel_create(request: Request):
    from .. import channels, scheduler
    try:
        data = await body(request)
        plans.check(ws_id(request), "channel")
        info = channels.lookup_youtube(data["youtube"]) if str(data.get("youtube") or "").strip() else None
        cid = channels.create(data, ws_id(request))
        if info:
            channels.link_youtube(cid, info["id"])
    except (ValueError, TypeError, plans.LimitExceeded) as e:
        return bad(str(e))
    scheduler.sync_channels()
    logs.event("info", "channels", f"Channel #{cid} created")
    return ok(next=f"/channels/{cid}?tab=setup")


@app.post("/api/channels/{cid}")
async def api_channel_update(request: Request, cid: int):
    from .. import channels, scheduler
    if not channels.get(cid):
        return bad("not found", 404)
    try:
        channels.update(cid, await body(request))
    except (ValueError, TypeError) as e:
        return bad(str(e))
    scheduler.sync_channels()
    return ok()


@app.post("/api/channels/lookup")
async def api_channel_lookup(request: Request):
    from .. import channels
    try:
        info = channels.lookup_youtube(str((await body(request)).get("youtube") or ""))
    except Exception as e:
        return bad(str(e))
    return ok(channel=info, detail=f"Found “{info['title']}” ({info['handle']}) · {info['subscribers']} subscribers · "
                                   f"{info['videos']} videos")


@app.post("/api/channels/{cid}/delete")
def api_channel_delete(request: Request, cid: int):
    from .. import channels, scheduler
    try:
        res = channels.remove(cid)
    except ValueError as e:
        return bad(str(e))
    scheduler.sync_channels()
    return ok(detail=f"Channel removed — {res['videos_kept']} video(s) kept in your library", next="/channels")


@app.post("/api/channels/{cid}/youtube/{action}")
async def api_channel_youtube(request: Request, cid: int, action: str):
    from .. import channels
    if not channels.get(cid):
        return bad("not found", 404)
    if action == "link":
        try:
            info = channels.link_youtube(cid, str((await body(request)).get("youtube") or ""))
        except Exception as e:
            return bad(str(e))
        return ok(detail=f"Locked to “{info['title']}” ({info['handle']}). Now click Connect and pick that channel.")
    try:
        if action == "connect":
            return ok(url=oauth.auth_url(cid))
        if action == "disconnect":
            oauth.disconnect(cid)
            db.ex("UPDATE channels SET youtube_channel_id=NULL, youtube_status='{}', youtube_title=CASE WHEN "
                  "youtube_expected_id IS NULL THEN NULL ELSE youtube_title END WHERE id=?", (cid,))
            return ok()
        if action == "verify":
            info = oauth.verify_destination(cid)
            return ok(detail=f"Uploads for this studio channel go to “{info.get('title')}” ({info.get('id')}).")
    except oauth.AuthError as e:
        return bad(str(e))
    return bad("unknown action")


@app.post("/api/channels/{cid}/create")
async def api_channel_job(request: Request, cid: int):
    from .. import jobs
    data = await body(request)
    try:
        res = jobs.create(cid, str(data.get("request") or ""), idea_id=data.get("idea_id") or None,
                          fmt=data.get("format") or None)
    except ValueError as e:
        return bad(str(e))
    pids = res.get("production_ids") or []
    plan = res.get("plan") or {}
    detail = (f"Queued: {plan.get('content_type')} · {plan.get('format')} · {plan.get('language')} "
              f"({'animated story' if plan.get('animated') else 'researched video'})") if plan else "Daily episode queued"
    return ok(production_ids=pids, task_id=res.get("task_id"), detail=detail,
              next=f"/productions/{pids[0]}" if pids else "/queue")


@app.post("/api/channels/{cid}/growth")
def api_channel_growth(cid: int):
    from ..youtube import growth
    try:
        m = growth.refresh(cid)
    except Exception as e:
        return bad(f"Couldn't read this channel's numbers: {e}")
    return ok(detail=f"{m['subscribers']} subscribers · {m['watch_hours_12m']} watch hours · "
                     f"{m['shorts_views_90d']:,} Shorts views (90 days)")


@app.post("/api/channels/{cid}/branding")
def api_channel_branding(cid: int):
    from ..creative import branding
    branding.make_all(cid)
    return ok(detail="Channel art created")


@app.post("/api/channels/{cid}/plan")
async def api_channel_plan(request: Request, cid: int):
    """Preview how a request will be interpreted, without queuing anything."""
    from .. import channels, jobs
    if not channels.get(cid):
        return bad("not found", 404)
    return ok(plan=jobs.plan_for(cid, str((await body(request)).get("request") or "")))


@app.post("/api/channels/{cid}/characters")
async def api_character_create(request: Request, cid: int):
    from .. import channels
    from ..creative import characters
    if not channels.get(cid):
        return bad("not found", 404)
    data = await body(request)
    name = str(data.get("name") or "").strip()
    if not name:
        return bad("character name is required")
    voice = None
    if data.get("voice_id"):
        voice = {"engine": "sarvam", "voice_id": str(data["voice_id"]), "pace": 1.0}
    charid = characters.create(cid, name, data.get("design") or {}, role=str(data.get("role") or ""),
                               personality=str(data.get("personality") or ""),
                               catchphrases=[s.strip() for s in str(data.get("catchphrases") or "").split(",") if s.strip()],
                               relationships=str(data.get("relationships") or ""), rules=str(data.get("rules") or ""),
                               voice=voice)
    return ok(id=charid)


@app.post("/api/characters/{charid}/{action}")
def api_character_action(charid: int, action: str):
    from ..creative import characters
    c = characters.get(charid)
    if not c:
        return bad("not found", 404)
    if action in ("approve", "retire"):
        db.update("characters", charid, {"status": "approved" if action == "approve" else "retired",
                                         "updated_at": db.iso()})
        return ok()
    if action == "redraw":
        characters.render_reference(charid)
        return ok()
    return bad("unknown action")


@app.post("/api/productions/{pid}/job/{action}")
def api_job_control(pid: int, action: str):
    from .. import jobs
    if not db.q1("SELECT id FROM productions WHERE id=?", (pid,)):
        return bad("not found", 404)
    try:
        if action == "pause":
            return ok(detail="Paused" if jobs.pause(pid) else "Nothing queued to pause (a running stage finishes first)")
        if action == "resume":
            return ok(detail="Resumed" if jobs.resume(pid) else "Nothing paused")
        if action == "cancel":
            jobs.cancel(pid)
            return ok(detail="Cancelled")
        if action == "retry":
            jobs.retry(pid)
            return ok(detail="Retrying from the failed stage")
        if action == "shorts":
            from ..creative import workflow
            p = pipeline.load(pid)
            if p.get("kind") != "story" or p["format"] != "long" or p["status"] in ("queued",) or not p["timeline"]:
                return bad("Shorts can be cut from a finished long animated episode")
            return ok(detail=f"{len(workflow.make_shorts(pid))} Short(s) queued")
    except ValueError as e:
        return bad(str(e))
    return bad("unknown action")


# ------------------------------------------------------------------ create, upload & edit

@app.get("/create", response_class=HTMLResponse)
@app.get("/edit", response_class=HTMLResponse)
def create_page(request: Request, media: str = "", channel: str = ""):
    from .. import channels as chmod
    ws = ws_id(request)
    chosen = [int(x) for x in media.split(",") if x.isdigit()]
    picked = [m for m in (uploads_mod().get(i, ws) for i in chosen) if m]
    recent = db.q("SELECT * FROM media WHERE workspace_id=? AND status='ready' ORDER BY id DESC LIMIT 30", (ws,))
    lim = plans.limits(ws)
    from ..ai import videogen
    return page(request, "create.html", mode="edit" if request.url.path == "/edit" else "create",
                videogen=videogen.estimate(),
                channels=chmod.all_channels(ws), ch=chmod.get(_channel_arg(request, channel)), picked=picked,
                recent=recent, limits=lim, accept=",".join(sorted(e for ex in uploads_mod().KINDS.values() for e in ex)))


def uploads_mod():
    from .. import uploads
    return uploads


@app.post("/api/media/upload")
async def api_media_upload(request: Request):
    """Streamed upload: the request body is the file itself (no size-limited JSON/base64)."""
    from urllib.parse import unquote
    up = uploads_mod()
    name = unquote(request.headers.get("x-filename", ""))[:255]
    if not name:
        return bad("missing file name")
    ch = request.headers.get("x-channel", "")
    channel_id = int(ch) if ch.isdigit() and auth.owns(ws_id(request), "channels", int(ch)) else None
    try:
        m = await up.save_stream(ws_id(request), request.state.sess["user"]["id"], name, request.stream(), channel_id)
    except (up.UploadError, plans.LimitExceeded) as e:
        return bad(str(e))
    return ok(media={k: m.get(k) for k in ("id", "kind", "original_name", "size_bytes", "duration", "width", "height",
                                          "has_audio")}, duplicate=bool(m.get("duplicate")))


@app.post("/api/create")
async def api_create(request: Request):
    """One entry point for the Create and Upload & Edit pages."""
    from .. import channels as chmod, jobs
    from ..editing import workflow as editing
    data = await body(request)
    ws = ws_id(request)
    req = str(data.get("request") or "").strip()[:4000]
    fmt = data.get("format") or None
    media_ids = [int(x) for x in data.get("media_ids") or [] if str(x).isdigit()]
    cid = int(data["channel_id"]) if str(data.get("channel_id") or "").isdigit() else None
    if cid and not auth.owns(ws, "channels", cid):
        return bad("channel not found", 404)
    try:
        if media_ids:
            pid = editing.create(ws, req, media_ids, channel_id=cid, fmt=fmt, user_id=request.state.sess["user"]["id"])
            return ok(production_ids=[pid], next=f"/productions/{pid}", detail="Edit job started")
        if not req:
            return bad("Describe the video you want, or add files to edit.")
        if not cid:                                  # no channel picked: a general-purpose one, never the NIFTY slot
            mine = chmod.all_channels(ws)
            general = [c for c in mine if c["workflow"] in ("auto", "factual", "animated_story")]
            auto = [c for c in general if c["workflow"] == "auto"]
            if auto or general:
                cid = (auto or general)[0]["id"]
            else:
                try:
                    plans.check(ws, "channel")
                    cid = chmod.create({"name": "General videos", "workflow": "auto", "formats": "both",
                                        "language": editing.output_language(req, None)}, ws)
                except plans.LimitExceeded:
                    cid = mine[0]["id"] if mine else None
                if not cid:
                    return bad("Create a channel first.")
        if data.get("ai_video"):
            from ..ai import videogen
            est = videogen.estimate()
            if not est["enabled"]:
                return bad("AI video is switched off — the platform admin can enable it under AI agents & tools.")
            if not est["fits_cap"]:
                return bad(f"This job's AI clips (~${est['usd']:.2f}) would pass the monthly cap "
                           f"(${est['spent_this_month']:.2f} of ${est['cap']:.2f} used).")
        res = jobs.create(cid, req, fmt=fmt, no_upload=bool(data.get("no_upload")), ai_video=bool(data.get("ai_video")))
    except (ValueError, plans.LimitExceeded) as e:
        return bad(str(e))
    pids = res.get("production_ids") or []
    return ok(production_ids=pids, next=f"/productions/{pids[0]}" if pids else "/queue", detail="Video job started")


@app.get("/uploads", response_class=HTMLResponse)
def uploads_page(request: Request, kind: str = "", q: str = ""):
    ws = ws_id(request)
    where, args = ["workspace_id=?", "status='ready'"], [ws]
    if kind in uploads_mod().KINDS:
        where.append("kind=?"); args.append(kind)
    if q.strip():
        where.append("original_name LIKE ?"); args.append(f"%{q.strip()}%")
    rows = db.q(f"SELECT * FROM media WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 200", tuple(args))
    for r in rows:
        r["used_by"] = db.q("SELECT id, title, kind, status FROM productions WHERE workspace_id=? AND "
                            "EXISTS (SELECT 1 FROM json_each(productions.source_media) WHERE value=?) ORDER BY id DESC",
                            (ws, r["id"]))
        r["transcribed"] = r["transcript"] is not None
    return page(request, "uploads.html", media=rows, kind=kind, q=q, kinds=list(uploads_mod().KINDS))


@app.post("/api/media-files/{mid}/delete")
def api_media_delete(request: Request, mid: int):
    try:
        uploads_mod().delete(mid, ws_id(request))
    except uploads_mod().UploadError as e:
        return bad(str(e))
    return ok(detail="Original deleted (videos made from it are kept)")


@app.get("/media-files/{mid}")
def media_file(request: Request, mid: int, download: int = 0):
    m = uploads_mod().get(mid, ws_id(request))
    if not m or not Path(m["path"]).is_file():
        raise HTTPException(404)
    return FileResponse(m["path"], media_type=mimetypes.guess_type(m["path"])[0] or "application/octet-stream",
                        filename=m["original_name"] if download else None,
                        content_disposition_type="attachment" if download else "inline")


# ------------------------------------------------------------------ downloads (real files only)

DOWNLOADS = {"video": ("video_path", "mp4"), "srt": ("srt_path", "srt"), "thumbnail": ("thumbnail_path", "jpg")}


def outputs_of(p: dict[str, Any]) -> list[dict[str, Any]]:
    """Every downloadable file of a production that exists on disk right now."""
    out = []
    for key, (col, ext) in DOWNLOADS.items():
        path = p.get(col)
        if path and Path(path).is_file():
            out.append({"variant": key, "label": {"video": "Video", "srt": "Subtitles (.srt)",
                                                   "thumbnail": "Thumbnail"}[key],
                        "size": Path(path).stat().st_size, "path": path})
    ep = p.get("episode") if isinstance(p.get("episode"), dict) else db.loads(p.get("episode"), {}) or {}
    res = (ep.get("output") or (ep.get("outputs") or [None])[0]) if p.get("kind") in ("edit", "edit_clip") else None
    if res and res.get("clean_video") and Path(res["clean_video"]).is_file():
        out.insert(1, {"variant": "clean", "label": "Video without burned-in subtitles",
                       "size": Path(res["clean_video"]).stat().st_size, "path": res["clean_video"]})
    return out


@app.get("/download/{pid}/{variant}")
def download(pid: int, variant: str):
    p = pipeline.load(pid)
    f = next((o for o in outputs_of(p) if o["variant"] == variant), None)
    if not f:
        raise HTTPException(404, "This file doesn't exist (yet). Check the job status.")
    safe = re.sub(r"[^\w\- ]+", "", p.get("title") or f"video-{pid}", flags=re.UNICODE).strip()[:60] or f"video-{pid}"
    ext = Path(f["path"]).suffix
    suffix = {"clean": " (no subtitles)", "srt": "", "thumbnail": " thumbnail"}.get(variant, "")
    return FileResponse(f["path"], media_type=mimetypes.guess_type(f["path"])[0] or "application/octet-stream",
                        filename=f"{safe}{suffix}{ext}")


# ------------------------------------------------------------------ publishing

@app.post("/api/productions/{pid}/publish")
async def api_publish(request: Request, pid: int):
    """Choose the destination channel, confirm metadata, then publish now / schedule / keep private."""
    from .. import channels as chmod
    data = await body(request)
    p = pipeline.load(pid)
    if not p.get("video_path") or not Path(p["video_path"]).is_file():
        return bad("This video hasn't been rendered yet — nothing to publish.")
    if p["status"] in ("uploading", "uploaded") or p.get("youtube_video_id"):
        return bad("Already uploaded" + (f": https://youtu.be/{p['youtube_video_id']}" if p.get("youtube_video_id") else "."))
    cid = int(data.get("channel_id") or p.get("channel_id") or 0)
    if not cid or not auth.owns(ws_id(request), "channels", cid):
        return bad("Pick one of your channels to publish to.")
    if not chmod.uploads_allowed(chmod.get(cid)):
        return bad(f"Uploads are switched off for '{chmod.get(cid)['name']}'. Switch them on under YouTube channels, "
                   f"or download the video.")
    if not oauth.connected(cid):
        return bad(f"'{chmod.get(cid)['name']}' isn't connected to YouTube yet. Connect it under Channels → YouTube & "
                   f"publishing, or download the video and upload it yourself.")
    pkg = p["package"]
    title = str(data.get("title") or pkg.get("title") or p["title"]).replace("<", "").replace(">", "").strip()[:100]
    if len(title) < 3:
        return bad("Add a title.")
    desc = str(data.get("description") if data.get("description") is not None else pkg.get("final_description", ""))
    desc = desc.replace("<", "").replace(">", "")
    if len(desc.encode()) > 5000:
        return bad("Description exceeds YouTube's 5000-byte limit.")
    tags = [t.strip()[:30] for t in str(data.get("tags") if data.get("tags") is not None else ",".join(pkg.get("tags", [])))
            .split(",") if t.strip()][:20]
    pkg.update({"title": title, "final_description": desc, "tags": tags})
    pipeline.save(pid, package=pkg, title=title, channel_id=cid)
    return _approve(pid, str(data.get("privacy") or "private"), data.get("publish_at_local"))


# ------------------------------------------------------------------ projects, publishing, usage, admin, tools

@app.get("/projects", response_class=HTMLResponse)
def projects_page(request: Request, status: str = "all"):
    ws = ws_id(request)
    rows = db.q(f"SELECT p.*, c.name AS channel_name, (SELECT COUNT(*) FROM productions k WHERE k.parent_id=p.id) "
                f"AS children FROM productions p LEFT JOIN channels c ON c.id=p.channel_id WHERE p.workspace_id=? "
                f"AND p.parent_id IS NULL AND {LIB_STATUS.get(status, '1=1')} ORDER BY p.id DESC LIMIT 100", (ws,))
    for r in rows:
        stages, _, _ = pipeline.stages_for(r.get("kind") or "general")
        r["stages"] = stages
        r["sources"] = [m for m in (uploads_mod().get(i, ws) for i in db.loads(r.get("source_media"), []) or []) if m]
        req = db.loads(r.get("request"), {}) or {}
        r["prompt"] = req.get("request") if isinstance(req, dict) else ""
    return page(request, "projects.html", projects=rows, filter=status)


@app.get("/publishing", response_class=HTMLResponse)
def publishing_page(request: Request):
    from .. import channels as chmod
    ws = ws_id(request)
    rows = db.q("SELECT p.*, c.name AS channel_name FROM productions p LEFT JOIN channels c ON c.id=p.channel_id "
                "WHERE p.workspace_id=? AND (p.status IN ('approved','uploading','uploaded','awaiting_approval') "
                "OR p.youtube_video_id IS NOT NULL) ORDER BY CASE p.status WHEN 'uploading' THEN 0 WHEN 'approved' THEN 1 "
                "WHEN 'awaiting_approval' THEN 2 ELSE 3 END, p.updated_at DESC LIMIT 150", (ws,))
    chans = chmod.all_channels(ws)
    for c in chans:
        c["connected"] = oauth.connected(c["id"])
    return page(request, "publishing.html", rows=rows, channels=chans)


@app.get("/billing", response_class=HTMLResponse)
def billing_page(request: Request):
    ws = ws_id(request)
    w = plans.workspace(ws)
    used = plans.used(ws)
    by_kind = db.q("SELECT kind, SUM(quantity) AS q, SUM(COALESCE(cost_usd,0)) AS c, "
                   "GROUP_CONCAT(DISTINCT cost_basis) AS basis FROM usage WHERE workspace_id=? AND created_at>=? "
                   "GROUP BY kind ORDER BY kind", (ws, plans._month_start()))
    return page(request, "billing.html", ws=w, limits=plans.limits(ws), used=used, by_kind=by_kind,
                plans_table=plans.PLANS, labels=plans.LIMIT_LABELS, provider=settings.get("billing.provider"))


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    users = db.q("SELECT u.*, (SELECT GROUP_CONCAT(workspace_id) FROM workspace_members m WHERE m.user_id=u.id) AS ws "
                 "FROM users u ORDER BY u.id")
    workspaces = db.q("SELECT w.*, (SELECT COUNT(*) FROM channels c WHERE c.workspace_id=w.id) AS channels, "
                      "(SELECT COUNT(*) FROM productions p WHERE p.workspace_id=w.id) AS videos, "
                      "(SELECT email FROM users u WHERE u.id=w.owner_user_id) AS owner FROM workspaces w ORDER BY w.id")
    for w in workspaces:
        w["used"] = plans.used(w["id"])
    errors = db.q("SELECT * FROM logs WHERE level='error' ORDER BY id DESC LIMIT 30")
    return page(request, "admin.html", users=users, workspaces=workspaces, errors=errors, plans_table=plans.PLANS,
                allow_signup=settings.get("auth.allow_signup"))


@app.post("/api/admin/workspaces/{wid}")
async def api_admin_workspace(request: Request, wid: int):
    data = await body(request)
    if not db.q1("SELECT id FROM workspaces WHERE id=?", (wid,)):
        return bad("not found", 404)
    if data.get("plan") not in plans.PLANS:
        return bad("unknown plan")
    db.ex("UPDATE workspaces SET plan=? WHERE id=?", (data["plan"], wid))
    logs.event("info", "admin", f"Workspace #{wid} moved to the {data['plan']} plan")
    return ok()


@app.post("/api/admin/users/{uid}/reset-password")
def api_admin_reset_password(request: Request, uid: int):
    """No email system yet: the admin gives the creator a one-time password, shown here once."""
    if not db.q1("SELECT id FROM users WHERE id=?", (uid,)):
        return bad("not found", 404)
    temp = secrets.token_urlsafe(9)
    db.ex("UPDATE users SET password_hash=? WHERE id=?", (auth._hash_pw(temp), uid))
    db.ex("DELETE FROM sessions WHERE user_id=?", (uid,))
    logs.event("info", "admin", f"Password reset for user #{uid}")
    return ok(detail="Temporary password created", show=temp)


@app.post("/api/admin/users/{uid}")
async def api_admin_user(request: Request, uid: int):
    data = await body(request)
    if uid == request.state.sess["user"]["id"]:
        return bad("You can't disable your own account.")
    if data.get("status") not in ("active", "disabled"):
        return bad("status must be active or disabled")
    db.ex("UPDATE users SET status=? WHERE id=?", (data["status"], uid))
    if data["status"] == "disabled":
        db.ex("DELETE FROM sessions WHERE user_id=?", (uid,))
    return ok()


@app.post("/api/admin/signup")
async def api_admin_signup(request: Request):
    settings.set("auth.allow_signup", bool((await body(request)).get("allow")))
    return ok()


@app.get("/tools", response_class=HTMLResponse)
def tools_page(request: Request):
    """Which production capabilities are available right now, and what powers each one."""
    import shutil as _sh
    from ..ai import videogen
    vg = videogen.cfg()
    prov = providers.status_all()
    llm_ok = any(p["ok"] for p in prov)
    whisper = (config.DATA_DIR / "models" / "whisper").exists()
    sarvam = settings.has_secret("sarvam_api_key")
    yt = oauth.configured()
    rows = [
        ("Prompt interpretation & planning", llm_ok, "AI text model (" + ", ".join(p["label"] for p in prov if p["ok"]) + ")" if llm_ok else "no AI text model configured"),
        ("Research (news, Wikipedia, YouTube, web search)", True, "built-in collectors; YouTube/Ollama search when their keys are set"),
        ("Scriptwriting & screenplays", llm_ok, "AI text model"),
        ("Transcription (speech-to-text)", whisper, "faster-whisper running on this computer" + ("" if whisper else " — model downloads on first use (~460 MB)")),
        ("Scene detection & clip selection", bool(_sh.which("ffmpeg") or config.FFMPEG), "FFmpeg scene analysis + AI selection from the transcript"),
        ("Video editing & rendering", True, f"FFmpeg ({config.FFMPEG})"),
        ("Subtitles (burned-in + .srt, translation)", True, "transcript timings; translation by the AI text model"),
        ("Animated characters & scenes", True, "built-in 2D cartoon renderer"),
        ("Voiceover", sarvam, "Sarvam Bulbul (Hindi/Hinglish/English)" if sarvam else "add a Sarvam key; Kokoro offline voice as fallback"),
        ("Music & sound", True, "original music generated by the app"),
        ("Thumbnails", True, "Pillow renderer"),
        ("Quality checks", True, "ffprobe validation of every output"),
        ("Publishing to YouTube", yt, "official YouTube Data API (OAuth per channel)" if yt else "add a Google OAuth client in Settings"),
        ("AI image generation", settings.has_secret("gemini_api_key"), "Gemini images (optional)"),
        ("AI video generation (text-to-video)", vg["enabled"], "Google Gemini Omni Flash — "
         + ("on" if vg["enabled"] else "off until the platform admin enables it")
         + (f"; ${videogen.month_spent():.2f} of ${vg['monthly_cap_usd']:.2f} monthly cap used" if is_admin(request) else "")),
        ("Voice replacement / dubbing of uploads", False, "not available yet"),
    ]
    return page(request, "tools.html", rows=rows, providers=prov, vg=vg, vg_est=videogen.estimate(),
                vg_spent=videogen.month_spent())


# ------------------------------------------------------------------ storage: delete videos, free space

@app.get("/storage", response_class=HTMLResponse)
def storage_page(request: Request):
    from .. import storage
    rep = storage.report(ws_id(request))
    if not is_admin(request):
        rep.pop("cache_bytes", None)
        rep.pop("disk_free_bytes", None)
    return page(request, "storage.html", rep=rep, lim=plans.limits(ws_id(request)),
                auto_clean=settings.get("storage.auto_clean_days"),
                auto_delete=settings.get("storage.auto_delete_rejected_days"))


@app.post("/api/productions/{pid}/delete")
def api_delete_video(pid: int):
    from .. import storage
    try:
        freed = storage.delete_video(pid)
    except ValueError as e:
        return bad(str(e))
    return ok(detail=f"Deleted — {freed / 1e6:.0f} MB freed", next="/productions")


@app.post("/api/productions/{pid}/clean")
def api_clean_video(pid: int):
    from .. import storage
    freed = storage.clear_working_files(pid)
    return ok(detail=f"{freed / 1e6:.0f} MB of working files cleared" if freed else "Nothing to clear")


@app.post("/api/storage/{action}")
async def api_storage(request: Request, action: str):
    from .. import storage
    ws = ws_id(request)
    data = await body(request)
    if action == "delete":
        ids = [int(x) for x in data.get("ids") or [] if str(x).isdigit()]
        if not ids or any(not auth.owns(ws, "productions", i) for i in ids):
            return bad("Select videos from your library.")
        freed, errors = 0, []
        for i in ids:
            if not db.q1("SELECT 1 FROM productions WHERE id=?", (i,)):
                continue                                # already removed with its parent
            try:
                freed += storage.delete_video(i)
            except ValueError as e:
                errors.append(str(e))
        return ok(detail=f"Deleted {len(ids) - len(errors)} video(s), {freed / 1e6:.0f} MB freed"
                  + (f". Skipped: {'; '.join(errors)}" if errors else ""))
    if action == "clean":
        freed = sum(storage.clear_working_files(r["id"]) for r in
                    db.q("SELECT id FROM productions WHERE workspace_id=?", (ws,)))
        return ok(detail=f"{freed / 1e6:.0f} MB of working files cleared (final videos kept)")
    if action == "settings" and is_admin(request):
        settings.set("storage.auto_clean_days", max(0, min(365, int(data.get("auto_clean_days") or 0))))
        settings.set("storage.auto_delete_rejected_days", max(0, min(365, int(data.get("auto_delete_days") or 0))))
        return ok()
    if action == "cache" and is_admin(request):
        freed = storage.clear_cache(int(data.get("older_than_days") or 0))
        return ok(detail=f"Download cache: {freed / 1e6:.0f} MB freed")
    return bad("unknown action", 404)


# ------------------------------------------------------------------ AI video (text-to-video)

@app.get("/api/videogen/estimate")
def api_videogen_estimate():
    from ..ai import videogen
    return ok(**videogen.estimate())


@app.post("/api/admin/videogen/{action}")
async def api_videogen_admin(request: Request, action: str):
    from ..ai import videogen
    data = await body(request)
    if action == "check":
        return ok(**videogen.check_access(), detail=videogen.check_access()["detail"])
    if action == "save":
        enable = bool(data.get("enabled"))
        if enable and not videogen.check_access()["available"]:
            return bad(videogen.check_access()["detail"])
        settings.set("videogen.enabled", enable)
        settings.set("videogen.monthly_cap_usd", max(0.0, min(500.0, float(data.get("monthly_cap_usd") or 0))))
        settings.set("videogen.max_clips_per_video", max(1, min(10, int(data.get("max_clips_per_video") or 3))))
        return ok(detail="AI video " + ("enabled" if enable else "switched off"))
    return bad("unknown action", 404)


# ------------------------------------------------------------------ new-user setup

@app.get("/welcome", response_class=HTMLResponse)
@app.get("/channels/new", response_class=HTMLResponse)
def channel_setup_page(request: Request):
    from .. import channels as chmod
    return page(request, "channel_setup.html", languages=chmod.LANGUAGES, styles=chmod.STYLES,
                welcome=request.url.path == "/welcome")


@app.post("/api/welcome")
async def api_welcome(request: Request):
    from .. import channels as chmod, scheduler
    data = await body(request)
    ws = ws_id(request)
    if data.get("workflow") not in ("auto", "animated_story", "factual"):
        return bad("pick what kind of videos")
    try:
        plans.check(ws, "channel")
        info = chmod.lookup_youtube(data["youtube"]) if str(data.get("youtube") or "").strip() else None
        cid = chmod.create({k: data.get(k) for k in ("name", "niche", "audience", "language", "workflow", "formats",
                                                       "made_for_kids")} | {"auto_publish": False}, ws)
        if info:
            chmod.link_youtube(cid, info["id"])
    except (ValueError, TypeError, plans.LimitExceeded) as e:
        return bad(str(e))
    scheduler.sync_channels()
    return ok(next=f"/channels/{cid}?tab=youtube")
