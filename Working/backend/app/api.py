"""HTTP API for the desktop client.

Error responses carry a stable machine-readable `code` and a short human
message. Internal detail (stack traces, SQL, library errors) never reaches the
client — those go to the server log only.
"""
from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app import services
from app.config import get_settings
from app.database import get_db
from app.models import ConsentDocument, User

settings = get_settings()
router = APIRouter()


# ── crude in-process rate limiter ──────────────────────────────────────────
# Adequate for a single-process MVP. Multi-worker or multi-instance deployment
# needs a shared store (Redis) — noted rather than silently assumed away.
_BUCKETS: dict[str, deque] = defaultdict(deque)


def rate_limit(key: str, limit: int, window: int) -> None:
    now = time.time()
    q = _BUCKETS[key]
    while q and q[0] < now - window:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(status_code=429, detail={"code": "RATE_LIMITED",
                                                     "message": "Too many attempts. Try again shortly."})
    q.append(now)


def client_ip(request: Request) -> str:
    return (request.client.host if request.client else "unknown")


def _err(exc: services.ServiceError):
    payload = {"code": exc.code, "message": exc.message}
    payload.update(exc.extra or {})
    return HTTPException(status_code=exc.http_status, detail=payload)


# ── schemas ────────────────────────────────────────────────────────────────
class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)
    device_public_key: str = Field(min_length=10, max_length=1000)
    device_name: str | None = Field(default=None, max_length=200)
    os_name: str | None = Field(default=None, max_length=100)
    app_version: str | None = Field(default=None, max_length=50)
    takeover: bool = False
    totp_code: str | None = Field(default=None, max_length=10)


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)
    full_name: str | None = Field(default=None, max_length=200)


class MfaConfirmIn(BaseModel):
    code: str = Field(min_length=6, max_length=6)


class ConsentIn(BaseModel):
    document: str
    version: str
    accepted: bool
    app_version: str | None = None


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail={"code": "SESSION_INVALID",
                                                     "message": "Missing session token."})
    return authorization.split(" ", 1)[1].strip()


def current_session(authorization: str | None = Header(default=None),
                    db: Session = Depends(get_db)):
    try:
        return services.resolve_session(db, _bearer(authorization))
    except services.ServiceError as e:
        raise _err(e)


# ══════════════════════════════════════════════════════════════════════════
# Auth + session
# ══════════════════════════════════════════════════════════════════════════
@router.post("/auth/register", tags=["auth"])
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db)):
    """Self-serve signup. Creates the account PENDING — no licence, cannot log
    in — until an admin reviews and approves it in the panel (same manual
    payment-reconciliation step every customer already goes through)."""
    ip = client_ip(request)
    rate_limit(f"register:{ip}", limit=5, window=3600)
    rate_limit(f"register:{body.email.lower()}", limit=3, window=3600)
    try:
        user = services.register_user(db, body.email, body.password, body.full_name, ip=ip)
    except services.ServiceError as e:
        raise _err(e)
    return {"status": "pending", "message":
            "Account created. An administrator will review and activate it shortly.",
            "user_id": str(user.id)}


@router.post("/auth/login", tags=["auth"])
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    ip = client_ip(request)
    rate_limit(f"login:{ip}", limit=10, window=300)
    rate_limit(f"login:{body.email.lower()}", limit=8, window=300)

    try:
        user = services.authenticate(db, body.email, body.password, ip=ip)

        pending = services.outstanding_consents(db, user)
        if pending:
            raise _err(services.ConsentRequired("Required agreements must be accepted."))

        services.effective_licence(db, user)   # authoritative licence gate

        device = services.enrol_device(
            db, user, public_key=body.device_public_key, device_name=body.device_name,
            os_name=body.os_name, app_version=body.app_version,
        )
        sess, token = services.start_session(
            db, user, device, takeover=body.takeover, totp_code=body.totp_code, ip=ip,
        )
        grant = services.issue_grant_for(db, sess)
    except services.ServiceError as e:
        raise _err(e)

    return {
        "session_token": token,
        "grant": grant,
        "device_id": str(device.id),
        "heartbeat_interval": settings.HEARTBEAT_INTERVAL_SECONDS,
        "offline_grace_seconds": settings.OFFLINE_GRACE_SECONDS,
        "mfa_enabled": bool(user.mfa and user.mfa.is_confirmed),
    }


@router.post("/session/heartbeat", tags=["session"])
def session_heartbeat(authorization: str | None = Header(default=None),
                      db: Session = Depends(get_db)):
    try:
        return services.heartbeat(db, _bearer(authorization))
    except services.ServiceError as e:
        raise _err(e)


@router.post("/session/logout", tags=["session"])
def logout(sess=Depends(current_session), db: Session = Depends(get_db)):
    services.end_session(db, sess)
    return {"status": "logged_out"}


# ══════════════════════════════════════════════════════════════════════════
# MFA
# ══════════════════════════════════════════════════════════════════════════
@router.post("/mfa/enrol", tags=["mfa"])
def mfa_enrol(sess=Depends(current_session), db: Session = Depends(get_db)):
    user = db.get(User, sess.user_id)
    try:
        secret, uri = services.begin_mfa_enrolment(db, user)
    except services.ServiceError as e:
        raise _err(e)
    # The secret is shown once, during setup, so the authenticator can be
    # provisioned. It is never logged and never returned again afterwards.
    return {"secret": secret, "otpauth_uri": uri}


@router.post("/mfa/confirm", tags=["mfa"])
def mfa_confirm(body: MfaConfirmIn, request: Request,
                sess=Depends(current_session), db: Session = Depends(get_db)):
    rate_limit(f"mfa:{client_ip(request)}", limit=10, window=300)
    user = db.get(User, sess.user_id)
    try:
        codes = services.confirm_mfa_enrolment(db, user, body.code)
    except services.ServiceError as e:
        raise _err(e)
    return {"status": "enabled", "recovery_codes": codes,
            "notice": "Store these now. They are not retrievable later."}


# ══════════════════════════════════════════════════════════════════════════
# Consent
# ══════════════════════════════════════════════════════════════════════════
@router.get("/consent/required", tags=["consent"])
def consent_required(email: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email.strip().lower()).one_or_none()
    if user is None:
        # Same shape regardless of existence — no account enumeration.
        return {"outstanding": [{"document": d.value, "version": v}
                                for d, v in services.REQUIRED_CONSENTS.items()]}
    return {"outstanding": services.outstanding_consents(db, user)}


@router.post("/consent/accept", tags=["consent"])
def consent_accept(body: ConsentIn, email: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email.strip().lower()).one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Unknown account."})
    try:
        doc = ConsentDocument(body.document)
    except ValueError:
        raise HTTPException(status_code=400, detail={"code": "BAD_DOCUMENT", "message": "Unknown document."})
    services.record_consent(db, user, doc, body.version, body.accepted, body.app_version)
    return {"status": "recorded", "outstanding": services.outstanding_consents(db, user)}


# ══════════════════════════════════════════════════════════════════════════
# Public key distribution — lets the client pin the verification key
# ══════════════════════════════════════════════════════════════════════════
@router.get("/pubkey", tags=["ops"])
def pubkey():
    """The PUBLIC verification key only. Safe to serve; the private half never
    leaves the server."""
    return {"key_id": settings.SIGNING_KEY_ID, "public_key_b64": settings.SIGNING_PUBLIC_KEY_B64}
