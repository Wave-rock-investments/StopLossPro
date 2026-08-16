"""Cryptographic primitives for admin auth — Phase 7.

Every algorithm here is delegated to an established library, matching the
absolute rule already set in this repo's Working/backend/app/security.py
("nothing in this file invents cryptography" — that rule exists because an
XOR-with-fixed-key "encryption" of a GitHub token was trivially broken in
this same project's history, see PROJECT_STATUS.md §3.1):

  admin passwords    Argon2id              (argon2-cffi)
  admin TOTP         RFC 6238              (pyotp)
  admin session      itsdangerous signed,  (itsdangerous — URLSafeTimedSerializer)
                      time-limited token

Session design note: unlike Working/backend's `sessions` table (DB-backed,
individually revocable, built for a desktop client's licensing model),
Sterling_Room's admin sessions are a signed, stateless, short-TTL cookie —
no admin_sessions table exists yet. That is a deliberate scope decision, not
an oversight: it means an admin session cannot be individually force-expired
before its TTL without rotating ADMIN_SESSION_SECRET (which invalidates
every admin session at once, not just one). If per-session revocation
becomes a real requirement, add an admin_sessions table and switch to
opaque-token-plus-DB-lookup, the same pattern already proven in Working/backend.
"""
from __future__ import annotations

import time

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import get_settings

_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, stored_hash: str | None) -> bool:
    if not stored_hash:
        return False
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
    except Exception:
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except Exception:
        return False


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, account: str, issuer: str = "Sterling_Room Admin") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=issuer)


def verify_totp(secret: str, code: str) -> bool:
    if not secret or not code:
        return False
    try:
        return pyotp.TOTP(secret).verify(code, valid_window=1)
    except Exception:
        return False


_SESSION_SALT = "sterling-admin-session"
_SESSION_MAX_AGE_S = 12 * 3600  # 12h — short-lived, matches the "no per-session revocation" tradeoff above


def _serializer() -> URLSafeTimedSerializer:
    settings = get_settings()
    secret = settings.ADMIN_SESSION_SECRET or settings.ADAPTER_API_KEYS or "dev-insecure-secret"
    return URLSafeTimedSerializer(secret, salt=_SESSION_SALT)


def issue_admin_session(admin_id: str) -> str:
    return _serializer().dumps({"admin_id": admin_id, "iat": time.time()})


def verify_admin_session(token: str) -> str | None:
    """Returns the admin_id if the token is valid and unexpired, else None."""
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=_SESSION_MAX_AGE_S)
    except (BadSignature, SignatureExpired):
        return None
    except Exception:
        return None
    return data.get("admin_id")
