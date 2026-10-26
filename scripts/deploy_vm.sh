#!/usr/bin/env bash
# Deploy the current AGEDS checkout on the VM (docker compose -p ageds, overlay docker-compose.vm.yml,
# 127.0.0.1:8090, data in /var/lib/ageds). Dry run by default: prints every step. --apply executes.
# Needs sudo for docker and /var/lib/ageds. Stops the old api and worker before the backup (an old
# loaded worker has no lease fencing from edited files), backs up SQLite with the online backup API
# and the content store byte for byte, then builds, starts and checks. It never deletes a backup.
#   scripts/deploy_vm.sh [--apply] [--skip-tests]
set -euo pipefail
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${AGEDS_DATA:-/var/lib/ageds}"
BACKUPS="${AGEDS_BACKUPS:-/var/lib/ageds-backups}"
PORT="${AGEDS_PORT:-8090}"
APPLY=no TESTS=yes
for arg in "$@"; do case "$arg" in
  --apply) APPLY=yes;; --skip-tests) TESTS=no;;
  -h|--help) sed -n '2,8p' "$0"; exit 0;; *) echo "unknown argument: $arg" >&2; exit 2;; esac; done
cd "$REPO"
COMMIT="$(git rev-parse HEAD)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="$BACKUPS/$STAMP-${COMMIT:0:12}"
COMPOSE=(sudo docker compose -p ageds -f docker-compose.yml -f docker-compose.vm.yml)

run() {
  printf '+ %s\n' "$*"
  if [ "$APPLY" = yes ]; then "$@"; fi
}

echo "AGEDS deployment of $COMMIT ($([ "$APPLY" = yes ] && echo APPLY || echo 'dry run'))"
[ -f docker-compose.vm.yml ] || { echo "docker-compose.vm.yml missing: this is not the VM checkout" >&2; exit 1; }
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "tracked files have local changes; deploy only a committed revision" >&2; exit 1
fi
if [ "$TESTS" = yes ]; then
  run "$REPO/.venv/bin/python" -m pytest server/tests -q -x
fi

echo "== 1. stop the old api and worker (no writes during backup)"
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
run sudo sh -c "printf '%s\n' '$COMMIT' > '$TARGET/deployed_commit'"

echo "== 3. build and start $COMMIT (migrations are additive and run at startup)"
run "${COMPOSE[@]}" build api worker
run "${COMPOSE[@]}" up -d api worker

echo "== 4. checks"
run sh -c "for i in \$(seq 1 60); do curl -sf http://127.0.0.1:$PORT/advocate > /dev/null && exit 0; sleep 2; done; exit 1"
run sh -c "curl -s -o /dev/null -D - http://127.0.0.1:$PORT/api/artifacts | grep -i '^x-ageds-auth-mode:'"
run sh -c "curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:$PORT/api/advocate/status | grep -E '^(200|409)$'"
run sudo python3 -c 'import sqlite3,sys
db = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
versions = {row[0] for row in db.execute("SELECT version FROM schema_migrations")}
missing = {1, 2, 3, 4, 5, 6} - versions
assert not missing, f"migrations missing: {sorted(missing)}"
print("migrations", sorted(versions), "artifacts", db.execute("SELECT count(*) FROM artifacts").fetchone()[0])' "$DATA/evidence.db"

cat <<EOF
== rollback (if a check failed)
  ${COMPOSE[*]} stop worker api
  sudo cp -a "$DATA" "$DATA.failed-$STAMP"            # keep the failed state, never delete it
  sudo cp "$TARGET/evidence.db" "$DATA/evidence.db"; sudo rm -f "$DATA/evidence.db-wal" "$DATA/evidence.db-shm"
  git checkout <previous commit> && ${COMPOSE[*]} build api worker && ${COMPOSE[*]} up -d api worker
The content store is append-only by content hash; the backup copy is in $TARGET/store.
Accounts stay in legacy_open until the owner runs authz-owner-init and authz-mode tokens, inside the container:
  ${COMPOSE[*]} exec api python -m app.cli authz-show
EOF
