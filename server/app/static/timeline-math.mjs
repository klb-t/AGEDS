// Pure helpers for the timeline view: zoom/pan math, UTC calendar buckets,
// virtualization windows and marker layout. No DOM access; unit-tested in
// server/tests/js/timeline-math.test.mjs.

export const MINUTE = 60_000;
export const HOUR = 60 * MINUTE;
export const DAY = 24 * HOUR;
export const WEEK = 7 * DAY;
export const YEAR_APPROX = 365.25 * DAY;
export const MONTH_APPROX = YEAR_APPROX / 12;

// Zoom ladder from one minute to fifty years (milliseconds).
export const ZOOM_SPANS = [
  MINUTE, 5 * MINUTE, 15 * MINUTE, 30 * MINUTE, HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 3 * DAY, WEEK,
  14 * DAY, 30 * DAY, 91 * DAY, 182 * DAY, 365 * DAY, 2 * 365 * DAY, 5 * 365 * DAY, 10 * 365 * DAY,
  20 * 365 * DAY, 50 * 365 * DAY,
];
export const UNITS = ['minute', 'hour', 'day', 'week', 'month', 'year'];
const FIXED = {minute: MINUTE, hour: HOUR, day: DAY, week: WEEK};
const MONDAY = 4 * DAY; // 1970-01-05T00:00Z was a Monday
export const MIN_TIME = -62135596800000; // 0001-01-01T00:00:00Z (server axis lower bound)
export const MAX_TIME = Date.UTC(9999, 11, 31, 23, 59, 59, 999);

/** Index of the smallest ladder span that is >= span. */
export function zoomIndexForSpan(span) {
  for (let i = 0; i < ZOOM_SPANS.length; i++) if (ZOOM_SPANS[i] >= span) return i;
  return ZOOM_SPANS.length - 1;
}

export function clampView(view) {
  let {start, end} = view;
  const span = Math.max(1, end - start);
  if (start < MIN_TIME) {start = MIN_TIME; end = start + span;}
  if (end > MAX_TIME) {end = MAX_TIME; start = end - span;}
  return {start: Math.round(start), end: Math.round(end)};
}

/** Zoom by ladder steps (negative = in) keeping the time under `anchor` (0..1) fixed. */
export function zoom(view, steps, anchor = 0.5) {
  const span = view.end - view.start;
  const index = Math.min(ZOOM_SPANS.length - 1, Math.max(0, zoomIndexForSpan(span) + steps));
  const next = ZOOM_SPANS[index];
  const a = Math.min(1, Math.max(0, anchor));
  const pivot = view.start + span * a;
  return clampView({start: pivot - next * a, end: pivot + next * (1 - a)});
}

/** Pan by a fraction of the visible span (positive = later). */
export function pan(view, fraction) {
  const shift = (view.end - view.start) * fraction;
  return clampView({start: view.start + shift, end: view.end + shift});
}

export function timeToX(t, view, width) {
  return ((t - view.start) / (view.end - view.start)) * width;
}

export function xToTime(x, view, width) {
  return view.start + (x / width) * (view.end - view.start);
}

/** Padded view around an extent; a single instant gets a one-hour window. */
export function viewForExtent(from, to, pad = 0.03) {
  if (from == null || to == null) {
    const now = Date.UTC(2000, 0, 1);
    return {start: now, end: now + DAY};
  }
  const span = Math.max(HOUR, to - from);
  return clampView({start: from - span * pad, end: from + span * (1 + pad)});
}

export function floorUnit(t, unit) {
  if (unit === 'week') return t - mod(t - MONDAY, WEEK);
  if (FIXED[unit]) return t - mod(t, FIXED[unit]);
  const d = new Date(t);
  if (unit === 'month') return utc(d.getUTCFullYear(), d.getUTCMonth(), 1);
  if (unit === 'year') return utc(d.getUTCFullYear(), 0, 1);
  throw new Error(`unknown unit ${unit}`);
}

export function addUnit(t, unit, count = 1) {
  if (FIXED[unit]) return t + count * FIXED[unit];
  const d = new Date(t);
  if (unit === 'month') return utc(d.getUTCFullYear(), d.getUTCMonth() + count, d.getUTCDate(), d.getUTCHours(), d.getUTCMinutes(), d.getUTCSeconds(), d.getUTCMilliseconds());
  if (unit === 'year') return utc(d.getUTCFullYear() + count, d.getUTCMonth(), d.getUTCDate(), d.getUTCHours(), d.getUTCMinutes(), d.getUTCSeconds(), d.getUTCMilliseconds());
  throw new Error(`unknown unit ${unit}`);
}

function mod(a, b) {return ((a % b) + b) % b;}

// Date.UTC maps years 0..99 to 1900..1999; setUTCFullYear keeps them literal.
function utc(year, month, day, h = 0, m = 0, s = 0, ms = 0) {
  const d = new Date(Date.UTC(2000, 0, 1, h, m, s, ms));
  d.setUTCFullYear(year, month, day);
  return d.getTime();
}

/** Number of UTC calendar buckets of `unit` touched by [start, end). */
export function bucketCount(start, end, unit) {
  const first = floorUnit(start, unit);
  if (FIXED[unit]) return Math.floor((end - 1 - first) / FIXED[unit]) + 1;
  const a = new Date(first), b = new Date(end - 1);
  const months = (b.getUTCFullYear() - a.getUTCFullYear()) * 12 + (b.getUTCMonth() - a.getUTCMonth());
  return (unit === 'month' ? months : b.getUTCFullYear() - a.getUTCFullYear()) + 1;
}

/** Finest bucket unit whose count over [start, end) stays within maxBuckets (mirrors the server). */
export function chooseBucket(start, end, maxBuckets) {
  for (const unit of UNITS) if (bucketCount(start, end, unit) <= maxBuckets) return unit;
  return 'year';
}

/** Items are drawn individually only when the window holds few enough of them. */
export function modeFor(total, threshold) {
  return total <= threshold ? 'items' : 'density';
}

/** Lane rows to render for a vertical scroll position (inclusive indices, with overscan). */
export function visibleLaneRange(count, rowHeight, scrollTop, viewportHeight, overscan = 2) {
  if (count <= 0) return [0, -1];
  const first = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
  const last = Math.min(count - 1, Math.ceil((scrollTop + viewportHeight) / rowHeight) + overscan - 1);
  return [first, Math.max(first - 1, last)];
}

/** Items intersecting the view (by display time or by uncertainty interval). */
export function itemsInView(items, view, {useInterval = false} = {}) {
  return items.filter(item => {
    if (item.t == null) return false;
    if (!useInterval) return item.t >= view.start && item.t < view.end;
    const lo = item.lo ?? item.t, hi = item.hi ?? item.t;
    return hi >= view.start && lo < view.end;
  });
}

/**
 * Greedy row packing of point markers inside one lane. Items closer than
 * minGap pixels go to the next row; beyond maxRows they are counted as
 * overflow per row-0 neighbour so the lane can show "+N" instead of overlap.
 */
export function layoutMarkers(items, view, width, {minGap = 10, maxRows = 3} = {}) {
  const sorted = [...items].map(item => ({item, x: timeToX(item.t, view, width)})).sort((a, b) => a.x - b.x || cmp(a.item.key, b.item.key));
  const rowEnds = new Array(maxRows).fill(-Infinity);
  const placed = [];
  const overflow = [];
  for (const entry of sorted) {
    let row = rowEnds.findIndex(end => entry.x - end >= minGap);
    if (row === -1) {
      overflow.push(entry);
      continue;
    }
    rowEnds[row] = entry.x;
    placed.push({...entry, row});
  }
  return {placed, overflow: clusterOverflow(overflow, minGap)};
}

function clusterOverflow(entries, gap) {
  const clusters = [];
  for (const entry of entries) {
    const last = clusters.at(-1);
    if (last && entry.x - last.x1 < gap) {last.x1 = entry.x; last.count++; last.keys.push(entry.item.key);}
    else clusters.push({x0: entry.x, x1: entry.x, count: 1, keys: [entry.item.key]});
  }
  return clusters;
}

function cmp(a, b) {return a < b ? -1 : a > b ? 1 : 0;}

/** Tick unit/step so that labels are at least minPx apart. */
export function tickStep(view, width, minPx = 90) {
  const span = view.end - view.start;
  const steps = [
    ['minute', 1], ['minute', 5], ['minute', 15], ['minute', 30], ['hour', 1], ['hour', 3], ['hour', 6], ['hour', 12],
    ['day', 1], ['day', 2], ['week', 1], ['month', 1], ['month', 3], ['month', 6], ['year', 1], ['year', 2],
    ['year', 5], ['year', 10], ['year', 25], ['year', 50], ['year', 100], ['year', 250], ['year', 500], ['year', 1000],
  ];
  for (const [unit, n] of steps) {
    const approx = (FIXED[unit] ?? (unit === 'month' ? MONTH_APPROX : YEAR_APPROX)) * n;
    if ((approx / span) * width >= minPx) return {unit, n};
  }
  return {unit: 'year', n: 1000};
}

/** Aligned UTC tick instants within the view. */
export function ticks(view, width, minPx = 90, max = 200) {
  const {unit, n} = tickStep(view, width, minPx);
  let t = floorUnit(view.start, unit);
  if (unit === 'year') {
    const y = new Date(t).getUTCFullYear();
    t = utc(y - mod(y, n), 0, 1);
  } else if (unit === 'month') {
    const d = new Date(t);
    t = utc(d.getUTCFullYear(), d.getUTCMonth() - mod(d.getUTCMonth(), n), 1);
  } else if (unit !== 'week') {
    t = t - mod(t, FIXED[unit] * n);
  }
  const out = [];
  while (t < view.end && out.length < max) {
    if (t >= view.start) out.push({t, unit, label: tickLabel(t, unit)});
    t = addUnit(t, unit, n);
  }
  return out;
}

export function tickLabel(t, unit) {
  const iso = new Date(t).toISOString();
  if (unit === 'year') return iso.slice(0, 4);
  if (unit === 'month') return iso.slice(0, 7);
  if (unit === 'day' || unit === 'week') return iso.slice(0, 10);
  return iso.slice(0, 10) + ' ' + iso.slice(11, 16);
}

/** Ordered, clamped selection from two pixel positions. */
export function selectionRange(x1, x2, view, width) {
  const a = Math.max(0, Math.min(width, Math.min(x1, x2)));
  const b = Math.max(0, Math.min(width, Math.max(x1, x2)));
  return {start: Math.round(xToTime(a, view, width)), end: Math.round(xToTime(b, view, width))};
}

/** ISO (UTC, Z) for an API bound; milliseconds kept. */
export function isoBound(t) {
  return new Date(Math.round(t)).toISOString();
}

/** URLSearchParams with repeated keys for array values; empty values are dropped. */
export function buildQuery(params) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value == null || value === '' || value === false) continue;
    if (Array.isArray(value)) {for (const v of value) if (v != null && v !== '') query.append(key, String(v));}
    else query.append(key, String(value));
  }
  return query;
}

/** Max buckets that keep each bar at least minPx wide. */
export function maxBucketsFor(width, minPx = 4) {
  return Math.max(1, Math.min(2000, Math.floor(width / minPx)));
}
