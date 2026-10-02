import test from 'node:test';
import assert from 'node:assert/strict';
import {snapshotCitationSelection, validateCreatedCitation, citationMilliseconds} from '../../app/static/citations.mjs';

function result(expected) {
  return {id: 9, artifact_id: expected.artifactId, derived_text_id: expected.transcriptId,
    quote_text: expected.quoteText, start_ms: expected.startMs, end_ms: expected.endMs,
    selector: structuredClone(expected.selector)};
}

test('request and exact word projection are copied before source units change', () => {
  const picked = [{text: ' yes', start: 0.0005, end: 0.0015, segmentIndex: 2, wordIndex: 3}];
  const expected = snapshotCitationSelection({artifactId: 1, transcriptId: 2, kind: 'words', picked});
  picked[0].text = ' no'; picked[0].wordIndex = 4; picked.push({...picked[0]});
  assert.equal(expected.quoteText, ' yes');
  assert.deepEqual(expected.requestBody, {derivedTextId: 2, quoteText: ' yes', wordRefs: [{segment_index: 2, word_index: 3}]});
  assert.equal(expected.startMs, 0); assert.equal(expected.endMs, 2);
  assert.equal(expected.selector.source_start, 0.0005);
  assert.throws(() => { expected.requestBody.wordRefs[0].word_index = 7; }, TypeError);
  assert.throws(() => { expected.selector.source_start = 1; }, TypeError);
  assert.equal(validateCreatedCitation(result(expected), expected).id, 9);
});

test('segment occurrence cannot be replaced by repeated raw text at the same interval', () => {
  const expected = snapshotCitationSelection({artifactId: 1, transcriptId: 2, kind: 'segments',
    picked: [{text: 'same', start: 0, end: 0, segmentIndex: 3}]});
  assert.ok(!Object.hasOwn(expected.selector, 'source_start'));
  const wrong = result(expected); wrong.selector.indices = [7];
  assert.throws(() => validateCreatedCitation(wrong, expected));
  assert.equal(validateCreatedCitation(result(expected), expected).start_ms, 0);
});

test('millisecond projection rounds the actual binary product to even without epsilon', () => {
  assert.deepEqual([0.0005, 0.0015, 0.0025, 1.0005, 1.0015].map(citationMilliseconds), [0, 2, 2, 1000, 1002]);
  assert.equal(citationMilliseconds(0.0004999999999999999), 0);
  assert.equal(citationMilliseconds(0.0005000000000000001), 1);
  for (const invalid of [true, '0', null, undefined, NaN, Infinity, -0.1, Number.MAX_VALUE]) {
    assert.throws(() => citationMilliseconds(invalid));
  }
});

test('snapshot rejects malformed selected unit identity and noncontiguous segment order', () => {
  const base = {artifactId: 1, transcriptId: 2, kind: 'segments', picked: [{text: 'x', start: 0, end: 1, segmentIndex: 0}]};
  for (const segmentIndex of ['0', false, -1, 1.5, Number.MAX_SAFE_INTEGER + 1]) {
    assert.throws(() => snapshotCitationSelection({...base, picked: [{...base.picked[0], segmentIndex}]}));
  }
  assert.throws(() => snapshotCitationSelection({...base, picked: [...base.picked, {text: 'y', start: 1, end: 2, segmentIndex: 2}]}));
});
