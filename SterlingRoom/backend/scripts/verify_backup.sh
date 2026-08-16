#!/usr/bin/env bash
# Sterling_Room backup verification drill (Phase 10 — launch hardening;
# see DEPLOYMENT.md §3). "A backup that has never been restored is
# unverified" — this script automates the restore-into-a-scratch-database
# half of that drill and checks the result is actually usable, not just
# that a file exists on disk.
#
# Usage (SQLite backup — fully self-contained, restores into a throwaway
# temp file that is deleted when the script exits):
#   ./scripts/verify_backup.sh backups/sterling_backup_20260101T000000Z.db
#
# Usage (Postgres backup — VERIFY_TARGET_URL must point at a scratch
# database dedicated to this drill, never a live one):
#   VERIFY_TARGET_URL=postgresql://user:pass@host/sterling_verify_scratch \
#     ./scripts/verify_backup.sh backups/sterling_backup_20260101T000000Z.sql
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_FILE="${1:?usage: verify_backup.sh <backup_file>}"
[[ -f "$BACKUP_FILE" ]] || { echo "verify_backup: backup file not found: $BACKUP_FILE" >&2; exit 1; }

if [[ "$BACKUP_FILE" == *.db ]]; then
  SCRATCH="$(mktemp -u /tmp/sterling_verify_XXXXXX).db"
  trap 'rm -f "$SCRATCH"' EXIT
  "$SCRIPT_DIR/restore_db.sh" "$BACKUP_FILE" "sqlite:///$SCRATCH"
  TARGET_URL="sqlite:///$SCRATCH"
else
  : "${VERIFY_TARGET_URL:?VERIFY_TARGET_URL must be set to a scratch Postgres database for verifying a .sql backup — never point this at a live database}"
  "$SCRIPT_DIR/restore_db.sh" "$BACKUP_FILE" "$VERIFY_TARGET_URL"
  TARGET_URL="$VERIFY_TARGET_URL"
fi

echo "verify_backup: checking migration state (alembic current)..."
STERLING_DATABASE_URL="$TARGET_URL" python3 -m alembic -c "$SCRIPT_DIR/../alembic.ini" current

echo "verify_backup: checking required tables are present and queryable..."
STERLING_DATABASE_URL="$TARGET_URL" python3 "$SCRIPT_DIR/_verify_backup_counts.py"

echo "verify_backup: OK — backup restored successfully and passed sanity checks"
