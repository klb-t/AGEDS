/**
 * Event-study charts: lag histogram vs. permutation-null band, and the
 * documentation-density control series. Pure geometry functions are exported
 * for Node tests; render* functions build SVG with createElementNS and put
 * every label in textContent (labels are data, never markup).
 * The chart shows stored numbers only; it computes no statistics.
 */
const SVG = 'http://www.w3.org/2000/svg';

export function linearScale([d0, d1], [r0, r1]) {
  const span = d1 - d0;
  const scale = value => (span === 0 ? (r0 + r1) / 2 : r0 + ((value - d0) / span) * (r1 - r0));
  scale.domain = [d0, d1];
  scale.range = [r0, r1];
  return scale;
}

/** Round step 1/2/5×10^k so that about `count` ticks cover [0, max]. */
export function niceTicks(max, count = 4) {
  if (!(Number.isFinite(max) && max > 0)) return [0, 1];
  const raw = max / count;
  const power = 10 ** Math.floor(Math.log10(raw));
  const unit = [1, 2, 5, 10].find(f => f * power >= raw) * power;
  const ticks = [];
  for (let v = 0; v < max + unit * 1e-9; v += unit) ticks.push(Number(v.toPrecision(12)));
  if (ticks[ticks.length - 1] < max) ticks.push(Number((ticks[ticks.length - 1] + unit).toPrecision(12)));
  return ticks;
}

export function compact(value) {
  const abs = Math.abs(value);
  if (abs >= 1e6) return `${Number((value / 1e6).toPrecision(3))}M`;
  if (abs >= 1e3) return `${Number((value / 1e3).toPrecision(3))}K`;
  return String(Number(value.toPrecision(6)));
}

/** Lag in seconds → short Polish label with a true minus sign. */
export function formatLag(seconds) {
  const sign = seconds < 0 ? '−' : seconds > 0 ? '+' : '';
  const abs = Math.abs(seconds);
  if (abs === 0) return '0';
  if (abs % 86400 === 0) return `${sign}${abs / 86400} d`;
  if (abs % 3600 === 0) return `${sign}${abs / 3600} h`;
  if (abs % 60 === 0) return `${sign}${abs / 60} min`;
  return `${sign}${abs} s`;
}

/** Column with a rounded data end (radius ≤ 4) and a square baseline. */
export function barPath(x, y, width, height, radius = 4) {
  if (!(width > 0) || !(height > 0)) return '';
  const r = Math.min(radius, width / 2, height);
  const b = y + height;
  return `M${x},${b}V${y + r}Q${x},${y} ${x + r},${y}H${x + width - r}Q${x + width},${y} ${x + width},${y + r}V${b}Z`;
}

/** Step outline between lows and highs over the bin edges (closed polygon). */
export function bandPath(edges, lows, highs, xs, ys) {
  if (!edges.length || lows.length !== edges.length - 1 || highs.length !== lows.length) return '';
  const top = [], bottom = [];
  for (let j = 0; j < highs.length; j += 1) {
    top.push(`${xs(edges[j])},${ys(highs[j])}`, `${xs(edges[j + 1])},${ys(highs[j])}`);
    bottom.push(`${xs(edges[j + 1])},${ys(lows[j])}`, `${xs(edges[j])},${ys(lows[j])}`);
  }
  return `M${top.join('L')}L${bottom.reverse().join('L')}Z`;
}

export function stepPath(edges, values, xs, ys) {
  if (!values.length || edges.length !== values.length + 1) return '';
  const parts = [];
  values.forEach((v, j) => parts.push(`${xs(edges[j])},${ys(v)}`, `${xs(edges[j + 1])},${ys(v)}`));
  return `M${parts.join('L')}`;
}

/** All geometry of the lag histogram for a given box; deterministic and DOM-free. */
export function histogramGeometry(hist, {width = 720, height = 260, margin = {top: 28, right: 16, bottom: 34, left: 44}, windows = []} = {}) {
  const edges = hist.edges_s;
  const n = hist.observed.length;
  if (edges.length !== n + 1 || [hist.null_mean, hist.null_q025, hist.null_q975].some(a => a.length !== n)) {
    throw new Error('histogram arrays do not match the bin edges');
  }
  const xs = linearScale([edges[0], edges[n]], [margin.left, width - margin.right]);
  const maxValue = Math.max(1, ...hist.observed, ...hist.null_q975, ...hist.null_mean);
  const ticks = niceTicks(maxValue, 4);
  const ys = linearScale([0, ticks[ticks.length - 1]], [height - margin.bottom, margin.top]);
  const baseline = ys(0);
  const bars = hist.observed.map((value, j) => {
    const x0 = xs(edges[j]), x1 = xs(edges[j + 1]);
    const slot = Math.max(0, x1 - x0 - 2);
    const w = Math.min(24, slot);
    const x = x0 + (x1 - x0 - w) / 2;
    const y = ys(value);
    return {index: j, x, width: w, y, height: baseline - y, value, lo: edges[j], hi: edges[j + 1],
      hitX: x0, hitWidth: x1 - x0, nullMean: hist.null_mean[j], q025: hist.null_q025[j], q975: hist.null_q975[j],
      aboveBand: value > hist.null_q975[j], belowBand: value < hist.null_q025[j], path: barPath(x, y, w, baseline - y)};
  });
  const zero = edges[0] <= 0 && 0 <= edges[n] ? xs(0) : null;
  const windowMarks = windows
    .map(w => ({label: w.label, x0: xs(Math.max(edges[0], w.start_s)), x1: xs(Math.min(edges[n], w.end_s))}))
    .filter(w => w.x1 > w.x0);
  const xTicks = pickTicks(edges, 7).map(v => ({value: v, x: xs(v), label: formatLag(v)}));
  return {width, height, margin, baseline, bars, zero, windowMarks, xTicks,
    yTicks: ticks.map(v => ({value: v, y: ys(v), label: compact(v)})),
    band: bandPath(edges, hist.null_q025, hist.null_q975, xs, ys), mean: stepPath(edges, hist.null_mean, xs, ys)};
}

/** Up to `count` evenly spaced edges, always including 0 when it is an edge. */
export function pickTicks(edges, count) {
  if (edges.length <= count) return [...edges];
  const step = Math.ceil((edges.length - 1) / (count - 1));
  const picked = new Set();
  for (let j = 0; j < edges.length; j += step) picked.add(edges[j]);
  picked.add(edges[edges.length - 1]);
  if (edges.includes(0)) picked.add(0);
  return [...picked].sort((a, b) => a - b);
}

/** Small multiples of the control series (each row its own y scale, shared x). */
export function controlGeometry(series, {width = 720, rowHeight = 64, gap = 18, margin = {top: 10, right: 16, bottom: 26, left: 44}} = {}) {
  const rows = [['counts_all', 'Wszystkie zdarzenia (kontrola gęstości)'], ['counts_density', 'Dokumentacja bez bodźców i wyników'],
    ['counts_outcome', 'Zdarzenia wyniku'], ['counts_stimulus', 'Bodźce']];
  const bins = series.bins;
  const xs = linearScale([0, bins], [margin.left, width - margin.right]);
  const height = margin.top + rows.length * (rowHeight + gap) + margin.bottom;
  const out = rows.map(([key, label], r) => {
    const values = series[key];
    if (!Array.isArray(values) || values.length !== bins) throw new Error(`control series ${key} has the wrong length`);
    const top = margin.top + r * (rowHeight + gap) + gap;
    const max = Math.max(1, ...values);
    const ys = linearScale([0, max], [top + rowHeight, top]);
    const points = values.map((v, j) => `${xs(j + 0.5)},${ys(v)}`);
    const area = `M${xs(0.5)},${ys(0)}L${points.join('L')}L${xs(bins - 0.5)},${ys(0)}Z`;
    return {key, label, top, max, baseline: ys(0), line: `M${points.join('L')}`, area,
      ticks: key === 'counts_stimulus' ? values.map((v, j) => (v > 0 ? {x: xs(j + 0.5), count: v, index: j} : null)).filter(Boolean) : []};
  });
  return {width, height, rows: out, xAt: index => xs(index + 0.5), indexAt: x => Math.floor((x - margin.left) / ((width - margin.left - margin.right) / bins)),
    margin, bins, periodS: series.period_s, startUtc: series.start_utc};
}

export function periodStart(startUtc, periodS, index) {
  return new Date(Date.parse(startUtc) + index * periodS * 1000).toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
}

// ---------------------------------------------------------------- DOM rendering (browser only)

function el(name, attrs = {}, text) {
  const node = document.createElementNS(SVG, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  if (text !== undefined) node.textContent = text;
  return node;
}

function tooltip(container) {
  let tip = container.querySelector('.hyp-tip');
  if (!tip) {
    tip = document.createElement('div');
    tip.className = 'hyp-tip';
    tip.hidden = true;
    tip.setAttribute('role', 'status');
    container.append(tip);
  }
  return {
    show(rows, x, y) {
      tip.replaceChildren(...rows.map(([value, label]) => {
        const row = document.createElement('div');
        const strong = document.createElement('strong');
        strong.textContent = value;
        const span = document.createElement('span');
        span.textContent = ` ${label}`;
        row.append(strong, span);
        return row;
      }));
      tip.hidden = false;
      const box = container.getBoundingClientRect();
      tip.style.left = `${Math.min(Math.max(0, x - box.left + 12), box.width - 220)}px`;
      tip.style.top = `${Math.max(0, y - box.top - 10)}px`;
    },
    hide() { tip.hidden = true; },
  };
}

export function renderHistogram(container, hist, windows = []) {
  const g = histogramGeometry(hist, {windows});
  const svg = el('svg', {viewBox: `0 0 ${g.width} ${g.height}`, role: 'img', class: 'hyp-chart', 'data-chart': 'lag-histogram',
    'aria-label': 'Histogram opóźnień: obserwowane pary bodziec–wynik i pasmo 2,5–97,5% rozkładu zerowego'});
  for (const t of g.yTicks) {
    svg.append(el('line', {x1: g.margin.left, x2: g.width - g.margin.right, y1: t.y, y2: t.y, class: 'hyp-grid'}));
    svg.append(el('text', {x: g.margin.left - 6, y: t.y + 4, 'text-anchor': 'end', class: 'hyp-axis'}, t.label));
  }
  for (const w of g.windowMarks) {
    svg.append(el('rect', {x: w.x0, y: 4, width: w.x1 - w.x0, height: 3, class: 'hyp-window', 'data-window': w.label}));
    svg.append(el('text', {x: (w.x0 + w.x1) / 2, y: 18, 'text-anchor': 'middle', class: 'hyp-axis'}, w.label));
  }
  svg.append(el('path', {d: g.band, class: 'hyp-band', 'data-null-band': ''}));
  const tip = tooltip(container);
  for (const bar of g.bars) {
    if (bar.path) svg.append(el('path', {d: bar.path, class: `hyp-bar${bar.aboveBand ? ' hyp-above' : ''}`, 'data-bin': bar.index}));
    const hit = el('rect', {x: bar.hitX, y: g.margin.top, width: Math.max(1, bar.hitWidth), height: g.baseline - g.margin.top,
      class: 'hyp-hit', tabindex: 0, 'data-hit-bin': bar.index,
      'aria-label': `${formatLag(bar.lo)} do ${formatLag(bar.hi)}: obserwowane ${bar.value}, średnia zerowa ${bar.nullMean}`});
    const show = event => {
      const r = event.target.getBoundingClientRect();
      tip.show([[String(bar.value), 'par obserwowanych'], [`${bar.q025}–${bar.q975}`, 'pasmo zerowe 2,5–97,5%'],
        [String(bar.nullMean), 'średnia zerowa'], [`${formatLag(bar.lo)} … ${formatLag(bar.hi)}`, 'opóźnienie']],
      event.clientX ?? r.left + r.width / 2, event.clientY ?? r.top);
    };
    hit.addEventListener('pointermove', show);
    hit.addEventListener('focus', show);
    hit.addEventListener('pointerleave', () => tip.hide());
    hit.addEventListener('blur', () => tip.hide());
    svg.append(hit);
  }
  svg.append(el('path', {d: g.mean, class: 'hyp-mean', 'data-null-mean': ''}));
  svg.append(el('line', {x1: g.margin.left, x2: g.width - g.margin.right, y1: g.baseline, y2: g.baseline, class: 'hyp-baseline'}));
  if (g.zero !== null) {
    svg.append(el('line', {x1: g.zero, x2: g.zero, y1: g.margin.top - 6, y2: g.baseline, class: 'hyp-zero'}));
    svg.append(el('text', {x: g.zero + 4, y: g.margin.top + 2, class: 'hyp-axis'}, 'bodziec'));
  }
  for (const t of g.xTicks) svg.append(el('text', {x: t.x, y: g.height - 12, 'text-anchor': 'middle', class: 'hyp-axis'}, t.label));
  container.replaceChildren(svg, container.querySelector('.hyp-tip') ?? '');
  tooltip(container);
  return g;
}

export function renderControl(container, series) {
  const g = controlGeometry(series);
  const svg = el('svg', {viewBox: `0 0 ${g.width} ${g.height}`, role: 'img', class: 'hyp-chart', 'data-chart': 'control-series',
    'aria-label': 'Seria kontrolna: liczba zdarzeń na okres, osobno wszystkie, dokumentacja, wyniki i bodźce'});
  for (const row of g.rows) {
    svg.append(el('text', {x: g.margin.left, y: row.top - 4, class: 'hyp-axis'}, `${row.label} (maks. ${compact(row.max)})`));
    svg.append(el('line', {x1: g.margin.left, x2: g.width - g.margin.right, y1: row.baseline, y2: row.baseline, class: 'hyp-baseline'}));
    if (row.key === 'counts_stimulus') {
      for (const t of row.ticks) svg.append(el('line', {x1: t.x, x2: t.x, y1: row.baseline, y2: row.baseline - 14, class: 'hyp-stim'}));
    } else {
      svg.append(el('path', {d: row.area, class: row.key === 'counts_outcome' ? 'hyp-area-outcome' : 'hyp-area'}));
      svg.append(el('path', {d: row.line, class: row.key === 'counts_outcome' ? 'hyp-line-outcome' : 'hyp-line'}));
    }
  }
  svg.append(el('text', {x: g.margin.left, y: g.height - 8, class: 'hyp-axis'}, periodStart(g.startUtc, g.periodS, 0)));
  svg.append(el('text', {x: g.width - g.margin.right, y: g.height - 8, 'text-anchor': 'end', class: 'hyp-axis'},
    periodStart(g.startUtc, g.periodS, g.bins)));
  const cross = el('line', {y1: 0, y2: g.height - g.margin.bottom, class: 'hyp-cross', visibility: 'hidden'});
  svg.append(cross);
  const tip = tooltip(container);
  svg.addEventListener('pointermove', event => {
    const box = svg.getBoundingClientRect();
    const x = ((event.clientX - box.left) / box.width) * g.width;
    const index = Math.min(g.bins - 1, Math.max(0, g.indexAt(x)));
    cross.setAttribute('x1', g.xAt(index));
    cross.setAttribute('x2', g.xAt(index));
    cross.setAttribute('visibility', 'visible');
    tip.show([[periodStart(g.startUtc, g.periodS, index), `okres ${g.periodS / 3600} h`],
      ...g.rows.map(row => [String(series[row.key][index]), row.label])], event.clientX, event.clientY);
  });
  svg.addEventListener('pointerleave', () => { cross.setAttribute('visibility', 'hidden'); tip.hide(); });
  container.replaceChildren(svg, container.querySelector('.hyp-tip') ?? '');
  tooltip(container);
  return g;
}
