/** Real Chromium: the owner's path without the CLI — settings, upload, plan, originals, ASR/OCR, report.
 *  Synthetic files and a test-only fake Vertex transport (server/tests/browser_owner_fixture.py). */
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
  const server = spawn(process.env.PYTHON || 'python', ['-m', 'server.tests.browser_owner_fixture'],
    {cwd: repo, env: {...process.env, AGEDS_BROWSER_PORT: String(port)}, stdio: ['ignore', 'pipe', 'pipe']});
  let stderr = ''; server.stderr.on('data', d => stderr += d);
  const receipt = {git_head: execFileSync('git', ['rev-parse', 'HEAD'], {cwd: repo, encoding: 'utf8'}).trim(),
    task: 'AGEDS-TASK-20261027-ZBIERANIE-1 owner web path',
    fixture: 'synthetic WhatsApp zip, WAV and PNG; fake Vertex transport; temporary SQLite; legacy_open on loopback',
    started_at: new Date().toISOString(), cases: []};
  let browser;
  try {
    const fixture = await new Promise((resolve, reject) => {
      let buf = ''; server.stdout.on('data', d => {buf += d; if (buf.includes('\n')) {try {resolve(JSON.parse(buf.split('\n')[0]));} catch (e) {reject(e);}}});
      server.on('exit', code => reject(new Error(`fixture exited ${code}: ${stderr}`)));
    });
    const base = `http://127.0.0.1:${port}`;
    for (let i = 0; i < 200; i++) {try {if ((await fetch(`${base}/start`)).ok) break;} catch {} await delay(50);}
    browser = await chromium.launch({headless: true, executablePath: process.env.AGEDS_BROWSER_EXECUTABLE || undefined, args: ['--no-sandbox']});
    receipt.browser = browser.version();
    const page = await (await browser.newContext()).newPage();
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    async function test(name, fn) {await fn(); receipt.cases.push({name, status: 'passed'}); console.log(`PASS ${name}`);}
    async function submit(locator) {await Promise.all([page.waitForNavigation(), locator.click()]);}

    await test('start page states the limits before anything is uploaded and links the settings', async () => {
      await page.goto(`${base}/start`);
      assert.equal(await page.locator('nav.ageds-nav a[href="/start"]').count(), 1);
      const limits = await page.textContent('.owner-limits');
      assert.match(limits, /daty wgrania/);
      assert.match(limits, /wymagają zgody/);
      assert.equal(await page.locator('[data-owner-vertex-missing]').count(), 1);
    });

    await test('settings: consents, provider, budget and presets without the CLI', async () => {
      await page.goto(`${base}/settings`);
      const form = page.locator('[data-owner-consent-form]');
      await form.locator('select[name=provider]').selectOption('vertex');
      await form.locator('input[name=basis]').fill('test przeglądarkowy (dane syntetyczne)');
      await submit(form.locator('button'));
      assert.equal(await page.locator('[data-consent="vertex"][data-consent-state="active"]').count(), 1);
      await submit(page.locator('form[action="/settings/provider"] button'));
      assert.match(await page.textContent('[data-owner-provider]'), /Vertex/);
      await page.fill('[data-owner-budget-form] input[name=eur]', '0,5');
      await submit(page.locator('[data-owner-budget-form] button'));
      assert.match(await page.textContent('[data-owner-budget]'), /0\.5/);
      await page.check('input[name=asr][value="asr-vertex-gemini"]');
      await page.check('input[name=ocr][value="ocr-vertex-gemini"]');
      await submit(page.locator('[data-owner-recipes-form] button'));
      assert.equal(await page.isChecked('input[name=asr][value="asr-vertex-gemini"]'), true);
    });

    let batchUrl;
    await test('upload of an export zip, a recording and a screenshot creates a reviewed plan', async () => {
      await page.goto(`${base}/start`);
      assert.equal(await page.locator('[data-owner-vertex-missing]').count(), 0);
      await page.setInputFiles('[data-owner-files]', Object.values(fixture.files));
      await page.fill('input[name=label]', 'paczka testowa');
      await submit(page.locator('[data-owner-upload] button'));
      batchUrl = page.url().split('?')[0];
      assert.match(batchUrl, /\/start\/batch\/\d+$/);
      assert.equal(await page.locator('[data-owner-done="uploaded"]').count(), 1);
      assert.equal(await page.locator('[data-owner-files-table] tr').count(), 4);
      assert.notEqual(await page.textContent('[data-owner-files-table]'), '');
      assert.match(await page.textContent('[data-owner-plan]'), /1 jednostek/);
    });

    await test('the plan is approved and run on the import page', async () => {
      await submit(page.locator('[data-owner-plan-link]'));
      await Promise.all([page.waitForEvent('load'), page.click('button[value="approve-ready"]')]);
      await Promise.all([page.waitForEvent('load'), page.click('form[data-ip-action="run"] button')]);
      await page.waitForSelector('.ip-runs .ip-run-completed');
    });

    await test('remaining files are kept as originals and ASR/OCR is queued and finished by the worker', async () => {
      await page.goto(batchUrl);
      await submit(page.locator('[data-owner-keep]'));
      assert.match(await page.getAttribute('[data-owner-done]', 'data-owner-done'), /^kept:\d+$/);
      await submit(page.locator('[data-owner-process]'));
      assert.match(await page.getAttribute('[data-owner-done]', 'data-owner-done'), /^queued:[1-9]/);
      for (let i = 0; i < 60; i++) {
        await page.goto(batchUrl);
        const open = await page.locator('[data-job-status="queued"], [data-job-status="running"]').count();
        if (!open) break;
        await delay(500);
      }
      assert.equal(await page.locator('[data-job-status="failed"]').count(), 0, await page.textContent('[data-owner-artifacts]'));
      assert.ok(await page.locator('[data-text-kind="transcript"]').count() >= 1);
      assert.ok(await page.locator('[data-text-kind="ocr"]').count() >= 1);
    });

    await test('the report page shows originals, labelled model output, chat coverage and costs', async () => {
      await submit(page.locator('[data-owner-report-link]'));
      assert.match(await page.textContent('.owner-layers'), /wynik modelu/);
      const text = await page.textContent('main');
      assert.match(text, /Syntetyczna rozmowa testowa/);
      assert.match(text, /whatsapp_message/);
      assert.match(text, /Koszty dostawcy: \d+ wywołań/);
      assert.ok(await page.locator('[data-report-gaps] li').count() >= 1);
    });

    await test('new pages have no horizontal scroll at phone width and no script errors', async () => {
      await page.setViewportSize({width: 390, height: 844});
      for (const url of [`${base}/start`, batchUrl, `${base}/report`, `${base}/settings`]) {
        await page.goto(url);
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
        assert.ok(overflow <= 1, `${url} overflows by ${overflow}px`);
      }
      assert.deepEqual(errors, []);
    });
    receipt.finished_at = new Date().toISOString();
    if (process.env.AGEDS_BROWSER_RECEIPT) require('node:fs').writeFileSync(process.env.AGEDS_BROWSER_RECEIPT, JSON.stringify(receipt, null, 1));
  } catch (error) {
    console.error(error); console.error(stderr.slice(-3000)); process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
    server.kill('SIGTERM');
  }
})();
