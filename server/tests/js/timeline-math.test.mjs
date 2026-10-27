import test from 'node:test';
import assert from 'node:assert/strict';
import * as M from '../../app/static/timeline-math.mjs';

const T = (...parts) => Date.UTC(...parts);

test('zoom follows the ladder and keeps the anchor time fixed', () => {
  const view = {start: T(2024, 0, 1), end: T(2024, 0, 2)};
  const zoomed = M.zoom(view, -1);
  assert.equal(zoomed.end - zoomed.start, 12 * M.HOUR);
  assert.equal((zoomed.start + zoomed.end) / 2, (view.start + view.end) / 2);
  const anchored = M.zoom(view, -1, 0.25);
  const pivot = view.start + (view.end - view.start) * 0.25;
  assert.equal(anchored.start + (anchored.end - anchored.start) * 0.25, pivot);
  assert.equal(M.zoom(view, 1).end - M.zoom(view, 1).start, 3 * M.DAY);
  let out = view;
  for (let i = 0; i < 50; i++) out = M.zoom(out, -1);
  assert.equal(out.end - out.start, M.MINUTE);
  for (let i = 0; i < 50; i++) out = M.zoom(out, 1);
  assert.equal(out.end - out.start, M.ZOOM_SPANS.at(-1));
  assert.ok(out.start >= M.MIN_TIME && out.end <= M.MAX_TIME);
});

test('pan shifts by a fraction of the span and clamps to the axis', () => {
  const view = {start: T(2024, 0, 1), end: T(2024, 0, 2)};
  assert.deepEqual(M.pan(view, 0.25), {start: view.start + 6 * M.HOUR, end: view.end + 6 * M.HOUR});
  const edge = M.pan({start: M.MAX_TIME - M.DAY, end: M.MAX_TIME}, 1);
  assert.equal(edge.end, M.MAX_TIME);
  assert.equal(edge.end - edge.start, M.DAY);
});

test('time and pixel mapping round-trip', () => {
  const view = {start: 1000, end: 11000};
  assert.equal(M.timeToX(6000, view, 500), 250);
  assert.equal(M.xToTime(250, view, 500), 6000);
  assert.deepEqual(M.selectionRange(400, 100, view, 500), {start: 3000, end: 9000});
  assert.deepEqual(M.selectionRange(-50, 900, view, 500), {start: 1000, end: 11000});
});

test('calendar buckets match the server (UTC months, Monday weeks)', () => {
  assert.equal(M.floorUnit(T(2024, 1, 29, 13, 5), 'month'), T(2024, 1, 1));
  assert.equal(M.floorUnit(T(2024, 1, 29, 13, 5), 'year'), T(2024, 0, 1));
  assert.equal(new Date(M.floorUnit(T(2024, 1, 29, 13, 5), 'week')).getUTCDay(), 1);
  assert.equal(M.floorUnit(T(1969, 11, 31, 23, 30), 'hour'), T(1969, 11, 31, 23));
  assert.equal(M.addUnit(T(2024, 0, 31), 'month'), T(2024, 2, 2));
  assert.equal(M.addUnit(T(2024, 0, 1), 'month', 13), T(2025, 1, 1));
  assert.equal(M.bucketCount(T(2023, 11, 15), T(2024, 1, 2), 'month'), 3);
  assert.equal(M.bucketCount(T(2024, 0, 1), T(2024, 0, 2), 'hour'), 24);
  assert.equal(M.bucketCount(T(2024, 0, 1), T(2024, 0, 1, 0, 0, 0, 1), 'day'), 1);
  assert.equal(M.chooseBucket(T(2024, 0, 1), T(2024, 0, 2), 400), 'hour');
  assert.equal(M.chooseBucket(T(2020, 0, 1), T(2024, 0, 1), 400), 'week');
  assert.equal(M.chooseBucket(T(1900, 0, 1), T(2024, 0, 1), 400), 'year');
  // Years below 100 are literal, not mapped to 1900+.
  assert.equal(new Date(M.floorUnit(-62000000000000, 'year')).getUTCFullYear(), 5);
});

test('items vs density mode and bucket budget per pixel', () => {
  assert.equal(M.modeFor(1500, 1500), 'items');
  assert.equal(M.modeFor(1501, 1500), 'density');
  assert.equal(M.maxBucketsFor(1000, 4), 250);
  assert.equal(M.maxBucketsFor(1, 4), 1);
  assert.equal(M.maxBucketsFor(1e6, 4), 2000);
});

test('vertical virtualization renders only rows in the viewport plus overscan', () => {
  assert.deepEqual(M.visibleLaneRange(1000, 46, 0, 460, 2), [0, 11]);
  assert.deepEqual(M.visibleLaneRange(1000, 46, 4600, 460, 2), [98, 111]);
  assert.deepEqual(M.visibleLaneRange(5, 46, 0, 460, 2), [0, 4]);
  assert.deepEqual(M.visibleLaneRange(0, 46, 0, 460), [0, -1]);
  const [first, last] = M.visibleLaneRange(200000, 46, 46 * 150000, 600, 2);
  assert.ok(last - first + 1 <= 18);
});

test('items in view by display time or uncertainty interval', () => {
  const view = {start: 100, end: 200};
  const items = [{key: 'a', t: 150}, {key: 'b', t: 250, lo: 50, hi: 300}, {key: 'c', t: null}, {key: 'd', t: 99}];
  assert.deepEqual(M.itemsInView(items, view).map(i => i.key), ['a']);
  assert.deepEqual(M.itemsInView(items, view, {useInterval: true}).map(i => i.key), ['a', 'b']);
});

test('marker layout stacks close items and clusters overflow', () => {
  const view = {start: 0, end: 1000};
  const items = Array.from({length: 10}, (_, i) => ({key: `k${i}`, t: 500 + i}));
  const {placed, overflow} = M.layoutMarkers(items, view, 1000, {minGap: 10, maxRows: 3});
  assert.equal(placed.length, 3);
  assert.deepEqual(placed.map(p => p.row), [0, 1, 2]);
  assert.equal(overflow.length, 1);
  assert.equal(overflow[0].count, 7);
  const spread = M.layoutMarkers([{key: 'x', t: 100}, {key: 'y', t: 300}], view, 1000);
  assert.deepEqual(spread.placed.map(p => p.row), [0, 0]);
  assert.equal(spread.overflow.length, 0);
});

test('ticks are aligned, ordered, labelled and bounded', () => {
  const day = M.ticks({start: T(2024, 0, 1, 0, 7), end: T(2024, 0, 2)}, 1000, 90);
  assert.ok(day.length > 3 && day.length < 20);
  assert.ok(day.every((tick, i) => i === 0 || tick.t > day[i - 1].t));
  assert.ok(day.every(tick => new Date(tick.t).getUTCMinutes() === 0));
  const years = M.ticks({start: T(1990, 5, 1), end: T(2030, 0, 1)}, 800, 90);
  assert.ok(years.every(tick => /^\d{4}$/.test(tick.label)));
  assert.ok(M.ticks({start: M.MIN_TIME, end: M.MAX_TIME}, 100000, 1).length <= 200);
  assert.equal(M.tickLabel(T(2024, 2, 5, 7, 9), 'minute'), '2024-03-05 07:09');
});

test('query building repeats arrays and drops empty values', () => {
  const q = M.buildQuery({event_type: ['sms', 'call'], q: '', kind: [], from: '2024-01-01T00:00:00.000Z', candidates: 'false', x: null, y: false});
  assert.equal(q.toString(), 'event_type=sms&event_type=call&from=2024-01-01T00%3A00%3A00.000Z&candidates=false');
  assert.equal(M.isoBound(T(2024, 0, 1, 0, 0, 0, 500)), '2024-01-01T00:00:00.500Z');
  const fitted = M.viewForExtent(T(2024, 0, 1), T(2024, 0, 1));
  assert.ok(fitted.end - fitted.start >= M.HOUR);
});
