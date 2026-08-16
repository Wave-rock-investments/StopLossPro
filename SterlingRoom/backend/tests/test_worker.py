"""Tests for the Phase 10 background worker (master-prompt "STERLING_ROOM
— FINAL PRE-LAUNCH HARDENING" §1/§2): Telegram retry, subscription
lifecycle + access revocation, results-channel automation, and
worker-restart idempotency.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("STERLING_DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("STERLING_ADAPTER_API_KEYS", "test-key-123")
os.environ.setdefault("STERLING_TELEGRAM_WEBHOOK_SECRET", "test-webhook-secret")

import pytest

from app import subscriptions as subs
from app import telegram_bot
from app import worker
from app.models import (
    Call, CallDirection, CallMessage, CallStatus, DeliveryStatus, MessageType, Plan,
)
from app.payments import ManualPaymentProvider
from app.telegram_access import AccessResult


# ══════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════
def _make_call(db, **overrides) -> Call:
    fields = dict(
        trade_id=f"SR-260816-{uuid.uuid4().hex[:3]}", source_call_id=uuid.uuid4().hex,
        instrument="XAUUSD", direction=CallDirection.SELL, stop_loss=1950.0,
        status=CallStatus.ACTIVE, route_premium=True,
    )
    fields.update(overrides)
    call = Call(**fields)
    db.add(call)
    db.flush()
    return call


def _make_failed_message(db, call, *, retry_count=0, last_attempt_at=None, message_text="hello", chat_id="-1002") -> CallMessage:
    msg = CallMessage(
        call_id=call.id, telegram_chat_id=chat_id, message_type=MessageType.ENTRY,
        message_content_hash=telegram_bot.content_hash(message_text) if message_text else "no-text-hash",
        message_text=message_text, delivery_status=DeliveryStatus.FAILED,
        retry_count=retry_count, last_attempt_at=last_attempt_at,
    )
    db.add(msg)
    db.flush()
    return msg


def _ok(*a, **kw):
    return telegram_bot.SendResult(ok=True, telegram_message_id="999")


def _fail(*a, **kw):
    return telegram_bot.SendResult(ok=False, error="simulated failure")


def _make_plan(db, **overrides) -> Plan:
    p = dict(plan_id=f"PLAN-{uuid.uuid4().hex[:6]}", name="Monthly", duration_days=30, price=49.0, currency="USD")
    p.update(overrides)
    plan = Plan(**p)
    db.add(plan)
    db.flush()
    return plan


def _active_subscription(db, *, telegram_user_id: str, expiry_date: datetime):
    plan = _make_plan(db)
    tu = subs.get_or_create_telegram_user(db, telegram_user_id=telegram_user_id)
    subscriber = subs.get_or_create_subscriber(db, tu)
    db.commit()
    provider = ManualPaymentProvider()
    subscription, payment = subs.start_subscription(db, subscriber, plan, provider=provider, actor="bot")
    db.commit()
    activated = subs.confirm_payment(db, payment, actor="admin:test")
    activated.expiry_date = expiry_date  # force a specific expiry for the test, independent of plan duration
    db.commit()
    return activated


# ══════════════════════════════════════════════════════════════════════════
# Telegram retry job
# ══════════════════════════════════════════════════════════════════════════
def test_retry_sends_stored_text_and_marks_sent(db):
    call = _make_call(db)
    msg = _make_failed_message(db, call, message_text="exact stored text")
    db.commit()

    # Freshly-FAILED (retry_count=0, last_attempt_at defaults to "now") is
    # still inside backoff[0]'s window — advance the clock past it so this
    # test exercises "eligible", not "not yet due" (that's a separate test).
    later = datetime.now(timezone.utc) + timedelta(seconds=31)
    with patch("app.telegram_bot._send_telegram_message", side_effect=_ok):
        stats = telegram_bot.process_telegram_retries(db, now=later)

    db.refresh(msg)
    assert stats.candidates == 1
    assert stats.sent == 1
    assert msg.delivery_status == DeliveryStatus.SENT
    assert msg.retry_count == 1


def test_retry_not_due_before_backoff_window_elapses(db):
    call = _make_call(db)
    now = datetime.now(timezone.utc)
    msg = _make_failed_message(db, call, retry_count=1, last_attempt_at=now)  # backoff[1] = 60s
    db.commit()

    with patch("app.telegram_bot._send_telegram_message", side_effect=_ok) as mock_send:
        stats = telegram_bot.process_telegram_retries(db, now=now + timedelta(seconds=5))

    assert stats.not_due == 1
    assert stats.sent == 0
    mock_send.assert_not_called()


def test_retry_due_after_backoff_window_elapses(db):
    call = _make_call(db)
    now = datetime.now(timezone.utc)
    msg = _make_failed_message(db, call, retry_count=1, last_attempt_at=now)  # backoff[1] = 60s

    with patch("app.telegram_bot._send_telegram_message", side_effect=_ok):
        stats = telegram_bot.process_telegram_retries(db, now=now + timedelta(seconds=61))

    assert stats.sent == 1


def test_retry_exhausts_after_max_total_attempts(db):
    call = _make_call(db)
    msg = _make_failed_message(db, call, retry_count=telegram_bot._MAX_TOTAL_ATTEMPTS)
    db.commit()

    with patch("app.telegram_bot._send_telegram_message", side_effect=_ok) as mock_send:
        stats = telegram_bot.process_telegram_retries(db)

    assert stats.exhausted == 1
    mock_send.assert_not_called()  # never even attempted once exhausted


def test_retry_reaching_max_this_run_is_reported_exhausted(db):
    call = _make_call(db)
    long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    msg = _make_failed_message(db, call, retry_count=telegram_bot._MAX_TOTAL_ATTEMPTS - 1, last_attempt_at=long_ago)
    db.commit()

    with patch("app.telegram_bot._send_telegram_message", side_effect=_fail):
        stats = telegram_bot.process_telegram_retries(db)

    db.refresh(msg)
    assert msg.retry_count == telegram_bot._MAX_TOTAL_ATTEMPTS
    assert stats.exhausted == 1
    assert stats.still_failing == 0


def test_retry_skips_row_with_no_stored_text(db):
    call = _make_call(db)
    msg = _make_failed_message(db, call, message_text=None)
    db.commit()

    with patch("app.telegram_bot._send_telegram_message", side_effect=_ok) as mock_send:
        stats = telegram_bot.process_telegram_retries(db)

    assert stats.skipped_no_text == 1
    mock_send.assert_not_called()


def test_retry_never_creates_a_duplicate_message_row(db):
    call = _make_call(db)
    _make_failed_message(db, call, message_text="only one row ever")
    db.commit()
    before = db.query(CallMessage).filter_by(call_id=call.id).count()

    base = datetime.now(timezone.utc)
    with patch("app.telegram_bot._send_telegram_message", side_effect=_fail):
        telegram_bot.process_telegram_retries(db, now=base + timedelta(seconds=31))
    with patch("app.telegram_bot._send_telegram_message", side_effect=_ok):
        telegram_bot.process_telegram_retries(db, now=base + timedelta(minutes=20))

    after = db.query(CallMessage).filter_by(call_id=call.id).count()
    assert before == after == 1


def test_retry_is_safe_to_run_twice_in_a_row_after_success(db):
    """Simulates a worker restart between ticks: run the job, then run it
    again immediately — the second run must find nothing left to do (the
    message is already SENT), never re-sending."""
    call = _make_call(db)
    _make_failed_message(db, call, message_text="restart safety")
    db.commit()

    later = datetime.now(timezone.utc) + timedelta(seconds=31)
    with patch("app.telegram_bot._send_telegram_message", side_effect=_ok) as mock_send:
        stats1 = telegram_bot.process_telegram_retries(db, now=later)
        stats2 = telegram_bot.process_telegram_retries(db, now=later)

    assert stats1.sent == 1
    assert stats2.candidates == 0  # no longer FAILED, so not even a candidate
    assert mock_send.call_count == 1


# ══════════════════════════════════════════════════════════════════════════
# Subscription lifecycle job
# ══════════════════════════════════════════════════════════════════════════
def test_lifecycle_marks_expiring_soon_and_expires(db):
    now = datetime.now(timezone.utc)
    soon = _active_subscription(db, telegram_user_id="lc-1", expiry_date=now + timedelta(days=1))
    already_expired = _active_subscription(db, telegram_user_id="lc-2", expiry_date=now - timedelta(hours=1))
    far_out = _active_subscription(db, telegram_user_id="lc-3", expiry_date=now + timedelta(days=20))

    # Whether settings.telegram_configured is True here depends on which
    # other test module's env setup ran first in this process (several set
    # STERLING_TELEGRAM_BOT_TOKEN/STERLING_TELEGRAM_PREMIUM_CHAT_ID globally
    # — see tests/test_rate_limit.py's note on the same lru_cache-singleton
    # behavior). Patch the actual Telegram call directly so this test never
    # depends on that ordering, and never makes a real network call either
    # way — access revocation itself is covered by the dedicated tests
    # above, not this one.
    with patch("app.subscriptions.telegram_access.revoke_premium_access", return_value=AccessResult(ok=True)):
        stats = subs.run_lifecycle_job(db, now=now)

    db.refresh(soon)
    db.refresh(already_expired)
    db.refresh(far_out)
    from app.models import SubscriptionStatus
    assert soon.status == SubscriptionStatus.EXPIRING_SOON
    assert already_expired.status == SubscriptionStatus.EXPIRED
    assert far_out.status == SubscriptionStatus.ACTIVE
    # Both `soon` and `already_expired` match mark_expiring_soon's WHERE
    # clause (expiry_date <= threshold covers overdue ones too) — an
    # already-overdue subscription passes through EXPIRING_SOON on its way
    # to EXPIRED within the same tick, which is correct: it's still one
    # subscription ending at the right terminal state, not a double-count
    # bug.
    assert stats.marked_expiring_soon == 2
    assert stats.expired == 1


def test_lifecycle_revokes_telegram_access_on_expiry(db, monkeypatch):
    import app.subscriptions as subs_module
    monkeypatch.setattr(subs_module, "get_settings", lambda: _fake_settings(telegram_configured=True, premium_chat_id="-1002"))

    now = datetime.now(timezone.utc)
    sub = _active_subscription(db, telegram_user_id="lc-revoke-1", expiry_date=now - timedelta(hours=1))

    with patch("app.subscriptions.telegram_access.revoke_premium_access",
               return_value=AccessResult(ok=True)) as mock_revoke:
        stats = subs_module.run_lifecycle_job(db, now=now)

    db.refresh(sub)
    assert sub.telegram_access_revoked_at is not None
    assert stats.access.revoked == 1
    mock_revoke.assert_called_once()


def test_lifecycle_revocation_is_idempotent_no_double_call(db, monkeypatch):
    import app.subscriptions as subs_module
    monkeypatch.setattr(subs_module, "get_settings", lambda: _fake_settings(telegram_configured=True, premium_chat_id="-1002"))

    now = datetime.now(timezone.utc)
    sub = _active_subscription(db, telegram_user_id="lc-revoke-2", expiry_date=now - timedelta(hours=1))

    with patch("app.subscriptions.telegram_access.revoke_premium_access",
               return_value=AccessResult(ok=True)) as mock_revoke:
        subs_module.run_lifecycle_job(db, now=now)
        subs_module.run_lifecycle_job(db, now=now)  # simulated next tick / restart

    assert mock_revoke.call_count == 1  # never re-revoked once telegram_access_revoked_at is set


def test_lifecycle_retries_revocation_after_a_failed_attempt(db, monkeypatch):
    """Restart-safety for the revoke step specifically: a failed Telegram
    call must leave telegram_access_revoked_at NULL so the next tick
    retries it, rather than silently giving up forever."""
    import app.subscriptions as subs_module
    monkeypatch.setattr(subs_module, "get_settings", lambda: _fake_settings(telegram_configured=True, premium_chat_id="-1002"))

    now = datetime.now(timezone.utc)
    sub = _active_subscription(db, telegram_user_id="lc-revoke-3", expiry_date=now - timedelta(hours=1))

    with patch("app.subscriptions.telegram_access.revoke_premium_access",
               return_value=AccessResult(ok=False, error="not enough rights")):
        subs_module.run_lifecycle_job(db, now=now)
    db.refresh(sub)
    assert sub.telegram_access_revoked_at is None

    with patch("app.subscriptions.telegram_access.revoke_premium_access",
               return_value=AccessResult(ok=True)) as mock_revoke:
        subs_module.run_lifecycle_job(db, now=now)
    db.refresh(sub)
    assert sub.telegram_access_revoked_at is not None
    mock_revoke.assert_called_once()


def test_lifecycle_skips_revocation_gracefully_when_telegram_not_configured(db, monkeypatch):
    import app.subscriptions as subs_module
    monkeypatch.setattr(subs_module, "get_settings", lambda: _fake_settings(telegram_configured=False, premium_chat_id=""))

    now = datetime.now(timezone.utc)
    sub = _active_subscription(db, telegram_user_id="lc-revoke-4", expiry_date=now - timedelta(hours=1))

    stats = subs_module.run_lifecycle_job(db, now=now)

    db.refresh(sub)
    assert sub.telegram_access_revoked_at is None
    assert stats.access.skipped_not_configured == 1


class _FakeSettings:
    def __init__(self, telegram_configured, premium_chat_id):
        self._telegram_configured = telegram_configured
        self.TELEGRAM_PREMIUM_CHAT_ID = premium_chat_id
        self.TELEGRAM_BOT_TOKEN = "fake-token" if telegram_configured else ""

    @property
    def telegram_configured(self):
        return self._telegram_configured


def _fake_settings(*, telegram_configured, premium_chat_id):
    return _FakeSettings(telegram_configured, premium_chat_id)


# ══════════════════════════════════════════════════════════════════════════
# Results-channel automation (via the real HTTP app, matching the pattern
# in tests/test_end_to_end.py)
# ══════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def http_env():
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.database import get_db
    from app.main import app
    from app.models import Base

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    def _override_get_db():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c, SessionLocal
    app.dependency_overrides.clear()


AUTH = {"Authorization": "Bearer test-key-123"}


@pytest.fixture()
def results_channel_configured(monkeypatch):
    """Module-level `settings` singletons in app.api/app.telegram_bot are
    captured at import time — env vars set later in a test file never reach
    them (see tests/test_rate_limit.py's identical note for adapter keys).
    Patch the live objects directly instead.

    Per the 2026-08-16 production Telegram architecture decision there is
    no separate results destination — verified results post to
    TELEGRAM_FREE_CHAT_ID itself (app/api.py::transition_call), so that's
    what this fixture configures. Kept as "-1003" (its value when this
    fixture still configured a dedicated results chat) purely to minimize
    diff noise in the assertions below that filter on that literal."""
    import app.api as api_module

    monkeypatch.setattr(api_module.settings, "TELEGRAM_FREE_CHAT_ID", "-1003")
    monkeypatch.setattr(api_module.settings, "TELEGRAM_BOT_TOKEN", "fake-token")
    yield


def test_results_post_lands_in_the_same_chat_as_the_free_teaser(http_env, results_channel_configured):
    """Direct proof of the 2026-08-16 production architecture decision:
    'there is NO separate Results channel' — a call's delayed Free teaser
    (once delivered) and that same call's later verified result must land
    in the identical chat ID, not two different destinations.

    Updated for Gate 3/4 (freemium delay+sanitization): a free-routed call
    no longer gets an immediate ENTRY message at all — only Premium does.
    Free gets a separate FREE_ENTRY teaser once process_delayed_free_calls
    runs past free_call_due_at, which this test triggers directly rather
    than waiting out the real delay."""
    client, SessionLocal = http_env
    with patch("app.telegram_bot._send_telegram_message", side_effect=_ok):
        r = client.post("/api/v1/calls", json={
            "source_call_id": "res-samechat-1", "instrument": "XAUUSD", "direction": "BUY",
            "stop_loss": 1900, "route_free": True, "route_premium": False,
        }, headers=AUTH)
        trade_id = r.json()["trade_id"]
        r2 = client.post(f"/api/v1/calls/{trade_id}/events",
                          json={"new_status": "CLOSED", "result_r": 1.5}, headers=AUTH)
        assert r2.status_code == 200

    db = SessionLocal()
    try:
        call = db.query(Call).filter_by(trade_id=trade_id).one()
        with patch("app.telegram_bot._send_telegram_message", side_effect=_ok):
            telegram_bot.process_delayed_free_calls(db, now=call.free_call_due_at + timedelta(seconds=1))

        free_teaser_chat_ids = {
            m.telegram_chat_id for m in
            db.query(CallMessage).filter_by(call_id=call.id, message_type=MessageType.FREE_ENTRY).all()
        }
        results_chat_ids = {
            m.telegram_chat_id for m in
            db.query(CallMessage).filter_by(call_id=call.id, message_type=MessageType.RESULTS).all()
        }
        assert free_teaser_chat_ids == results_chat_ids == {"-1003"}
    finally:
        db.close()


def test_settings_has_no_separate_results_chat_id_field():
    """Guards against the retired TELEGRAM_RESULTS_CHAT_ID setting being
    reintroduced by accident — per the 2026-08-16 decision, Sterling_Room
    must not have a separate results destination to configure."""
    from app.config import get_settings

    assert not hasattr(get_settings(), "TELEGRAM_RESULTS_CHAT_ID")


def test_results_channel_posts_on_close(http_env, results_channel_configured):
    client, SessionLocal = http_env
    with patch("app.telegram_bot._send_telegram_message", side_effect=_ok):
        r = client.post("/api/v1/calls", json={
            "source_call_id": "res-1", "instrument": "XAUUSD", "direction": "BUY", "stop_loss": 1900,
        }, headers=AUTH)
        trade_id = r.json()["trade_id"]

        r2 = client.post(f"/api/v1/calls/{trade_id}/events",
                          json={"new_status": "CLOSED", "result_r": 2.0}, headers=AUTH)
        assert r2.status_code == 200

    db = SessionLocal()
    try:
        call = db.query(Call).filter_by(trade_id=trade_id).one()
        results_msgs = db.query(CallMessage).filter_by(
            call_id=call.id, telegram_chat_id="-1003", message_type=MessageType.RESULTS,
        ).all()
        assert len(results_msgs) == 1
        assert results_msgs[0].delivery_status == DeliveryStatus.SENT
        assert call.result_r == 2.0
    finally:
        db.close()


def test_results_channel_posted_for_losing_trade_too(http_env, results_channel_configured):
    """Master-prompt: 'losing trades must never disappear' — a STOPPED
    (losing) call must post to Results exactly like a winning CLOSED one."""
    client, SessionLocal = http_env
    with patch("app.telegram_bot._send_telegram_message", side_effect=_ok):
        r = client.post("/api/v1/calls", json={
            "source_call_id": "res-loss-1", "instrument": "XAUUSD", "direction": "BUY", "stop_loss": 1900,
        }, headers=AUTH)
        trade_id = r.json()["trade_id"]
        client.post(f"/api/v1/calls/{trade_id}/events",
                     json={"new_status": "STOPPED", "result_r": -1.0}, headers=AUTH)

    db = SessionLocal()
    try:
        call = db.query(Call).filter_by(trade_id=trade_id).one()
        results_msgs = db.query(CallMessage).filter_by(call_id=call.id, message_type=MessageType.RESULTS).all()
        assert len(results_msgs) == 1
        assert call.result_r == -1.0
    finally:
        db.close()


def test_duplicate_close_event_produces_exactly_one_result_post_and_no_result_overwrite(http_env, results_channel_configured):
    """The exact scenario master-prompt Phase 10 §2 calls out: 'if a result
    has already been posted, retrying the same close event must not create
    a duplicate result.' Also confirms the retry doesn't 409 and doesn't
    silently rewrite the already-recorded result_r."""
    client, SessionLocal = http_env
    with patch("app.telegram_bot._send_telegram_message", side_effect=_ok):
        r = client.post("/api/v1/calls", json={
            "source_call_id": "res-dup-1", "instrument": "XAUUSD", "direction": "BUY", "stop_loss": 1900,
        }, headers=AUTH)
        trade_id = r.json()["trade_id"]

        r1 = client.post(f"/api/v1/calls/{trade_id}/events",
                          json={"new_status": "CLOSED", "result_r": 3.0}, headers=AUTH)
        assert r1.status_code == 200

        # Retry of the SAME close event (e.g. the adapter never saw r1's
        # response) — must be a clean 200, not a 409, and must not create a
        # second Results post or overwrite result_r with a different value.
        r2 = client.post(f"/api/v1/calls/{trade_id}/events",
                          json={"new_status": "CLOSED", "result_r": 3.0}, headers=AUTH)
        assert r2.status_code == 200
        assert r2.json()["status"] == "CLOSED"

    db = SessionLocal()
    try:
        call = db.query(Call).filter_by(trade_id=trade_id).one()
        assert call.result_r == 3.0
        results_msgs = db.query(CallMessage).filter_by(call_id=call.id, message_type=MessageType.RESULTS).all()
        assert len(results_msgs) == 1  # not two
        exit_msgs = db.query(CallMessage).filter_by(call_id=call.id, message_type=MessageType.EXIT).all()
        assert len(exit_msgs) <= 1  # route_premium defaults True -> at most one chat
    finally:
        db.close()


def test_retry_of_close_event_does_not_overwrite_result_with_a_different_value(http_env, results_channel_configured):
    """A defensive case beyond the literal duplicate-retry scenario: even if
    a second CLOSED request somehow carries a DIFFERENT result_r, the
    already-recorded historical result must not be silently overwritten
    (master-prompt: 'never silently alter historical results')."""
    client, SessionLocal = http_env
    with patch("app.telegram_bot._send_telegram_message", side_effect=_ok):
        r = client.post("/api/v1/calls", json={
            "source_call_id": "res-dup-2", "instrument": "XAUUSD", "direction": "BUY", "stop_loss": 1900,
        }, headers=AUTH)
        trade_id = r.json()["trade_id"]
        client.post(f"/api/v1/calls/{trade_id}/events", json={"new_status": "CLOSED", "result_r": 3.0}, headers=AUTH)
        r2 = client.post(f"/api/v1/calls/{trade_id}/events",
                          json={"new_status": "CLOSED", "result_r": -5.0}, headers=AUTH)
        assert r2.status_code == 200

    db = SessionLocal()
    try:
        call = db.query(Call).filter_by(trade_id=trade_id).one()
        assert call.result_r == 3.0  # the ORIGINAL result, not the retry's different value
    finally:
        db.close()


def test_results_channel_not_posted_without_a_result(http_env, results_channel_configured):
    client, SessionLocal = http_env
    with patch("app.telegram_bot._send_telegram_message", side_effect=_ok):
        r = client.post("/api/v1/calls", json={
            "source_call_id": "res-none-1", "instrument": "XAUUSD", "direction": "BUY", "stop_loss": 1900,
        }, headers=AUTH)
        trade_id = r.json()["trade_id"]
        client.post(f"/api/v1/calls/{trade_id}/events", json={"new_status": "CLOSED"}, headers=AUTH)  # no result_r

    db = SessionLocal()
    try:
        call = db.query(Call).filter_by(trade_id=trade_id).one()
        assert call.result_r is None
        results_msgs = db.query(CallMessage).filter_by(call_id=call.id, message_type=MessageType.RESULTS).all()
        assert len(results_msgs) == 0
    finally:
        db.close()


def test_illegal_transition_still_rejected_with_duplicate_retry_handling_in_place(http_env, results_channel_configured):
    """Guards against a regression where the idempotent-retry carve-out
    accidentally swallows a GENUINELY illegal transition (different target
    status, not a retry of the same one)."""
    client, SessionLocal = http_env
    with patch("app.telegram_bot._send_telegram_message", side_effect=_ok):
        r = client.post("/api/v1/calls", json={
            "source_call_id": "res-illegal-1", "instrument": "XAUUSD", "direction": "BUY", "stop_loss": 1900,
        }, headers=AUTH)
        trade_id = r.json()["trade_id"]
        client.post(f"/api/v1/calls/{trade_id}/events", json={"new_status": "CLOSED", "result_r": 1.0}, headers=AUTH)
        r2 = client.post(f"/api/v1/calls/{trade_id}/events", json={"new_status": "ACTIVE"}, headers=AUTH)
    assert r2.status_code == 409


# ══════════════════════════════════════════════════════════════════════════
# Freemium: Premium immediate + full, Free delayed + sanitized
# (2026-08-16 production architecture, Gate 3/4).
# ══════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def freemium_configured(monkeypatch):
    """Explicit FREE/PREMIUM chat IDs + bot token for this section's tests
    — not relying on whatever another test file's os.environ.setdefault
    happened to set globally (see results_channel_configured's identical
    note above)."""
    import app.api as api_module

    monkeypatch.setattr(api_module.settings, "TELEGRAM_FREE_CHAT_ID", "-2001")
    monkeypatch.setattr(api_module.settings, "TELEGRAM_PREMIUM_CHAT_ID", "-2002")
    monkeypatch.setattr(api_module.settings, "TELEGRAM_BOT_TOKEN", "fake-token")
    yield


def test_free_teaser_contains_no_execution_numbers(db):
    """The Gate 4 STOP CONDITION, tested directly: search the rendered Free
    payload for every execution-critical number on the call and confirm
    none of them appear anywhere in the text."""
    call = _make_call(
        db, direction=CallDirection.BUY, stop_loss=1950.777,
        entry_min=1948.111, entry_max=1949.222, tp1=1955.333, tp2=1960.444, tp3=1965.555,
        risk_percent=2.25,
    )
    text = telegram_bot.render_free_teaser_message(call)
    for forbidden in ("1950.777", "1948.111", "1949.222", "1955.333", "1960.444", "1965.555", "2.25"):
        assert forbidden not in text, f"leaked execution number {forbidden!r} in free teaser: {text}"


def test_free_teaser_contains_instrument_and_direction(db):
    call = _make_call(db, instrument="EURUSD", direction=CallDirection.SELL, stop_loss=1.2)
    text = telegram_bot.render_free_teaser_message(call)
    assert "EURUSD" in text
    assert "SELL" in text
    assert call.trade_id in text


def test_free_teaser_does_not_echo_analysis_or_setup_type_free_text(db):
    """Defensive test for the exact leak vector the renderer's docstring
    calls out: free-text fields the adapter/analyst supplies (which could
    themselves contain a typed-in price) must never be echoed verbatim."""
    call = _make_call(
        db, stop_loss=1900, analysis="Enter near 1950.5 with SL at 1900.25 for a clean R:R",
        setup_type="breakout above 1955.0", invalidation="close below 1890.0",
    )
    text = telegram_bot.render_free_teaser_message(call)
    for leaked in ("1950.5", "1900.25", "1955.0", "1890.0"):
        assert leaked not in text


def test_delayed_free_delivery_skips_calls_not_yet_due(db):
    _make_call(db, route_free=True, route_premium=False,
               free_call_due_at=datetime.now(timezone.utc) + timedelta(minutes=10))
    stats = telegram_bot.process_delayed_free_calls(db)
    assert stats.candidates == 0
    assert stats.sent == 0


def test_delayed_free_delivery_skips_premium_only_calls(db):
    """A call with route_free=False (so free_call_due_at is never set, per
    app/services.py::create_call) must never surface here at all, no
    matter how much time has passed."""
    _make_call(db, route_free=False, route_premium=True, free_call_due_at=None)
    stats = telegram_bot.process_delayed_free_calls(db, now=datetime.now(timezone.utc) + timedelta(days=1))
    assert stats.candidates == 0


def test_delayed_free_delivery_sends_when_due(db, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "TELEGRAM_FREE_CHAT_ID", "-2001")
    monkeypatch.setattr(get_settings(), "TELEGRAM_BOT_TOKEN", "fake-token")

    call = _make_call(db, route_free=True, route_premium=False,
                       free_call_due_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    with patch("app.telegram_bot._send_telegram_message", side_effect=_ok):
        stats = telegram_bot.process_delayed_free_calls(db)

    assert stats.candidates == 1
    assert stats.sent == 1
    assert stats.trade_ids == [call.trade_id]
    msgs = db.query(CallMessage).filter_by(call_id=call.id, message_type=MessageType.FREE_ENTRY).all()
    assert len(msgs) == 1
    assert msgs[0].delivery_status == DeliveryStatus.SENT
    assert msgs[0].telegram_chat_id == "-2001"


def test_delayed_free_delivery_is_idempotent_no_duplicate_on_second_run(db, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "TELEGRAM_FREE_CHAT_ID", "-2001")
    monkeypatch.setattr(get_settings(), "TELEGRAM_BOT_TOKEN", "fake-token")

    call = _make_call(db, route_free=True, route_premium=False,
                       free_call_due_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    with patch("app.telegram_bot._send_telegram_message", side_effect=_ok):
        telegram_bot.process_delayed_free_calls(db)
        stats2 = telegram_bot.process_delayed_free_calls(db)

    assert stats2.sent == 0
    assert stats2.already_delivered == 1
    msgs = db.query(CallMessage).filter_by(call_id=call.id, message_type=MessageType.FREE_ENTRY).all()
    assert len(msgs) == 1  # not two


def test_delayed_free_delivery_failure_is_handed_off_to_the_retry_job_not_resent(db, monkeypatch):
    """A failed first attempt must NOT be retried from scratch by this job
    on the next tick (that would bypass the backoff schedule) — it must be
    left as a FAILED row for process_telegram_retries to pick up on its own
    schedule, exactly like any other FAILED CallMessage."""
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "TELEGRAM_FREE_CHAT_ID", "-2001")
    monkeypatch.setattr(get_settings(), "TELEGRAM_BOT_TOKEN", "fake-token")

    call = _make_call(db, route_free=True, route_premium=False,
                       free_call_due_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    with patch("app.telegram_bot._send_telegram_message", side_effect=_fail):
        stats = telegram_bot.process_delayed_free_calls(db)
    assert stats.sent == 1  # "sent" here means "distribute_call was invoked", not "delivery succeeded"

    msg = db.query(CallMessage).filter_by(call_id=call.id, message_type=MessageType.FREE_ENTRY).one()
    assert msg.delivery_status == DeliveryStatus.FAILED

    # Second tick: must NOT re-invoke distribute_call for this call again.
    stats2 = telegram_bot.process_delayed_free_calls(db)
    assert stats2.sent == 0
    assert stats2.already_delivered == 1

    # The existing retry job picks it up once its backoff window elapses.
    with patch("app.telegram_bot._send_telegram_message", side_effect=_ok):
        retry_stats = telegram_bot.process_telegram_retries(
            db, now=datetime.now(timezone.utc) + timedelta(seconds=31),
        )
    assert retry_stats.sent == 1
    db.refresh(msg)
    assert msg.delivery_status == DeliveryStatus.SENT


def test_delayed_free_delivery_skipped_cleanly_when_not_configured(db, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "TELEGRAM_FREE_CHAT_ID", "")
    call = _make_call(db, route_free=True, route_premium=False,
                       free_call_due_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    stats = telegram_bot.process_delayed_free_calls(db)
    assert stats.skipped_not_configured == 1
    assert stats.sent == 0
    assert db.query(CallMessage).filter_by(call_id=call.id).count() == 0


def test_create_call_premium_gets_full_immediately_free_gets_nothing_yet(http_env, freemium_configured):
    """The core Gate 3/4 proof: right after POST /calls, Premium already
    has the full ENTRY message and Free has NOTHING — no CallMessage row
    of any type for the free chat exists until the delay job runs."""
    client, SessionLocal = http_env
    with patch("app.telegram_bot._send_telegram_message", side_effect=_ok):
        r = client.post("/api/v1/calls", json={
            "source_call_id": "free-delay-1", "instrument": "XAUUSD", "direction": "BUY",
            "stop_loss": 1900, "tp1": 1950, "route_free": True, "route_premium": True,
        }, headers=AUTH)
        assert r.status_code == 200
        trade_id = r.json()["trade_id"]

    db = SessionLocal()
    try:
        call = db.query(Call).filter_by(trade_id=trade_id).one()

        premium_msgs = db.query(CallMessage).filter_by(
            call_id=call.id, telegram_chat_id="-2002", message_type=MessageType.ENTRY,
        ).all()
        assert len(premium_msgs) == 1
        assert premium_msgs[0].delivery_status == DeliveryStatus.SENT
        assert telegram_bot._format_price(1900) in premium_msgs[0].message_text  # full stop loss present for Premium

        free_msgs = db.query(CallMessage).filter_by(call_id=call.id, telegram_chat_id="-2001").all()
        assert len(free_msgs) == 0  # nothing sent to Free yet, at any message type

        assert call.free_call_due_at is not None
        expected = call.created_at + timedelta(seconds=900)
        assert abs((call.free_call_due_at - expected).total_seconds()) < 2
    finally:
        db.close()


def test_delayed_free_job_delivers_after_due_and_matches_immediate_premium_call(http_env, freemium_configured):
    """End-to-end: create a free+premium call, then simulate the worker
    tick firing after the delay window — Free gets its sanitized teaser,
    with no execution numbers, while Premium's original message is
    untouched."""
    client, SessionLocal = http_env
    with patch("app.telegram_bot._send_telegram_message", side_effect=_ok):
        r = client.post("/api/v1/calls", json={
            "source_call_id": "free-delay-2", "instrument": "GBPUSD", "direction": "SELL",
            "stop_loss": 1.4321, "route_free": True, "route_premium": True,
        }, headers=AUTH)
        trade_id = r.json()["trade_id"]

    db = SessionLocal()
    try:
        call = db.query(Call).filter_by(trade_id=trade_id).one()
        with patch("app.telegram_bot._send_telegram_message", side_effect=_ok):
            stats = telegram_bot.process_delayed_free_calls(
                db, now=call.free_call_due_at + timedelta(seconds=1),
            )
        assert stats.sent == 1

        free_msg = db.query(CallMessage).filter_by(
            call_id=call.id, telegram_chat_id="-2001", message_type=MessageType.FREE_ENTRY,
        ).one()
        assert free_msg.delivery_status == DeliveryStatus.SENT
        assert "1.4321" not in free_msg.message_text
        assert "GBPUSD" in free_msg.message_text

        # Premium's original message is untouched by the free delivery job.
        premium_msg = db.query(CallMessage).filter_by(
            call_id=call.id, telegram_chat_id="-2002", message_type=MessageType.ENTRY,
        ).one()
        assert "1.4321" in premium_msg.message_text
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════════════════
# app.worker.run_once() — full integration, and restart-safety across
# independent SessionLocal()s the way a real process restart would look.
# ══════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def worker_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    from app.models import Base

    db_path = tmp_path / "worker_test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _busy_timeout(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()

    Base.metadata.create_all(engine)
    TestSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(worker, "SessionLocal", TestSessionLocal)
    return TestSessionLocal


def test_run_once_processes_both_jobs_and_is_safe_to_call_twice(worker_db):
    db = worker_db()
    try:
        call = _make_call(db)
        # last_attempt_at set safely in the past so the very first
        # worker.run_once() call (which computes its own "now" internally,
        # not test-controlled) already sees it as past the backoff window.
        _make_failed_message(db, call, message_text="worker integration",
                              last_attempt_at=datetime.now(timezone.utc) - timedelta(hours=1))
        now = datetime.now(timezone.utc)
        _active_subscription(db, telegram_user_id="worker-int-1", expiry_date=now - timedelta(hours=1))
        db.commit()
    finally:
        db.close()

    with patch("app.telegram_bot._send_telegram_message", side_effect=_ok), \
         patch("app.subscriptions.telegram_access.revoke_premium_access", return_value=AccessResult(ok=False, error="not configured in test")):
        summary1 = worker.run_once()
        summary2 = worker.run_once()  # simulated restart / next tick

    assert summary1["telegram_retry"]["sent"] == 1
    assert summary1["subscription_lifecycle"]["expired"] == 1
    # Second run: nothing left to (re)send, nothing left to (re)expire.
    assert summary2["telegram_retry"]["candidates"] == 0
    assert summary2["subscription_lifecycle"]["expired"] == 0


def test_run_once_one_job_failing_does_not_prevent_the_other_from_running(worker_db):
    db = worker_db()
    try:
        call = _make_call(db)
        _make_failed_message(db, call, message_text="isolated failure test")
        db.commit()
    finally:
        db.close()

    with patch("app.telegram_bot.process_telegram_retries", side_effect=RuntimeError("boom")), \
         patch("app.telegram_bot._send_telegram_message", side_effect=_ok):
        summary = worker.run_once()

    assert "error" in summary["telegram_retry"]
    assert "error" not in summary["subscription_lifecycle"]
    assert "error" not in summary["free_call_delivery"]


def test_run_once_includes_and_processes_the_delayed_free_delivery_job(worker_db, monkeypatch):
    """Confirms the third job (2026-08-16 Gate 3/4) is actually wired into
    run_once(), not just defined and never called, and that it survives a
    simulated restart (second tick) the same way the other two jobs do."""
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "TELEGRAM_FREE_CHAT_ID", "-2001")
    monkeypatch.setattr(get_settings(), "TELEGRAM_BOT_TOKEN", "fake-token")

    db = worker_db()
    try:
        _make_call(db, route_free=True, route_premium=False,
                   free_call_due_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        db.commit()
    finally:
        db.close()

    with patch("app.telegram_bot._send_telegram_message", side_effect=_ok):
        summary1 = worker.run_once()
        summary2 = worker.run_once()  # simulated restart / next tick

    assert "free_call_delivery" in summary1
    assert summary1["free_call_delivery"]["sent"] == 1
    assert summary2["free_call_delivery"]["sent"] == 0
    assert summary2["free_call_delivery"]["already_delivered"] == 1
