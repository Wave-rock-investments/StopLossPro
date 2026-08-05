"""PHASE 17 — self-serve registration (PENDING) + admin approve/reject.

Model already supported AccountStatus.PENDING as the default and
authenticate() already refused non-ACTIVE accounts; this phase adds the
missing public entry point (POST /auth/register) and the admin actions that
turn a pending signup into either a real licence or a closed account.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import admin as admin_mod  # noqa: E402
from app import security  # noqa: E402
from app.api import _BUCKETS as _rate_limit_buckets  # noqa: E402
from app.api import router as api_router  # noqa: E402
from app.database import get_db  # noqa: E402
from app.models import (  # noqa: E402
    AccountStatus, AuditEvent, Base, Licence, LicenceStatus, User,
)


@pytest.fixture()
def db_session():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)

    @event.listens_for(eng, "connect")
    def _fk(c, _):
        c.cursor().execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(eng)
    SF = sessionmaker(bind=eng)
    s = SF()
    try:
        yield s, SF
    finally:
        s.close()


@pytest.fixture()
def client(db_session):
    session, SF = db_session
    app = FastAPI()
    app.include_router(admin_mod.router)
    app.include_router(api_router, prefix="/api/v1")

    def _override_get_db():
        s = SF()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_get_db
    admin_mod._ADMIN_SESSIONS.clear()
    _rate_limit_buckets.clear()
    with TestClient(app, base_url="https://testserver") as c:
        yield c
    admin_mod._ADMIN_SESSIONS.clear()
    _rate_limit_buckets.clear()


def _make_admin(db_session, *, password="AdminPass123!"):
    session, _SF = db_session
    a = admin_mod.AdminUser(
        email="ops@example.com", password_hash=security.hash_password(password),
        totp_confirmed=False,
    )
    session.add(a)
    session.commit()
    return a


def _login_admin(client, email="ops@example.com", password="AdminPass123!"):
    r = client.post("/admin/login", data={"email": email, "password": password, "totp": ""},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers.get("location") == "/admin"


# ══════════════════════════════════════════════════════════════════════════
# POST /auth/register
# ══════════════════════════════════════════════════════════════════════════
def test_register_creates_pending_account_with_no_licence(client, db_session):
    session, _SF = db_session
    r = client.post("/api/v1/auth/register", json={
        "email": "NewCustomer@Example.com", "password": "CorrectHorseBattery1!",
        "full_name": "New Customer",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "pending"

    u = session.execute(select(User).where(User.email == "newcustomer@example.com")).scalar_one()
    assert u.status is AccountStatus.PENDING
    assert u.full_name == "New Customer"
    assert security.verify_password("CorrectHorseBattery1!", u.password_hash)

    lic = session.execute(select(Licence).where(Licence.user_id == u.id)).scalar_one_or_none()
    assert lic is None, "registration must not grant a licence"


def test_register_rejects_short_password(client, db_session):
    session, _SF = db_session
    r = client.post("/api/v1/auth/register", json={
        "email": "shortpw@example.com", "password": "short1!",
    })
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "WEAK_PASSWORD"
    assert session.execute(select(User)).scalars().first() is None


def test_register_rejects_duplicate_email(client, db_session):
    r1 = client.post("/api/v1/auth/register", json={
        "email": "dupe@example.com", "password": "CorrectHorseBattery1!",
    })
    assert r1.status_code == 200

    r2 = client.post("/api/v1/auth/register", json={
        "email": "Dupe@Example.com", "password": "AnotherPassword2!",
    })
    assert r2.status_code == 409
    assert r2.json()["detail"]["code"] == "EMAIL_ALREADY_REGISTERED"


def test_pending_account_cannot_log_in(client, db_session):
    client.post("/api/v1/auth/register", json={
        "email": "pending@example.com", "password": "CorrectHorseBattery1!",
    })
    r = client.post("/api/v1/auth/login", json={
        "email": "pending@example.com", "password": "CorrectHorseBattery1!",
        "device_public_key": "x" * 20,
    })
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "ACCOUNT_NOT_ACTIVE"


def test_register_is_rate_limited_per_ip(client):
    for i in range(5):
        client.post("/api/v1/auth/register", json={
            "email": f"rl{i}@example.com", "password": "CorrectHorseBattery1!",
        })
    r = client.post("/api/v1/auth/register", json={
        "email": "rl-overflow@example.com", "password": "CorrectHorseBattery1!",
    })
    assert r.status_code == 429


def test_register_is_rate_limited_per_email(client):
    for _ in range(3):
        client.post("/api/v1/auth/register", json={
            "email": "hammered@example.com", "password": "CorrectHorseBattery1!",
        })
    r = client.post("/api/v1/auth/register", json={
        "email": "hammered@example.com", "password": "CorrectHorseBattery1!",
    })
    assert r.status_code == 429


# ══════════════════════════════════════════════════════════════════════════
# Admin: approve / reject pending signups
# ══════════════════════════════════════════════════════════════════════════
def test_pending_signup_appears_on_admin_customers_page(client, db_session):
    _make_admin(db_session)
    _login_admin(client)
    client.post("/api/v1/auth/register", json={
        "email": "visible@example.com", "password": "CorrectHorseBattery1!",
    })
    r = client.get("/admin")
    assert "visible@example.com" in r.text
    assert "Pending signups" in r.text


def test_approve_activates_account_and_creates_licence(client, db_session):
    session, _SF = db_session
    _make_admin(db_session)
    _login_admin(client)
    client.post("/api/v1/auth/register", json={
        "email": "approve-me@example.com", "password": "CorrectHorseBattery1!",
    })
    u = session.execute(select(User).where(User.email == "approve-me@example.com")).scalar_one()

    r = client.post(f"/admin/signup/{u.id}/approve", data={"days": "90", "note": "crypto 2026-08-05"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers.get("location") == f"/admin/customer/{u.id}"

    session.expire_all()
    u2 = session.get(User, u.id)
    assert u2.status is AccountStatus.ACTIVE
    lic = session.execute(select(Licence).where(Licence.user_id == u.id)).scalar_one()
    assert lic.status is LicenceStatus.ACTIVE
    assert lic.activation_note == "crypto 2026-08-05"

    ev = session.execute(select(AuditEvent).where(
        AuditEvent.event_type == "SIGNUP_APPROVED")).scalars().first()
    assert ev is not None and ev.target_user_id == u.id


def test_approved_account_can_now_log_in(client, db_session):
    """Full realistic journey: register -> approve -> still blocked on
    outstanding consent (same gate every account hits, see test_admin_bootstrap
    et al.) -> accept every required document -> login succeeds."""
    session, _SF = db_session
    _make_admin(db_session)
    _login_admin(client)
    client.post("/api/v1/auth/register", json={
        "email": "canlogin@example.com", "password": "CorrectHorseBattery1!",
    })
    u = session.execute(select(User).where(User.email == "canlogin@example.com")).scalar_one()
    client.post(f"/admin/signup/{u.id}/approve", data={"days": "30", "note": ""})

    login_payload = {"email": "canlogin@example.com", "password": "CorrectHorseBattery1!",
                     "device_public_key": "x" * 20}

    r_blocked = client.post("/api/v1/auth/login", json=login_payload)
    assert r_blocked.status_code == 403
    assert r_blocked.json()["detail"]["code"] == "CONSENT_REQUIRED"

    outstanding = client.get("/api/v1/consent/required",
                             params={"email": "canlogin@example.com"}).json()["outstanding"]
    assert len(outstanding) == 3
    for doc in outstanding:
        r = client.post("/api/v1/consent/accept", params={"email": "canlogin@example.com"},
                        json={"document": doc["document"], "version": doc["version"],
                              "accepted": True, "app_version": "1.0.0"})
        assert r.status_code == 200

    r_ok = client.post("/api/v1/auth/login", json=login_payload)
    assert r_ok.status_code == 200


def test_reject_closes_account_without_licence(client, db_session):
    session, _SF = db_session
    _make_admin(db_session)
    _login_admin(client)
    client.post("/api/v1/auth/register", json={
        "email": "reject-me@example.com", "password": "CorrectHorseBattery1!",
    })
    u = session.execute(select(User).where(User.email == "reject-me@example.com")).scalar_one()

    r = client.post(f"/admin/signup/{u.id}/reject", follow_redirects=False)
    assert r.status_code == 303

    session.expire_all()
    u2 = session.get(User, u.id)
    assert u2.status is AccountStatus.CLOSED
    assert session.execute(select(Licence).where(Licence.user_id == u.id)).scalars().first() is None

    ev = session.execute(select(AuditEvent).where(
        AuditEvent.event_type == "SIGNUP_REJECTED")).scalars().first()
    assert ev is not None and ev.target_user_id == u.id


def test_approve_reject_require_admin_auth(client, db_session):
    session, _SF = db_session
    client.post("/api/v1/auth/register", json={
        "email": "unauth@example.com", "password": "CorrectHorseBattery1!",
    })
    u = session.execute(select(User).where(User.email == "unauth@example.com")).scalar_one()

    r1 = client.post(f"/admin/signup/{u.id}/approve", data={"days": "30"}, follow_redirects=False)
    assert r1.status_code == 303 and r1.headers.get("location") == "/admin/login"

    r2 = client.post(f"/admin/signup/{u.id}/reject", follow_redirects=False)
    assert r2.status_code == 303 and r2.headers.get("location") == "/admin/login"

    session.expire_all()
    assert session.get(User, u.id).status is AccountStatus.PENDING


def test_approve_on_already_active_account_is_404(client, db_session):
    session, _SF = db_session
    _make_admin(db_session)
    _login_admin(client)
    u = User(email="already@example.com", status=AccountStatus.ACTIVE,
             password_hash=security.hash_password("CorrectHorseBattery1!"))
    session.add(u)
    session.commit()

    r = client.post(f"/admin/signup/{u.id}/approve", data={"days": "30"})
    assert r.status_code == 404
