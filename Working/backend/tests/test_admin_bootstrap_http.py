"""Tests for the HTTP admin-bootstrap route (app/admin.py).

This is the free-tier alternative to `python -m app.bootstrap_admin` for
hosts with no shell access (e.g. Render's Free instance type). Same rules as
that script — interactive-equivalent, MFA mandatory, refuses once an admin
exists — enforced twice as hard here because this surface is reachable over
the network: nothing works unless a long random token is set AND matches,
and every write re-checks that zero admins exist immediately before commit.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pyotp
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
from app.models import AuditEvent, Base  # noqa: E402

TOKEN = "test-bootstrap-token-" + "x" * 20
GOOD_PW = "CorrectHorseBattery1!"


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
def client(db_session, monkeypatch):
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
    monkeypatch.setattr(admin_mod, "get_settings",
                        lambda: SimpleNamespace(ADMIN_BOOTSTRAP_TOKEN=TOKEN))
    with TestClient(app, base_url="https://testserver") as c:
        yield c
    _rate_limit_buckets.clear()


def _make_admin(db_session, *, password="AdminPass123!"):
    session, SF = db_session
    secret = security.new_totp_secret()
    a = admin_mod.AdminUser(
        email="existing@ops.example",
        password_hash=security.hash_password(password),
        totp_secret_encrypted=security.encrypt_totp_secret(secret),
        totp_confirmed=True,
    )
    session.add(a)
    session.commit()
    return a


def _step1(client, email="owner@example.com", password=GOOD_PW, password2=GOOD_PW, token=TOKEN):
    return client.post("/admin/bootstrap", params={"token": token},
                       data={"email": email, "password": password, "password2": password2})


def _extract(html: str):
    secret = re.search(r"Secret: <code>([A-Z2-7]+)</code>", html).group(1)
    pending = re.search(r'name="pending" value="([^"]+)"', html).group(1)
    return secret, pending


# ══════════════════════════════════════════════════════════════════════════
# Gating — the route must be inert unless the token is set, matches, AND
# zero admins currently exist
# ══════════════════════════════════════════════════════════════════════════
def test_no_token_configured_is_404(client, monkeypatch):
    monkeypatch.setattr(admin_mod, "get_settings", lambda: SimpleNamespace(ADMIN_BOOTSTRAP_TOKEN=""))
    r = client.get("/admin/bootstrap", params={"token": "anything"})
    assert r.status_code == 404


def test_wrong_token_is_404(client):
    r = client.get("/admin/bootstrap", params={"token": "wrong-token"})
    assert r.status_code == 404


def test_missing_token_is_404(client):
    r = client.get("/admin/bootstrap")
    assert r.status_code == 404


def test_correct_token_with_zero_admins_shows_form(client):
    r = client.get("/admin/bootstrap", params={"token": TOKEN})
    assert r.status_code == 200
    assert "Create the first StopLossPro administrator" in r.text


def test_route_disabled_once_an_admin_exists(client, db_session):
    _make_admin(db_session)
    r = client.get("/admin/bootstrap", params={"token": TOKEN})
    assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Happy path
# ══════════════════════════════════════════════════════════════════════════
def test_full_bootstrap_flow_creates_confirmed_super_admin(client, db_session):
    session, _SF = db_session

    r1 = _step1(client, email="Owner@Example.com")
    assert r1.status_code == 200
    assert "Add this to your authenticator app" in r1.text
    secret, pending = _extract(r1.text)

    code = pyotp.TOTP(secret).now()
    r2 = client.post("/admin/bootstrap/confirm", params={"token": TOKEN},
                     data={"pending": pending, "code": code})
    assert r2.status_code == 200
    assert "Administrator created" in r2.text

    admin = session.execute(select(admin_mod.AdminUser)).scalars().first()
    assert admin is not None
    assert admin.email == "owner@example.com"
    assert admin.totp_confirmed is True
    assert admin.role == admin_mod.AdminRole.SUPER_ADMIN.value
    assert security.verify_password(GOOD_PW, admin.password_hash)

    ok, _step = security.verify_totp(security.decrypt_totp_secret(admin.totp_secret_encrypted), code)
    assert ok


def test_second_bootstrap_attempt_after_first_succeeds_is_404(client, db_session):
    r1 = _step1(client, email="first@example.com")
    secret, pending = _extract(r1.text)
    code = pyotp.TOTP(secret).now()
    client.post("/admin/bootstrap/confirm", params={"token": TOKEN},
               data={"pending": pending, "code": code})

    r3 = client.get("/admin/bootstrap", params={"token": TOKEN})
    assert r3.status_code == 404


def test_concurrent_bootstrap_second_confirm_loses_the_race(client, db_session):
    """Two step-1 submissions before either confirms; the second confirm to
    land must be rejected even though its own pending token is still valid."""
    r1 = _step1(client, email="racer-a@example.com")
    secret_a, pending_a = _extract(r1.text)
    r2 = _step1(client, email="racer-b@example.com")
    secret_b, pending_b = _extract(r2.text)

    ok1 = client.post("/admin/bootstrap/confirm", params={"token": TOKEN},
                      data={"pending": pending_a, "code": pyotp.TOTP(secret_a).now()})
    assert ok1.status_code == 200
    assert "Administrator created" in ok1.text

    ok2 = client.post("/admin/bootstrap/confirm", params={"token": TOKEN},
                      data={"pending": pending_b, "code": pyotp.TOTP(secret_b).now()})
    assert ok2.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Input validation — nothing is created on any rejected path
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("email,password,password2,expected", [
    ("not-an-email", GOOD_PW, GOOD_PW, "does not look like an email"),
    ("a@b.com", "short", "short", "at least 12 characters"),
    ("a@b.com", GOOD_PW, "Mismatch1!!!", "do not match"),
])
def test_step1_validation_rejects_bad_input(client, db_session, email, password, password2, expected):
    session, _SF = db_session
    r = _step1(client, email=email, password=password, password2=password2)
    assert r.status_code == 200
    assert expected in r.text
    assert session.execute(select(admin_mod.AdminUser)).scalars().first() is None


def test_wrong_totp_code_does_not_create_admin(client, db_session):
    session, _SF = db_session
    r1 = _step1(client)
    _secret, pending = _extract(r1.text)

    r2 = client.post("/admin/bootstrap/confirm", params={"token": TOKEN},
                     data={"pending": pending, "code": "000000"})
    assert "did not verify" in r2.text
    assert session.execute(select(admin_mod.AdminUser)).scalars().first() is None


def test_tampered_pending_blob_is_rejected(client):
    r1 = _step1(client)
    _secret, pending = _extract(r1.text)
    tampered = pending[:-4] + "AAAA"

    r2 = client.post("/admin/bootstrap/confirm", params={"token": TOKEN},
                     data={"pending": tampered, "code": "123456"})
    assert "expired or was tampered" in r2.text


def test_wrong_token_on_confirm_step_is_404_even_with_valid_pending(client):
    r1 = _step1(client)
    _secret, pending = _extract(r1.text)

    r2 = client.post("/admin/bootstrap/confirm", params={"token": "not-the-token"},
                     data={"pending": pending, "code": "123456"})
    assert r2.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Rate limiting + audit trail
# ══════════════════════════════════════════════════════════════════════════
def test_bootstrap_route_is_rate_limited(client):
    for _ in range(10):
        client.get("/admin/bootstrap", params={"token": "wrong"})
    r = client.get("/admin/bootstrap", params={"token": "wrong"})
    assert r.status_code == 429


def test_audit_event_recorded_with_no_secret_in_detail(client, db_session):
    session, _SF = db_session
    r1 = _step1(client)
    secret, pending = _extract(r1.text)
    code = pyotp.TOTP(secret).now()
    client.post("/admin/bootstrap/confirm", params={"token": TOKEN},
               data={"pending": pending, "code": code})

    ev = session.execute(select(AuditEvent).where(
        AuditEvent.event_type == "ADMIN_BOOTSTRAPPED")).scalars().first()
    assert ev is not None
    assert GOOD_PW not in (ev.detail or "")
    assert secret not in (ev.detail or "")
