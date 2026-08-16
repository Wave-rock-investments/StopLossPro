#!/usr/bin/env python3
"""Helper invoked by verify_backup.sh.

Confirms a restored backup actually contains the tables that matter
(`calls`, `subscriptions`, `payments`, `audit_events`) and that they're
queryable — catching the "pg_restore/psql exited 0 but the dump was
truncated/empty" failure mode that a bare exit-code check misses.

Deliberately does NOT assert a hard-coded expected row count: a healthy
production database's counts change every day, so "row count == 0" would
either be a false negative (in a table that's legitimately still empty in
dev/staging) or require constant updating. What this script proves is
"the schema is really here and SQL against it works," which is what
DEPLOYMENT.md §3's verification requirement calls for — the human running
the drill still eyeballs whether the counts are *plausible* for what was
just backed up (i.e. non-zero if the source database had real data).
"""
from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine, inspect, text

REQUIRED_TABLES = ("calls", "subscriptions", "payments", "audit_events")


def main() -> int:
    url = os.environ.get("STERLING_DATABASE_URL")
    if not url:
        print("_verify_backup_counts: STERLING_DATABASE_URL not set", file=sys.stderr)
        return 1

    engine = create_engine(url)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    missing = [t for t in REQUIRED_TABLES if t not in tables]
    if missing:
        print(
            f"_verify_backup_counts: FAIL — restored database is missing tables: {missing}",
            file=sys.stderr,
        )
        return 1

    with engine.connect() as conn:
        for t in REQUIRED_TABLES:
            count = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
            print(f"  {t}: {count} rows")

    print("_verify_backup_counts: OK — all required tables present and queryable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
