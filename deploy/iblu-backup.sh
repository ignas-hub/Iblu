#!/usr/bin/env bash
# Nightly logical backup of the IBLU database.
#
# Postgres runs in the `iblu-db` container and there is no pg_dump on the host,
# so the dump is taken inside the container (the `ignas` user is in the docker
# group; no sudo anywhere). Keeps 14 days.
set -euo pipefail

CONTAINER="${IBLU_DB_CONTAINER:-iblu-db}"
BACKUP_DIR="${IBLU_BACKUP_DIR:-/home/ignas/backups/iblu}"
KEEP_DAYS="${IBLU_BACKUP_KEEP_DAYS:-14}"
STAMP="$(date +%F)"
TARGET="${BACKUP_DIR}/iblu_keeper_${STAMP}.sql.gz"

mkdir -p "$BACKUP_DIR"

if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
    echo "backup: container ${CONTAINER} is not running — nothing dumped" >&2
    exit 1
fi

# Write to a temp file first: a truncated .sql.gz that looks like a backup is
# worse than no backup at all.
TMP="$(mktemp "${TARGET}.XXXXXX")"
trap 'rm -f "$TMP"' EXIT

docker exec "$CONTAINER" pg_dump -U iblu --clean --if-exists iblu_keeper | gzip -9 > "$TMP"

if [ ! -s "$TMP" ]; then
    echo "backup: dump was empty — refusing to publish it" >&2
    exit 1
fi
gzip -t "$TMP"

mv "$TMP"  "$TARGET"
trap - EXIT
chmod 600 "$TARGET"

find "$BACKUP_DIR" -name 'iblu_keeper_*.sql.gz' -mtime "+${KEEP_DAYS}" -delete

echo "backup: $(du -h "$TARGET" | cut -f1) -> ${TARGET} ($(find "$BACKUP_DIR" -name 'iblu_keeper_*.sql.gz' | wc -l) kept)"
