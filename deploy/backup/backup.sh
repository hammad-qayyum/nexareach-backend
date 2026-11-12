#!/bin/sh
# Encrypted database backups with restic.
#   backup.sh once   take one backup now
#   backup.sh loop   take one every day at BACKUP_HOUR (UTC), keep BACKUP_KEEP_DAILY of them
set -eu

backup_once() {
  restic cat config >/dev/null 2>&1 || restic init
  echo "$(date -u +%FT%TZ) backup starting"
  pg_dump --format=custom --no-owner | restic backup --stdin --stdin-filename nexareach.dump --tag db
  restic forget --tag db --keep-daily "${BACKUP_KEEP_DAILY:-30}" --prune
  echo "$(date -u +%FT%TZ) backup done"
}

case "${1:-loop}" in
  once) backup_once ;;
  loop)
    while true; do
      now=$(date -u +%s)
      target=$(date -u -d "today ${BACKUP_HOUR:-21}:00" +%s)
      [ "$target" -le "$now" ] && target=$((target + 86400))
      echo "next backup at $(date -u -d "@$target" +%FT%TZ)"
      sleep $((target - now))
      backup_once || echo "BACKUP FAILED" >&2
    done
    ;;
  *) echo "usage: backup.sh once|loop" >&2; exit 2 ;;
esac
