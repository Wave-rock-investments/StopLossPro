"""Tests for the Phase 10 backup/restore/verify tooling (scripts/) — the
"backup scripts where testable" requirement from the launch-hardening
directive. Only the SQLite path and the scripts' own argument/guard-rail
validation are exercised here: the PostgreSQL path (`pg_dump`/`psql`
against a real server) is not testable in this environment without a live
Postgres instance, and is exercised manually per DEPLOYMENT.md §3 instead.

These tests run the actual shell scripts via subprocess — not a
reimplementation of their logic in Python — so a real bug in the bash
(a wrong flag, a broken quoting edge case) is caught the same way it would
break in production.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
BACKUP_SH = SCRIPTS_DIR / "backup_db.sh"
RESTORE_SH = SCRIPTS_DIR / "restore_db.sh"
VERIFY_SH = SCRIPTS_DIR / "verify_backup.sh"


def _run(cmd: list[str], **env_overrides) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(env_overrides)
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


@pytest.fixture()
def seeded_sqlite_db(tmp_path):
    """A real, freshly-migrated SQLite DB (via alembic, not raw DDL) with
    one seeded Call row, matching what a production-shaped source database
    actually looks like."""
    db_path = tmp_path / "source.db"
    result = _run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        STERLING_DATABASE_URL=f"sqlite:///{db_path}",
        STERLING_ADAPTER_API_KEYS="test-key",
        STERLING_ADMIN_SESSION_SECRET="test-secret",
    )
    assert result.returncode == 0, result.stderr

    from app.models import Call, CallStatus

    engine = create_engine(f"sqlite:///{db_path}")
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(
        Call(
            id=uuid.uuid4(), trade_id="T-BACKUP-TEST", source="stoplosspro",
            source_call_id="backup-test-1", instrument="XAUUSD", direction="BUY",
            stop_loss=1900, status=CallStatus.CLOSED, route_free=True, route_premium=True,
            result_r=3.0,
        )
    )
    db.commit()
    db.close()
    engine.dispose()
    return db_path


def test_backup_db_requires_database_url(tmp_path):
    result = _run(["bash", str(BACKUP_SH), str(tmp_path)], DATABASE_URL="")
    assert result.returncode != 0
    assert "DATABASE_URL must be set" in (result.stdout + result.stderr)


def test_backup_db_rejects_unrecognized_scheme(tmp_path):
    result = _run(["bash", str(BACKUP_SH), str(tmp_path)], DATABASE_URL="mysql://nope")
    assert result.returncode != 0
    assert "unrecognized DATABASE_URL scheme" in (result.stdout + result.stderr)


def test_backup_db_missing_sqlite_file_fails_loudly(tmp_path):
    missing = tmp_path / "does_not_exist.db"
    result = _run(["bash", str(BACKUP_SH), str(tmp_path)], DATABASE_URL=f"sqlite:///{missing}")
    assert result.returncode != 0
    assert "not found" in (result.stdout + result.stderr)


def test_backup_db_sqlite_produces_a_valid_snapshot(seeded_sqlite_db, tmp_path):
    out_dir = tmp_path / "backups"
    result = _run(
        ["bash", str(BACKUP_SH), str(out_dir)],
        DATABASE_URL=f"sqlite:///{seeded_sqlite_db}",
    )
    assert result.returncode == 0, result.stderr
    backups = list(out_dir.glob("sterling_backup_*.db"))
    assert len(backups) == 1

    # The snapshot is a real, independently-openable SQLite DB with the
    # seeded row — not just a byte-for-byte file copy coincidence.
    engine = create_engine(f"sqlite:///{backups[0]}")
    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM calls")).scalar()
        assert count == 1
    engine.dispose()


def test_restore_db_refuses_to_overwrite_an_existing_file(seeded_sqlite_db, tmp_path):
    # seeded_sqlite_db already exists on disk — restoring "into" it must
    # be refused, not silently clobbered.
    result = _run(
        ["bash", str(RESTORE_SH), str(seeded_sqlite_db), f"sqlite:///{seeded_sqlite_db}"]
    )
    assert result.returncode != 0
    assert "refusing to overwrite" in (result.stdout + result.stderr)


def test_restore_db_requires_both_arguments():
    result = _run(["bash", str(RESTORE_SH)])
    assert result.returncode != 0


def test_verify_backup_passes_for_a_healthy_sqlite_backup(seeded_sqlite_db, tmp_path):
    out_dir = tmp_path / "backups"
    backup_result = _run(
        ["bash", str(BACKUP_SH), str(out_dir)],
        DATABASE_URL=f"sqlite:///{seeded_sqlite_db}",
    )
    assert backup_result.returncode == 0, backup_result.stderr
    backup_file = next(out_dir.glob("sterling_backup_*.db"))

    result = _run(["bash", str(VERIFY_SH), str(backup_file)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK — backup restored successfully" in result.stdout
    assert "calls: 1 rows" in result.stdout


def test_verify_backup_detects_a_schema_missing_required_tables(tmp_path):
    """A 'backup' that is just an empty SQLite file (simulating a truncated
    or failed dump that nonetheless produced a file) must fail verification
    loudly, not report success."""
    empty_backup = tmp_path / "empty_backup.db"
    engine = create_engine(f"sqlite:///{empty_backup}")
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE unrelated (id INTEGER)"))
        conn.commit()
    engine.dispose()

    result = _run(["bash", str(VERIFY_SH), str(empty_backup)])
    assert result.returncode != 0
    assert "missing tables" in (result.stdout + result.stderr)


def test_verify_backup_missing_file_fails_loudly(tmp_path):
    result = _run(["bash", str(VERIFY_SH), str(tmp_path / "nope.db")])
    assert result.returncode != 0
    assert "not found" in (result.stdout + result.stderr)
