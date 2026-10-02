/** Production module/handlers, minimal synthetic DOM. This is NOT a browser test. */
import test from 'node:test';
import assert from 'node:assert/strict';

class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.dataset = {}; this.listeners = new Map(); this.textContent = ''; this.value = ''; this.disabled = false; }
  get options() { return this.children; }
  addEventListener(name, fn) { this.listeners.set(name, fn); }
  async emit(name) { await this.listeners.get(name)?.({target: this, preventDefault() {}}); }
  append(...nodes) { this.children.push(...nodes); if (this.tagName === 'select' && !this.value && nodes.length) this.value = nodes[0].value; }
  prepend(node) { this.children.unshift(node); }
  replaceChildren(...nodes) { this.children = []; this.value = ''; this.append(...nodes); }
  querySelectorAll(selector) { if (selector === 'option[value=""]') return this.children.filter(x => x.value === ''); throw new Error(`Unimplemented selector ${selector}`); }
  remove() {}
}
class Audio extends EventTarget {
  plays = 0; paused = true; readyState = 1; duration = 10; currentTime = 0;
  pause() { this.paused = true; this.dispatchEvent(new Event('pause')); }
  async play() { this.plays++; this.paused = false; this.dispatchEvent(new Event('play')); }
}
const deferred = () => { let resolve; const promise = new Promise(r => resolve = r); return {promise, resolve}; };
const wordSelector = (segment = 0) => ({kind: 'words', word_refs: [{segment_index: segment, word_index: 0}], text_join: 'concatenate_exact', time_unit: 'seconds', stored_time_unit: 'milliseconds', rounding: 'nearest_ms', precision: 'word_asr', source_start: .0005, source_end: .0025, alignment_verification: 'not_performed'});
const segmentSelector = (segment = 0) => ({kind: 'segments', indices: [segment], text_join: 'concatenate_exact', time_unit: 'seconds', stored_time_unit: 'milliseconds', rounding: 'nearest_ms', precision: 'segment'});
const response = (selector = wordSelector()) => ({id: 81, artifact_id: 12, derived_text_id: 17, quote_text: ' echo', start_ms: 0, end_ms: 2, selector});
let sequence = 0;
async function setup(t, kind = 'words') {
  const nodes = Object.fromEntries(['version', 'kind', 'first', 'last', 'preview', 'status', 'save', 'play', 'stop', 'saved'].map(name => [name, new Element(['version', 'kind', 'first', 'last'].includes(name) ? 'select' : 'div')]));
  nodes.version.value = '17'; nodes.kind.value = kind;
  const root = new Element(); root.dataset.artifactId = '12';
  root.querySelector = selector => nodes[/^\[data-(.+)\]$/.exec(selector)?.[1]] ?? null;
  root.querySelectorAll = () => [];
  const audio = new Audio(), win = new Element(), selectors = new Map();
  selectors.set('[data-citation-workspace]', root); selectors.set('[data-evidence-audio]', audio);
  for (const name of ['transcript-text', 'annotation-version', 'version-summaries', 'annotation-history']) selectors.set(`[data-${name}]`, new Element());
  const pages = new Element(); pages.textContent = JSON.stringify(Object.fromEntries(['versions', 'citations', 'annotations'].map(name => [name, {artifactId: 12, items: [], nextBeforeId: null, snapshotMaxId: 0, hasMore: false, limit: 50}])));
  selectors.set('[data-history-pages]', pages);
  for (const name of ['versions', 'citations', 'annotations']) for (const field of ['more', 'status']) selectors.set(`[data-history-${field}="${name}"]`, new Element());
  const transcript = {id: 17, artifactId: 12, text: ' echo echo', segments: [0, 1].map(() => ({start: .0005, end: .0025, text: ' echo', words: [{start: .0005, end: .0025, word: ' echo'}]})), wordTiming: {segments: [0, 1].map(segment_index => ({segment_index, word_selection_available: true}))}};
  const newer = {id: 18, artifactId: 12, text: ' NEW', segments: [{start: 1, end: 2, text: ' NEW'}]};
  let saved = response(), pending = null; const posted = [];
  const original = {document: globalThis.document, window: globalThis.window, fetch: globalThis.fetch};
  globalThis.document = {querySelector: selector => selectors.get(selector) ?? null, createElement: tag => new Element(tag)};
  globalThis.window = win;
  globalThis.fetch = async (url, options) => {
    if (options?.method === 'POST') { posted.push(JSON.parse(options.body)); const body = pending ? await pending.promise : structuredClone(saved); return {ok: true, json: async () => body}; }
    if (url.includes('/transcript?')) return {ok: true, json: async () => url.endsWith('=18') ? newer : transcript};
    throw new Error(`Unexpected fetch ${url}`);
  };
  t.after(async () => { await win.emit('pagehide'); for (const [key, value] of Object.entries(original)) { if (value === undefined) delete globalThis[key]; else globalThis[key] = value; } });
  await import(`../../app/static/citations.mjs?handler-test=${++sequence}`);
  for (let i = 0; i < 20 && !nodes.status.textContent.startsWith('Wersja #17.'); i++) await new Promise(setImmediate);
  assert.match(nodes.status.textContent, /^Wersja #17\./);
  return {nodes, audio, transcript, posted, history: selectors.get('[data-history-status="citations"]'),
    reply(body) { saved = body; pending = null; }, hold() { pending = deferred(); return pending; },
    async switchVersion() { nodes.version.value = '18'; await nodes.version.emit('change'); }};
}

for (const kind of ['words', 'segments']) test(`handler rejects other ${kind} occurrence before history or links; same ID remains admissible`, async t => {
  const h = await setup(t, kind), selector = kind === 'words' ? wordSelector : segmentSelector;
  h.reply(response(selector(1))); await h.nodes.save.emit('click');
  assert.match(h.nodes.status.textContent, /Nie udostępniono/);
  assert.doesNotMatch(h.nodes.status.textContent, /Zapisano cytat/);
  assert.equal(h.nodes.saved.children.length, 0); assert.match(h.history.textContent, /^0 pozycji/); assert.equal(h.audio.plays, 0);
  // Reusing ID 81 proves the rejected response was not silently entered into pager state.
  h.reply(response(selector(0))); await h.nodes.save.emit('click');
  assert.equal(h.nodes.saved.children.length, 1); assert.match(h.history.textContent, /^1 pozycji/);
  assert.match(h.nodes.status.textContent, /Zapisano cytat #81/);
  const button = h.nodes.saved.children[0].children[2]; assert.equal(button.dataset.startMs, '0'); assert.equal(button.dataset.endMs, '2');
  assert.equal(h.audio.plays, 0);
});

test('handler rejects changed response range despite identical version, selector and text', async t => {
  const h = await setup(t); h.reply({...response(), start_ms: 1}); await h.nodes.save.emit('click');
  assert.equal(h.nodes.saved.children.length, 0); assert.match(h.history.textContent, /^0 pozycji/); assert.doesNotMatch(h.nodes.status.textContent, /Zapisano cytat/);
});

test('pending save compares immutable pre-request selection while controls and source objects mutate', async t => {
  const h = await setup(t), held = h.hold(); const saving = h.nodes.save.emit('click');
  h.nodes.first.value = '1'; h.nodes.last.value = '1'; await h.nodes.first.emit('change');
  h.transcript.segments[0].words[0].start = 5; h.transcript.segments[0].words[0].word = ' mutated';
  held.resolve(response()); await saving;
  assert.deepEqual(h.posted[0].wordRefs, [{segment_index: 0, word_index: 0}]);
  assert.equal(h.nodes.saved.children.length, 1); assert.equal(h.nodes.saved.children[0].children[1].textContent, ' echo');
  assert.match(h.nodes.status.textContent, /Zapisano cytat #81/);
});

for (const valid of [true, false]) test(`late ${valid ? 'valid' : 'mismatched'} save preserves new version status and ${valid ? 'retains pinned history' : 'does not publish history'}`, async t => {
  const h = await setup(t), held = h.hold(); const saving = h.nodes.save.emit('click');
  h.nodes.kind.value = 'segments'; await h.nodes.kind.emit('change'); await h.switchVersion();
  const status = h.nodes.status.textContent; assert.match(status, /^Wersja #18\./);
  held.resolve(response(wordSelector(valid ? 0 : 1))); await saving;
  assert.equal(h.nodes.status.textContent, status); assert.equal(h.nodes.version.value, '18'); assert.equal(h.nodes.preview.textContent, ' NEW');
  assert.equal(h.nodes.saved.children.length, valid ? 1 : 0); assert.equal(h.audio.plays, 0);
  if (valid) assert.equal(h.nodes.saved.children[0].children[2].dataset.derivedTextId, '17');
});
