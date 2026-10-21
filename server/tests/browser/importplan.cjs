/** Real Chromium acceptance of /import (AGEDS-TASK-20261016-I): isolated synthetic catalog, review, run. */
const {chromium} = require('playwright');
const {spawn, execFileSync} = require('node:child_process');
const {createHash} = require('node:crypto');
const {once} = require('node:events');
const net = require('node:net');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const repo = path.resolve(__dirname, '../../..');
const delay = ms => new Promise(r => setTimeout(r, ms));

function treeHash(root) {
  const hash = createHash('sha256');
  const walk = dir => {
    for (const name of fs.readdirSync(dir).sort()) {
      const full = path.join(dir, name);
      const stat = fs.lstatSync(full);
      if (stat.isDirectory()) walk(full);
      else {
        hash.update(path.relative(root, full));
        hash.update(stat.isSymbolicLink() ? fs.readlinkSync(full) : fs.readFileSync(full));
        hash.update(String(stat.mtimeMs));
      }
    }
  };
  walk(root);
  return hash.digest('hex');
}

(async () => {
  const socket = net.createServer(); socket.listen(0, '127.0.0.1'); await once(socket, 'listening');
  const port = socket.address().port; await new Promise(r => socket.close(r));
  const server = spawn(process.env.PYTHON || 'python', ['-m', 'server.tests.browser.importplan_fixture'],
    {cwd: repo, env: {...process.env, AGEDS_BROWSER_PORT: String(port)}, stdio: ['ignore', 'pipe', 'pipe']});
  let stderr = ''; server.stderr.on('data', d => stderr += d);
  let browser;
  const receipt = {git_head: execFileSync('git', ['rev-parse', 'HEAD'], {cwd: repo, encoding: 'utf8'}).trim(),
    playwright: require('playwright/package.json').version, task: 'AGEDS-TASK-20261016-I', started_at: new Date().toISOString(),
    fixture: 'generated multi-device layout (3 collections, Takeout zip with nested zip, ChatGPT export zip + copy); temporary SQLite',
    cases: []};
  try {
    const fixture = await new Promise((resolve, reject) => {
      let buf = ''; server.stdout.on('data', d => {buf += d; if (buf.includes('\n')) {try {resolve(JSON.parse(buf.split('\n')[0]));} catch (e) {reject(e);}}});
      server.on('exit', code => reject(new Error(`fixture exited ${code}: ${stderr}`)));
    });
    const base = `http://127.0.0.1:${port}`;
    for (let i = 0; i < 200; i++) {try {if ((await fetch(base + '/import')).ok) break;} catch {} await delay(50);}
    const before = treeHash(fixture.corpus);
    browser = await chromium.launch({headless: true, executablePath: process.env.AGEDS_BROWSER_EXECUTABLE || undefined, args: ['--no-sandbox']});
    receipt.browser = browser.version();
    const page = await browser.newPage();
    const errors = []; let dialogs = 0;
    page.on('pageerror', e => errors.push(e.message));
    page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text()); });
    const failures = [];
    page.on('response', r => { if (r.status() >= 400) failures.push(`${r.status()} ${new URL(r.url()).pathname}`); });
    page.on('dialog', d => { dialogs += 1; d.dismiss(); });
    async function test(name, fn) {await fn(); receipt.cases.push({name, status: 'passed'}); console.log(`PASS ${name}`);}
    const row = locator => page.locator('tr.ip-row', {has: page.locator('code.ip-locator', {hasText: locator})}).first();
    const rowStatus = async locator => (await row(locator).locator('td').nth(6).locator('.badge').textContent()).trim();
    async function decide(locator, setup, button = 'approved') {
      const form = row(locator).locator('form[data-ip-action="decide"]');
      if (setup) await setup(form);
      await Promise.all([page.waitForNavigation(), form.locator(`button[value="${button}"]`).click()]);
    }

    await test('landing page lists collections and the Import navigation entry', async () => {
      await page.goto(`${base}/import`);
      assert.equal(await page.title(), 'Plan importu');
      assert.equal(await page.locator('nav.ageds-nav a[href="/import"]').textContent(), 'Import');
      for (const label of Object.values(fixture.labels)) assert.ok((await page.locator('aside').textContent()).includes(label));
    });

    let planUrl;
    await test('generate a plan from three collections', async () => {
      for (const id of Object.values(fixture.collections)) await page.locator(`input[name=collection][value="${id}"]`).check();
      await page.locator('select[name=billingProfileId]').selectOption('');
      await Promise.all([page.waitForURL(/\/import\?plan=\d+$/), page.locator('form[data-ip-action="create"] button').click()]);
      planUrl = page.url();
      assert.equal(await page.locator('tr.ip-row').count(), 24);
      assert.match(await page.locator('ul.ip-plans li.active').textContent(), /24 pozycji · wersja 1/);
      assert.equal(await page.locator('tr.ip-row.is-ambiguous').count(), 1);
      assert.equal(await rowStatus(fixture.ambiguous), 'zaproponowany');
    });

    await test('source names are data: a markup-like locator renders as text and never executes', async () => {
      const code = row(fixture.xssLocator).locator('code.ip-locator');
      assert.equal((await code.textContent()).trim(), fixture.xssLocator);
      assert.equal(await page.evaluate(() => window.__ageds_xss), undefined);
      assert.equal(await page.locator('main img').count(), 0);
      assert.equal(dialogs, 0);
    });

    await test('filters narrow the review table on the server', async () => {
      await page.locator('form.ip-filters input[name=ambiguous]').check();
      await Promise.all([page.waitForNavigation(), page.locator('form.ip-filters button').click()]);
      assert.equal(await page.locator('tr.ip-row').count(), 1);
      await page.goto(`${planUrl}&blocked=true`);
      assert.equal(await page.locator('tr.ip-row').count(), 5);
      await page.goto(`${planUrl}&importer=recording_names`);
      assert.equal(await page.locator('tr.ip-row').count(), 4);
      await page.goto(planUrl);
    });

    await test('ambiguous item needs an explicit importer; opt-in and billing need explicit choices', async () => {
      await row(fixture.ambiguous).locator('form[data-ip-action="decide"] button[value="approved"]').click();
      await page.waitForFunction(() => document.getElementById('importplan-status').textContent.includes('cannot be approved'));
      assert.match(await page.locator('#importplan-status').textContent(), /importer_not_chosen/);
      await page.goto(planUrl);
      await decide(fixture.ambiguous, form => form.locator('select[name=importer]').selectOption('chatgpt_export'));
      assert.equal(await rowStatus(fixture.ambiguous), 'zatwierdzony');
      const location = 'Location History/Records.json';
      await decide(location, form => form.locator('input[name="optin:enable_location"]').check());
      assert.equal(await rowStatus(location), 'zatwierdzony');
      await decide('billing/bill_daymonth.csv', form => form.locator('select[name="param:profile_id"]').selectOption('example_semicolon_daymonth_v1'));
      assert.equal(await rowStatus('billing/bill_daymonth.csv'), 'zatwierdzony');
    });

    await test('bulk approval approves every ready item and reports blocked ones', async () => {
      await Promise.all([page.waitForNavigation(), page.locator('form[data-ip-action="bulk"] button[value="approve-ready"]').click()]);
      assert.match(await page.locator('#importplan-status').textContent(), /Pominięto 2 zablokowanych/);
      assert.equal(await page.locator('tr.ip-row-approved').count(), 22);
      assert.equal(await rowStatus('Chrome/BrowserHistory.json'), 'zaproponowany');
    });

    await test('run imports the approved items and links results', async () => {
      await page.locator('form[data-ip-action="run"] select[name=mode]').selectOption('sync');
      await Promise.all([page.waitForNavigation({timeout: 120000}), page.locator('form[data-ip-action="run"] button').click()]);
      assert.equal(await page.locator('tr.ip-row-done').count(), 22);
      assert.equal(await page.locator('tr.ip-row-proposed').count(), 2);
      assert.equal((await page.locator('table.ip-runs tbody tr').first().locator('.badge').textContent()).trim(), 'completed');
      const link = row('chatgpt-export-2025-03.zip').first().locator('.ip-links a').first();
      const href = await link.getAttribute('href');
      assert.match(href, /^\/artifact\/\d+$/);
      assert.equal((await fetch(base + href)).status, 200);
      const cross = await page.locator('table.ip-cross').textContent();
      assert.ok(cross.includes('eksport #'), 'cross-export table shows imported exports');
      assert.equal(fs.readdirSync(fixture.work).length, 0, 'temporary directories removed');
      if (process.env.AGEDS_SCREENSHOT_DIR) {
        await page.screenshot({path: path.join(process.env.AGEDS_SCREENSHOT_DIR, 'import-desktop.png'), fullPage: false});
        await page.locator('table.ip-items tbody tr').nth(9).screenshot({path: path.join(process.env.AGEDS_SCREENSHOT_DIR, 'import-row.png')});
      }
    });

    await test('originals unchanged and mobile layout has no horizontal page scroll', async () => {
      assert.equal(treeHash(fixture.corpus), before);
      await page.setViewportSize({width: 390, height: 844});
      await page.goto(planUrl);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      if (process.env.AGEDS_SCREENSHOT_DIR) await page.screenshot({path: path.join(process.env.AGEDS_SCREENSHOT_DIR, 'import-mobile.png')});
      assert.ok(overflow <= 1, `horizontal overflow ${overflow}px`);
    });

    await test('no page errors, console errors or dialogs', async () => {
      assert.deepEqual(errors, []);
      assert.equal(dialogs, 0);
      // The only failed requests: the deliberately refused approval and the browser's favicon probe.
      const unexpected = failures.filter(f => !/^422 \/api\/import-plans\/\d+\/decisions$/.test(f) && f !== '404 /favicon.ico');
      assert.deepEqual(unexpected, []);
      assert.equal(failures.filter(f => f.startsWith('422')).length, 1);
    });
    receipt.finished_at = new Date().toISOString();
    receipt.result = 'passed';
    if (process.env.AGEDS_BROWSER_RECEIPT) fs.writeFileSync(process.env.AGEDS_BROWSER_RECEIPT, JSON.stringify(receipt, null, 2) + '\n');
  } finally {
    if (browser) await browser.close();
    server.kill('SIGTERM');
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
