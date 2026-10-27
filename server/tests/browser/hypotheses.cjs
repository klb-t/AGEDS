/** Real Chromium acceptance of /hypotheses on a temporary synthetic SQLite (never a live catalog).
 * Usage: NODE_PATH=<playwright> PYTHON=<venv python> AGEDS_BROWSER_EXECUTABLE=<chrome> node server/tests/browser/hypotheses.cjs
 * Optional: AGEDS_BROWSER_RECEIPT=/abs/path.json (outside the repository), AGEDS_BROWSER_SCREENSHOTS=/abs/dir. */
const {chromium} = require('playwright');
const {spawn, execFileSync} = require('node:child_process');
const {once} = require('node:events');
const net = require('node:net');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const repo = path.resolve(__dirname, '../../..');
const delay = ms => new Promise(r => setTimeout(r, ms));

// Fixture: generated density-burst NULL scenario (naive looks significant, normalized does not),
// one registered hypothesis with a run and candidate proposals, plus a hostile event body.
const FIXTURE = String.raw`
import json, os, tempfile, sys
from pathlib import Path
d = tempfile.mkdtemp(prefix='ageds-hyp-browser-')
os.environ.update(EW_DATA_DIR=d, EW_DB_PATH=str(Path(d) / 'evidence.sqlite'), EW_STORE_DIR=str(Path(d) / 'store'))
from server.app import db, evidence
from server.app.analysis import dossier, eventstudy, preregistration
from server.tests.corpus import hypotheses as corpus
import uvicorn
db.init_db()
scenario = corpus.SCENARIOS['density_burst_null']()
ids = corpus.populate(scenario)
source = evidence.ensure_source('synthetic', 'browser hostile text')
hostile = '<img src=x onerror="window.__sourceExecuted=1"> wizyta </script>'
evidence.add_event(source_id=source, artifact_id=None, event_type='health', body=hostile, ts_start='2025-03-01T10:00:00+00:00')
c = db.connect()
v = preregistration.register_hypothesis(c, corpus.definition_for(scenario, permutations=199), actor='browser-fixture')
run = eventstudy.run_event_study(c, v['version_id'])
cand = dossier.candidate_search(c, v['hypothesis_id'], queries=['onerror', 'wizyta'], use_rules=False, limit=20)
print(json.dumps({'hypothesis': v['hypothesis_id'], 'version': v['version_id'], 'run': run['run_id'],
                  'result_sha256': run['result_sha256'], 'bins': len(run['result']['lag_histogram']['observed']),
                  'nonzero_bins': sum(1 for x in run['result']['lag_histogram']['observed'] if x > 0),
                  'candidates': cand['added_item_ids'], 'dir': d}), flush=True)
c.close()
uvicorn.run('server.app.main:app', host='127.0.0.1', port=int(os.environ['AGEDS_BROWSER_PORT']), log_level='warning')
`;

(async () => {
  const socket = net.createServer(); socket.listen(0, '127.0.0.1'); await once(socket, 'listening');
  const port = socket.address().port; await new Promise(r => socket.close(r));
  const server = spawn(process.env.PYTHON || 'python', ['-c', FIXTURE], {cwd: repo, env: {...process.env, AGEDS_BROWSER_PORT: String(port), PYTHONPATH: repo}, stdio: ['ignore', 'pipe', 'pipe']});
  let stderr = ''; server.stderr.on('data', d => { stderr += d; });
  const receipt = {task: 'AGEDS-TASK-20261016-E (E2 hypotheses)', git_head: execFileSync('git', ['rev-parse', 'HEAD'], {cwd: repo, encoding: 'utf8'}).trim(),
    playwright: require('playwright/package.json').version, started_at: new Date().toISOString(), cases: []};
  let browser, fixtureDir;
  try {
    const fixture = await new Promise((resolve, reject) => {
      let buf = ''; server.stdout.on('data', d => { buf += d; if (buf.includes('\n')) { try { resolve(JSON.parse(buf.split('\n')[0])); } catch (e) { reject(e); } } });
      server.on('exit', code => reject(new Error(`fixture exited ${code}: ${stderr}`)));
    });
    fixtureDir = fixture.dir;
    const base = `http://127.0.0.1:${port}`;
    for (let i = 0; i < 200; i++) { try { if ((await fetch(base + '/hypotheses')).ok) break; } catch {} await delay(50); }
    browser = await chromium.launch({headless: true, executablePath: process.env.AGEDS_BROWSER_EXECUTABLE || undefined, args: ['--no-sandbox']});
    receipt.browser = browser.version();
    const page = await browser.newPage({viewport: {width: 1280, height: 900}});
    const pageErrors = []; page.on('pageerror', e => pageErrors.push(e.message));
    async function test(name, fn) { await fn(); receipt.cases.push({name, status: 'passed'}); console.log(`PASS ${name}`); }
    const shots = process.env.AGEDS_BROWSER_SCREENSHOTS;
    async function submitAndReload(button) { await Promise.all([page.waitForEvent('load', {timeout: 30000}), button.click()]); await reloadSettled(); }
    async function reloadSettled() { await page.waitForLoadState('load'); await page.waitForFunction(() => document.querySelector('[data-hypotheses-page]')?.dataset.chartsRendered === '1' || !document.getElementById('hyp-run-data')); }

    await test('page renders charts from the stored run, nav link present, no script errors', async () => {
      await page.goto(`${base}/hypotheses?id=${fixture.hypothesis}`);
      await reloadSettled();
      assert.equal(await page.locator('nav.ageds-nav a[href="/hypotheses"]').textContent(), 'Hipotezy');
      assert.equal(await page.locator('[data-chart=lag-histogram] path.hyp-bar').count(), fixture.nonzero_bins);
      assert.equal(await page.locator('[data-chart=lag-histogram] [data-hit-bin]').count(), fixture.bins);
      assert.equal(await page.locator('[data-chart=lag-histogram] [data-null-band]').count(), 1);
      assert.equal(await page.locator('[data-chart=control-series] path.hyp-line').count(), 2);
      assert.ok(await page.locator('[data-chart=control-series] line.hyp-stim').count() > 0);
      assert.equal(await page.locator('.hyp-run').getAttribute('data-result-sha256'), fixture.result_sha256);
      if (shots) await page.screenshot({path: path.join(shots, 'hypotheses-desktop.png'), fullPage: true});
      assert.deepEqual(pageErrors, []);
    });

    await test('every section carries the fixed disclaimer whose SHA-256 matches in the browser', async () => {
      const checks = await page.evaluate(async () => {
        const out = [];
        for (const section of document.querySelectorAll('section[data-section]')) {
          const label = section.querySelector('[data-disclaimer]');
          const text = label.querySelector('[lang=pl]').textContent;
          const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
          const hex = [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
          out.push({section: section.dataset.section, text, ok: hex === label.dataset.disclaimerSha256});
        }
        return out;
      });
      assert.deepEqual(checks.map(c => c.section), ['register', 'versions', 'event-study', 'dossier', 'theses', 'disclosure']);
      for (const c of checks) {
        assert.ok(c.ok, c.section);
        assert.equal(c.text, 'Wynik analizatora jest materiałem pomocniczym dla człowieka/biegłego, nie wnioskiem przyczynowym.');
      }
    });

    await test('warnings are always visible; detection bias is flagged for the density-burst null', async () => {
      const ids = await page.locator('[data-warnings] > li').evaluateAll(els => els.map(e => [e.dataset.warningId, e.dataset.flagged]));
      assert.deepEqual(ids.map(x => x[0]), ['causation', 'detection_bias', 'seasonality', 'clustering', 'dependence',
        'unknown_zone_excluded', 'time_uncertainty', 'time_basis', 'preregistration_deviation']);
      assert.ok(await page.locator('[data-warnings] > li').first().isVisible());
      assert.equal(Object.fromEntries(ids).detection_bias, '1');
      assert.match(await page.locator('[data-warning-id=detection_bias]').textContent(), /znormalizowany nie/);
      const naive = await page.locator('tr[data-window="after_3d"] [data-p-naive]').textContent();
      const normalized = await page.locator('tr[data-window="after_3d"] [data-p-normalized]').textContent();
      assert.ok(parseFloat(naive) <= 0.05 && parseFloat(normalized) >= 0.1, `${naive} / ${normalized}`);
    });

    await test('histogram hover shows the stored bin values in a tooltip (text only)', async () => {
      const hit = page.locator('[data-chart=lag-histogram] [data-hit-bin]').nth(Math.floor(fixture.bins / 2));
      await hit.hover();
      const tip = page.locator('[data-histogram] .hyp-tip');
      await tip.waitFor({state: 'visible'});
      assert.match(await tip.textContent(), /par obserwowanych/);
      await hit.focus();
      assert.match(await tip.textContent(), /pasmo zerowe/);
    });

    await test('source text in the dossier stays inert text', async () => {
      assert.equal(await page.evaluate(() => window.__sourceExecuted), undefined);
      const excerpts = await page.locator('td.hyp-excerpt').allTextContents();
      assert.ok(excerpts.some(t => t.includes('<img src=x onerror=')));
    });

    await test('register form previews the hash (verified with WebCrypto) and registers a new hypothesis', async () => {
      const form = page.locator('form[data-register-form]');
      const definition = JSON.parse(await form.locator('textarea[name=definition]').inputValue());
      definition.title = 'Browser-registered hypothesis';
      definition.analysis.permutations = 99;
      await form.locator('textarea[name=definition]').fill(JSON.stringify(definition, null, 1));
      await form.locator('[data-preview]').click();
      await page.waitForFunction(() => document.querySelector('form[data-register-form] [data-browser-hash]').textContent.startsWith('zgodny'));
      const serverHash = await form.locator('[data-preview-hash]').textContent();
      assert.match(serverHash, /^[0-9a-f]{64}$/);
      await submitAndReload(form.locator('button[type=submit]'));
      assert.notEqual(new URL(page.url()).searchParams.get('id'), String(fixture.hypothesis));
      assert.equal(await page.locator('[data-versions] [data-definition-sha256]').first().textContent(), serverHash);
      assert.equal(await page.locator('section[data-section=versions] h2').evaluate(e => e.firstChild.textContent.trim()), 'Browser-registered hypothesis');
      assert.equal(await page.locator('[data-versions] [data-first-analysed-at]').count(), 0);
      receipt.registered_hash = serverHash;
    });

    await test('changed definition becomes version 2 linked to version 1; unchanged is refused as no-op', async () => {
      const form = page.locator('form[data-version-form]');
      const definition = JSON.parse(await form.locator('textarea[name=definition]').inputValue());
      await form.locator('[data-preview]').click();
      await page.waitForFunction(() => document.querySelector('form[data-version-form] [data-browser-hash]').textContent.startsWith('zgodny'));
      await form.locator('button[type=submit]').click();
      await page.waitForFunction(() => document.querySelector('form[data-version-form] [data-form-status]').textContent.includes('Bez zmian'));
      definition.windows[1].end_s = 4 * 86400;
      await form.locator('textarea[name=definition]').fill(JSON.stringify(definition));
      await form.locator('input[name=change_note]').fill('wider window');
      await submitAndReload(form.locator('button[type=submit]'));
      const rows = page.locator('[data-versions] tr[data-version-id]');
      assert.equal(await rows.count(), 2);
      assert.match(await rows.nth(1).textContent(), /v2/);
      assert.match(await rows.nth(1).textContent(), /wider window/);
    });

    await test('event study runs from the form; overrides are shown as a preregistration deviation', async () => {
      const form = page.locator('form[data-run-form]');
      await form.locator('input[name=permutations]').fill('49');
      await submitAndReload(form.locator('button[type=submit]'));
      assert.match(page.url(), /run=\d+/);
      assert.equal(await page.locator('[data-warning-id=preregistration_deviation]').getAttribute('data-flagged'), '1');
      assert.equal(await page.locator('[data-versions] [data-first-analysed-at]').count(), 1);
      assert.ok(await page.locator('[data-chart=lag-histogram]').isVisible());
    });

    await test('human classification, thesis without evidence and disclosure are recorded', async () => {
      await page.goto(`${base}/hypotheses?id=${fixture.hypothesis}`);
      await reloadSettled();
      const item = fixture.candidates[0];
      const form = page.locator(`form[data-classify-form][data-item-id="${item}"]`);
      await form.locator('select[name=classification]').selectOption('supporting');
      await form.locator('input[name=rationale]').fill('matches the letter period');
      await submitAndReload(form.locator('button[type=submit]'));
      assert.equal(await page.locator(`tr[data-item-id="${item}"]`).getAttribute('data-classification'), 'supporting');
      assert.match(await page.locator(`tr[data-item-id="${item}"] details summary`).last().textContent(), /historia \(2\)/);
      const thesis = page.locator('form[data-thesis-form]');
      await thesis.locator('textarea[name=text]').fill('Documentation density explains the raw association.');
      await thesis.locator('select[name=status_type]').selectOption('speculation');
      await submitAndReload(thesis.locator('button[type=submit]'));
      const row = page.locator('[data-theses] tr[data-thesis-id]').last();
      assert.equal(await row.getAttribute('data-flags'), 'unsupported');
      const disclosure = page.locator('form[data-disclosure-form][data-quadrant=known_unknowns]');
      await disclosure.locator('textarea[name=text]').fill('Device clock zone of the letters is not documented.');
      await submitAndReload(disclosure.locator('button[type=submit]'));
      assert.match(await page.locator('.hyp-quadrant[data-quadrant=known_unknowns] .hyp-human').textContent(), /Device clock zone/);
      assert.ok(await page.locator('.hyp-quadrant[data-quadrant=known_unknowns] [data-fact=untimed_events_excluded]').count() === 1);
    });

    await test('phone width has no horizontal page scroll', async () => {
      await page.setViewportSize({width: 390, height: 844});
      await page.goto(`${base}/hypotheses?id=${fixture.hypothesis}`);
      await reloadSettled();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      assert.ok(overflow <= 1, `overflow ${overflow}px`);
      if (shots) await page.screenshot({path: path.join(shots, 'hypotheses-phone.png'), fullPage: false});
      assert.deepEqual(pageErrors, []);
    });
    receipt.finished_at = new Date().toISOString();
    receipt.result = 'passed';
  } catch (error) {
    receipt.result = 'failed';
    receipt.error = String(error && error.stack || error);
    console.error(error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
    server.kill('SIGTERM');
    if (fixtureDir && fixtureDir.includes('ageds-hyp-browser-')) fs.rmSync(fixtureDir, {recursive: true, force: true});
    if (process.env.AGEDS_BROWSER_RECEIPT) fs.writeFileSync(process.env.AGEDS_BROWSER_RECEIPT, JSON.stringify(receipt, null, 1));
  }
})();
