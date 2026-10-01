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
  const receipt = {git_head: execFileSync('git', ['rev-parse', 'HEAD'], {cwd: repo, encoding: 'utf8'}).trim(), playwright: require('playwright/package.json').version, source_sha256: Object.fromEntries(['server/app/main.py', 'server/app/db.py', 'server/app/static/citations.mjs', 'server/app/static/range-player.mjs', 'server/app/templates/artifact.html', 'server/app/templates/index.html', 'server/app/exchange.py', 'server/app/exchange_consumer.py', 'server/app/verified_media.py', 'server/app/search.py', 'server/app/read_pages.py', 'server/tests/browser/acceptance.cjs', 'server/tests/browser_fixture.py'].map(file => [file, fingerprint(file)])), task: 'AGEDS-20261001-N40-browser', baseline_task: 'AGEDS-20261001-N12', started_at: new Date().toISOString(), fixture: 'generated 4s mono PCM WAV; temporary SQLite; synthetic transcript versions', cases: []};
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
    await test('verified media endpoint supports exact ranges and HEAD in Chromium context', async () => {
      const url = `${base}/api/artifacts/${fixture.artifact}/content`;
      const full = await context.request.get(url);
      assert.equal(full.status(), 200);
      const all = await full.body();
      const range = await context.request.get(url, {headers: {Range: 'bytes=32-79'}});
      assert.equal(range.status(), 206);
      assert.deepEqual(await range.body(), all.subarray(32, 80));
      assert.equal(range.headers()['content-range'], `bytes 32-79/${all.length}`);
      assert.equal(range.headers()['cache-control'], 'no-store');
      const head = await context.request.head(url, {headers: {Range: 'bytes=32-79'}});
      assert.equal(head.status(), 200);
      assert.equal((await head.body()).length, 0);
      assert.equal(head.headers()['content-length'], String(all.length));
      const mismatch = await context.request.get(url, {headers: {Range: 'bytes=32-79', 'If-Range': '"different"'}});
      assert.equal(mismatch.status(), 200);
      assert.deepEqual(await mismatch.body(), all);
      receipt.verified_media = {full_bytes: all.length, range_bytes: 48, head_body_bytes: 0};
    });
    await test('unsafe DOM artifact ID refuses transcript requests before rounding', async () => {
      let transcriptRequests = 0;
      const watch = request => {if (request.url().includes('/transcript?')) transcriptRequests++;};
      const pattern = `${base}/artifact/${fixture.artifact}`;
      await page.route(pattern, async route => {
        const response = await route.fetch();
        const html = (await response.text()).replace(`data-artifact-id="${fixture.artifact}"`, 'data-artifact-id="9007199254740993"');
        await route.fulfill({response, body: html});
      });
      page.on('request', watch);
      await page.goto(pattern);
      await page.waitForFunction(() => document.querySelector('[data-status]').textContent.includes('nie zostanie zaokrąglone'));
      assert.equal(transcriptRequests, 0);
      assert.ok(await sel('save').isDisabled()); assert.ok(await sel('play').isDisabled());
      assert.equal(await page.locator('[data-citation-packet][href]').count(), 0);
      page.off('request', watch); await page.unroute(pattern);
    });
    await test('unsafe DOM transcript ID refuses fetch and preserves exact unsupported ID', async () => {
      await page.goto(`${base}/artifact/${fixture.artifact}`); await version(fixture.new);
      let transcriptRequests = 0;
      const watch = request => {if (request.url().includes('/transcript?')) transcriptRequests++;};
      page.on('request', watch);
      await sel('version').evaluate(select => {
        const option = document.createElement('option'); option.value = '9007199254740993'; option.textContent = '#9007199254740993';
        select.append(option); select.value = option.value; select.dispatchEvent(new Event('change', {bubbles: true}));
      });
      await page.waitForFunction(() => document.querySelector('[data-status]').textContent.includes('nie zostanie zaokrąglone'));
      assert.equal(transcriptRequests, 0); assert.equal(await sel('version').inputValue(), '9007199254740993');
      assert.ok(await sel('save').isDisabled()); assert.ok(await sel('play').isDisabled());
      page.off('request', watch);
    });
    await test('unsafe or mismatched API version identity never enables quote actions', async () => {
      for (const badId of [9007199254740992, fixture.old]) {
        const pattern = '**/transcript?*';
        await page.route(pattern, async route => {
          const response = await route.fetch(); const body = await response.json(); body.id = badId;
          await route.fulfill({response, json: body});
        });
        await page.goto(`${base}/artifact/${fixture.artifact}`);
        await page.waitForFunction(() => document.querySelector('[data-status]').textContent.includes('Operację wstrzymano'));
        assert.ok(await sel('save').isDisabled()); assert.ok(await sel('play').isDisabled());
        assert.equal(await sel('preview').textContent(), '');
        await page.unroute(pattern);
      }
    });
    await test('paged versions preserve selected pin across delayed older load', async () => {
      await page.goto(`${base}/artifact/${fixture.historyArtifact}`);
      await version(fixture.historyVersions.at(-1));
      assert.equal(await sel('version').locator('option').count(), 50);
      const more = page.locator('[data-history-more="versions"]');
      let entered, release; const arrived = new Promise(r => entered = r), held = new Promise(r => release = r);
      const pattern = '**/transcripts/page?*';
      await page.route(pattern, async route => {const response = await route.fetch(); entered(); await held; await route.fulfill({response});});
      await more.click(); await arrived; const pinned = fixture.historyVersions.at(-3); await version(pinned); release();
      await page.waitForFunction(() => document.querySelector('[data-version]').options.length === 100);
      assert.equal(await sel('version').inputValue(), String(pinned));
      assert.match(await sel('status').textContent(), new RegExp(`Wersja #${pinned}\\.`));
      await page.unroute(pattern); await more.click();
      await page.waitForFunction(() => document.querySelector('[data-version]').options.length === 125);
      assert.ok(await more.isDisabled());
      await version(fixture.historyVersions[0]);
      assert.ok((await page.locator('[data-transcript-text]').textContent()).includes(' History 0 '));
      assert.equal(await page.locator('img').count(), 0);
    });
    await test('citation and annotation pages expose all 125 rows without duplicates or source HTML', async () => {
      for (const [name, target] of [['citations', '[data-saved]'], ['annotations', '[data-annotation-history]']]) {
        assert.equal(await page.locator(`${target} article`).count(), 50);
        const button = page.locator(`[data-history-more="${name}"]`);
        await button.click(); await page.waitForFunction(target => document.querySelectorAll(`${target} article`).length === 100, target);
        await button.click(); await page.waitForFunction(target => document.querySelectorAll(`${target} article`).length === 125, target);
        assert.ok(await button.isDisabled());
        assert.match(await page.locator(`[data-history-status="${name}"]`).textContent(), /Koniec historii/);
      }
      const ids = await page.locator('[data-saved] [data-citation-play]').evaluateAll(items => items.map(item => item.dataset.citationId));
      assert.equal(new Set(ids).size, 125);
      assert.equal(await page.locator('img').count(), 0);
      assert.equal(await page.evaluate(() => window.__sourceExecuted), undefined);
      receipt.history_pages = {versions: 125, citations: 125, annotations: 125, initial_limit: 50};
    });
    await test('mismatched history snapshot is rejected without adding rows', async () => {
      await page.goto(`${base}/artifact/${fixture.historyArtifact}`); await version(fixture.historyVersions.at(-1));
      const pattern = '**/citations/page?*';
      await page.route(pattern, async route => {
        const response = await route.fetch(), body = await response.json(); body.snapshotMaxId += 1;
        await route.fulfill({response, json: body});
      });
      await page.locator('[data-history-more="citations"]').click();
      await page.waitForFunction(() => document.querySelector('[data-history-status="citations"]').textContent.includes('Nic nie dodano'));
      assert.equal(await page.locator('[data-saved] article').count(), 50);
      await page.unroute(pattern);
    });
    if (fixture.budgetArtifact !== null) await test('aggregate payload cap stops older quotes with explicit omission', async () => {
      await page.goto(`${base}/artifact/${fixture.budgetArtifact}`);
      await page.waitForFunction(() => document.querySelector('[data-status]').textContent.includes('Wersja #'));
      assert.equal(await page.locator('[data-saved] article').count(), 2);
      const more = page.locator('[data-history-more="citations"]');
      await more.click(); await page.waitForFunction(() => document.querySelectorAll('[data-saved] article').length === 4);
      await more.click();
      await page.waitForFunction(() => /budżet treści/.test(document.querySelector('[data-history-status="citations"]').textContent));
      assert.equal(await more.isDisabled(), true);
      assert.equal(await page.locator('[data-saved] article').count(), 4);
      const coverage = await page.locator('[data-history-status="citations"]').textContent();
      assert.doesNotMatch(coverage, /Koniec historii/);
      assert.match(coverage, /limit|budżet/i);
      const bytes = await page.locator('[data-saved] .transcript').evaluateAll(items => items.reduce((sum, item) => sum + new TextEncoder().encode(item.textContent).length, 0));
      assert.ok(bytes <= 4 * 1024 * 1024);
      receipt.payload_budget = {retained_quotes: 4, source_quotes: 6, retained_quote_text_bytes: bytes, encoded_payload_cap: 4 * 1024 * 1024, coverage};
    });
    assert.deepEqual(pageErrors, []); receipt.page_errors = pageErrors; receipt.status = 'passed';
  } catch (error) {receipt.status = 'failed'; receipt.error = error.stack; process.exitCode = 1; console.error(error);}
  finally {if (browser) await browser.close(); server.kill('SIGTERM'); receipt.finished_at = new Date().toISOString(); if (process.env.AGEDS_BROWSER_RECEIPT) fs.writeFileSync(process.env.AGEDS_BROWSER_RECEIPT, JSON.stringify(receipt, null, 2) + '\n');}
})();
