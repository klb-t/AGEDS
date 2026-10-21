/** Real Chromium check of track D's /media pages: render, navigation, compare view escapes model text, no page errors. */
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
  const server = spawn(process.env.PYTHON || 'python', ['-m', 'server.tests.browser.media_fixture'],
    {cwd: repo, env: {...process.env, AGEDS_BROWSER_PORT: String(port)}, stdio: ['ignore', 'pipe', 'pipe']});
  let stderr = ''; server.stderr.on('data', d => stderr += d);
  let browser;
  const receipt = {git_head: execFileSync('git', ['rev-parse', 'HEAD'], {cwd: repo, encoding: 'utf8'}).trim(),
    playwright: require('playwright/package.json').version, task: 'AGEDS-TASK-20261016-I (acceptance of D pages)',
    started_at: new Date().toISOString(), fixture: 'generated WAV + PNG, two transcript versions and one OCR version with markup-like model text; temporary SQLite',
    cases: []};
  try {
    const fixture = await new Promise((resolve, reject) => {
      let buf = ''; server.stdout.on('data', d => {buf += d; if (buf.includes('\n')) {try {resolve(JSON.parse(buf.split('\n')[0]));} catch (e) {reject(e);}}});
      server.on('exit', code => reject(new Error(`fixture exited ${code}: ${stderr}`)));
    });
    const base = `http://127.0.0.1:${port}`;
    for (let i = 0; i < 200; i++) {try {if ((await fetch(base + '/media')).ok) break;} catch {} await delay(50);}
    browser = await chromium.launch({headless: true, executablePath: process.env.AGEDS_BROWSER_EXECUTABLE || undefined, args: ['--no-sandbox']});
    receipt.browser = browser.version();
    // Polish UI explicitly: the workbench shell (track H) negotiates Accept-Language, and headless Chromium sends en-US.
    const page = await browser.newPage({locale: 'pl-PL'});
    const errors = []; let dialogs = 0; const failures = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('console', m => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text()); });
    page.on('dialog', d => { dialogs += 1; d.dismiss(); });
    page.on('response', r => { if (r.status() >= 400) failures.push(`${r.status()} ${new URL(r.url()).pathname}`); });
    async function test(name, fn) {await fn(); receipt.cases.push({name, status: 'passed'}); console.log(`PASS ${name}`);}
    const noExecution = async () => {
      assert.equal(await page.evaluate(() => window.__ageds_media_xss), undefined);
      assert.equal(await page.locator('main img, body > img, td img, p img').count(), 0);
    };

    await test('media index lists artifacts with navigation', async () => {
      const response = await page.goto(`${base}/media`);
      assert.equal(response.status(), 200);
      assert.equal(await page.locator('nav.ageds-nav a[href="/media"]').textContent(), 'Transkrypcje');
      const body = await page.locator('body').textContent();
      assert.ok(body.includes('<b>tone</b>.wav'), 'original name rendered as text');
      assert.equal(await page.locator('b', {hasText: 'tone'}).count(), 0);
      await noExecution();
    });

    await test('artifact page lists versions and offers comparison', async () => {
      const response = await page.goto(`${base}/media/artifact/${fixture.artifact}`);
      assert.equal(response.status(), 200);
      const text = await page.locator('body').textContent();
      assert.ok(text.includes(`${fixture.old}`) && text.includes(`${fixture.new}`));
      await page.locator('select[name=a]').selectOption(String(fixture.old));
      await page.locator('select[name=b]').selectOption(String(fixture.new));
      await Promise.all([page.waitForURL(/\/media\/compare\?/), page.locator('form[action="/media/compare"] button').click()]);
      await noExecution();
    });

    await test('compare view escapes model text and marks differences', async () => {
      const response = await page.goto(`${base}/media/compare?a=${fixture.old}&b=${fixture.new}`);
      assert.equal(response.status(), 200);
      const diff = page.locator('p.media-diff');
      const text = await diff.textContent();
      assert.ok(text.includes('<img'), 'model text is shown literally');
      assert.ok(text.includes('<script>window.__ageds_media_xss=2</script>'), 'script-like model text is shown literally');
      assert.ok(await diff.locator('del').count() >= 1 && await diff.locator('ins').count() >= 1);
      assert.equal(await diff.locator('script').count(), 0);
      await noExecution();
    });

    await test('OCR artifact page escapes recognized text and file name', async () => {
      const response = await page.goto(`${base}/media/artifact/${fixture.image}`);
      assert.equal(response.status(), 200);
      assert.ok((await page.locator('body').textContent()).includes('<script>.png'));
      await noExecution();
    });

    await test('queue and speakers pages render', async () => {
      let response = await page.goto(`${base}/media/queue`);
      assert.equal(response.status(), 200);
      assert.ok((await page.locator('body').textContent()).includes(`${fixture.job}`));
      response = await page.goto(`${base}/media/artifact/${fixture.artifact}/speakers?derived_text_id=${fixture.old}`);
      assert.equal(response.status(), 200);
      await noExecution();
    });

    await test('mobile width of the compare view is measured (strict with AGEDS_MEDIA_STRICT_MOBILE=1)', async () => {
      await page.setViewportSize({width: 390, height: 844});
      await page.goto(`${base}/media/compare?a=${fixture.old}&b=${fixture.new}`);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      receipt.mobile_overflow_px = overflow;
      receipt.mobile_overflow_elements = await page.evaluate(() => [...document.querySelectorAll('body *')]
        .filter(e => e.getBoundingClientRect().right > window.innerWidth + 1)
        .slice(0, 8).map(e => `${e.tagName.toLowerCase()}${e.className ? '.' + String(e.className).split(' ').join('.') : ''}`));
      // Finding for the media/UI owners (not fixed here: files owned by tracks D/H). Strict when requested.
      if (overflow > 1) console.log(`FINDING compare view overflows a 390px viewport by ${overflow}px: ${receipt.mobile_overflow_elements.join(', ')}`);
      if (process.env.AGEDS_MEDIA_STRICT_MOBILE === '1') assert.ok(overflow <= 1, `horizontal overflow ${overflow}px`);
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
