import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {bandPath, barPath, compact, controlGeometry, formatLag, histogramGeometry, linearScale, niceTicks, periodStart,
  pickTicks, stepPath} from '../../app/static/hypotheses-chart.mjs';
import {integerList, optionalInteger, runOverrides, sha256Hex} from '../../app/static/hypotheses.mjs';

const DAY = 86400;

function hist() {
  const edges = [-3 * DAY, -2 * DAY, -DAY, 0, DAY, 2 * DAY, 3 * DAY];
  return {edges_s: edges, observed: [2, 3, 1, 9, 14, 2], null_mean: [3.1, 2.9, 3, 3.2, 3, 2.8],
    null_q025: [0, 0, 1, 1, 0, 0], null_q975: [7, 6, 7, 8, 7, 6]};
}

test('linear scale maps endpoints and centers a degenerate domain', () => {
  const s = linearScale([0, 10], [100, 200]);
  assert.equal(s(0), 100);
  assert.equal(s(10), 200);
  assert.equal(s(5), 150);
  assert.equal(linearScale([4, 4], [0, 10])(4), 5);
});

test('nice ticks use 1/2/5 steps, cover the maximum and reject non-positive input', () => {
  assert.deepEqual(niceTicks(14, 4), [0, 5, 10, 15]);
  assert.deepEqual(niceTicks(8, 4), [0, 2, 4, 6, 8]);
  assert.deepEqual(niceTicks(0.9, 4), [0, 0.5, 1]);
  assert.deepEqual(niceTicks(0), [0, 1]);
  assert.deepEqual(niceTicks(Number.NaN), [0, 1]);
  for (const max of [1, 3, 7, 99, 1234, 98765]) {
    const ticks = niceTicks(max, 4);
    assert.ok(ticks[ticks.length - 1] >= max, String(max));
    assert.ok(ticks.length <= 7, String(max));
  }
  assert.equal(compact(1500), '1.5K');
  assert.equal(compact(2_000_000), '2M');
  assert.equal(compact(12), '12');
});

test('lag labels use a true minus sign and the largest exact unit', () => {
  assert.equal(formatLag(-3 * DAY), '−3 d');
  assert.equal(formatLag(12 * 3600), '+12 h');
  assert.equal(formatLag(120), '+2 min');
  assert.equal(formatLag(-90), '−90 s');
  assert.equal(formatLag(0), '0');
});

test('bars have a rounded data end, a square baseline and never exceed the radius', () => {
  assert.equal(barPath(0, 0, 10, 0), '');
  assert.equal(barPath(0, 0, 0, 10), '');
  const path = barPath(10, 20, 6, 2);
  assert.match(path, /^M10,22V22Q10,20 12,20H14Q16,20 16,22V22Z$/); // radius limited by height 2
  const tall = barPath(0, 0, 20, 50);
  assert.ok(tall.startsWith('M0,50V4Q0,0 4,0H16Q20,0 20,4V50Z'));
});

test('null band and mean are step shapes over the bin edges', () => {
  const xs = linearScale([0, 3], [0, 30]);
  const ys = linearScale([0, 10], [100, 0]);
  const band = bandPath([0, 1, 2, 3], [1, 2, 3], [5, 6, 7], xs, ys);
  assert.equal(band, 'M0,50L10,50L10,40L20,40L20,30L30,30L30,70L20,70L20,80L10,80L10,90L0,90Z');
  assert.equal(bandPath([0, 1], [1, 2], [3], xs, ys), '');
  assert.equal(stepPath([0, 1, 2], [2, 4], xs, ys), 'M0,80L10,80L10,60L20,60');
  assert.equal(stepPath([0, 1], [], xs, ys), '');
});

test('histogram geometry: one capped bar per bin inside the plot, flags above the null band', () => {
  const g = histogramGeometry(hist(), {width: 600, height: 240, windows: [{label: 'after', start_s: 0, end_s: 3 * DAY}]});
  assert.equal(g.bars.length, 6);
  for (const bar of g.bars) {
    assert.ok(bar.width <= 24 && bar.width > 0);
    assert.ok(bar.x >= g.margin.left && bar.x + bar.width <= g.width - g.margin.right);
    assert.ok(Math.abs(bar.y + bar.height - g.baseline) < 1e-9);
    assert.ok(bar.hitWidth >= bar.width);
  }
  assert.deepEqual(g.bars.map(b => b.aboveBand), [false, false, false, true, true, false]);
  assert.ok(g.zero > g.bars[2].x && g.zero < g.bars[3].x + g.bars[3].width);
  assert.equal(g.windowMarks.length, 1);
  assert.ok(Math.abs(g.windowMarks[0].x0 - g.zero) < 1e-9);
  assert.ok(g.xTicks.some(t => t.value === 0 && t.label === '0'));
  assert.ok(g.yTicks[g.yTicks.length - 1].value >= 14);
  assert.match(g.band, /^M.*Z$/);
  assert.throws(() => histogramGeometry({...hist(), observed: [1, 2]}), /do not match/);
});

test('tick picking keeps both ends and zero', () => {
  const edges = Array.from({length: 29}, (_, j) => (j - 14) * 3600);
  const ticks = pickTicks(edges, 7);
  assert.equal(ticks[0], edges[0]);
  assert.equal(ticks[ticks.length - 1], edges[edges.length - 1]);
  assert.ok(ticks.includes(0));
  assert.deepEqual(pickTicks([0, 1, 2], 7), [0, 1, 2]);
});

test('control series: four rows with their own scale, stimulus rug only where present', () => {
  const series = {bins: 5, period_s: DAY, start_utc: '2025-01-06T00:00:00.000000Z', counts_all: [3, 30, 28, 2, 0],
    counts_density: [2, 25, 24, 2, 0], counts_outcome: [1, 4, 3, 0, 0], counts_stimulus: [0, 2, 1, 0, 0]};
  const g = controlGeometry(series, {width: 500});
  assert.equal(g.rows.length, 4);
  assert.deepEqual(g.rows.map(r => r.max), [30, 25, 4, 2]);
  assert.equal(g.rows[3].ticks.length, 2);
  assert.equal(g.rows[0].line.split('L').length, 5);
  assert.equal(g.indexAt(g.xAt(3)), 3);
  assert.equal(periodStart(series.start_utc, DAY, 2), '2025-01-08 00:00 UTC');
  assert.throws(() => controlGeometry({...series, counts_outcome: [1]}), /wrong length/);
});

test('browser hash check equals Node SHA-256 of the canonical text; strict integer fields', async () => {
  const canonical = '{"title":"Zażółć \\"x\\"","windows":[{"end_s":86400}]}';
  assert.equal(await sha256Hex(canonical), createHash('sha256').update(canonical, 'utf8').digest('hex'));
  assert.equal(await sha256Hex('x', null), null);
  assert.equal(optionalInteger('  ', 'x'), undefined);
  assert.equal(optionalInteger('-12', 'x'), -12);
  for (const bad of ['1.5', '1e3', 'abc', '0x10', '99999999999999999999']) assert.throws(() => optionalInteger(bad, 'x'));
  assert.deepEqual(integerList('3, 5;7 9', 'ids'), [3, 5, 7, 9]);
  const form = {elements: {permutations: {value: '199'}, seed: {value: ''}, block_s: {value: ''}, null_model: {value: 'permutation'}}};
  assert.deepEqual(runOverrides(form), {permutations: 199, null_model: 'permutation'});
  const empty = {elements: {permutations: {value: ''}, seed: {value: ''}, block_s: {value: ''}, null_model: {value: ''}}};
  assert.equal(runOverrides(empty), undefined);
});
