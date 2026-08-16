#!/usr/bin/env bash
# Sterling_Room database backup (Phase 10 — launch hardening; see
# DEPLOYMENT.md §3 "Backup strategy" for the full policy this implements).
#
# Provider-agnostic by design: works against ANY PostgreSQL-compatible
# DATABASE_URL (Render, RDS, Neon, Supabase, self-hosted, ...) via the
# standard `pg_dump` client. This script does NOT assume or configure a
# specific hosting provider's managed-backup product — see DEPLOYMENT.md
# §3 for why that's a deliberate scope boundary, not an oversight.
#
# Usage:
#   DATABASE_URL=postgresql://user:pass@host:5432/dbname ./scripts/backup_db.sh [output_dir]
#   DATABASE_URL=sqlite:////absolute/path/to/db ./scripts/backup_db.sh [output_dir]
#
# Output:
#   <output_dir>/sterling_backup_<UTC timestamp>.sql   (Postgres — plain SQL dump)
#   <output_dir>/sterling_backup_<UTC timestamp>.db    (SQLite — VACUUM INTO snapshot)
#
# Exit code is nonzero on ANY failure — this is meant to be safe to wire
# into a cron/scheduled job where a silent partial failure is worse than a
# loud one; do not swallow this script's exit code.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${1:-./backups}"
mkdir -p "$OUT_DIR"
TS="$(date -u +%Y%m%dT%H%M%SZ)"

: "${DATABASE_URL:?DATABASE_URL must be set — refusing to guess which database to back up}"

if [[ "$DATABASE_URL" == sqlite:* ]]; then
  DB_PATH="${DATABASE_URL#sqlite:///}"
  DB_PATH="${DB_PATH#sqlite://}"
  if [[ ! -f "$DB_PATH" ]]; then
    echo "backup_db: sqlite file not found at $DB_PATH" >&2
    exit 1
  fi
  OUT_FILE="$OUT_DIR/sterling_backup_${TS}.db"
  python3 "$SCRIPT_DIR/_sqlite_backup.py" "$DB_PATH" "$OUT_FILE"
  echo "backup_db: OK — wrote $OUT_FILE"

elif [[ "$DATABASE_URL" == postgresql://* || "$DATABASE_URL" == postgres://* ]]; then
  command -v pg_dump >/dev/null || {
    echo "backup_db: pg_dump not found — install the postgresql-client package on this host" >&2
    exit 1
  }
  OUT_FILE="$OUT_DIR/sterling_backup_${TS}.sql"
  # --format=plain: a portable, human-diffable SQL script (restored via
  # psql -f, see restore_db.sh) rather than the custom -Fc archive format —
  # simplest thing that works and doesn't require pg_restore version
  # matching between backup time and restore time.
  pg_dump --format=plain --no-owner --no-privileges "$DATABASE_URL" > "$OUT_FILE"
  echo "backup_db: OK — wrote $OUT_FILE"

else
  echo "backup_db: unrecognized DATABASE_URL scheme — expected sqlite:/// or postgresql://" >&2
  exit 1
fi
