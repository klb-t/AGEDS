/** Real Chromium check of the ledger page (/ledger, task AGEDS-TASK-20261017-L3): render, masking, escaping, filter, mobile width. */
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
  const server = spawn(process.env.PYTHON || 'python', ['-m', 'server.tests.browser.ledger_fixture'],
    {cwd: repo, env: {...process.env, AGEDS_BROWSER_PORT: String(port)}, stdio: ['ignore', 'pipe', 'pipe']});
  let stderr = ''; server.stderr.on('data', d => stderr += d);
  let browser;
  const receipt = {git_head: execFileSync('git', ['rev-parse', 'HEAD'], {cwd: repo, encoding: 'utf8'}).trim(),
    playwright: require('playwright/package.json').version, task: 'AGEDS-TASK-20261017-L3',
    started_at: new Date().toISOString(),
    fixture: 'generated ledger corpus (bank CSV, CAMT.053, MT940, transit taps, Apple Health with opt-in) + markup-like bank row; temporary SQLite',
    cases: []};
  try {
    const fixture = await new Promise((resolve, reject) => {
      let buf = ''; server.stdout.on('data', d => {buf += d; if (buf.includes('\n')) {try {resolve(JSON.parse(buf.split('\n')[0]));} catch (e) {reject(e);}}});
      server.on('exit', code => reject(new Error(`fixture exited ${code}: ${stderr}`)));
    });
    const base = `http://127.0.0.1:${port}`;
    for (let i = 0; i < 200; i++) {try {if ((await fetch(base + '/ledger')).ok) break;} catch {} await delay(50);}
    browser = await chromium.launch({headless: true, executablePath: process.env.AGEDS_BROWSER_EXECUTABLE || undefined, args: ['--no-sandbox']});
    receipt.browser = browser.version();
    const page = await browser.newPage({locale: 'pl-PL'});
    const errors = []; let dialogs = 0; const failures = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text()); });
    page.on('dialog', d => { dialogs += 1; d.dismiss(); });
    page.on('response', r => { if (r.status() >= 400) failures.push(`${r.status()} ${new URL(r.url()).pathname}`); });
    async function test(name, fn) {await fn(); receipt.cases.push({name, status: 'passed'}); console.log(`PASS ${name}`);}
    const noExecution = async () => {
      assert.equal(await page.evaluate(() => window.__ageds_ledger_xss), undefined);
      assert.equal(await page.locator('main img, td img').count(), 0);
      assert.equal(await page.locator('main script').count(), 0);
    };

    await test('ledger page renders statements, balance checks, totals and sessions', async () => {
      const response = await page.goto(`${base}/ledger`);
      assert.equal(response.status(), 200);
      assert.equal(await page.locator('nav.ageds-nav a[href="/ledger"]').textContent(), 'Rejestry');
      assert.ok(await page.locator('.ledger-check-mismatch').count() >= 1);
      assert.ok(await page.locator('.ledger-check-match').count() >= 1);
      const text = await page.locator('main').textContent();
      assert.ok(text.includes('-1234.56') && text.includes('PLN'), 'exact amount shown');
      assert.ok(text.includes('provider_inference'), 'session basis shown');
    });

    await test('account numbers are masked for display by default', async () => {
      await page.goto(`${base}/ledger`);
      assert.ok(!(await page.locator('main').textContent()).includes(fixture.iban));
      await page.goto(`${base}/ledger?mask=0`);
      assert.ok((await page.locator('main').textContent()).includes(fixture.iban));
    });

    await test('source text with markup renders as text', async () => {
      await page.goto(`${base}/ledger?mask=0`);
      const text = await page.locator('main').textContent();
      assert.ok(text.includes(fixture.literal), 'markup-like counterparty shown literally');
      assert.ok(text.includes('<script>window.__ageds_ledger_xss=2</script>'));
      await noExecution();
    });

    await test('kind filter narrows entries', async () => {
      await page.goto(`${base}/ledger`);
      await page.locator('form.ledger-filter select[name=kind]').selectOption('travel_tap');
      await Promise.all([page.waitForURL(/kind=travel_tap/), page.locator('form.ledger-filter button').click()]);
      const kinds = await page.locator('section[aria-labelledby=ledger-entries] tbody tr td:nth-child(2)').allTextContents();
      assert.ok(kinds.length > 0 && kinds.every(k => k === 'travel_tap'), kinds.join(','));
    });

    await test('mobile width: tables scroll inside their panels, the page does not overflow', async () => {
      await page.setViewportSize({width: 390, height: 844});
      await page.goto(`${base}/ledger`);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      receipt.mobile_overflow_px = overflow;
      assert.ok(overflow <= 1, `horizontal overflow ${overflow}px`);
    });

    await test('no page errors, console errors, dialogs or failed requests', async () => {
      assert.deepEqual(errors, []);
      assert.equal(dialogs, 0);
      assert.deepEqual(failures.filter(f => f !== '404 /favicon.ico'), []);
    });
    receipt.finished_at = new Date().toISOString();
    receipt.result = 'passed';
    if (process.env.AGEDS_BROWSER_RECEIPT) fs.writeFileSync(process.env.AGEDS_BROWSER_RECEIPT, JSON.stringify(receipt, null, 2) + '\n');
  } finally {
    if (browser) await browser.close();
    server.kill('SIGTERM');
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
