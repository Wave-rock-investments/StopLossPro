"""Core data model — the seven tables of the Security MVP.

Design rules applied throughout:

* UUID primary keys, never sequential integers. Sequential IDs leak customer
  counts and invite enumeration (IDOR/BOLA).
* All timestamps are timezone-aware UTC.
* Status is a constrained enum, never a free-text string or a bare boolean.
* The server is authoritative. Nothing here trusts a value supplied by the
  desktop client.
* No secret is ever stored in plaintext: passwords are Argon2id hashes,
  recovery codes are hashes, TOTP secrets are encrypted at rest.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ══════════════════════════════════════════════════════════════════════════
# Enums
# ══════════════════════════════════════════════════════════════════════════
class AccountStatus(str, enum.Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    REVOKED = "REVOKED"
    CLOSED = "CLOSED"


class LicenceStatus(str, enum.Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    SUSPENDED = "SUSPENDED"
    REVOKED = "REVOKED"


class DeviceStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    REMOVED = "REMOVED"


class SessionStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    LOGGED_OUT = "LOGGED_OUT"


class ConsentDocument(str, enum.Enum):
    TERMS_OF_SERVICE = "TERMS_OF_SERVICE"
    RISK_DISCLOSURE = "RISK_DISCLOSURE"
    PRIVACY_NOTICE = "PRIVACY_NOTICE"


# ══════════════════════════════════════════════════════════════════════════
# 1. users
# ══════════════════════════════════════════════════════════════════════════
class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    full_name: Mapped[str | None] = mapped_column(String(200))

    # Argon2id hash. Never the password itself.
    password_hash: Mapped[str | None] = mapped_column(String(255))

    status: Mapped[AccountStatus] = mapped_column(
        SAEnum(AccountStatus, native_enum=False, length=20),
        default=AccountStatus.PENDING,
        nullable=False,
        index=True,
    )

    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    licences: Mapped[list[Licence]] = relationship(back_populates="user", cascade="all, delete-orphan")
    devices: Mapped[list[Device]] = relationship(back_populates="user", cascade="all, delete-orphan")
    sessions: Mapped[list[AppSession]] = relationship(back_populates="user", cascade="all, delete-orphan")
    mfa: Mapped[MfaCredential | None] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    consents: Mapped[list[ConsentRecord]] = relationship(back_populates="user", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("failed_login_count >= 0", name="ck_users_failed_login_nonneg"),
    )


# ══════════════════════════════════════════════════════════════════════════
# 2. licences
# ══════════════════════════════════════════════════════════════════════════
class Licence(Base):
    __tablename__ = "licences"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    product: Mapped[str] = mapped_column(String(50), default="STOPLOSSPRO", nullable=False)
    plan: Mapped[str] = mapped_column(String(50), default="STANDARD", nullable=False)

    status: Mapped[LicenceStatus] = mapped_column(
        SAEnum(LicenceStatus, native_enum=False, length=20),
        default=LicenceStatus.PENDING,
        nullable=False,
        index=True,
    )

    max_concurrent_sessions: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    # How the sale was verified. Free text on purpose — cash/crypto/manual.
    # Deliberately NOT a wallet address or txn hash: we keep no more payment
    # data than the business genuinely needs.
    activation_note: Mapped[str | None] = mapped_column(String(500))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="licences")

    __table_args__ = (
        CheckConstraint("max_concurrent_sessions >= 1", name="ck_licences_max_sessions_min1"),
    )


# ══════════════════════════════════════════════════════════════════════════
# 3. devices
# ══════════════════════════════════════════════════════════════════════════
class Device(Base):
    """A device is identified by a keypair it generates at enrolment.

    Hardware fingerprints are recorded ONLY as a weak risk signal. They are
    never the trust anchor — customers legitimately replace SSDs, NICs and
    motherboards, and the previous design locked them out when they did.
    """

    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Ed25519 public key (base64). The private half never leaves the client's
    # OS-protected storage (Windows DPAPI).
    public_key: Mapped[str] = mapped_column(Text, nullable=False)

    device_name: Mapped[str | None] = mapped_column(String(200))
    os_name: Mapped[str | None] = mapped_column(String(100))
    app_version: Mapped[str | None] = mapped_column(String(50))

    # Advisory only. Never used for authorization decisions.
    hardware_hint: Mapped[str | None] = mapped_column(String(128))

    status: Mapped[DeviceStatus] = mapped_column(
        SAEnum(DeviceStatus, native_enum=False, length=20),
        default=DeviceStatus.ACTIVE,
        nullable=False,
        index=True,
    )

    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="devices")
    sessions: Mapped[list[AppSession]] = relationship(back_populates="device")

    __table_args__ = (
        Index("ix_devices_user_status", "user_id", "status"),
    )


# ══════════════════════════════════════════════════════════════════════════
# 4. sessions  — the one-active-session invariant lives here
# ══════════════════════════════════════════════════════════════════════════
class AppSession(Base):
    """Named AppSession to avoid colliding with SQLAlchemy's Session.

    THE CRITICAL CONSTRAINT is `uq_sessions_one_active_per_user` below: a
    PARTIAL unique index over user_id, restricted to rows where status =
    'ACTIVE'. The database therefore makes a second concurrent active session
    physically unrepresentable — two racing logins cannot both commit, because
    the second violates a uniqueness constraint at the storage layer.

    Application-level checking ("does an active session already exist?") is
    NOT sufficient on its own: between the check and the insert, the other
    transaction can commit. The index closes that window. The row lock added
    in Phase 5 makes the losing side fail gracefully rather than with an
    integrity error.
    """

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )

    status: Mapped[SessionStatus] = mapped_column(
        SAEnum(SessionStatus, native_enum=False, length=20),
        default=SessionStatus.ACTIVE,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Why the session ended — shown in the admin panel and to the losing client.
    end_reason: Mapped[str | None] = mapped_column(String(100))

    # Monotonic counter for clock-rollback detection (Phase 9).
    grant_counter: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    user: Mapped[User] = relationship(back_populates="sessions")
    device: Mapped[Device] = relationship(back_populates="sessions")

    __table_args__ = (
        Index(
            "uq_sessions_one_active_per_user",
            "user_id",
            unique=True,
            sqlite_where=(status == SessionStatus.ACTIVE),
            postgresql_where=(status == SessionStatus.ACTIVE),
        ),
        Index("ix_sessions_status_heartbeat", "status", "last_heartbeat_at"),
    )


# ══════════════════════════════════════════════════════════════════════════
# 5. mfa_credentials
# ══════════════════════════════════════════════════════════════════════════
class MfaCredential(Base):
    __tablename__ = "mfa_credentials"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False, index=True
    )

    # TOTP shared secret, ENCRYPTED at rest (never plaintext, never logged).
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Replay protection: the last TOTP step consumed. A given code is accepted once.
    last_used_step: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="mfa")
    recovery_codes: Mapped[list[RecoveryCode]] = relationship(
        back_populates="credential", cascade="all, delete-orphan"
    )


class RecoveryCode(Base):
    """Single-use MFA fallback. Stored as a hash — the plaintext is shown to
    the customer exactly once, at enrolment, and never again."""

    __tablename__ = "recovery_codes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    credential_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("mfa_credentials.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    credential: Mapped[MfaCredential] = relationship(back_populates="recovery_codes")


# ══════════════════════════════════════════════════════════════════════════
# 6. consent_records
# ══════════════════════════════════════════════════════════════════════════
class ConsentRecord(Base):
    """Versioned proof of acceptance.

    Document TEXT is not stored here — only which version was accepted, when,
    and by whom. Actual wording is
    `[PLACEHOLDER — LAWYER REVIEW REQUIRED]` and lives in versioned files.
    """

    __tablename__ = "consent_records"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    document: Mapped[ConsentDocument] = mapped_column(
        SAEnum(ConsentDocument, native_enum=False, length=40), nullable=False
    )
    document_version: Mapped[str] = mapped_column(String(20), nullable=False)
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    app_version: Mapped[str | None] = mapped_column(String(50))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    user: Mapped[User] = relationship(back_populates="consents")

    __table_args__ = (
        Index("ix_consent_user_doc_version", "user_id", "document", "document_version"),
    )


# ══════════════════════════════════════════════════════════════════════════
# 7. audit_events
# ══════════════════════════════════════════════════════════════════════════
class AuditEvent(Base):
    """Append-only record of security-relevant actions.

    Never contains a password, TOTP secret, recovery code, or raw token.
    """

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    actor: Mapped[str | None] = mapped_column(String(200))       # "admin:<email>" or "user:<uuid>" or "system"
    target_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    result: Mapped[str] = mapped_column(String(20), default="SUCCESS", nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )

    __table_args__ = (
        Index("ix_audit_type_created", "event_type", "created_at"),
    )
