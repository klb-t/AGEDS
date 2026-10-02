# Browser acceptance after generic search example — 2026-10-02

**25 of 25 real Chromium scenarios passed** on
`6110f052e72eb3c040a3bb0ad39da07b73d9537d`, after replacing the search
placeholder with a generic query example. This is a new acceptance, preserving
the earlier [browser completion receipt](BROWSER_COMPLETION_2026-10-02.json).
The executed run and input hashes are recorded in
[`BROWSER_PRIVACY_2026-10-02.json`](BROWSER_PRIVACY_2026-10-02.json).

Only `server/app/templates/index.html` differs among the browser inputs from the
previous successful run. The production citation selectors, response validation,
range player, fixtures and acceptance script retain the same hashes. The Node
suite was not repeated; its separate 85-test acceptance remains recorded in the
earlier receipt.

The new run again exercised the four wave-13 selector/race scenarios, actual
HTTP save and media-range responses, native `HTMLAudioElement` playback of a
generated WAV, pinned packet downloads and independent inspection, literal source
HTML, exact identities, 125-row history paging and the 4 MiB payload limit. All
cases passed with no browser page errors. Private material and paid services
were not used.

The read-only inspection found no known case-identity literals in the current
tracked tree. The search input now displays `Szukaj: "fraza" OR termin`. This
inspection does not modify historical Git ancestry or source materials.

Reproduction uses the same local Python fixture, Playwright 1.62.1 and real
Chromium headless shell 141.0.7390.37 as the earlier acceptance:

```sh
NODE_PATH="$CODEX_PRIMARY_RUNTIME_NODE_MODULES" \
PYTHON="$PWD/.venv/bin/python" \
AGEDS_BROWSER_EXECUTABLE=/root/.cache/ms-playwright/chromium_headless_shell-1194/chrome-linux/headless_shell \
AGEDS_BROWSER_BUDGET_FIXTURE=1 \
AGEDS_BROWSER_RECEIPT=/tmp/ageds-browser-new-privacy-receipt.json \
"$CODEX_PRIMARY_RUNTIME_NODE" server/tests/browser/acceptance.cjs
```

The executable path is specific to the recorded environment. Use the installed
compatible Chromium path on another machine and a new receipt destination.
Browser flow acceptance does not establish ASR quality, acoustic alignment,
Android/SAF runtime or partner integrations.
