#!/usr/bin/env bash
# Off-site backup of the SQLite DB, run daily by inbox-jev-backup.timer (as root); by hand:
#   sudo scripts/backup.sh
#
# 1. Snapshot data/inbox-jev.db with sqlite3's .backup (the online backup API: a consistent copy,
#    WAL included, while a triage pass may be writing), check it (PRAGMA quick_check) and gzip it.
# 2. Upload it with `rclone copyto` to BACKUP_RCLONE_REMOTE (e.g. gdrive:inbox-jev-backups) as
#    inbox-jev-<UTC stamp>.db.gz.
# 3. Delete the remote's inbox-jev-*.db.gz older than BACKUP_RETENTION_DAYS (default 30), at its
#    top level only: never in a subfolder (a copy kept by hand) or elsewhere under a bare remote.
#
# Off (logged, exit 0) when BACKUP_RCLONE_REMOTE isn't set. Any failure exits non-zero, so the
# service's OnFailure= posts an alert. One-time rclone setup: README → "Off-site backups".
set -euo pipefail

# shellcheck source=ops_common.sh
. "$(dirname "$0")/ops_common.sh"
cd "$OPS_ROOT"

remote=$(env_value BACKUP_RCLONE_REMOTE)
if [ -z "$remote" ]; then
  echo "BACKUP_RCLONE_REMOTE is not set: off-site backup is off"
  exit 0
fi
retention_days=$(env_value BACKUP_RETENTION_DAYS 30)
require_count BACKUP_RETENTION_DAYS "$retention_days" 1
db=data/inbox-jev.db
command -v rclone >/dev/null || { echo "rclone is not installed (README → Off-site backups)" >&2; exit 1; }
[ -f "$db" ] || { echo "$db not found: nothing to back up (hosted DATABASE_URL? unset BACKUP_RCLONE_REMOTE)" >&2; exit 1; }

# As root, sqlite3 runs as the app's uid: a -wal or -shm file it creates must stay writable by
# the app (data/ belongs to APP_UID:APP_GID).
uid=$(env_value APP_UID 1000)
gid=$(env_value APP_GID 1000)
as_app() {
  if [ "$(id -u)" = 0 ]; then
    setpriv --reuid "$uid" --regid "$gid" --clear-groups "$@"
  else
    "$@"
  fi
}
tmp=$(mktemp -d "${TMPDIR:-/var/tmp}/inbox-jev-backup.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
if [ "$(id -u)" = 0 ]; then
  chown "$uid:$gid" "$tmp"
fi

name="inbox-jev-$(date -u +%Y%m%dT%H%M%SZ).db"
as_app sqlite3 "$db" ".backup '$tmp/$name'"
check=$(as_app sqlite3 "$tmp/$name" 'PRAGMA quick_check;')
if [ "$check" != ok ]; then
  echo "the snapshot failed its integrity check: $check" >&2
  exit 1
fi
gzip -9 "$tmp/$name"

case $remote in
  *:) target=$remote ;;
  *) target=${remote%/}/ ;;
esac
echo "== uploading $name.gz ($(du -h "$tmp/$name.gz" | cut -f1)) to $target"
rclone copyto "$tmp/$name.gz" "$target$name.gz"
echo "== deleting remote backups older than $retention_days days"
# --max-depth 1 and the anchored pattern: rclone delete recurses, and an unanchored --include
# matches the name at any depth.
rclone delete --max-depth 1 --min-age "${retention_days}d" --include '/inbox-jev-*.db.gz' "$remote"
echo "== done"
