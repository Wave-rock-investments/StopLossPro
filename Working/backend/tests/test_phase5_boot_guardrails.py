"""STEP 5 — production boot guardrail verification.

Proves the FastAPI `@app.on_event("startup")` guard in app/main.py actually
fires and actually blocks the app from serving under each unsafe production
condition — not just that `Settings.assert_production_ready()` returns a
non-empty list in isolation (already covered by test_phase1_foundation.py),
but that the ASGI app itself refuses to come up.

Runs each scenario in a FRESH subprocess. Necessary because `get_settings()`
is `@lru_cache`d and captured at module-import time in app/main.py — testing
multiple configs in one process would just be testing the first one repeatedly.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent

_PROBE = textwrap.dedent("""
    import sys
    from fastapi.testclient import TestClient
    try:
        from app.main import app
        with TestClient(app):
            pass
        print("STARTED_OK")
    except RuntimeError as exc:
        print("REFUSED:" + str(exc).replace("\\n", " | "))
    except Exception as exc:
        print("OTHER_EXCEPTION:" + type(exc).__name__ + ":" + str(exc))
""")


def _run(env_overrides: dict) -> str:
    env = {
        "PATH": "/usr/bin:/bin",
        "STOPLOSS_ENV": "production",
        "STOPLOSS_DEBUG": "false",
        "STOPLOSS_DATABASE_URL": "postgresql+psycopg://u:p@nonexistent-host:5432/db",
        "STOPLOSS_SIGNING_PRIVATE_KEY_B64": "dummy-priv",
        "STOPLOSS_SIGNING_PUBLIC_KEY_B64": "dummy-pub",
        "STOPLOSS_TOTP_ENCRYPTION_KEY_B64": "dummy-totp-key-independent-value",
    }
    env.update(env_overrides)
    result = subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=str(BACKEND), env=env,
        capture_output=True, text=True, timeout=30,
    )
    return (result.stdout + result.stderr).strip()


def test_refuses_sqlite_in_production():
    out = _run({"STOPLOSS_DATABASE_URL": "sqlite:///./x.db"})
    assert "REFUSED:" in out and "SQLite" in out, out


def test_refuses_debug_true_in_production():
    out = _run({"STOPLOSS_DEBUG": "true"})
    assert "REFUSED:" in out and "DEBUG" in out, out


def test_refuses_missing_signing_private_key():
    out = _run({"STOPLOSS_SIGNING_PRIVATE_KEY_B64": ""})
    assert "REFUSED:" in out and "SIGNING_PRIVATE_KEY_B64" in out, out


def test_refuses_missing_signing_public_key():
    out = _run({"STOPLOSS_SIGNING_PUBLIC_KEY_B64": ""})
    assert "REFUSED:" in out and "SIGNING_PUBLIC_KEY_B64" in out, out


def test_refuses_missing_totp_encryption_key():
    out = _run({"STOPLOSS_TOTP_ENCRYPTION_KEY_B64": ""})
    assert "REFUSED:" in out and "TOTP_ENCRYPTION_KEY_B64" in out, out


def test_refuses_totp_key_coupled_to_signing_key():
    out = _run({
        "STOPLOSS_SIGNING_PRIVATE_KEY_B64": "same-secret",
        "STOPLOSS_TOTP_ENCRYPTION_KEY_B64": "same-secret",
    })
    assert "REFUSED:" in out and "identical" in out, out


def test_refuses_all_unsafe_conditions_at_once_and_lists_every_one():
    """Boot should report every problem, not just the first, so a single
    server restart cycle is enough to fix a misconfigured deploy."""
    out = _run({
        "STOPLOSS_DATABASE_URL": "sqlite:///./x.db",
        "STOPLOSS_DEBUG": "true",
        "STOPLOSS_SIGNING_PRIVATE_KEY_B64": "",
        "STOPLOSS_SIGNING_PUBLIC_KEY_B64": "",
        "STOPLOSS_TOTP_ENCRYPTION_KEY_B64": "",
    })
    assert "REFUSED:" in out
    for needle in ("SQLite", "DEBUG", "SIGNING_PRIVATE_KEY_B64",
                   "SIGNING_PUBLIC_KEY_B64", "TOTP_ENCRYPTION_KEY_B64"):
        assert needle in out, f"missing '{needle}' in combined-failure message: {out}"


def test_starts_cleanly_with_a_fully_valid_production_config():
    """The negative-space check: a genuinely valid config must NOT be refused.
    A guard that rejects everything is as useless as one that rejects nothing.
    """
    out = _run({})  # all defaults above are already a valid combination
    assert "STARTED_OK" in out, out


def test_development_env_is_never_subject_to_these_guards():
    """The guard is production-only by design (assert_production_ready
    returns [] immediately if not is_production) — confirm a wide-open dev
    config still boots, since local development must not require production
    secrets."""
    out = _run({
        "STOPLOSS_ENV": "development",
        "STOPLOSS_DATABASE_URL": "sqlite:///./x.db",
        "STOPLOSS_DEBUG": "true",
        "STOPLOSS_SIGNING_PRIVATE_KEY_B64": "",
        "STOPLOSS_SIGNING_PUBLIC_KEY_B64": "",
        "STOPLOSS_TOTP_ENCRYPTION_KEY_B64": "",
    })
    assert "STARTED_OK" in out, out
