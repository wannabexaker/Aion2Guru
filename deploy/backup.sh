#!/usr/bin/env bash
# Nightly logical backup of the guru database (custom format), 14-day retention.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
out="backups/guru-$(date +%Y%m%d-%H%M).dump"
docker compose exec -T postgres pg_dump -U guru -d guru -Fc > "$out"
test -s "$out" || { echo "backup failed: empty dump" >&2; exit 1; }
find backups -name 'guru-*.dump' -mtime +14 -delete
echo "backup ok: $out ($(du -h "$out" | cut -f1))"
