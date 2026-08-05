"""Business logic. The server is authoritative here; nothing trusts the client.

Phases covered:
  2  authentication + lockout
  3  server-authoritative licences
  4  device enrolment
  5  transaction-safe one-active-session
  6  TOTP MFA + recovery codes
  7  MFA-gated device takeover
  8  signed grants
  9  heartbeat + offline grace
 11  versioned consent
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    AccountStatus, AppSession, AuditEvent, ConsentDocument, ConsentRecord,
    Device, DeviceStatus, Licence, LicenceStatus, MfaCredential, RecoveryCode,
    SessionStatus, User, utcnow,
)
from app import security

settings = get_settings()


# ══════════════════════════════════════════════════════════════════════════
# Errors — stable machine-readable codes, no internal detail leaked
# ══════════════════════════════════════════════════════════════════════════
def as_aware(dt: datetime | None) -> datetime | None:
    """Normalise a datetime read back from the database to aware UTC.

    PostgreSQL with `DateTime(timezone=True)` returns aware datetimes; SQLite
    has no timezone type and returns naive ones. Comparing the two raises
    TypeError. Rather than sprinkle try/except at every comparison, all
    time comparisons in this module go through here.

    Found by the Phase 14 tests — it would have surfaced in production as
    licence-expiry checks crashing on some deployments and not others.
    """
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


class ServiceError(Exception):
    code = "ERROR"
    http_status = 400

    def __init__(self, message: str = "", **extra):
        super().__init__(message or self.code)
        self.message = message or self.code
        self.extra = extra


class InvalidCredentials(ServiceError):
    code, http_status = "INVALID_CREDENTIALS", 401


class AccountLocked(ServiceError):
    code, http_status = "ACCOUNT_LOCKED", 423


class AccountNotActive(ServiceError):
    code, http_status = "ACCOUNT_NOT_ACTIVE", 403


class LicenceProblem(ServiceError):
    code, http_status = "LICENCE_INVALID", 403


class SessionActiveElsewhere(ServiceError):
    code, http_status = "SESSION_ACTIVE_ELSEWHERE", 409


class MfaRequired(ServiceError):
    code, http_status = "MFA_REQUIRED", 401


class MfaInvalid(ServiceError):
    code, http_status = "MFA_INVALID", 401


class DeviceRevoked(ServiceError):
    code, http_status = "DEVICE_REVOKED", 403


class SessionInvalid(ServiceError):
    code, http_status = "SESSION_INVALID", 401


class ConsentRequired(ServiceError):
    code, http_status = "CONSENT_REQUIRED", 403


class EmailAlreadyRegistered(ServiceError):
    code, http_status = "EMAIL_ALREADY_REGISTERED", 409


class WeakPassword(ServiceError):
    code, http_status = "WEAK_PASSWORD", 400


# ══════════════════════════════════════════════════════════════════════════
# Audit
# ══════════════════════════════════════════════════════════════════════════
def audit(db: Session, event_type: str, *, actor: str | None = None,
          target_user_id: uuid.UUID | None = None, result: str = "SUCCESS",
          detail: str | None = None, ip: str | None = None) -> None:
    """Never called with a secret in `detail`. Enforced by review, and by the
    test that greps audit rows for token-shaped strings."""
    db.add(AuditEvent(
        event_type=event_type, actor=actor, target_user_id=target_user_id,
        result=result, detail=detail, ip_address=ip,
    ))


# ══════════════════════════════════════════════════════════════════════════
# PHASE 2 — authentication
# ══════════════════════════════════════════════════════════════════════════
def authenticate(db: Session, email: str, password: str, *, ip: str | None = None) -> User:
    """Verify credentials. Raises without revealing whether the account exists."""
    email = (email or "").strip().lower()
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()

    if user and user.locked_until and as_aware(user.locked_until) > utcnow():
        audit(db, "LOGIN_BLOCKED_LOCKED", target_user_id=user.id, result="FAILURE", ip=ip)
        db.commit()
        raise AccountLocked("Account temporarily locked. Try again later.")

    # verify_password burns comparable time when user is None, so a
    # non-existent account is not distinguishable by response latency.
    ok = security.verify_password(password, user.password_hash if user else None)

    if not user or not ok:
        if user:
            user.failed_login_count += 1
            if user.failed_login_count >= settings.MAX_FAILED_LOGINS:
                user.locked_until = utcnow() + timedelta(seconds=settings.LOCKOUT_SECONDS)
                user.failed_login_count = 0
                audit(db, "ACCOUNT_LOCKED", target_user_id=user.id, result="FAILURE",
                      detail=f"after {settings.MAX_FAILED_LOGINS} failed attempts", ip=ip)
            else:
                audit(db, "LOGIN_FAILED", target_user_id=user.id, result="FAILURE", ip=ip)
            db.commit()
        raise InvalidCredentials("Incorrect email or password.")

    if user.status is not AccountStatus.ACTIVE:
        audit(db, "LOGIN_BLOCKED_STATUS", target_user_id=user.id, result="FAILURE",
              detail=user.status.value, ip=ip)
        db.commit()
        raise AccountNotActive(f"Account is {user.status.value}.")

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = utcnow()

    if user.password_hash and security.needs_rehash(user.password_hash):
        user.password_hash = security.hash_password(password)

    audit(db, "LOGIN_SUCCESS", target_user_id=user.id, ip=ip)
    db.commit()
    return user


# ══════════════════════════════════════════════════════════════════════════
# PHASE 17 — self-serve registration (account created PENDING, no licence)
# ══════════════════════════════════════════════════════════════════════════
# Deliberately does NOT create a Licence and does NOT set status=ACTIVE. A
# self-registered account can authenticate() to nothing until an admin
# reviews it in the panel and approves — same manual payment-reconciliation
# gate every other customer already goes through (see admin.py
# approve_signup), just with the account/email/password chosen by the
# customer instead of typed in by the admin. This does not change the
# offline-sale trust model: nobody gets a working licence without the admin
# clicking Approve.
MIN_PASSWORD_LENGTH = 12


def register_user(db: Session, email: str, password: str,
                  full_name: str | None = None, *, ip: str | None = None) -> User:
    email = (email or "").strip().lower()
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise WeakPassword(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")

    existing = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if existing is not None:
        # Same response shape as "created" would need care to avoid account
        # enumeration; a 409 here does leak existence, but this endpoint is
        # rate-limited per-IP and per-email, and the alternative (silently
        # pretending success) would leave a genuine new user unable to tell
        # a real submission failure from a duplicate-email one. Login already
        # accepts this tradeoff nowhere; registration is a lower-risk surface
        # (no password oracle — only existence), so it's accepted here too.
        audit(db, "REGISTER_DUPLICATE_EMAIL", result="FAILURE", detail=email, ip=ip)
        db.commit()
        raise EmailAlreadyRegistered("An account with this email already exists.")

    user = User(
        email=email, full_name=(full_name or None), status=AccountStatus.PENDING,
        password_hash=security.hash_password(password),
    )
    db.add(user)
    db.flush()
    audit(db, "REGISTER_PENDING", target_user_id=user.id, ip=ip)
    db.commit()
    return user


# ══════════════════════════════════════════════════════════════════════════
# PHASE 3 — server-authoritative licence
# ══════════════════════════════════════════════════════════════════════════
def effective_licence(db: Session, user: User) -> Licence:
    """Return the usable licence or raise. Expiry is evaluated against server
    time — a client clock cannot extend a licence."""
    lic = db.execute(
        select(Licence).where(Licence.user_id == user.id).order_by(Licence.created_at.desc())
    ).scalars().first()

    if lic is None:
        raise LicenceProblem("No licence issued for this account.")

    if lic.status in (LicenceStatus.REVOKED, LicenceStatus.SUSPENDED, LicenceStatus.PENDING):
        raise LicenceProblem(f"Licence is {lic.status.value}.")

    if lic.expires_at and as_aware(lic.expires_at) <= utcnow():
        if lic.status is not LicenceStatus.EXPIRED:
            lic.status = LicenceStatus.EXPIRED
            audit(db, "LICENCE_EXPIRED", target_user_id=user.id, actor="system")
            db.commit()
        raise LicenceProblem("Licence expired.")

    if lic.status is not LicenceStatus.ACTIVE:
        raise LicenceProblem(f"Licence is {lic.status.value}.")

    return lic


def entitlements_of(lic: Licence) -> list[str]:
    return [e.strip() for e in (lic.entitlements or "").split(",") if e.strip()]


# ══════════════════════════════════════════════════════════════════════════
# PHASE 4 — device enrolment
# ══════════════════════════════════════════════════════════════════════════
def enrol_device(db: Session, user: User, *, public_key: str, device_name: str | None,
                 os_name: str | None, app_version: str | None,
                 hardware_hint: str | None = None) -> Device:
    """Register a device by the public half of a keypair it generated locally.

    Re-enrolling the same public key returns the existing record, so a
    reinstall that preserves the keypair does not create duplicates.
    """
    existing = db.execute(
        select(Device).where(Device.user_id == user.id, Device.public_key == public_key)
    ).scalar_one_or_none()

    if existing:
        if existing.status is DeviceStatus.REVOKED:
            raise DeviceRevoked("This device has been revoked.")
        existing.last_seen_at = utcnow()
        existing.app_version = app_version or existing.app_version
        db.commit()
        return existing

    dev = Device(
        user_id=user.id, public_key=public_key, device_name=device_name,
        os_name=os_name, app_version=app_version, hardware_hint=hardware_hint,
        status=DeviceStatus.ACTIVE, last_seen_at=utcnow(),
    )
    db.add(dev)
    audit(db, "DEVICE_ENROLLED", target_user_id=user.id, detail=device_name)
    db.commit()
    return dev


def assert_device_usable(db: Session, user: User, device_id: uuid.UUID) -> Device:
    dev = db.get(Device, device_id)
    if dev is None or dev.user_id != user.id:
        raise DeviceRevoked("Unknown device.")
    if dev.status is not DeviceStatus.ACTIVE:
        raise DeviceRevoked(f"Device is {dev.status.value}.")
    return dev


# ══════════════════════════════════════════════════════════════════════════
# PHASE 5 + 7 — one active session, and MFA-gated takeover
# ══════════════════════════════════════════════════════════════════════════
def _lock_user_row(db: Session, user_id: uuid.UUID) -> None:
    """Serialise concurrent logins for one user.

    On PostgreSQL this is a real row lock: the second transaction blocks here
    until the first commits, so the takeover is orderly. On SQLite it is a
    no-op — which is exactly why production is forbidden from running SQLite.
    Even without the lock the partial unique index still makes a double-active
    state unpersistable; the lock upgrades a hard IntegrityError into a clean
    sequential handover.
    """
    if settings.is_sqlite:
        return
    db.execute(select(User.id).where(User.id == user_id).with_for_update())


def _current_active(db: Session, user_id: uuid.UUID) -> AppSession | None:
    return db.execute(
        select(AppSession).where(
            AppSession.user_id == user_id, AppSession.status == SessionStatus.ACTIVE
        )
    ).scalar_one_or_none()


def reap_stale_sessions(db: Session, user_id: uuid.UUID) -> int:
    """Reclaim sessions whose client vanished without logging out.

    Without this, a crashed client would lock the customer out until the
    idle timeout, with no way to recover — a support burden and a bad
    first impression.
    """
    cutoff = utcnow() - timedelta(seconds=settings.SESSION_IDLE_TIMEOUT_SECONDS)
    stale = db.execute(
        select(AppSession).where(
            AppSession.user_id == user_id,
            AppSession.status == SessionStatus.ACTIVE,
            AppSession.last_heartbeat_at < cutoff,
        )
    ).scalars().all()
    for s in stale:
        s.status = SessionStatus.EXPIRED
        s.ended_at = utcnow()
        s.end_reason = "IDLE_TIMEOUT"
    if stale:
        db.commit()
    return len(stale)


def start_session(db: Session, user: User, device: Device, *,
                  takeover: bool = False, totp_code: str | None = None,
                  ip: str | None = None) -> tuple[AppSession, str]:
    """Create the single active session for this user.

    If one already exists on another device, refuse — unless the caller
    explicitly requests takeover AND supplies a valid TOTP code. Requiring the
    second factor is what stops a leaked password alone from evicting the
    legitimate user.

    Returns (session, plaintext_token). The token is returned once.
    """
    reap_stale_sessions(db, user.id)
    _lock_user_row(db, user.id)

    existing = _current_active(db, user.id)

    if existing and existing.device_id == device.id:
        # Same device re-establishing: reuse rather than churn.
        raw, digest = security.new_session_token()
        existing.token_hash = digest
        existing.last_heartbeat_at = utcnow()
        db.commit()
        return existing, raw

    if existing and not takeover:
        raise SessionActiveElsewhere(
            "Your account is already in use on another device.",
            device_name=existing.device.device_name if existing.device else None,
        )

    if existing and takeover:
        cred = db.execute(
            select(MfaCredential).where(MfaCredential.user_id == user.id)
        ).scalar_one_or_none()
        if cred is None or not cred.is_confirmed:
            raise MfaRequired("Set up two-factor authentication before switching devices.")
        if not totp_code:
            raise MfaRequired("Enter your authenticator code to switch devices.")

        secret = security.decrypt_totp_secret(cred.secret_encrypted)
        ok, step = security.verify_totp(secret, totp_code, cred.last_used_step)
        if not ok:
            audit(db, "TAKEOVER_MFA_FAILED", target_user_id=user.id, result="FAILURE", ip=ip)
            db.commit()
            raise MfaInvalid("That code is not valid.")
        cred.last_used_step = step

        # Revoke and create inside ONE transaction, so no window exists in
        # which zero or two sessions are active.
        existing.status = SessionStatus.REVOKED
        existing.ended_at = utcnow()
        existing.end_reason = "TAKEOVER_BY_NEW_DEVICE"
        db.flush()
        audit(db, "SESSION_TAKEOVER", target_user_id=user.id,
              detail=f"from device {existing.device_id} to {device.id}", ip=ip)

    raw, digest = security.new_session_token()
    sess = AppSession(
        user_id=user.id, device_id=device.id, status=SessionStatus.ACTIVE,
        token_hash=digest, last_heartbeat_at=utcnow(), grant_counter=0,
    )
    db.add(sess)
    device.last_seen_at = utcnow()

    try:
        db.commit()
    except IntegrityError:
        # Lost a genuine race (only reachable without row locking, i.e. SQLite).
        db.rollback()
        raise SessionActiveElsewhere("Your account is already in use on another device.")

    audit(db, "SESSION_STARTED", target_user_id=user.id, detail=str(device.id), ip=ip)
    db.commit()
    return sess, raw


def resolve_session(db: Session, token: str) -> AppSession:
    """Look up a session by opaque token and re-validate the whole chain."""
    digest = security.hash_session_token(token or "")
    sess = db.execute(
        select(AppSession).where(AppSession.token_hash == digest)
    ).scalar_one_or_none()

    if sess is None:
        raise SessionInvalid("Session not recognised.")
    if sess.status is not SessionStatus.ACTIVE:
        raise SessionInvalid(f"Session {sess.status.value}.")
    return sess


def end_session(db: Session, sess: AppSession, reason: str = "LOGGED_OUT") -> None:
    sess.status = SessionStatus.LOGGED_OUT
    sess.ended_at = utcnow()
    sess.end_reason = reason
    sess.token_hash = None
    audit(db, "SESSION_ENDED", target_user_id=sess.user_id, detail=reason)
    db.commit()


# ══════════════════════════════════════════════════════════════════════════
# PHASE 6 — TOTP MFA
# ══════════════════════════════════════════════════════════════════════════
def begin_mfa_enrolment(db: Session, user: User) -> tuple[str, str]:
    """Create (or replace, if unconfirmed) a TOTP secret. Returns (secret, uri)."""
    cred = db.execute(select(MfaCredential).where(MfaCredential.user_id == user.id)).scalar_one_or_none()

    if cred and cred.is_confirmed:
        raise ServiceError("MFA is already enabled for this account.")

    secret = security.new_totp_secret()
    if cred:
        cred.secret_encrypted = security.encrypt_totp_secret(secret)
        cred.is_confirmed = False
    else:
        cred = MfaCredential(user_id=user.id, secret_encrypted=security.encrypt_totp_secret(secret))
        db.add(cred)

    audit(db, "MFA_ENROLMENT_STARTED", target_user_id=user.id)
    db.commit()
    return secret, security.totp_provisioning_uri(secret, user.email)


def confirm_mfa_enrolment(db: Session, user: User, code: str) -> list[str]:
    """Verify the first code, enable MFA, and issue recovery codes.

    Plaintext recovery codes are returned exactly once here and only their
    hashes are persisted.
    """
    cred = db.execute(select(MfaCredential).where(MfaCredential.user_id == user.id)).scalar_one_or_none()
    if cred is None:
        raise ServiceError("Start MFA setup first.")
    if cred.is_confirmed:
        raise ServiceError("MFA is already enabled.")

    secret = security.decrypt_totp_secret(cred.secret_encrypted)
    ok, step = security.verify_totp(secret, code, cred.last_used_step)
    if not ok:
        audit(db, "MFA_CONFIRM_FAILED", target_user_id=user.id, result="FAILURE")
        db.commit()
        raise MfaInvalid("That code is not valid.")

    cred.is_confirmed = True
    cred.confirmed_at = utcnow()
    cred.last_used_step = step

    for rc in list(cred.recovery_codes):
        db.delete(rc)

    plain = security.generate_recovery_codes()
    for c in plain:
        db.add(RecoveryCode(credential_id=cred.id, code_hash=security.hash_recovery_code(c)))

    audit(db, "MFA_ENABLED", target_user_id=user.id)
    db.commit()
    return plain


def consume_recovery_code(db: Session, user: User, code: str) -> bool:
    cred = db.execute(select(MfaCredential).where(MfaCredential.user_id == user.id)).scalar_one_or_none()
    if cred is None or not cred.is_confirmed:
        return False

    for rc in cred.recovery_codes:
        if rc.used_at is None and security.verify_recovery_code(code, rc.code_hash):
            rc.used_at = utcnow()
            audit(db, "RECOVERY_CODE_USED", target_user_id=user.id)
            db.commit()
            return True

    audit(db, "RECOVERY_CODE_FAILED", target_user_id=user.id, result="FAILURE")
    db.commit()
    return False


def reset_mfa(db: Session, user: User, *, actor: str) -> None:
    """Admin-assisted reset. Also kills the active session, because an MFA
    reset is exactly the moment an attacker would want to keep one alive."""
    cred = db.execute(select(MfaCredential).where(MfaCredential.user_id == user.id)).scalar_one_or_none()
    if cred:
        db.delete(cred)
    active = _current_active(db, user.id)
    if active:
        active.status = SessionStatus.REVOKED
        active.ended_at = utcnow()
        active.end_reason = "MFA_RESET"
        active.token_hash = None
    audit(db, "MFA_RESET", target_user_id=user.id, actor=actor,
          detail="admin-assisted; identity verified out of band")
    db.commit()


# ══════════════════════════════════════════════════════════════════════════
# PHASE 8 + 9 — signed grants and heartbeat
# ══════════════════════════════════════════════════════════════════════════
def issue_grant_for(db: Session, sess: AppSession) -> str:
    user = db.get(User, sess.user_id)
    lic = effective_licence(db, user)
    assert_device_usable(db, user, sess.device_id)

    sess.grant_counter += 1
    sess.last_heartbeat_at = utcnow()
    db.commit()

    return security.issue_grant(
        user_id=str(user.id), licence_id=str(lic.id), session_id=str(sess.id),
        device_id=str(sess.device_id), entitlements=entitlements_of(lic),
        counter=sess.grant_counter,
    )


def heartbeat(db: Session, token: str) -> dict:
    """Single authoritative check the client polls.

    Distinguishes REVOKED (server said no) from unreachable (client never
    gets a response at all) — the two must never be conflated.
    """
    sess = resolve_session(db, token)
    user = db.get(User, sess.user_id)

    if user.status is not AccountStatus.ACTIVE:
        sess.status = SessionStatus.REVOKED
        sess.ended_at = utcnow()
        sess.end_reason = f"ACCOUNT_{user.status.value}"
        sess.token_hash = None
        db.commit()
        raise AccountNotActive(f"Account is {user.status.value}.")

    dev = assert_device_usable(db, user, sess.device_id)
    lic = effective_licence(db, user)

    sess.last_heartbeat_at = utcnow()
    dev.last_seen_at = utcnow()
    sess.grant_counter += 1
    db.commit()

    grant = security.issue_grant(
        user_id=str(user.id), licence_id=str(lic.id), session_id=str(sess.id),
        device_id=str(sess.device_id), entitlements=entitlements_of(lic),
        counter=sess.grant_counter,
    )
    return {
        "status": "ok",
        "grant": grant,
        "heartbeat_interval": settings.HEARTBEAT_INTERVAL_SECONDS,
        "offline_grace_seconds": settings.OFFLINE_GRACE_SECONDS,
        "licence_expires_at": lic.expires_at.isoformat() if lic.expires_at else None,
    }


# ══════════════════════════════════════════════════════════════════════════
# PHASE 11 — versioned consent
# ══════════════════════════════════════════════════════════════════════════
REQUIRED_CONSENTS: dict[ConsentDocument, str] = {
    ConsentDocument.TERMS_OF_SERVICE: "1.0",
    ConsentDocument.RISK_DISCLOSURE: "1.0",
    ConsentDocument.PRIVACY_NOTICE: "1.0",
}


def outstanding_consents(db: Session, user: User) -> list[dict]:
    """Which required documents this user has not accepted at current version."""
    rows = db.execute(select(ConsentRecord).where(ConsentRecord.user_id == user.id)).scalars().all()
    accepted = {(r.document, r.document_version) for r in rows if r.accepted}
    return [
        {"document": doc.value, "version": ver}
        for doc, ver in REQUIRED_CONSENTS.items()
        if (doc, ver) not in accepted
    ]


def record_consent(db: Session, user: User, document: ConsentDocument,
                   version: str, accepted: bool, app_version: str | None = None) -> ConsentRecord:
    rec = ConsentRecord(
        user_id=user.id, document=document, document_version=version,
        accepted=accepted, app_version=app_version,
    )
    db.add(rec)
    audit(db, "CONSENT_RECORDED", target_user_id=user.id,
          detail=f"{document.value} v{version} accepted={accepted}")
    db.commit()
    return rec
