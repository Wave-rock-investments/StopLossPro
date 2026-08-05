"""Shared test setup.

Generates a throwaway Ed25519 signing pair for the whole test session. Real
keys are never committed, and tests must never depend on a developer having
configured a specific key locally.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(scope="session", autouse=True)
def _test_signing_keys():
    from app import security
    from app.keygen import generate, generate_totp_key

    priv, pub = generate()
    security.settings.SIGNING_PRIVATE_KEY_B64 = priv
    security.settings.SIGNING_PUBLIC_KEY_B64 = pub
    # Independent of the signing key on purpose — see app/config.py.
    security.settings.TOTP_ENCRYPTION_KEY_B64 = generate_totp_key()
    yield
