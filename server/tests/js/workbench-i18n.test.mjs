import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {DEFAULT_LANGUAGE, LANGUAGES, createTranslator, formatTemplate, loadTranslator, negotiate, parseAcceptLanguage, pluralCategory} from '../../app/static/workbench-i18n.mjs';

const load = lang => JSON.parse(readFileSync(new URL(`../../app/static/i18n/${lang}.json`, import.meta.url), 'utf8'));

test('catalogs share keys and placeholders with Polish (nav.<path> optional)', () => {
  const pl = load('pl');
  const placeholders = v => new Set([...(typeof v === 'string' ? [v] : Object.values(v)).join(' ').matchAll(/\{([a-zA-Z_]\w*)\}/g)].map(m => m[1]));
  for (const lang of LANGUAGES) {
    const catalog = load(lang);
    for (const k of Object.keys(pl)) if (k !== '_meta') {
      assert.ok(Object.hasOwn(catalog, k), `${lang} misses ${k}`);
      assert.deepEqual(placeholders(catalog[k]), placeholders(pl[k]), `${lang} ${k}`);
    }
    for (const k of Object.keys(catalog)) if (k !== '_meta' && !k.startsWith('nav./')) assert.ok(Object.hasOwn(pl, k), `${lang} extra ${k}`);
  }
  assert.equal(DEFAULT_LANGUAGE, 'pl');
});

test('plural rules match the Python helper', () => {
  const expected = {1: 'one', 2: 'few', 4: 'few', 5: 'many', 12: 'many', 14: 'many', 22: 'few', 25: 'many', 0: 'many', 112: 'many', 1.5: 'other'};
  for (const [n, category] of Object.entries(expected)) assert.equal(pluralCategory('pl', Number(n)), category, n);
  assert.equal(pluralCategory('en', 1), 'one');
  assert.equal(pluralCategory('nl', 2), 'other');
  const t = createTranslator({lang: 'pl', catalog: load('pl')});
  assert.equal(t('index.search.count', {n: 3}), '3 wyniki');
  assert.equal(t('index.search.count', {n: 5}), '5 wyników');
});

test('missing keys fall back to Polish visibly and are reported once; never empty', () => {
  const reports = [];
  const t = createTranslator({lang: 'en', catalog: {'a': 'A {x}', 'empty': ''}, fallback: {'b': 'B-pl', 'empty': 'pusty'},
    report: (level, key, lang) => reports.push([level, key, lang])});
  assert.equal(t('a', {x: '<b>'}), 'A <b>', 'formatting does not interpret markup; callers use textContent');
  assert.equal(t('b'), 'B-pl');
  assert.equal(t('b'), 'B-pl');
  assert.equal(t('empty'), 'pusty');
  assert.equal(t('zzz'), '⟦zzz⟧');
  assert.equal(t('_meta'), '⟦_meta⟧');
  assert.deepEqual(reports, [['warn', 'b', 'en'], ['warn', 'empty', 'en'], ['error', 'zzz', 'en'], ['error', '_meta', 'en']]);
  assert.equal(formatTemplate('{a} {b}', {a: 1}), '1 {b}', 'unknown placeholders stay visible');
});

test('negotiation precedence and bounded Accept-Language parsing', () => {
  assert.deepEqual(negotiate({query: 'en', cookie: 'nl'}), {lang: 'en', source: 'query'});
  assert.deepEqual(negotiate({query: 'xx', cookie: 'nl'}), {lang: 'nl', source: 'cookie'});
  assert.deepEqual(negotiate({acceptLanguage: 'de, en;q=0.4, nl;q=0.5'}), {lang: 'nl', source: 'accept-language'});
  assert.deepEqual(negotiate({navigatorLanguages: ['de-DE', 'en-GB']}), {lang: 'en', source: 'navigator'});
  assert.deepEqual(negotiate({}), {lang: 'pl', source: 'default'});
  assert.deepEqual(parseAcceptLanguage('en;q=0, pl'), [['pl', 1]]);
  assert.deepEqual(parseAcceptLanguage('x'.repeat(2000)), []);
  assert.deepEqual(parseAcceptLanguage('nl;q=oops, <x>, en'), [['en', 1]]);
});

test('loadTranslator degrades to visible keys when fetch fails', async () => {
  const t = await loadTranslator('en', {fetchImpl: async () => {throw new Error('offline');}, report: () => {}});
  assert.equal(t('wb.title'), '⟦wb.title⟧');
  const ok = await loadTranslator('nl', {fetchImpl: async url => ({ok: true, json: async () => load(url.includes('/nl.') ? 'nl' : 'pl')}), report: () => {}});
  assert.equal(ok('wb.title'), 'Werkplaats');
  const unsupported = await loadTranslator('de', {fetchImpl: async () => ({ok: true, json: async () => load('pl')}), report: () => {}});
  assert.equal(unsupported('wb.title'), 'Warsztat');
});
