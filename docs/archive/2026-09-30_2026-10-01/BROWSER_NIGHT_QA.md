# Actual browser acceptance — AGEDS-20261001-N12

The reproducible runner `server/tests/browser/acceptance.cjs` launches a local
FastAPI process and real headless Chromium. `server/tests/browser_fixture.py`
creates a temporary SQLite/store, two synthetic transcript versions, and a
4-second mono PCM WAV generated from a 440 Hz sine wave. It does not read a
private corpus, run ASR, or connect to an existing browser/session.

Run from the repository root with the server test dependencies and Playwright
available to Python/Node:

```sh
PYTHONPATH=/path/to/python-deps:$PWD \
AGEDS_BROWSER_EXECUTABLE=/path/to/chromium/headless_shell \
AGEDS_BROWSER_RECEIPT=docs/BROWSER_NIGHT_RECEIPT.json \
node server/tests/browser/acceptance.cjs
```

`AGEDS_BROWSER_EXECUTABLE` may be omitted if the installed Playwright browser
matches the package. `PYTHON` can select a virtualenv interpreter. The receipt
records actual Chromium/Playwright versions, UTC timestamps, tested git HEAD,
and SHA-256 fingerprints of the application files under test. A random loopback
port and temporary database isolate every run; fixtures are not committed.

All **11 browser cases passed** with no uncaught page errors. The completed cases cover an older transcript's exact word selector and quote,
literal source HTML in preview/newly saved/reloaded citations and search,
a delayed transcript response after a newer version has been selected,
a pending save across a version change, and real browser playback. Newly saved and reloaded older citations replay their stored 500–1800 ms range while the latest transcript remains selected; seeking outside the saved range and explicit stop cancel playback. The stale
transcript case intentionally removes AbortSignal in the test harness so the
separate generation check must reject the late response. The pending save must
retain its original version label without replacing the new version's status.

Audio checks use the native `HTMLAudioElement`, not a fake object. The browser
decodes the WAV, seeks from 0.2 seconds, stops at the selected 1.1-second endpoint,
and cancels on explicit stop, selection/version changes, and stop while metadata
is delayed. Endpoint assertion tolerance is 0.03 seconds after pause/clamping;
this is not a measured bound on acoustic output latency or scheduler overshoot.

The JSON receipt is the exact current outcome. These tests establish local
headless Chromium behavior. They do not establish audible output quality,
acoustic alignment of ASR, background-tab timing, every browser codec/platform,
physical-device behavior, or accessibility acceptance. No existing application
source was changed by this task.
