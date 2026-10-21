// Time budget page: candle chart, table, sensitivity, exclusions and method.
// Pure layout helpers are exported for Node tests; the DOM part runs only in a browser.
// Source-derived text is always inserted with textContent, never as HTML.

export const UNOBSERVED = 'nieobserwowane';
const SVG = 'http://www.w3.org/2000/svg';

export function scaleLinear([d0, d1], [r0, r1]) {
  const span = d1 - d0;
  const f = v => (span === 0 ? r0 : r0 + ((v - d0) / span) * (r1 - r0));
  f.domain = [d0, d1];
  f.range = [r0, r1];
  return f;
}

export function niceStep(span, count = 5) {
  if (!(span > 0)) return 1;
  const raw = span / Math.max(1, count);
  const power = 10 ** Math.floor(Math.log10(raw));
  const unit = raw / power;
  const nice = unit <= 1 ? 1 : unit <= 2 ? 2 : unit <= 2.5 ? 2.5 : unit <= 5 ? 5 : 10;
  return nice * power;
}

// Ticks in hours for a maximum given in seconds; always starts at 0 and covers max.
export function hourTicks(maxSeconds, count = 5) {
  const maxHours = Math.max(0, maxSeconds) / 3600;
  const step = niceStep(maxHours || 1, count);
  const top = Math.max(step, Math.ceil(maxHours / step - 1e-9) * step);
  const ticks = [];
  for (let v = 0; v <= top + step / 1e6; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
  return {step, top: top * 3600, ticks: ticks.map(h => ({hours: h, seconds: h * 3600}))};
}

export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '—';
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  if (h) return `${h} h ${String(m).padStart(2, '0')} min`;
  if (m) return `${m} min ${String(r).padStart(2, '0')} s`;
  return `${r} s`;
}

export function shownCategories(candles, palette) {
  const present = new Set();
  for (const c of candles) for (const id of Object.keys(c.categories || {})) present.add(id);
  return palette.map(p => p.id).filter(id => id !== UNOBSERVED && present.has(id));
}

// Geometry for category candles. y grows downward (SVG); every candle keeps
// wickTop <= bodyTop <= bodyBottom <= wickBottom and the probable mark inside the body.
export function layoutCandles(candles, categoryIds, {width = 960, height = 320, margin = {top: 12, right: 12, bottom: 36, left: 52}, minBody = 2} = {}) {
  let max = 0;
  for (const c of candles) for (const id of categoryIds) {
    const v = c.categories?.[id];
    if (v) max = Math.max(max, v.wick_high, v.upper);
  }
  const ticks = hourTicks(max);
  const plotW = Math.max(1, width - margin.left - margin.right);
  const plotH = Math.max(1, height - margin.top - margin.bottom);
  const y = scaleLinear([0, ticks.top], [margin.top + plotH, margin.top]);
  const groupW = plotW / Math.max(1, candles.length);
  const slots = Math.max(1, categoryIds.length);
  const candleW = Math.max(3, Math.min(18, (groupW * 0.8) / slots));
  const marks = [];
  candles.forEach((c, i) => {
    const groupX = margin.left + i * groupW;
    const used = categoryIds.filter(id => c.categories?.[id]);
    const offset = (groupW - used.length * candleW) / 2;
    used.forEach((id, j) => {
      const v = c.categories[id];
      const x = groupX + offset + j * candleW;
      let bodyTop = y(v.body_high), bodyBottom = y(v.body_low);
      if (bodyBottom - bodyTop < minBody) {
        const mid = (bodyTop + bodyBottom) / 2;
        bodyTop = mid - minBody / 2; bodyBottom = mid + minBody / 2;
      }
      marks.push({bucket: c.bucket, category: id, x, w: candleW, cx: x + candleW / 2,
                  wickTop: Math.min(y(v.wick_high), bodyTop), wickBottom: Math.max(y(v.wick_low), bodyBottom),
                  bodyTop, bodyBottom, lowerY: y(v.lower), probableY: y(v.probable), upperY: y(v.upper),
                  unstable: (v.unstable || []).length > 0, values: v});
    });
  });
  return {y, ticks, marks, groupW, plotW, plotH, margin, width, height,
          labels: candles.map((c, i) => ({bucket: c.bucket, x: margin.left + i * groupW + groupW / 2, partial: c.partial}))};
}

// The unobserved band as a share of each bucket (0..1) on its own strip.
export function layoutUnobserved(candles, {width = 960, height = 90, margin = {top: 8, right: 12, bottom: 8, left: 52}} = {}) {
  const plotW = Math.max(1, width - margin.left - margin.right);
  const plotH = Math.max(1, height - margin.top - margin.bottom);
  const y = scaleLinear([0, 1], [margin.top + plotH, margin.top]);
  const groupW = plotW / Math.max(1, candles.length);
  const w = Math.max(3, Math.min(22, groupW * 0.5));
  return {y, groupW, marks: candles.map((c, i) => {
    const len = c.length_s || 1, u = c.unobserved;
    const share = v => Math.min(1, Math.max(0, v / len));
    return {bucket: c.bucket, x: margin.left + i * groupW + (groupW - w) / 2, w,
            wickTop: y(share(u.wick_high)), wickBottom: y(share(u.wick_low)),
            bodyTop: y(share(u.body_high)), bodyBottom: y(share(u.body_low)),
            lowerY: y(share(u.lower)), probableY: y(share(u.probable)), upperY: y(share(u.upper)),
            shareProbable: share(u.probable), values: u};
  })};
}

export function tableRows(result, granularity) {
  const rows = [];
  const palette = result.categories || [];
  for (const c of result.candles?.[granularity] || []) {
    for (const p of palette) {
      if (p.id === UNOBSERVED) {
        rows.push({bucket: c.bucket, category: UNOBSERVED, values: c.unobserved, events: null, unstable: [], partial: c.partial});
      } else if (c.categories[p.id]) {
        const v = c.categories[p.id];
        rows.push({bucket: c.bucket, category: p.id, values: v, events: v.event_count, unstable: v.unstable, partial: c.partial,
                   paths: v.label_paths});
      }
    }
  }
  return rows;
}

// ------------------------------------------------------------------ DOM

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'text') node.textContent = v; else node.setAttribute(k, v === true ? '' : String(v));
  }
  for (const child of children) if (child !== null && child !== undefined) node.append(child);
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) node.setAttribute(k, String(v));
  return node;
}

function secondsCell(v) {
  return el('td', {class: 'num', title: `${v} s`, text: formatDuration(v)});
}

async function getJSON(url, options) {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail || body));
  return body;
}

export function mount(root) {
  const state = {runs: [], run: null, granularity: 'day', palette: new Map()};
  const $ = sel => root.querySelector(sel);
  const status = $('[data-status]');
  const say = text => { status.textContent = text; };
  const label = id => state.palette.get(id)?.label || id;
  const color = id => state.palette.get(id)?.color || '#9da7b3';

  async function loadRuns(select) {
    const data = await getJSON('/api/analysis/runs?kind=time_budget&limit=200');
    state.runs = data.runs;
    const picker = $('[data-runs]');
    picker.replaceChildren(...state.runs.map(r => el('option', {value: r.run_id,
      text: `#${r.run_id} · ${r.parameters.window.from} → ${r.parameters.window.to} (${r.parameters.window.zone}) · ${r.parameters.params.overlap_rule} · ${r.created_at}`})));
    if (!state.runs.length) { say('Brak przebiegów. Utwórz nowy przebieg poniżej.'); root.dataset.state = 'empty'; return; }
    const wanted = select || Number(new URLSearchParams(location.search).get('run')) || state.runs[0].run_id;
    picker.value = String(state.runs.some(r => r.run_id === wanted) ? wanted : state.runs[0].run_id);
    await loadRun(Number(picker.value));
  }

  async function loadRun(id) {
    say(`Wczytywanie przebiegu #${id}…`);
    const run = await getJSON(`/api/analysis/budget/runs/${id}`);
    state.run = run;
    state.palette = new Map(run.result.categories.map(c => [c.id, c]));
    const grans = ['day', 'week', 'month'].filter(g => run.result.candles[g]);
    const gsel = $('[data-granularity]');
    gsel.replaceChildren(...grans.map(g => el('option', {value: g, text: {day: 'dzień', week: 'tydzień', month: 'miesiąc'}[g] || g})));
    if (!grans.includes(state.granularity)) state.granularity = grans[0];
    gsel.value = state.granularity;
    renderAll();
    say(`Przebieg #${run.run_id} · wynik ${run.result_sha256.slice(0, 12)} · ${run.result.sensitivity.unstable ? 'NIESTABILNY w siatce parametrów' : 'stabilny w siatce parametrów'}`);
    root.dataset.state = 'ready';
    root.dataset.runId = String(run.run_id);
  }

  function renderAll() {
    renderLegend(); renderChart(); renderTable(); renderSensitivity(); renderExclusions(); renderMethod();
    $('[data-candle-panel]').hidden = true;
  }

  function renderLegend() {
    const candles = state.run.result.candles[state.granularity];
    const ids = shownCategories(candles, state.run.result.categories);
    $('[data-legend]').replaceChildren(...ids.map(id => el('span', {class: 'ab-key'},
      el('i', {style: `background:${color(id)}`}), document.createTextNode(label(id)))),
      el('span', {class: 'ab-key'}, el('i', {class: 'ab-hatch'}), document.createTextNode('nieobserwowane (brak obserwacji, nie bezczynność)')));
  }

  function renderChart() {
    const candles = state.run.result.candles[state.granularity];
    const ids = shownCategories(candles, state.run.result.categories);
    const box = $('[data-chart]');
    const width = Math.max(480, box.clientWidth || 960);
    const L = layoutCandles(candles, ids, {width, height: 320});
    const s = svg('svg', {viewBox: `0 0 ${width} 320`, width: '100%', height: 320, 'data-candles': ''});
    for (const t of L.ticks.ticks) {
      const yy = L.y(t.seconds);
      s.append(svg('line', {x1: L.margin.left, x2: width - L.margin.right, y1: yy, y2: yy, class: 'ab-grid'}));
      const txt = svg('text', {x: L.margin.left - 6, y: yy + 4, class: 'ab-axis', 'text-anchor': 'end'});
      txt.textContent = `${t.hours} h`;
      s.append(txt);
    }
    const every = Math.max(1, Math.ceil(L.labels.length / Math.max(1, Math.floor(width / 70))));
    L.labels.forEach((b, i) => {
      if (i % every) return;
      const txt = svg('text', {x: b.x, y: 320 - 14, class: 'ab-axis', 'text-anchor': 'middle'});
      txt.textContent = b.bucket + (b.partial ? '*' : '');
      s.append(txt);
    });
    for (const m of L.marks) {
      const g = svg('g', {class: 'ab-candle' + (m.unstable ? ' ab-unstable' : ''), 'data-candle': '', 'data-bucket': m.bucket,
                          'data-category': m.category, tabindex: 0, role: 'button',
                          'aria-label': `${label(m.category)} ${m.bucket}: prawdopodobnie ${formatDuration(m.values.probable)}, dolna ${formatDuration(m.values.lower)}, górna ${formatDuration(m.values.upper)}`});
      const title = svg('title');
      title.textContent = `${label(m.category)} · ${m.bucket}\nprawdopodobnie ${formatDuration(m.values.probable)}\ndolna ${formatDuration(m.values.lower)} · górna ${formatDuration(m.values.upper)}\nknot ${formatDuration(m.values.wick_low)}–${formatDuration(m.values.wick_high)} · korpus ${formatDuration(m.values.body_low)}–${formatDuration(m.values.body_high)}\nzdarzenia: ${m.values.event_count}${m.unstable ? '\nNIESTABILNE: ' + m.values.unstable.join(', ') : ''}`;
      g.append(title,
        svg('rect', {x: m.x - 2, y: m.wickTop - 4, width: m.w + 4, height: m.wickBottom - m.wickTop + 8, class: 'ab-hit'}),
        svg('line', {x1: m.cx, x2: m.cx, y1: m.wickTop, y2: m.wickBottom, stroke: color(m.category), class: 'ab-wick'}),
        svg('rect', {x: m.x + 1, y: m.bodyTop, width: Math.max(1, m.w - 2), height: m.bodyBottom - m.bodyTop, rx: 2, fill: color(m.category), class: 'ab-body'}),
        svg('line', {x1: m.x, x2: m.x + m.w, y1: m.lowerY, y2: m.lowerY, class: 'ab-bound'}),
        svg('line', {x1: m.x, x2: m.x + m.w, y1: m.upperY, y2: m.upperY, class: 'ab-bound'}),
        svg('line', {x1: m.x - 1, x2: m.x + m.w + 1, y1: m.probableY, y2: m.probableY, class: 'ab-probable'}));
      s.append(g);
    }
    box.replaceChildren(s);
    const U = layoutUnobserved(candles, {width, height: 90});
    const u = svg('svg', {viewBox: `0 0 ${width} 90`, width: '100%', height: 90, 'data-unobserved': ''});
    const defs = svg('defs');
    const pattern = svg('pattern', {id: 'ab-hatch', width: 6, height: 6, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)'});
    pattern.append(svg('rect', {width: 6, height: 6, fill: '#30363d'}), svg('line', {x1: 0, y1: 0, x2: 0, y2: 6, stroke: '#8b949e', 'stroke-width': 2}));
    defs.append(pattern); u.append(defs);
    for (const [v, text] of [[1, '100%'], [0, '0%']]) {
      const t = svg('text', {x: 46, y: U.y(v) + 4, class: 'ab-axis', 'text-anchor': 'end'}); t.textContent = text; u.append(t);
    }
    for (const m of U.marks) {
      const g = svg('g', {class: 'ab-candle', 'data-candle': '', 'data-bucket': m.bucket, 'data-category': UNOBSERVED, tabindex: 0, role: 'button',
                          'aria-label': `nieobserwowane ${m.bucket}: ${Math.round(m.shareProbable * 100)}% okresu`});
      const title = svg('title');
      title.textContent = `nieobserwowane · ${m.bucket}\nprawdopodobnie ${formatDuration(m.values.probable)} (${Math.round(m.shareProbable * 100)}%)\nzakres ${formatDuration(m.values.lower)}–${formatDuration(m.values.upper)}\nbrak obserwacji ≠ bezczynność`;
      g.append(title,
        svg('line', {x1: m.x + m.w / 2, x2: m.x + m.w / 2, y1: m.wickTop, y2: m.wickBottom, class: 'ab-wick ab-wick-unobserved'}),
        svg('rect', {x: m.x, y: m.bodyTop, width: m.w, height: Math.max(2, m.bodyBottom - m.bodyTop), fill: 'url(#ab-hatch)', class: 'ab-body'}),
        svg('line', {x1: m.x - 1, x2: m.x + m.w + 1, y1: m.probableY, y2: m.probableY, class: 'ab-probable'}));
      u.append(g);
    }
    $('[data-unobserved-strip]').replaceChildren(u);
  }

  async function showCandle(bucket, category) {
    const panel = $('[data-candle-panel]');
    const list = $('[data-candle-events]');
    panel.hidden = false;
    list.replaceChildren(el('p', {text: 'Wczytywanie…'}));
    try {
      const data = await getJSON(`/api/analysis/budget/runs/${state.run.run_id}/candle?granularity=${encodeURIComponent(state.granularity)}&bucket=${encodeURIComponent(bucket)}&category=${encodeURIComponent(category)}&limit=200`);
      const v = data.values;
      const head = el('p', {class: 'ab-note', 'data-candle-summary': '',
        text: `${label(category)} · ${bucket}: dolna ${formatDuration(v.lower)}, prawdopodobna ${formatDuration(v.probable)}, górna ${formatDuration(v.upper)}; zdarzeń ${data.event_count}${data.stored_ids_truncated ? ' (lista ograniczona przez max_candle_events)' : ''}.`});
      if (!data.events.length) {
        list.replaceChildren(head, el('p', {class: 'ab-note', text: data.note || 'Brak zdarzeń w tej świecy.'}));
        return;
      }
      const table = el('table', {'data-candle-table': ''}, el('tr', {}, ...['#', 'Typ', 'Czas (UTC)', 'Kontrahent / chatbot', 'Etykieta w przebiegu (waga · ścieżka · reguły)', 'Linki'].map(t => el('th', {text: t}))));
      const roles = v.roles || {};
      for (const e of data.events) {
        const lab = e.label_in_run;
        const links = el('td', {});
        if (e.links?.artifact) links.append(el('a', {href: e.links.artifact, text: 'materiał'}), document.createTextNode(' '));
        links.append(el('a', {href: e.links.event, text: 'etykiety'}), document.createTextNode(' '), el('a', {href: e.links.timeline, text: 'oś czasu'}));
        table.append(el('tr', {'data-event-id': e.event_id},
          el('td', {text: e.event_id}), el('td', {text: e.event_type || ''}),
          el('td', {text: e.t_start != null ? new Date(e.t_start * 1000).toISOString().replace('.000Z', 'Z') : 'brak'}),
          el('td', {text: [e.counterparty, e.chatbot].filter(Boolean).join(' · ')}),
          el('td', {text: lab ? `${lab.weight} · ${lab.path}${lab.rules ? ' · ' + lab.rules : ''}${lab.annotation_id ? ' · korekta #' + lab.annotation_id : ''}${lab.model_run_id ? ' · model #' + lab.model_run_id : ''}` : '—'}),
          links));
      }
      list.replaceChildren(head, el('p', {class: 'ab-note', text: `Role zdarzeń w świecy: ${Object.entries(roles).map(([k, n]) => `${k} ${n}`).join(', ') || '—'}.`}), table);
    } catch (error) {
      list.replaceChildren(el('p', {class: 'ab-error', text: `Błąd: ${error.message}`}));
    }
  }

  function renderTable() {
    const rows = tableRows(state.run.result, state.granularity);
    const table = el('table', {'data-budget-table': ''}, el('tr', {}, ...['Okres', 'Kategoria', 'Dolna', 'Prawdopodobna', 'Górna', 'Knot (min–max)', 'Korpus (min–max)', 'Zdarzenia', 'Ścieżki etykiet', 'Stabilność', ''].map(t => el('th', {text: t}))));
    for (const r of rows) {
      const v = r.values;
      const button = el('button', {type: 'button', class: 'ab-small', 'data-bucket': r.bucket, 'data-category': r.category, text: 'zdarzenia'});
      table.append(el('tr', {class: r.category === UNOBSERVED ? 'ab-row-unobserved' : ''},
        el('td', {text: r.bucket + (r.partial ? '*' : '')}), el('td', {text: label(r.category)}),
        secondsCell(v.lower), secondsCell(v.probable), secondsCell(v.upper),
        el('td', {class: 'num', text: `${formatDuration(v.wick_low)} – ${formatDuration(v.wick_high)}`}),
        el('td', {class: 'num', text: `${formatDuration(v.body_low)} – ${formatDuration(v.body_high)}`}),
        el('td', {class: 'num', text: r.events ?? '—'}),
        el('td', {text: r.paths ? Object.entries(r.paths).map(([k, n]) => `${k}: ${n}`).join(', ') : '—'}),
        el('td', {text: r.unstable.length ? `niestabilne (${r.unstable.join(', ')})` : '—'}),
        el('td', {}, button)));
    }
    $('[data-table]').replaceChildren(el('p', {class: 'ab-note', text: '* okres obcięty przez okno analizy lub niepełny tydzień/miesiąc. Wartości w sekundach w podpowiedzi komórki.'}), table);
  }

  function renderSensitivity() {
    const sens = state.run.result.sensitivity;
    const ids = Object.keys(sens.ranges);
    const banner = el('p', {class: sens.unstable ? 'ab-warn' : 'ab-note', 'data-unstable': sens.unstable ? 'true' : 'false',
      text: sens.unstable ? `Wynik NIESTABILNY: kolejność/kierunek zmienia się w siatce parametrów (${Object.entries(sens.flip_counts).map(([k, n]) => `${k}: ${n}`).join(', ')}).` : 'Brak zmian kolejności i kierunku w siatce parametrów (tolerancja ' + formatDuration(sens.tolerance_s) + ').'});
    const ranges = el('table', {}, el('tr', {}, ...['Kategoria', 'Bazowa (prawdop.)', 'Min w siatce', 'Max w siatce', 'Względny rozrzut'].map(t => el('th', {text: t}))));
    for (const id of ids) {
      const r = sens.ranges[id];
      ranges.append(el('tr', {}, el('td', {text: label(id)}), secondsCell(r.base), secondsCell(r.min), secondsCell(r.max),
        el('td', {class: 'num', text: r.relative_spread === null ? '—' : `${Math.round(r.relative_spread * 1000) / 10}%`})));
    }
    const points = el('table', {'data-grid-points': ''}, el('tr', {}, el('th', {text: 'Punkt'}), el('th', {text: 'Zmiana parametru'}), ...ids.map(id => el('th', {text: label(id)}))));
    for (const p of sens.points) {
      points.append(el('tr', {}, el('td', {text: p.index === 0 ? '0 (bazowy)' : p.index}),
        el('td', {text: p.index === 0 ? 'parametry przebiegu' : Object.entries(p.differs).map(([k, v]) => `${k} = ${v}`).join(', ')}),
        ...ids.map(id => secondsCell(p.totals[id].probable))));
    }
    const flips = el('ul', {'data-flips': ''});
    for (const [kind, list] of Object.entries(sens.flips)) {
      for (const f of list.slice(0, 50)) {
        const what = kind === 'ordering' ? `${label(f.categories[0])} vs ${label(f.categories[1])}: ${formatDuration(f.base_difference_s)} → ${formatDuration(f.point_difference_s)}`
          : kind === 'top_category' ? `${f.granularity} ${f.bucket}: ${label(f.base_top)} → ${label(f.point_top)}`
          : `${f.granularity} ${f.from_bucket}→${f.to_bucket} ${label(f.category)}: ${formatDuration(f.base_change_s)} → ${formatDuration(f.point_change_s)}`;
        flips.append(el('li', {text: `${kind}: ${what} (punkt ${f.point_index}: ${Object.entries(f.differs).map(([k, v]) => `${k}=${v}`).join(', ')})`}));
      }
    }
    $('[data-sensitivity]').replaceChildren(banner,
      el('p', {class: 'ab-note', text: `Siatka: ${state.run.result.grid.mode}, punktów ${state.run.result.grid.points_evaluated}/${state.run.result.grid.points_total}${state.run.result.grid.truncated ? ' (OBCIĘTA)' : ''}. Wartości łączne dla okna (wariant prawdopodobny).`}),
      ranges, el('h3', {text: 'Punkty siatki'}), points, el('h3', {text: 'Zmiany znaku / kolejności'}), flips);
  }

  function renderExclusions() {
    const ex = state.run.result.exclusions;
    const input = state.run.result.input;
    const untimed = el('ul', {'data-untimed': ''}, ...Object.entries(ex.untimed.by_reason).map(([k, n]) => el('li', {text: `${k}: ${n}`})));
    const ids = el('p', {class: 'ab-note'});
    ex.untimed.event_ids.slice(0, 100).forEach((id, i) => { if (i) ids.append(document.createTextNode(', ')); ids.append(el('a', {href: `/api/analysis/events/${id}`, text: `#${id}`})); });
    const filtered = el('ul', {'data-filtered': ''}, ...Object.entries(ex.filtered.by_reason).map(([k, n]) => el('li', {text: `${k}: ${n}`})));
    const tu = input.time_uncertainty || {};
    $('[data-exclusions]').replaceChildren(
      el('p', {text: `Bez czasu na osi UTC (cały zbiór, okno nieznane): ${ex.untimed.count}${ex.untimed.event_ids_truncated ? ' (lista ograniczona)' : ''}.`}), untimed, ids,
      el('p', {text: `Odfiltrowane parametrami: ${ex.filtered.count}.`}), filtered,
      el('p', {text: `Strefa przyjęta przez operatora: ${ex.assumed_zone_items} zdarzeń${state.run.result.window.assume_zone ? ` (${state.run.result.window.assume_zone}, decyzja ${state.run.result.window.assumption_rule})` : ''}. Źródło obcięte limitem: ${ex.source_truncated ? 'TAK' : 'nie'}.`}),
      el('p', {text: `Niepewność czasu: ${tu.items_with_interval ?? 0} zdarzeń z przedziałem (max ${formatDuration(tu.max_width_s)}), ${tu.items_crossing_day_boundary ?? 0} przecina granicę dnia; tryb ${tu.mode}.`}));
  }

  function renderMethod() {
    const r = state.run.result, run = state.run;
    const dl = el('dl', {});
    const add = (k, v) => dl.append(el('dt', {text: k}), el('dd', {text: v}));
    add('Klasa artefaktu', r.artifact_class);
    add('Reguła', `${r.method.rule}; sesje ${r.method.session_rule}; etykiety ${r.method.labeller}`);
    add('Źródło czasu', `${r.input.seam}: ${r.input.source_rule} (sha256 ${r.input.source_rule_sha256 || 'unknown'})`);
    add('Konfiguracja kategorii', `wersja ${r.config.version}, sha256 ${r.config.sha256}, pochodzenie ${r.config.origin}`);
    add('Kod', `${r.code.version} · ${r.code.sha256.slice(0, 16)}`);
    add('Okno', `${r.window.from} → ${r.window.to} (${r.window.zone}); UTC ${r.window.from_utc} → ${r.window.to_utc}; margines ${formatDuration(r.window.margin_s)}`);
    add('Wejście', `${r.input.items_used} zdarzeń (w oknie ${r.input.items_in_window}); zbiór id ${r.input.event_ids_sha256.slice(0, 16)}`);
    add('Etykiety', `przebieg #${r.labels.label_run_id} · ${Object.entries(r.labels.by_path).map(([k, n]) => `${k}: ${n}`).join(', ')}`);
    add('Wynik', `#${run.run_id} · ${run.created_at} · sha256 ${run.result_sha256}`);
    for (const [k, v] of Object.entries(r.method.variants)) add(`Wariant ${k}`, v);
    add('Nakładanie', `${r.method.overlap_rule}: ${r.method.overlap_rules[r.method.overlap_rule]}`);
    add('Nieobserwowane', r.method.unobserved);
    add('Świeca', `knot: ${r.method.candle.wick}; korpus: ${r.method.candle.body}`);
    add('Niestabilność', r.method.instability);
    $('[data-method]').replaceChildren(el('p', {class: 'ab-warn', text: r.disclaimer}), dl,
      el('details', {}, el('summary', {text: 'Parametry (JSON)'}), el('pre', {text: JSON.stringify(r.parameters, null, 2)})));
  }

  root.addEventListener('click', event => {
    const target = event.target.closest('[data-candle], button[data-bucket]');
    if (target && root.contains(target)) showCandle(target.dataset.bucket, target.dataset.category);
  });
  root.addEventListener('keydown', event => {
    if ((event.key === 'Enter' || event.key === ' ') && event.target.matches?.('[data-candle]')) {
      event.preventDefault(); showCandle(event.target.dataset.bucket, event.target.dataset.category);
    }
  });
  $('[data-runs]').addEventListener('change', e => loadRun(Number(e.target.value)).catch(err => say(`Błąd: ${err.message}`)));
  $('[data-granularity]').addEventListener('change', e => { state.granularity = e.target.value; renderAll(); });
  $('[data-new-run]').addEventListener('submit', async event => {
    event.preventDefault();
    const form = new FormData(event.target);
    const body = {from: form.get('from'), to: form.get('to'), zone: form.get('zone')};
    try {
      for (const key of ['params', 'config']) {
        const text = String(form.get(key) || '').trim();
        if (text) body[key] = JSON.parse(text);
      }
      for (const key of ['assume_zone', 'assumption_rule']) if (String(form.get(key) || '').trim()) body[key] = String(form.get(key)).trim();
      say('Obliczanie…');
      const out = await getJSON('/api/analysis/budget/runs', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(body)});
      await loadRuns(out.run.run_id);
      say(`Przebieg #${out.run.run_id} ${out.run.reused ? '(identyczny wynik już istniał — użyto zapisanego)' : 'zapisany'}.`);
    } catch (error) { say(`Błąd: ${error.message}`); }
  });
  let resizeTimer;
  window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => state.run && renderChart(), 150); });
  loadRuns().catch(err => { say(`Błąd: ${err.message}`); root.dataset.state = 'error'; });
}

if (typeof document !== 'undefined') {
  const root = document.querySelector('[data-budget]');
  if (root) mount(root);
}
