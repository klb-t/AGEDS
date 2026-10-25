/** Real Chromium: advocate login, question, citation, range playback; synthetic data in tokens mode. */
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
  const server = spawn(process.env.PYTHON || 'python', ['-m', 'server.tests.browser_advocate_fixture'],
    {cwd: repo, env: {...process.env, AGEDS_BROWSER_PORT: String(port)}, stdio: ['ignore', 'pipe', 'pipe']});
  let stderr = ''; server.stderr.on('data', d => stderr += d);
  const receipt = {git_head: execFileSync('git', ['rev-parse', 'HEAD'], {cwd: repo, encoding: 'utf8'}).trim(),
    task: 'AGEDS-TASK-20261025-P10', fixture: 'synthetic 6 s WAV, synthetic transcript, temporary SQLite, tokens mode',
    started_at: new Date().toISOString(), cases: []};
  let browser;
  try {
    const fixture = await new Promise((resolve, reject) => {
      let buf = ''; server.stdout.on('data', d => {buf += d; if (buf.includes('\n')) {try {resolve(JSON.parse(buf.split('\n')[0]));} catch (e) {reject(e);}}});
      server.on('exit', code => reject(new Error(`fixture exited ${code}: ${stderr}`)));
    });
    const base = `http://127.0.0.1:${port}`;
    for (let i = 0; i < 100; i++) {try {if ((await fetch(`${base}/advocate`)).ok) break;} catch {} await delay(50);}
    browser = await chromium.launch({headless: true, executablePath: process.env.AGEDS_BROWSER_EXECUTABLE || undefined,
      args: ['--no-sandbox', '--autoplay-policy=no-user-gesture-required']});
    receipt.browser = browser.version();
    const page = await (await browser.newContext()).newPage();
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    async function test(name, fn) {await fn(); receipt.cases.push({name, status: 'passed'}); console.log(`PASS ${name}`);}

    await test('anonymous visitor sees only the login form', async () => {
      await page.goto(`${base}/advocate`);
      await page.waitForSelector('#login:not([hidden])');
      assert.equal(await page.isHidden('#chat'), true);
    });
    await test('a wrong token is refused without a session', async () => {
      await page.fill('input[name=token]', 'nieprawidlowy.token');
      await Promise.all([page.waitForNavigation(), page.click('#login button')]);
      await page.waitForSelector('#login [role=alert]');
      assert.equal((await page.context().cookies()).length, 0);
    });
    await test('login opens the conversation with honest notices', async () => {
      await page.fill('input[name=token]', fixture.token);
      await Promise.all([page.waitForNavigation(), page.click('#login button')]);
      await page.waitForSelector('#chat:not([hidden])');
      const notices = await page.textContent('#notices');
      assert.match(notices, /Model językowy nie jest włączony/);
      assert.match(notices, /Właściciel materiału nie widzi treści tej rozmowy/);
      assert.match(await page.textContent('#who'), /adwokat \(syntetyczny\)/);
    });
    await test('a question returns labelled quotes with citations', async () => {
      await page.fill('#question', 'Kiedy podpisano umowę najmu?');
      await page.click('#send');
      await page.waitForSelector('.answer .claim');
      const text = await page.textContent('#turns');
      assert.match(text, /cytat z zapisanego tekstu/);
      assert.match(text, /Umowa najmu została podpisana piątego marca/);
      assert.ok(text.includes('<img src=x'), 'source markup is shown as text');
      assert.equal(await page.evaluate(() => window.__sourceExecuted), undefined);
    });
    await test('a citation opens its pinned basis and plays its range', async () => {
      await page.click('.answer .cite >> nth=0');
      await page.waitForSelector('.basis blockquote');
      const basis = await page.textContent('.basis');
      assert.match(basis, /transkrypcja nagrania/);
      assert.match(basis, /SHA-256 materiału/);
      await page.click('.basis .play');
      await page.waitForFunction(() => { const a = document.getElementById('player'); return a.currentTime >= 0.5; }, null, {timeout: 10000});
      await page.waitForFunction(() => document.getElementById('player').paused, null, {timeout: 10000});
      const time = await page.evaluate(() => document.getElementById('player').currentTime);
      assert.ok(time >= 1.9 && time <= 2.2, `stopped at range end, got ${time}`);
      if (process.env.AGEDS_BROWSER_SCREENSHOT) await page.screenshot({path: process.env.AGEDS_BROWSER_SCREENSHOT, fullPage: true});
    });
    await test('history survives a reload and hidden material never appears', async () => {
      await page.reload();
      await page.waitForSelector('.answer .claim');
      assert.equal(await page.locator('.turn').count(), 1);
      const all = await page.content();
      assert.equal(all.includes(fixture.hidden), false);
      await page.fill('#question', `umowa najmu ${'poufna'}`);
      await page.click('#send');
      await page.waitForFunction(() => document.querySelectorAll('.turn').length === 2);
      assert.equal((await page.content()).includes(fixture.hidden), false);
    });
    await test('logout ends the session', async () => {
      await Promise.all([page.waitForNavigation(), page.click('#logout button')]);
      await page.waitForSelector('#login:not([hidden])');
    });
    assert.deepEqual(errors, []);
    receipt.finished_at = new Date().toISOString();
    if (process.env.AGEDS_BROWSER_RECEIPT) fs.writeFileSync(process.env.AGEDS_BROWSER_RECEIPT, JSON.stringify(receipt, null, 2) + '\n', {flag: 'wx'});
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
})().catch(error => { console.error(error); process.exit(1); });
