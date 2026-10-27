#!/usr/bin/env bash
# AGEDS pilot outside Docker: a separate database and content store (never /var/lib/ageds), Vertex as
# the default ASR/OCR preset (owner consent R100), loopback only. Needs no sudo.
#   scripts/pilot_local.sh init            # create the data dir, owner, provider, consent, budget
#   scripts/pilot_local.sh serve           # API and pages on 127.0.0.1:${AGEDS_PILOT_PORT:-8091}
#   scripts/pilot_local.sh work [N]        # run the worker for N jobs (default: until the queue is empty)
#   scripts/pilot_local.sh backup          # SQLite online backup + content store copy + SHA-256 manifest
#   scripts/pilot_local.sh cli ARGS...     # any `python -m server.app.cli` command against the pilot
# Environment: AGEDS_PILOT_DIR (default ~/ageds-pilot), AGEDS_BACKUP_BUCKET (optional gs:// target).
set -euo pipefail
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PILOT="${AGEDS_PILOT_DIR:-$HOME/ageds-pilot}"
PY="$REPO/.venv/bin/python"
export EW_DATA_DIR="$PILOT/data" EW_DB_PATH="$PILOT/data/evidence.db" EW_STORE_DIR="$PILOT/data/store"
export EW_CASE_NAME="${EW_CASE_NAME:-Pilot}" EW_ASR_RECIPE="${EW_ASR_RECIPE:-asr-vertex-gemini}"
export EW_OCR_RECIPE="${EW_OCR_RECIPE:-ocr-vertex-gemini}"
cd "$REPO"
cli() { "$PY" -m server.app.cli "$@"; }

case "${1:-}" in
  init)
    umask 077
    mkdir -p "$PILOT/data/store" "$PILOT/backups"
    chmod 700 "$PILOT"
    if ! cli authz-show | grep -q '"installation.owner_principal_uid": "'; then
      cli authz-owner-init --name "Owner (pilot)" > "$PILOT/owner-token.json"
      chmod 600 "$PILOT/owner-token.json"
      echo "owner token saved to $PILOT/owner-token.json (0600)"
    fi
    cli advocate-provider vertex
    if ! cli advocate-spend | grep -q '"provider_name": "vertex"'; then
      cli advocate-consent --scope installation --basis "R100 owner 2026-10-25; pilot 2026-10-27"
    fi
    cli advocate-budget "${AGEDS_PILOT_BUDGET_EUR:-1.0}"
    ;;
  serve)
    exec "$PY" -m uvicorn server.app.main:app --host 127.0.0.1 --port "${AGEDS_PILOT_PORT:-8091}"
    ;;
  work)
    "$PY" - "${2:-0}" <<'PYEOF'
import sys
from server.app import worker
from server.app.jobs import claim_job
limit, done = int(sys.argv[1]), 0
while limit == 0 or done < limit:
    job = claim_job(worker_id='pilot')
    if job is None:
        break
    try:
        result = worker.process_job(job)
        print({'job': job['id'], 'kind': job['kind'], 'result': result}, flush=True)
    except Exception as exc:  # recorded on the job by process_job; keep going
        print({'job': job['id'], 'kind': job['kind'], 'error': f'{type(exc).__name__}: {str(exc)[:300]}'}, flush=True)
    done += 1
print({'processed': done})
PYEOF
    ;;
  backup)
    umask 077
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    target="$PILOT/backups/$stamp"
    mkdir -p "$target"
    "$PY" -c 'import sqlite3,sys
src = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True); dst = sqlite3.connect(sys.argv[2])
src.backup(dst); assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"; dst.close(); src.close()' \
      "$EW_DB_PATH" "$target/evidence.db"
    tar -C "$PILOT/data" -cf "$target/store.tar" store
    (cd "$target" && sha256sum evidence.db store.tar > SHA256SUMS)
    echo "backup: $target"
    if [ -n "${AGEDS_BACKUP_BUCKET:-}" ]; then
      gcloud storage cp -r "$target" "$AGEDS_BACKUP_BUCKET/$stamp/"
      echo "uploaded: $AGEDS_BACKUP_BUCKET/$stamp/ (no tokens or keys: owner-token.json stays local)"
    fi
    ;;
  cli)
    shift; cli "$@"
    ;;
  *)
    sed -n '2,9p' "$0"; exit 2
    ;;
esac
