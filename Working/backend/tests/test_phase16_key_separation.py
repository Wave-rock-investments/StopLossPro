"""STEP 1.1 — MFA encryption / signing key domain separation.

Proves the property the release plan requires before production provisioning:
compromising or rotating the Ed25519 SIGNING key must not force TOTP secrets
to be re-encrypted, and vice versa. Also proves the migration path for TOTP
secrets encrypted under the old (pre-split) scheme still decrypts.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import security  # noqa: E402
from app.config import Settings  # noqa: E402
from app.keygen import generate, generate_totp_key  # noqa: E402


def test_totp_secret_survives_signing_key_rotation(monkeypatch):
    """The headline requirement: rotate the signing key, TOTP still decrypts."""
    secret = security.new_totp_secret()
    blob = security.encrypt_totp_secret(secret)

    # Simulate a full signing-key rotation (e.g. after a suspected leak).
    new_priv, new_pub = generate()
    monkeypatch.setattr(security.settings, "SIGNING_PRIVATE_KEY_B64", new_priv)
    monkeypatch.setattr(security.settings, "SIGNING_PUBLIC_KEY_B64", new_pub)

    assert security.decrypt_totp_secret(blob) == secret


def test_signing_still_works_after_totp_key_rotation(monkeypatch):
    """The reverse: rotate the TOTP key, grant signing is untouched."""
    tok_before = security.issue_grant(
        user_id="u", licence_id="l", session_id="s", device_id="d",
        entitlements=["risk_engine"], counter=1,
    )
    monkeypatch.setattr(security.settings, "TOTP_ENCRYPTION_KEY_B64", generate_totp_key())

    claims = security.verify_grant(tok_before)
    assert claims.user_id == "u"

    tok_after = security.issue_grant(
        user_id="u", licence_id="l", session_id="s", device_id="d",
        entitlements=["risk_engine"], counter=2,
    )
    assert security.verify_grant(tok_after).user_id == "u"


def test_new_totp_blobs_are_versioned_and_independent_of_signing_key(monkeypatch):
    secret = security.new_totp_secret()
    blob = security.encrypt_totp_secret(secret)
    assert blob.startswith("v2:"), "current scheme must be distinguishable from legacy blobs"

    # Decrypting must not touch the signing key at all — corrupt it and prove
    # decryption still succeeds via the independent TOTP key.
    monkeypatch.setattr(security.settings, "SIGNING_PRIVATE_KEY_B64", "not-a-real-key")
    assert security.decrypt_totp_secret(blob) == secret


def test_legacy_signing_derived_blob_still_decrypts_for_migration():
    """Blobs encrypted before this migration (no version prefix) must not be
    orphaned — customers with MFA already enabled must not be locked out the
    moment this ships."""
    secret = security.new_totp_secret()
    legacy_blob = security._legacy_signing_derived_fernet().encrypt(secret.encode()).decode()
    assert not legacy_blob.startswith("v2:")

    assert security.decrypt_totp_secret(legacy_blob) == secret


def test_legacy_blob_breaks_cleanly_if_signing_key_rotates_without_migration(monkeypatch):
    """Documents the one real limitation of the legacy fallback: it is only a
    bridge. A legacy blob that was never migrated to the v2 scheme is, by
    definition, still coupled to the signing key — rotating the signing key
    before migrating it will break it. This must fail loudly (ValueError),
    never silently return garbage.
    """
    secret = security.new_totp_secret()
    legacy_blob = security._legacy_signing_derived_fernet().encrypt(secret.encode()).decode()

    new_priv, _ = generate()
    monkeypatch.setattr(security.settings, "SIGNING_PRIVATE_KEY_B64", new_priv)

    with pytest.raises(ValueError, match="legacy signing-derived"):
        security.decrypt_totp_secret(legacy_blob)


def test_totp_key_rotation_without_reencryption_fails_cleanly_not_silently(monkeypatch):
    """Rotating TOTP_ENCRYPTION_KEY_B64 is a symmetric-key rotation — there is
    no public half. Old blobs become undecryptable until a re-encryption pass
    runs (documented in app/keygen.py). This must raise, not return corrupt
    plaintext."""
    secret = security.new_totp_secret()
    blob = security.encrypt_totp_secret(secret)

    monkeypatch.setattr(security.settings, "TOTP_ENCRYPTION_KEY_B64", generate_totp_key())

    with pytest.raises(ValueError, match="TOTP_ENCRYPTION_KEY_B64"):
        security.decrypt_totp_secret(blob)


def test_production_guard_requires_totp_key_independent_of_signing_key():
    problems = Settings(
        ENV="production", DEBUG=False,
        DATABASE_URL="postgresql+psycopg://u:p@h:5432/db",
        SIGNING_PRIVATE_KEY_B64="priv", SIGNING_PUBLIC_KEY_B64="pub",
        TOTP_ENCRYPTION_KEY_B64="",
    ).assert_production_ready()
    assert any("TOTP_ENCRYPTION_KEY_B64 is not set" in p for p in problems)

    problems2 = Settings(
        ENV="production", DEBUG=False,
        DATABASE_URL="postgresql+psycopg://u:p@h:5432/db",
        SIGNING_PRIVATE_KEY_B64="same-value", SIGNING_PUBLIC_KEY_B64="pub",
        TOTP_ENCRYPTION_KEY_B64="same-value",
    ).assert_production_ready()
    assert any("identical to SIGNING_PRIVATE_KEY_B64" in p for p in problems2)

    clean = Settings(
        ENV="production", DEBUG=False,
        DATABASE_URL="postgresql+psycopg://u:p@h:5432/db",
        SIGNING_PRIVATE_KEY_B64="priv", SIGNING_PUBLIC_KEY_B64="pub",
        TOTP_ENCRYPTION_KEY_B64="totally-different-secret",
    ).assert_production_ready()
    assert clean == []
