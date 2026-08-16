"""Sterling_Room data model.

Design rules, deliberately matching Working/backend/app/models.py's
established conventions in this repo (same discipline, separate database):

* UUID primary keys, never sequential integers — sequential IDs leak
  business volume (subscriber/call counts) and invite enumeration.
* `trade_id` (e.g. "SR-260816-001") is a separate, human-facing business key
  — unique and immutable, but not the primary key, so it can be validated
  and formatted independently of storage concerns.
* All timestamps are timezone-aware UTC.
* Status is always a constrained enum, never a free-text string or bool.
* The server is authoritative. A call is not distributed, and a subscription
  is not activated, on the strength of anything a client claims.
* Historical rows are never deleted. A closed call, an expired subscription,
  a cancelled ticket — all stay queryable, matching master-prompt §15/§33's
  "never delete losing trades / subscriber history" rules.
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
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ══════════════════════════════════════════════════════════════════════════
# Enums — the explicit state machines master-prompt §15/§20/§26 require
# ══════════════════════════════════════════════════════════════════════════
class CallStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    WATCH = "WATCH"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    TP1_HIT = "TP1_HIT"
    MANAGED = "MANAGED"
    BREAKEVEN = "BREAKEVEN"
    CLOSED = "CLOSED"
    STOPPED = "STOPPED"
    INVALIDATED = "INVALIDATED"
    CANCELLED = "CANCELLED"


# Legal transitions — enforced in services.py, not just documented here.
# CLOSED/STOPPED/INVALIDATED/CANCELLED are terminal: nothing leaves them.
CALL_STATUS_TRANSITIONS: dict[CallStatus, set[CallStatus]] = {
    CallStatus.DRAFT: {CallStatus.WATCH, CallStatus.APPROVED, CallStatus.CANCELLED},
    CallStatus.WATCH: {CallStatus.APPROVED, CallStatus.ACTIVE, CallStatus.INVALIDATED, CallStatus.CANCELLED},
    CallStatus.APPROVED: {CallStatus.ACTIVE, CallStatus.INVALIDATED, CallStatus.CANCELLED},
    CallStatus.ACTIVE: {CallStatus.TP1_HIT, CallStatus.MANAGED, CallStatus.BREAKEVEN, CallStatus.CLOSED, CallStatus.STOPPED, CallStatus.CANCELLED},
    CallStatus.TP1_HIT: {CallStatus.MANAGED, CallStatus.BREAKEVEN, CallStatus.CLOSED, CallStatus.STOPPED},
    CallStatus.MANAGED: {CallStatus.BREAKEVEN, CallStatus.CLOSED, CallStatus.STOPPED, CallStatus.TP1_HIT},
    CallStatus.BREAKEVEN: {CallStatus.CLOSED, CallStatus.STOPPED, CallStatus.MANAGED, CallStatus.TP1_HIT},
    CallStatus.CLOSED: set(),
    CallStatus.STOPPED: set(),
    CallStatus.INVALIDATED: set(),
    CallStatus.CANCELLED: set(),
}

TERMINAL_CALL_STATUSES = {
    CallStatus.CLOSED, CallStatus.STOPPED, CallStatus.INVALIDATED, CallStatus.CANCELLED,
}


class CallDirection(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class CallEventType(str, enum.Enum):
    CALL_CREATED = "CALL_CREATED"
    CALL_VALIDATED = "CALL_VALIDATED"
    CALL_APPROVED = "CALL_APPROVED"
    CALL_SENT = "CALL_SENT"
    CALL_UPDATED = "CALL_UPDATED"
    TP1_REACHED = "TP1_REACHED"
    SL_MOVED = "SL_MOVED"
    BREAKEVEN = "BREAKEVEN"
    CALL_CLOSED = "CALL_CLOSED"
    CALL_STOPPED = "CALL_STOPPED"
    CALL_INVALIDATED = "CALL_INVALIDATED"
    CALL_CANCELLED = "CALL_CANCELLED"
    MESSAGE_FAILED = "MESSAGE_FAILED"
    MESSAGE_RETRIED = "MESSAGE_RETRIED"


class MessageType(str, enum.Enum):
    WATCH = "WATCH"
    ENTRY = "ENTRY"
    UPDATE = "UPDATE"
    TP1 = "TP1"
    MANAGEMENT = "MANAGEMENT"
    BREAKEVEN = "BREAKEVEN"
    EXIT = "EXIT"
    INVALIDATED = "INVALIDATED"
    CANCELLED = "CANCELLED"
    RESULTS = "RESULTS"  # Phase 10 — automatic post to the Results channel on close
    FREE_ENTRY = "FREE_ENTRY"  # 2026-08-16 — sanitized, delayed teaser for the Free channel


class ChatRole(str, enum.Enum):
    FREE = "FREE"
    PREMIUM = "PREMIUM"
    RESULTS = "RESULTS"


class DeliveryStatus(str, enum.Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    RETRYING = "RETRYING"


class SubscriptionStatus(str, enum.Enum):
    PENDING_PAYMENT = "PENDING_PAYMENT"
    ACTIVE = "ACTIVE"
    EXPIRING_SOON = "EXPIRING_SOON"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    CANCELLED = "CANCELLED"


SUBSCRIPTION_STATUS_TRANSITIONS: dict[SubscriptionStatus, set[SubscriptionStatus]] = {
    SubscriptionStatus.PENDING_PAYMENT: {SubscriptionStatus.ACTIVE, SubscriptionStatus.CANCELLED},
    SubscriptionStatus.ACTIVE: {SubscriptionStatus.EXPIRING_SOON, SubscriptionStatus.EXPIRED, SubscriptionStatus.REVOKED, SubscriptionStatus.CANCELLED},
    SubscriptionStatus.EXPIRING_SOON: {SubscriptionStatus.ACTIVE, SubscriptionStatus.EXPIRED, SubscriptionStatus.REVOKED, SubscriptionStatus.CANCELLED},
    SubscriptionStatus.EXPIRED: {SubscriptionStatus.REVOKED, SubscriptionStatus.ACTIVE},  # renewal re-activates
    SubscriptionStatus.REVOKED: set(),
    SubscriptionStatus.CANCELLED: set(),
}


class PaymentStatus(str, enum.Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


class TicketCategory(str, enum.Enum):
    PAYMENT = "PAYMENT"
    SUBSCRIPTION = "SUBSCRIPTION"
    ACCESS = "ACCESS"
    RENEWAL = "RENEWAL"
    TECHNICAL = "TECHNICAL"
    TRADE_QUESTION = "TRADE_QUESTION"
    OTHER = "OTHER"


class TicketStatus(str, enum.Enum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


# ══════════════════════════════════════════════════════════════════════════
# 1. calls — the canonical Sterling_Room call object (master-prompt §17)
# ══════════════════════════════════════════════════════════════════════════
class Call(Base):
    __tablename__ = "calls"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    # Human-facing business key: SR-YYMMDD-NNN. Unique, immutable, never reused.
    trade_id: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)

    # Idempotency key supplied BY THE CALLER (e.g. a UUID4 the StopLossPro
    # adapter generates once per SHARE tap / order fill). A retry from a
    # flaky connection reuses the same source_call_id — the unique
    # constraint below makes a duplicate Trade ID for one real call
    # structurally impossible, not just unlikely (master-prompt §28).
    source_call_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(50), default="stoplosspro", nullable=False)

    instrument: Mapped[str] = mapped_column(String(20), nullable=False)
    direction: Mapped[CallDirection] = mapped_column(SAEnum(CallDirection, native_enum=False, length=10), nullable=False)

    entry_min: Mapped[float | None] = mapped_column(Numeric(18, 6))
    entry_max: Mapped[float | None] = mapped_column(Numeric(18, 6))
    stop_loss: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    tp1: Mapped[float | None] = mapped_column(Numeric(18, 6))
    tp2: Mapped[float | None] = mapped_column(Numeric(18, 6))
    tp3: Mapped[float | None] = mapped_column(Numeric(18, 6))

    risk_percent: Mapped[float | None] = mapped_column(Numeric(6, 3))
    setup_type: Mapped[str | None] = mapped_column(String(100))
    analysis: Mapped[str | None] = mapped_column(Text)
    invalidation: Mapped[str | None] = mapped_column(Text)

    # Optional event-risk metadata (master-prompt §65). Not a macro engine —
    # just a place to carry it if/when a source supplies it.
    event_risk: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    event_name: Mapped[str | None] = mapped_column(String(200))
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[CallStatus] = mapped_column(
        SAEnum(CallStatus, native_enum=False, length=20),
        default=CallStatus.DRAFT, nullable=False, index=True,
    )

    # Distribution routing — which channel classes this call goes to.
    # Deliberately NOT "every call -> every channel" (master-prompt §26).
    route_free: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    route_premium: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Freemium delivery timing (2026-08-16 production architecture): set at
    # creation time (created_at + settings.FREE_CALL_DELAY_SECONDS) only
    # when route_free is True — never recomputed later, so changing the
    # global delay setting doesn't retroactively move an already-scheduled
    # call. app/worker.py's process_delayed_free_calls job polls for calls
    # past this timestamp that don't yet have a FREE_ENTRY CallMessage row
    # and sends the sanitized teaser then. NULL means "not free-routed, or
    # free-routed calls created before this feature existed."
    free_call_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    result_r: Mapped[float | None] = mapped_column(Numeric(8, 3))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    events: Mapped[list[CallEvent]] = relationship(back_populates="call", cascade="all, delete-orphan", order_by="CallEvent.created_at")
    messages: Mapped[list[CallMessage]] = relationship(back_populates="call", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("stop_loss > 0", name="ck_calls_sl_positive"),
        Index("ix_calls_status_created", "status", "created_at"),
    )


# ══════════════════════════════════════════════════════════════════════════
# 2. call_events — append-only audit trail per call (master-prompt §31/§61)
# ══════════════════════════════════════════════════════════════════════════
class CallEvent(Base):
    __tablename__ = "call_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    call_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("calls.id", ondelete="CASCADE"), nullable=False, index=True)

    event_type: Mapped[CallEventType] = mapped_column(SAEnum(CallEventType, native_enum=False, length=30), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(200))  # "system", "admin:<email>", "adapter:<source>"
    old_status: Mapped[CallStatus | None] = mapped_column(SAEnum(CallStatus, native_enum=False, length=20))
    new_status: Mapped[CallStatus | None] = mapped_column(SAEnum(CallStatus, native_enum=False, length=20))
    detail: Mapped[str | None] = mapped_column(Text)  # free-text / JSON-encoded metadata

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)

    call: Mapped[Call] = relationship(back_populates="events")


# ══════════════════════════════════════════════════════════════════════════
# 3. call_messages — exactly what was sent to Telegram, and the result
# ══════════════════════════════════════════════════════════════════════════
class CallMessage(Base):
    __tablename__ = "call_messages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    call_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("calls.id", ondelete="CASCADE"), nullable=False, index=True)

    telegram_chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    telegram_message_id: Mapped[str | None] = mapped_column(String(64))
    message_type: Mapped[MessageType] = mapped_column(SAEnum(MessageType, native_enum=False, length=20), nullable=False)
    message_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # sha256, for dedup/trace
    # The exact text that was (or will be) sent — Phase 10. A background
    # retry runs in a separate process/request from the one that queued the
    # message, so it cannot re-render the original text from the Call's
    # CURRENT state (fields may have changed since, and transient kwargs
    # like update_text were never persisted anywhere else) — storing the
    # rendered text is what makes retry actually resend the same content,
    # not "whatever the call looks like now". Nullable because rows written
    # before this column existed have no text to retry with (see
    # app/telegram_bot.py::process_telegram_retries).
    message_text: Mapped[str | None] = mapped_column(Text)

    delivery_status: Mapped[DeliveryStatus] = mapped_column(
        SAEnum(DeliveryStatus, native_enum=False, length=20), default=DeliveryStatus.PENDING, nullable=False,
    )
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    # Persisted so retry backoff survives a worker restart (master-prompt
    # Phase 10: "safe after process restart") — an in-memory-only backoff
    # clock would reset to "immediately eligible" on every restart and
    # hammer Telegram instead of backing off.
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    call: Mapped[Call] = relationship(back_populates="messages")

    __table_args__ = (
        # Same (call, chat, type, content) never gets a second row created for
        # a NEW send — retries update the existing row instead (see services.py).
        UniqueConstraint("call_id", "telegram_chat_id", "message_type", "message_content_hash",
                          name="uq_call_messages_dedup"),
    )


# ══════════════════════════════════════════════════════════════════════════
# 4. telegram_chats — configured destination channels
# ══════════════════════════════════════════════════════════════════════════
class TelegramChat(Base):
    __tablename__ = "telegram_chats"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    role: Mapped[ChatRole] = mapped_column(SAEnum(ChatRole, native_enum=False, length=10), nullable=False)
    title: Mapped[str | None] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# ══════════════════════════════════════════════════════════════════════════
# 5. telegram_users — anyone who has interacted with the bot
# ══════════════════════════════════════════════════════════════════════════
class TelegramUser(Base):
    __tablename__ = "telegram_users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    telegram_user_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(100))
    display_name: Mapped[str | None] = mapped_column(String(200))
    acquisition_source: Mapped[str | None] = mapped_column(String(100))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_activity: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    subscriber: Mapped[Subscriber | None] = relationship(back_populates="telegram_user", uselist=False)


# ══════════════════════════════════════════════════════════════════════════
# 6. plans — configurable, never hardcoded prices (master-prompt §12)
# ══════════════════════════════════════════════════════════════════════════
class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    plan_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)  # e.g. "MONTHLY"
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="USD", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint("duration_days > 0", name="ck_plans_duration_positive"),
        CheckConstraint("price >= 0", name="ck_plans_price_nonneg"),
    )


# ══════════════════════════════════════════════════════════════════════════
# 7. subscribers
# ══════════════════════════════════════════════════════════════════════════
class Subscriber(Base):
    __tablename__ = "subscribers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    telegram_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("telegram_users.id", ondelete="CASCADE"), unique=True, nullable=False,
    )

    renewal_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lifetime_value: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    last_payment_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    telegram_user: Mapped[TelegramUser] = relationship(back_populates="subscriber")
    subscriptions: Mapped[list[Subscription]] = relationship(back_populates="subscriber", cascade="all, delete-orphan")


# ══════════════════════════════════════════════════════════════════════════
# 8. subscriptions — state machine lives here (master-prompt §15)
# ══════════════════════════════════════════════════════════════════════════
class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    subscriber_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("subscribers.id", ondelete="CASCADE"), nullable=False, index=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("plans.id"), nullable=False)

    status: Mapped[SubscriptionStatus] = mapped_column(
        SAEnum(SubscriptionStatus, native_enum=False, length=20),
        default=SubscriptionStatus.PENDING_PAYMENT, nullable=False, index=True,
    )

    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expiry_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    payment_reference: Mapped[str | None] = mapped_column(String(200))
    # Set once app/worker.py's subscription-lifecycle job successfully calls
    # telegram_access.revoke_premium_access() for this subscription — NULL
    # means "entitlement is gone (EXPIRED/REVOKED) but Telegram access has
    # not been confirmed pulled yet". Driving the worker off this column
    # (not off "just transitioned this tick") is what makes revocation safe
    # after a restart: a crash between the status transition and the
    # Telegram call leaves this NULL, so the next tick picks it back up
    # instead of silently never revoking access (master-prompt Phase 10).
    telegram_access_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    subscriber: Mapped[Subscriber] = relationship(back_populates="subscriptions")
    plan: Mapped[Plan] = relationship()
    payments: Mapped[list[Payment]] = relationship(back_populates="subscription", cascade="all, delete-orphan")


# ══════════════════════════════════════════════════════════════════════════
# 9. payments — schema exists now; no live provider wired yet.
#
# Per the 2026-08-16 decision ("build the abstraction first, choose a
# provider later"), this table and app/payments.py's PaymentProvider
# interface are real, but there is no Stripe/PayPal/etc. implementation
# behind it yet — see app/payments.py's module docstring.
# ══════════════════════════════════════════════════════════════════════════
class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    subscription_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False, index=True)

    provider: Mapped[str] = mapped_column(String(50), nullable=False)  # "manual", "stripe", ...
    provider_payment_id: Mapped[str | None] = mapped_column(String(200), unique=True)  # idempotency anchor for webhooks
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="USD", nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(SAEnum(PaymentStatus, native_enum=False, length=20), default=PaymentStatus.PENDING, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    subscription: Mapped[Subscription] = relationship(back_populates="payments")


# ══════════════════════════════════════════════════════════════════════════
# 10. support_tickets
# ══════════════════════════════════════════════════════════════════════════
class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    telegram_user_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    category: Mapped[TicketCategory] = mapped_column(SAEnum(TicketCategory, native_enum=False, length=20), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[TicketStatus] = mapped_column(SAEnum(TicketStatus, native_enum=False, length=20), default=TicketStatus.OPEN, nullable=False)
    assigned_to: Mapped[str | None] = mapped_column(String(200))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ══════════════════════════════════════════════════════════════════════════
# 11. audit_events — same shape as Working/backend's, own copy (separate DB)
# ══════════════════════════════════════════════════════════════════════════
class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    actor: Mapped[str | None] = mapped_column(String(200))
    result: Mapped[str] = mapped_column(String(20), default="SUCCESS", nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)

    __table_args__ = (
        Index("ix_sr_audit_type_created", "event_type", "created_at"),
    )


# ══════════════════════════════════════════════════════════════════════════
# 12. admin_users — Phase 7 admin dashboard auth.
#
# Deliberately its own table here (not bolted onto `users`, which doesn't
# even exist in this schema — Sterling_Room has no end-user login, only
# Telegram identity). Mirrors Working/backend's AdminUser shape (email +
# Argon2id password + TOTP, role-gated) closely enough that the same
# security review applies, but is defined directly in models.py rather than
# admin.py so Alembic autogenerate picks it up without the import-order
# workaround Working/backend/alembic/env.py needs.
# ══════════════════════════════════════════════════════════════════════════
class AdminRole(str, enum.Enum):
    SUPER_ADMIN = "SUPER_ADMIN"
    ADMIN = "ADMIN"
    ANALYST = "ANALYST"
    SUPPORT = "SUPPORT"
    VIEWER = "VIEWER"


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # TOTP is optional here (unlike Working/backend, where it's mandatory) —
    # deliberately not copying that requirement wholesale until Sterling_Room
    # actually has money moving through it via a real payment provider. Once
    # one is wired in (see app/payments.py), mandatory admin MFA should be
    # revisited — flagged, not silently downgraded.
    totp_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    totp_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    role: Mapped[str] = mapped_column(String(20), default=AdminRole.ADMIN.value, nullable=False)

    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint("failed_login_count >= 0", name="ck_admin_users_failed_login_nonneg"),
    )


# ══════════════════════════════════════════════════════════════════════════
# 13. telegram_update_log — webhook duplicate-delivery guard (Phase 4).
#
# Telegram's webhook contract explicitly allows redelivery (network hiccups,
# timeouts) — update_id is the thing it guarantees is stable and increasing.
# Recording each processed update_id here, with a primary-key uniqueness
# constraint, makes reprocessing the same update structurally impossible to
# do twice — the same "unique constraint, not a mutable flag" pattern this
# codebase already uses for call and payment idempotency.
# ══════════════════════════════════════════════════════════════════════════
class TelegramUpdateLog(Base):
    __tablename__ = "telegram_update_log"

    update_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
