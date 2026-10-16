/** Real Chromium acceptance for /reports: forms, register, sandboxed preview, downloads,
 *  tamper detection, prefilled range from the timeline link. Isolated synthetic fixture. */
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
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
(async () => {
  const socket = net.createServer(); socket.listen(0, '127.0.0.1'); await once(socket, 'listening');
  const port = socket.address().port; await new Promise(r => socket.close(r));
  const server = spawn(process.env.PYTHON || 'python', ['-m', 'server.tests.browser.reports_fixture'], {cwd: repo, env: {...process.env, AGEDS_BROWSER_PORT: String(port)}, stdio: ['ignore', 'pipe', 'pipe']});
  let stderr = ''; server.stderr.on('data', d => stderr += d);
  let browser;
  const receipt = {git_head: execFileSync('git', ['rev-parse', 'HEAD'], {cwd: repo, encoding: 'utf8'}).trim(),
    playwright: require('playwright/package.json').version, task: 'AGEDS-TASK-20261016-F-browser', started_at: new Date().toISOString(),
    fixture: 'server/tests/browser/reports_fixture.py (temporary SQLite, synthetic corpus parts chat_exports/communications/catalog_files, planted hard cases)', cases: []};
  try {
    const fixture = await new Promise((resolve, reject) => {
      let buf = ''; server.stdout.on('data', d => {buf += d; if (buf.includes('\n')) {try {resolve(JSON.parse(buf.split('\n')[0]));} catch (e) {reject(e);}}});
      server.on('exit', code => reject(new Error(`fixture exited ${code}: ${stderr}`)));
    });
    const base = `http://127.0.0.1:${port}`;
    for (let i = 0; i < 200; i++) {try {if ((await fetch(base + '/reports')).ok) break;} catch {} await delay(50);}
    browser = await chromium.launch({headless: true, executablePath: process.env.AGEDS_BROWSER_EXECUTABLE || undefined, args: ['--no-sandbox']});
    receipt.browser = browser.version();
    const context = await browser.newContext({acceptDownloads: true, locale: 'pl-PL'});
    const page = await context.newPage();
    const pageErrors = []; page.on('pageerror', e => pageErrors.push(e.message));
    const foreign = []; context.on('request', r => {if (!r.url().startsWith(base) && !r.url().startsWith('data:') && r.url() !== 'about:blank') foreign.push(r.url());});
    async function test(name, fn) {await fn(); receipt.cases.push({name, status: 'passed'}); console.log(`PASS ${name}`);}
    let timelineId, lawyerId;

    await test('reports page lists forms for every type and an empty register', async () => {
      await page.goto(base + '/reports');
      assert.equal(await page.locator('nav.ageds-nav a[href="/reports"]').textContent(), 'Raporty');
      assert.equal(await page.locator('[data-type-form]').count(), 5);
      for (const type of ['catalog', 'timeline', 'time_budget', 'hypothesis_dossier', 'lawyer_package']) assert.equal(await page.locator(`[data-type-form="${type}"]`).count(), 1);
      assert.match(await page.locator('[data-report-list]').textContent(), /Brak raportów/);
      assert.equal(await page.locator('[data-output-disabled]').count(), 0);
    });

    await test('timeline link prefills the range on /reports/new', async () => {
      await page.goto(base + '/reports/new?from=2026-03-01T00:00:00Z&to=2026-03-06T00:00:00Z');
      const form = page.locator('[data-type-form="timeline"]');
      assert.equal(await form.getAttribute('open'), '');
      assert.equal(await form.locator('input[name="from"]').inputValue(), '2026-03-01T00:00:00Z');
      assert.equal(await form.locator('input[name="to"]').inputValue(), '2026-03-06T00:00:00Z');
    });

    await test('invalid naive time is refused with a visible error and no report', async () => {
      const form = page.locator('[data-type-form="timeline"] form');
      await form.locator('input[name="from"]').fill('2026-03-01T10:00');
      await Promise.all([page.waitForLoadState('load'), form.locator('button[data-create]').click()]);
      assert.match(await page.locator('[data-report-error]').textContent(), /offset/);
      assert.match(await page.locator('[data-report-list]').textContent(), /Brak raportów/);
    });

    await test('timeline report created from the form verifies and previews in a sandboxed frame', async () => {
      await page.goto(base + '/reports/new?from=2026-03-01T00:00:00Z&to=2026-03-06T00:00:00Z');
      const form = page.locator('[data-type-form="timeline"] form');
      await form.locator('input[name="counterparty"]').fill('Urząd');
      await Promise.all([page.waitForURL(/\/reports\/\d+$/), form.locator('button[data-create]').click()]);
      timelineId = Number(page.url().split('/').pop());
      assert.equal(await page.locator('[data-verification]').getAttribute('data-verification'), 'valid');
      const iframe = page.locator('iframe[data-report-preview]');
      assert.equal(await iframe.getAttribute('sandbox'), '');
      const frame = page.frameLocator('iframe[data-report-preview]');
      await frame.locator('h1').waitFor();
      const text = await frame.locator('body').textContent();
      assert.match(text, /<script>window.__x=1<\/script>/);  // source HTML shown as text
      assert.match(text, /Urząd Przykładowy/);
      assert.equal(await frame.locator('script').count(), 0);
      assert.equal(await page.evaluate(() => window.__x), undefined);
      const handle = await iframe.elementHandle(); const content = await handle.contentFrame();
      assert.equal(await content.evaluate(() => typeof window.__x), 'undefined');
      assert.equal(await content.evaluate(() => document.querySelectorAll('[data-k]').length > 40), true);
    });

    await test('downloaded report.json matches the SHA-256 listed in the manifest table', async () => {
      const row = page.locator('[data-report-files] tr', {hasText: 'report.json'});
      const listed = (await row.locator('code').textContent()).trim();
      const [download] = await Promise.all([page.waitForEvent('download'), row.locator('a').click()]);
      const bytes = fs.readFileSync(await download.path());
      assert.equal(sha256(bytes), listed);
      const document = JSON.parse(bytes);
      assert.equal(document.report_type, 'timeline');
      assert.equal(document.method.parameters.counterparty, 'Urząd');
      const [manifestDownload] = await Promise.all([page.waitForEvent('download'), page.locator('[data-manifest-link]').click()]);
      const manifestBytes = fs.readFileSync(await manifestDownload.path());
      assert.equal(sha256(manifestBytes), (await page.locator('[data-manifest-sha256]').textContent()).trim());
    });

    await test('lawyer package with a typed question previews; packet link navigates inside the frame', async () => {
      await page.goto(base + '/reports');
      const details = page.locator('[data-type-form="lawyer_package"]');
      await details.locator('summary').click();
      const form = details.locator('form');
      await form.locator('textarea[name="open_questions"]').fill('Czy pismo z 3 marca dotarło?');
      if (fixture.pdf) await form.locator('input[name="formats"][value="pdf"]').check();
      await Promise.all([page.waitForURL(/\/reports\/\d+$/), form.locator('button[data-create]').click()]);
      lawyerId = Number(page.url().split('/').pop());
      assert.notEqual(lawyerId, timelineId);
      const frame = page.frameLocator('iframe[data-report-preview]');
      await frame.locator('h1').waitFor();
      assert.match(await frame.locator('body').textContent(), /Czy pismo z 3 marca dotarło\?/);
      const link = frame.locator(`a[href="citations/packets/a${fixture.artifactAudio}-c${fixture.anchorSegment}.json"]`);
      assert.equal(await link.count(), 1);
      const handle = await page.locator('iframe[data-report-preview]').elementHandle(); const content = await handle.contentFrame();
      await Promise.all([content.waitForURL(/\/view\/citations\/packets\//), link.click()]);
      assert.match(await content.evaluate(() => document.body.textContent), /ageds\.citation-evidence\/v1/);
      if (fixture.pdf) {
        const row = page.locator('[data-report-files] tr', {hasText: 'report.pdf'});
        const [download] = await Promise.all([page.waitForEvent('download'), row.locator('a').click()]);
        assert.equal(fs.readFileSync(await download.path()).subarray(0, 5).toString(), '%PDF-');
        receipt.pdf = 'written by EW_PDF_CHROMIUM adapter and downloaded';
      } else receipt.pdf = 'not requested (EW_PDF_CHROMIUM not set for this run)';
    });

    await test('register lists both reports; earlier report remains after regeneration', async () => {
      await page.goto(base + '/reports');
      const ids = await page.locator('[data-report-list] a').allTextContents();
      assert.deepEqual(ids.map(Number).sort(), [timelineId, lawyerId].sort());
    });

    await test('tampering with a file blocks the preview and shows the mismatch', async () => {
      const dirs = fs.readdirSync(fixture.reportsDir).filter(name => name.startsWith('ageds-report-timeline-'));
      assert.equal(dirs.length, 1);
      fs.appendFileSync(path.join(fixture.reportsDir, dirs[0], 'report.html'), '<!-- changed -->');
      await page.goto(`${base}/reports/${timelineId}`);
      assert.equal(await page.locator('[data-verification]').getAttribute('data-verification'), 'invalid');
      assert.match(await page.locator('[data-verification-errors]').textContent(), /file_digest_mismatch/);
      assert.equal(await page.locator('[data-preview-blocked]').count(), 1);
      const response = await page.request.get(`${base}/reports/${timelineId}/files/report.html`);
      assert.equal(response.status(), 409);
    });

    await test('no page errors and no requests outside the local server', async () => {
      assert.deepEqual(pageErrors, []);
      assert.deepEqual(foreign, []);
    });
    receipt.finished_at = new Date().toISOString();
    receipt.result = 'passed';
    if (process.env.AGEDS_BROWSER_RECEIPT) fs.writeFileSync(process.env.AGEDS_BROWSER_RECEIPT, JSON.stringify(receipt, null, 2) + '\n');
  } catch (error) {
    console.error(error);
    console.error(stderr.slice(-4000));
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
    server.kill('SIGTERM');
  }
})();
