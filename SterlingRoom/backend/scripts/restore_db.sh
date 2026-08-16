#!/usr/bin/env bash
# Sterling_Room database restore (Phase 10 — launch hardening; see
# DEPLOYMENT.md §3). Restores a backup produced by backup_db.sh into a
# TARGET database that is always an explicit, separate argument — this
# script never reads the ambient DATABASE_URL and never restores "in
# place," specifically to make a fat-fingered production overwrite
# structurally harder (you cannot accidentally restore over the live
# database by forgetting to pass a target — there is no default).
#
# Usage:
#   ./scripts/restore_db.sh <backup_file> <target_database_url>
#
# Examples:
#   ./scripts/restore_db.sh backups/sterling_backup_20260101T000000Z.sql \
#     postgresql://user:pass@host:5432/sterling_restore_verify
#
#   ./scripts/restore_db.sh backups/sterling_backup_20260101T000000Z.db \
#     sqlite:////tmp/sterling_restore_verify.db
set -euo pipefail

BACKUP_FILE="${1:?usage: restore_db.sh <backup_file> <target_database_url>}"
TARGET_URL="${2:?usage: restore_db.sh <backup_file> <target_database_url>}"

[[ -f "$BACKUP_FILE" ]] || { echo "restore_db: backup file not found: $BACKUP_FILE" >&2; exit 1; }

if [[ "$TARGET_URL" == sqlite:* ]]; then
  DB_PATH="${TARGET_URL#sqlite:///}"
  DB_PATH="${DB_PATH#sqlite://}"
  if [[ -f "$DB_PATH" ]]; then
    echo "restore_db: refusing to overwrite existing file at $DB_PATH — remove it first if this is intentional" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$DB_PATH")"
  cp "$BACKUP_FILE" "$DB_PATH"
  echo "restore_db: OK — restored sqlite backup into $DB_PATH"

elif [[ "$TARGET_URL" == postgresql://* || "$TARGET_URL" == postgres://* ]]; then
  command -v psql >/dev/null || {
    echo "restore_db: psql not found — install the postgresql-client package on this host" >&2
    exit 1
  }
  # backup_db.sh produces a PLAIN SQL dump (--format=plain), so restore via
  # psql -f, not pg_restore (pg_restore is only for the custom/-Fc archive
  # format). ON_ERROR_STOP=1 so a mid-restore SQL error fails the script
  # loudly instead of silently leaving a half-restored target database.
  psql "$TARGET_URL" -v ON_ERROR_STOP=1 -f "$BACKUP_FILE"
  echo "restore_db: OK — restored into target database"

else
  echo "restore_db: unrecognized target URL scheme — expected sqlite:/// or postgresql://" >&2
  exit 1
fi
