"""STEP 3 — production PostgreSQL validation.

SQLite's row lock is a documented no-op (see app/config.py, app/services.py
`_lock_user_row`). The one-active-session guarantee is real only if it holds
under a real PostgreSQL `SELECT ... FOR UPDATE`. This file is what proves
that, against an actual PostgreSQL server — not an assumption, not the
SQLite result extrapolated.

Requires STOPLOSS_PG_TEST_URL pointing at a live, disposable PostgreSQL
database (e.g. `postgresql+psycopg://postgres@/postgres?host=/path/to/socket`).
Every test SKIPS (never silently passes as if it were satisfied) if that
variable is unset — per the release instruction, the SQLite result is not an
acceptable substitute for what this file checks.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

PG_URL = os.environ.get("STOPLOSS_PG_TEST_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason=(
        "STOPLOSS_PG_TEST_URL not set. This suite must run against a real "
        "PostgreSQL server — SKIPPED here means NOT VALIDATED, not PASSED. "
        "Do not report PostgreSQL: PASS on the strength of this file skipping."
    ),
)


# ══════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def pg_engine():
    """Runs the REAL alembic migration path against the REAL server, then
    hands back an engine bound to the now-migrated schema."""
    env = {**os.environ, "STOPLOSS_DATABASE_URL": PG_URL}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND), env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"alembic upgrade head FAILED against real PostgreSQL:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    eng = create_engine(PG_URL, pool_pre_ping=True, pool_size=20, max_overflow=20)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(pg_engine):
    SF = sessionmaker(bind=pg_engine)
    s = SF()
    yield s
    s.rollback()
    s.close()
    with pg_engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE users, licences, devices, sessions, mfa_credentials, "
            "recovery_codes, consent_records, audit_events, admin_users CASCADE"
        ))


def _sessionmaker(pg_engine):
    return sessionmaker(bind=pg_engine)


def _make_customer(SF, n_devices=2):
    from app import security
    from app.models import AccountStatus, Device, DeviceStatus, Licence, LicenceStatus, User, utcnow

    s = SF()
    u = User(email=f"{uuid.uuid4().hex[:10]}@pgtest.example",
             status=AccountStatus.ACTIVE,
             password_hash=security.hash_password("CorrectHorse1!"))
    s.add(u)
    s.flush()
    s.add(Licence(user_id=u.id, status=LicenceStatus.ACTIVE,
                  activated_at=utcnow(), expires_at=utcnow() + timedelta(days=365)))
    dev_ids = []
    for _ in range(n_devices):
        d = Device(user_id=u.id, public_key=f"pk-{uuid.uuid4().hex[:12]}",
                   status=DeviceStatus.ACTIVE)
        s.add(d)
        s.flush()
        dev_ids.append(d.id)
    s.commit()
    uid = u.id
    s.close()
    return uid, dev_ids


# ══════════════════════════════════════════════════════════════════════════
# Schema / FK / partial-unique-index validation on the REAL migrated schema
# ══════════════════════════════════════════════════════════════════════════
def test_all_tables_present_after_real_migration(pg_engine):
    with pg_engine.connect() as conn:
        tables = set(conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"
        )).scalars().all())
    expected = {"users", "licences", "devices", "sessions", "mfa_credentials",
                "recovery_codes", "consent_records", "audit_events",
                "admin_users", "alembic_version"}
    assert expected <= tables, f"missing tables after migration: {expected - tables}"


def test_alembic_at_head_revision(pg_engine):
    with pg_engine.connect() as conn:
        rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert rev == "7aa03f28bee7", f"expected head revision, got {rev}"


def test_partial_unique_index_survived_migration_as_partial(pg_engine):
    """The whole guarantee rests on this index being PARTIAL (WHERE status=
    'ACTIVE'), not a plain unique index on user_id. Confirm via direct
    catalog introspection — do not take the model definition's word for it."""
    with pg_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'uq_sessions_one_active_per_user'"
        )).fetchone()
    assert row is not None, "uq_sessions_one_active_per_user missing on real Postgres"
    indexdef = row[0].upper()
    assert "UNIQUE" in indexdef
    assert "WHERE" in indexdef, (
        f"index exists but lost its WHERE clause during migration — it would "
        f"now block a user from EVER having a second session at all, "
        f"active or not: {row[0]}"
    )


def test_foreign_keys_enforced_on_real_postgres(db):
    from app.models import Device
    db.add(Device(user_id=uuid.uuid4(), public_key="pk-orphan"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_cascade_delete_on_real_postgres(db):
    from app.models import AppSession, Device, Licence, SessionStatus, utcnow
    from app import security
    from app.models import AccountStatus, User

    u = User(email=f"{uuid.uuid4().hex[:8]}@pgtest.example", status=AccountStatus.ACTIVE,
             password_hash=security.hash_password("x"))
    db.add(u)
    db.flush()
    d = Device(user_id=u.id, public_key="pk")
    db.add(d)
    db.flush()  # populate d.id (client-side default, not assigned until flush)
    db.add(Licence(user_id=u.id))
    db.add(AppSession(user_id=u.id, device_id=d.id, status=SessionStatus.ACTIVE))
    db.commit()

    db.delete(u)
    db.commit()

    assert db.query(Device).count() == 0
    assert db.query(AppSession).count() == 0
    assert db.query(Licence).count() == 0


# ══════════════════════════════════════════════════════════════════════════
# THE headline requirement: single-session invariant under REAL concurrency
# ══════════════════════════════════════════════════════════════════════════
def _race_once(pg_engine, n_threads: int) -> tuple[int, list[str]]:
    """One round: n_threads devices race to open a session for one customer.
    Returns (count of ACTIVE sessions afterward, per-thread outcomes)."""
    from app import services
    from app.models import AppSession, SessionStatus, User, Device

    SF = _sessionmaker(pg_engine)
    uid, dev_ids = _make_customer(SF, n_devices=n_threads)

    results: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(n_threads, timeout=15)

    def attempt(dev_id):
        s = SF()
        try:
            barrier.wait()
            usr = s.get(User, uid)
            dev = s.get(Device, dev_id)
            services.start_session(s, usr, dev)
            with lock:
                results.append("won")
        except Exception as exc:
            with lock:
                results.append(type(exc).__name__)
        finally:
            s.close()

    threads = [threading.Thread(target=attempt, args=(d,)) for d in dev_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    check = SF()
    n_active = check.query(AppSession).filter_by(
        user_id=uid, status=SessionStatus.ACTIVE).count()
    check.close()
    return n_active, results


def test_simultaneous_login_race_yields_exactly_one_session_on_real_postgres(pg_engine):
    """The exact scenario named in the release instructions: two threads,
    real PostgreSQL, real SELECT ... FOR UPDATE."""
    n_active, results = _race_once(pg_engine, n_threads=2)
    assert n_active == 1, f"expected exactly 1 active session on real Postgres, got {n_active} ({results})"
    assert results.count("won") == 1, f"more than one thread believed it won: {results}"


@pytest.mark.parametrize("round_num", range(15))
def test_stress_single_session_invariant_repeated_rounds(pg_engine, round_num):
    """'Stress the single-session invariant repeatedly under concurrent login
    attempts' — 15 independent rounds, 4 devices racing simultaneously each
    round, fresh customer per round so rounds cannot interfere with each
    other. Required invariant: COUNT(ACTIVE) <= 1 for every round, no
    exceptions, no flakiness."""
    n_active, results = _race_once(pg_engine, n_threads=4)
    assert n_active == 1, (
        f"round {round_num}: invariant violated — {n_active} active sessions "
        f"for one user under real Postgres concurrency ({results})"
    )
    assert results.count("won") == 1, f"round {round_num}: {results}"


def test_higher_contention_eight_way_race(pg_engine):
    """Push contention further than the documented scenario to see if the
    invariant is robust or merely lucky at low thread counts."""
    n_active, results = _race_once(pg_engine, n_threads=8)
    assert n_active == 1, f"8-way race: got {n_active} active sessions ({results})"
    assert results.count("won") == 1, f"8-way race: {results}"


# ══════════════════════════════════════════════════════════════════════════
# Representative service-layer behavior against the real backend (not
# exhaustive re-run of all 44 SQLite tests — those already prove the logic;
# this proves the same logic still behaves identically on Postgres, focusing
# on the paths most likely to be backend-sensitive: locking, revocation, MFA
# takeover, licence-state transitions).
# ══════════════════════════════════════════════════════════════════════════
def test_takeover_revokes_previous_session_on_real_postgres(db):
    import pyotp
    from app import services
    from app.models import AppSession, Device, DeviceStatus, SessionStatus, User

    uid, _ = _make_customer(_sessionmaker(db.get_bind()), n_devices=0)
    u = db.get(User, uid)
    laptop = Device(user_id=u.id, public_key=f"pk-{uuid.uuid4().hex[:8]}", status=DeviceStatus.ACTIVE)
    desktop = Device(user_id=u.id, public_key=f"pk-{uuid.uuid4().hex[:8]}", status=DeviceStatus.ACTIVE)
    db.add_all([laptop, desktop])
    db.commit()

    secret, _ = services.begin_mfa_enrolment(db, u)
    code = pyotp.TOTP(secret).now()
    services.confirm_mfa_enrolment(db, u, code)
    u.mfa.last_used_step = None
    db.commit()

    s1, _ = services.start_session(db, u, laptop)
    s2, _ = services.start_session(db, u, desktop, takeover=True,
                                    totp_code=pyotp.TOTP(secret).now())

    db.refresh(s1)
    assert s1.status is SessionStatus.REVOKED
    assert s2.status is SessionStatus.ACTIVE
    assert db.query(AppSession).filter_by(user_id=u.id, status=SessionStatus.ACTIVE).count() == 1


def test_licence_revocation_blocks_heartbeat_on_real_postgres(db):
    from app.models import Device, DeviceStatus, Licence, LicenceStatus, User
    from app import services

    uid, _ = _make_customer(_sessionmaker(db.get_bind()), n_devices=0)
    u = db.get(User, uid)
    d = Device(user_id=u.id, public_key=f"pk-{uuid.uuid4().hex[:8]}", status=DeviceStatus.ACTIVE)
    db.add(d)
    db.commit()

    _, tok = services.start_session(db, u, d)
    db.query(Licence).filter_by(user_id=u.id).one().status = LicenceStatus.REVOKED
    db.commit()

    with pytest.raises(services.LicenceProblem):
        services.heartbeat(db, tok)


def test_session_token_stored_only_as_hash_on_real_postgres(db):
    from app.models import Device, DeviceStatus, User
    from app import services, security

    uid, _ = _make_customer(_sessionmaker(db.get_bind()), n_devices=0)
    u = db.get(User, uid)
    d = Device(user_id=u.id, public_key=f"pk-{uuid.uuid4().hex[:8]}", status=DeviceStatus.ACTIVE)
    db.add(d)
    db.commit()

    sess, tok = services.start_session(db, u, d)
    assert sess.token_hash != tok
    assert sess.token_hash == security.hash_session_token(tok)
