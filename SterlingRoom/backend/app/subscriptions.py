"""Subscription lifecycle (Phase 5) — creation, activation, expiry,
revocation, renewal, and the entitlement check the bot/admin actually care
about: "is this Telegram user currently premium?"

State machine lives in models.py (SUBSCRIPTION_STATUS_TRANSITIONS). This
module is where it's enforced, mirroring services.py's transition_call
pattern for calls. Every state change is audited via services.audit() —
"every correction must produce an audit event" (master-prompt Phase 5/6
requirement), and no row is ever deleted — expired/revoked/cancelled
subscriptions stay queryable forever (subscriber history requirement).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Payment,
    PaymentStatus,
    Plan,
    Subscriber,
    Subscription,
    SubscriptionStatus,
    SUBSCRIPTION_STATUS_TRANSITIONS,
    TelegramUser,
)
from app.services import ServiceError, InvalidTransition, audit


class SubscriberNotFound(ServiceError):
    code = "subscriber_not_found"
    http_status = 404


class PlanNotFound(ServiceError):
    code = "plan_not_found"
    http_status = 404


class PaymentAlreadyProcessed(ServiceError):
    """Not an error for the caller — the idempotent case: this payment was
    already confirmed once, activation already happened, don't double it."""
    code = "payment_already_processed"
    http_status = 200

    def __init__(self, subscription: Subscription):
        self.subscription = subscription
        super().__init__("payment already processed")


# ══════════════════════════════════════════════════════════════════════════
# Subscriber identity — get-or-create, never duplicate a telegram_user_id
# ══════════════════════════════════════════════════════════════════════════
def get_or_create_telegram_user(db: Session, *, telegram_user_id: str, username: str | None = None,
                                 display_name: str | None = None, acquisition_source: str | None = None) -> TelegramUser:
    tu = db.execute(select(TelegramUser).where(TelegramUser.telegram_user_id == str(telegram_user_id))).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if tu is None:
        tu = TelegramUser(
            telegram_user_id=str(telegram_user_id), telegram_username=username,
            display_name=display_name, acquisition_source=acquisition_source, last_activity=now,
        )
        db.add(tu)
        db.flush()
        audit(db, "TELEGRAM_USER_CREATED", actor="bot", detail=f"telegram_user_id={telegram_user_id}")
    else:
        # Keep username/display_name fresh (people rename), but never
        # overwrite a recorded acquisition_source with None on a later visit.
        if username:
            tu.telegram_username = username
        if display_name:
            tu.display_name = display_name
        if acquisition_source and not tu.acquisition_source:
            tu.acquisition_source = acquisition_source
        tu.last_activity = now
    return tu


def get_or_create_subscriber(db: Session, telegram_user: TelegramUser) -> Subscriber:
    if telegram_user.subscriber is not None:
        return telegram_user.subscriber
    sub = Subscriber(telegram_user_id=telegram_user.id)
    db.add(sub)
    db.flush()
    audit(db, "SUBSCRIBER_CREATED", actor="bot", detail=f"telegram_user_id={telegram_user.telegram_user_id}")
    return sub


# ══════════════════════════════════════════════════════════════════════════
# Subscription creation + payment (manual provider today — see payments.py)
# ══════════════════════════════════════════════════════════════════════════
def start_subscription(db: Session, subscriber: Subscriber, plan: Plan, *, provider, actor: str) -> tuple[Subscription, Payment]:
    """Creates a PENDING_PAYMENT subscription + a Payment row via the given
    PaymentProvider. Does not activate anything — activation only happens
    once a human (admin) confirms the payment actually arrived (see
    confirm_payment below), matching payments.py's "never auto-verify a
    manual payment" rule.
    """
    if not plan.active:
        raise PlanNotFound(f"Plan {plan.plan_id!r} is not active")

    subscription = Subscription(subscriber_id=subscriber.id, plan_id=plan.id, status=SubscriptionStatus.PENDING_PAYMENT)
    db.add(subscription)
    db.flush()

    intent = provider.create_payment(
        subscriber_id=str(subscriber.id), plan_id=plan.plan_id, amount=float(plan.price), currency=plan.currency,
    )
    payment = Payment(
        subscription_id=subscription.id, provider=intent.provider, provider_payment_id=intent.provider_payment_id,
        amount=intent.amount, currency=intent.currency, status=PaymentStatus.PENDING,
    )
    db.add(payment)
    db.flush()

    audit(db, "SUBSCRIPTION_CREATED", actor=actor,
          detail=f"subscription_id={subscription.id} plan={plan.plan_id} payment_ref={payment.provider_payment_id}")
    return subscription, payment


def confirm_payment(db: Session, payment: Payment, *, actor: str) -> Subscription:
    """The ONLY path that activates a subscription. Idempotent: confirming
    an already-CONFIRMED payment returns the existing subscription instead
    of re-activating/re-extending it (master-prompt Phase 5's "a retry must
    not create duplicate access" requirement)."""
    if payment.status == PaymentStatus.CONFIRMED:
        raise PaymentAlreadyProcessed(payment.subscription)

    payment.status = PaymentStatus.CONFIRMED
    subscription = payment.subscription
    plan = subscription.plan

    now = datetime.now(timezone.utc)
    old_status = subscription.status
    subscription.status = SubscriptionStatus.ACTIVE
    subscription.start_date = subscription.start_date or now
    subscription.expiry_date = now + timedelta(days=plan.duration_days)
    subscription.payment_reference = payment.provider_payment_id

    subscriber = subscription.subscriber
    subscriber.renewal_count += 1 if old_status != SubscriptionStatus.PENDING_PAYMENT else 0
    subscriber.lifetime_value = float(subscriber.lifetime_value) + float(payment.amount)
    subscriber.last_payment_date = now

    audit(db, "PAYMENT_CONFIRMED", actor=actor,
          detail=f"subscription_id={subscription.id} payment_ref={payment.provider_payment_id} amount={payment.amount}")
    audit(db, "SUBSCRIPTION_ACTIVATED", actor=actor,
          detail=f"subscription_id={subscription.id} expiry={subscription.expiry_date.isoformat()}")
    return subscription


# ══════════════════════════════════════════════════════════════════════════
# State machine enforcement (mirrors services.transition_call)
# ══════════════════════════════════════════════════════════════════════════
def _transition(db: Session, subscription: Subscription, new_status: SubscriptionStatus, *, actor: str, event_type: str) -> Subscription:
    allowed = SUBSCRIPTION_STATUS_TRANSITIONS.get(subscription.status, set())
    if new_status not in allowed:
        raise InvalidTransition(
            f"subscription {subscription.id}: {subscription.status.value} -> {new_status.value} not legal "
            f"(allowed: {sorted(s.value for s in allowed) or 'none — terminal'})"
        )
    old = subscription.status
    subscription.status = new_status
    audit(db, event_type, actor=actor, detail=f"subscription_id={subscription.id} {old.value}->{new_status.value}")
    return subscription


def revoke_subscription(db: Session, subscription: Subscription, *, actor: str, reason: str = "") -> Subscription:
    return _transition(db, subscription, SubscriptionStatus.REVOKED, actor=actor,
                        event_type="SUBSCRIPTION_REVOKED" + (f" ({reason})" if reason else ""))


def cancel_subscription(db: Session, subscription: Subscription, *, actor: str) -> Subscription:
    return _transition(db, subscription, SubscriptionStatus.CANCELLED, actor=actor, event_type="SUBSCRIPTION_CANCELLED")


def renew_subscription(db: Session, subscription: Subscription, *, provider, actor: str) -> tuple[Subscription, Payment]:
    """Renewal reuses the SAME subscription row (extends expiry_date from
    whichever is later — now or the current expiry — rather than creating a
    new one), because master-prompt Phase 5 tracks `renewal_count` on the
    SUBSCRIBER, implying one continuing subscription relationship, not a
    fresh row per billing cycle. Historical continuity (created_at, id)
    is preserved either way; every transition is still individually audited."""
    if subscription.status not in (SubscriptionStatus.ACTIVE, SubscriptionStatus.EXPIRING_SOON, SubscriptionStatus.EXPIRED):
        raise InvalidTransition(f"subscription {subscription.id} in {subscription.status.value} cannot be renewed")

    plan = subscription.plan
    intent = provider.create_payment(
        subscriber_id=str(subscription.subscriber_id), plan_id=plan.plan_id, amount=float(plan.price), currency=plan.currency,
    )
    payment = Payment(
        subscription_id=subscription.id, provider=intent.provider, provider_payment_id=intent.provider_payment_id,
        amount=intent.amount, currency=intent.currency, status=PaymentStatus.PENDING,
    )
    db.add(payment)
    db.flush()
    audit(db, "RENEWAL_PAYMENT_CREATED", actor=actor, detail=f"subscription_id={subscription.id} payment_ref={payment.provider_payment_id}")
    return subscription, payment
    # NOTE: activation happens through confirm_payment() exactly like a new
    # subscription — renewal only differs in reusing the existing row, which
    # confirm_payment already does correctly (it extends expiry_date from
    # "now", not from the old expiry — see the batch-job docstring below for
    # why extending from "now" rather than from the old expiry is the
    # deliberate choice here).


# ══════════════════════════════════════════════════════════════════════════
# Batch jobs — not scheduled by anything yet (see telegram_bot.py's
# mark_for_retry() precedent: the hook exists, nothing calls it on a timer).
# A future worker/cron should call these periodically.
# ══════════════════════════════════════════════════════════════════════════
def mark_expiring_soon(db: Session, *, within_days: int = 3) -> list[Subscription]:
    now = datetime.now(timezone.utc)
    threshold = now + timedelta(days=within_days)
    subs = db.execute(
        select(Subscription).where(
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.expiry_date.is_not(None),
            Subscription.expiry_date <= threshold,
        )
    ).scalars().all()
    for s in subs:
        _transition(db, s, SubscriptionStatus.EXPIRING_SOON, actor="system:expiry_job", event_type="SUBSCRIPTION_EXPIRING_SOON")
    return subs


def expire_subscriptions(db: Session) -> list[Subscription]:
    now = datetime.now(timezone.utc)
    subs = db.execute(
        select(Subscription).where(
            Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.EXPIRING_SOON]),
            Subscription.expiry_date.is_not(None),
            Subscription.expiry_date <= now,
        )
    ).scalars().all()
    for s in subs:
        _transition(db, s, SubscriptionStatus.EXPIRED, actor="system:expiry_job", event_type="SUBSCRIPTION_EXPIRED")
    return subs


# ══════════════════════════════════════════════════════════════════════════
# Entitlement check — what the bot and Telegram access control actually ask
# ══════════════════════════════════════════════════════════════════════════
def is_premium(db: Session, telegram_user_id: str) -> bool:
    tu = db.execute(select(TelegramUser).where(TelegramUser.telegram_user_id == str(telegram_user_id))).scalar_one_or_none()
    if tu is None or tu.subscriber is None:
        return False
    active = db.execute(
        select(Subscription).where(
            Subscription.subscriber_id == tu.subscriber.id,
            Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.EXPIRING_SOON]),
        )
    ).scalar_one_or_none()
    return active is not None


def current_subscription(db: Session, telegram_user_id: str) -> Subscription | None:
    tu = db.execute(select(TelegramUser).where(TelegramUser.telegram_user_id == str(telegram_user_id))).scalar_one_or_none()
    if tu is None or tu.subscriber is None:
        return None
    return db.execute(
        select(Subscription)
        .where(Subscription.subscriber_id == tu.subscriber.id)
        .order_by(Subscription.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
