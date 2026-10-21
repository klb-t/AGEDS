import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {KeyRegistry, comboFromEvent, isEditable, normalizeCombo, parseSequence, renderHelp} from '../../app/static/workbench-keys.mjs';

const bindingsFile = JSON.parse(readFileSync(new URL('../../app/static/workbench-keys.json', import.meta.url), 'utf8'));
const key = (k, extra = {}) => ({key: k, ctrlKey: false, altKey: false, metaKey: false, shiftKey: false, target: {closest: () => null}, prevented: false,
  preventDefault() {this.prevented = true;}, ...extra});

test('combos normalise modifiers, printable characters and named keys', () => {
  assert.equal(normalizeCombo('Ctrl+K'), 'Ctrl+k');
  assert.equal(normalizeCombo('control+k'), 'Ctrl+k');
  assert.equal(normalizeCombo('cmd+K'), 'Meta+k');
  assert.equal(normalizeCombo('?'), '?');
  assert.equal(normalizeCombo('Shift+?'), '?', 'shift is part of the produced character');
  assert.equal(normalizeCombo('Shift+Tab'), 'Shift+Tab');
  assert.equal(normalizeCombo('+'), '+');
  assert.equal(normalizeCombo('Ctrl++'), 'Ctrl++');
  assert.equal(normalizeCombo('esc'), 'Escape');
  assert.equal(normalizeCombo('Space'), 'Space');
  assert.deepEqual(parseSequence('g  h'), ['g', 'h']);
  assert.throws(() => normalizeCombo('Hyper+x'), /unknown modifier/);
  assert.throws(() => normalizeCombo(''), /empty/);
  assert.equal(comboFromEvent(key('K', {ctrlKey: true, shiftKey: true})), 'Ctrl+k');
  assert.equal(comboFromEvent(key('?', {shiftKey: true})), '?');
  assert.equal(comboFromEvent(key(' ')), 'Space');
  assert.equal(comboFromEvent(key('Shift', {shiftKey: true})), null);
});

test('default bindings load without conflicts, including the timeline reservations', () => {
  for (const page of ['/', '/timeline', '/workbench']) {
    const registry = new KeyRegistry({page});
    for (const r of bindingsFile.reserved) registry.reserve(r);
    for (const b of bindingsFile.bindings) assert.equal(registry.register(b).ok, true, `${page} ${b.id}`);
    assert.deepEqual(registry.conflicts, []);
  }
});

test('conflicts are recorded, never silently shadowed; prefixes count as conflicts', () => {
  const registry = new KeyRegistry({page: '/timeline'});
  registry.reserve({page: '/timeline', owner: 'timeline', keys: ['ArrowLeft', '+']});
  assert.equal(registry.register({id: 'a', keys: ['g h']}).ok, true);
  const prefix = registry.register({id: 'b', keys: ['g']});
  assert.equal(prefix.ok, false);
  assert.equal(prefix.clashes[0].with, 'a');
  assert.equal(registry.register({id: 'c', keys: ['ArrowLeft']}).ok, false);
  assert.equal(registry.register({id: 'd', keys: ['ArrowLeft'], scope: '/workbench'}).ok, true, 'other page scope');
  assert.equal(registry.register({id: 'e', keys: ['Escape'], scope: 'overlay'}).ok, true);
  assert.equal(registry.conflicts.length, 2);
  assert.deepEqual(registry.list().map(b => b.id), ['a', 'd', 'e']);
});

test('sequences, timeouts, overlay scope and editable targets', () => {
  let now = 0; const fired = [];
  const registry = new KeyRegistry({timeoutMs: 1000, now: () => now, page: '/'});
  registry.register({id: 'go-home', keys: ['g h'], handler: () => fired.push('home')});
  registry.register({id: 'help', keys: ['?'], handler: () => fired.push('help')});
  registry.register({id: 'palette', keys: ['Ctrl+K', 'Meta+K'], handler: () => fired.push('palette')});
  registry.register({id: 'close', keys: ['Escape'], scope: 'overlay', handler: () => fired.push('close')});
  assert.equal(registry.handle(key('g')), null);
  now = 500; const h = key('h');
  assert.equal(registry.handle(h), 'go-home'); assert.equal(h.prevented, true);
  registry.handle(key('g')); now = 2000;
  assert.equal(registry.handle(key('h')), null, 'sequence expired');
  registry.handle(key('g'));
  assert.equal(registry.handle(key('?', {shiftKey: true})), 'help', 'a broken sequence restarts with the new key');
  assert.equal(registry.handle(key('k', {metaKey: true})), 'palette');
  assert.equal(registry.handle(key('Escape')), null, 'overlay bindings are inactive without an overlay');
  assert.equal(registry.handle(key('Escape'), {overlayOpen: true}), 'close');
  assert.equal(registry.handle(key('?'), {overlayOpen: true}), null, 'global bindings pause while an overlay is open');
  const typing = key('?', {target: {closest: selector => selector.includes('input') ? {} : null}});
  assert.equal(registry.handle(typing), null);
  assert.equal(typing.prevented, false, 'typing is never intercepted');
  assert.equal(isEditable({isContentEditable: true, closest: () => null}), true);
  assert.equal(isEditable(null), false);
  assert.deepEqual(fired, ['home', 'help', 'palette', 'close']);
});

test('help rendering uses text nodes only', () => {
  const created = [];
  const node = tag => {const el = {tag, children: [], attrs: {}, textContent: '', className: '',
    append(...items) {this.children.push(...items);}, replaceChildren() {this.children = [];}, setAttribute(k, v) {this.attrs[k] = v;}};
    created.push(el); return el;};
  const doc = {createElement: node};
  const container = node('div');
  const registry = new KeyRegistry();
  registry.register({id: 'x', keys: ['?'], label: 'keys.help'});
  registry.register({id: 'y', keys: ['?'], label: '<img src=x onerror=alert(1)>'});
  renderHelp(doc, container, registry, (k, p) => p ? `${k}:${JSON.stringify(p)}` : k);
  assert.equal(created.some(el => 'innerHTML' in el), false);
  const warning = container.children.at(-1);
  assert.equal(warning.attrs['data-wb-key-conflicts'], '1');
  assert.ok(created.some(el => el.tag === 'kbd' && el.textContent === '?'));
});
