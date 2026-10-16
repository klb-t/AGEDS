"""Optional real-Chromium check of /catalog (skipped when node/Playwright/Chromium are unavailable)."""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
import time

import pytest

from server.app import db as dbmod
from server.app import main
from server.tests.test_catalog_scan import make_world, tree_state

SCRIPT = r'''
const {chromium} = require('playwright');
(async () => {
  const [base, collection, shot] = process.argv.slice(2);
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox'],
    executablePath: process.env.AGEDS_BROWSER_EXECUTABLE || undefined});
  const page = await browser.newPage();
  const errors = []; let dialogs = 0;
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('dialog', d => { dialogs += 1; d.dismiss(); });
  const out = {browser: browser.version()};
  await page.goto(`${base}/catalog?collection=${collection}`);
  out.title = await page.title();
  out.rows = await page.locator('table.catalog-entries tbody tr').count();
  const mark = page.locator('form[data-locator="docs/report.pdf"]');
  await mark.locator('input[name=priority]').fill('7');
  await mark.locator('input[name=note]').fill('browser mark');
  await Promise.all([page.waitForNavigation(), mark.locator('button').click()]);
  out.markPriority = await page.locator('form[data-locator="docs/report.pdf"] input[name=priority]').inputValue();
  const update = page.locator('form[data-catalog-action="collection-update"]');
  await update.locator('textarea[name=notes]').fill('notes from browser');
  await Promise.all([page.waitForNavigation(), update.locator('button').click()]);
  out.notes = await page.locator('form[data-catalog-action="collection-update"] textarea[name=notes]').inputValue();
  const explain = page.locator('form[data-catalog-action="explain"]');
  await explain.locator('select').selectOption('fake');
  await Promise.all([page.waitForNavigation(), explain.locator('button').click()]);
  out.annotations = await page.locator('.catalog-annotation').count();
  const decision = page.locator('form[data-catalog-action="decision"]').first();
  await decision.locator('input[name=actor]').fill('browser reviewer');
  await Promise.all([page.waitForNavigation(), decision.locator('button[value=promoted]').click()]);
  out.decision = (await page.locator('.catalog-annotation b').first().textContent()).trim();
  await page.locator('form[data-catalog-action="explain"] select').selectOption('none');
  await page.locator('form[data-catalog-action="explain"] button').click();
  await page.waitForFunction(() => document.getElementById('catalog-status').textContent.includes('Profil zapisany'));
  const create = page.locator('form[data-catalog-action="collection-create"]');
  await page.locator('details summary', {hasText: 'Nowa kolekcja'}).click();
  await create.locator('input[name=label]').fill('Browser collection');
  await create.locator('input[name=priority]').fill('3');
  await Promise.all([page.waitForNavigation(), create.locator('button').click()]);
  out.treeHasNew = await page.locator('.catalog-tree', {hasText: 'Browser collection'}).count();
  await page.goto(`${base}/catalog?collection=${collection}&conflict=true`);
  out.conflictRows = await page.locator('table.catalog-entries tbody tr').count();
  await page.goto(`${base}/catalog?collection=${collection}&limit=10`);
  await Promise.all([page.waitForNavigation(), page.locator('a[rel=next]').click()]);
  out.secondPageFirstId = Number(await page.locator('table.catalog-entries tbody tr td').first().textContent());
  await page.setViewportSize({width: 1400, height: 900});
  await page.goto(`${base}/catalog?collection=${collection}&minGap=100`);
  await page.screenshot({path: shot.replace('mobile', 'desktop'), fullPage: true});
  await page.setViewportSize({width: 390, height: 844});
  await page.goto(`${base}/catalog?collection=${collection}`);
  out.mobileScrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
  out.wide = await page.evaluate(() => [...document.querySelectorAll('body *')].filter(el => el.getBoundingClientRect().right > 392)
    .slice(0, 12).map(el => `${el.tagName}.${el.className}#${Math.round(el.getBoundingClientRect().right)}`));
  await page.screenshot({path: shot});
  out.dialogs = dialogs; out.errors = errors;
  console.log(JSON.stringify(out));
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
'''


def _node_env():
    node, npm = shutil.which('node'), shutil.which('npm')
    if not node or not npm:
        return None
    root = subprocess.run([npm, 'root', '-g'], capture_output=True, text=True, timeout=60).stdout.strip()
    env = {'NODE_PATH': root, 'PATH': __import__('os').environ.get('PATH', '')}
    for key in ('PLAYWRIGHT_BROWSERS_PATH', 'AGEDS_BROWSER_EXECUTABLE', 'HOME'):
        value = __import__('os').environ.get(key)
        if value:
            env[key] = value
    probe = subprocess.run([node, '-e', "require('playwright')"], env=env, capture_output=True, timeout=60)
    return (node, env) if probe.returncode == 0 else None


@pytest.fixture
def served(tmp_path, monkeypatch):
    found = _node_env()
    if found is None:
        pytest.skip('node with Playwright is not available')
    import uvicorn
    world = make_world(tmp_path, monkeypatch)
    monkeypatch.setattr(main, 'settings', world.settings)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(main.app, host='127.0.0.1', port=port, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    world.base, world.node = f'http://127.0.0.1:{port}', found
    yield world
    server.should_exit = True
    thread.join(timeout=10)


def test_catalog_page_in_real_chromium(served, tmp_path):
    node, env = served.node
    script = tmp_path / 'catalog-browser.cjs'
    script.write_text(SCRIPT)
    shot = tmp_path / 'catalog-mobile.png'
    process = subprocess.run([node, str(script), served.base, str(served.a['id']), str(shot)], env=env,
                             capture_output=True, text=True, timeout=180)
    if process.returncode != 0 and 'Executable doesn' in process.stderr:
        pytest.skip('Chromium executable for Playwright is not installed')
    assert process.returncode == 0, process.stderr[-3000:]
    out = json.loads(process.stdout.strip().splitlines()[-1])
    assert out['title'] == 'Katalog źródeł' and out['rows'] > 50
    assert out['markPriority'] == '7' and out['notes'] == 'notes from browser'
    assert out['annotations'] >= 1 and out['decision'] == 'promoted'
    assert out['treeHasNew'] == 1 and out['conflictRows'] == len(served.expected['type_conflicts'])
    assert out['secondPageFirstId'] > 10 and out['dialogs'] == 0 and out['errors'] == []
    assert out['mobileScrollWidth'] <= 390, (out['mobileScrollWidth'], out['wide'])
    assert shot.stat().st_size > 0
    with dbmod.session() as db:
        assert db.execute("SELECT priority, note FROM catalog_marks ORDER BY id DESC LIMIT 1").fetchone()[:] == (7, 'browser mark')
        assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action='collection_updated'").fetchone()[0] == 1
    assert tree_state(served.corpus) == served.before
