"""Accounts, workspaces and sessions.

Passwords are hashed with scrypt; sessions are random tokens stored hashed (a stolen database can't be used to
log in). The first account created becomes the platform admin and owns workspace #1 — the studio's existing
channels and videos. Every later sign-up gets its own empty workspace.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Any

from . import db, logs

SESSION_DAYS = 30
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,}$")


class AuthError(Exception):
    pass


def _hash_pw(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def _check_pw(password: str, stored: str) -> bool:
    try:
        _, salt, dk = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(_hash_pw(password, bytes.fromhex(salt)).split("$")[2], dk)


def has_users() -> bool:
    return bool(db.q1("SELECT id FROM users LIMIT 1"))


def register(email: str, password: str, name: str = "", workspace_name: str = "") -> dict[str, Any]:
    email = email.strip().lower()
    if not EMAIL.match(email):
        raise AuthError("enter a valid email address")
    if len(password) < 10:
        raise AuthError("use a password of at least 10 characters")
    if db.q1("SELECT id FROM users WHERE email=?", (email,)):
        raise AuthError("an account with this email already exists — sign in instead")
    first = not has_users()
    uid = db.insert("users", {"email": email, "name": name.strip()[:80], "password_hash": _hash_pw(password),
                              "is_admin": int(first), "status": "active", "created_at": db.iso()})
    if first and db.q1("SELECT id FROM workspaces WHERE id=1"):
        ws = 1                                       # existing studio data (created by migrations/bootstrap)
        db.ex("UPDATE workspaces SET owner_user_id=? WHERE id=1", (uid,))
    else:
        ws_name = workspace_name.strip() or f"{(name or email.split('@')[0]).strip()}'s studio"
        ws = db.insert("workspaces", {"name": ws_name[:80], "plan": "studio" if first else "free",
                                      "owner_user_id": uid, "created_at": db.iso()})
    db.insert("workspace_members", {"workspace_id": ws, "user_id": uid, "role": "owner", "created_at": db.iso()})
    logs.event("info", "auth", f"Account created ({'platform admin' if first else 'creator'}), workspace #{ws}")
    return {"user_id": uid, "workspace_id": ws}


def login(email: str, password: str) -> tuple[str, dict[str, Any]]:
    u = db.q1("SELECT * FROM users WHERE email=?", (email.strip().lower(),))
    if not u or not _check_pw(password, u["password_hash"]):
        raise AuthError("wrong email or password")
    if u["status"] != "active":
        raise AuthError("this account is disabled")
    ws = db.q1("SELECT workspace_id FROM workspace_members WHERE user_id=? ORDER BY workspace_id LIMIT 1", (u["id"],))
    if not ws:
        raise AuthError("this account has no workspace")
    token = secrets.token_urlsafe(32)
    db.insert("sessions", {"token_hash": hashlib.sha256(token.encode()).hexdigest(), "user_id": u["id"],
                           "workspace_id": ws["workspace_id"], "created_at": db.iso(),
                           "expires_at": db.iso_in(SESSION_DAYS * 86400)})
    db.update("users", u["id"], {"last_login_at": db.iso()})
    return token, u


def session(token: str | None) -> dict[str, Any] | None:
    """{user, workspace, role} for a valid session token, else None."""
    if not token:
        return None
    s = db.q1("SELECT * FROM sessions WHERE token_hash=? AND expires_at>?",
              (hashlib.sha256(token.encode()).hexdigest(), db.iso()))
    if not s:
        return None
    u = db.q1("SELECT id, email, name, is_admin, status FROM users WHERE id=?", (s["user_id"],))
    if not u or u["status"] != "active":
        return None
    m = db.q1("SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=?", (s["workspace_id"], u["id"]))
    if not m:
        return None
    ws = db.q1("SELECT * FROM workspaces WHERE id=?", (s["workspace_id"],))
    return {"user": u, "workspace": ws, "role": m["role"]}


def logout(token: str | None) -> None:
    if token:
        db.ex("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))


def ensure_default_workspace() -> None:
    """Workspace #1 holds everything made before accounts existed."""
    if not db.q1("SELECT id FROM workspaces WHERE id=1"):
        db.ex("INSERT INTO workspaces (id, name, plan, created_at) VALUES (1, 'My studio', 'studio', ?)", (db.iso(),))


# ------------------------------------------------------------------ ownership checks

OWNED = {"productions": "workspace_id", "topics": "workspace_id", "channels": "workspace_id", "media": "workspace_id"}


def owns(ws_id: int, table: str, row_id: int) -> bool:
    if table == "characters":
        r = db.q1("SELECT c.workspace_id FROM characters x JOIN channels c ON c.id=x.channel_id WHERE x.id=?", (row_id,))
    elif table == "tasks":
        r = db.q1("SELECT p.workspace_id FROM tasks t JOIN productions p ON p.id=t.production_id WHERE t.id=?", (row_id,))
    else:
        r = db.q1(f"SELECT {OWNED[table]} AS workspace_id FROM {table} WHERE id=?", (row_id,))
    return bool(r) and r["workspace_id"] == ws_id
