"""Cryptographic primitives.

Every algorithm here is delegated to an established library. Nothing in this
file invents cryptography — that rule is absolute and is the reason the
previous XOR-with-key-11 "encryption" of a GitHub token was trivially broken.

  passwords        Argon2id            (argon2-cffi)
  recovery codes   Argon2id            (same hasher, single-use)
  TOTP             RFC 6238            (pyotp)
  TOTP secret      Fernet AES-128-CBC  (cryptography) — encrypted at rest
  grants           Ed25519             (cryptography)
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.exceptions import InvalidSignature

from app.config import get_settings

settings = get_settings()

# OWASP-recommended baseline for Argon2id.
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16)


# ══════════════════════════════════════════════════════════════════════════
# Passwords
# ══════════════════════════════════════════════════════════════════════════
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, stored_hash: str | None) -> bool:
    """Constant-ish time. Returns False rather than raising, so callers cannot
    accidentally distinguish 'no such user' from 'wrong password' by exception
    type — that distinction is a user-enumeration oracle."""
    if not stored_hash:
        # Still burn comparable time so a missing user isn't detectable by latency.
        _hasher.hash("dummy-password-for-timing-equalisation")
        return False
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError, Exception):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════
# Recovery codes
# ══════════════════════════════════════════════════════════════════════════
def generate_recovery_codes(count: int | None = None) -> list[str]:
    """Plaintext is returned ONCE, shown to the customer, then never again."""
    n = count or settings.RECOVERY_CODE_COUNT
    out = []
    for _ in range(n):
        raw = secrets.token_hex(5).upper()          # 10 hex chars
        out.append(f"{raw[:5]}-{raw[5:]}")
    return out


def hash_recovery_code(code: str) -> str:
    return _hasher.hash(_normalise_recovery(code))


def verify_recovery_code(code: str, stored_hash: str) -> bool:
    try:
        return _hasher.verify(stored_hash, _normalise_recovery(code))
    except Exception:
        return False


def _normalise_recovery(code: str) -> str:
    return code.strip().upper().replace("-", "").replace(" ", "")


# ══════════════════════════════════════════════════════════════════════════
# TOTP  (RFC 6238 — Google Authenticator compatible)
# ══════════════════════════════════════════════════════════════════════════
def _fernet_from(key_material: str) -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(key_material.encode()).digest())
    return Fernet(key)


def _totp_fernet() -> Fernet:
    """MFA secrets are encrypted with an INDEPENDENT key, not derived from the
    Ed25519 signing key. This is a deliberate security-domain split (see
    app/config.py TOTP_ENCRYPTION_KEY_B64 and docs/CREDENTIAL_INCIDENT.md
    step 1.1): a signing-key rotation must not force every customer's TOTP
    secret to be re-encrypted, and a TOTP-key rotation must not touch grant
    signing. Do not merge these back into one secret.
    """
    src = settings.TOTP_ENCRYPTION_KEY_B64 or "dev-only-insecure-totp-placeholder"
    return _fernet_from(src)


def _legacy_signing_derived_fernet() -> Fernet:
    """Pre-migration scheme (kept for decrypt-only backward compatibility).

    Before the independent TOTP_ENCRYPTION_KEY_B64 was introduced, the TOTP
    Fernet key was derived from SIGNING_PRIVATE_KEY_B64. Any secret encrypted
    under that scheme carries no version prefix. This function exists only so
    those legacy blobs keep decrypting during the migration window; it must
    never be used to encrypt anything new.
    """
    src = settings.SIGNING_PRIVATE_KEY_B64 or "dev-only-insecure-placeholder"
    return _fernet_from(src)


_TOTP_ENC_V2_PREFIX = "v2:"  # independent-key scheme (current)
                             # no prefix = legacy signing-key-derived scheme


def new_totp_secret() -> str:
    return pyotp.random_base32()


def encrypt_totp_secret(secret: str) -> str:
    """Always encrypts under the current (independent-key) scheme."""
    token = _totp_fernet().encrypt(secret.encode()).decode()
    return _TOTP_ENC_V2_PREFIX + token


def decrypt_totp_secret(blob: str) -> str:
    """Decrypts current-scheme secrets; falls back to the legacy
    signing-key-derived scheme for blobs encrypted before the migration.
    """
    if blob.startswith(_TOTP_ENC_V2_PREFIX):
        token = blob[len(_TOTP_ENC_V2_PREFIX):]
        try:
            return _totp_fernet().decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise ValueError(
                "TOTP secret could not be decrypted (TOTP_ENCRYPTION_KEY_B64 "
                "missing or rotated without a re-encryption pass?)"
            ) from exc

    # No prefix -> pre-migration blob, encrypted from the signing key.
    try:
        return _legacy_signing_derived_fernet().decrypt(blob.encode()).decode()
    except InvalidToken as exc:
        raise ValueError(
            "TOTP secret could not be decrypted (legacy signing-derived "
            "scheme — signing key rotated since this secret was encrypted?)"
        ) from exc


def totp_provisioning_uri(secret: str, account: str, issuer: str = "StopLossPro") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=issuer)


def verify_totp(secret: str, code: str, last_used_step: int | None = None) -> tuple[bool, int | None]:
    """Verify a TOTP code with ±1 step tolerance for clock drift.

    Returns (ok, step_consumed). The caller MUST persist step_consumed and pass
    it back as last_used_step next time — otherwise the same code can be
    replayed inside its 30-second window, which matters because our device
    takeover flow is gated on exactly one TOTP entry.
    """
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return False, None

    totp = pyotp.TOTP(secret)
    now = int(time.time())
    for offset in (0, -1, 1):
        step = now // 30 + offset
        if secrets.compare_digest(totp.at(step * 30), code):
            if last_used_step is not None and step <= last_used_step:
                return False, None          # replay
            return True, step
    return False, None


# ══════════════════════════════════════════════════════════════════════════
# Ed25519 signed authorization grants
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class GrantClaims:
    user_id: str
    licence_id: str
    session_id: str
    device_id: str
    entitlements: list[str]
    issued_at: int
    expires_at: int
    counter: int
    key_id: str


def _private_key() -> ed25519.Ed25519PrivateKey:
    if not settings.SIGNING_PRIVATE_KEY_B64:
        raise RuntimeError("SIGNING_PRIVATE_KEY_B64 is not configured")
    pem = base64.b64decode(settings.SIGNING_PRIVATE_KEY_B64)
    return serialization.load_pem_private_key(pem, password=None)


def _public_key() -> ed25519.Ed25519PublicKey:
    if not settings.SIGNING_PUBLIC_KEY_B64:
        raise RuntimeError("SIGNING_PUBLIC_KEY_B64 is not configured")
    pem = base64.b64decode(settings.SIGNING_PUBLIC_KEY_B64)
    return serialization.load_pem_public_key(pem)


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_grant(
    *, user_id: str, licence_id: str, session_id: str, device_id: str,
    entitlements: list[str], counter: int, ttl_seconds: int | None = None,
) -> str:
    """Produce a compact signed grant: base64url(payload).base64url(signature).

    Deliberately NOT a JWT. JWT's `alg` header is a well-known downgrade
    footgun ("alg":"none", HS/RS confusion). Here the algorithm is fixed by
    the verifier and is not negotiable by the token.
    """
    now = int(time.time())
    ttl = ttl_seconds or settings.GRANT_TTL_SECONDS
    payload = {
        "sub": user_id, "lic": licence_id, "sid": session_id, "did": device_id,
        "ent": entitlements, "iat": now, "exp": now + ttl,
        "ctr": counter, "kid": settings.SIGNING_KEY_ID,
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sig = _private_key().sign(body)
    return f"{_b64u(body)}.{_b64u(sig)}"


def verify_grant(token: str, *, public_key_b64: str | None = None) -> GrantClaims:
    """Verify signature then expiry. Raises ValueError on any problem.

    Signature is checked BEFORE any field is trusted, so a forged token cannot
    influence control flow through its own claims.
    """
    try:
        body_b64, sig_b64 = token.split(".")
        body, sig = _b64u_dec(body_b64), _b64u_dec(sig_b64)
    except Exception as exc:
        raise ValueError("malformed grant") from exc

    if public_key_b64:
        pub = serialization.load_pem_public_key(base64.b64decode(public_key_b64))
    else:
        pub = _public_key()

    try:
        pub.verify(sig, body)
    except InvalidSignature as exc:
        raise ValueError("invalid grant signature") from exc

    p = json.loads(body)
    if int(p["exp"]) <= int(time.time()):
        raise ValueError("grant expired")

    return GrantClaims(
        user_id=p["sub"], licence_id=p["lic"], session_id=p["sid"], device_id=p["did"],
        entitlements=list(p.get("ent", [])), issued_at=int(p["iat"]),
        expires_at=int(p["exp"]), counter=int(p.get("ctr", 0)), key_id=p.get("kid", ""),
    )


# ══════════════════════════════════════════════════════════════════════════
# Opaque session tokens (client -> server auth for heartbeat/session calls)
# ══════════════════════════════════════════════════════════════════════════
def new_session_token() -> tuple[str, str]:
    """Returns (plaintext, sha256-hash). Only the hash is stored, so a database
    read does not yield usable credentials."""
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def hash_session_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
