"""PHASE 1 tests — backend and database foundation.

Scope is deliberately narrow: prove the foundation is correct before any auth,
licensing or session logic is built on it. The single most important test here
is the one-active-session constraint, because every later phase depends on it
holding at the storage layer rather than in application code.
"""
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import (  # noqa: E402
    AccountStatus,
    AppSession,
    AuditEvent,
    Base,
    ConsentDocument,
    ConsentRecord,
    Device,
    DeviceStatus,
    Licence,
    LicenceStatus,
    MfaCredential,
    RecoveryCode,
    SessionStatus,
    User,
)


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})

    @event.listens_for(eng, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.cursor().execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def user(db):
    u = User(email=f"{uuid.uuid4().hex[:8]}@example.com", status=AccountStatus.ACTIVE)
    db.add(u)
    db.commit()
    return u


# ── schema sanity ──────────────────────────────────────────────────────────
def test_all_seven_entities_present():
    expected = {
        "users", "licences", "devices", "sessions",
        "mfa_credentials", "recovery_codes", "consent_records", "audit_events",
        "admin_users",
    }
    assert expected == set(Base.metadata.tables.keys())


def test_primary_keys_are_uuid_not_sequential():
    """Sequential IDs leak customer counts and invite enumeration."""
    for table in Base.metadata.tables.values():
        pk = list(table.primary_key.columns)[0]
        assert pk.type.python_type is uuid.UUID, f"{table.name}.{pk.name} is not a UUID"


# ── THE critical invariant ─────────────────────────────────────────────────
def test_two_active_sessions_are_impossible(db, user):
    d1 = Device(user_id=user.id, public_key="pk-laptop")
    d2 = Device(user_id=user.id, public_key="pk-desktop")
    db.add_all([d1, d2])
    db.commit()

    db.add(AppSession(user_id=user.id, device_id=d1.id, status=SessionStatus.ACTIVE))
    db.commit()

    db.add(AppSession(user_id=user.id, device_id=d2.id, status=SessionStatus.ACTIVE))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    active = db.query(AppSession).filter_by(user_id=user.id, status=SessionStatus.ACTIVE).all()
    assert len(active) == 1


def test_non_active_sessions_do_not_block_new_ones(db, user):
    """Otherwise a customer could never log in again after their first session."""
    d1 = Device(user_id=user.id, public_key="pk1")
    d2 = Device(user_id=user.id, public_key="pk2")
    db.add_all([d1, d2])
    db.commit()

    for ended in (SessionStatus.REVOKED, SessionStatus.EXPIRED, SessionStatus.LOGGED_OUT):
        db.add(AppSession(user_id=user.id, device_id=d1.id, status=ended))
    db.commit()

    db.add(AppSession(user_id=user.id, device_id=d2.id, status=SessionStatus.ACTIVE))
    db.commit()

    assert db.query(AppSession).filter_by(user_id=user.id, status=SessionStatus.ACTIVE).count() == 1


def test_takeover_sequence_leaves_exactly_one_active(db, user):
    """The Phase 7 device-switch flow, at the data layer."""
    laptop = Device(user_id=user.id, public_key="pk-laptop")
    desktop = Device(user_id=user.id, public_key="pk-desktop")
    db.add_all([laptop, desktop])
    db.commit()

    s1 = AppSession(user_id=user.id, device_id=laptop.id, status=SessionStatus.ACTIVE)
    db.add(s1)
    db.commit()

    # takeover: revoke then create, in one transaction
    s1.status = SessionStatus.REVOKED
    s1.end_reason = "TAKEOVER_BY_NEW_DEVICE"
    db.add(AppSession(user_id=user.id, device_id=desktop.id, status=SessionStatus.ACTIVE))
    db.commit()

    active = db.query(AppSession).filter_by(user_id=user.id, status=SessionStatus.ACTIVE).all()
    assert len(active) == 1
    assert active[0].device_id == desktop.id
    assert s1.status is SessionStatus.REVOKED


def test_two_different_users_may_each_have_an_active_session(db):
    """The constraint is per-user, not global."""
    users = []
    for _ in range(2):
        u = User(email=f"{uuid.uuid4().hex[:8]}@example.com", status=AccountStatus.ACTIVE)
        db.add(u)
        db.commit()
        d = Device(user_id=u.id, public_key=f"pk-{u.id}")
        db.add(d)
        db.commit()
        db.add(AppSession(user_id=u.id, device_id=d.id, status=SessionStatus.ACTIVE))
        db.commit()
        users.append(u)

    assert db.query(AppSession).filter_by(status=SessionStatus.ACTIVE).count() == 2


# ── relational integrity ───────────────────────────────────────────────────
def test_foreign_keys_enforced(db):
    orphan = Device(user_id=uuid.uuid4(), public_key="pk")
    db.add(orphan)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_deleting_user_cascades(db, user):
    d = Device(user_id=user.id, public_key="pk")
    db.add(d)
    db.commit()
    db.add(AppSession(user_id=user.id, device_id=d.id, status=SessionStatus.ACTIVE))
    db.add(Licence(user_id=user.id, status=LicenceStatus.ACTIVE))
    db.commit()

    db.delete(user)
    db.commit()

    assert db.query(Device).count() == 0
    assert db.query(AppSession).count() == 0
    assert db.query(Licence).count() == 0


def test_email_is_unique(db):
    db.add(User(email="dup@example.com"))
    db.commit()
    db.add(User(email="dup@example.com"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_one_mfa_credential_per_user(db, user):
    db.add(MfaCredential(user_id=user.id, secret_encrypted="enc1"))
    db.commit()
    db.add(MfaCredential(user_id=user.id, secret_encrypted="enc2"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# ── defaults and business rules ────────────────────────────────────────────
def test_safe_defaults(db, user):
    u2 = User(email="new@example.com")
    db.add(u2)
    db.commit()
    assert u2.status is AccountStatus.PENDING, "new accounts must not default to ACTIVE"

    lic = Licence(user_id=user.id)
    db.add(lic)
    db.commit()
    assert lic.status is LicenceStatus.PENDING, "new licences must not default to ACTIVE"
    assert lic.max_concurrent_sessions == 1

    d = Device(user_id=user.id, public_key="pk")
    db.add(d)
    db.commit()
    assert d.status is DeviceStatus.ACTIVE


def test_consent_records_are_versioned(db, user):
    for doc in ConsentDocument:
        db.add(ConsentRecord(
            user_id=user.id, document=doc, document_version="1.0",
            accepted=True, app_version="1.0.0",
        ))
    db.commit()
    rows = db.query(ConsentRecord).filter_by(user_id=user.id).all()
    assert len(rows) == 3
    assert all(r.document_version == "1.0" and r.accepted_at is not None for r in rows)


def test_recovery_codes_store_hashes_not_plaintext(db, user):
    cred = MfaCredential(user_id=user.id, secret_encrypted="enc")
    db.add(cred)
    db.commit()
    db.add_all([RecoveryCode(credential_id=cred.id, code_hash=f"$argon2id$hash{i}") for i in range(10)])
    db.commit()

    codes = db.query(RecoveryCode).all()
    assert len(codes) == 10
    assert all(c.used_at is None for c in codes)
    assert all(c.code_hash.startswith("$argon2") for c in codes)


def test_audit_event_can_be_written(db, user):
    db.add(AuditEvent(
        event_type="LICENCE_ACTIVATED", actor="admin:owner",
        target_user_id=user.id, result="SUCCESS", detail="manual activation after cash payment",
    ))
    db.commit()
    ev = db.query(AuditEvent).one()
    assert ev.created_at is not None and ev.event_type == "LICENCE_ACTIVATED"


# ── configuration guard rails ──────────────────────────────────────────────
def test_production_refuses_sqlite_and_missing_keys():
    from app.config import Settings

    unsafe = Settings(
        ENV="production", DEBUG=True,
        DATABASE_URL="sqlite:///./x.db",
        SIGNING_PRIVATE_KEY_B64="", SIGNING_PUBLIC_KEY_B64="",
    )
    problems = unsafe.assert_production_ready()
    joined = " ".join(problems).lower()
    assert any("sqlite" in p.lower() for p in problems)
    assert "debug" in joined
    assert any("private" in p.lower() for p in problems)

    safe = Settings(
        ENV="production", DEBUG=False,
        DATABASE_URL="postgresql+psycopg://u:p@h:5432/db",
        SIGNING_PRIVATE_KEY_B64="x", SIGNING_PUBLIC_KEY_B64="y",
    )
    assert safe.assert_production_ready() == []


def test_no_secret_defaults_in_config():
    """A signing key must never have a usable default value."""
    from app.config import Settings

    s = Settings()
    assert s.SIGNING_PRIVATE_KEY_B64 == ""
    assert s.SIGNING_PUBLIC_KEY_B64 == ""


# ── signing keys ───────────────────────────────────────────────────────────
def test_keygen_produces_usable_ed25519_pair():
    import base64
    from cryptography.hazmat.primitives import serialization

    from app.keygen import generate

    priv_b64, pub_b64 = generate()
    priv = serialization.load_pem_private_key(base64.b64decode(priv_b64), password=None)
    pub = serialization.load_pem_public_key(base64.b64decode(pub_b64))

    sig = priv.sign(b"grant-payload")
    pub.verify(sig, b"grant-payload")          # must not raise

    with pytest.raises(Exception):
        pub.verify(sig, b"tampered-payload")   # must raise

    assert generate()[0] != priv_b64, "keys must not be deterministic"
