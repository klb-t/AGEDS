#!/usr/bin/env bash
# Switch the VM installation between auth.mode legacy_open and tokens, and back. Dry run by default.
#   scripts/auth_mode_vm.sh --to tokens [--owner-token-file PATH] [--apply]
#   scripts/auth_mode_vm.sh --to legacy_open --proxy-exception-removed [--owner-token-file PATH] [--apply]
# tokens: recipients' grants are enforced and the owner signs in at /advocate with the owner token
# (its session cookie opens every page). Order with the proxy (docs/ADVOCATE.md):
#   to tokens:      1. this script --to tokens --apply   2. then add the proxy exception for the recipient routes
#   to legacy_open: 1. remove the proxy exception first  2. then this script --to legacy_open
# The owner token is read from its file and never printed. Needs sudo for docker.
set -euo pipefail
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${AGEDS_PORT:-8090}"
TO="" APPLY=no REMOVED=no TOKEN_FILE="${AGEDS_OWNER_TOKEN_FILE:-$HOME/.config/ageds/owner_init.json}"
while [ $# -gt 0 ]; do case "$1" in
  --to) TO="$2"; shift 2;; --apply) APPLY=yes; shift;; --proxy-exception-removed) REMOVED=yes; shift;;
  --owner-token-file) TOKEN_FILE="$2"; shift 2;;
  -h|--help) sed -n '2,9p' "$0"; exit 0;; *) echo "unknown argument: $1" >&2; exit 2;; esac; done
case "$TO" in tokens|legacy_open) ;; *) echo "--to tokens|legacy_open is required" >&2; exit 2;; esac
cd "$REPO"
COMPOSE=(sudo docker compose -p ageds -f docker-compose.yml -f docker-compose.vm.yml)
BASE="http://127.0.0.1:$PORT"

run() {
  printf '+ %s\n' "$*"
  if [ "$APPLY" = yes ]; then "$@"; fi
}
status_of() {  # HTTP status of a GET, with optional extra curl arguments
  curl -s -o /dev/null -w '%{http_code}' "$@"
}

echo "AGEDS auth.mode -> $TO ($([ "$APPLY" = yes ] && echo APPLY || echo 'dry run'))"
[ -r "$TOKEN_FILE" ] || { echo "owner token file not readable: $TOKEN_FILE (written by authz-owner-init)" >&2; exit 1; }
OWNER_TOKEN="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["token"])' "$TOKEN_FILE")"
[ -n "$OWNER_TOKEN" ] || { echo "no token in $TOKEN_FILE" >&2; exit 1; }

echo "== 1. preconditions"
run sh -c "${COMPOSE[*]} exec -T api python -m app.cli authz-show | python3 -c 'import json,sys
shown = json.load(sys.stdin)
settings = shown.get(\"settings\", {})
uid = settings.get(\"installation.owner_principal_uid\") if isinstance(settings, dict) else None
assert uid, \"no installation owner: run authz-owner-init first\"
print(\"owner present; current auth.mode:\", settings.get(\"auth.mode\", \"legacy_open\"))'"
if [ "$TO" = legacy_open ] && [ "$REMOVED" = no ]; then
  echo "refusing: remove the proxy exception for the recipient routes first, then pass --proxy-exception-removed" >&2
  echo "(in legacy_open the proxy is a local client, so an exempted route would make any visitor the owner)" >&2
  exit 1
fi

echo "== 2. switch"
if [ "$TO" = tokens ]; then
  run "${COMPOSE[@]}" exec -T api python -m app.cli authz-mode tokens
else
  run "${COMPOSE[@]}" exec -T api python -m app.cli authz-mode legacy_open --confirm
fi

echo "== 3. checks"
if [ "$APPLY" = yes ]; then
  anonymous="$(status_of "$BASE/api/artifacts")"
  owner="$(status_of -H "X-AGEDS-Token: $OWNER_TOKEN" "$BASE/api/artifacts")"
  marked="$(status_of -H 'X-AGEDS-Public-Route: 1' "$BASE/api/artifacts/1/content")"
  echo "anonymous /api/artifacts: $anonymous · owner token: $owner · public-route header without token: $marked"
  if [ "$TO" = tokens ]; then
    [ "$anonymous" = 401 ] && [ "$owner" = 200 ] && [ "$marked" = 401 ] \
      || { echo "unexpected result; to go back: $0 --to legacy_open --proxy-exception-removed --apply" >&2; exit 1; }
    echo "ok: the owner signs in at /advocate with the owner token; then add the proxy exception (docs/ADVOCATE.md)"
  else
    [ "$owner" = 200 ] && [ "$marked" = 403 ] || { echo "unexpected result" >&2; exit 1; }
    echo "ok: legacy_open again; routes marked public by the proxy are refused"
  fi
else
  echo "+ GET $BASE/api/artifacts without token (expect $([ "$TO" = tokens ] && echo 401 || echo 200)), with the owner token (expect 200)"
  echo "+ GET $BASE/api/artifacts/1/content with X-AGEDS-Public-Route and no token (expect $([ "$TO" = tokens ] && echo 401 || echo 403))"
fi
