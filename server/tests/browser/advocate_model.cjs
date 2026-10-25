/** Real Chromium: the advocate page in model mode with a deterministic test transport (no network). */
const {chromium} = require('playwright');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const net = require('node:net');
const path = require('node:path');
const assert = require('node:assert/strict');
const repo = path.resolve(__dirname, '../../..');
const delay = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const socket = net.createServer(); socket.listen(0, '127.0.0.1'); await once(socket, 'listening');
  const port = socket.address().port; await new Promise(r => socket.close(r));
  const server = spawn(process.env.PYTHON || 'python', ['-m', 'server.tests.browser_advocate_fixture'],
    {cwd: repo, env: {...process.env, AGEDS_BROWSER_PORT: String(port), AGEDS_BROWSER_TEST_PROVIDER: '1'}, stdio: ['ignore', 'pipe', 'pipe']});
  let stderr = ''; server.stderr.on('data', d => stderr += d);
  let browser;
  try {
    const fixture = await new Promise((resolve, reject) => {
      let buf = ''; server.stdout.on('data', d => {buf += d; if (buf.includes('\n')) {try {resolve(JSON.parse(buf.split('\n')[0]));} catch (e) {reject(e);}}});
      server.on('exit', code => reject(new Error(`fixture exited ${code}: ${stderr}`)));
    });
    const base = `http://127.0.0.1:${port}`;
    for (let i = 0; i < 100; i++) {try {if ((await fetch(`${base}/advocate`)).ok) break;} catch {} await delay(50);}
    browser = await chromium.launch({headless: true, executablePath: process.env.AGEDS_BROWSER_EXECUTABLE || undefined, args: ['--no-sandbox']});
    const page = await (await browser.newContext()).newPage();
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    async function test(name, fn) {await fn(); console.log(`PASS ${name}`);}
    await page.goto(`${base}/advocate`);
    await page.fill('input[name=token]', fixture.token);
    await Promise.all([page.waitForNavigation(), page.click('#login button')]);
    await page.waitForSelector('#chat:not([hidden])');
    await test('model mode is announced', async () => {
      assert.match(await page.textContent('#notices'), /gemini-3\.5-flash \(Google Vertex\)/);
      assert.equal(await page.isHidden('#summaries'), false);
    });
    await test('model answer shows word labels and checkable citations', async () => {
      await page.fill('#question', 'Kiedy podpisano umowę najmu?');
      await page.click('#send');
      await page.waitForSelector('.answer .claim');
      const text = await page.textContent('#turns');
      assert.match(text, /Odpowiedź modelu gemini-3\.5-flash \(Google Vertex\)/);
      assert.match(text, /wynika z cytowanych fragmentów/);
      assert.match(text, /brak podstawy w materiale/);
      await page.click('.answer .cite >> nth=0');
      await page.waitForSelector('.basis blockquote');
      assert.match(await page.textContent('.basis'), /Umowa najmu została podpisana/);
    });
    await test('a summary is created and listed with its basis', async () => {
      await page.fill('#topic', 'umowa najmu');
      await page.click('#summary-send');
      await page.waitForSelector('#summary-list .claim');
      assert.match(await page.textContent('#summary-list'), /umowa najmu/);
      if (process.env.AGEDS_BROWSER_SCREENSHOT) await page.screenshot({path: process.env.AGEDS_BROWSER_SCREENSHOT, fullPage: true});
    });
    await test('no hidden material anywhere', async () => {
      assert.equal((await page.content()).includes(fixture.hidden), false);
    });
    assert.deepEqual(errors, []);
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
})().catch(error => { console.error(error); process.exit(1); });
