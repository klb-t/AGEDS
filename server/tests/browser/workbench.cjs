/** Real Chromium acceptance for the workbench: theme, language, shortcuts, palette, bulk tags, folders.
 * Isolated temporary SQLite and generated synthetic data only (never a live catalog). */
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

const FIXTURE = String.raw`
import json, math, os, struct, tempfile, wave
from pathlib import Path
with tempfile.TemporaryDirectory(prefix='ageds-wb-browser-') as directory:
    root = Path(directory)
    os.environ.update(EW_DATA_DIR=str(root), EW_DB_PATH=str(root / 'evidence.sqlite'), EW_STORE_DIR=str(root / 'store'), EW_REQUIRE_TOKEN='')
    from server.app import db, evidence, citations
    import uvicorn
    db.init_db()
    path = root / 'synthetic-tone.wav'
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b''.join(struct.pack('<h', int(800 * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(16000)))
    literal = ' <img src=x onerror="window.__wbSourceExecuted=1">'
    source = evidence.ensure_source('synthetic', 'workbench browser fixture')
    artifact = evidence.ingest_file(path, source_id=source, source_locator='synthetic:wb-tone', original_name='nagranie Łódź.wav')
    segments = [{'start': 0.0, 'end': 0.5, 'text': ' Zażółć gęślą jaźń'}, {'start': 0.5, 'end': 1.0, 'text': ' umowa w Łodzi' + literal}]
    version = evidence.add_derived_text(artifact, 'transcript', ''.join(s['text'] for s in segments), model='synthetic', language='pl', segments=segments)
    anchor = citations.create_citation(artifact, version, [1])
    events = [evidence.add_event(source_id=source, artifact_id=artifact, event_type='sms', ts_start='2026-03-01T08:00:00+00:00',
                                 body=f'umowa podpisana {i}' + literal, external_id=f'wb-{i}') for i in range(3)]
    with db.session() as conn:
        conn.execute("INSERT INTO annotations(artifact_id, kind, label, body) VALUES (?, 'note', 'Ważne', ?)", (artifact, 'Łukasz: umowa' + literal))
    print(json.dumps({'artifact': artifact, 'version': version, 'anchor': anchor['id'], 'events': events, 'literal': literal}), flush=True)
    uvicorn.run('server.app.main:app', host='127.0.0.1', port=int(os.environ['AGEDS_BROWSER_PORT']), log_level='warning')
`;

(async () => {
  const socket = net.createServer(); socket.listen(0, '127.0.0.1'); await once(socket, 'listening');
  const port = socket.address().port; await new Promise(r => socket.close(r));
  const server = spawn(process.env.PYTHON || 'python', ['-c', FIXTURE], {cwd: repo, env: {...process.env, PYTHONPATH: repo, AGEDS_BROWSER_PORT: String(port)}, stdio: ['ignore', 'pipe', 'pipe']});
  let stderr = ''; server.stderr.on('data', d => stderr += d);
  let browser;
  const fingerprint = file => createHash('sha256').update(fs.readFileSync(path.join(repo, file))).digest('hex');
  const files = ['server/app/static/app.css', 'server/app/static/workbench.css', 'server/app/static/workbench-shell.mjs', 'server/app/static/workbench-keys.mjs',
    'server/app/static/workbench-keys.json', 'server/app/static/workbench-i18n.mjs', 'server/app/static/workbench-theme.mjs', 'server/app/static/workbench.mjs',
    'server/app/templates/_nav.html', 'server/app/templates/workbench.html', 'server/app/templates/workbench_folder.html', 'server/app/api/workbench.py',
    'server/tests/browser/workbench.cjs'];
  const receipt = {git_head: execFileSync('git', ['rev-parse', 'HEAD'], {cwd: repo, encoding: 'utf8'}).trim(), playwright: require('playwright/package.json').version,
    task: 'AGEDS-TASK-20261016-H', source_sha256: Object.fromEntries(files.map(f => [f, fingerprint(f)])), started_at: new Date().toISOString(),
    fixture: 'generated 1s mono PCM WAV; temporary SQLite; synthetic Polish transcript, events and annotation with literal source HTML', cases: []};
  try {
    const fixture = await new Promise((resolve, reject) => {
      let buf = ''; server.stdout.on('data', d => {buf += d; if (buf.includes('\n')) {try {resolve(JSON.parse(buf.split('\n')[0]));} catch (e) {reject(e);}}});
      server.on('exit', code => reject(new Error(`fixture exited ${code}: ${stderr}`)));
    });
    const base = `http://127.0.0.1:${port}`;
    for (let i = 0; i < 100; i++) {try {if ((await fetch(base)).ok) break;} catch {} await delay(50);}
    browser = await chromium.launch({headless: true, executablePath: process.env.AGEDS_BROWSER_EXECUTABLE || undefined, args: ['--no-sandbox']});
    receipt.browser = browser.version();
    const context = await browser.newContext({locale: 'pl-PL', colorScheme: 'dark'});
    const page = await context.newPage();
    const pageErrors = []; page.on('pageerror', e => pageErrors.push(e.message));
    const consoleErrors = []; page.on('console', m => {if (m.type() === 'error') consoleErrors.push(m.text());});
    page.on('dialog', dialog => dialog.accept());
    const ready = async () => page.waitForFunction(() => document.documentElement.dataset.wbShell === 'ready');
    const inert = async () => {
      assert.equal(await page.locator('img').count(), 0);
      assert.equal(await page.evaluate(() => window.__wbSourceExecuted), undefined);
    };
    const bg = () => page.evaluate(() => getComputedStyle(document.body).backgroundImage + '|' + getComputedStyle(document.documentElement).getPropertyValue('--bg').trim());
    const cookie = async name => (await context.cookies(base)).find(c => c.name === name)?.value;
    async function test(name, fn) {await fn(); receipt.cases.push({name, status: 'passed'}); console.log(`PASS ${name}`);}

    await test('shell loads on every page without errors and keeps source HTML inert', async () => {
      for (const target of ['/', `/artifact/${fixture.artifact}`, '/workbench?q=umowa', '/workbench/folders', '/workbench/bookmarks',
                            `/workbench/artifacts/${fixture.artifact}`, '/integrations']) {
        await page.goto(base + target); await ready(); await inert();
        assert.equal(await page.evaluate(() => document.documentElement.dataset.wbKeyConflicts), '0');
      }
      const text = await page.goto(`${base}/workbench?q=umowa`).then(() => page.locator('[data-wb-search-panel]').textContent());
      assert.ok(text.includes('<img src=x onerror='), 'source markup is shown as text');
    });

    await test('theme toggle cycles auto → light → dark, persists in a cookie and is rendered by the server', async () => {
      await page.goto(`${base}/workbench`); await ready();
      const toggle = page.locator('[data-wb-theme-toggle]');
      assert.equal(await page.evaluate(() => document.documentElement.getAttribute('data-theme')), null);
      assert.equal(await page.evaluate(() => document.documentElement.dataset.wbThemeShown), 'dark');
      const darkBg = await bg();
      await toggle.click();
      assert.equal(await page.evaluate(() => document.documentElement.getAttribute('data-theme')), 'light');
      assert.equal(await cookie('ageds_theme'), 'light');
      const lightBg = await bg();
      assert.notEqual(lightBg, darkBg);
      assert.match(lightBg, /#ffffff$/);
      const html = await (await page.request.get(`${base}/workbench`)).text();
      assert.ok(html.includes('data-theme="light"'), 'server renders the explicit choice, no flash');
      await toggle.click();
      assert.equal(await page.evaluate(() => document.documentElement.getAttribute('data-theme')), 'dark');
      await page.emulateMedia({colorScheme: 'light'});
      assert.match(await bg(), /#0d1117$/, 'explicit dark wins over the system preference');
      await toggle.click();
      assert.equal(await cookie('ageds_theme'), 'auto');
      assert.match(await bg(), /#ffffff$/, 'auto follows prefers-color-scheme');
      await page.emulateMedia({colorScheme: 'dark'});
      assert.match(await bg(), /#0d1117$/);
      receipt.theme = {dark_bg: darkBg, light_bg: lightBg};
    });

    await test('page renders and toggles without cookie storage', async () => {
      const blocked = await browser.newContext({locale: 'pl-PL', colorScheme: 'light'});
      await blocked.addInitScript(() => Object.defineProperty(Document.prototype, 'cookie', {get() {throw new Error('blocked');}, set() {throw new Error('blocked');}}));
      await blocked.addInitScript(() => Object.defineProperty(window, 'localStorage', {get() {throw new Error('blocked');}}));
      const other = await blocked.newPage(); const errors = []; other.on('pageerror', e => errors.push(e.message));
      await other.goto(`${base}/workbench`);
      await other.waitForFunction(() => document.documentElement.dataset.wbShell === 'ready');
      await other.locator('[data-wb-theme-toggle]').click();
      assert.equal(await other.evaluate(() => document.documentElement.dataset.wbThemePersisted), 'false');
      assert.equal(await other.evaluate(() => document.documentElement.getAttribute('data-theme')), 'light');
      assert.deepEqual(errors, []);
      await blocked.close();
    });

    await test('language switch: Accept-Language, cookie from the selector, ?lang= override', async () => {
      const english = await browser.newContext({locale: 'en-US'});
      const en = await english.newPage();
      await en.goto(`${base}/workbench`);
      assert.equal(await en.evaluate(() => document.documentElement.lang), 'en');
      assert.ok((await en.locator('.ageds-nav').textContent()).includes('Folders'));
      await english.close();
      await page.goto(`${base}/workbench?q=umowa`); await ready();
      assert.equal(await page.evaluate(() => document.documentElement.lang), 'pl');
      await Promise.all([page.waitForURL(/\/workbench\?q=umowa$/), page.locator('[data-wb-lang]').selectOption('en')]);
      await ready();
      assert.equal(await cookie('ageds_lang'), 'en');
      assert.equal(await page.evaluate(() => document.documentElement.lang), 'en');
      assert.ok((await page.locator('h1').textContent()).includes('Workbench'));
      assert.ok((await page.locator('[data-wb-search-panel]').textContent()).includes('umowa podpisana'), 'source text is not translated');
      await page.goto(`${base}/workbench?lang=nl`); await ready();
      assert.ok((await page.locator('h1').textContent()).includes('Werkplaats'));
      await page.locator('[data-wb-lang]').selectOption('pl'); await page.waitForLoadState('load'); await ready();
      assert.equal(await page.evaluate(() => document.documentElement.lang), 'pl');
      assert.ok(!page.url().includes('lang='));
    });

    await test('shortcuts overlay opens with ?, closes with Escape and never steals typing', async () => {
      await page.goto(`${base}/workbench`); await ready();
      await page.keyboard.press('Shift+Slash');
      const overlay = page.locator('[data-wb-overlay="help"]');
      await overlay.waitFor({state: 'visible'});
      const rows = await overlay.locator('tr').count();
      assert.ok(rows >= 10, `help lists bindings (${rows})`);
      assert.ok((await overlay.textContent()).includes('Skróty klawiszowe'));
      await page.keyboard.press('Escape');
      await overlay.waitFor({state: 'hidden'});
      await page.locator('[data-wb-search]').click();
      await page.keyboard.type('a?b/');
      assert.equal(await page.locator('[data-wb-search]').inputValue(), 'a?b/');
      assert.equal(await overlay.isHidden(), true);
      await page.locator('h1').click();
      await page.keyboard.press('Slash');
      assert.equal(await page.evaluate(() => document.activeElement?.hasAttribute('data-wb-search')), true);
      await page.locator('h1').click();
      await Promise.all([page.waitForURL(/\/workbench\/folders$/), page.keyboard.press('g').then(() => page.keyboard.press('f'))]);
      await ready();
    });

    await test('command palette jumps by id and searches all layers', async () => {
      await page.keyboard.press('Control+k');
      const input = page.locator('[data-wb-palette-input]');
      await input.waitFor({state: 'visible'});
      await input.fill(`a:${fixture.artifact}`);
      await page.locator('[data-wb-palette-entry="jump"]').first().waitFor();
      await Promise.all([page.waitForURL(new RegExp(`/artifact/${fixture.artifact}$`)), input.press('Enter')]);
      await ready(); await inert();
      await page.keyboard.press('Control+k');
      await input.fill('lukasz');
      await page.locator('[data-wb-palette-entry="search"]').waitFor();
      await page.locator('[data-wb-palette-entry="search"]').click();
      await page.waitForURL(/\/workbench\?q=lukasz$/); await ready(); await inert();
      assert.equal(await page.locator('[data-wb-hit][data-layer="annotation"]').count(), 1, 'ł folding finds Łukasz');
      await page.keyboard.press('Control+k');
      const recent = await page.locator('.wb-palette-list li').allTextContents();
      assert.ok(recent.some(text => text.includes('ostatnio oglądane')), 'recently viewed pages are offered');
      await page.keyboard.press('Escape');
    });

    await test('bulk tag flow: select all, add, audited summary, remove', async () => {
      await page.goto(`${base}/workbench?q=umowa&layer=event`); await ready();
      assert.equal(await page.locator('[data-wb-hit]').count(), 3);
      await page.locator('[data-wb-select-all]').check();
      await page.locator('[data-wb-tag-input]').fill('Sprawa A');
      await Promise.all([page.waitForURL(/done=\d+/), page.locator('[data-wb-tag-add]').click()]);
      const summary = await page.locator('.wb-done').textContent();
      assert.match(summary, /dodano tag/); assert.match(summary, /zmieniono 3/);
      let tags = await (await page.request.get(`${base}/api/workbench/tags`)).json();
      assert.deepEqual(tags.tags.map(t => [t.name, t.event_count]), [['Sprawa A', 3]]);
      await page.locator('[data-wb-target]').first().check();
      await page.locator('[data-wb-tag-input]').fill('Sprawa A');
      await Promise.all([page.waitForURL(/done=\d+/), page.locator('[data-wb-tag-remove]').click()]);
      tags = await (await page.request.get(`${base}/api/workbench/tags`)).json();
      assert.equal(tags.tags[0].event_count, 2);
      receipt.bulk_tag = {added: 3, removed: 1};
    });

    await test('folder add and remove keep evidence untouched; citation copies as text', async () => {
      const before = await (await page.request.get(`${base}/api/artifacts/${fixture.artifact}/citations`)).json();
      await page.goto(`${base}/workbench/folders`); await ready();
      await page.locator('[data-wb-folder-name]').fill('Teczka dla prawnika');
      await Promise.all([page.waitForURL(/\/workbench\/folders\/\d+$/), page.locator('[data-wb-folder-create] button').click()]);
      await ready();
      const form = page.locator('[data-wb-folder-add]');
      await form.locator('select[name="kind"]').selectOption('citation');
      await form.locator('[data-wb-item-id]').fill(String(fixture.anchor));
      await Promise.all([page.waitForURL(/added=citation/), form.locator('button').click()]);
      await ready(); await inert();
      assert.equal(await page.locator('[data-wb-item]').count(), 1);
      assert.ok((await page.locator('[data-wb-items]').textContent()).includes(`ageds:citation/${fixture.anchor}@derived_text/${fixture.version}`));
      await context.grantPermissions(['clipboard-read', 'clipboard-write'], {origin: base});
      await page.locator('[data-wb-copy-citation]').click();
      await page.waitForFunction(() => document.querySelector('[data-wb-copy-citation]')?.hasAttribute('data-wb-copied'));
      const mode = await page.locator('[data-wb-copy-citation]').getAttribute('data-wb-copied');
      const copied = mode === 'clipboard' ? await page.evaluate(() => navigator.clipboard.readText()) : await page.locator('[data-wb-copy-fallback]').inputValue();
      assert.ok(copied.includes(' umowa w Łodzi' + fixture.literal), 'quote verbatim');
      assert.ok(copied.includes(`ageds:citation/${fixture.anchor}@derived_text/${fixture.version}`));
      await inert();
      await Promise.all([page.waitForURL(/done=\d+/), page.locator('[data-wb-remove-item]').click()]);
      assert.equal(await page.locator('[data-wb-item]').count(), 0);
      const after = await (await page.request.get(`${base}/api/artifacts/${fixture.artifact}/citations`)).json();
      assert.deepEqual(after, before);
      receipt.copy_mode = mode;
    });

    await test('artifact page knowledge panel renders without disturbing the citation workspace', async () => {
      await page.goto(`${base}/artifact/${fixture.artifact}`); await ready();
      await page.waitForFunction(() => (document.querySelector('[data-status]')?.textContent || '').includes('Wersja #'));
      const panel = page.locator('[data-wb-artifact-panel]');
      await panel.locator('summary').click();
      assert.ok((await panel.textContent()).includes('Czego te rekordy nie twierdzą'));
      assert.equal(await page.locator('[data-saved] article').count(), 1);
      await inert();
    });

    assert.deepEqual(pageErrors, []); receipt.page_errors = pageErrors; receipt.console_errors = consoleErrors; receipt.status = 'passed';
  } catch (error) {receipt.status = 'failed'; receipt.error = error.stack; process.exitCode = 1; console.error(error); console.error(stderr.slice(-4000));}
  finally {if (browser) await browser.close(); server.kill('SIGTERM'); receipt.finished_at = new Date().toISOString(); if (process.env.AGEDS_BROWSER_RECEIPT) fs.writeFileSync(process.env.AGEDS_BROWSER_RECEIPT, JSON.stringify(receipt, null, 2) + '\n');}
})();
