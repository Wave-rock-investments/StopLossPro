"""PHASE 14 — security, concurrency and adversarial tests.

These map directly onto the agreed RELEASE BLOCKERS. Each test is named for
the property it proves so a failure says what regressed, not just where.
"""
import base64
import sys
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyotp  # noqa: E402

from app import security, services  # noqa: E402
from app.models import (  # noqa: E402
    AccountStatus, AppSession, AuditEvent, Base, Device, DeviceStatus,
    Licence, LicenceStatus, SessionStatus, User, utcnow,
)
from app import admin as admin_mod  # noqa: E402,F401  (registers admin_users)


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})

    @event.listens_for(eng, "connect")
    def _fk(c, _):
        c.cursor().execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    try:
        yield s
    finally:
        s.close()


def make_customer(db, email=None, days=365):
    u = User(email=email or f"{uuid.uuid4().hex[:8]}@ex.com",
             status=AccountStatus.ACTIVE,
             password_hash=security.hash_password("CorrectHorse1!"))
    db.add(u)
    db.flush()
    db.add(Licence(user_id=u.id, status=LicenceStatus.ACTIVE,
                   activated_at=utcnow(), expires_at=utcnow() + timedelta(days=days)))
    db.commit()
    return u


def make_device(db, u, key="pk", name="Laptop"):
    d = Device(user_id=u.id, public_key=key, device_name=name, status=DeviceStatus.ACTIVE)
    db.add(d)
    db.commit()
    return d


def enable_mfa(db, u):
    """Enable MFA, then clear the consumed-step marker.

    Confirming enrolment legitimately burns the current 30-second TOTP step
    (replay protection — a code accepted once must never be accepted again).
    In production, enrolment and a later device switch are separated by
    minutes or days, so a fresh step is always available. In a test they
    happen microseconds apart, which would otherwise make every subsequent
    code look like a replay. Clearing the marker here simulates that elapsed
    time and keeps the replay behaviour itself under test in
    `test_totp_code_cannot_be_replayed`.
    """
    secret, _ = services.begin_mfa_enrolment(db, u)
    code = pyotp.TOTP(secret).now()
    codes = services.confirm_mfa_enrolment(db, u, code)
    u.mfa.last_used_step = None
    db.commit()
    return secret, codes


# ══════════════════════════════════════════════════════════════════════════
# BLOCKER: one active session, incl. simultaneous login race
# ══════════════════════════════════════════════════════════════════════════
def test_second_device_refused_without_takeover(db):
    u = make_customer(db)
    d1, d2 = make_device(db, u, "k1", "Laptop"), make_device(db, u, "k2", "Desktop")
    services.start_session(db, u, d1)
    with pytest.raises(services.SessionActiveElsewhere):
        services.start_session(db, u, d2)


def test_simultaneous_login_race_yields_exactly_one_session():
    """Two threads racing to create a session for one user.

    On SQLite the row lock is a no-op, so this exercises the WORST case:
    only the partial unique index protects us. Exactly one must win.
    """
    eng = create_engine("sqlite:///file:race?mode=memory&cache=shared&uri=true",
                        connect_args={"check_same_thread": False})

    @event.listens_for(eng, "connect")
    def _fk(c, _):
        c.cursor().execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(eng)
    SF = sessionmaker(bind=eng)

    setup = SF()
    u = make_customer(setup)
    d1, d2 = make_device(setup, u, "k1"), make_device(setup, u, "k2")
    uid, d1id, d2id = u.id, d1.id, d2.id
    setup.close()

    results, barrier = [], threading.Barrier(2)

    def attempt(dev_id):
        s = SF()
        try:
            barrier.wait(timeout=5)
            usr = s.get(User, uid)
            dev = s.get(Device, dev_id)
            services.start_session(s, usr, dev)
            results.append("won")
        except Exception as exc:
            results.append(type(exc).__name__)
        finally:
            s.close()

    ts = [threading.Thread(target=attempt, args=(d,)) for d in (d1id, d2id)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)

    check = SF()
    active = check.execute(select(AppSession).where(
        AppSession.user_id == uid, AppSession.status == SessionStatus.ACTIVE)).scalars().all()
    n = len(active)
    check.close()

    assert n == 1, f"expected exactly 1 active session, got {n} (results={results})"
    assert results.count("won") <= 1, f"more than one thread believed it won: {results}"


# ══════════════════════════════════════════════════════════════════════════
# BLOCKER: TOTP device switching + old session revoked
# ══════════════════════════════════════════════════════════════════════════
def test_takeover_requires_mfa_and_revokes_previous(db):
    u = make_customer(db)
    laptop, desktop = make_device(db, u, "k1", "Laptop"), make_device(db, u, "k2", "Desktop")
    secret, _ = enable_mfa(db, u)

    s1, _ = services.start_session(db, u, laptop)

    with pytest.raises(services.MfaRequired):
        services.start_session(db, u, desktop, takeover=True)

    with pytest.raises(services.MfaInvalid):
        services.start_session(db, u, desktop, takeover=True, totp_code="000000")

    s2, _ = services.start_session(db, u, desktop, takeover=True,
                                   totp_code=pyotp.TOTP(secret).now())

    db.refresh(s1)
    assert s1.status is SessionStatus.REVOKED
    assert s1.end_reason == "TAKEOVER_BY_NEW_DEVICE"
    assert s2.status is SessionStatus.ACTIVE
    assert db.query(AppSession).filter_by(user_id=u.id, status=SessionStatus.ACTIVE).count() == 1


def test_revoked_session_token_stops_working(db):
    """The old computer must lose authorization at its next heartbeat."""
    u = make_customer(db)
    laptop, desktop = make_device(db, u, "k1"), make_device(db, u, "k2")
    secret, _ = enable_mfa(db, u)

    _, tok1 = services.start_session(db, u, laptop)
    services.heartbeat(db, tok1)                      # works while active

    services.start_session(db, u, desktop, takeover=True, totp_code=pyotp.TOTP(secret).now())

    with pytest.raises(services.SessionInvalid):
        services.heartbeat(db, tok1)


def test_totp_code_cannot_be_replayed(db):
    u = make_customer(db)
    d1, d2, d3 = (make_device(db, u, f"k{i}") for i in (1, 2, 3))
    secret, _ = enable_mfa(db, u)
    services.start_session(db, u, d1)

    code = pyotp.TOTP(secret).now()
    services.start_session(db, u, d2, takeover=True, totp_code=code)

    with pytest.raises(services.MfaInvalid):
        services.start_session(db, u, d3, takeover=True, totp_code=code)


# ══════════════════════════════════════════════════════════════════════════
# BLOCKER: revocation, licence states, device revocation
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("state,exc", [
    (LicenceStatus.REVOKED, services.LicenceProblem),
    (LicenceStatus.SUSPENDED, services.LicenceProblem),
    (LicenceStatus.PENDING, services.LicenceProblem),
])
def test_bad_licence_states_block_heartbeat(db, state, exc):
    u = make_customer(db)
    d = make_device(db, u)
    _, tok = services.start_session(db, u, d)
    db.query(Licence).filter_by(user_id=u.id).one().status = state
    db.commit()
    with pytest.raises(exc):
        services.heartbeat(db, tok)


def test_expired_licence_blocks_and_is_marked_expired(db):
    u = make_customer(db)
    d = make_device(db, u)
    _, tok = services.start_session(db, u, d)
    lic = db.query(Licence).filter_by(user_id=u.id).one()
    lic.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    with pytest.raises(services.LicenceProblem):
        services.heartbeat(db, tok)
    assert db.query(Licence).filter_by(user_id=u.id).one().status is LicenceStatus.EXPIRED


def test_suspended_account_blocks_and_kills_session(db):
    u = make_customer(db)
    d = make_device(db, u)
    sess, tok = services.start_session(db, u, d)
    u.status = AccountStatus.SUSPENDED
    db.commit()
    with pytest.raises(services.AccountNotActive):
        services.heartbeat(db, tok)
    db.refresh(sess)
    assert sess.status is SessionStatus.REVOKED


def test_revoked_device_blocks_login_and_heartbeat(db):
    u = make_customer(db)
    d = make_device(db, u)
    _, tok = services.start_session(db, u, d)
    d.status = DeviceStatus.REVOKED
    db.commit()
    with pytest.raises(services.DeviceRevoked):
        services.heartbeat(db, tok)
    with pytest.raises(services.DeviceRevoked):
        services.enrol_device(db, u, public_key="pk", device_name=None,
                              os_name=None, app_version=None)


# ══════════════════════════════════════════════════════════════════════════
# BLOCKER: copied local state cannot self-authorize; grants unforgeable
# ══════════════════════════════════════════════════════════════════════════
def test_grant_signature_is_required_and_tamper_evident(monkeypatch):
    priv, pub = _fresh_keys(monkeypatch)
    tok = security.issue_grant(user_id="u", licence_id="l", session_id="s",
                               device_id="d", entitlements=["risk_engine"], counter=1)
    claims = security.verify_grant(tok)
    assert claims.user_id == "u" and "risk_engine" in claims.entitlements

    body, sig = tok.split(".")
    with pytest.raises(ValueError):
        security.verify_grant(f"{body}x.{sig}")      # tampered payload
    with pytest.raises(ValueError):
        security.verify_grant(f"{body}.{sig[:-4]}AAAA")  # tampered signature
    with pytest.raises(ValueError):
        security.verify_grant("not-a-grant")


def test_grant_from_a_different_key_is_rejected(monkeypatch):
    """A licence forged with an attacker's own keypair must not verify."""
    from app.keygen import generate
    _fresh_keys(monkeypatch)
    attacker_priv, attacker_pub = generate()

    monkeypatch.setattr(security.settings, "SIGNING_PRIVATE_KEY_B64", attacker_priv)
    forged = security.issue_grant(user_id="u", licence_id="l", session_id="s",
                                  device_id="d", entitlements=["risk_engine"], counter=1)

    # Client pins the REAL public key, so the forgery fails.
    with pytest.raises(ValueError):
        security.verify_grant(forged)


def test_expired_grant_rejected(monkeypatch):
    _fresh_keys(monkeypatch)
    tok = security.issue_grant(user_id="u", licence_id="l", session_id="s",
                               device_id="d", entitlements=[], counter=1, ttl_seconds=1)
    time.sleep(1.2)
    with pytest.raises(ValueError, match="expired"):
        security.verify_grant(tok)


def _fresh_keys(monkeypatch):
    from app.keygen import generate
    priv, pub = generate()
    monkeypatch.setattr(security.settings, "SIGNING_PRIVATE_KEY_B64", priv)
    monkeypatch.setattr(security.settings, "SIGNING_PUBLIC_KEY_B64", pub)
    return priv, pub


def test_session_token_stored_only_as_hash(db):
    u = make_customer(db)
    d = make_device(db, u)
    sess, tok = services.start_session(db, u, d)
    assert sess.token_hash != tok
    assert sess.token_hash == security.hash_session_token(tok)
    assert len(sess.token_hash) == 64


def test_unknown_or_stale_token_rejected(db):
    u = make_customer(db)
    d = make_device(db, u)
    sess, tok = services.start_session(db, u, d)
    with pytest.raises(services.SessionInvalid):
        services.resolve_session(db, "fabricated-token")
    services.end_session(db, sess)
    with pytest.raises(services.SessionInvalid):
        services.resolve_session(db, tok)


# ══════════════════════════════════════════════════════════════════════════
# BLOCKER: authentication hardening
# ══════════════════════════════════════════════════════════════════════════
def test_password_never_stored_plaintext(db):
    u = make_customer(db)
    assert "CorrectHorse1!" not in (u.password_hash or "")
    assert u.password_hash.startswith("$argon2id$")


def test_wrong_password_rejected_and_lockout_engages(db):
    u = make_customer(db, email="lock@ex.com")
    for _ in range(services.settings.MAX_FAILED_LOGINS):
        with pytest.raises(services.InvalidCredentials):
            services.authenticate(db, "lock@ex.com", "wrong")
    with pytest.raises(services.AccountLocked):
        services.authenticate(db, "lock@ex.com", "CorrectHorse1!")   # correct pw, still locked


def test_unknown_account_and_wrong_password_are_indistinguishable(db):
    make_customer(db, email="real@ex.com")
    with pytest.raises(services.InvalidCredentials) as a:
        services.authenticate(db, "real@ex.com", "wrong")
    with pytest.raises(services.InvalidCredentials) as b:
        services.authenticate(db, "ghost@ex.com", "wrong")
    assert str(a.value) == str(b.value)


def test_non_active_account_cannot_authenticate(db):
    u = make_customer(db, email="p@ex.com")
    u.status = AccountStatus.PENDING
    db.commit()
    with pytest.raises(services.AccountNotActive):
        services.authenticate(db, "p@ex.com", "CorrectHorse1!")


# ══════════════════════════════════════════════════════════════════════════
# MFA recovery
# ══════════════════════════════════════════════════════════════════════════
def test_recovery_codes_hashed_single_use(db):
    u = make_customer(db)
    _, codes = enable_mfa(db, u)
    assert len(codes) == services.settings.RECOVERY_CODE_COUNT

    stored = [rc.code_hash for rc in u.mfa.recovery_codes]
    assert all(c not in stored for c in codes), "recovery codes stored in plaintext"

    assert services.consume_recovery_code(db, u, codes[0]) is True
    assert services.consume_recovery_code(db, u, codes[0]) is False   # single use
    assert services.consume_recovery_code(db, u, "AAAAA-BBBBB") is False


def test_admin_mfa_reset_also_ends_session(db):
    u = make_customer(db)
    d = make_device(db, u)
    enable_mfa(db, u)
    sess, _ = services.start_session(db, u, d)

    services.reset_mfa(db, u, actor="admin:owner")

    db.refresh(sess)
    assert sess.status is SessionStatus.REVOKED
    assert sess.end_reason == "MFA_RESET"
    assert u.mfa is None


# ══════════════════════════════════════════════════════════════════════════
# Stale-session reaping (crashed client must not lock the customer out)
# ══════════════════════════════════════════════════════════════════════════
def test_stale_session_is_reclaimed(db):
    u = make_customer(db)
    d1, d2 = make_device(db, u, "k1"), make_device(db, u, "k2")
    s1, _ = services.start_session(db, u, d1)

    s1.last_heartbeat_at = utcnow() - timedelta(
        seconds=services.settings.SESSION_IDLE_TIMEOUT_SECONDS + 60)
    db.commit()

    s2, _ = services.start_session(db, u, d2)     # no takeover, no MFA needed
    db.refresh(s1)
    assert s1.status is SessionStatus.EXPIRED and s1.end_reason == "IDLE_TIMEOUT"
    assert s2.status is SessionStatus.ACTIVE


# ══════════════════════════════════════════════════════════════════════════
# Privacy + audit hygiene
# ══════════════════════════════════════════════════════════════════════════
def test_audit_log_contains_no_secrets(db):
    u = make_customer(db)
    d = make_device(db, u)
    secret, codes = enable_mfa(db, u)
    _, tok = services.start_session(db, u, d)
    services.heartbeat(db, tok)

    blob = " ".join(f"{e.event_type} {e.actor or ''} {e.detail or ''}"
                    for e in db.query(AuditEvent).all())
    assert secret not in blob
    assert tok not in blob
    assert "CorrectHorse1!" not in blob
    for c in codes:
        assert c not in blob


def test_no_location_or_financial_fields_in_schema():
    """Requirement 14: no covert GPS, no arbitrary collection.

    The replacement schema must not even have somewhere to put coordinates,
    MT5 balances or open positions.
    """
    banned = {"latitude", "longitude", "gps", "coords", "location",
              "balance", "equity", "mt5_login", "positions", "profit",
              "mac_address", "hostname"}
    for table in Base.metadata.tables.values():
        for col in table.columns:
            assert col.name.lower() not in banned, f"{table.name}.{col.name} collects banned data"


def test_consent_gate_blocks_until_all_accepted(db):
    from app.models import ConsentDocument
    u = make_customer(db)
    assert len(services.outstanding_consents(db, u)) == 3

    for doc, ver in services.REQUIRED_CONSENTS.items():
        services.record_consent(db, u, doc, ver, accepted=True, app_version="1.0.0")
    assert services.outstanding_consents(db, u) == []

    # A new document version re-triggers the requirement.
    services.REQUIRED_CONSENTS[ConsentDocument.RISK_DISCLOSURE] = "2.0"
    try:
        assert len(services.outstanding_consents(db, u)) == 1
    finally:
        services.REQUIRED_CONSENTS[ConsentDocument.RISK_DISCLOSURE] = "1.0"


# ══════════════════════════════════════════════════════════════════════════
# Client must contain no server secret
# ══════════════════════════════════════════════════════════════════════════
def test_no_private_key_material_in_client_tree():
    client = Path(__file__).resolve().parents[2] / "StopLossPro_OfflineSale"
    if not client.exists():
        pytest.skip("client tree not present")

    bad = []
    for p in list(client.rglob("*.py")) + list(client.rglob("*.kv")) + list(client.rglob("*.bat")):
        if any(x in p.parts for x in ("build", "dist", "__pycache__")):
            continue
        # verify_release.py necessarily contains every banned pattern as a
        # SEARCH STRING; scanning it would flag the scanner itself forever.
        if p.name == "verify_release.py":
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for marker in ("BEGIN PRIVATE KEY", "BEGIN OPENSSH PRIVATE KEY",
                       "SIGNING_PRIVATE_KEY", "ghp_", "github_pat_"):
            if marker in txt:
                bad.append(f"{p.name}: {marker}")
    assert not bad, f"client tree contains server secrets: {bad}"
