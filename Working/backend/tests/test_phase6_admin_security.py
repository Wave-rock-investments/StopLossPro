"""STEP 6 — admin panel adversarial security tests.

The admin account is the highest-value identity in the system: compromising
it compromises every customer licence. This file treats it accordingly.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyotp
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import admin as admin_mod  # noqa: E402
from app import security, services  # noqa: E402
from app.api import _BUCKETS as _rate_limit_buckets  # noqa: E402
from app.api import router as api_router  # noqa: E402
from app.database import get_db  # noqa: E402
from app.models import (  # noqa: E402
    AccountStatus, AppSession, AuditEvent, Base, Device, DeviceStatus,
    Licence, LicenceStatus, SessionStatus, User, utcnow,
)


@pytest.fixture()
def db_session():
    # StaticPool: the admin panel opens a SEPARATE session per request (via
    # the dependency-injected get_db override) from the one this fixture
    # writes fixture data with. Plain sqlite:///:memory: hands out a fresh,
    # empty, private database per connection — StaticPool forces every
    # connection from this engine to share the same one.
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
    _rate_limit_buckets.clear()  # module-global; must not leak between tests
    with TestClient(app, base_url="https://testserver") as c:
        yield c
    admin_mod._ADMIN_SESSIONS.clear()
    _rate_limit_buckets.clear()


def _make_admin(db_session, *, mfa=True, password="AdminPass123!"):
    session, SF = db_session
    secret = security.new_totp_secret()
    a = admin_mod.AdminUser(
        email=f"{__import__('uuid').uuid4().hex[:8]}@ops.example",
        password_hash=security.hash_password(password),
        totp_secret_encrypted=security.encrypt_totp_secret(secret) if mfa else None,
        totp_confirmed=mfa,
    )
    session.add(a)
    session.commit()
    return a, secret


def _make_customer(db_session, *, password="CorrectHorse1!"):
    session, SF = db_session
    u = User(email=f"{__import__('uuid').uuid4().hex[:8]}@customer.example",
             status=AccountStatus.ACTIVE, password_hash=security.hash_password(password))
    session.add(u)
    session.flush()
    session.add(Licence(user_id=u.id, status=LicenceStatus.ACTIVE,
                        activated_at=utcnow(), expires_at=utcnow() + timedelta(days=365)))
    session.commit()
    return u


def _login(client, email, password, totp=""):
    return client.post("/admin/login", data={"email": email, "password": password, "totp": totp},
                       follow_redirects=False)


# ══════════════════════════════════════════════════════════════════════════
# Unauthenticated / cross-role access is BLOCKED
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("method,path", [
    ("GET", "/admin"),
    ("GET", "/admin/audit"),
    ("POST", "/admin/user/00000000-0000-0000-0000-000000000000/force-logout"),
    ("POST", "/admin/user/00000000-0000-0000-0000-000000000000/reset-mfa"),
    ("POST", "/admin/device/00000000-0000-0000-0000-000000000000/revoke"),
    ("POST", "/admin/licence/00000000-0000-0000-0000-000000000000/action"),
])
def test_unauthenticated_request_never_reaches_admin_logic(client, method, path):
    r = client.request(method, path, follow_redirects=False,
                       data={"action": "revoke"} if "licence" in path else None)
    assert r.status_code == 303, f"{method} {path} did not redirect-to-login unauthenticated: {r.status_code}"
    assert r.headers.get("location") == "/admin/login"


def test_customer_credentials_are_not_a_valid_admin_credential(client, db_session):
    """A customer bearer session token must be completely inert against
    admin routes — the admin cookie and the customer bearer scheme share
    nothing, but this proves it rather than assumes it."""
    u = _make_customer(db_session)
    session, SF = db_session
    d = Device(user_id=u.id, public_key="pk", status=DeviceStatus.ACTIVE)
    session.add(d)
    session.commit()
    _, tok = services.start_session(session, u, d)

    r = client.get("/admin", headers={"Authorization": f"Bearer {tok}"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers.get("location") == "/admin/login"


def test_forged_admin_cookie_is_rejected(client):
    client.cookies.set(admin_mod.COOKIE, "totally-fabricated-token")
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers.get("location") == "/admin/login"


def test_expired_admin_session_is_rejected(client, db_session):
    admin, secret = _make_admin(db_session)
    r = _login(client, admin.email, "AdminPass123!", pyotp.TOTP(secret).now())
    assert r.status_code == 303 and r.headers.get("location") == "/admin"

    tok = client.cookies.get(admin_mod.COOKIE)
    admin_mod._ADMIN_SESSIONS[tok]["expires"] = datetime.now(timezone.utc) - timedelta(seconds=1)

    r2 = client.get("/admin", follow_redirects=False)
    assert r2.status_code == 303 and r2.headers.get("location") == "/admin/login"
    assert tok not in admin_mod._ADMIN_SESSIONS, "expired session must be evicted, not just ignored"


# ══════════════════════════════════════════════════════════════════════════
# MFA is mandatory and cannot be bypassed
# ══════════════════════════════════════════════════════════════════════════
def test_wrong_password_rejected(client, db_session):
    admin, secret = _make_admin(db_session)
    r = _login(client, admin.email, "WrongPassword!", pyotp.TOTP(secret).now())
    assert r.status_code == 303
    assert "error" in r.headers.get("location", "")
    assert admin_mod.COOKIE not in client.cookies


def test_login_without_mfa_code_rejected_when_mfa_confirmed(client, db_session):
    admin, _secret = _make_admin(db_session)
    r = _login(client, admin.email, "AdminPass123!", "")
    assert r.status_code == 303 and "Invalid+authenticator" in r.headers.get("location", "")
    assert admin_mod.COOKIE not in client.cookies


def test_wrong_totp_rejected(client, db_session):
    admin, _secret = _make_admin(db_session)
    r = _login(client, admin.email, "AdminPass123!", "000000")
    assert r.status_code == 303 and "Invalid+authenticator" in r.headers.get("location", "")
    assert admin_mod.COOKIE not in client.cookies


def test_replayed_totp_rejected(client, db_session):
    """A code good enough to log in once must never work a second time."""
    session, SF = db_session
    admin, secret = _make_admin(db_session)
    code = pyotp.TOTP(secret).now()

    r1 = _login(client, admin.email, "AdminPass123!", code)
    assert r1.status_code == 303 and r1.headers.get("location") == "/admin"
    client.cookies.clear()

    r2 = _login(client, admin.email, "AdminPass123!", code)
    assert r2.status_code == 303 and "Invalid+authenticator" in r2.headers.get("location", "")


def test_unknown_admin_email_and_wrong_password_indistinguishable(client, db_session):
    admin, secret = _make_admin(db_session)
    r_wrong = _login(client, admin.email, "WrongPassword!", "")
    r_ghost = _login(client, "nonexistent-admin@ops.example", "WrongPassword!", "")
    assert r_wrong.headers.get("location") == r_ghost.headers.get("location")


# ══════════════════════════════════════════════════════════════════════════
# Brute force is rate limited (password AND TOTP)
# ══════════════════════════════════════════════════════════════════════════
def test_password_brute_force_is_rate_limited(client, db_session):
    admin, secret = _make_admin(db_session)
    statuses = []
    for _ in range(15):
        r = _login(client, admin.email, "WrongEachTime!", "")
        statuses.append(r.status_code)
    assert 429 in statuses, f"no rate limit ever triggered across 15 attempts: {statuses}"


def test_totp_brute_force_is_rate_limited(client, db_session):
    admin, secret = _make_admin(db_session)
    statuses = []
    for i in range(15):
        r = _login(client, admin.email, "AdminPass123!", f"{i:06d}")
        statuses.append(r.status_code)
    assert 429 in statuses, f"no rate limit ever triggered across 15 TOTP attempts: {statuses}"


# ══════════════════════════════════════════════════════════════════════════
# Authorization boundaries: no customer-reachable path exists for admin actions
# ══════════════════════════════════════════════════════════════════════════
def test_no_customer_facing_route_can_mutate_a_licence():
    """Enumerate the customer-facing API router and confirm none of its
    routes touch licence mutation, device revocation of ANOTHER user, or
    admin creation. This is a regression guard: it fails loudly if someone
    later adds such a route to api.py without putting it behind admin auth."""
    paths = {r.path for r in api_router.routes}
    for p in paths:
        assert "/licence" not in p, f"customer router exposes a licence route: {p}"
        assert "/admin" not in p, f"customer router exposes an admin route: {p}"


def test_customer_cannot_revoke_another_customers_device_or_create_an_admin(client, db_session):
    session, SF = db_session
    u1 = _make_customer(db_session)
    u2 = _make_customer(db_session)

    d1 = Device(user_id=u1.id, public_key="pk1", status=DeviceStatus.ACTIVE)
    d2 = Device(user_id=u2.id, public_key="pk2", status=DeviceStatus.ACTIVE)
    session.add_all([d1, d2])
    session.commit()

    _, tok1 = services.start_session(session, u1, d1)

    # u1's own valid bearer token, pointed at u2's device via the ADMIN route.
    # If this succeeded it would mean any customer could revoke anyone's
    # device by guessing a UUID — it must not, because the admin route
    # ignores Authorization entirely and only trusts the admin cookie.
    r = client.post(f"/admin/device/{d2.id}/revoke",
                    headers={"Authorization": f"Bearer {tok1}"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers.get("location") == "/admin/login"
    session.refresh(d2)
    assert d2.status is DeviceStatus.ACTIVE, "customer bearer token revoked another user's device"

    # There is no customer-facing route to create an admin account at all —
    # confirm no path under the customer router even contains "admin".
    assert not any("admin" in r.path.lower() for r in api_router.routes)


def test_admin_licence_action_is_audited(client, db_session):
    session, SF = db_session
    admin, secret = _make_admin(db_session)
    u = _make_customer(db_session)
    lic = session.execute(select(Licence).where(Licence.user_id == u.id)).scalar_one()

    _login(client, admin.email, "AdminPass123!", pyotp.TOTP(secret).now())
    r = client.post(f"/admin/licence/{lic.id}/action", data={"action": "suspend"},
                    follow_redirects=False)
    assert r.status_code == 303

    events = session.execute(select(AuditEvent).where(
        AuditEvent.event_type == "LICENCE_SUSPEND")).scalars().all()
    assert len(events) == 1
    assert events[0].actor == f"admin:{admin.email}"
    assert events[0].target_user_id == u.id


def test_admin_mfa_reset_is_audited(client, db_session):
    session, SF = db_session
    admin, secret = _make_admin(db_session)
    u = _make_customer(db_session)

    _login(client, admin.email, "AdminPass123!", pyotp.TOTP(secret).now())
    r = client.post(f"/admin/user/{u.id}/reset-mfa", follow_redirects=False)
    assert r.status_code == 303

    events = session.execute(select(AuditEvent).where(
        AuditEvent.event_type == "MFA_RESET", AuditEvent.target_user_id == u.id)).scalars().all()
    assert len(events) == 1
    assert events[0].actor == f"admin:{admin.email}"


def test_admin_force_logout_is_audited(client, db_session):
    session, SF = db_session
    admin, secret = _make_admin(db_session)
    u = _make_customer(db_session)

    _login(client, admin.email, "AdminPass123!", pyotp.TOTP(secret).now())
    r = client.post(f"/admin/user/{u.id}/force-logout", follow_redirects=False)
    assert r.status_code == 303

    events = session.execute(select(AuditEvent).where(
        AuditEvent.event_type == "ADMIN_FORCE_LOGOUT", AuditEvent.target_user_id == u.id)).scalars().all()
    assert len(events) == 1


def test_admin_login_success_and_failure_are_both_audited(client, db_session):
    session, SF = db_session
    admin, secret = _make_admin(db_session)

    _login(client, admin.email, "WrongPassword!", "")
    _login(client, admin.email, "AdminPass123!", pyotp.TOTP(secret).now())

    types = {e.event_type for e in session.execute(select(AuditEvent)).scalars()}
    assert "ADMIN_LOGIN_FAILED" in types
    assert "ADMIN_LOGIN" in types


# ══════════════════════════════════════════════════════════════════════════
# Cookie hardening (the actual, testable half of CSRF defense — SameSite
# enforcement itself is a browser behavior TestClient cannot simulate; what
# CAN be verified here is that the cookie is configured to trigger it).
# ══════════════════════════════════════════════════════════════════════════
def test_admin_session_cookie_is_hardened(client, db_session):
    admin, secret = _make_admin(db_session)
    r = _login(client, admin.email, "AdminPass123!", pyotp.TOTP(secret).now())
    set_cookie = r.headers.get("set-cookie", "")
    assert admin_mod.COOKIE in set_cookie
    assert "httponly" in set_cookie.lower(), "cookie not HttpOnly — readable by any injected script"
    assert "samesite=strict" in set_cookie.lower(), (
        "cookie is not SameSite=Strict — this is the primary CSRF defense for "
        "this panel (no CSRF token exists); weakening it to Lax or None "
        "reopens cross-site form submission against every mutating endpoint"
    )
    assert "secure" in set_cookie.lower(), "cookie not marked Secure — could be sent over plain HTTP"
