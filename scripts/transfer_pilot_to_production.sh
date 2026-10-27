#!/usr/bin/env bash
# Carry the pilot (~/ageds-pilot) into the VM installation (docker compose -p ageds, /var/lib/ageds):
# acquire the same originals there through a read-only catalog scan, then copy the pilot ASR/OCR versions
# and event candidates onto them as new versions with their own runs (no model call, nothing sent anywhere).
# Dry run by default: prints every step and the plan. --apply executes. Needs sudo for docker.
# The deployed image must already contain `import-pilot-results` (deploy this commit with deploy_vm.sh first).
#   scripts/transfer_pilot_to_production.sh --source DIR [--pilot-db PATH] [--apply]
# DIR holds the pilot originals (the owner's uploads); it is mounted read-only. Stops api and worker,
# backs up SQLite (online backup API) and the content store with SHA-256 manifests, snapshots the pilot
# database next to the backup (that snapshot is what is transferred and recorded), runs, restarts, checks.
set -euo pipefail
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${AGEDS_DATA:-/var/lib/ageds}"
BACKUPS="${AGEDS_BACKUPS:-/var/lib/ageds-backups}"
PORT="${AGEDS_PORT:-8090}"
PILOT_DB="${AGEDS_PILOT_DIR:-$HOME/ageds-pilot}/data/evidence.db" SOURCE="" APPLY=no
while [ $# -gt 0 ]; do case "$1" in
  --apply) APPLY=yes; shift;; --source) SOURCE="$2"; shift 2;; --pilot-db) PILOT_DB="$2"; shift 2;;
  -h|--help) sed -n '2,10p' "$0"; exit 0;; *) echo "unknown argument: $1" >&2; exit 2;; esac; done
[ -n "$SOURCE" ] && [ -d "$SOURCE" ] || { echo "--source DIR (the pilot originals) is required" >&2; exit 2; }
[ -f "$PILOT_DB" ] || { echo "pilot database not found: $PILOT_DB" >&2; exit 2; }
SOURCE="$(cd "$SOURCE" && pwd)"
cd "$REPO"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="$BACKUPS/$STAMP-pre-pilot-transfer"
COMPOSE=(sudo docker compose -p ageds -f docker-compose.yml -f docker-compose.vm.yml)
ONE_OFF=("${COMPOSE[@]}" run --rm --no-deps -T -v "$SOURCE:/pilot-source:ro" -v "$TARGET/pilot-evidence.db:/pilot/evidence.db:ro"
         api python -m app.cli import-pilot-results --pilot-db /pilot/evidence.db --source /pilot-source)

run() {
  printf '+ %s\n' "$*"
  if [ "$APPLY" = yes ]; then "$@"; fi
}

echo "AGEDS pilot transfer ($([ "$APPLY" = yes ] && echo APPLY || echo 'dry run')): $PILOT_DB + $SOURCE -> $DATA"
echo "== 0. the deployed image knows import-pilot-results"
run sh -c "${COMPOSE[*]} exec -T api python -m app.cli import-pilot-results --help > /dev/null"

echo "== 1. stop the api and worker (no writes during backup and transfer)"
run "${COMPOSE[@]}" stop worker api

echo "== 2. backup to $TARGET"
run sudo mkdir -p "$TARGET"
run sudo python3 -c 'import sqlite3,sys
src = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
dst.close(); src.close()' "$DATA/evidence.db" "$TARGET/evidence.db"
run sudo cp -a "$DATA/store" "$TARGET/store"
run sudo sh -c "cd '$TARGET' && find store -type f -print0 | sort -z | xargs -0 -r sha256sum > store.sha256 && sha256sum evidence.db > evidence.sha256"
run sudo sh -c "cd '$DATA' && find store -type f -print0 | sort -z | xargs -0 -r sha256sum | diff - '$TARGET/store.sha256'"
echo "   pilot snapshot (consistent, without WAL) - the transferred and recorded database"
run sudo python3 -c 'import sqlite3,sys
src = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
dst.close(); src.close()' "$PILOT_DB" "$TARGET/pilot-evidence.db"
run sudo sh -c "cd '$TARGET' && sha256sum pilot-evidence.db > pilot-evidence.sha256 && (cd '$SOURCE' && sha256sum *) > source.sha256"

echo "== 3. plan, then transfer (one process; the source is mounted read-only)"
run "${ONE_OFF[@]}"
run "${ONE_OFF[@]}" --apply
run sh -c "cd '$SOURCE' && sha256sum * | diff - '$TARGET/source.sha256' && echo 'source unchanged'"

echo "== 4. start and check"
run "${COMPOSE[@]}" up -d api worker
run sh -c "for i in \$(seq 1 60); do curl -sf http://127.0.0.1:$PORT/advocate > /dev/null && exit 0; sleep 2; done; exit 1"
run sudo python3 -c 'import sqlite3,sys
db = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
copied = db.execute("SELECT count(*) FROM derived_text WHERE json_extract(metadata_json, \"$.pilot_transfer.key\") IS NOT NULL").fetchone()[0]
print("artifacts", db.execute("SELECT count(*) FROM artifacts").fetchone()[0], "transferred text versions", copied,
      "event candidates", db.execute("SELECT count(*) FROM event_candidates").fetchone()[0],
      "provider calls", db.execute("SELECT count(*) FROM provider_calls").fetchone()[0])' "$DATA/evidence.db"

cat <<EOF
== rollback (if a check failed)
  ${COMPOSE[*]} stop worker api
  sudo cp -a "$DATA" "$DATA.failed-$STAMP"            # keep the failed state, never delete it
  sudo cp "$TARGET/evidence.db" "$DATA/evidence.db"; sudo rm -f "$DATA/evidence.db-wal" "$DATA/evidence.db-shm"
  ${COMPOSE[*]} up -d api worker
The content store is append-only by content hash; new originals are extra files the restored database does not reference.
Running this again changes nothing: originals already here are not acquired again, each pilot version is copied once.
EOF
