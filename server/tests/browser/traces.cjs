/** Real Chromium check of the traces page (/traces, NAV "Ślady"): render, counts, privacy, escaping, no page errors. */
const {chromium} = require('playwright');
const {spawn, execFileSync} = require('node:child_process');
const {once} = require('node:events');
const net = require('node:net');
const path = require('node:path');
const assert = require('node:assert/strict');
const repo = path.resolve(__dirname, '../../..');
const delay = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const socket = net.createServer(); socket.listen(0, '127.0.0.1'); await once(socket, 'listening');
  const port = socket.address().port; await new Promise(r => socket.close(r));
  const server = spawn(process.env.PYTHON || 'python', ['-m', 'server.tests.browser.traces_fixture'],
    {cwd: repo, env: {...process.env, AGEDS_BROWSER_PORT: String(port)}, stdio: ['ignore', 'pipe', 'pipe']});
  let stderr = ''; server.stderr.on('data', d => stderr += d);
  let browser; let failed = false;
  const receipt = {git_head: execFileSync('git', ['rev-parse', 'HEAD'], {cwd: repo, encoding: 'utf8'}).trim(),
    task: 'AGEDS-TASK-20261017-L2', started_at: new Date().toISOString(), cases: []};
  try {
    const fixture = await new Promise((resolve, reject) => {
      let buf = ''; server.stdout.on('data', d => {buf += d; if (buf.includes('\n')) {try {resolve(JSON.parse(buf.split('\n')[0]));} catch (e) {reject(e);}}});
      server.on('exit', code => reject(new Error(`fixture exited ${code}: ${stderr}`)));
    });
    const base = `http://127.0.0.1:${port}`;
    for (let i = 0; i < 200; i++) {try {if ((await fetch(base + '/traces')).ok) break;} catch {} await delay(50);}
    browser = await chromium.launch({headless: true, executablePath: process.env.AGEDS_BROWSER_EXECUTABLE || undefined, args: ['--no-sandbox']});
    receipt.browser = browser.version();
    const page = await browser.newPage({locale: 'pl-PL'});
    const errors = []; const failures = []; let dialogs = 0;
    page.on('pageerror', e => errors.push(e.message));
    page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text()); });
    page.on('dialog', d => { dialogs += 1; d.dismiss(); });
    page.on('response', r => { if (r.status() >= 400) failures.push(`${r.status()} ${new URL(r.url()).pathname}`); });
    async function test(name, fn) {await fn(); receipt.cases.push({name, status: 'passed'}); console.log(`PASS ${name}`);}

    await test('navigation shows Ślady and the page renders', async () => {
      const response = await page.goto(`${base}/`);
      assert.equal(response.status(), 200);
      const link = page.locator('nav.ageds-nav a[href="/traces"]');
      assert.equal(await link.textContent(), 'Ślady');
      await Promise.all([page.waitForURL(/\/traces$/), link.click()]);
      assert.equal(await page.locator('h1').textContent(), 'Ślady cyfrowe');
    });

    await test('event, geotag, match and document counts are listed', async () => {
      const events = await page.locator('table[data-traces-events]').textContent();
      for (const type of ['photo_metadata', 'chat_message', 'document_metadata', 'note', 'image_capture']) assert.ok(events.includes(type), type);
      const geotags = await page.locator('table[data-traces-geotags]').textContent();
      assert.ok(geotags.includes('present_not_stored'), 'without the opt-in geotags are presence only');
      const body = await page.locator('main').textContent();
      assert.ok(body.includes('single_candidate') && body.includes('ambiguous'));
      assert.ok(body.includes('facebook_messenger') && body.includes('telegram'));
    });

    await test('runs show options as text; markup-like labels do not execute', async () => {
      const runs = await page.locator('table[data-traces-runs]').textContent();
      assert.ok(runs.includes('facebook_messages') && runs.includes('google_photos_sidecar'));
      assert.ok(runs.includes('img src=x'), 'export label rendered literally');
      assert.equal(await page.evaluate(() => window.__ageds_traces_xss), undefined);
      assert.equal(await page.locator('main img').count(), 0);
    });

    await test('third-party names and coordinates are not on the page', async () => {
      const html = await page.content();
      assert.ok(!html.includes(fixture.people));
      assert.ok(!html.includes('30.25') && !html.includes('-30.25'));
    });

    await test('no page errors, dialogs or failed requests', async () => {
      assert.deepEqual(errors, []); assert.equal(dialogs, 0); assert.deepEqual(failures, []);
    });
  } catch (error) {
    failed = true; console.error(error); console.error(stderr);
  } finally {
    if (browser) await browser.close();
    server.kill('SIGTERM');
    receipt.finished_at = new Date().toISOString();
    console.log(JSON.stringify(receipt));
    process.exit(failed ? 1 : 0);
  }
})();
