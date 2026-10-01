import test from 'node:test';
import assert from 'node:assert/strict';
import {
  parseDomId, requireApiId, validateTranscriptIdentity,
  validateCitationIdentity, validatePacketPath,
} from '../../app/static/citations.mjs';

const MAX = Number.MAX_SAFE_INTEGER;
const transcript = (artifactId = 7, id = 41) => ({artifactId, id});
const citation = (artifact_id = 7, derived_text_id = 41, id = 9) => ({artifact_id, derived_text_id, id});

test('DOM canonical decimal IDs preserve both supported boundaries exactly', () => {
  assert.equal(parseDomId('1'), 1);
  assert.equal(parseDomId('9007199254740991'), MAX);
});

test('DOM refuses unsafe SQLite64 values before Number conversion', () => {
  for (const value of ['9007199254740992', '9007199254740993', '9223372036854775807', '18446744073709551615', '9'.repeat(10000)]) {
    assert.throws(() => parseDomId(value), /nie zostanie zaokrąglone/);
  }
});

test('DOM refuses coercions, malformed and noncanonical decimal IDs', () => {
  for (const value of [false, true, 1, 1n, null, undefined, {}, [], '', '0', '-1', '+1', '01', ' 1', '1 ', '1.0', '1e2', '0x10', 'NaN', 'Infinity', '1/2', '１２', '\n1']) {
    assert.throws(() => parseDomId(value));
  }
});

test('legacy API requires safe positive numeric IDs without coercion', () => {
  assert.equal(requireApiId(1), 1);
  assert.equal(requireApiId(MAX), MAX);
  for (const value of [false, true, '1', String(MAX), 1n, null, undefined, {}, [], 0, -0, -1, 1.5, NaN, Infinity, -Infinity, MAX + 1]) {
    assert.throws(() => requireApiId(value));
  }
});

test('unsafe JSON numeric response cannot alias an adjacent SQLite identity', () => {
  for (const decimal of ['9007199254740992', '9007199254740993', '9223372036854775807']) {
    const body = JSON.parse(`{"artifactId":7,"id":${decimal}}`);
    assert.throws(() => validateTranscriptIdentity(body, 7, 41));
    assert.throws(() => validateTranscriptIdentity(body, 7, body.id));
  }
});

test('transcript response must match captured artifact and selected version', () => {
  const body = transcript();
  assert.equal(validateTranscriptIdentity(body, 7, 41), body);
  assert.equal(validateTranscriptIdentity(transcript(MAX, MAX), MAX, MAX).id, MAX);
  for (const invalid of [transcript(8), transcript(7, 42), transcript('7'), transcript(7, '41'), transcript(false), {}, null]) {
    assert.throws(() => validateTranscriptIdentity(invalid, 7, 41));
  }
});

test('citation result requires safe citation ID and exact artifact/version ownership', () => {
  const body = citation();
  assert.equal(validateCitationIdentity(body, 7, 41), body);
  assert.equal(validateCitationIdentity(citation(MAX, MAX, MAX), MAX, MAX).id, MAX);
  for (const invalid of [citation(8), citation(7, 42), citation(7, 41, MAX + 1), citation('7'), citation(7, '41'), citation(7, 41, '9'), citation(7, 41, false), {}, null]) {
    assert.throws(() => validateCitationIdentity(invalid, 7, 41));
  }
});

test('saved citation retains its captured version and exact unedited quote', () => {
  const saved = {...citation(7, 41), quote_text: '  raw <text>\n'};
  assert.equal(validateCitationIdentity(saved, 7, 41, '  raw <text>\n'), saved);
  assert.throws(() => validateCitationIdentity(saved, 7, 42, '  raw <text>\n'));
  assert.throws(() => validateCitationIdentity(saved, 7, 41, 'raw <text>'));
  assert.throws(() => validateCitationIdentity(saved, 7, 41, null));
});

test('packet exports only exact canonical paths matching checked IDs', () => {
  const path = '/api/artifacts/7/citations/9/packet';
  assert.equal(validatePacketPath(path, 7, 9), path);
  const boundary = `/api/artifacts/${MAX}/citations/${MAX}/packet`;
  assert.equal(validatePacketPath(boundary, MAX, MAX), boundary);
  for (const invalid of [null, false, '/api/artifacts/8/citations/9/packet', '/api/artifacts/7/citations/10/packet', '/api/artifacts/07/citations/9/packet', '/api/artifacts/7/citations/9007199254740993/packet', path + '?id=8', path + '#x', 'https://other.example' + path, '//other.example' + path, '/api/artifacts/7/citations/9/packet/']) {
    assert.throws(() => validatePacketPath(invalid, 7, 9));
  }
});
