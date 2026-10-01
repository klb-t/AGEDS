import test from 'node:test';
import assert from 'node:assert/strict';
import {
  snapshotCitationSelection, validateCreatedCitation, citationMilliseconds,
  validateCitationIdentity, validateHistoryItem,
} from '../../app/static/citations.mjs';

// Independent expected wire fixtures: deliberately not copied from the helper's selector.
const common = {
  text_join: 'concatenate_exact', time_unit: 'seconds',
  stored_time_unit: 'milliseconds', rounding: 'nearest_ms',
};
const wordSelector = () => ({
  kind: 'words', word_refs: [{segment_index: 0, word_index: 0}, {segment_index: 0, word_index: 1}],
  ...common, precision: 'word_asr', source_start: 0, source_end: 0.5,
  alignment_verification: 'not_performed',
});
const segmentSelector = () => ({kind: 'segments', indices: [0, 1], ...common, precision: 'segment'});
const wordUnits = () => [
  {text: ' yes', start: 0, end: 0.25, segmentIndex: 0, wordIndex: 0},
  {text: ' yes\n', start: 0.25, end: 0.5, segmentIndex: 0, wordIndex: 1},
];
const segmentUnits = () => [
  {text: ' yes', start: 0, end: 0.25, segmentIndex: 0},
  {text: ' yes\n', start: 0.25, end: 0.5, segmentIndex: 1},
];
const snapshot = (kind = 'words', picked = kind === 'words' ? wordUnits() : segmentUnits()) =>
  snapshotCitationSelection({artifactId: 12, transcriptId: 17, kind, picked});
const saved = (kind = 'words') => ({
  id: 9, artifact_id: 12, derived_text_id: 17, quote_text: ' yes yes\n',
  start_ms: 0, end_ms: 500, selector: kind === 'words' ? wordSelector() : segmentSelector(),
});
const clone = x => JSON.parse(JSON.stringify(x));
const rejectedMutation = (kind, mutate) => {
  const body = saved(kind); mutate(body);
  assert.throws(() => validateCreatedCitation(body, snapshot(kind)));
};

for (const kind of ['words', 'segments']) {
  test(`create ${kind}: positive exact raw quote, range, identity and canonical selector`, () => {
    const expected = snapshot(kind), body = saved(kind);
    assert.equal(validateCreatedCitation(body, expected), body);
    assert.deepEqual(expected.selector, body.selector);
    assert.equal(expected.quoteText, ' yes yes\n');
    assert.equal(expected.startMs, 0); assert.equal(expected.endMs, 500);
    const request = kind === 'words' ? {derivedTextId: 17, quoteText: ' yes yes\n', wordRefs: wordSelector().word_refs} :
      {derivedTextId: 17, quoteText: ' yes yes\n', segmentIndices: [0, 1]};
    assert.deepEqual(expected.requestBody, request);
  });
  test(`create ${kind}: same text and interval at another occurrence is refused`, () => {
    rejectedMutation(kind, b => {
      if (kind === 'words') b.selector.word_refs = [{segment_index: 1, word_index: 0}, {segment_index: 1, word_index: 1}];
      else b.selector.indices = [2, 3];
    });
  });
  test(`create ${kind}: ordered selection cannot be reversed, duplicated, shortened or extended`, () => {
    for (const change of [a => a.reverse(), a => {a[1] = clone(a[0]);}, a => a.pop(), a => a.push(clone(a.at(-1)))]) {
      rejectedMutation(kind, b => change(kind === 'words' ? b.selector.word_refs : b.selector.indices));
    }
  });
  test(`create ${kind}: canonical metadata is exact including missing and extra keys`, () => {
    for (const key of Object.keys(saved(kind).selector)) {
      rejectedMutation(kind, b => {delete b.selector[key];});
      rejectedMutation(kind, b => {b.selector[key] = null;});
    }
    for (const [key, value] of Object.entries({kind: 'characters', text_join: 'space', time_unit: 'milliseconds', stored_time_unit: 'seconds', rounding: 'floor', precision: 'verified_audio'})) {
      rejectedMutation(kind, b => {b.selector[key] = value;});
    }
    rejectedMutation(kind, b => {b.selector.extra = 'not canonical';});
  });
  test(`create ${kind}: response identities, exact raw text and range cannot change`, () => {
    for (const [key, values] of Object.entries({
      id: [0, -1, false, true, '9', 1.5, Number.MAX_SAFE_INTEGER + 1, null],
      artifact_id: [13, '12', true], derived_text_id: [18, '17', true],
      quote_text: ['yes yes', ' yes yes\r\n', false, null],
      start_ms: [1, '0', false, -1, NaN, Infinity], end_ms: [499, 501, '500', false, NaN, Infinity],
    })) for (const value of values) rejectedMutation(kind, b => {b[key] = value;});
  });
  test(`create ${kind}: JSON object key order is irrelevant`, () => {
    const body = saved(kind);
    body.selector = Object.fromEntries(Object.entries(body.selector).reverse());
    if (kind === 'words') body.selector.word_refs = body.selector.word_refs.map(ref => Object.fromEntries(Object.entries(ref).reverse()));
    assert.equal(validateCreatedCitation(body, snapshot(kind)), body);
  });
  test(`create ${kind}: defensive snapshot does not alias mutable selection`, () => {
    const picked = kind === 'words' ? wordUnits() : segmentUnits();
    const expected = snapshot(kind, picked), original = saved(kind);
    picked[0].text = 'changed'; picked[0].start = 30; picked[0].segmentIndex = 123;
    picked[0].wordIndex = 123; picked.reverse(); picked.push({...picked[0]});
    assert.equal(validateCreatedCitation(original, expected), original);
    const visit = value => {
      if (!value || typeof value !== 'object') return;
      assert.ok(Object.isFrozen(value));
      for (const child of Object.values(value)) visit(child);
    };
    visit(expected);
    assert.throws(() => {expected.selector.kind = 'changed';}, TypeError);
    assert.throws(() => {expected.requestBody.quoteText = 'changed';}, TypeError);
  });
}

test('word references reject booleans, numeric strings and extra fields without coercion', () => {
  for (const key of ['segment_index', 'word_index']) {
    for (const value of [false, true, '0', null, -1, 0.5, Number.MAX_SAFE_INTEGER + 1]) {
      rejectedMutation('words', b => {b.selector.word_refs[0][key] = value;});
    }
  }
  rejectedMutation('words', b => {b.selector.word_refs[0].extra = 0;});
});
test('segment indices reject booleans and numeric strings without coercion', () => {
  for (const value of [false, true, '0', null, -1, 0.5, Number.MAX_SAFE_INTEGER + 1]) {
    rejectedMutation('segments', b => {b.selector.indices[0] = value;});
  }
});
test('word source bounds and alignment status remain part of exact response contract', () => {
  for (const [key, values] of Object.entries({
    source_start: [0.0001, '0', false, null], source_end: [0.5001, '0.5', false, null],
    alignment_verification: ['verified', false, null],
  })) for (const value of values) rejectedMutation('words', b => {b.selector[key] = value;});
});
test('numeric JSON zero and zero-point-zero are equivalent but quoted zero is not', () => {
  const body = JSON.parse(JSON.stringify(saved()).replace('"source_start":0', '"source_start":0.0').replace('"segment_index":0', '"segment_index":0.0'));
  assert.equal(validateCreatedCitation(body, snapshot()), body);
  body.selector.source_start = '0.0';
  assert.throws(() => validateCreatedCitation(body, snapshot()));
});
test('milliseconds use Python ties-to-even, not JavaScript Math.round', () => {
  for (const [seconds, milliseconds] of [[0,0], [0.0005,0], [0.0015,2], [0.0025,2], [1.0005,1000], [1.0015,1002]]) {
    assert.equal(citationMilliseconds(seconds), milliseconds, String(seconds));
  }
  assert.equal(citationMilliseconds(0.000499999999), 0);
  assert.equal(citationMilliseconds(0.000500000001), 1);
});
test('time conversion refuses unsafe milliseconds and invalid numeric inputs', () => {
  for (const value of ['0', false, true, null, undefined, NaN, Infinity, -Infinity, -0.1, Number.MAX_VALUE, Number.MAX_SAFE_INTEGER / 1000 + 1]) {
    assert.throws(() => citationMilliseconds(value), String(value));
  }
});
test('half-millisecond selection validates rounded times and preserves raw bounds', () => {
  const expected = snapshot('words', [{text: 'yes', start: 0.0005, end: 0.0025, segmentIndex: 0, wordIndex: 0}]);
  const body = {...saved(), quote_text: 'yes', start_ms: 0, end_ms: 2,
    selector: {...wordSelector(), word_refs: [{segment_index: 0, word_index: 0}], source_start: 0.0005, source_end: 0.0025}};
  assert.equal(validateCreatedCitation(body, expected), body);
  assert.throws(() => validateCreatedCitation({...body, start_ms: 1}, expected));
  assert.throws(() => validateCreatedCitation({...body, end_ms: 3}, expected));
});
test('create selection refuses bad kinds, empty picks and unsafe source numbers', () => {
  assert.throws(() => snapshotCitationSelection({artifactId:12, transcriptId:17, kind:'anything', picked:wordUnits()}));
  assert.throws(() => snapshot('words', []));
  for (const [key, values] of Object.entries({start: ['0', false, NaN, -1], end: ['1', Infinity, Number.MAX_SAFE_INTEGER], segmentIndex:['0', false, -1], wordIndex:['0', false, -1]})) {
    for (const value of values) {
      const picked = wordUnits(); picked[0][key] = value;
      assert.throws(() => snapshot('words', picked), `${key}=${String(value)}`);
    }
  }
});
test('history identity remains compatible with historical minimal selector rows', () => {
  const historic = {...saved(), selector: {legacy: 'opaque'}};
  assert.equal(validateCitationIdentity(historic, 12, 17), historic);
  assert.equal(validateHistoryItem(historic, 'citations', 12), historic);
  assert.throws(() => validateCreatedCitation(historic, snapshot()));
});
