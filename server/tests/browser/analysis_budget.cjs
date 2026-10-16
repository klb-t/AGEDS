/** Real Chromium acceptance of /analysis/budget against an isolated synthetic SQLite fixture.
 *
 * Fixture: `python -m server.tests.corpus.activity` (generated corpus, temporary database,
 * two stored runs and one human correction). Env: PYTHON, AGEDS_BROWSER_EXECUTABLE,
 * NODE_PATH (playwright), optional AGEDS_BROWSER_RECEIPT (JSON receipt path).
 */
const {chromium} = require('playwright');
const {spawn, execFileSync} = require('node:child_process');
const {once} = require('node:events');
const net = require('node:net');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const repo = path.resolve(__dirname, '../../..');
const delay = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const socket = net.createServer(); socket.listen(0, '127.0.0.1'); await once(socket, 'listening');
  const port = socket.address().port; await new Promise(r => socket.close(r));
  const server = spawn(process.env.PYTHON || 'python', ['-m', 'server.tests.corpus.activity'], {
    cwd: repo, env: {...process.env, AGEDS_BROWSER_PORT: String(port)}, stdio: ['ignore', 'pipe', 'pipe']});
  let stderr = ''; server.stderr.on('data', d => stderr += d);
  let browser;
  const receipt = {git_head: execFileSync('git', ['rev-parse', 'HEAD'], {cwd: repo, encoding: 'utf8'}).trim(),
    task: 'AGEDS-TASK-20261016-E (E1 time budget)', playwright: require('playwright/package.json').version,
    started_at: new Date().toISOString(), cases: [], measurements: {}};
  let failed = false;
  try {
    const fixture = await new Promise((resolve, reject) => {
      let buf = ''; server.stdout.on('data', d => {buf += d; if (buf.includes('\n')) {try {resolve(JSON.parse(buf.split('\n')[0]));} catch (e) {reject(e);}}});
      server.on('exit', code => reject(new Error(`fixture exited ${code}: ${stderr}`)));
    });
    const base = `http://127.0.0.1:${port}`;
    for (let i = 0; i < 200; i++) {try {if ((await fetch(base + '/api/analysis/runs')).ok) break;} catch {} await delay(50);}
    browser = await chromium.launch({headless: true, executablePath: process.env.AGEDS_BROWSER_EXECUTABLE || undefined, args: ['--no-sandbox']});
    receipt.browser = browser.version();
    const page = await browser.newPage({viewport: {width: 1280, height: 900}});
    const pageErrors = [], consoleErrors = [];
    page.on('pageerror', e => pageErrors.push(e.message));
    page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text()); });
    const failedResponses = [];
    page.on('response', r => { if (r.status() >= 400) failedResponses.push(`${r.status()} ${new URL(r.url()).pathname}`); });
    async function test(name, fn) {
      const started = Date.now();
      await fn();
      receipt.cases.push({name, status: 'passed', ms: Date.now() - started});
      console.log(`PASS ${name}`);
    }
    const [firstRun, secondRun] = fixture.runs;
    const ready = id => page.waitForFunction(id => document.querySelector('[data-budget]')?.dataset.state === 'ready'
      && document.querySelector('[data-budget]').dataset.runId === String(id), id);

    await test('page renders newest run with candles and unobserved strip', async () => {
      const t = Date.now();
      await page.goto(base + '/analysis/budget');
      await ready(secondRun);
      receipt.measurements.first_render_ms = Date.now() - t;
      assert.equal(await page.locator('[data-runs] option').count(), 2);
      assert.ok(await page.locator('[data-candles] [data-candle]').count() > 5);
      assert.equal(await page.locator('[data-unobserved] [data-candle]').count(), 8);  // 8 local days, one of 23 h
      assert.deepEqual(await page.locator('[data-granularity] option').evaluateAll(o => o.map(x => x.value)), ['day', 'week']);
      assert.match(await page.locator('header').innerText(), /nie bezczynność/);
    });

    await test('run picker switches runs and granularity', async () => {
      await page.selectOption('[data-runs]', String(firstRun));
      await ready(firstRun);
      assert.deepEqual(await page.locator('[data-granularity] option').evaluateAll(o => o.map(x => x.value)), ['day', 'week', 'month']);
      await page.selectOption('[data-granularity]', 'week');
      await page.waitForFunction(() => [...document.querySelectorAll('[data-candles] [data-candle]')].every(g => g.dataset.bucket.includes('-W')));
      await page.selectOption('[data-granularity]', 'day');
      await page.waitForFunction(() => document.querySelector('[data-candles] [data-candle]')?.dataset.bucket.length === 10);
    });

    await test('click candle lists contributing events with label path and links', async () => {
      const candle = page.locator('[data-candles] [data-candle][data-bucket="2025-03-28"][data-category="sprawy_urzedowe"]');
      await candle.click();
      const row = page.locator(`[data-candle-table] tr[data-event-id="${fixture.events['call-official']}"]`);
      await row.waitFor();
      assert.match(await row.innerText(), /rule · urzedy-numery@1/);
      assert.equal(await row.locator('a', {hasText: 'etykiety'}).getAttribute('href'), `/api/analysis/events/${fixture.events['call-official']}`);
      assert.match(await page.locator('[data-candle-summary]').innerText(), /dolna 20 min 00 s/);
    });

    await test('manual correction is visible as the annotation path', async () => {
      await page.locator('[data-candles] [data-candle][data-bucket="2025-03-28"][data-category="odpoczynek"]').click();
      const row = page.locator(`[data-candle-table] tr[data-event-id="${fixture.events['sms-override']}"]`);
      await row.waitFor();
      assert.match(await row.innerText(), /annotation · korekta #/);
    });

    await test('unobserved candle explains the gap and is keyboard reachable', async () => {
      const gap = page.locator('[data-unobserved] [data-candle][data-bucket="2025-04-01"]');
      await gap.focus();
      await page.keyboard.press('Enter');
      await page.waitForFunction(() => /not idleness/.test(document.querySelector('[data-candle-events]')?.innerText || ''));
      assert.match(await page.locator('[data-candle-summary]').innerText(), /prawdopodobna 24 h 00 min/);
    });

    await test('table, sensitivity, exclusions and method panels are filled', async () => {
      assert.ok(await page.locator('[data-budget-table] tr').count() > 10);
      assert.equal(await page.locator('[data-unstable]').getAttribute('data-unstable'), 'true');
      assert.ok(await page.locator('[data-grid-points] tr').count() >= 12);
      assert.match(await page.locator('[data-untimed]').innerText(), /unknown_zone: 2/);
      assert.match(await page.locator('[data-filtered]').innerText(), /hidden_by_projection: 1/);
      const method = await page.locator('[data-method]').innerText();
      assert.match(method, /display_time@1/);
      assert.match(method, /corpus-activity-1/);
      const button = page.locator(`[data-budget-table] button[data-bucket="2025-03-27"][data-category="praca_projekty"]`);
      await button.click();
      await page.waitForFunction(() => document.querySelectorAll('[data-candle-table] tr[data-event-id]').length === 20);
    });

    await test('new run from the form is stored and selected; flip window is flagged unstable', async () => {
      await page.locator('details.ab-new summary').click();
      await page.fill('[data-new-run] input[name=from]', '2025-04-03');
      await page.fill('[data-new-run] input[name=to]', '2025-04-04');
      await page.fill('[data-new-run] textarea[name=params]', '{"granularities": ["day"]}');
      await page.click('[data-new-run] button[type=submit]');
      await page.waitForFunction(() => document.querySelectorAll('[data-runs] option').length === 3);
      const newId = Number(await page.locator('[data-runs]').inputValue());
      await ready(newId);
      assert.ok(newId > secondRun);
      assert.match(await page.locator('[data-status]').innerText(), /zapisany/);
      assert.equal(await page.locator('[data-unstable]').getAttribute('data-unstable'), 'true');
      assert.match(await page.locator('[data-flips]').innerText(), /ordering: Praca \/ projekty vs Zdrowie/);
      const bad = page.locator('[data-new-run] textarea[name=params]');
      await bad.fill('{"overlap_rule": "sum"}');
      await page.click('[data-new-run] button[type=submit]');
      await page.waitForFunction(() => /Błąd: .*overlap_rule/.test(document.querySelector('[data-status]').innerText));
    });

    await test('phone width keeps the chart inside the viewport', async () => {
      await page.setViewportSize({width: 390, height: 800});
      await page.waitForTimeout(300);
      const chart = await page.locator('[data-chart]').boundingBox();
      assert.ok(chart.width <= 390);
      receipt.measurements.phone_document_scroll_width = await page.evaluate(() => document.documentElement.scrollWidth);
    });

    await test('no page errors or console errors', async () => {
      assert.deepEqual(pageErrors, []);
      receipt.measurements.failed_responses = failedResponses;
      // Expected: the deliberate invalid-parameter submission (422) and the browser's implicit favicon request (404).
      assert.deepEqual(failedResponses.filter(r => r !== '422 /api/analysis/budget/runs' && r !== '404 /favicon.ico'), []);
      assert.equal(consoleErrors.filter(t => !/Failed to load resource/.test(t)).length, 0, consoleErrors.join('\n'));
    });
  } catch (error) {
    failed = true;
    receipt.error = String(error && error.stack || error);
    console.error(error);
  } finally {
    if (browser) await browser.close();
    server.kill('SIGTERM');
    receipt.finished_at = new Date().toISOString();
    receipt.status = failed ? 'failed' : 'passed';
    if (process.env.AGEDS_BROWSER_RECEIPT) fs.writeFileSync(process.env.AGEDS_BROWSER_RECEIPT, JSON.stringify(receipt, null, 2) + '\n');
    process.exit(failed ? 1 : 0);
  }
})();
