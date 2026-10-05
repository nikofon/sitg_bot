#!/bin/sh
# SITG database backup with hang protection.
#
# Safety properties:
#   - flock: a second run while one is active exits instead of stacking;
#   - timeout: a hung dump is killed after TIMEOUT_SECONDS (Ctrl+Z-style
#     suspended or stuck clients otherwise hold locks server-side forever);
#   - on failure the pg_dump session is terminated inside PostgreSQL so it
#     cannot linger holding ACCESS SHARE locks;
#   - dumps older than RETENTION_DAYS are pruned.
#
# Run as root from the project root, e.g. from cron:
#   cd /opt/SITGBot && ./deploy/backup_database.sh >> /var/log/sitg-backup.log 2>&1
set -eu

compose_file="${COMPOSE_FILE:-compose.vps.yaml}"
backup_dir="${BACKUP_DIR:-backups}"
retention_days="${RETENTION_DAYS:-14}"
timeout_seconds="${TIMEOUT_SECONDS:-1800}"

mkdir -p "$backup_dir"
exec 9>"$backup_dir/.backup.lock"
if ! flock -n 9; then
  echo "$(date -Is) another backup is already running, skipping" >&2
  exit 0
fi

terminate_dump_sessions() {
  docker compose -f "$compose_file" exec -T postgres psql -U sitg -d sitg -c \
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity
     WHERE query LIKE 'COPY%' AND pid <> pg_backend_pid();" </dev/null >/dev/null 2>&1 || true
}

stamp="$(date +%F-%H%M%S)"
target="$backup_dir/sitg-$stamp.dump"
trap 'rm -f "$target.partial"' EXIT

if timeout "$timeout_seconds" docker compose -f "$compose_file" exec -T postgres \
  pg_dump -U sitg -d sitg -Fc </dev/null > "$target.partial"; then
  mv "$target.partial" "$target"
  echo "$(date -Is) backup written: $target ($(du -h "$target" | cut -f1))"
else
  status=$?
  terminate_dump_sessions
  echo "$(date -Is) backup failed (exit $status); dump sessions terminated" >&2
  exit "$status"
fi

find "$backup_dir" -maxdepth 1 -name 'sitg-*.dump' -mtime "+$retention_days" -delete
echo "$(date -Is) pruned dumps older than ${retention_days} days"
