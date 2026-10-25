"""SQLite access: one connection per thread, WAL mode, versioned migrations."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from . import config

_local = threading.local()

MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE niches (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'candidate',      -- candidate | approved | rejected
        source TEXT NOT NULL DEFAULT 'ai',             -- ai | user
        search_queries TEXT NOT NULL DEFAULT '[]',
        assessment TEXT,                               -- AI synthesis (labelled hypothesis)
        metrics TEXT,                                  -- computed from evidence (verified)
        research_run_id INTEGER,
        created_at TEXT NOT NULL,
        approved_at TEXT
    );
    CREATE TABLE research_runs (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,                            -- niche | ideation | topic
        subject TEXT,
        status TEXT NOT NULL DEFAULT 'running',
        notes TEXT,
        sources_used TEXT,
        created_at TEXT NOT NULL,
        finished_at TEXT
    );
    CREATE TABLE evidence (
        id INTEGER PRIMARY KEY,
        run_id INTEGER REFERENCES research_runs(id) ON DELETE CASCADE,
        niche_id INTEGER,
        topic_id INTEGER,
        source_type TEXT NOT NULL,
        url TEXT NOT NULL,
        title TEXT,
        published_at TEXT,
        snippet TEXT,
        content TEXT,
        metrics TEXT,
        fetched_at TEXT NOT NULL
    );
    CREATE INDEX idx_evidence_run ON evidence(run_id);
    CREATE INDEX idx_evidence_topic ON evidence(topic_id);
    CREATE TABLE topics (
        id INTEGER PRIMARY KEY,
        niche_id INTEGER,
        run_id INTEGER,
        title TEXT NOT NULL,
        angle TEXT,
        format TEXT NOT NULL,                          -- short | long
        hook_idea TEXT,
        why_now TEXT,
        status TEXT NOT NULL DEFAULT 'idea',           -- idea | approved | rejected | in_production | produced | duplicate
        score REAL NOT NULL DEFAULT 0,
        score_breakdown TEXT,
        claims TEXT,
        evidence_ids TEXT,
        gap TEXT,
        search_query TEXT,
        norm_title TEXT,
        duplicate_of INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE productions (
        id INTEGER PRIMARY KEY,
        topic_id INTEGER,
        format TEXT NOT NULL,
        title TEXT,
        status TEXT NOT NULL,
        stage TEXT,
        package TEXT,
        timeline TEXT,
        qa TEXT,
        video_path TEXT,
        thumbnail_path TEXT,
        srt_path TEXT,
        test_mode INTEGER NOT NULL DEFAULT 0,
        approved_at TEXT,
        youtube_video_id TEXT,
        youtube_upload_uri TEXT,
        privacy TEXT,
        publish_at TEXT,
        uploaded_at TEXT,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE assets (
        id INTEGER PRIMARY KEY,
        production_id INTEGER REFERENCES productions(id) ON DELETE CASCADE,
        scene_index INTEGER,
        kind TEXT,                                     -- image | video
        provider TEXT,
        provider_id TEXT,
        source_url TEXT,
        page_url TEXT,
        local_path TEXT,
        license TEXT,
        license_url TEXT,
        author TEXT,
        attribution TEXT,
        width INTEGER,
        height INTEGER,
        duration REAL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_assets_prod ON assets(production_id);
    CREATE TABLE tasks (
        id INTEGER PRIMARY KEY,
        type TEXT NOT NULL,
        lane TEXT NOT NULL DEFAULT 'general',          -- general | media
        payload TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'pending',        -- pending | running | done | failed | paused | cancelled
        priority INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3,
        run_after TEXT NOT NULL,
        locked_at TEXT,
        last_error TEXT,
        result TEXT,
        production_id INTEGER,
        schedule_id INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX idx_tasks_claim ON tasks(status, lane, run_after);
    CREATE TABLE schedules (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        task_type TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}',
        kind TEXT NOT NULL,                            -- daily | interval
        time_of_day TEXT,
        interval_minutes INTEGER,
        days TEXT,                                     -- JSON list of weekday ints (0=Mon) or null
        enabled INTEGER NOT NULL DEFAULT 1,
        next_run_at TEXT,
        last_run_at TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE ai_usage (
        id INTEGER PRIMARY KEY,
        provider TEXT,
        model TEXT,
        task TEXT,
        input_tokens INTEGER,
        output_tokens INTEGER,
        cost_usd REAL NOT NULL DEFAULT 0,
        ok INTEGER NOT NULL DEFAULT 1,
        error TEXT,
        request_id TEXT,
        duration_s REAL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE api_quota (
        id INTEGER PRIMARY KEY,
        api TEXT NOT NULL,
        units INTEGER NOT NULL,
        day TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE logs (
        id INTEGER PRIMARY KEY,
        level TEXT NOT NULL,
        source TEXT,
        message TEXT NOT NULL,
        production_id INTEGER,
        task_id INTEGER,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_logs_prod ON logs(production_id);
    CREATE TABLE feeds (
        id INTEGER PRIMARY KEY,
        url TEXT UNIQUE NOT NULL,
        title TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE TABLE analytics (
        id INTEGER PRIMARY KEY,
        production_id INTEGER,
        video_id TEXT NOT NULL,
        metrics TEXT NOT NULL,
        fetched_at TEXT NOT NULL
    );
    CREATE INDEX idx_analytics_video ON analytics(video_id, fetched_at);
    """,
    # v2 — compliance-first finance content (SEBI / YouTube). Additive only: existing data is untouched.
    """
    ALTER TABLE productions ADD COLUMN kind TEXT NOT NULL DEFAULT 'general';
    ALTER TABLE productions ADD COLUMN episode TEXT;
    ALTER TABLE productions ADD COLUMN compliance TEXT;
    CREATE TABLE compliance_rules (
        id TEXT PRIMARY KEY,
        category TEXT NOT NULL,                        -- sebi | youtube | data | assets | legal
        title TEXT NOT NULL,
        authority TEXT,
        url TEXT,
        document_url TEXT,
        reference TEXT,                                -- circular / notification number
        document_date TEXT,
        effective_date TEXT,
        last_checked TEXT,
        status TEXT NOT NULL,                          -- VERIFIED | REVIEW_REQUIRED | NOT_APPLICABLE
        critical INTEGER NOT NULL DEFAULT 0,           -- unresolved => blocks public publication
        summary TEXT,
        effect TEXT,
        evidence TEXT,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE compliance_reviews (
        id INTEGER PRIMARY KEY,
        rule_id TEXT NOT NULL REFERENCES compliance_rules(id),
        reviewer TEXT NOT NULL,
        qualification TEXT,
        decision TEXT NOT NULL,                        -- resolved | resolved_with_conditions | still_open
        conditions TEXT,
        notes TEXT,
        evidence_url TEXT,
        reviewed_on TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE data_sources (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,                            -- manual_import | licensed_vendor | test_fixture
        terms_url TEXT,
        terms_checked TEXT,
        terms_summary TEXT,
        automated_access_allowed INTEGER NOT NULL DEFAULT 0,
        permits_public_video INTEGER,                  -- 1 yes / 0 no / NULL unknown
        permits_monetization INTEGER,
        licence_reference TEXT,
        licence_holder TEXT,
        licence_expiry TEXT,
        licence_evidence TEXT,
        status TEXT NOT NULL,                          -- PERMITTED | REVIEW_REQUIRED | PROHIBITED
        notes TEXT,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE market_datasets (
        id INTEGER PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES data_sources(id),
        symbol TEXT NOT NULL,
        exchange TEXT NOT NULL,
        timeframe TEXT NOT NULL,                       -- 1d | 1h | 15m
        timezone TEXT NOT NULL,
        data_nature TEXT NOT NULL,                     -- end_of_day | delayed | intraday
        retrieved_at TEXT NOT NULL,
        retrieved_by TEXT,
        retrieval_notes TEXT,
        original_filename TEXT,
        stored_path TEXT,
        sha256 TEXT,
        first_date TEXT,
        last_date TEXT,
        rows INTEGER,
        validation TEXT,
        status TEXT NOT NULL,                          -- valid | valid_with_warnings | invalid
        created_at TEXT NOT NULL
    );
    CREATE TABLE market_bars (
        id INTEGER PRIMARY KEY,
        dataset_id INTEGER NOT NULL REFERENCES market_datasets(id) ON DELETE CASCADE,
        trade_date TEXT NOT NULL,
        ts TEXT,
        open REAL NOT NULL,
        high REAL NOT NULL,
        low REAL NOT NULL,
        close REAL NOT NULL,
        volume REAL
    );
    CREATE INDEX idx_bars_dataset ON market_bars(dataset_id, trade_date, ts);
    CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY,
        production_id INTEGER,
        event TEXT NOT NULL,
        actor TEXT NOT NULL,
        details TEXT NOT NULL,
        artifact_path TEXT,
        artifact_sha256 TEXT,
        prev_hash TEXT,
        entry_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_audit_prod ON audit_log(production_id);
    """,
    # v3 — per-video audience and language (Hindi for India by default, English for international)
    """
    ALTER TABLE productions ADD COLUMN audience TEXT NOT NULL DEFAULT 'india';
    ALTER TABLE productions ADD COLUMN language TEXT;
    """,
    # v4: Shorts cut from a long episode are their own productions, linked to it
    """
    ALTER TABLE productions ADD COLUMN parent_id INTEGER;
    CREATE INDEX IF NOT EXISTS idx_productions_parent ON productions(parent_id);
    """,
    # v5: some sources give only high / low / close (e.g. the Pivot Reversal project's level files): the open
    # is stored as unknown (NULL), never estimated
    """
    CREATE TABLE market_bars_v5 (
        id INTEGER PRIMARY KEY,
        dataset_id INTEGER NOT NULL REFERENCES market_datasets(id) ON DELETE CASCADE,
        trade_date TEXT NOT NULL,
        ts TEXT,
        open REAL,
        high REAL NOT NULL,
        low REAL NOT NULL,
        close REAL NOT NULL,
        volume REAL
    );
    INSERT INTO market_bars_v5 SELECT id, dataset_id, trade_date, ts, open, high, low, close, volume FROM market_bars;
    DROP TABLE market_bars;
    ALTER TABLE market_bars_v5 RENAME TO market_bars;
    CREATE INDEX idx_bars_dataset ON market_bars(dataset_id, trade_date, ts);
    """,
    # v6: one studio, many channels — every channel has its own niche, settings, characters, ideas, library,
    # schedule and YouTube authorization
    """
    CREATE TABLE channels (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        niche TEXT NOT NULL DEFAULT '',
        sub_niche TEXT NOT NULL DEFAULT '',
        audience TEXT NOT NULL DEFAULT '',
        language TEXT NOT NULL DEFAULT 'Hindi',
        tone TEXT NOT NULL DEFAULT '',
        styles TEXT NOT NULL DEFAULT '[]',
        workflow TEXT NOT NULL DEFAULT 'auto',            -- auto | animated_story | factual | nifty_daily
        formats TEXT NOT NULL DEFAULT 'both',             -- long | short | both
        long_minutes REAL NOT NULL DEFAULT 8,
        short_seconds INTEGER NOT NULL DEFAULT 50,
        shorts_per_episode INTEGER NOT NULL DEFAULT 4,
        instructions TEXT NOT NULL DEFAULT '',
        restrictions TEXT NOT NULL DEFAULT '',
        research_required INTEGER NOT NULL DEFAULT 0,
        branding TEXT NOT NULL DEFAULT '{}',
        voice TEXT NOT NULL DEFAULT '{}',
        schedule_enabled INTEGER NOT NULL DEFAULT 0,
        schedule_time TEXT NOT NULL DEFAULT '18:00',
        schedule_days TEXT NOT NULL DEFAULT '[0,1,2,3,4,5,6]',
        timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
        auto_publish INTEGER NOT NULL DEFAULT 0,
        publish_privacy TEXT NOT NULL DEFAULT 'private',
        youtube_channel_id TEXT,
        youtube_title TEXT,
        status TEXT NOT NULL DEFAULT 'active',            -- active | paused
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE characters (
        id INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT '',
        design TEXT NOT NULL DEFAULT '{}',
        personality TEXT NOT NULL DEFAULT '',
        voice TEXT NOT NULL DEFAULT '{}',
        catchphrases TEXT NOT NULL DEFAULT '[]',
        relationships TEXT NOT NULL DEFAULT '',
        rules TEXT NOT NULL DEFAULT '',
        reference_path TEXT,
        status TEXT NOT NULL DEFAULT 'approved',          -- approved | draft
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX idx_characters_channel ON characters(channel_id);
    ALTER TABLE productions ADD COLUMN channel_id INTEGER;
    ALTER TABLE productions ADD COLUMN request TEXT;
    ALTER TABLE topics ADD COLUMN channel_id INTEGER;
    ALTER TABLE topics ADD COLUMN idea_kind TEXT NOT NULL DEFAULT 'ai_original';
    ALTER TABLE topics ADD COLUMN style TEXT;
    ALTER TABLE topics ADD COLUMN language TEXT;
    ALTER TABLE topics ADD COLUMN summary TEXT;
    ALTER TABLE topics ADD COLUMN source_url TEXT;
    ALTER TABLE topics ADD COLUMN source_date TEXT;
    ALTER TABLE topics ADD COLUMN saved INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE schedules ADD COLUMN channel_id INTEGER;
    CREATE INDEX idx_topics_channel ON topics(channel_id, created_at);
    CREATE INDEX idx_productions_channel ON productions(channel_id, created_at);
    """,
    # v7: multi-user SaaS — users, workspaces, sessions; uploaded media; usage metering
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        email TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL DEFAULT '',
        password_hash TEXT NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 0,          -- platform admin (operator of the SaaS)
        status TEXT NOT NULL DEFAULT 'active',        -- active | disabled
        created_at TEXT NOT NULL,
        last_login_at TEXT
    );
    CREATE TABLE workspaces (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        plan TEXT NOT NULL DEFAULT 'free',
        limits TEXT NOT NULL DEFAULT '{}',            -- per-workspace overrides of the plan limits
        owner_user_id INTEGER,
        created_at TEXT NOT NULL
    );
    CREATE TABLE workspace_members (
        workspace_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        role TEXT NOT NULL DEFAULT 'owner',           -- owner | editor | viewer
        created_at TEXT NOT NULL,
        PRIMARY KEY (workspace_id, user_id)
    );
    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        workspace_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    CREATE TABLE media (
        id INTEGER PRIMARY KEY,
        workspace_id INTEGER NOT NULL,
        channel_id INTEGER,
        kind TEXT NOT NULL,                           -- video | audio | image | subtitle | document
        original_name TEXT NOT NULL,
        path TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        mime TEXT,
        duration REAL,
        width INTEGER,
        height INTEGER,
        fps REAL,
        has_audio INTEGER,
        meta TEXT NOT NULL DEFAULT '{}',
        transcript TEXT,                              -- JSON segments once transcribed
        status TEXT NOT NULL DEFAULT 'ready',         -- ready | deleted
        created_by INTEGER,
        created_at TEXT NOT NULL,
        deleted_at TEXT
    );
    CREATE INDEX idx_media_ws ON media(workspace_id, created_at);
    CREATE TABLE usage (
        id INTEGER PRIMARY KEY,
        workspace_id INTEGER NOT NULL,
        kind TEXT NOT NULL,                           -- render_minutes | voice_chars | ai_calls | storage_bytes | uploads | videos
        quantity REAL NOT NULL,
        cost_usd REAL,
        cost_basis TEXT,                              -- actual | estimate
        production_id INTEGER,
        idem_key TEXT UNIQUE,                         -- one charge per job step, even after retries
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_usage_ws ON usage(workspace_id, created_at);
    ALTER TABLE channels ADD COLUMN workspace_id INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE productions ADD COLUMN workspace_id INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE productions ADD COLUMN source_media TEXT;  -- JSON list of media ids this output was made from
    ALTER TABLE topics ADD COLUMN workspace_id INTEGER NOT NULL DEFAULT 1;
    CREATE INDEX idx_productions_ws ON productions(workspace_id, created_at);
    """,
    # v8: per-channel upload switch and YouTube growth / monetization progress
    """
    ALTER TABLE channels ADD COLUMN uploads_enabled INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE channels ADD COLUMN monetization TEXT NOT NULL DEFAULT '{}';
    """,
    # v9: channel management — link by @handle (locked target), made-for-kids, YouTube upload readiness
    """
    ALTER TABLE channels ADD COLUMN made_for_kids INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE channels ADD COLUMN youtube_expected_id TEXT;
    ALTER TABLE channels ADD COLUMN youtube_handle TEXT;
    ALTER TABLE channels ADD COLUMN youtube_status TEXT NOT NULL DEFAULT '{}';
    """,
    # v10: the time of day a channel's videos go public (YouTube scheduled publishing); NULL = right away
    """
    ALTER TABLE channels ADD COLUMN publish_time TEXT;
    """,
    # v11: superadmin approval of sign-ups, per-workspace feature permissions, upgrade requests
    """
    ALTER TABLE users ADD COLUMN approved_at TEXT;
    ALTER TABLE users ADD COLUMN approved_by INTEGER;
    ALTER TABLE workspaces ADD COLUMN permissions TEXT NOT NULL DEFAULT '{}';
    CREATE TABLE upgrade_requests (
        id INTEGER PRIMARY KEY,
        workspace_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        plan TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',      -- pending | approved | declined
        created_at TEXT NOT NULL,
        decided_at TEXT,
        decided_by INTEGER
    );
    """,
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    """UTC timestamp in a fixed format so string comparison orders correctly."""
    return (dt or utcnow()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def iso_in(seconds: float) -> str:
    return iso(utcnow() + timedelta(seconds=seconds))


def _row_factory(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
        c.row_factory = _row_factory
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.conn = c
    return c


def migrate() -> None:
    c = conn()
    version = c.execute("PRAGMA user_version").fetchone()["user_version"]
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        c.execute("BEGIN IMMEDIATE")
        try:
            for stmt in _split_sql(sql):
                c.execute(stmt)
            c.execute(f"PRAGMA user_version={i}")
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def _split_sql(sql: str) -> list[str]:
    # Migrations contain no string literals with ';', so a plain split is safe.
    lines = [ln.split("--", 1)[0] for ln in sql.splitlines()]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def q(sql: str, params: tuple | list | dict = ()) -> list[dict[str, Any]]:
    return conn().execute(sql, params).fetchall()


def q1(sql: str, params: tuple | list | dict = ()) -> dict[str, Any] | None:
    return conn().execute(sql, params).fetchone()


def ex(sql: str, params: tuple | list | dict = ()) -> int:
    """Execute a statement; returns the number of rows changed (use insert() for new ids)."""
    return conn().execute(sql, params).rowcount


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def loads(s: str | None, default: Any = None) -> Any:
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


def insert(table: str, row: dict[str, Any]) -> int:
    cols = list(row.keys())
    sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})"
    return conn().execute(sql, [row[c] for c in cols]).lastrowid


def update(table: str, row_id: int, fields: dict[str, Any]) -> None:
    if not fields:
        return
    sets = ",".join(f"{k}=?" for k in fields)
    conn().execute(f"UPDATE {table} SET {sets} WHERE id=?", [*fields.values(), row_id])
