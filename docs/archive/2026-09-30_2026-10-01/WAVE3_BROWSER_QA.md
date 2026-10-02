# Wave 3 — actual browser packet-download acceptance

Task: `AGEDS-20261001-N21-browser`; baseline browser task `AGEDS-20261001-N12`.
Result: **13 actual headless Chromium cases passed**, including the previous 11
browser cases and two new packet-download cases. No page errors occurred.
Machine-readable evidence: `docs/WAVE3_BROWSER_RECEIPT.json`; earlier browser
receipts remain unchanged.

## Execution

```
AGEDS_BROWSER_RECEIPT=docs/WAVE3_BROWSER_RECEIPT.json \
PYTHONPATH=/workspace/scratch/773725428b88/review/deps:$PWD \
AGEDS_BROWSER_EXECUTABLE=/root/.cache/ms-playwright/chromium_headless_shell-1194/chrome-linux/headless_shell \
node server/tests/browser/acceptance.cjs
```

Used existing Playwright 1.62.1 and Chromium 141.0.7390.37; no downloads or new
runtime installation. Execution lasted approximately eight seconds. The receipt
records Git HEAD, exact hashes of server/UI/exchange/consumer/test inputs, browser
version, timestamps and all individual cases.

## New evidence

The fixture generates a four-second PCM WAV and an isolated temporary SQLite
store with two synthetic transcript versions. The browser saves a word citation
from the older version, selects the latest transcript and clicks the real download
link. It repeats the operation after a page reload, exercising both JavaScript-created
and server-rendered persisted links.

Both downloaded packets:

- Arrive as actual browser downloads, with filename `ageds-citation-1-1.json`,
  attachment Content-Disposition, application/json and `Cache-Control: no-store`.
- Preserve the older transcript ID, exact selected quote and complete old
  transcript context even while the newer transcript stays selected.
- Independently pass the standalone standard-library consumer in fresh Python
  processes. Those processes create no live database/store directory.
- Are byte-identical: 3,925 bytes and SHA256
  `705e0e7df983ee07cd359ba378a8439d78299805abf07f19903b44582a2ff045`
  in this recorded run. Fixture timestamps/temporary paths can change this hash
  on another run.
- Omit `stored_path`, retain acquisition provenance and the declared content
  digest, and explicitly exclude source/media bytes, restore and task import.
- Trigger zero source-media HTTP requests during download, keep the page and
  selected version unchanged, and preserve malicious-looking source HTML as
  inert text without creating an image element or executing its handler.

The previous cases still verify exact word selection, stored old-version replay,
stale-response suppression, pending save pinning, genuine HTMLAudioElement seek
and bounded stop, cancellation controls, delayed media metadata and inert search
snippets. The recorded playback stopped at 1.1 seconds within its 30 ms test
allowance.

## Limits

This is local headless Chromium against the real local HTTP server, generated
sound and synthetic transcripts. It is not phone/device acceptance, acoustic
alignment or human listening validation. Download verification establishes internal
consistency of included metadata; there is no signature, authentication of source
claims, source-byte recheck, replay, live restore or exercised partner adapter.
Names/locators/full transcript content remain literal metadata suitable only for
intentional sharing. Browser media-request observation complements, but does not
replace, the server-side no-source-read tests in `docs/WAVE3_EXCHANGE_QA.md`.
