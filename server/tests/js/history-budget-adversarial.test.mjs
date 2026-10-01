import test from 'node:test';
import assert from 'node:assert/strict';
import {createHistoryPager, HISTORY_PAYLOAD_BYTES} from '../../app/static/citations.mjs';

const row = (id, text = '') => ({id, artifact_id:7, derived_text_id:3, quote_text:text, start_ms:1, end_ms:4});
const cost = item => new TextEncoder().encode(JSON.stringify(item)).byteLength;
const page = (items, more = false, snapshot = 100, limit = 100) => ({artifactId:7,items,nextBeforeId:more ? items.at(-1)?.id : null,snapshotMaxId:snapshot,hasMore:more,limit});
const pager = (items, maxPayloadBytes, more = false) => createHistoryPager('citations',7,page(items,more),{maxPayloadBytes});

test('actual JSON UTF-8 cost preserves escapes, combining marks and supplementary characters', () => {
  const item=row(10,'ą🐈e\u0301\n\t"\\\u0000');
  const bytes=cost(item);
  assert.ok(bytes>JSON.stringify(item).length);
  const p=pager([item],bytes);
  assert.equal(p.retainedPayloadBytes,bytes);
  assert.deepEqual(p.initialItems,[item]);
  assert.equal(p.reachedPayloadLimit,false);
  assert.equal(p.count,1);
});

test('one-byte-over initial row is omitted explicitly with no smaller later-row skip', () => {
  const large=row(10,'a'.repeat(100)),small=row(9,'');
  const p=pager([large,small],cost(large)-1);
  assert.deepEqual(p.initialItems,[]);
  assert.equal(p.retainedPayloadBytes,0);assert.equal(p.count,0);
  assert.equal(p.reachedPayloadLimit,true);assert.equal(p.hasMore,true);assert.equal(p.begin(),null);
});

test('initial prefix admission stops at first unfit row and keeps exact raw text', () => {
  const a=row(10,' raw\n'),b=row(9,'x'.repeat(100)),c=row(8,'');
  const p=pager([a,b,c],cost(a)+cost(b)-1);
  assert.deepEqual(p.initialItems,[a]);assert.equal(p.retainedPayloadBytes,cost(a));
  assert.equal(p.count,1);assert.equal(p.reachedPayloadLimit,true);assert.equal(p.begin(),null);
});

test('exact terminal budget fits without false omission; continuing exact budget stops', () => {
  const a=row(10,'ą'),b=row(9,'🐈');const budget=cost(a)+cost(b);
  const terminal=pager([a,b],budget);
  assert.equal(terminal.reachedPayloadLimit,false);assert.equal(terminal.hasMore,false);
  const continuing=pager([a,b],budget,true);
  assert.equal(continuing.reachedPayloadLimit,true);assert.equal(continuing.begin(),null);
});

test('aggregate cost includes prior pages and admits only the fitting continuation prefix', () => {
  const a=row(10,'A'),b=row(9,'B'),c=row(8,'C');
  const p=pager([a],cost(a)+cost(b),true);const request=p.begin();
  assert.deepEqual(p.accept(request,page([b,c])),[b]);
  assert.equal(p.retainedPayloadBytes,cost(a)+cost(b));assert.equal(p.count,2);
  assert.equal(p.reachedPayloadLimit,true);assert.equal(p.begin(),null);
});

test('invalid row beyond fitting prefix rejects entire page before accounting mutation', () => {
  const a=row(10),b=row(9),bad={...row(8),artifact_id:9};
  const p=pager([a],cost(a)+cost(b),true);const request=p.begin();
  assert.throws(()=>p.accept(request,page([b,bad])));
  assert.equal(p.count,1);assert.equal(p.retainedPayloadBytes,cost(a));
  p.fail(request);assert.equal(p.begin().beforeId,10);
});

test('local saved row during older request consumes budget before continuation admission', () => {
  const a=row(10),saved=row(101),b=row(9),c=row(8);
  const p=pager([a],cost(a)+cost(saved)+cost(b),true);const request=p.begin();
  assert.equal(p.addNew(saved),true);
  assert.deepEqual(p.accept(request,page([b,c])),[b]);
  assert.equal(p.count,3);assert.equal(p.retainedPayloadBytes,cost(a)+cost(saved)+cost(b));
  assert.equal(p.reachedPayloadLimit,true);assert.equal(p.begin(),null);
});

test('oversized saved row cannot bypass cap or permit a later smaller row to skip it', () => {
  const a=row(10),saved=row(101,'x'.repeat(100)),smaller=row(102);
  const p=pager([a],cost(a)+cost(saved)-1,true);
  assert.equal(p.addNew(saved),false);assert.equal(p.retainedPayloadBytes,cost(a));
  assert.equal(p.reachedPayloadLimit,true);assert.equal(p.addNew(smaller),false);assert.equal(p.begin(),null);
});

test('duplicate save costs no second allocation in retained budget', () => {
  const a=row(10),saved=row(101);const p=pager([a],cost(a)+cost(saved)+1000,true);
  assert.equal(p.addNew(saved),true);const bytes=p.retainedPayloadBytes;
  assert.equal(p.addNew(saved),false);assert.equal(p.retainedPayloadBytes,bytes);assert.equal(p.count,2);
});

test('closed stale responses cannot change accounting or admit bytes into replacement pager', () => {
  const a=row(10),b=row(9);const p=pager([a],cost(a)+cost(b),true);const request=p.begin();p.close();
  const replacement=pager([row(20)],1000,true);
  assert.equal(p.accept(request,page([b])),null);assert.equal(p.retainedPayloadBytes,cost(a));
  assert.equal(replacement.count,1);assert.equal(replacement.retainedPayloadBytes,cost(row(20)));
});

test('refresh starts a new independent budget after exhausted old pager', () => {
  const a=row(10),huge=row(9,'x'.repeat(100));const old=pager([a,huge],cost(a));
  assert.equal(old.reachedPayloadLimit,true);old.close();
  const fresh=pager([row(20)],1000,true);
  assert.equal(fresh.reachedPayloadLimit,false);assert.ok(fresh.begin());
});

test('default real four-MiB ceiling bounds multiple actual large model rows', () => {
  assert.equal(HISTORY_PAYLOAD_BYTES,4*1024*1024);
  const items=[10,9,8,7,6].map(id=>row(id,'x'.repeat(1024*1024-200)));
  const p=createHistoryPager('citations',7,page(items));
  assert.equal(p.count,4);assert.equal(p.reachedPayloadLimit,true);
  assert.equal(p.retainedPayloadBytes,items.slice(0,4).reduce((n,item)=>n+cost(item),0));
  assert.ok(p.retainedPayloadBytes<=HISTORY_PAYLOAD_BYTES);assert.equal(p.begin(),null);
});
