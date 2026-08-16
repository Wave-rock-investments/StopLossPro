import pytest

from app import subscriptions as subs
from app.models import Plan, PaymentStatus, SubscriptionStatus
from app.payments import ManualPaymentProvider
from app.services import InvalidTransition


def _make_plan(db, **overrides):
    p = dict(plan_id="MONTHLY", name="Monthly", duration_days=30, price=49.0, currency="USD")
    p.update(overrides)
    plan = Plan(**p)
    db.add(plan)
    db.flush()
    return plan


def test_get_or_create_telegram_user_is_idempotent(db):
    tu1 = subs.get_or_create_telegram_user(db, telegram_user_id="111", username="alice")
    db.commit()
    tu2 = subs.get_or_create_telegram_user(db, telegram_user_id="111", username="alice_renamed")
    db.commit()
    assert tu1.id == tu2.id
    assert tu2.telegram_username == "alice_renamed"  # refreshed, not duplicated

    count = db.query(type(tu1)).filter_by(telegram_user_id="111").count()
    assert count == 1


def test_start_subscription_creates_pending_payment(db):
    plan = _make_plan(db)
    tu = subs.get_or_create_telegram_user(db, telegram_user_id="222")
    subscriber = subs.get_or_create_subscriber(db, tu)
    db.commit()

    provider = ManualPaymentProvider()
    subscription, payment = subs.start_subscription(db, subscriber, plan, provider=provider, actor="bot")
    db.commit()

    assert subscription.status == SubscriptionStatus.PENDING_PAYMENT
    assert payment.status == PaymentStatus.PENDING
    assert payment.provider == "manual"
    assert payment.provider_payment_id.startswith("manual-")


def test_confirm_payment_activates_and_sets_expiry(db):
    plan = _make_plan(db, duration_days=30)
    tu = subs.get_or_create_telegram_user(db, telegram_user_id="333")
    subscriber = subs.get_or_create_subscriber(db, tu)
    db.commit()

    provider = ManualPaymentProvider()
    subscription, payment = subs.start_subscription(db, subscriber, plan, provider=provider, actor="bot")
    db.commit()

    activated = subs.confirm_payment(db, payment, actor="admin:test")
    db.commit()

    assert activated.status == SubscriptionStatus.ACTIVE
    assert activated.expiry_date is not None
    assert activated.start_date is not None
    assert (activated.expiry_date - activated.start_date).days == 30
    assert subs.is_premium(db, "333") is True


def test_confirm_payment_is_idempotent(db):
    plan = _make_plan(db)
    tu = subs.get_or_create_telegram_user(db, telegram_user_id="444")
    subscriber = subs.get_or_create_subscriber(db, tu)
    db.commit()
    provider = ManualPaymentProvider()
    subscription, payment = subs.start_subscription(db, subscriber, plan, provider=provider, actor="bot")
    db.commit()

    subs.confirm_payment(db, payment, actor="admin:test")
    db.commit()
    first_expiry = subscription.expiry_date

    with pytest.raises(subs.PaymentAlreadyProcessed):
        subs.confirm_payment(db, payment, actor="admin:test")
    db.commit()

    assert subscription.expiry_date == first_expiry  # not extended twice


def test_is_premium_false_for_unknown_user(db):
    assert subs.is_premium(db, "does-not-exist") is False


def test_revoke_subscription(db):
    plan = _make_plan(db)
    tu = subs.get_or_create_telegram_user(db, telegram_user_id="555")
    subscriber = subs.get_or_create_subscriber(db, tu)
    db.commit()
    provider = ManualPaymentProvider()
    subscription, payment = subs.start_subscription(db, subscriber, plan, provider=provider, actor="bot")
    subs.confirm_payment(db, payment, actor="admin:test")
    db.commit()
    assert subs.is_premium(db, "555") is True

    subs.revoke_subscription(db, subscription, actor="admin:test", reason="chargeback")
    db.commit()
    assert subscription.status == SubscriptionStatus.REVOKED
    assert subs.is_premium(db, "555") is False


def test_revoke_from_terminal_state_rejected(db):
    plan = _make_plan(db)
    tu = subs.get_or_create_telegram_user(db, telegram_user_id="666")
    subscriber = subs.get_or_create_subscriber(db, tu)
    db.commit()
    provider = ManualPaymentProvider()
    subscription, payment = subs.start_subscription(db, subscriber, plan, provider=provider, actor="bot")
    subs.confirm_payment(db, payment, actor="admin:test")
    subs.revoke_subscription(db, subscription, actor="admin:test")
    db.commit()

    with pytest.raises(InvalidTransition):
        subs.revoke_subscription(db, subscription, actor="admin:test")


def test_renewal_count_increments_only_on_actual_renewal(db):
    plan = _make_plan(db, duration_days=30)
    tu = subs.get_or_create_telegram_user(db, telegram_user_id="777")
    subscriber = subs.get_or_create_subscriber(db, tu)
    db.commit()
    provider = ManualPaymentProvider()

    subscription, payment1 = subs.start_subscription(db, subscriber, plan, provider=provider, actor="bot")
    subs.confirm_payment(db, payment1, actor="admin:test")
    db.commit()
    assert subscriber.renewal_count == 0  # first activation isn't a "renewal"

    subscription2, payment2 = subs.renew_subscription(db, subscription, provider=provider, actor="bot")
    subs.confirm_payment(db, payment2, actor="admin:test")
    db.commit()
    assert subscriber.renewal_count == 1


def test_mark_expiring_soon_and_expire(db):
    import datetime as dt
    plan = _make_plan(db, duration_days=1)
    tu = subs.get_or_create_telegram_user(db, telegram_user_id="888")
    subscriber = subs.get_or_create_subscriber(db, tu)
    db.commit()
    provider = ManualPaymentProvider()
    subscription, payment = subs.start_subscription(db, subscriber, plan, provider=provider, actor="bot")
    subs.confirm_payment(db, payment, actor="admin:test")
    db.commit()

    # Force it into the "expiring soon" window without waiting a real day.
    subscription.expiry_date = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
    db.commit()

    soon = subs.mark_expiring_soon(db, within_days=3)
    db.commit()
    assert subscription in soon
    assert subscription.status == SubscriptionStatus.EXPIRING_SOON

    subscription.expiry_date = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    db.commit()
    expired = subs.expire_subscriptions(db)
    db.commit()
    assert subscription in expired
    assert subscription.status == SubscriptionStatus.EXPIRED
    assert subs.is_premium(db, "888") is False
