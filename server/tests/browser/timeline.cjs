/** Real Chromium acceptance of the timeline view against an isolated synthetic SQLite fixture.
 *
 * Env: PYTHON, AGEDS_BROWSER_EXECUTABLE, NODE_PATH (playwright), optional
 * AGEDS_TIMELINE_BULK (extra synthetic events; e.g. 200000 for the measured run)
 * and AGEDS_BROWSER_RECEIPT (JSON receipt path).
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
const bulk = Number(process.env.AGEDS_TIMELINE_BULK || 5000);

(async () => {
  const socket = net.createServer(); socket.listen(0, '127.0.0.1'); await once(socket, 'listening');
  const port = socket.address().port; await new Promise(r => socket.close(r));
  const fixtureStarted = Date.now();
  const server = spawn(process.env.PYTHON || 'python', ['-m', 'server.tests.timeline_browser_fixture'], {
    cwd: repo, env: {...process.env, AGEDS_BROWSER_PORT: String(port), AGEDS_TIMELINE_BULK: String(bulk)}, stdio: ['ignore', 'pipe', 'pipe']});
  let stderr = ''; server.stderr.on('data', d => stderr += d);
  let browser;
  const receipt = {git_head: execFileSync('git', ['rev-parse', 'HEAD'], {cwd: repo, encoding: 'utf8'}).trim(),
    task: 'AGEDS-TASK-20261016-C', playwright: require('playwright/package.json').version, bulk_events: bulk,
    started_at: new Date().toISOString(), cases: [], measurements: {}};
  try {
    const fixture = await new Promise((resolve, reject) => {
      let buf = ''; server.stdout.on('data', d => {buf += d; if (buf.includes('\n')) {try {resolve(JSON.parse(buf.split('\n')[0]));} catch (e) {reject(e);}}});
      server.on('exit', code => reject(new Error(`fixture exited ${code}: ${stderr}`)));
    });
    receipt.measurements.fixture_seconds = (Date.now() - fixtureStarted) / 1000;
    receipt.fixture = {bulk: fixture.bulk ?? null};
    const base = `http://127.0.0.1:${port}`;
    for (let i = 0; i < 200; i++) {try {if ((await fetch(base + '/api/timeline/rules')).ok) break;} catch {} await delay(50);}
    // Server-side timings of the first (cold) projection build and of warm queries.
    let t = Date.now();
    const projection = await (await fetch(base + '/api/timeline/projection')).json();
    receipt.measurements.projection_first_request_ms = Date.now() - t;
    receipt.measurements.projection_build = projection.build;
    const facets = await (await fetch(base + '/api/timeline/facets')).json();
    const timings = {};
    for (const [name, url] of [
      ['density_full_extent_auto', `/api/timeline/density?bucket=auto&max_buckets=300`],
      ['density_month_by_type', `/api/timeline/density?bucket=month&group=type&max_buckets=400`],
      ['window_one_day_1000', `/api/timeline/window?from=2023-03-01&to=2023-03-02&limit=1000&candidates=false`],
      ['window_one_day_with_candidates_200', `/api/timeline/window?from=2023-03-01&to=2023-03-02&limit=200`],
      ['window_full_first_page_1000', `/api/timeline/window?limit=1000&candidates=false`],
      ['legacy_api_timeline_500', `/api/timeline?limit=500`],
    ]) {
      t = performance.now(); const response = await fetch(base + url); await response.arrayBuffer();
      assert.equal(response.status, 200, url); timings[name] = Math.round(performance.now() - t);
    }
    receipt.measurements.http_ms_warm = timings;
    receipt.measurements.dated_items = facets.extent.dated_items;

    browser = await chromium.launch({headless: true, executablePath: process.env.AGEDS_BROWSER_EXECUTABLE || undefined, args: ['--no-sandbox']});
    receipt.browser = browser.version();
    const page = await browser.newPage({viewport: {width: 1500, height: 1000}});
    const pageErrors = []; page.on('pageerror', e => pageErrors.push(e.message));
    const consoleErrors = []; page.on('console', m => {if (m.type() === 'error') consoleErrors.push(m.text());});
    const root = page.locator('[data-timeline]');
    const ready = async previous => page.waitForFunction(prev => {
      const node = document.querySelector('[data-timeline]');
      return node.dataset.state === 'ready' && node.dataset.renderSeq !== prev;
    }, previous ?? null, {timeout: 60000});
    const seq = async () => (await root.getAttribute('data-render-seq'));
    const shots = process.env.AGEDS_TIMELINE_SCREENSHOTS;
    async function shot(name) {if (shots) await page.screenshot({path: path.join(shots, `${name}.png`), fullPage: false});}
    async function test(name, fn) {const started = Date.now(); await fn(); receipt.cases.push({name, status: 'passed', ms: Date.now() - started}); console.log(`PASS ${name}`);}

    await test('page loads, lanes render, no page errors', async () => {
      t = Date.now();
      await page.goto(`${base}/timeline`);
      await ready();
      receipt.measurements.initial_render_ms = Date.now() - t;
      receipt.measurements.initial_client_render_ms = Number(await root.getAttribute('data-render-ms'));
      assert.ok(await page.locator('.tl-lane').count() > 0);
      assert.ok(await page.locator('.ageds-nav a[href="/timeline"]').count() === 1);
      receipt.measurements.initial_mode = await root.getAttribute('data-mode');
      receipt.measurements.initial_bucket = await root.getAttribute('data-bucket');
      receipt.measurements.initial_rendered_nodes = Number(await root.getAttribute('data-rendered-nodes'));
      assert.equal(receipt.measurements.initial_mode, 'density');
      await shot('initial-density');
      assert.deepEqual(pageErrors, []);
    });

    await test('zoom changes level from density to items', async () => {
      const before = await root.getAttribute('data-bucket');
      let previous = await seq();
      await page.locator('[data-lanes]').focus();
      // Zoom in from years toward one day around a known synthetic day.
      await page.evaluate(() => {});
      const levels = [];
      for (let i = 0; i < 30; i++) {
        await page.keyboard.press('+');
        await ready(previous); previous = await seq();
        levels.push(`${await root.getAttribute('data-mode')}:${await root.getAttribute('data-bucket')}`);
        if ((await root.getAttribute('data-mode')) === 'items') break;
      }
      receipt.measurements.zoom_levels = levels;
      assert.notEqual(await root.getAttribute('data-bucket'), before);
      assert.equal(await root.getAttribute('data-mode'), 'items');
      receipt.measurements.items_mode_render_ms = Number(await root.getAttribute('data-render-ms'));
      receipt.measurements.items_mode_rendered_nodes = Number(await root.getAttribute('data-rendered-nodes'));
      await page.keyboard.press('-'); await ready(previous); previous = await seq();
      await page.keyboard.press('ArrowRight'); await ready(previous);
    });

    await test('parallel chatbot conversations as side-by-side lanes', async () => {
      const previous = await seq();
      await page.selectOption('[data-group]', 'chatbot');
      await ready(previous);
      // Jump to the hand-made day with two overlapping chatbot conversations.
      const p2 = await seq();
      await page.evaluate(() => {
        const form = document.querySelector('[data-filters]');
        form.querySelector('[name=q]').value = 'wiadomość';
        form.requestSubmit();
      });
      await ready(p2);
      const p3 = await seq();
      await page.locator('[data-action="fit"]').click();
      await ready(p3);
      const labels = await page.locator('.tl-lane-label span').allTextContents();
      assert.ok(labels.includes('chatgpt') && labels.includes('claude'), labels.join(','));
      const visibleMarkers = await page.locator('.tl-item').count();
      // Hidden chat parts appear only on request.
      const p4 = await seq();
      await page.locator('[name=include_hidden]').check();
      await page.evaluate(() => document.querySelector('[data-filters]').requestSubmit());
      await ready(p4);
      assert.equal(await page.locator('.tl-item').count(), visibleMarkers + 1);
      receipt.hidden_parts_toggle = {visible: visibleMarkers, with_hidden: visibleMarkers + 1};
      const p5 = await seq();
      await page.locator('[name=include_hidden]').uncheck();
      await page.evaluate(() => document.querySelector('[data-filters]').requestSubmit());
      await ready(p5);
      await shot('chatbot-lanes');
    });

    await test('click shows all time candidates; source HTML stays text', async () => {
      // Clear the text query; show the hand-made day where items are drawn.
      const p1 = await seq();
      await page.evaluate(() => {
        const form = document.querySelector('[data-filters]');
        form.querySelector('[name=q]').value = '';
        form.querySelector('[data-group]').value = 'source';
        form.requestSubmit();
      });
      await ready(p1);
      const p3 = await seq();
      await page.evaluate(() => {
        const form = document.querySelector('[data-filters]');
        form.querySelector('[name=q]').value = '"sms img"';
        form.requestSubmit();
      });
      await ready(p3);
      const p4 = await seq();
      await page.locator('[data-action="fit"]').click();
      await ready(p4);
      await page.locator('.tl-item').first().click();
      await page.waitForFunction(() => document.querySelector('[data-timeline]').dataset.detailKey?.startsWith('event:'));
      const rows = await page.locator('[data-candidates] [data-candidate]').count();
      assert.ok(rows >= 2, `candidates: ${rows}`);
      assert.equal(await page.locator('[data-candidates] .tl-selected').count(), 1);
      const text = await page.locator('[data-detail-text]').textContent();
      assert.ok(text.includes(fixture.literal));
      assert.equal(await page.locator('img').count(), 0);
      assert.equal(await page.evaluate(() => window.__timelineSourceExecuted), undefined);
      receipt.detail_candidates = rows;
      await shot('item-detail-candidates');
    });

    await test('range selection offers a report link to /reports/new and export of range', async () => {
      const box = await page.locator('[data-ruler]').boundingBox();
      await page.mouse.move(box.x + 400, box.y + box.height / 2);
      await page.mouse.down();
      await page.mouse.move(box.x + 700, box.y + box.height / 2, {steps: 5});
      await page.mouse.up();
      await page.waitForFunction(() => !document.querySelector('[data-range-panel]').hidden);
      const link = page.locator('[data-report-link]');
      // The reports area is integrated, so the range link is enabled.
      assert.notEqual(await link.getAttribute('aria-disabled'), 'true');
      assert.match(await link.getAttribute('href'), /^\/reports\/new\?from=.*&to=/);
      const csv = await page.locator('[data-range-export="csv"]').getAttribute('href');
      assert.match(csv, /from=.*&to=/);
      const response = await fetch(base + csv);
      assert.equal(response.status, 200);
      assert.match(response.headers.get('content-disposition'), /attachment/);
      const previous = await seq();
      await page.locator('[data-action="zoom-selection"]').click();
      await ready(previous);
    });

    await test('undated items listed separately', async () => {
      const previous = await seq();
      await page.evaluate(() => {
        const form = document.querySelector('[data-filters]');
        form.querySelector('[name=q]').value = '';
        form.requestSubmit();
      });
      await ready(previous);
      await shot('density-or-items-after-range');
      await page.locator('[data-undated-button]').click();
      await page.waitForFunction(() => document.querySelectorAll('[data-undated] li').length > 0);
      const text = await page.locator('[data-undated]').textContent();
      assert.match(text, /nie są sortowane według czasu importu/);
      assert.equal(await page.locator('img').count(), 0);
    });

    await test('transcript segment detail shows inherited recording candidates and version', async () => {
      const previous = await seq();
      await page.evaluate(() => {
        const form = document.querySelector('[data-filters]');
        form.querySelector('[name=q]').value = '';
        form.querySelector('[name=kind]').value = 'transcript_segment';
        form.requestSubmit();
      });
      await ready(previous);
      const p2 = await seq();
      await page.locator('[data-action="fit"]').click();
      await ready(p2);
      await page.locator('.tl-item').first().click();
      await page.waitForFunction(() => document.querySelector('[data-timeline]').dataset.detailKey?.startsWith('segment:'));
      const detail = await page.locator('[data-detail]').textContent();
      assert.match(detail, /Wersja transkrypcji/);
      assert.ok(await page.locator('[data-candidates] [data-candidate^="artifact_time:"]').count() >= 1);
      assert.equal(await page.evaluate(() => window.__timelineSourceExecuted), undefined);
      await shot('segment-detail');
    });

    assert.deepEqual(pageErrors, []);
    receipt.page_errors = pageErrors;
    receipt.console_errors = consoleErrors;
    receipt.status = 'passed';
  } catch (error) {
    receipt.status = 'failed'; receipt.error = error.stack; process.exitCode = 1; console.error(error);
  } finally {
    if (browser) await browser.close();
    server.kill('SIGTERM');
    receipt.finished_at = new Date().toISOString();
    if (process.env.AGEDS_BROWSER_RECEIPT) fs.writeFileSync(process.env.AGEDS_BROWSER_RECEIPT, JSON.stringify(receipt, null, 2) + '\n');
    else console.log(JSON.stringify(receipt.measurements, null, 1));
  }
})();
