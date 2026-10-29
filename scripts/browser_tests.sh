#!/usr/bin/env bash
# Real-Chromium acceptance suites without per-session setup.
#   scripts/browser_tests.sh            # every server/tests/browser/*.cjs
#   scripts/browser_tests.sh owner media # only these suites
# Playwright is pinned in server/tests/browser/package.json (lockfile) to the build of the browser
# preinstalled in /opt/pw-browsers; `npm ci` restores it when node_modules is missing. Set
# PLAYWRIGHT_BROWSERS_PATH or AGEDS_BROWSER_EXECUTABLE to use another browser. Synthetic fixtures only.
set -euo pipefail
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
DIR="$REPO/server/tests/browser"
export PYTHON="${PYTHON:-$REPO/.venv/bin/python}"
if [ -z "${PLAYWRIGHT_BROWSERS_PATH:-}" ] && [ -d /opt/pw-browsers ]; then
  export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
fi
if [ ! -d "$DIR/node_modules/playwright" ]; then
  (cd "$DIR" && PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm ci --no-audit --no-fund)
fi
suites=("$@")
if [ ${#suites[@]} -eq 0 ]; then
  for file in "$DIR"/*.cjs; do suites+=("$(basename "$file" .cjs)"); done
fi
failed=0
for suite in "${suites[@]}"; do
  log="$(mktemp)"
  if (cd "$REPO" && timeout "${AGEDS_BROWSER_TIMEOUT:-900}" node "$DIR/$suite.cjs") > "$log" 2>&1; then status=ok; else status=FAILED; failed=1; fi
  echo "$suite: $(grep -c '^PASS' "$log") passed, $status"
  [ "$status" = ok ] || tail -20 "$log"
  rm -f "$log"
done
exit "$failed"
