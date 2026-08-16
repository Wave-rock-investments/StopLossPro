"""End-to-end HTTP test of the actual FastAPI app (master-prompt §58's
"end-to-end test" scenario, narrowed to Phase 1-3 scope: StopLossPro ->
adapter -> validated -> Trade ID -> database -> Telegram send)."""
import os

os.environ.setdefault("STERLING_DATABASE_URL", "sqlite:///:memory:")
os.environ["STERLING_ADAPTER_API_KEYS"] = "test-key-123"
os.environ.setdefault("STERLING_TELEGRAM_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("STERLING_TELEGRAM_BOT_TOKEN", "test-bot-token")

import pytest
from unittest.mock import patch


@pytest.fixture()
def client():
    # Import here, after env vars are set, and with a fresh in-memory DB per
    # test via dependency override (app.database uses a module-level engine
    # normally, so tests override get_db directly rather than relying on it).
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.main import app
    from app.database import get_db
    from app.models import Base

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine)

    def _override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


AUTH = {"Authorization": "Bearer test-key-123"}


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_create_call_requires_auth(client):
    r = client.post("/api/v1/calls", json={
        "source_call_id": "x", "instrument": "XAUUSD", "direction": "BUY", "stop_loss": 1900,
    })
    assert r.status_code == 401


def test_create_call_end_to_end(client):
    with patch("app.telegram_bot._send_telegram_message") as mock_send:
        mock_send.return_value = __import__("app.telegram_bot", fromlist=["SendResult"]).SendResult(ok=True, telegram_message_id="99")
        r = client.post("/api/v1/calls", json={
            "source_call_id": "e2e-1", "instrument": "XAUUSD", "direction": "SELL",
            "stop_loss": 1950.0, "tp1": 1900.0, "tp2": 1870.0, "route_premium": True,
        }, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["trade_id"].startswith("SR-")
    assert body["status"] == "ACTIVE"
    assert body["instrument"] == "XAUUSD"


def test_create_call_idempotent_over_http(client):
    payload = {"source_call_id": "e2e-dup", "instrument": "EURUSD", "direction": "BUY", "stop_loss": 1.08}
    r1 = client.post("/api/v1/calls", json=payload, headers=AUTH)
    r2 = client.post("/api/v1/calls", json=payload, headers=AUTH)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["trade_id"] == r2.json()["trade_id"]


def test_create_call_validation_error(client):
    r = client.post("/api/v1/calls", json={
        "source_call_id": "bad-1", "instrument": "XAUUSD", "direction": "UP", "stop_loss": 1900,
    }, headers=AUTH)
    assert r.status_code == 422


def test_get_call_not_found(client):
    r = client.get("/api/v1/calls/SR-000000-999", headers=AUTH)
    assert r.status_code == 404


def test_illegal_transition_rejected_over_http(client):
    r1 = client.post("/api/v1/calls", json={
        "source_call_id": "trans-1", "instrument": "BTCUSD", "direction": "BUY", "stop_loss": 60000,
    }, headers=AUTH)
    trade_id = r1.json()["trade_id"]

    r2 = client.post(f"/api/v1/calls/{trade_id}/events", json={"new_status": "CLOSED"}, headers=AUTH)
    assert r2.status_code == 200
    assert r2.json()["status"] == "CLOSED"

    r3 = client.post(f"/api/v1/calls/{trade_id}/events", json={"new_status": "ACTIVE"}, headers=AUTH)
    assert r3.status_code == 409


# ══════════════════════════════════════════════════════════════════════════
# Telegram webhook (Phase 4) — fails closed on a wrong/missing secret,
# routes a real Update through to app/bot.py otherwise.
# ══════════════════════════════════════════════════════════════════════════
def test_webhook_wrong_secret_404s(client):
    r = client.post("/api/v1/telegram/webhook/not-the-secret", json={"update_id": 1})
    assert r.status_code == 404


def test_webhook_correct_secret_processes_update(client):
    with patch("app.telegram_bot.send_message") as mock_send:
        mock_send.return_value = __import__("app.telegram_bot", fromlist=["SendResult"]).SendResult(ok=True, telegram_message_id="1")
        r = client.post("/api/v1/telegram/webhook/test-webhook-secret", json={
            "update_id": 555,
            "message": {"chat": {"id": 42}, "from": {"id": 42, "username": "bob"}, "text": "/start"},
        })
    assert r.status_code == 200
    assert mock_send.called


def test_webhook_duplicate_update_id_over_http_is_a_noop(client):
    update = {
        "update_id": 556,
        "message": {"chat": {"id": 43}, "from": {"id": 43, "username": "carol"}, "text": "/start"},
    }
    with patch("app.telegram_bot.send_message") as mock_send:
        mock_send.return_value = __import__("app.telegram_bot", fromlist=["SendResult"]).SendResult(ok=True, telegram_message_id="1")
        r1 = client.post("/api/v1/telegram/webhook/test-webhook-secret", json=update)
        r2 = client.post("/api/v1/telegram/webhook/test-webhook-secret", json=update)
    assert r1.status_code == 200 and r2.status_code == 200
    assert mock_send.call_count == 1


def test_webhook_malformed_json_body(client):
    r = client.post(
        "/api/v1/telegram/webhook/test-webhook-secret",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


# ══════════════════════════════════════════════════════════════════════════
# Monitoring (Phase 8)
# ══════════════════════════════════════════════════════════════════════════
def test_monitoring_requires_auth(client):
    r = client.get("/api/v1/monitoring")
    assert r.status_code == 401


def test_monitoring_reports_counts(client):
    r = client.get("/api/v1/monitoring", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["db_ok"] is True
    assert "active_calls" in body
    assert "production_ready" in body
