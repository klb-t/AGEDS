import test from 'node:test';
import assert from 'node:assert/strict';
import {createHistoryPager, historyRowBytes} from '../../app/static/citations.mjs';

const row = (id, quote = 'Zażółć 😀 "literal"\n') => ({id, artifact_id: 7, derived_text_id: 3,
  quote_text: quote, start_ms: 0, end_ms: 1});
const page = (items, more = false) => ({artifactId: 7, items, snapshotMaxId: 9,
  nextBeforeId: more ? items.at(-1).id : null, hasMore: more, limit: 2});

test('UTF8 row cost includes JSON escaping and source Unicode exactly', () => {
  const value = row(9);
  assert.equal(historyRowBytes(value), Buffer.byteLength(JSON.stringify(value), 'utf8'));
  assert.ok(historyRowBytes(value) > JSON.stringify(value).length);
  assert.equal(value.quote_text, 'Zażółć 😀 "literal"\n');
});

test('first unfit row stops initial prefix and no smaller later row is silently substituted', () => {
  const first = row(9, 'a'.repeat(200));
  const state = createHistoryPager('citations', 7, page([first, row(8, 'x')]), {maxPayloadBytes: historyRowBytes(first) - 1});
  assert.deepEqual(state.initialItems, []);
  assert.equal(state.retainedPayloadBytes, 0);
  assert.equal(state.reachedPayloadLimit, true);
  assert.equal(state.hasMore, true);
  assert.equal(state.begin(), null);
});

test('closed view refuses late save without changing measured retention', () => {
  const initial = row(9);
  const state = createHistoryPager('citations', 7, page([initial], true));
  const bytes = state.retainedPayloadBytes;
  state.close(); assert.equal(state.addNew(row(10)), false);
  assert.equal(state.retainedPayloadBytes, bytes); assert.equal(state.count, 1);
});
