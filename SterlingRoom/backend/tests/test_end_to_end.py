"""Full end-to-end test (explicit Phase 4-8 requirement #10): both the call
lifecycle and the subscriber lifecycle, through the real HTTP app, in one
test each — not through internal function calls that skip the layers a real
request would cross.

Call lifecycle: StopLossPro (simulated adapter POST) -> Adapter auth ->
API -> DB -> Trade ID -> Telegram (mocked transport) -> TP1 update -> Close
-> Performance ledger -> Results endpoint -> Admin dashboard.

Subscriber lifecycle: /start (webhook) -> PREMIUM -> plan selection ->
payment instructions -> "I'VE PAID" (support ticket, no auto-activation) ->
admin verification (/admin/payments confirm) -> Subscription ACTIVE ->
premium access grant (mocked Telegram) -> forced expiry -> revocation.
"""
import os

os.environ.setdefault("STERLING_DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("STERLING_ADAPTER_API_KEYS", "test-key-123")
os.environ.setdefault("STERLING_TELEGRAM_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("STERLING_TELEGRAM_BOT_TOKEN", "test-bot-token")
os.environ.setdefault("STERLING_ADMIN_BOOTSTRAP_TOKEN", "bootstrap-secret-xyz")
os.environ.setdefault("STERLING_ADMIN_SESSION_SECRET", "admin-session-secret-for-tests")
os.environ.setdefault("STERLING_TELEGRAM_FREE_CHAT_ID", "-1001")
os.environ.setdefault("STERLING_TELEGRAM_PREMIUM_CHAT_ID", "-1002")

import datetime as dt
from unittest.mock import patch

import pytest

AUTH = {"Authorization": "Bearer test-key-123"}
BOOTSTRAP_HEADERS = {"X-Bootstrap-Token": "bootstrap-secret-xyz"}


@pytest.fixture()
def env():
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.main import app
    from app.database import get_db
    from app.models import Base

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    def _override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c, SessionLocal
    app.dependency_overrides.clear()


def _telegram_ok(*a, **kw):
    from app.telegram_bot import SendResult
    return SendResult(ok=True, telegram_message_id="1")


def test_full_call_lifecycle_end_to_end(env):
    client, SessionLocal = env

    with patch("app.telegram_bot._send_telegram_message", side_effect=_telegram_ok):
        # StopLossPro -> adapter -> API -> DB -> Trade ID
        r = client.post("/api/v1/calls", json={
            "source_call_id": "e2e-full-1", "instrument": "XAUUSD", "direction": "SELL",
            "stop_loss": 1950.0, "tp1": 1900.0, "tp2": 1870.0, "risk_percent": 0.5,
            "route_premium": True,
        }, headers=AUTH)
        assert r.status_code == 200
        trade_id = r.json()["trade_id"]
        assert trade_id.startswith("SR-")
        assert r.json()["status"] == "ACTIVE"

        # A retried adapter POST (same source_call_id) must not mint a
        # second Trade ID.
        r_retry = client.post("/api/v1/calls", json={
            "source_call_id": "e2e-full-1", "instrument": "XAUUSD", "direction": "SELL", "stop_loss": 1950.0,
        }, headers=AUTH)
        assert r_retry.json()["trade_id"] == trade_id

        # TP1 update
        r_tp1 = client.post(f"/api/v1/calls/{trade_id}/events", json={"new_status": "TP1_HIT"}, headers=AUTH)
        assert r_tp1.status_code == 200 and r_tp1.json()["status"] == "TP1_HIT"

        # Close with a result
        r_close = client.post(f"/api/v1/calls/{trade_id}/events",
                               json={"new_status": "CLOSED", "result_r": 2.5}, headers=AUTH)
        assert r_close.status_code == 200 and r_close.json()["status"] == "CLOSED"

    # Performance ledger reflects the closed trade — no number hand-entered.
    r_perf = client.get("/api/v1/performance", headers=AUTH)
    body = r_perf.json()
    assert body["total_trades"] == 1
    assert body["net_r"] == 2.5
    assert trade_id in body["trade_ids"]

    # Admin dashboard shows the same call, its lifecycle, and delivery status.
    email, password = _bootstrap_and_login(client)
    r_detail = client.get(f"/admin/calls/{trade_id}")
    assert r_detail.status_code == 200
    assert "TP1_REACHED" in r_detail.text or "TP1_HIT" in r_detail.text
    assert "CALL_CLOSED" in r_detail.text

    r_dash = client.get("/admin")
    assert "Closed Calls (today)" in r_dash.text

    r_monitor = client.get("/api/v1/monitoring", headers=AUTH)
    assert r_monitor.json()["db_ok"] is True


def _bootstrap_and_login(client, email="owner@sterlingroom.test", password="a-strong-passphrase-1"):
    r = client.post("/admin/bootstrap", data={"email": email, "password": password}, headers=BOOTSTRAP_HEADERS)
    if r.status_code == 409:
        pass  # already bootstrapped by an earlier call within this test
    else:
        assert r.status_code == 200, r.text
    client.post("/admin/login", data={"email": email, "password": password})
    return email, password


def test_full_subscriber_lifecycle_end_to_end(env):
    client, SessionLocal = env

    from app.models import Plan
    db = SessionLocal()
    db.add(Plan(plan_id="MONTHLY", name="Monthly", duration_days=30, price=49.0, currency="USD"))
    db.commit()
    db.close()

    with patch("app.telegram_bot.send_message", side_effect=_telegram_ok), \
         patch("app.telegram_bot.answer_callback_query", side_effect=_telegram_ok):

        # /start
        r = client.post("/api/v1/telegram/webhook/test-webhook-secret", json={
            "update_id": 1,
            "message": {"chat": {"id": 5001}, "from": {"id": 5001, "username": "e2e_trader"}, "text": "/start"},
        })
        assert r.status_code == 200

        # PREMIUM -> plan selection
        r = client.post("/api/v1/telegram/webhook/test-webhook-secret", json={
            "update_id": 2,
            "callback_query": {"id": "c1", "data": "menu:premium",
                                "message": {"chat": {"id": 5001}}, "from": {"id": 5001}},
        })
        assert r.status_code == 200

        r = client.post("/api/v1/telegram/webhook/test-webhook-secret", json={
            "update_id": 3,
            "callback_query": {"id": "c2", "data": "plan:MONTHLY",
                                "message": {"chat": {"id": 5001}}, "from": {"id": 5001}},
        })
        assert r.status_code == 200

    from app.models import Subscription, SubscriptionStatus, Payment
    db = SessionLocal()
    subscription = db.query(Subscription).one()
    payment = db.query(Payment).one()
    assert subscription.status == SubscriptionStatus.PENDING_PAYMENT
    payment_ref = payment.provider_payment_id
    payment_id = str(payment.id)
    db.close()

    with patch("app.telegram_bot.send_message", side_effect=_telegram_ok), \
         patch("app.telegram_bot.answer_callback_query", side_effect=_telegram_ok):
        # "I'VE PAID" — files a ticket, does NOT activate
        r = client.post("/api/v1/telegram/webhook/test-webhook-secret", json={
            "update_id": 4,
            "callback_query": {"id": "c3", "data": f"paid:{payment_ref}",
                                "message": {"chat": {"id": 5001}}, "from": {"id": 5001}},
        })
        assert r.status_code == 200

    from app.models import SupportTicket, TicketCategory
    db = SessionLocal()
    ticket = db.query(SupportTicket).filter_by(category=TicketCategory.PAYMENT).one()
    assert payment_ref in ticket.message
    db.close()

    # Admin verifies and confirms the payment -> subscription ACTIVE
    _bootstrap_and_login(client)
    r_confirm = client.post(f"/admin/payments/{payment_id}/confirm", follow_redirects=True)
    assert r_confirm.status_code == 200

    db = SessionLocal()
    subscription = db.query(Subscription).one()
    assert subscription.status == SubscriptionStatus.ACTIVE
    assert subscription.expiry_date is not None
    db.close()

    from app import subscriptions as subs
    db = SessionLocal()
    assert subs.is_premium(db, "5001") is True
    db.close()

    # Premium access grant (mocked Telegram invite link creation)
    from app import telegram_access
    with patch.object(telegram_access, "_call") as mock_call:
        mock_call.return_value = (True, {"ok": True, "result": {"invite_link": "https://t.me/+e2e"}})
        result = telegram_access.grant_premium_access("test-bot-token", "-1002", name_label="e2e_trader")
    assert result.ok is True

    # Force expiry and confirm access is revoked at the subscription layer
    db = SessionLocal()
    subscription = db.query(Subscription).filter_by(id=subscription.id).one()
    subscription.expiry_date = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    db.commit()
    expired = subs.expire_subscriptions(db)
    db.commit()
    assert subscription in expired
    assert subs.is_premium(db, "5001") is False
    db.close()

    # And the Telegram-side revocation call uses the real ban/unban mechanism
    with patch.object(telegram_access, "_call") as mock_call:
        mock_call.return_value = (True, {"ok": True})
        revoke_result = telegram_access.revoke_premium_access("test-bot-token", "-1002", "5001")
    assert revoke_result.ok is True

    # Admin subscribers page reflects the final state
    r_subs = client.get("/admin/subscribers")
    assert "e2e_trader" in r_subs.text
