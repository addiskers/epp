"""
Admin auth: scrypt password hashing + a small signed bearer token (HMAC, no extra deps).
The React SPA logs in, stores the token, and sends it as `Authorization: Bearer <token>`.

Roles: ``admin`` (everything) and ``dept_user`` (their own department's tickets only).
A per-username login throttle blunts password guessing; every login attempt is audited.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time

from fastapi import HTTPException, Request

import eo_db

logger = logging.getLogger(__name__)

_SESSION_SECRET = (os.getenv("EPP_SESSION_SECRET") or os.getenv("EO_SESSION_SECRET")
                   or os.getenv("ANALYTICS_SECRET") or "epp-dev-secret").encode()
_TOKEN_TTL = int(os.getenv("EPP_SESSION_TTL", os.getenv("EO_SESSION_TTL", str(60 * 60 * 12))))  # 12 hours

_SCRYPT = dict(n=16384, r=8, p=1, dklen=32)

ROLES = eo_db.ROLES

# Login throttle: 5 failures in a row locks the username for 15 minutes.
_LOCK_AFTER = int(os.getenv("EPP_LOGIN_MAX_FAILURES", "5"))
_LOCK_SECONDS = int(os.getenv("EPP_LOGIN_LOCK_SECONDS", "900"))
_failures: dict = {}
_failures_lock = threading.Lock()


# Password hashing
def hash_password(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return dk.hex(), salt.hex()


def verify_password(password: str, hash_hex: str, salt_hex: str) -> bool:
    try:
        expected = bytes.fromhex(hash_hex)
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


# Signed tokens
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_SESSION_SECRET, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def _verify(token: str) -> dict | None:
    try:
        body, sig = token.split(".", 1)
        expected = _b64(hmac.new(_SESSION_SECRET, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_unb64(body))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def issue_token(user: dict) -> str:
    return _sign({"uid": user["id"], "role": user["role"], "exp": int(time.time()) + _TOKEN_TTL})


def verify_token(token: str) -> dict | None:
    payload = _verify(token)
    if not payload or payload.get("kind"):
        return None                  # a purpose token is not a session
    return payload


# Purpose tokens: browser-mic agent test on /ws, and the live-call watcher socket. Both
# sockets are otherwise unauthenticated, so the SPA mints a short-lived token for each.
_TEST_TOKEN_KIND = "agent_test"
_LIVE_TOKEN_KIND = "live_watch"
_TEST_TOKEN_TTL = int(os.getenv("EPP_TEST_TOKEN_TTL", os.getenv("EO_TEST_TOKEN_TTL", "300")))


def issue_test_token(user: dict, *, agent_id=None) -> str:
    return _sign({"kind": _TEST_TOKEN_KIND, "uid": user["id"], "agent_id": agent_id,
                  "exp": int(time.time()) + _TEST_TOKEN_TTL})


def verify_test_token(token: str) -> dict | None:
    payload = _verify(token)
    return payload if payload and payload.get("kind") == _TEST_TOKEN_KIND else None


def issue_live_token(user: dict) -> str:
    return _sign({"kind": _LIVE_TOKEN_KIND, "uid": user["id"], "role": user["role"],
                  "exp": int(time.time()) + _TEST_TOKEN_TTL})


def verify_live_token(token: str) -> dict | None:
    payload = _verify(token)
    return payload if payload and payload.get("kind") == _LIVE_TOKEN_KIND else None


# Seed + login
def seed_admin() -> None:
    """Create the first admin from env if the users table is empty."""
    if eo_db.count_users() > 0:
        return
    username = os.getenv("EPP_ADMIN_USER") or os.getenv("EO_ADMIN_USER") or "admin"
    password = os.getenv("EPP_ADMIN_PASS") or os.getenv("EO_ADMIN_PASS") or "change-me-now"
    h, s = hash_password(password)
    eo_db.create_user(username=username, name="Helpline Admin", password_hash=h, password_salt=s, role="admin")
    logger.info("Seeded initial admin user '%s'", username)


def lock_remaining(username: str) -> int:
    """Seconds until this username may try again (0 = not locked)."""
    with _failures_lock:
        n, until = _failures.get(username, (0, 0.0))
    return int(max(0.0, until - time.time())) if n >= _LOCK_AFTER else 0


def _record_failure(username: str) -> None:
    with _failures_lock:
        n, until = _failures.get(username, (0, 0.0))
        if n >= _LOCK_AFTER and until <= time.time():
            n = 0                       # lock expired — start a fresh count
        n += 1
        _failures[username] = (n, time.time() + _LOCK_SECONDS if n >= _LOCK_AFTER else 0.0)


def _clear_failures(username: str) -> None:
    with _failures_lock:
        _failures.pop(username, None)


def reset_throttle() -> None:
    """Test hook."""
    with _failures_lock:
        _failures.clear()


def authenticate(username: str, password: str) -> dict | None:
    """The user row on success, None on failure. Raises HTTP 429 while locked."""
    username = (username or "").strip().lower()
    remaining = lock_remaining(username)
    if remaining:
        raise HTTPException(status_code=429,
                            detail=f"Too many failed attempts. Try again in {max(1, remaining // 60)} minute(s).")
    user = eo_db.get_user_by_username(username)
    if not user or not user.get("active"):
        hash_password(password)         # constant-ish time: blunt username enumeration
        _record_failure(username)
        return None
    if not verify_password(password, user["password_hash"], user["password_salt"]):
        _record_failure(username)
        return None
    _clear_failures(username)
    return user


# FastAPI dependencies
def _token_from_request(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # ?token= lets <audio> elements authenticate (no Authorization header) when streaming recordings
    return request.cookies.get("epp_session", "") or request.query_params.get("token", "")


def require_user(request: Request) -> dict:
    """Any active, authenticated user."""
    payload = verify_token(_token_from_request(request))
    if not payload:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = eo_db.get_user(int(payload["uid"]))
    if not user or not user.get("active"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


def is_admin(user: dict) -> bool:
    return (user or {}).get("role") == "admin"


def superadmin_usernames() -> set:
    """Usernames allowed to change the subscription plan (the service provider's people, listed
    in EPP_SUPERADMIN_USERS). Blank = nobody: the client's admins only ever see it."""
    raw = os.getenv("EPP_SUPERADMIN_USERS") or ""
    return {u.strip().lower() for u in raw.split(",") if u.strip()}


def is_superadmin(user: dict) -> bool:
    return is_admin(user) and (user or {}).get("username", "").lower() in superadmin_usernames()


def require_superadmin(request: Request) -> dict:
    user = require_admin(request)
    if not is_superadmin(user):
        raise HTTPException(status_code=403, detail="Only the service provider can change the plan")
    return user


def scope_department(user: dict):
    """None for an admin (sees everything); the department id for a dept_user. A dept_user
    with no department sees nothing (-1 matches no row) rather than everything."""
    if is_admin(user):
        return None
    dep = user.get("department_id")
    return int(dep) if dep not in (None, "") else -1
