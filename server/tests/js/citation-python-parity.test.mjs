import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {citationMilliseconds, snapshotCitationSelection, validateCreatedCitation} from '../../app/static/citations.mjs';

const repo = fileURLToPath(new URL('../../../', import.meta.url));
const result = spawnSync(process.env.PYTHON || 'python3', ['-c', `
import json,math,random
from server.app.citations import milliseconds,projection_from_segments,projection_from_words
r=random.Random(20261001)
values=[0,0.0005,0.0015,0.0025,1.0005,1.0015]
for i in range(1000):
    half=(r.randrange(10000000)+0.5)/1000
    values.extend([math.nextafter(half,0),half,math.nextafter(half,math.inf)])
values.extend([9007199254740.99,9007199254740.992])
segments=[{'start':0,'end':0.0015,'text':' Aż','words':[
    {'start':0,'end':0.0005,'word':' A'}, {'start':0.0005,'end':0.0015,'word':'ż'}]},
    {'start':0.0025,'end':1.0005,'text':' 👋','words':[
    {'start':0.0025,'end':1.0005,'word':' 👋'}]}]
projections=[projection_from_segments(segments,[0,1]),projection_from_words(segments,[
    {'segment_index':0,'word_index':1},{'segment_index':1,'word_index':0}])]
print(json.dumps({'times':[[v,milliseconds(v)] for v in values], 'segments':segments,'projections':projections},ensure_ascii=False))
`], {cwd:repo,encoding:'utf8',env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'},maxBuffer:1024*1024});
assert.equal(result.status,0,result.stderr || String(result.error));
const fixture = JSON.parse(result.stdout);

test('browser milliseconds agree with actual Python projection at 3008 seeded IEEE-754 boundaries', () => {
  assert.equal(fixture.times.length,3008);
  for (const [seconds, expected] of fixture.times) {
    if (Number.isSafeInteger(expected)) assert.equal(citationMilliseconds(seconds),expected,`seconds=${seconds}`);
    else assert.throws(() => citationMilliseconds(seconds));
  }
});

for (const kind of ['segments','words']) {
  test(`browser ${kind} snapshot agrees with actual Python canonical projection`, () => {
    const picked = kind === 'segments'
      ? fixture.segments.map((s,segmentIndex) => ({...s,segmentIndex}))
      : [{...fixture.segments[0].words[1],text:fixture.segments[0].words[1].word,segmentIndex:0,wordIndex:1},
         {...fixture.segments[1].words[0],text:fixture.segments[1].words[0].word,segmentIndex:1,wordIndex:0}];
    const expected=snapshotCitationSelection({artifactId:12,transcriptId:17,kind,picked});
    const projection=fixture.projections[kind === 'segments' ? 0 : 1];
    assert.equal(expected.quoteText,projection.quote_text);
    assert.equal(expected.startMs,projection.start_ms);
    assert.equal(expected.endMs,projection.end_ms);
    assert.deepEqual(expected.selector,projection.selector);
    const response={id:31,artifact_id:12,derived_text_id:17,...projection};
    assert.equal(validateCreatedCitation(response,expected),response);
  });
}
