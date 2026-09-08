"""Org/user/session store: same flat-JSON-on-disk pattern as the rest of
this project (backend/storage.py, backend/jobs.py) — no database, three
files under auth_root (orgs.json, users.json, sessions.json).

Accounts are created only via scripts/create_org.py and
scripts/create_user.py — deliberately no self-service signup or password
reset endpoint, matching this project's CLI-only administration pattern
elsewhere. An organization is the real isolation boundary (see
backend/storage.py's org-scoped batch_dir): multiple users at the same
org share access to that org's batches.
"""

import hashlib
import json
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Cookie, HTTPException, Request

ROOT = Path(__file__).resolve().parent.parent
# Overridable via FMH_AUTH_ROOT, separate from FMH_UPLOADS_ROOT (see
# storage.py) — account/session state and uploaded video are different
# kinds of data with different lifecycles, kept in different roots
# rather than nesting one under the other.
DEFAULT_AUTH_ROOT = Path(os.environ.get("FMH_AUTH_ROOT", str(ROOT / "auth")))

SESSION_COOKIE_NAME = "fmh_session"
SESSION_TTL = timedelta(days=30)

# scrypt cost parameters: Python's own hashlib docs' recommended
# interactive-login defaults (n=2**14, r=8, p=1) — expensive enough to
# resist offline brute force, cheap enough for one real login request.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _orgs_path(auth_root) -> Path:
    return Path(auth_root) / "orgs.json"


def _users_path(auth_root) -> Path:
    return Path(auth_root) / "users.json"


def _sessions_path(auth_root) -> Path:
    return Path(auth_root) / "sessions.json"


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(password.encode(), salt=salt,
                            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$")
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    actual = hashlib.scrypt(password.encode(), salt=salt,
                            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return secrets.compare_digest(actual, expected)


def create_org(auth_root, name: str) -> dict:
    orgs = _load_json(_orgs_path(auth_root))
    org_id = uuid.uuid4().hex[:12]
    orgs[org_id] = {"name": name, "created_at": _now()}
    _save_json(_orgs_path(auth_root), orgs)
    return {"org_id": org_id, **orgs[org_id]}


def list_orgs(auth_root) -> dict:
    return _load_json(_orgs_path(auth_root))


def create_user(auth_root, org_id: str, username: str, password: str) -> dict:
    orgs = _load_json(_orgs_path(auth_root))
    if org_id not in orgs:
        raise ValueError(f"no such org: {org_id}")
    users = _load_json(_users_path(auth_root))
    if username in users:
        raise ValueError(f"username already exists: {username}")
    user_id = uuid.uuid4().hex[:12]
    users[username] = {
        "user_id": user_id, "org_id": org_id,
        "password_hash": hash_password(password), "created_at": _now(),
    }
    _save_json(_users_path(auth_root), users)
    return {"user_id": user_id, "org_id": org_id, "username": username}


def authenticate(auth_root, username: str, password: str) -> dict | None:
    users = _load_json(_users_path(auth_root))
    user = users.get(username)
    if not user or not verify_password(password, user["password_hash"]):
        return None
    return {"user_id": user["user_id"], "org_id": user["org_id"], "username": username}


def create_session(auth_root, user_id: str, org_id: str) -> str:
    sessions = _load_json(_sessions_path(auth_root))
    token = secrets.token_urlsafe(32)
    sessions[token] = {
        "user_id": user_id, "org_id": org_id,
        "expires_at": (datetime.now(timezone.utc) + SESSION_TTL).isoformat(),
    }
    _save_json(_sessions_path(auth_root), sessions)
    return token


def delete_session(auth_root, token: str | None) -> None:
    if not token:
        return
    sessions = _load_json(_sessions_path(auth_root))
    if sessions.pop(token, None) is not None:
        _save_json(_sessions_path(auth_root), sessions)


def resolve_session(auth_root, token: str | None) -> dict | None:
    """Returns {"user_id", "org_id"} for a live, unexpired token, or None
    — never raises, since an invalid/expired/missing cookie is the
    normal "not logged in" case, not an error condition."""
    if not token:
        return None
    sessions = _load_json(_sessions_path(auth_root))
    session = sessions.get(token)
    if not session:
        return None
    if datetime.now(timezone.utc) >= datetime.fromisoformat(session["expires_at"]):
        return None
    return {"user_id": session["user_id"], "org_id": session["org_id"]}


def require_login(request: Request,
                  fmh_session: str | None = Cookie(default=None)) -> dict:
    """FastAPI dependency: every gated endpoint takes
    `session: dict = Depends(require_login)` and reads session["org_id"]
    — org_id is always resolved here, server-side, from the cookie,
    never from a URL path parameter."""
    session = resolve_session(request.app.state.auth_root, fmh_session)
    if session is None:
        raise HTTPException(401, "login required")
    return session
