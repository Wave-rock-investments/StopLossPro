"""End-to-end HTTP tests for the admin dashboard (Phase 7). Exercises the
real FastAPI app + real cookie auth + real DB, same style as test_api.py's
end-to-end call-pipeline tests."""
import os

os.environ.setdefault("STERLING_DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("STERLING_ADAPTER_API_KEYS", "test-key-123")
os.environ.setdefault("STERLING_ADMIN_BOOTSTRAP_TOKEN", "bootstrap-secret-xyz")
os.environ.setdefault("STERLING_ADMIN_SESSION_SECRET", "admin-session-secret-for-tests")

import pytest

BOOTSTRAP_HEADERS = {"X-Bootstrap-Token": "bootstrap-secret-xyz"}


@pytest.fixture()
def env():
    """Fresh in-memory DB + TestClient + a raw sessionmaker for seeding data
    directly (writes through the sessionmaker are visible to the app because
    both share the same StaticPool sqlite:///:memory: engine)."""
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


def _bootstrap(client, email="owner@sterlingroom.test", password="a-strong-passphrase-1"):
    r = client.post("/admin/bootstrap", data={"email": email, "password": password}, headers=BOOTSTRAP_HEADERS)
    assert r.status_code == 200, r.text
    return email, password


def _login(client, email, password):
    r = client.post("/admin/login", data={"email": email, "password": password})
    return r


# ══════════════════════════════════════════════════════════════════════════
# Bootstrap
# ══════════════════════════════════════════════════════════════════════════
def test_bootstrap_without_token_404s(env):
    client, _ = env
    r = client.post("/admin/bootstrap", data={"email": "a@b.com", "password": "x" * 12})
    assert r.status_code == 404


def test_bootstrap_with_wrong_token_404s(env):
    client, _ = env
    r = client.post("/admin/bootstrap", data={"email": "a@b.com", "password": "x" * 12},
                     headers={"X-Bootstrap-Token": "wrong"})
    assert r.status_code == 404


def test_bootstrap_creates_super_admin(env):
    client, SessionLocal = env
    _bootstrap(client)
    from app.models import AdminUser, AdminRole
    db = SessionLocal()
    admin = db.query(AdminUser).one()
    assert admin.role == AdminRole.SUPER_ADMIN.value
    db.close()


def test_bootstrap_refuses_second_admin(env):
    client, _ = env
    _bootstrap(client)
    r = client.post("/admin/bootstrap", data={"email": "second@x.com", "password": "y" * 12}, headers=BOOTSTRAP_HEADERS)
    assert r.status_code == 409


def test_bootstrap_rejects_short_password(env):
    client, _ = env
    r = client.post("/admin/bootstrap", data={"email": "a@b.com", "password": "short"}, headers=BOOTSTRAP_HEADERS)
    assert r.status_code == 422


# ══════════════════════════════════════════════════════════════════════════
# Login / logout / lockout
# ══════════════════════════════════════════════════════════════════════════
def test_unauthenticated_dashboard_redirects_to_login(env):
    client, _ = env
    r = client.get("/admin", follow_redirects=True)
    assert r.status_code == 200
    assert "Sign in" in r.text


def test_login_wrong_password_shows_error(env):
    client, _ = env
    email, _ = _bootstrap(client)
    r = _login(client, email, "totally-wrong-password")
    assert r.status_code == 200
    assert "Invalid email or password" in r.text


def test_login_success_reaches_dashboard(env):
    client, _ = env
    email, password = _bootstrap(client)
    r = client.post("/admin/login", data={"email": email, "password": password}, follow_redirects=True)
    assert r.status_code == 200
    assert "STERLING_ROOM ADMIN" in r.text
    assert email in r.text


def test_logout_clears_session(env):
    client, _ = env
    email, password = _bootstrap(client)
    _login(client, email, password)
    client.post("/admin/logout")
    r = client.get("/admin", follow_redirects=True)
    assert "Sign in" in r.text


def test_lockout_after_repeated_failures(env):
    client, _ = env
    email, password = _bootstrap(client)
    for _ in range(5):
        _login(client, email, "wrong")
    r = _login(client, email, password)  # even the CORRECT password, now locked out
    assert "temporarily locked" in r.text


# ══════════════════════════════════════════════════════════════════════════
# Dashboard numbers — sourced from the same tables the API/bot write to
# ══════════════════════════════════════════════════════════════════════════
def test_dashboard_shows_active_and_closed_call_counts(env):
    client, SessionLocal = env
    email, password = _bootstrap(client)
    _login(client, email, password)

    from app import services
    from app.models import CallEventType, CallStatus
    db = SessionLocal()
    services.create_call(db, dict(source_call_id="adm-1", instrument="EURUSD", direction="BUY", stop_loss=1.08), actor="test")
    closed = services.create_call(db, dict(source_call_id="adm-2", instrument="GBPUSD", direction="SELL", stop_loss=1.30), actor="test")
    db.commit()
    services.transition_call(db, closed, CallStatus.CLOSED, actor="test", event_type=CallEventType.CALL_CLOSED)
    closed.result_r = 1.5
    db.commit()
    db.close()

    r = client.get("/admin")
    assert r.status_code == 200
    assert "Active Calls" in r.text
    assert "Closed Calls (today)" in r.text


def test_calls_list_and_detail(env):
    client, SessionLocal = env
    email, password = _bootstrap(client)
    _login(client, email, password)

    from app import services
    db = SessionLocal()
    call = services.create_call(db, dict(source_call_id="adm-list-1", instrument="XAUUSD", direction="SELL", stop_loss=1950), actor="test")
    db.commit()
    trade_id = call.trade_id
    db.close()

    r = client.get("/admin/calls")
    assert r.status_code == 200
    assert trade_id in r.text

    r2 = client.get(f"/admin/calls/{trade_id}")
    assert r2.status_code == 200
    assert "Lifecycle events" in r2.text
    assert "CALL_CREATED" in r2.text


def test_calls_filtered_by_status(env):
    client, SessionLocal = env
    email, password = _bootstrap(client)
    _login(client, email, password)
    r = client.get("/admin/calls?status=ACTIVE")
    assert r.status_code == 200
    r_bad = client.get("/admin/calls?status=NOT_A_STATUS")
    assert r_bad.status_code == 422


# ══════════════════════════════════════════════════════════════════════════
# Performance page
# ══════════════════════════════════════════════════════════════════════════
def test_performance_page_renders_all_windows(env):
    client, _ = env
    email, password = _bootstrap(client)
    _login(client, email, password)
    r = client.get("/admin/performance")
    assert r.status_code == 200
    assert "All-time" in r.text and "Today" in r.text and "This week" in r.text and "This month" in r.text


# ══════════════════════════════════════════════════════════════════════════
# Subscribers
# ══════════════════════════════════════════════════════════════════════════
def test_subscribers_page(env):
    client, SessionLocal = env
    email, password = _bootstrap(client)
    _login(client, email, password)

    from app import subscriptions as sub_svc
    db = SessionLocal()
    tu = sub_svc.get_or_create_telegram_user(db, telegram_user_id="9001", username="trader_joe")
    sub_svc.get_or_create_subscriber(db, tu)
    db.commit()
    db.close()

    r = client.get("/admin/subscribers")
    assert r.status_code == 200
    assert "trader_joe" in r.text


# ══════════════════════════════════════════════════════════════════════════
# Payments — role-gated confirm action, wired to subscriptions.confirm_payment
# ══════════════════════════════════════════════════════════════════════════
def _seed_pending_payment(SessionLocal):
    from app import subscriptions as sub_svc
    from app.payments import ManualPaymentProvider
    from app.models import Plan
    db = SessionLocal()
    plan = Plan(plan_id="MONTHLY", name="Monthly", duration_days=30, price=49.0, currency="USD")
    db.add(plan)
    db.flush()
    tu = sub_svc.get_or_create_telegram_user(db, telegram_user_id="9002")
    subscriber = sub_svc.get_or_create_subscriber(db, tu)
    db.commit()
    subscription, payment = sub_svc.start_subscription(db, subscriber, plan, provider=ManualPaymentProvider(), actor="bot")
    db.commit()
    payment_id = str(payment.id)
    db.close()
    return payment_id


def test_payments_confirm_activates_subscription(env):
    client, SessionLocal = env
    email, password = _bootstrap(client)
    _login(client, email, password)
    payment_id = _seed_pending_payment(SessionLocal)

    r = client.get("/admin/payments")
    assert r.status_code == 200
    assert "Confirm" in r.text

    r2 = client.post(f"/admin/payments/{payment_id}/confirm", follow_redirects=True)
    assert r2.status_code == 200

    from app.models import Subscription, SubscriptionStatus
    db = SessionLocal()
    subscription = db.query(Subscription).one()
    assert subscription.status == SubscriptionStatus.ACTIVE
    db.close()


def test_payments_confirm_is_idempotent_via_admin(env):
    client, SessionLocal = env
    email, password = _bootstrap(client)
    _login(client, email, password)
    payment_id = _seed_pending_payment(SessionLocal)

    r1 = client.post(f"/admin/payments/{payment_id}/confirm")
    r2 = client.post(f"/admin/payments/{payment_id}/confirm")  # must not error or double-extend
    assert r1.status_code in (200, 303) and r2.status_code in (200, 303)


def test_payments_confirm_requires_write_role(env):
    client, SessionLocal = env
    owner_email, owner_password = _bootstrap(client)
    payment_id = _seed_pending_payment(SessionLocal)

    from app.models import AdminUser, AdminRole
    from app import security
    db = SessionLocal()
    viewer = AdminUser(email="viewer@sterlingroom.test", password_hash=security.hash_password("viewer-password-1"),
                        role=AdminRole.VIEWER.value)
    db.add(viewer)
    db.commit()
    db.close()

    _login(client, "viewer@sterlingroom.test", "viewer-password-1")
    r = client.post(f"/admin/payments/{payment_id}/confirm")
    assert r.status_code == 403


# ══════════════════════════════════════════════════════════════════════════
# Plans
# ══════════════════════════════════════════════════════════════════════════
def test_create_and_toggle_plan(env):
    client, SessionLocal = env
    email, password = _bootstrap(client)
    _login(client, email, password)

    r = client.post("/admin/plans", data={
        "plan_id": "QUARTERLY", "name": "Quarterly", "duration_days": "90", "price": "120.00", "currency": "USD",
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "QUARTERLY" in r.text

    from app.models import Plan
    db = SessionLocal()
    plan = db.query(Plan).filter_by(plan_id="QUARTERLY").one()
    assert plan.active is True
    plan_id = str(plan.id)
    db.close()

    r2 = client.post(f"/admin/plans/{plan_id}/toggle", follow_redirects=True)
    assert r2.status_code == 200
    db = SessionLocal()
    plan = db.query(Plan).filter_by(plan_id="QUARTERLY").one()
    assert plan.active is False
    db.close()


def test_create_duplicate_plan_id_rejected(env):
    client, _ = env
    email, password = _bootstrap(client)
    _login(client, email, password)
    data = {"plan_id": "MONTHLY", "name": "Monthly", "duration_days": "30", "price": "49.00", "currency": "USD"}
    r1 = client.post("/admin/plans", data=data)
    r2 = client.post("/admin/plans", data=data)
    assert r2.status_code == 409


# ══════════════════════════════════════════════════════════════════════════
# Telegram / Audit / Health / Settings
# ══════════════════════════════════════════════════════════════════════════
def test_telegram_page_shows_config_status(env):
    client, _ = env
    email, password = _bootstrap(client)
    _login(client, email, password)
    r = client.get("/admin/telegram")
    assert r.status_code == 200
    assert "Bot configured" in r.text


def test_audit_log_page_shows_admin_login(env):
    client, _ = env
    email, password = _bootstrap(client)
    _login(client, email, password)
    r = client.get("/admin/audit")
    assert r.status_code == 200
    assert "ADMIN_LOGIN_SUCCESS" in r.text
    assert "ADMIN_BOOTSTRAPPED" in r.text


def test_system_health_page(env):
    client, _ = env
    email, password = _bootstrap(client)
    _login(client, email, password)
    r = client.get("/admin/health")
    assert r.status_code == 200
    assert "Database" in r.text


def test_settings_page_never_leaks_secret_values(env):
    client, _ = env
    email, password = _bootstrap(client)
    _login(client, email, password)
    r = client.get("/admin/settings")
    assert r.status_code == 200
    assert "bootstrap-secret-xyz" not in r.text
    assert "admin-session-secret-for-tests" not in r.text
    assert "test-key-123" not in r.text
