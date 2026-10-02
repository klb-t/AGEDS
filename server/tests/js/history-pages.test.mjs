import test from 'node:test';
import assert from 'node:assert/strict';
import {createHistoryPager, validateHistoryPage, validateHistoryItem, HISTORY_CAP} from '../../app/static/citations.mjs';

const item = (id, kind = 'versions') => kind === 'versions' ? {id, artifact_id: 7, run_id: null, model: 'literal <img>', language: null, created_at: 'raw-time'} :
  kind === 'citations' ? {id, artifact_id: 7, derived_text_id: 3, quote_text: ' raw <script>\n', start_ms: 1, end_ms: 4} :
  {id, artifactId: 7, derivedTextId: 3, kind: 'note', label: null, body: ' raw <img>\n', createdAt: 'raw-time', startMs: null, endMs: null};
const page = (ids, {kind = 'versions', snapshot = 10, more = false, limit = 2} = {}) => ({artifactId: 7, items: ids.map(id => item(id, kind)), nextBeforeId: more ? ids.at(-1) : null, snapshotMaxId: snapshot, hasMore: more, limit});
const check = (body, opts = {}) => validateHistoryPage(body, {kind: 'versions', artifactId: 7, ...opts});

test('each concrete row shape keeps literal source strings and ownership', () => {
  for (const kind of ['versions', 'citations', 'annotations']) {
    const body = page([10, 8], {kind, more: true});
    assert.equal(validateHistoryPage(body, {kind, artifactId: 7}), body);
    assert.equal(validateHistoryItem(body.items[0], kind, 7), body.items[0]);
  }
});

test('empty zero snapshot and terminal short page valid; null snapshots rejected', () => {
  check(page([], {snapshot: 0})); check(page([1]));
  assert.throws(() => check(page([], {snapshot: null})));
  assert.throws(() => check(page([1], {snapshot: 0})));
});

test('byte-limited nonterminal page may contain fewer items than requested', () => {
  check(page([10], {more: true, limit: 50}));
});

test('strict envelope schema and bounds refuse malformed responses', () => {
  const changes = [p => p.extra = 1, p => delete p.items, p => p.items = {}, p => p.limit = true,
    p => p.limit = 101, p => p.limit = 0, p => p.hasMore = 1, p => p.artifactId = 8,
    p => p.snapshotMaxId = Number.MAX_SAFE_INTEGER + 1, p => p.items.push(item(9)),
    p => p.nextBeforeId = 1, p => p.items = [item(10), item(10)], p => p.items.reverse(),
    p => p.items[0].id = '10', p => p.items[0].artifact_id = 8,
    p => p.items[0].run_id = Number.MAX_SAFE_INTEGER + 1, p => p.items[0].text = 'full text is not a summary'];
  for (const mutate of changes) {
    const body = page([10, 8]); mutate(body);
    assert.throws(() => check(body));
  }
});

test('ownership, unsafe foreign IDs, times and literal text types checked per row kind', () => {
  for (const kind of ['citations', 'annotations']) {
    for (const mutate of [x => x.id = false, x => x.id = Number.MAX_SAFE_INTEGER + 1,
      x => x[kind === 'citations' ? 'derived_text_id' : 'derivedTextId'] = '3',
      x => x[kind === 'citations' ? 'quote_text' : 'body'] = {html: 'bad'},
      x => x[kind === 'citations' ? 'end_ms' : 'endMs'] = -1]) {
      const value = item(10, kind); mutate(value); assert.throws(() => validateHistoryItem(value, kind, 7));
    }
  }
});

test('continuation requires fixed snapshot, decreasing cursor, exact limit and no seen IDs', () => {
  const opts = {beforeId: 8, snapshotMaxId: 10, limit: 2, seen: new Set([10, 8])};
  check(page([7, 4], {more: true}), opts);
  for (const body of [page([8, 7]), page([7], {snapshot: 11}), page([7], {limit: 3}), page([7], {more: true})]) {
    if (body.items[0].id === 7 && body.hasMore) body.nextBeforeId = 6;
    assert.throws(() => check(body, opts));
  }
  assert.throws(() => check(page([], {more: true}), opts));
  assert.throws(() => check(page([7]), {...opts, seen: new Set([7])}));
});

test('invalid page has no partial state change and retries same cursor', () => {
  const pager = createHistoryPager('versions', 7, page([10, 8], {more: true}));
  const request = pager.begin(); assert.equal(pager.begin(), null);
  const bad = page([7, 6]); bad.items[1].artifact_id = 99;
  assert.throws(() => pager.accept(request, bad)); assert.equal(pager.count, 2);
  pager.fail(request);
  const retry = pager.begin(); assert.equal(retry.beforeId, 8);
  assert.deepEqual(pager.accept(retry, page([7, 6])).map(x => x.id), [7, 6]);
  assert.equal(pager.count, 4); assert.equal(pager.hasMore, false); assert.equal(pager.begin(), null);
});

test('stale, forged, duplicate and closed requests cannot replace active cursor state', () => {
  const pager = createHistoryPager('versions', 7, page([10, 8], {more: true}));
  const old = pager.begin(); pager.fail(old); const current = pager.begin();
  assert.equal(pager.accept(old, page([7])), null);
  assert.equal(pager.accept({...current}, page([7])), null);
  assert.throws(() => { current.beforeId = 100; });
  assert.equal(pager.count, 2); assert.equal(pager.busy, true);
  pager.close(); assert.equal(pager.accept(current, page([7])), null); assert.equal(pager.begin(), null);
});

test('1000-row cap reports omission only when more exists; exact complete boundary stays complete', () => {
  for (const more of [false, true]) {
    const first = Array.from({length: 100}, (_, i) => 1000 - i);
    const pager = createHistoryPager('versions', 7, page(first, {snapshot: 1000, more: true, limit: 100}));
    for (let start = 900; start >= 100; start -= 100) {
      const request = pager.begin();
      pager.accept(request, page(Array.from({length: 100}, (_, i) => start - i), {snapshot: 1000, more: start > 100 || more, limit: 100}));
    }
    assert.equal(pager.count, HISTORY_CAP); assert.equal(pager.capped, more); assert.equal(pager.hasMore, more);
    assert.equal(pager.begin(), null); assert.equal(pager.addNew(item(1001)), false);
  }
});

test('new saved rows count toward cap without changing pinned older snapshot or cursor', () => {
  const pager = createHistoryPager('citations', 7, page([10, 8], {kind: 'citations', more: true}));
  assert.equal(pager.addNew(item(11, 'citations')), true);
  assert.equal(pager.addNew(item(11, 'citations')), false);
  const request = pager.begin(); assert.equal(request.snapshotMaxId, 10); assert.equal(request.beforeId, 8);
  pager.accept(request, page([7], {kind: 'citations'})); assert.equal(pager.count, 4);
});

test('save during older-page request cannot overflow client cap and retry shrinks requested limit', () => {
  const pager = createHistoryPager('citations', 7, page([10, 8], {kind: 'citations', more: true}));
  const request = pager.begin();
  for (let id = 11; id < 1008; id++) pager.addNew(item(id, 'citations'));
  assert.equal(pager.count, 999);
  assert.throws(() => pager.accept(request, page([7, 6], {kind: 'citations'})));
  assert.equal(pager.count, 999); pager.fail(request);
  const retry = pager.begin(); assert.equal(retry.limit, 1); assert.equal(retry.beforeId, 8);
  pager.accept(retry, page([7], {kind: 'citations', limit: 1, more: true}));
  assert.equal(pager.count, 1000); assert.equal(pager.capped, true); assert.equal(pager.begin(), null);
});
