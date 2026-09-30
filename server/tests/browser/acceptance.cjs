/** Real Chromium + HTMLAudioElement, isolated generated WAV and SQLite. */
const {chromium} = require('playwright');
const {spawn, execFileSync} = require('node:child_process');
const {createHash} = require('node:crypto');
const {once} = require('node:events');
const net = require('node:net');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');
const repo = path.resolve(__dirname, '../../..');
const delay = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const socket = net.createServer(); socket.listen(0, '127.0.0.1'); await once(socket, 'listening');
  const port = socket.address().port; await new Promise(r => socket.close(r));
  const server = spawn(process.env.PYTHON || 'python', ['-m', 'server.tests.browser_fixture'], {cwd: repo, env: {...process.env, AGEDS_BROWSER_PORT: String(port)}, stdio: ['ignore', 'pipe', 'pipe']});
  let stderr = ''; server.stderr.on('data', d => stderr += d);
  let browser;
  const fingerprint = file => createHash('sha256').update(fs.readFileSync(path.join(repo, file))).digest('hex');
  const receipt = {git_head: execFileSync('git', ['rev-parse', 'HEAD'], {cwd: repo, encoding: 'utf8'}).trim(), playwright: require('playwright/package.json').version, source_sha256: Object.fromEntries(['server/app/main.py', 'server/app/static/citations.mjs', 'server/app/static/range-player.mjs', 'server/app/templates/artifact.html', 'server/app/templates/index.html', 'server/app/exchange.py', 'server/app/exchange_consumer.py', 'server/tests/browser/acceptance.cjs', 'server/tests/browser_fixture.py'].map(file => [file, fingerprint(file)])), task: 'AGEDS-20261001-N21-browser', baseline_task: 'AGEDS-20261001-N12', started_at: new Date().toISOString(), fixture: 'generated 4s mono PCM WAV; temporary SQLite; synthetic transcript versions', cases: []};
  try {
    const fixture = await new Promise((resolve, reject) => {
      let buf = ''; server.stdout.on('data', d => {buf += d; if (buf.includes('\n')) {try {resolve(JSON.parse(buf.split('\n')[0]));} catch(e) {reject(e);}}});
      server.on('exit', code => reject(new Error(`fixture exited ${code}: ${stderr}`)));
    });
    const base = `http://127.0.0.1:${port}`;
    for (let i = 0; i < 100; i++) {try {if ((await fetch(base)).ok) break;} catch {} await delay(50);}
    browser = await chromium.launch({headless: true, executablePath: process.env.AGEDS_BROWSER_EXECUTABLE || undefined, args: ['--no-sandbox']});
    receipt.browser = browser.version();
    const context = await browser.newContext();
    const page = await context.newPage();
    const pageErrors = []; page.on('pageerror', e => pageErrors.push(e.message));
    // Ignore AbortSignal for transcript fetches to independently exercise the generation guard.
    await page.addInitScript(() => {const native = window.fetch; window.fetch = (url, options) => native(url, String(url).includes('/transcript?') ? {...options, signal: undefined} : options);});
    const workspace = '[data-citation-workspace]';
    const sel = name => page.locator(`${workspace} [data-${name}]`);
    async function version(id) {await sel('version').selectOption(String(id)); await page.waitForFunction(id => document.querySelector('[data-status]').textContent.includes(`Wersja #${id}.`), id);}
    async function test(name, fn) {await fn(); receipt.cases.push({name, status: 'passed'}); console.log(`PASS ${name}`);}
    let downloadedPacket;
    async function downloadPinnedPacket(rendering) {
      assert.equal(await sel('version').inputValue(), String(fixture.new));
      const link = page.locator('[data-citation-packet]').first();
      const href = await link.getAttribute('href');
      const anchors = await (await fetch(`${base}/api/artifacts/${fixture.artifact}/citations`)).json();
      const anchor = anchors[0];
      assert.equal(href, `/api/artifacts/${fixture.artifact}/citations/${anchor.id}/packet`);
      const mediaRequests = [];
      const observe = request => {if (new URL(request.url()).pathname.endsWith('/content')) mediaRequests.push(request.url());};
      page.on('request', observe);
      try {
        const [download, response] = await Promise.all([
          page.waitForEvent('download'), page.waitForResponse(r => r.url() === base + href), link.click()
        ]);
        assert.equal(response.status(), 200);
        const filename = `ageds-citation-${fixture.artifact}-${anchor.id}.json`;
        assert.equal(download.suggestedFilename(), filename);
        assert.equal(response.headers()['content-disposition'], `attachment; filename="${filename}"`);
        assert.equal(response.headers()['cache-control'], 'no-store');
        assert.match(response.headers()['content-type'], /^application\/json/);
        assert.equal(await download.failure(), null);
        const downloaded = await download.path();
        const bytes = fs.readFileSync(downloaded);
        const packet = JSON.parse(bytes);
        assert.equal(packet.payload.anchor.id, anchor.id);
        assert.equal(packet.payload.anchor.derived_text_id, fixture.old);
        assert.equal(packet.payload.derived_text.id, fixture.old);
        assert.notEqual(packet.payload.derived_text.id, fixture.new);
        assert.equal(packet.payload.projection.quote_text, '  gęślą' + fixture.literal);
        assert.equal(packet.payload.derived_text.text, ' Zażółć  gęślą' + fixture.literal);
        assert.equal('stored_path' in packet.payload.artifact, false);
        assert.equal(packet.payload.scope.stored_path_included, false);
        assert.equal(packet.payload.scope.source_bytes_included, false);
        assert.equal(packet.payload.scope.media_bytes_included, false);
        assert.equal(packet.payload.scope.live_restore_supported, false);
        assert.equal(packet.payload.scope.tasks_imported, false);
        assert.equal(packet.payload.scope.locators, 'literal_inert_metadata');
        assert.ok(packet.payload.source_observations.length > 0);
        assert.match(packet.payload.artifact.sha256, /^[a-f0-9]{64}$/);
        const isolated = fs.mkdtempSync(path.join(os.tmpdir(), 'ageds-download-inspect-'));
        let inspection;
        try {
          inspection = JSON.parse(execFileSync(process.env.PYTHON || 'python', ['-m', 'server.app.exchange_consumer', downloaded], {
            cwd: repo, encoding: 'utf8', env: {...process.env,
              EW_DATA_DIR: path.join(isolated, 'never-live'), EW_STORE_DIR: path.join(isolated, 'never-live', 'store'),
              EW_DB_PATH: path.join(isolated, 'never-live', 'database.sqlite')}
          }));
          assert.equal(fs.existsSync(path.join(isolated, 'never-live')), false);
        } finally {fs.rmSync(isolated, {recursive: true, force: true});}
        assert.equal(inspection.verification, 'matches_included_pinned_transcript');
        assert.equal(inspection.derived_text_id, fixture.old);
        assert.equal(inspection.quote_text, packet.payload.projection.quote_text);
        assert.equal(inspection.selector.precision, 'word_asr');
        assert.deepEqual(mediaRequests, []);
        assert.equal(await sel('version').inputValue(), String(fixture.new));
        assert.equal(page.url(), `${base}/artifact/${fixture.artifact}`);
        assert.equal(await page.locator('img').count(), 0);
        assert.equal(await page.evaluate(() => window.__sourceExecuted), undefined);
        if (downloadedPacket) assert.deepEqual(packet, downloadedPacket);
        downloadedPacket = packet;
        (receipt.downloads ||= []).push({rendering, filename, content_disposition: response.headers()['content-disposition'],
          bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex'),
          derived_text_id: inspection.derived_text_id, anchor_id: inspection.anchor_id,
          verification: inspection.verification, media_requests: mediaRequests.length,
          stored_path_included: false, source_bytes_included: false, media_bytes_included: false});
      } finally {page.off('request', observe);}
    }
    await page.goto(`${base}/artifact/${fixture.artifact}`);
    await page.waitForFunction(id => document.querySelector('[data-status]').textContent.includes(`Wersja #${id}.`), fixture.new);
    await test('older version exact word citation and literal XSS text', async () => {
      await version(fixture.old); await sel('kind').selectOption('words'); await sel('first').selectOption('1'); await sel('last').selectOption('2');
      const quote = '  gęślą' + fixture.literal;
      assert.equal(await sel('preview').textContent(), quote);
      await sel('save').click(); await page.waitForFunction(() => document.querySelector('[data-status]').textContent.startsWith('Zapisano cytat'));
      const anchors = await (await fetch(`${base}/api/artifacts/${fixture.artifact}/citations`)).json();
      assert.equal(anchors[0].derived_text_id, fixture.old); assert.equal(anchors[0].quote_text, quote);
      assert.equal(anchors[0].selector.precision, 'word_asr'); assert.equal(anchors[0].start_ms, 500); assert.equal(anchors[0].end_ms, 1800);
      assert.equal(await sel('saved').locator('img').count(), 0); assert.equal(await page.evaluate(() => window.__sourceExecuted), undefined);
      assert.equal(await page.locator('img').count(), 0);
    });
    await test('fresh saved quote replay uses old anchor while latest version selected', async () => {
      await sel('kind').selectOption('segments'); await version(fixture.new);
      const replay = page.locator('[data-citation-play]').first();
      assert.equal(await replay.getAttribute('data-derived-text-id'), String(fixture.old));
      assert.equal(await replay.getAttribute('data-start-ms'), '500'); assert.equal(await replay.getAttribute('data-end-ms'), '1800');
      await replay.click(); await page.waitForFunction(() => !document.querySelector('audio').paused);
      const state = await page.locator('audio').evaluate(a => ({time: a.currentTime, duration: a.duration}));
      assert.ok(state.time >= .5 && state.time < 1.8); assert.equal(state.duration, 4);
      assert.equal(await sel('version').inputValue(), String(fixture.new));
      assert.match(await sel('status').textContent(), new RegExp(`wersja #${fixture.old}`));
      await page.waitForFunction(() => {const a = document.querySelector('audio'); return a.paused && Math.abs(a.currentTime - 1.8) < .03;});
    });
    await test('fresh packet link downloads and independently verifies old version while latest selected', async () => {
      await downloadPinnedPacket('dynamic');
    });
    await test('persisted quote replay retains anchor and cancels on seek outside range', async () => {
      await page.reload(); await page.waitForFunction(id => document.querySelector('[data-status]').textContent.includes(`Wersja #${id}.`), fixture.new);
      assert.equal(await sel('saved').locator('p').first().textContent(), '  gęślą' + fixture.literal);
      assert.equal(await page.locator('img').count(), 0);
      const replay = page.locator('[data-citation-play]').first();
      await replay.click(); await page.waitForFunction(() => !document.querySelector('audio').paused);
      assert.ok(await page.locator('audio').evaluate(a => a.currentTime >= .5 && a.currentTime < 1.8));
      await page.locator('audio').evaluate(a => {a.currentTime = 3;});
      await page.waitForFunction(() => document.querySelector('audio').paused);
      await replay.click(); await page.waitForFunction(() => !document.querySelector('audio').paused);
      await sel('stop').click(); await delay(100); assert.ok(await page.locator('audio').evaluate(a => a.paused));
    });
    await test('persisted packet link downloads identical pinned evidence while latest selected', async () => {
      await downloadPinnedPacket('persisted');
    });
    await test('stale transcript response cannot replace newer selected version', async () => {
      let release, entered; const held = new Promise(r => release = r); const arrived = new Promise(r => entered = r);
      const pattern = `**/transcript?derived_text_id=${fixture.old}`;
      await page.route(pattern, async route => {const response = await route.fetch(); entered(); await held; await route.fulfill({response});});
      await version(fixture.new); await sel('version').selectOption(String(fixture.old)); await arrived;
      await version(fixture.new); release(); await delay(150);
      assert.equal(await sel('preview').textContent(), ' NEW VERSION'); assert.match(await sel('status').textContent(), new RegExp(`Wersja #${fixture.new}\\.`));
      await page.unroute(pattern);
    });
    await test('pending save stays pinned while version switches', async () => {
      await version(fixture.old);
      let release, entered; const held = new Promise(r => release = r); const arrived = new Promise(r => entered = r);
      const pattern = '**/citations'; let posted;
      await page.route(pattern, async route => {posted = route.request().postDataJSON(); const response = await route.fetch(); entered(); await held; await route.fulfill({response});});
      await sel('save').click(); await arrived; await version(fixture.new); release();
      await page.waitForFunction(() => document.querySelectorAll('[data-saved] article').length === 2);
      assert.equal(posted.derivedTextId, fixture.old); assert.match(await sel('saved').locator('small').first().textContent(), new RegExp(`transkrypt #${fixture.old}`));
      assert.match(await sel('status').textContent(), new RegExp(`Wersja #${fixture.new}\\.`)); await page.unroute(pattern);
    });
    const audioState = () => page.locator('audio').evaluate(a => ({paused: a.paused, time: a.currentTime, duration: a.duration, ready: a.readyState, native: a instanceof HTMLAudioElement}));
    await test('real WAV playback seeks and stops at selected end', async () => {
      await version(fixture.old); await sel('kind').selectOption('words'); await sel('last').selectOption('1');
      await sel('play').click(); await page.waitForFunction(() => !document.querySelector('audio').paused);
      const playing = await audioState(); assert.equal(playing.native, true); assert.equal(playing.duration, 4); assert.ok(playing.time >= .2 && playing.time < 1.1);
      await page.waitForFunction(() => {const a = document.querySelector('audio'); return a.paused && Math.abs(a.currentTime - 1.1) < .03;});
      receipt.playback = {playing, stopped: await audioState(), tolerance_seconds: .03};
    });
    await test('stop button cancels real playback without later resumption', async () => {
      await sel('last').selectOption('2'); await sel('play').click(); await page.waitForFunction(() => !document.querySelector('audio').paused);
      await sel('stop').click(); const stopped = await audioState(); await delay(250); const after = await audioState(); assert.ok(after.paused); assert.ok(Math.abs(after.time - stopped.time) < .03);
    });
    await test('version change cancels audio and clears unavailable word selection', async () => {
      await sel('play').click(); await page.waitForFunction(() => !document.querySelector('audio').paused);
      await sel('version').selectOption(String(fixture.new)); await page.waitForFunction(() => document.querySelector('[data-status]').textContent.includes('nie ma zgodnych'));
      assert.ok((await audioState()).paused); assert.equal(await sel('preview').textContent(), ''); assert.ok(await sel('save').isDisabled()); assert.ok(await sel('play').isDisabled());
    });
    await test('selection change cancels native playback', async () => {
      await version(fixture.old); await sel('last').selectOption('2'); await sel('play').click(); await page.waitForFunction(() => !document.querySelector('audio').paused); await sel('last').selectOption('1'); assert.ok((await audioState()).paused);
    });
    await test('stop during delayed metadata prevents late native playback', async () => {
      await page.reload(); await version(fixture.old);
      let release, entered; const held = new Promise(r => release = r); const arrived = new Promise(r => entered = r);
      const pattern = '**/content';
      await page.route(pattern, async route => {const response = await route.fetch(); entered(); await held; await route.fulfill({response});});
      await sel('play').click(); await arrived; await sel('stop').click(); release(); await delay(300);
      assert.ok((await audioState()).paused); assert.ok(!(await sel('status').textContent()).includes('anulowane'));
      await page.unroute(pattern);
    });
    await test('search snippets keep source HTML inert in actual browser', async () => {
      await page.goto(`${base}/?q=${encodeURIComponent('gęślą')}`);
      assert.ok((await page.locator('body').textContent()).includes('<img src=x onerror='));
      assert.equal(await page.locator('img').count(), 0); assert.equal(await page.evaluate(() => window.__sourceExecuted), undefined);
    });
    assert.deepEqual(pageErrors, []); receipt.page_errors = pageErrors; receipt.status = 'passed';
  } catch (error) {receipt.status = 'failed'; receipt.error = error.stack; process.exitCode = 1; console.error(error);}
  finally {if (browser) await browser.close(); server.kill('SIGTERM'); receipt.finished_at = new Date().toISOString(); if (process.env.AGEDS_BROWSER_RECEIPT) fs.writeFileSync(process.env.AGEDS_BROWSER_RECEIPT, JSON.stringify(receipt, null, 2) + '\n');}
})();
