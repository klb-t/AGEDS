import test from 'node:test';
import assert from 'node:assert/strict';
import {scaleLinear, niceStep, hourTicks, formatDuration, layoutCandles, layoutUnobserved, shownCategories, tableRows, UNOBSERVED}
  from '../../app/static/analysis_budget.mjs';

const candle = (bucket, cats, extra = {}) => ({bucket, length_s: 86400, partial: false, categories: cats,
  unobserved: {lower: 80000, probable: 84000, upper: 86400, wick_low: 70000, wick_high: 86400, body_low: 83000, body_high: 85000},
  ...extra});
const v = (lower, probable, upper, o = {}) => ({lower, probable, upper, wick_low: o.wl ?? lower, wick_high: o.wh ?? upper,
  body_low: o.bl ?? probable, body_high: o.bh ?? probable, event_count: 1, unstable: o.unstable ?? [], label_paths: {rule: 1}});

test('linear scale maps domain endpoints and handles a zero span', () => {
  const s = scaleLinear([0, 10], [300, 0]);
  assert.equal(s(0), 300); assert.equal(s(10), 0); assert.equal(s(5), 150);
  assert.equal(scaleLinear([3, 3], [7, 9])(3), 7);
});

test('nice steps and hour ticks cover the maximum from zero', () => {
  assert.equal(niceStep(10, 5), 2);
  assert.equal(niceStep(7, 5), 2);
  assert.equal(niceStep(0, 5), 1);
  const t = hourTicks(7 * 3600 + 1);
  assert.equal(t.ticks[0].hours, 0);
  assert.ok(t.top >= 7 * 3600 + 1);
  assert.deepEqual(t.ticks.map(x => x.hours), [0, 2, 4, 6, 8]);
  assert.deepEqual(hourTicks(0).ticks.map(x => x.hours), [0, 0.2]);
  assert.ok(hourTicks(1800).ticks.every((x, i, a) => i === 0 || x.hours > a[i - 1].hours));
});

test('durations format without rounding surprises', () => {
  assert.equal(formatDuration(0), '0 s');
  assert.equal(formatDuration(59.6), '1 min 00 s');
  assert.equal(formatDuration(3696), '1 h 01 min');
  assert.equal(formatDuration(null), '—');
});

test('candle geometry keeps wick around body around probable, and bounds in order', () => {
  const candles = [candle('2025-03-27', {a: v(0, 3696, 7296, {wl: 0, wh: 10896, bl: 672, bh: 3716})}),
                   candle('2025-03-28', {a: v(1800, 2490, 6090, {wh: 7890, bl: 1986, bh: 3096}), b: v(1200, 1200, 10200, {wh: 15600})})];
  const L = layoutCandles(candles, ['a', 'b'], {width: 600, height: 300});
  assert.equal(L.marks.length, 3);
  for (const m of L.marks) {
    assert.ok(m.wickTop <= m.bodyTop && m.bodyTop <= m.bodyBottom && m.bodyBottom <= m.wickBottom, JSON.stringify(m));
    assert.ok(m.upperY <= m.probableY && m.probableY <= m.lowerY);
    assert.ok(m.bodyTop <= m.probableY + 1e-9 && m.probableY <= m.bodyBottom + 1e-9);
    assert.ok(m.x >= L.margin.left && m.x + m.w <= 600 - L.margin.right + 1e-9);
  }
  // y domain covers the largest wick (15600 s -> 4.33 h -> nice top 5 h)
  assert.equal(L.ticks.top, 5 * 3600);
  assert.equal(L.y(0), 300 - L.margin.bottom);
  // a degenerate body (lower == probable == upper) still gets a visible minimum height
  const flat = L.marks.find(m => m.category === 'b');
  assert.ok(flat.bodyBottom - flat.bodyTop >= 2 - 1e-9);
  // candles of one bucket stay inside their group
  const second = L.marks.filter(m => m.bucket === '2025-03-28');
  assert.ok(second.every(m => m.x >= L.margin.left + L.groupW - 1e-9 && m.x + m.w <= L.margin.left + 2 * L.groupW + 1e-9));
});

test('empty input and many buckets do not break the layout', () => {
  const empty = layoutCandles([], ['a']);
  assert.equal(empty.marks.length, 0);
  assert.equal(empty.ticks.top, 0.2 * 3600);
  const many = Array.from({length: 400}, (_, i) => candle(`d${i}`, {a: v(0, 10, 20)}));
  const L = layoutCandles(many, ['a'], {width: 800});
  assert.ok(L.marks.every(m => m.w >= 3));
});

test('unobserved strip is a share of each bucket including DST lengths', () => {
  const dst = candle('2025-03-30', {}, {length_s: 82800, unobserved: {lower: 79170, probable: 82770, upper: 82800,
    wick_low: 75570, wick_high: 82800, body_low: 82750, body_high: 82780}});
  const U = layoutUnobserved([dst], {width: 300, height: 100});
  const m = U.marks[0];
  assert.ok(Math.abs(m.shareProbable - 82770 / 82800) < 1e-12);
  assert.equal(m.wickTop, U.y(1));
  assert.ok(m.wickTop <= m.bodyTop && m.bodyBottom <= m.wickBottom);
});

test('shown categories follow palette order and never include the unobserved band', () => {
  const palette = [{id: 'b'}, {id: 'a'}, {id: UNOBSERVED}, {id: 'c'}];
  assert.deepEqual(shownCategories([candle('x', {a: v(0, 1, 2), b: v(0, 1, 2)})], palette), ['b', 'a']);
  const rows = tableRows({categories: palette, candles: {day: [candle('x', {a: v(0, 1, 2)})]}}, 'day');
  assert.deepEqual(rows.map(r => r.category), ['a', UNOBSERVED]);
  assert.equal(rows[1].events, null);
});
