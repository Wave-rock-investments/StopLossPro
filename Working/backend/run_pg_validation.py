"""One-shot orchestrator for Step 3 (production PostgreSQL validation).

Starts a real, disposable PostgreSQL server (via the `pgserver` package,
which bundles an actual `postgres` binary — not an emulation), points
STOPLOSS_PG_TEST_URL at it, runs tests/test_postgres_production.py in-process
via pytest, then shuts the server down cleanly. Kept as a standalone script
(not committed as part of the permanent test suite) because it manages
server lifecycle, which the actual production deploy will do via a real
managed PostgreSQL host instead.
"""
import os
import shutil
import sys
from pathlib import Path

import pgserver
import pytest

PGDATA = Path(os.path.expanduser("~")) / "pgdata"
BACKEND = Path(__file__).resolve().parent


def main() -> int:
    if PGDATA.exists():
        shutil.rmtree(PGDATA, ignore_errors=True)
    PGDATA.mkdir(parents=True, exist_ok=True)

    print(f"Starting real PostgreSQL at {PGDATA} ...")
    server = pgserver.get_server(str(PGDATA))
    raw_uri = server.get_uri()
    sa_uri = raw_uri.replace("postgresql://", "postgresql+psycopg://", 1)
    print(f"PostgreSQL up. URI = {sa_uri}")

    os.environ["STOPLOSS_PG_TEST_URL"] = sa_uri
    os.environ["STOPLOSS_DATABASE_URL"] = sa_uri

    try:
        rc = pytest.main([
            str(BACKEND / "tests" / "test_postgres_production.py"),
            "-v", "--tb=short", "-p", "no:cacheprovider",
        ])
    finally:
        print("Shutting down PostgreSQL ...")
        server.cleanup()

    return int(rc)


if __name__ == "__main__":
    sys.exit(main())
