# Browser runtime completion — 2026-10-02

**Passed: 25 real Chromium scenarios and all 85 Node tests.** This completes the
previously prepared but unexecuted N83 browser acceptance from wave 13. No
production change was required. The machine receipt is
[`BROWSER_COMPLETION_2026-10-02.json`](BROWSER_COMPLETION_2026-10-02.json).

Tested source revision: `b6abe86ec1cdcd4ca8669be359ddb25422a51cc2`. The receipt
records the tested source hashes; subsequent documentation reorganisation does
not change those input files. Historical wave receipts remain unchanged in
[`../archive/2026-09-30_2026-10-01/`](../archive/2026-09-30_2026-10-01/).

## Executed scope

| Check | Result |
| --- | --- |
| Forged word occurrence with identical text, version and rounded times | Refused before adding history, playback or export links. |
| Forged segment occurrence with identical text, version and rounded times | Refused before adding history, playback or export links. |
| Real HTTP save after refusal | Saved selected word occurrence `(0, 0)`; `.0005–.0025` seconds projects to `0–2` milliseconds. |
| Late forged response after switching version | Refused; newer version status and preview retained; no added history or automatic playback. |
| Existing valid late response | Retains the originally selected version while preserving the newer version's status. |
| Real WAV and `HTMLAudioElement` | Four-second generated WAV plays, seeks, stops at the selected boundary and cancels on stop, selection/version change or outside-range seek. |
| Citation downloads | Dynamic and persisted links download identical pinned packets; independent Python consumer verifies them without opening locators or creating live storage. |
| HTTP media | Exact 48-byte range, full `HEAD` semantics and mismatched `If-Range` fallback verified. |
| History and payload limits | 125 versions, citations and annotations paginate without duplicates; four of six large citations retained under the 4 MiB JSON budget with an explicit omission message. |
| Source text and identities | Source HTML remains literal; unsafe or mismatched DOM/API IDs cannot enable quote actions. |
| Node suite | 85 passed, zero failed/skipped/cancelled; includes production save-handler tests and 3,008 Python/JavaScript time-boundary comparisons. |

Chromium reported no page errors. Browser execution used actual local HTTP,
temporary SQLite and a generated PCM WAV. Deliberately forged save responses and
delayed requests are Playwright fault injection into the real browser; they do
not replace the browser, backend or audio element and do not assert that the
backend generates corrupt selectors.

## Reproduction

From the repository root, install `server/requirements-test.txt` into `.venv` and
make Playwright and a compatible Chromium executable available. Node v24.19.0,
Playwright 1.62.1 and Chromium headless shell 141.0.7390.37 were used here:

```sh
PYTHON="$PWD/.venv/bin/python" node --test server/tests/js/*.test.mjs

NODE_PATH="$CODEX_PRIMARY_RUNTIME_NODE_MODULES" \
PYTHON="$PWD/.venv/bin/python" \
AGEDS_BROWSER_EXECUTABLE=/root/.cache/ms-playwright/chromium_headless_shell-1194/chrome-linux/headless_shell \
AGEDS_BROWSER_BUDGET_FIXTURE=1 \
AGEDS_BROWSER_RECEIPT=/tmp/ageds-browser-new-receipt.json \
"$CODEX_PRIMARY_RUNTIME_NODE" server/tests/browser/acceptance.cjs
```

The executable path above identifies this run's environment, not a portable
installation requirement. On another machine use its installed headless-shell
path, or omit `AGEDS_BROWSER_EXECUTABLE` when the matching Playwright browser is
installed. Always write a new receipt rather than replacing historical evidence.

The full cached Chrome executable failed to start in this environment because
its process singleton requires a blocked `AF_UNIX` socket. The existing
headless-shell executable started successfully and completed all cases. No
browser download, paid service, GitHub Actions run or private corpus was needed.

This acceptance establishes client selection/response consistency and the
listed browser flows. It does not measure ASR quality, acoustic alignment,
Android/SAF runtime, partner-project integration or the truth of source content.
