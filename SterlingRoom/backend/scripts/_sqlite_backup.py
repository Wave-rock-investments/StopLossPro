#!/usr/bin/env python3
"""Helper invoked by backup_db.sh for SQLite sources.

Performs an online, atomic snapshot via SQLite's own `VACUUM INTO` command
— a single consistent point-in-time copy that is safe to run against a
database the app or the background worker (app/worker.py) may be writing
to concurrently (unlike a raw `cp` of the file, which can copy a
mid-write, torn state).

Uses only Python's stdlib `sqlite3` module — Python is already a hard
dependency of this project, so this avoids requiring the separate
`sqlite3` CLI binary to be installed on the host, which is not guaranteed
on every minimal deployment image.
"""
from __future__ import annotations

import sqlite3
import sys


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: _sqlite_backup.py <source_db_path> <dest_backup_path>", file=sys.stderr)
        return 1

    src_path, dest_path = sys.argv[1], sys.argv[2]
    conn = sqlite3.connect(src_path)
    try:
        # VACUUM INTO takes its destination as SQL string-literal text, not
        # a bind parameter (SQLite does not support "?" there) — escape
        # embedded single quotes defensively since we're building SQL text
        # from a filesystem path.
        escaped = dest_path.replace("'", "''")
        conn.execute(f"VACUUM INTO '{escaped}'")
    finally:
        conn.close()

    print(f"_sqlite_backup: wrote {dest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
