// /location page: map (local projection, no external requests unless a recorded tile opt-in), time slider,
// presence table and lanes. Every text from data is set with textContent; nothing is parsed as HTML.
import {BASIS_COLORS, fitView, isoOfUs, metersToPixels, polylines, tilesForView, tileUrl, timeScale, toPixel,
  formatDistance, METERS_PER_DEGREE} from './location-math.mjs';

const SVG = 'http://www.w3.org/2000/svg';
const root = document.querySelector('[data-location]');
const config = JSON.parse(document.getElementById('location-config').textContent || '{}');
const $ = sel => root.querySelector(sel);
const status = $('[data-loc-status]');
const map = $('[data-loc-map]');
const lanesSvg = $('[data-loc-lanes]');
const detail = $('[data-loc-detail]');
const form = $('[data-loc-form]');
const params = new URLSearchParams(location.search);
const state = {from: params.get('from') || '', to: params.get('to') || '', projection: params.get('projection') ||
  (config.tiles ? 'mercator' : 'equirectangular'), maxPoints: Number(params.get('max_points') || 5000), data: null};

function el(name, attrs = {}, text) {
  const node = document.createElementNS(SVG, name);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) node.setAttribute(k, String(v));
  if (text !== undefined) node.textContent = text;
  return node;
}

function title(node, text) {
  node.appendChild(el('title', {}, text));
  return node;
}

function setStatus(text) { status.textContent = text; }

function extentUs() {
  const ext = config.extent || {};
  const a = ext.from_utc ? Date.parse(ext.from_utc) * 1000 : null;
  const b = ext.to_utc ? Date.parse(ext.to_utc) * 1000 + 1_000_000 : null;
  return a !== null && b !== null && b > a ? [a, b] : null;
}

function syncInputs() {
  form.elements.from.value = state.from;
  form.elements.to.value = state.to;
  form.elements.projection.value = state.projection;
  form.elements.max_points.value = String(state.maxPoints);
  const link = $('[data-loc-timeline]');
  const q = new URLSearchParams();
  if (state.from) q.set('from', state.from);
  if (state.to) q.set('to', state.to);
  link.href = '/timeline' + (q.toString() ? `?${q}` : '');
  const ext = extentUs();
  if (ext) {
    const pos = text => text ? Math.round((Date.parse(text) * 1000 - ext[0]) / (ext[1] - ext[0]) * 1000) : null;
    const s = pos(state.from), e = pos(state.to);
    $('[data-loc-slide-start]').value = String(Math.max(0, Math.min(1000, s ?? 0)));
    $('[data-loc-slide-end]').value = String(Math.max(0, Math.min(1000, e ?? 1000)));
  }
}

function pushUrl() {
  const q = new URLSearchParams();
  if (state.from) q.set('from', state.from);
  if (state.to) q.set('to', state.to);
  if (state.projection !== 'equirectangular') q.set('projection', state.projection);
  if (config.tiles) q.set('tile_opt_in', String(config.tiles.id));
  history.replaceState(null, '', '/location' + (q.toString() ? `?${q}` : ''));
}

async function getJSON(url) {
  const response = await fetch(url, {headers: {Accept: 'application/json'}});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `HTTP ${response.status}`);
  return body;
}

function query() {
  const q = new URLSearchParams();
  if (state.from) q.set('from', state.from);
  if (state.to) q.set('to', state.to);
  return q;
}

async function load() {
  root.dataset.state = 'loading';
  setStatus('Wczytywanie…');
  syncInputs();
  pushUrl();
  try {
    const q = query();
    q.set('max_points', String(state.maxPoints));
    const [data, lanes] = await Promise.all([getJSON(`/api/location/window?${q}`),
      getJSON(`/api/location/lanes?${query()}`)]);
    state.data = data;
    drawMap(data);
    drawPresence(data.presence);
    drawLanes(lanes);
    const f = data.fixes;
    setStatus(`Próbki w zakresie: ${f.total_in_window}, pokazane: ${f.returned}` +
      (f.thinned ? ` (co ${f.stride}. – przerzedzenie deterministyczne)` : '') +
      ` · zdarzenia: ${data.events.items.length}${data.events.truncated ? '+' : ''}` +
      ` · próbki bez czasu: ${f.untimed_total} · reguła osi czasu: ${lanes.source_rule || '—'}`);
    root.dataset.state = 'ready';
  } catch (error) {
    setStatus(`Błąd: ${error.message}`);
    root.dataset.state = 'error';
  }
}

function drawMap(data) {
  map.replaceChildren();
  const width = 900, height = 560;
  map.appendChild(el('rect', {class: 'loc-bg', x: 0, y: 0, width, height}));
  const fixes = data.fixes.items;
  const events = data.events.items;
  const places = (data.presence && data.presence.places) || [];
  const anchors = [...fixes, ...events.flatMap(e => [e.location, e.end_location].filter(Boolean))];
  const view = fitView(anchors.length ? anchors : places, width, height, {projection: state.projection});
  const note = $('[data-loc-map-note]');
  note.textContent = state.projection === 'mercator' ? 'Web Mercator' : 'lokalne równoodległościowe';
  if (config.tiles && state.projection === 'mercator') {
    const group = el('g', {'data-loc-tile-layer': ''});
    for (const t of tilesForView(view)) {
      group.appendChild(el('image', {href: tileUrl(config.tiles.template, t.z, t.x, t.y), x: t.left, y: t.top,
        width: t.size, height: t.size, preserveAspectRatio: 'none', 'data-tile': `${t.z}/${t.x}/${t.y}`}));
    }
    map.appendChild(group);
    note.textContent += ` · kafelki: ${config.tiles.origin} (zgoda #${config.tiles.id})`;
  } else if (config.tiles) {
    note.textContent += ' · kafelki tylko w Web Mercator';
  }
  if (!anchors.length && !places.length) {
    map.appendChild(el('text', {class: 'loc-empty', x: 24, y: 40}, 'Brak położeń w tym zakresie.'));
    return;
  }
  const placeLayer = el('g', {'data-loc-places': ''});
  for (const p of places) {
    const c = toPixel(p.lat, p.lon, view);
    const r = Math.max(2, metersToPixels(p.radius_m, p.lat, view));
    if (c.x + r < -50 || c.x - r > width + 50 || c.y + r < -50 || c.y - r > height + 50) continue;
    const g = el('g', {'data-place': p.id});
    g.appendChild(title(el('circle', {class: 'loc-place', cx: c.x, cy: c.y, r}),
      `${p.label} · promień ${formatDistance(p.radius_m)}${p.kind ? ' · ' + p.kind : ''}`));
    g.appendChild(el('text', {class: 'loc-place-label', x: c.x + r + 4, y: c.y + 4}, p.label));
    placeLayer.appendChild(g);
  }
  map.appendChild(placeLayer);
  const lineLayer = el('g', {'data-loc-tracks': ''});
  for (const line of polylines(fixes.filter(f => f.basis !== 'declared'), 15 * 60 * 1e6)) {
    const pts = line.points.map(f => toPixel(f.lat, f.lon, view)).map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`);
    const provider = line.points.every(f => f.basis !== 'raw_sample');
    lineLayer.appendChild(title(el('polyline', {class: `loc-track${provider ? ' loc-track-provider' : ''}`,
      points: pts.join(' '), 'data-stream': line.stream}), `strumień ${line.stream} · ${line.points.length} próbek`));
  }
  map.appendChild(lineLayer);
  const eventLayer = el('g', {'data-loc-events': ''});
  for (const e of events) {
    if (e.type === 'location_segment' && e.location && e.end_location) {
      const a = toPixel(e.location.lat, e.location.lon, view), b = toPixel(e.end_location.lat, e.end_location.lon, view);
      eventLayer.appendChild(title(el('line', {class: 'loc-segment', x1: a.x, y1: a.y, x2: b.x, y2: b.y,
        'data-event-id': e.id}), `${e.subject || 'odcinek'} · wnioskowanie dostawcy · ${e.start} – ${e.end || ''}`));
    }
    if ((e.type === 'location_visit' || e.type === 'location_waypoint') && e.location) {
      const c = toPixel(e.location.lat, e.location.lon, view);
      const visit = e.type === 'location_visit';
      const shape = visit
        ? el('rect', {class: `loc-visit${e.user_edit && e.user_edit.flagged ? ' loc-visit-edit' : ''}`, x: c.x - 6, y: c.y - 6,
          width: 12, height: 12, transform: `rotate(45 ${c.x} ${c.y})`, 'data-event-id': e.id, tabindex: 0})
        : el('rect', {class: 'loc-fix loc-fix-declared', x: c.x - 4, y: c.y - 4, width: 8, height: 8, 'data-event-id': e.id,
          tabindex: 0});
      const labels = e.provider_labels ? Object.entries(e.provider_labels).map(([k, v]) => `${k}=${v}`).join(', ') : '';
      title(shape, `${visit ? 'wizyta (wnioskowanie dostawcy)' : 'punkt zadeklarowany'}: ${e.subject || ''} · ${e.start}` +
        (e.end ? ` – ${e.end}` : '') + (labels ? ` · etykiety dostawcy: ${labels}` : '') +
        (e.user_edit && e.user_edit.flagged ? ' · edytowane przez użytkownika u dostawcy' : ''));
      shape.addEventListener('click', () => showEvent(e));
      eventLayer.appendChild(shape);
    }
  }
  map.appendChild(eventLayer);
  const fixLayer = el('g', {'data-loc-fixes': ''});
  for (const f of fixes) {
    const c = toPixel(f.lat, f.lon, view);
    if (c.x < -10 || c.x > width + 10 || c.y < -10 || c.y > height + 10) continue;
    const node = f.basis === 'declared'
      ? el('rect', {class: 'loc-fix loc-fix-declared', x: c.x - 2.5, y: c.y - 2.5, width: 5, height: 5})
      : el('circle', {class: `loc-fix loc-fix-${f.basis}`, cx: c.x, cy: c.y, r: 3});
    node.setAttribute('data-fix', String(f.id));
    title(node, `${f.t} · ${f.kind} · ${f.basis}${f.accuracy_m != null ? ` · dokładność ${formatDistance(f.accuracy_m)}` : ''}` +
      ` · ${f.lat}, ${f.lon} (${f.decimals} miejsc)`);
    fixLayer.appendChild(node);
  }
  map.appendChild(fixLayer);
  // scale bar: 100 px in metres at the view centre
  const metres = 100 / view.scale * METERS_PER_DEGREE * (state.projection === 'mercator' ? Math.cos(view.lat0 * Math.PI / 180) : 1);
  map.appendChild(el('line', {class: 'loc-scale', x1: 20, y1: height - 20, x2: 120, y2: height - 20}));
  map.appendChild(el('text', {class: 'loc-scale-label', x: 20, y: height - 26}, `≈ ${formatDistance(metres)}`));
}

function showEvent(e) {
  detail.replaceChildren();
  const strong = document.createElement('strong');
  strong.textContent = `${e.type} #${e.id}`;
  const link = document.createElement('a');
  link.href = `/api/timeline/items/event:${e.id}`;
  link.textContent = ' szczegóły na osi czasu';
  const text = document.createElement('span');
  text.textContent = ` · ${e.subject || ''} · ${e.start || ''}${e.end ? ' – ' + e.end : ''} · sygnał: ` +
    `${(e.signal && e.signal.class) || '?'} / ${(e.signal && e.signal.basis) || '?'}`;
  detail.append(strong, text, link);
}

function drawPresence(run) {
  const body = $('[data-loc-presence] tbody');
  body.replaceChildren();
  const label = $('[data-loc-run]');
  if (!run) {
    label.textContent = '(brak przebiegów)';
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 5;
    td.textContent = 'Uruchom presence-run z prywatnym plikiem miejsc.';
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  label.textContent = `przebieg #${run.run_id} · zestaw ${run.place_set.version} · ${run.place_set.sha256.slice(0, 12)}`;
  const names = Object.fromEntries((run.places || []).map(p => [p.id, p.label]));
  for (const i of run.intervals || []) {
    const tr = document.createElement('tr');
    tr.dataset.interval = String(i.id);
    tr.dataset.status = i.status;
    const cells = [names[i.place_id] || i.place_id, i.status === 'inside' ? 'wewnątrz' : 'niepewne',
      `${i.start_utc} – ${i.end_utc}`, `${i.outer_start_utc || '—'} – ${i.outer_end_utc || '—'}`,
      `${i.fixes} (${i.inside_fixes}/${i.uncertain_fixes})`];
    for (const text of cells) {
      const td = document.createElement('td');
      td.textContent = text;
      tr.appendChild(td);
    }
    body.appendChild(tr);
  }
}

function drawLanes(data) {
  lanesSvg.replaceChildren();
  const lanes = data.lanes.filter(l => l.items.length);
  const width = 1200, labelW = 210, laneH = 30, top = 24;
  const height = Math.max(80, top + Math.max(1, lanes.length) * laneH + 10);
  lanesSvg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  const all = lanes.flatMap(l => l.items);
  let t0 = state.from ? Date.parse(state.from) * 1000 : Math.min(...all.map(i => (i.t_lo ?? i.t_start) * 1e6));
  let t1 = state.to ? Date.parse(state.to) * 1000 : Math.max(...all.map(i => ((i.t_end ?? i.t_hi ?? i.t_start)) * 1e6));
  if (!all.length || !Number.isFinite(t0) || !Number.isFinite(t1)) {
    lanesSvg.appendChild(el('text', {class: 'loc-lane-label', x: 12, y: 40}, 'Brak zdarzeń lokalizacji w zakresie.'));
    return;
  }
  if (t1 <= t0) t1 = t0 + 3600e6;
  const x = timeScale(t0, t1, labelW, width - 10);
  lanesSvg.appendChild(el('line', {class: 'loc-axis', x1: labelW, y1: top - 4, x2: width - 10, y2: top - 4}));
  for (let k = 0; k <= 4; k++) {
    const t = t0 + (t1 - t0) * k / 4;
    lanesSvg.appendChild(el('text', {class: 'loc-axis-label', x: x(t), y: 14, 'text-anchor': k === 0 ? 'start' : k === 4 ? 'end' : 'middle'},
      isoOfUs(t)));
  }
  lanes.forEach((lane, index) => {
    const y = top + index * laneH;
    const g = el('g', {'data-lane': lane.id});
    g.appendChild(el('rect', {class: 'loc-lane-bg', x: 0, y, width, height: laneH - 2}));
    g.appendChild(el('text', {class: 'loc-lane-label', x: 8, y: y + laneH / 2 + 4}, `${lane.label} (${lane.items.length}${lane.truncated ? '+' : ''})`));
    for (const item of lane.items) {
      const basis = (item.signal && item.signal.basis) || 'raw_sample';
      const color = BASIS_COLORS[basis] || BASIS_COLORS.raw_sample;
      const s = item.t_start * 1e6, e = (item.t_end ?? item.t_start) * 1e6;
      const lo = (item.t_lo ?? item.t_start) * 1e6, hi = (item.t_hi ?? item.t_start) * 1e6;
      const node = el('g', {'data-lane-item': item.event_id, tabindex: 0});
      if (hi > lo) node.appendChild(el('line', {class: 'loc-whisker', x1: x(lo), x2: x(hi), y1: y + laneH / 2, y2: y + laneH / 2, stroke: color}));
      const w = Math.max(3, x(e) - x(s));
      node.appendChild(el('rect', {class: 'loc-bar', x: x(s), y: y + 6, width: w, height: laneH - 14, rx: 2, fill: color}));
      title(node, `${item.event_type} #${item.event_id} · ${isoOfUs(s)}${e > s ? ' – ' + isoOfUs(e) : ''} · podstawa: ${basis}` +
        ` · czas: ${item.basis || '?'}`);
      node.addEventListener('click', () => showEvent({id: item.event_id, type: item.event_type, start: isoOfUs(s),
        end: e > s ? isoOfUs(e) : null, signal: item.signal}));
      g.appendChild(node);
    }
    lanesSvg.appendChild(g);
  });
}

function slide() {
  const ext = extentUs();
  if (!ext) return;
  const a = Number($('[data-loc-slide-start]').value), b = Number($('[data-loc-slide-end]').value);
  const lo = Math.min(a, b), hi = Math.max(a, b);
  state.from = isoOfUs(Math.floor(ext[0] + (ext[1] - ext[0]) * lo / 1000));
  state.to = isoOfUs(Math.ceil(ext[0] + (ext[1] - ext[0]) * hi / 1000));
  load();
}

form.addEventListener('submit', event => {
  event.preventDefault();
  state.from = form.elements.from.value.trim();
  state.to = form.elements.to.value.trim();
  state.projection = form.elements.projection.value;
  state.maxPoints = Math.max(1, Math.min(20000, Number(form.elements.max_points.value) || 5000));
  load();
});
$('[data-loc-all]').addEventListener('click', () => { state.from = ''; state.to = ''; load(); });
$('[data-loc-slide-start]').addEventListener('change', slide);
$('[data-loc-slide-end]').addEventListener('change', slide);
form.elements.projection.addEventListener('change', () => { state.projection = form.elements.projection.value; load(); });

$('[data-loc-tile-form]').addEventListener('submit', async event => {
  event.preventDefault();
  const f = event.currentTarget;
  try {
    const response = await fetch('/api/location/tiles/opt-in', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({template: f.elements.template.value.trim(), acknowledge_disclosure: f.elements.acknowledge.checked})});
    const body = await response.json();
    if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `HTTP ${response.status}`);
    location.href = body.page;
  } catch (error) {
    setStatus(`Kafelki: ${error.message}`);
  }
});

load();
