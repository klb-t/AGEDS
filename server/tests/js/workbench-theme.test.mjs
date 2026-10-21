import test from 'node:test';
import assert from 'node:assert/strict';
import {COOKIE, THEMES, applyTheme, effectiveTheme, nextPreference, prefersLight, readCookie, readPreference, supportsLight, writeCookie} from '../../app/static/workbench-theme.mjs';
import {RECENT_KEY, RECENT_MAX, readRecent, recordRecent} from '../../app/static/workbench-shell.mjs';

class Root {
  attrs = {};
  getAttribute(name) {return this.attrs[name] ?? null;}
  setAttribute(name, value) {this.attrs[name] = String(value);}
  removeAttribute(name) {delete this.attrs[name];}
}

test('preference cycle, effective theme and support gating', () => {
  assert.deepEqual(THEMES, ['auto', 'dark', 'light']);
  assert.equal(nextPreference('auto'), 'light');
  assert.equal(nextPreference('light'), 'dark');
  assert.equal(nextPreference('dark'), 'auto');
  assert.equal(nextPreference('neon'), 'auto');
  assert.equal(effectiveTheme('auto', true), 'light');
  assert.equal(effectiveTheme('auto', false), 'dark', 'dark is the default look');
  assert.equal(effectiveTheme('dark', true), 'dark');
  assert.equal(effectiveTheme('light', false, false), 'dark', 'pages without light support stay dark');
  const root = new Root();
  assert.equal(supportsLight(root), false);
  root.setAttribute('data-theme-support', 'light dark');
  assert.equal(supportsLight(root), true);
  applyTheme(root, 'light'); assert.equal(root.getAttribute('data-theme'), 'light');
  applyTheme(root, 'auto'); assert.equal(root.getAttribute('data-theme'), null);
  assert.equal(prefersLight({matchMedia: () => ({matches: true})}), true);
  assert.equal(prefersLight({matchMedia: () => {throw new Error('blocked');}}), false);
  assert.equal(prefersLight(undefined), false);
});

test('cookie storage is optional: blocked cookies never throw and fall back to auto', () => {
  const doc = {cookie: ''};
  Object.defineProperty(doc, 'cookie', {get() {return this._c || '';}, set(v) {this._c = (this._c ? this._c + '; ' : '') + v.split(';')[0];}});
  assert.equal(writeCookie(doc, COOKIE, 'light'), true);
  assert.equal(readPreference(doc), 'light');
  const blocked = {};
  Object.defineProperty(blocked, 'cookie', {get() {throw new Error('SecurityError');}, set() {throw new Error('SecurityError');}});
  assert.equal(writeCookie(blocked, COOKIE, 'dark'), false);
  assert.equal(readCookie(blocked, COOKIE), null);
  assert.equal(readPreference(blocked), 'auto');
  assert.equal(readPreference({cookie: `${COOKIE}=neon`}), 'auto');
});

test('recently viewed list is bounded, local-only and tolerant of broken storage', () => {
  const store = new Map();
  const storage = {getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, v)};
  for (let i = 0; i < RECENT_MAX + 5; i++) recordRecent(storage, {path: `/artifact/${i}`, title: `A${i}`});
  recordRecent(storage, {path: '/artifact/3', title: 'again'});
  const list = readRecent(storage);
  assert.equal(list.length, RECENT_MAX);
  assert.equal(list[0].title, 'again');
  assert.equal(list.filter(e => e.path === '/artifact/3').length, 1);
  store.set(RECENT_KEY, JSON.stringify([{path: '//evil.example.org'}, {path: 'https://example.org'}, {path: '/ok'}, null, 'x']));
  assert.deepEqual(readRecent(storage).map(e => e.path), ['/ok']);
  store.set(RECENT_KEY, '{not json');
  assert.deepEqual(readRecent(storage), []);
  const throwing = {getItem() {throw new Error('denied');}, setItem() {throw new Error('denied');}};
  assert.deepEqual(readRecent(throwing), []);
  assert.equal(recordRecent(throwing, {path: '/x'}), null);
  assert.deepEqual(readRecent(null), []);
});
