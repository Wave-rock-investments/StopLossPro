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
    Patch the live objects directly instead."""
    import app.api as api_module

    monkeypatch.setattr(api_module.settings, "TELEGRAM_RESULTS_CHAT_ID", "-1003")
    monkeypatch.setattr(api_module.settings, "TELEGRAM_BOT_TOKEN", "fake-token")
    yield


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
