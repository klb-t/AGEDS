// Timeline swimlane view. Source text is data: it is only ever assigned to
// textContent or attributes, never parsed as HTML. Only lanes in the viewport
// and items of the fetched window are rendered; zoomed-out windows render
// density bars computed in SQL instead of individual items.
import * as M from './timeline-math.mjs';

const root = document.querySelector('[data-timeline]');
const $ = selector => root.querySelector(selector);
const lanesEl = $('[data-lanes]');
const spacer = $('[data-spacer]');
const ruler = $('[data-ruler]');
const statusEl = $('[data-status]');
const detailEl = $('[data-detail]');
const rangeEl = $('[data-range-panel]');
const undatedEl = $('[data-undated]');
const selectionEl = $('[data-selection]');
const form = $('[data-filters]');

const ROW = 46;
const LABEL = 190;
const ITEM_THRESHOLD = 1500;
const BASIS = {
  explicit_offset: 'jawny offset w źródle',
  utc_epoch: 'epoka UTC (czas bezwzględny)',
  assumed_zone: 'strefa przyjęta przy imporcie',
  assumed_zone_operator: 'strefa przyjęta jawnie przez operatora',
  local_as_utc_unknown_zone: 'czas lokalny, strefa nieznana — położony jak UTC, przedział offsetów',
  none: 'brak czasu',
};
const state = {
  view: null, extent: null, group: 'source', seq: 0, controller: null, mode: null,
  lanes: [], items: [], density: null, selection: null, undatedCount: 0,
  reportsAvailable: root.dataset.reportsAvailable === 'true', rows: new Map(),
};

function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'style') Object.assign(node.style, value);
    else node.setAttribute(key, value === true ? '' : String(value));
  }
  if (text != null) node.textContent = String(text);
  return node;
}

function setStatus(text) {statusEl.textContent = text;}

function trackWidth() {return Math.max(100, lanesEl.clientWidth - LABEL - 2);}

function formValues() {
  const data = new FormData(form);
  const many = name => data.getAll(name).map(String).filter(Boolean);
  const split = name => String(data.get(name) || '').split(',').map(s => s.trim()).filter(Boolean);
  return {
    group: String(data.get('group') || 'source'),
    filters: {
      event_type: many('event_type'), kind: many('kind'), chatbot: many('chatbot'), source: many('source'),
      collection: many('collection'), tag: many('tag'), counterparty: split('counterparty'),
      q: String(data.get('q') || '').trim(), transcript_versions: String(data.get('transcript_versions') || 'latest'),
      include_hidden: data.get('include_hidden') ? 'true' : '',
    },
    zone: {assume_zone: String(data.get('assume_zone') || '').trim(), assumption_rule: String(data.get('assumption_rule') || '').trim()},
  };
}

function common() {
  const values = formValues();
  state.group = values.group;
  return {...values.filters, ...values.zone};
}

async function getJSON(url, signal) {
  const response = await fetch(url, {signal, headers: {Accept: 'application/json'}});
  if (!response.ok) {
    let detail = response.statusText;
    try {detail = (await response.json()).detail ?? detail;} catch {}
    throw new Error(`${response.status}: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`);
  }
  return response.json();
}

function laneKey(value) {return value == null ? '∅' : String(value);}

function normalize(item) {
  const t = item.time;
  return {
    key: item.key, kind: item.kind, lane: laneKey(item.lane), title: item.title || item.event_type || item.kind,
    t: t.sort_us == null ? null : t.sort_us / 1000, lo: t.interval ? t.interval.from_us / 1000 : null,
    hi: t.interval ? t.interval.to_us / 1000 : null, end: t.end_us == null ? null : t.end_us / 1000,
    display: t.display, basis: t.basis, precision: t.precision, eventType: item.event_type,
  };
}

async function refresh() {
  const seq = ++state.seq;
  state.controller?.abort();
  const controller = state.controller = new AbortController();
  root.dataset.state = 'loading';
  const view = state.view;
  const params = common();
  const width = trackWidth();
  const maxBuckets = M.maxBucketsFor(width, 4);
  const bucket = M.chooseBucket(view.start, view.end, maxBuckets);
  const bounds = {from: M.isoBound(view.start), to: M.isoBound(view.end)};
  updateExports(params, bounds);
  try {
    const started = performance.now();
    const density = await getJSON('/api/timeline/density?' + M.buildQuery({...params, ...bounds, bucket, group: state.group,
      max_buckets: maxBuckets, max_lanes: 60}), controller.signal);
    if (seq !== state.seq) return;
    const mode = M.modeFor(density.total, ITEM_THRESHOLD);
    let items = [];
    if (mode === 'items' && density.total > 0) {
      let cursor = null;
      do {
        const page = await getJSON('/api/timeline/window?' + M.buildQuery({...params, ...bounds, group: state.group, limit: 1000,
          candidates: 'false', lanes: 'false', cursor}), controller.signal);
        if (seq !== state.seq) return;
        items.push(...page.items.map(normalize));
        cursor = page.next_cursor;
      } while (cursor && items.length < ITEM_THRESHOLD);
    }
    state.density = density;
    state.mode = mode;
    state.items = items;
    state.lanes = density.lanes.map(lane => ({key: laneKey(lane.key), label: lane.label, total: lane.total, buckets: lane.buckets}));
    state.undatedCount = density.undated_count;
    draw();
    const elapsed = Math.round(performance.now() - started);
    root.dataset.state = 'ready';
    root.dataset.mode = mode;
    root.dataset.bucket = density.bucket;
    root.dataset.renderSeq = String(seq);
    root.dataset.renderMs = String(elapsed);
    const zone = density.params?.assume_zone ? ` · przyjęta strefa ${density.params.assume_zone} (${density.params.assumption_rule})` : '';
    setStatus(`${M.tickLabel(view.start, 'minute')} – ${M.tickLabel(view.end, 'minute')} UTC · ${mode === 'items' ? 'elementy' : `gęstość (kubełek: ${density.bucket})`} · `
      + `${density.total} el. w widoku · pasy: ${state.lanes.length}${density.lanes_truncated ? ' (ograniczone)' : ''} · bez czasu: ${density.undated_count}`
      + ` · reguła ${density.rule.rule_id}@${density.rule.version}${zone} · ${elapsed} ms`);
    $('[data-undated-button]').textContent = `Bez czasu (${density.undated_count})`;
  } catch (error) {
    if (error.name === 'AbortError') return;
    root.dataset.state = 'error';
    setStatus(`Błąd: ${error.message}`);
  }
}

function draw() {
  const width = trackWidth();
  drawRuler(width);
  spacer.style.height = `${Math.max(1, state.lanes.length) * ROW}px`;
  for (const row of state.rows.values()) row.remove();
  state.rows.clear();
  state.byLane = new Map();
  for (const item of state.items) {
    const key = state.lanes.some(lane => lane.key === item.lane) ? item.lane : '__other__';
    if (!state.byLane.has(key)) state.byLane.set(key, []);
    state.byLane.get(key).push(item);
  }
  renderRows();
  $('[data-level]').textContent = `zakres: ${describeSpan(state.view.end - state.view.start)}`;
  if (!state.lanes.length) {
    const empty = el('p', {class: 'tl-empty', 'data-row': 'empty'}, 'Brak elementów z czasem w tym widoku dla wybranych filtrów.');
    lanesEl.append(empty);
    state.rows.set('empty', empty);
  }
}

function renderRows() {
  const width = trackWidth();
  const [first, last] = M.visibleLaneRange(state.lanes.length, ROW, lanesEl.scrollTop, lanesEl.clientHeight, 2);
  const wanted = new Set();
  for (let index = first; index <= last; index++) wanted.add(index);
  for (const [index, row] of state.rows) {
    if (index !== 'empty' && !wanted.has(index)) {row.remove(); state.rows.delete(index);}
  }
  for (const index of wanted) {
    if (state.rows.has(index)) continue;
    const row = laneRow(state.lanes[index], index, width);
    lanesEl.append(row);
    state.rows.set(index, row);
  }
  root.dataset.renderedLanes = String(state.rows.size);
  root.dataset.renderedNodes = String(lanesEl.querySelectorAll('.tl-item,.tl-bar,.tl-interval,.tl-duration,.tl-overflow').length);
}

function laneRow(lane, index, width) {
  const row = el('div', {class: 'tl-lane', 'data-lane': lane.key, style: {top: `${index * ROW}px`, height: `${ROW}px`}});
  const label = el('div', {class: 'tl-lane-label', title: `${lane.label} — ${lane.total} el. w widoku`});
  label.append(el('span', {}, lane.label), el('small', {}, String(lane.total)));
  const track = el('div', {class: 'tl-track', style: {width: `${width}px`}});
  row.append(label, track);
  if (state.mode === 'density') drawDensity(track, lane, width);
  else drawItems(track, state.byLane?.get(lane.key) || [], width);
  return row;
}

function drawDensity(track, lane, width) {
  const unit = state.density.bucket;
  const max = Math.max(1, state.density.max_count);
  for (const [, startUs, count, unknown] of lane.buckets) {
    const start = startUs / 1000;
    const x0 = Math.max(0, M.timeToX(start, state.view, width));
    const x1 = Math.min(width, M.timeToX(M.addUnit(start, unit), state.view, width));
    if (x1 <= 0 || x0 >= width) continue;
    const height = Math.max(2, Math.round((Math.sqrt(count) / Math.sqrt(max)) * (ROW - 10)));
    const bar = el('div', {class: 'tl-bar', title: `${M.tickLabel(start, unit === 'minute' || unit === 'hour' ? 'minute' : 'day')}: ${count} el.`
      + (unknown ? ` (w tym ${unknown} ze strefą nieznaną)` : ''), 'data-count': count,
      style: {left: `${x0}px`, width: `${Math.max(1, x1 - x0 - 1)}px`, height: `${height}px`}});
    if (unknown) bar.append(el('span', {class: 'tl-bar-unknown', style: {height: `${Math.round((unknown / count) * 100)}%`}}));
    bar.addEventListener('click', () => {state.view = M.clampView({start, end: M.addUnit(start, unit)}); refresh();});
    track.append(bar);
  }
}

function drawItems(track, items, width) {
  const {placed, overflow} = M.layoutMarkers(items, state.view, width, {minGap: 9, maxRows: 3});
  for (const {item, x, row} of placed) {
    const y = 8 + row * 11;
    if (item.lo != null && item.hi != null && item.hi > item.lo) {
      const a = Math.max(0, M.timeToX(item.lo, state.view, width)), b = Math.min(width, M.timeToX(item.hi, state.view, width));
      if (b - a >= 2) track.append(el('div', {class: 'tl-interval', style: {left: `${a}px`, width: `${b - a}px`, top: `${y + 3}px`}}));
    }
    if (item.end != null && item.end > item.t) {
      const b = Math.min(width, M.timeToX(item.end, state.view, width));
      if (b - x >= 2) track.append(el('div', {class: 'tl-duration', style: {left: `${x}px`, width: `${b - x}px`, top: `${y + 2}px`}}));
    }
    const marker = el('button', {type: 'button', class: `tl-item tl-kind-${item.kind}${item.basis === 'local_as_utc_unknown_zone' ? ' tl-unknown' : ''}`,
      'data-key': item.key, title: `${item.display} (precyzja: ${item.precision}; ${BASIS[item.basis] ?? item.basis}) · ${item.title}`,
      'aria-label': `${item.title}, ${item.display}`,
      style: {left: `${x - 4}px`, top: `${y}px`}});
    marker.addEventListener('click', event => {
      event.stopPropagation();
      for (const active of lanesEl.querySelectorAll('.tl-item.tl-active')) active.classList.remove('tl-active');
      marker.classList.add('tl-active');
      showDetail(item.key);
    });
    track.append(marker);
  }
  for (const cluster of overflow) {
    const more = el('button', {type: 'button', class: 'tl-overflow', title: `${cluster.count} kolejnych elementów — przybliż`,
      style: {left: `${cluster.x0}px`}}, `+${cluster.count}`);
    more.addEventListener('click', event => {
      event.stopPropagation();
      const t = M.xToTime((cluster.x0 + cluster.x1) / 2, state.view, width);
      state.view = M.zoom(state.view, -2, (t - state.view.start) / (state.view.end - state.view.start));
      refresh();
    });
    track.append(more);
  }
}

function drawRuler(width) {
  ruler.replaceChildren();
  ruler.style.setProperty('--label-width', `${LABEL}px`);
  for (const tick of M.ticks(state.view, width, 95)) {
    const x = M.timeToX(tick.t, state.view, width);
    ruler.append(el('span', {class: 'tl-tick', style: {left: `${LABEL + x}px`}}, tick.label));
  }
}

function describeSpan(ms) {
  if (ms >= 365 * M.DAY) return `${(ms / M.YEAR_APPROX).toFixed(1)} lat`;
  if (ms >= 2 * M.DAY) return `${Math.round(ms / M.DAY)} dni`;
  if (ms >= 2 * M.HOUR) return `${Math.round(ms / M.HOUR)} h`;
  return `${Math.round(ms / M.MINUTE)} min`;
}

function exportQuery(params, bounds, format) {
  return '/api/timeline/export?' + M.buildQuery({format, ...params, ...bounds});
}

function updateExports(params, bounds) {
  for (const link of root.querySelectorAll('[data-export]')) link.href = exportQuery(params, bounds, link.dataset.export);
}

function dl(entries) {
  const list = el('dl');
  for (const [term, value] of entries) {
    if (value == null || value === '') continue;
    list.append(el('dt', {}, term));
    const dd = el('dd');
    if (value instanceof Node) dd.append(value); else dd.textContent = String(value);
    list.append(dd);
  }
  return list;
}

function zoneQuery() {
  const {assume_zone, assumption_rule} = formValues().zone;
  return M.buildQuery({assume_zone, assumption_rule});
}

async function showDetail(key) {
  detailEl.replaceChildren(el('h2', {}, 'Szczegóły'), el('p', {}, 'Ładowanie…'));
  root.dataset.detailKey = 'loading';
  try {
    const item = await getJSON(`/api/timeline/items/${encodeURIComponent(key)}?${zoneQuery()}`);
    renderDetail(item);
    root.dataset.detailKey = key;
  } catch (error) {
    detailEl.replaceChildren(el('h2', {}, 'Szczegóły'), el('p', {role: 'alert'}, `Błąd: ${error.message}`));
    root.dataset.detailKey = 'error';
  }
}

function renderDetail(item) {
  const t = item.time;
  const parts = [el('h2', {}, 'Szczegóły'), el('h3', {}, item.title || item.key)];
  const interval = t.interval ? `${t.interval.from} – ${t.interval.to} (szer. ${formatSeconds(t.uncertainty_seconds)})` : null;
  parts.push(dl([
    ['Element', `${item.key} (${item.kind})`], ['Typ', item.event_type], ['Czas wyświetlany', t.display ?? 'brak'],
    ['Podstawa', `${BASIS[t.basis] ?? t.basis}`], ['Precyzja', t.precision], ['Przedział niepewności', interval],
    ['Koniec', t.end], ['Wybrany kandydat', t.candidate_ref ? `${t.candidate_ref} (${t.candidate_kind})` : null],
    ['Flagi', Object.keys(t.flags || {}).length ? JSON.stringify(t.flags) : null],
    ['Reguła', item.rule ? `${item.rule.rule_id}@${item.rule.version} · ${item.rule.sha256.slice(0, 12)}` : null],
    ['Kontrahent', item.counterparty], ['Chatbot', item.chatbot], ['Wątek', item.thread_id],
    ['Język', item.language ? `${item.language} (${item.language_basis})` : null],
    ['Wersja transkrypcji', item.transcript_version ? `#${item.transcript_version.id}${item.transcript_version.is_latest ? ' (najnowsza)' : ''}` : null],
    ['Tagi', item.tags?.length ? item.tags.join(', ') : null],
  ]));
  if (item.text != null) parts.push(el('pre', {class: 'tl-text transcript', 'data-detail-text': ''}, item.text + (item.text_truncated ? ' …' : '')));
  const links = el('p', {class: 'tl-links'});
  if (item.links?.artifact) links.append(el('a', {href: item.links.artifact}, 'Artefakt / źródło'));
  if (item.links?.citation_workspace) links.append(' · ', el('a', {href: item.links.citation_workspace}, 'Cytat (wybór wersji i zakresu)'));
  if (item.links?.transcript) links.append(' · ', el('a', {href: item.links.transcript}, `Transkrypcja #${item.derived_text_id} (JSON)`));
  if (links.childNodes.length) parts.push(links);
  parts.push(el('h3', {}, `Kandydaci czasu (${item.candidates?.length ?? 0})`));
  const list = el('div', {class: 'tl-candidates', 'data-candidates': ''});
  for (const c of item.candidates || []) {
    const card = el('article', {class: `tl-candidate${c.selected ? ' tl-selected' : ''}${c.eligible ? '' : ' tl-ineligible'}`, 'data-candidate': c.ref});
    const state = c.selected ? 'wybrany' : !c.usable ? `nieużywalny: ${c.unusable_reason}` : !c.eligible ? `poza wyborem: ${c.not_eligible_reason}` : 'kandydat';
    card.append(el('div', {class: 'tl-candidate-head'}, null));
    card.firstChild.append(el('code', {}, c.ref), el('span', {class: 'tl-badge'}, state));
    card.append(dl([
      ['Pochodzenie', c.origin], ['Rodzaj', c.kind], ['Surowa wartość', c.raw], ['UTC', c.utc], ['Lokalny', c.local],
      ['Strefa', `${c.tz_status}${c.tz ? ' · ' + c.tz : ''}`], ['Precyzja', c.precision], ['Źródło', c.source],
      ['Wyprowadzony z', c.derived_from?.join(', ')],
      ['Położenie', c.placement ? `${c.placement.display} · ${BASIS[c.placement.basis] ?? c.placement.basis} · przedział ${c.placement.interval.from} – ${c.placement.interval.to}` : null],
      ['Metadane', Object.keys(c.metadata || {}).length ? JSON.stringify(c.metadata) : null],
    ]));
    list.append(card);
  }
  parts.push(list);
  if (item.hypotheses?.length) {
    parts.push(el('h3', {}, 'Hipotezy korelacji'));
    const list = el('ul');
    for (const h of item.hypotheses) list.append(el('li', {}, `#${h.id} ${h.rule_id}@${h.rule_version}: ${h.left_kind} ${h.left_id} ↔ ${h.right_kind} ${h.right_id} — ${h.status}${h.candidate_count > 1 ? ` (niejednoznaczne: ${h.candidate_count} kandydatów)` : ''}`));
    parts.push(list);
  }
  detailEl.replaceChildren(...parts);
}

function formatSeconds(seconds) {
  if (seconds == null) return '';
  if (seconds >= 3600) return `${(seconds / 3600).toFixed(1)} h`;
  if (seconds >= 60) return `${(seconds / 60).toFixed(1)} min`;
  return `${seconds} s`;
}

function setSelection(range) {
  state.selection = range;
  const width = trackWidth();
  if (!range) {selectionEl.hidden = true; rangeEl.hidden = true; root.dataset.selection = ''; return;}
  const a = M.timeToX(range.start, state.view, width), b = M.timeToX(range.end, state.view, width);
  Object.assign(selectionEl.style, {left: `${LABEL + a}px`, width: `${Math.max(1, b - a)}px`, top: `${lanesEl.scrollTop}px`,
    height: `${lanesEl.clientHeight}px`});
  selectionEl.hidden = false;
  const bounds = {from: M.isoBound(range.start), to: M.isoBound(range.end)};
  const params = common();
  const report = state.reportsAvailable
    ? el('a', {href: '/reports/new?' + M.buildQuery({...bounds, ...params}), 'data-report-link': ''}, 'Raport z zakresu')
    : el('a', {class: 'tl-disabled', 'aria-disabled': 'true', 'data-report-link': '', role: 'link',
      title: 'Raporty z zakresu dostarczy późniejszy tor (F): trasa /reports/new jeszcze nie istnieje.'}, 'Raport z zakresu (niedostępny)');
  const zoomButton = el('button', {type: 'button', 'data-action': 'zoom-selection'}, 'Przybliż do zakresu');
  zoomButton.addEventListener('click', () => {state.view = M.clampView(range); setSelection(null); refresh();});
  const clear = el('button', {type: 'button'}, 'Wyczyść');
  clear.addEventListener('click', () => setSelection(null));
  rangeEl.replaceChildren(el('h2', {}, 'Zaznaczony zakres'), dl([['Od', bounds.from], ['Do', bounds.to]]),
    el('p', {class: 'tl-links'}, null), zoomButton, clear);
  const links = rangeEl.querySelector('.tl-links');
  links.append(report, ' · ', el('a', {href: exportQuery(params, bounds, 'csv'), download: '', 'data-range-export': 'csv'}, 'Eksport CSV'),
    ' · ', el('a', {href: exportQuery(params, bounds, 'json'), download: '', 'data-range-export': 'json'}, 'JSON'));
  rangeEl.hidden = false;
  root.dataset.selection = `${bounds.from}/${bounds.to}`;
}

function installSelection(surface) {
  let origin = null;
  surface.addEventListener('pointerdown', event => {
    if (event.button !== 0 || event.target.closest('.tl-item,.tl-overflow,.tl-bar,button,a')) return;
    const box = lanesEl.getBoundingClientRect();
    origin = event.clientX - box.left - LABEL;
    surface.setPointerCapture?.(event.pointerId);
  });
  surface.addEventListener('pointermove', event => {
    if (origin == null) return;
    const box = lanesEl.getBoundingClientRect();
    const x = event.clientX - box.left - LABEL;
    if (Math.abs(x - origin) < 4) return;
    const width = trackWidth();
    const a = Math.max(0, Math.min(origin, x)), b = Math.min(width, Math.max(origin, x));
    Object.assign(selectionEl.style, {left: `${LABEL + a}px`, width: `${b - a}px`, top: `${lanesEl.scrollTop}px`,
      height: `${lanesEl.clientHeight}px`});
    selectionEl.hidden = false;
  });
  surface.addEventListener('pointerup', event => {
    if (origin == null) return;
    const box = lanesEl.getBoundingClientRect();
    const x = event.clientX - box.left - LABEL;
    const start = origin;
    origin = null;
    if (Math.abs(x - start) < 4) {selectionEl.hidden = state.selection == null; return;}
    setSelection(M.selectionRange(start, x, state.view, trackWidth()));
  });
}

async function showUndated() {
  undatedEl.hidden = false;
  undatedEl.replaceChildren(el('h2', {}, 'Bez czasu'), el('p', {}, 'Ładowanie…'));
  try {
    const page = await getJSON('/api/timeline/undated?' + M.buildQuery({...common(), limit: 100}));
    const list = el('ul', {class: 'tl-undated'});
    for (const item of page.items) {
      const button = el('button', {type: 'button', class: 'tl-linkish', 'data-key': item.key}, `${item.key} · ${item.title ?? ''}`);
      button.addEventListener('click', () => showDetail(item.key));
      const reason = item.time?.flags?.undated_reason;
      list.append(el('li', {}, null));
      list.lastChild.append(button, el('small', {}, reason ? ` — ${reason}` : ''));
    }
    undatedEl.replaceChildren(el('h2', {}, 'Bez czasu'),
      el('p', {}, `Kolejność: ${page.order}. Elementy bez użytecznego czasu nie są sortowane według czasu importu.`), list,
      el('p', {}, page.has_more ? 'Pokazano pierwsze 100; zawęź filtry.' : (page.items.length ? 'Koniec listy.' : 'Brak elementów bez czasu dla tych filtrów.')));
  } catch (error) {
    undatedEl.replaceChildren(el('h2', {}, 'Bez czasu'), el('p', {role: 'alert'}, `Błąd: ${error.message}`));
  }
}

function act(action) {
  if (action === 'zoom-in') state.view = M.zoom(state.view, -1);
  else if (action === 'zoom-out') state.view = M.zoom(state.view, 1);
  else if (action === 'pan-left') state.view = M.pan(state.view, -0.25);
  else if (action === 'pan-right') state.view = M.pan(state.view, 0.25);
  else if (action === 'fit') return fit();
  else if (action === 'undated') return showUndated();
  else return;
  setSelection(null);
  refresh();
}

async function fit() {
  // Extent of the items matching the current filters (not of the whole catalog).
  try {
    const density = await getJSON('/api/timeline/density?' + M.buildQuery({...common(), bucket: 'year', group: '', max_buckets: 5000, max_lanes: 1}));
    const from = density.total ? density.window.from_us / 1000 : state.extent?.from;
    const to = density.total ? density.window.to_us / 1000 : state.extent?.to;
    state.view = M.viewForExtent(from, to);
  } catch (error) {
    state.view = M.viewForExtent(state.extent?.from, state.extent?.to);
  }
  setSelection(null);
  return refresh();
}

function fillFacets(facets) {
  const options = {
    event_types: facets.event_types.map(f => [f.value, `${f.value} (${f.count})`]),
    chatbots: facets.chatbots.map(f => [f.value, `${f.value} (${f.count})`]),
    sources: facets.sources.map(f => [f.value, `${f.label ?? f.value} (${f.count})`]),
    collections: facets.collections.map(f => [f.value, `${f.label ?? f.value} (${f.count})`]),
    tags: facets.tags.map(name => [name, name]),
  };
  for (const select of root.querySelectorAll('[data-facet]')) {
    const chosen = new Set([...select.selectedOptions].map(o => o.value));
    select.replaceChildren(...(options[select.dataset.facet] || []).map(([value, label]) => {
      const option = el('option', {value}, label);
      option.selected = chosen.has(String(value));
      return option;
    }));
  }
}

async function loadFacets() {
  const facets = await getJSON('/api/timeline/facets?' + zoneQuery());
  fillFacets(facets);
  state.extent = {from: facets.extent.from_us == null ? null : facets.extent.from_us / 1000,
                  to: facets.extent.to_us == null ? null : facets.extent.to_us / 1000};
  return facets;
}

async function start() {
  try {
    await loadFacets();
  } catch (error) {
    setStatus(`Błąd: ${error.message}`);
    root.dataset.state = 'error';
    return;
  }
  state.view = M.viewForExtent(state.extent.from, state.extent.to);
  root.addEventListener('click', event => {
    const button = event.target.closest('[data-action]');
    if (button && root.contains(button) && !button.closest('[data-range-panel]')) act(button.dataset.action);
  });
  form.addEventListener('submit', event => {event.preventDefault(); setSelection(null); refresh();});
  form.querySelector('[data-group]').addEventListener('change', () => refresh());
  lanesEl.addEventListener('scroll', () => requestAnimationFrame(renderRows), {passive: true});
  installSelection(lanesEl);
  installSelection(ruler);
  document.addEventListener('keydown', event => {
    if (event.target.closest?.('input,select,textarea') || event.altKey || event.ctrlKey || event.metaKey) return;
    const action = {ArrowLeft: 'pan-left', ArrowRight: 'pan-right', '+': 'zoom-in', '=': 'zoom-in', '-': 'zoom-out', '_': 'zoom-out'}[event.key];
    if (action) {event.preventDefault(); act(action);}
    else if (event.key === 'Escape') setSelection(null);
  });
  let resizeTimer;
  window.addEventListener('resize', () => {clearTimeout(resizeTimer); resizeTimer = setTimeout(refresh, 150);});
  await refresh();
}

start();
