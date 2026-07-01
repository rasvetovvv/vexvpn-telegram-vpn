#!/usr/bin/env bash
set -euo pipefail
BACKUP_DIR="/backups/vpnbot"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
OUT="$BACKUP_DIR/vpnbot-${STAMP}.sql.gz"
cd /root/vpnbot
if ! docker compose ps db >/dev/null 2>&1; then
  echo "docker compose db service not available" >&2
  exit 1
fi
docker compose exec -T db pg_dump -U vpnbot -d vpnbot | gzip -9 > "$OUT"
chmod 600 "$OUT"
find "$BACKUP_DIR" -type f -name 'vpnbot-*.sql.gz' -mtime +"$RETENTION_DAYS" -delete
printf 'OK %s %s\n' "$OUT" "$(du -h "$OUT" | awk '{print $1}')"
