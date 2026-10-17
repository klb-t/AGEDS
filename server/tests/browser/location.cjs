/** Real Chromium acceptance of /location against an isolated synthetic SQLite fixture (task L1).
 *
 * Fixture: `python -m server.tests.corpus.location` (generated corpus with fictional coordinates near 0°N 0°E,
 * temporary database, Records.json + Semantic Location History + GPX imported, one published presence run with a
 * hostile place label). Env: PYTHON, AGEDS_BROWSER_EXECUTABLE, NODE_PATH (playwright),
 * optional AGEDS_BROWSER_RECEIPT (JSON receipt path).
 *
 * Asserts that by default the page makes NO request outside the local server, that data text is never parsed as
 * HTML, and that map tiles are requested only after a recorded opt-in (tile requests are intercepted locally).
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
const PNG = Buffer.from('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360f8cfc0f01f0005fe02fea7d6a4a80000000049454e44ae426082', 'hex');

(async () => {
  const socket = net.createServer(); socket.listen(0, '127.0.0.1'); await once(socket, 'listening');
  const port = socket.address().port; await new Promise(r => socket.close(r));
  const server = spawn(process.env.PYTHON || 'python', ['-m', 'server.tests.corpus.location'], {
    cwd: repo, env: {...process.env, AGEDS_BROWSER_PORT: String(port)}, stdio: ['ignore', 'pipe', 'pipe']});
  let stderr = ''; server.stderr.on('data', d => stderr += d);
  let browser;
  const receipt = {git_head: execFileSync('git', ['rev-parse', 'HEAD'], {cwd: repo, encoding: 'utf8'}).trim(),
    task: 'AGEDS-TASK-20261017-L1 (location map)', playwright: require('playwright/package.json').version,
    started_at: new Date().toISOString(), cases: [], measurements: {}};
  let failed = false;
  try {
    const fixture = await new Promise((resolve, reject) => {
      let buf = ''; server.stdout.on('data', d => {buf += d; if (buf.includes('\n')) {try {resolve(JSON.parse(buf.split('\n')[0]));} catch (e) {reject(e);}}});
      server.on('exit', code => reject(new Error(`fixture exited ${code}: ${stderr}`)));
    });
    const base = `http://127.0.0.1:${port}`;
    for (let i = 0; i < 300; i++) {try {if ((await fetch(base + '/api/location/summary')).ok) break;} catch {} await delay(50);}
    browser = await chromium.launch({headless: true, executablePath: process.env.AGEDS_BROWSER_EXECUTABLE || undefined, args: ['--no-sandbox']});
    receipt.browser = browser.version();
    const context = await browser.newContext({viewport: {width: 1400, height: 1000}});
    const page = await context.newPage();
    const pageErrors = [], consoleErrors = [], requests = [], tileRequests = [];
    page.on('pageerror', e => pageErrors.push(e.message));
    page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text()); });
    page.on('request', r => requests.push(r.url()));
    await context.route('https://tiles.example.org/**', route => { tileRequests.push(route.request().url());
      route.fulfill({status: 200, contentType: 'image/png', body: PNG}); });
    async function test(name, fn) {
      const started = Date.now();
      await fn();
      receipt.cases.push({name, status: 'passed', ms: Date.now() - started});
      console.log(`PASS ${name}`);
    }
    const ready = () => page.waitForFunction(() => document.querySelector('[data-location]')?.dataset.state === 'ready');
    const external = () => requests.filter(u => !u.startsWith(base) && !u.startsWith('data:'));

    await test('page renders map, places, presence table and lanes without external requests', async () => {
      const t = Date.now();
      const response = await page.goto(base + '/location?from=2024-01-15T00:00:00Z&to=2024-01-17T00:00:00Z');
      await ready();
      receipt.measurements.first_render_ms = Date.now() - t;
      assert.match(response.headers()['content-security-policy'], /img-src 'self' data:;/);
      assert.ok(await page.locator('[data-loc-map] [data-fix]').count() > 20);
      assert.ok(await page.locator('[data-loc-map] [data-place]').count() >= 3);
      assert.ok(await page.locator('[data-loc-map] polyline').count() >= 1);
      assert.ok(await page.locator('[data-loc-map] [data-event-id]').count() >= 1, 'provider visits drawn');
      assert.equal(await page.locator('[data-loc-presence] tbody tr[data-interval]').count(), fixture.intervals);
      assert.ok(await page.locator('[data-loc-lanes] [data-lane="presence"] [data-lane-item]').count() >= 1);
      assert.ok(await page.locator('[data-loc-lanes] [data-lane="visits"] [data-lane-item]').count() >= 1);
      assert.match(await page.locator('header').innerText(), /nie dowód obecności osoby/);
      assert.match(await page.locator('[data-loc-tile-warning]').innerText(), /ujawniają/);
      assert.deepEqual(external(), []);
      assert.deepEqual(tileRequests, []);
      assert.equal(await page.locator('[data-loc-map] image').count(), 0);
    });

    await test('hostile place label is shown as text, never parsed as HTML', async () => {
      const labels = await page.locator('[data-loc-map] .loc-place-label').allTextContents();
      assert.ok(labels.includes(fixture.hostile_label), labels.join(' | '));
      assert.equal(await page.locator('[data-loc-map] img, [data-loc-presence] img').count(), 0);
      assert.ok((await page.locator('[data-loc-presence] tbody').innerText()).includes('onerror'));
      assert.equal(await page.evaluate(() => window.__locationExecuted), undefined);
    });

    await test('time slider narrows the window and updates the URL and the timeline link', async () => {
      const before = await page.locator('[data-loc-map] [data-fix]').count();
      await page.locator('[data-loc-all]').click();
      await ready();
      await page.locator('[data-loc-slide-end]').fill('300');
      await page.locator('[data-loc-slide-end]').dispatchEvent('change');
      await page.waitForFunction(() => new URL(location.href).searchParams.has('to'));
      await ready();
      const url = new URL(page.url());
      assert.ok(url.searchParams.get('to'));
      const link = await page.locator('[data-loc-timeline]').getAttribute('href');
      assert.match(link, /^\/timeline\?/);
      assert.equal(new URL(link, base).searchParams.get('to'), url.searchParams.get('to'));
      receipt.measurements.fixes_before_after_slider = [before, await page.locator('[data-loc-map] [data-fix]').count()];
    });

    await test('projection switch redraws and lane click shows the event detail', async () => {
      await page.goto(base + '/location?from=2024-01-15T00:00:00Z&to=2024-01-16T00:00:00Z');
      await ready();
      await page.selectOption('[data-loc-projection]', 'mercator');
      await page.waitForFunction(() => /Web Mercator/.test(document.querySelector('[data-loc-map-note]').textContent));
      await ready();
      assert.ok(await page.locator('[data-loc-map] [data-fix]').count() > 10);
      await page.locator('[data-loc-lanes] [data-lane="presence"] [data-lane-item]').first().click();
      assert.match(await page.locator('[data-loc-detail]').innerText(), /place_presence #\d+.*derived/s);
      assert.deepEqual(external(), []);
    });

    await test('tiles need a recorded opt-in; afterwards only the opted-in server is contacted', async () => {
      await page.fill('[data-loc-tile-form] input[name=template]', 'https://tiles.example.org/{z}/{x}/{y}.png');
      await page.click('[data-loc-tile-form] button[type=submit]');  // without acknowledgement: refused
      await page.waitForFunction(() => /Kafelki: .*acknowledge/.test(document.querySelector('[data-loc-status]').textContent));
      assert.deepEqual(tileRequests, []);
      await page.check('[data-loc-tile-form] input[name=acknowledge]');
      await Promise.all([page.waitForNavigation(), page.click('[data-loc-tile-form] button[type=submit]')]);
      assert.match(page.url(), /tile_opt_in=\d+/);
      await ready();
      await page.waitForFunction(() => document.querySelectorAll('[data-loc-map] image[data-tile]').length > 0);
      await page.waitForTimeout(300);
      assert.ok(tileRequests.length > 0);
      assert.ok(tileRequests.every(u => /^https:\/\/tiles\.example\.org\/\d+\/\d+\/\d+\.png$/.test(u)), tileRequests.join('\n'));
      assert.deepEqual(external().filter(u => !u.startsWith('https://tiles.example.org/')), []);
      assert.match(await page.locator('[data-loc-tiles-on]').innerText(), /zgoda #\d+/);
      receipt.measurements.tile_requests_after_opt_in = tileRequests.length;
      const optIns = await (await fetch(base + '/api/location/tiles/opt-ins')).json();
      assert.equal(optIns.opt_ins.length, 1);
    });

    await test('phone width keeps the map inside the viewport', async () => {
      await page.goto(base + '/location');
      await ready();
      await page.setViewportSize({width: 390, height: 800});
      await page.waitForTimeout(300);
      const box = await page.locator('[data-loc-map]').boundingBox();
      assert.ok(box.width <= 390);
      receipt.measurements.phone_document_scroll_width = await page.evaluate(() => document.documentElement.scrollWidth);
    });

    await test('no page errors or console errors', async () => {
      assert.deepEqual(pageErrors, []);
      assert.equal(consoleErrors.filter(t => !/Failed to load resource/.test(t)).length, 0, consoleErrors.join('\n'));
      receipt.measurements.requests_total = requests.length;
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
