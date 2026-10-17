// Pure projection and layout math for /location (no DOM; unit-tested by server/tests/js/location-math.test.mjs).
// Local projections only: nothing here contacts a server. Map tiles are an explicit, recorded opt-in elsewhere.

export const EARTH_RADIUS_M = 6371008.8;  // same sphere as server/app/location/geo.py
export const METERS_PER_DEGREE = Math.PI * EARTH_RADIUS_M / 180;
export const MERCATOR_MAX_LAT = 85.0511287798066;  // atan(sinh(pi))
export const BASIS_COLORS = {raw_sample: '#3987e5', provider_inference: '#d95926', provider_user_edit: '#d95926',
  derived: '#199e70', declared: '#c98500'};

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/** Longitude difference wrapped into [-180, 180). */
export function wrapLon(delta) {
  const d = ((delta + 180) % 360 + 360) % 360 - 180;
  return Object.is(d, -0) ? 0 : d;
}

/** Web-Mercator y in "degrees" units (same scale as longitude), clamped at the projection limit. */
export function mercatorY(lat) {
  const phi = clamp(lat, -MERCATOR_MAX_LAT, MERCATOR_MAX_LAT) * Math.PI / 180;
  return Math.log(Math.tan(Math.PI / 4 + phi / 2)) * 180 / Math.PI;
}

export function inverseMercatorY(y) {
  return (2 * Math.atan(Math.exp(y * Math.PI / 180)) - Math.PI / 2) * 180 / Math.PI;
}

/**
 * Centre longitude of a set of longitudes on the smallest arc that holds them all
 * (so a cluster across the antimeridian is not stretched over the whole globe).
 */
export function arcCentre(lons) {
  if (!lons.length) return 0;
  const sorted = [...lons].map(l => wrapLon(l)).sort((a, b) => a - b);
  let gap = sorted[0] + 360 - sorted[sorted.length - 1], start = sorted[0];
  for (let i = 1; i < sorted.length; i++) {
    const g = sorted[i] - sorted[i - 1];
    if (g > gap) { gap = g; start = sorted[i]; }
  }
  const span = 360 - gap;
  return wrapLon(start + span / 2);
}

/** Planar coordinates (x right, y up, both in degree units) of a point for a projection centred at lon0/lat0. */
export function planar(lat, lon, view) {
  const dx = wrapLon(lon - view.lon0);
  if (view.projection === 'mercator') return {x: dx, y: mercatorY(lat)};
  return {x: dx * Math.cos(view.lat0 * Math.PI / 180), y: lat};  // local equirectangular
}

/**
 * Fit a view to points ({lat, lon}) inside width x height pixels with padding.
 * Returns {projection, lon0, lat0, scale (px per planar unit), cx, cy, width, height}.
 */
export function fitView(points, width, height, {projection = 'equirectangular', padding = 24, minSpanM = 200} = {}) {
  const valid = points.filter(p => Number.isFinite(p.lat) && Number.isFinite(p.lon));
  const lat0 = valid.length ? valid.reduce((s, p) => s + p.lat, 0) / valid.length : 0;
  const lon0 = arcCentre(valid.map(p => p.lon));
  const view = {projection, lon0, lat0: clamp(lat0, -89.999, 89.999), width, height};
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const p of valid) {
    const q = planar(p.lat, p.lon, view);
    minX = Math.min(minX, q.x); maxX = Math.max(maxX, q.x); minY = Math.min(minY, q.y); maxY = Math.max(maxY, q.y);
  }
  if (!valid.length) { minX = maxX = 0; minY = maxY = 0; }
  const minSpan = minSpanM / METERS_PER_DEGREE;
  const spanX = Math.max(maxX - minX, minSpan), spanY = Math.max(maxY - minY, minSpan);
  const scale = Math.min((width - 2 * padding) / spanX, (height - 2 * padding) / spanY);
  return {...view, scale, cx: (minX + maxX) / 2, cy: (minY + maxY) / 2};
}

/** Pixel position of a point in a fitted view (SVG y grows downwards). */
export function toPixel(lat, lon, view) {
  const q = planar(lat, lon, view);
  return {x: view.width / 2 + (q.x - view.cx) * view.scale, y: view.height / 2 - (q.y - view.cy) * view.scale};
}

/** Radius in pixels of a circle of radiusM metres around latitude lat (north-south extent). */
export function metersToPixels(radiusM, lat, view) {
  const dLat = radiusM / METERS_PER_DEGREE;
  if (view.projection === 'mercator') {
    const top = mercatorY(clamp(lat + dLat, -MERCATOR_MAX_LAT, MERCATOR_MAX_LAT));
    const bottom = mercatorY(clamp(lat - dLat, -MERCATOR_MAX_LAT, MERCATOR_MAX_LAT));
    return Math.abs(top - bottom) / 2 * view.scale;
  }
  return dLat * view.scale;
}

/** Haversine distance in metres (display only; the server computes every stored number). */
export function haversine(lat1, lon1, lat2, lon2) {
  const r = Math.PI / 180, p1 = lat1 * r, p2 = lat2 * r;
  const a = Math.sin((p2 - p1) / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin((lon2 - lon1) * r / 2) ** 2;
  return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(Math.max(0, a))));
}

/** Polylines: consecutive points of one stream joined while the time gap is at most maxGapUs. */
export function polylines(fixes, maxGapUs) {
  const out = [];
  const byStream = new Map();
  for (const f of fixes) {
    if (!byStream.has(f.stream)) byStream.set(f.stream, []);
    byStream.get(f.stream).push(f);
  }
  for (const [stream, list] of byStream) {
    list.sort((a, b) => a.t_us - b.t_us);
    let current = [];
    for (const f of list) {
      if (current.length && f.t_us - current[current.length - 1].t_us > maxGapUs) {
        if (current.length > 1) out.push({stream, points: current});
        current = [];
      }
      current.push(f);
    }
    if (current.length > 1) out.push({stream, points: current});
  }
  return out;
}

/** Slippy-map tile indices covering a Web-Mercator view (only used after a recorded tile opt-in). */
export function tilesForView(view, zoomMax = 19) {
  if (view.projection !== 'mercator') return [];
  // planar x/y are in degree units; one tile at zoom z spans 360 / 2^z of them.
  const pxPerDegree = view.scale;
  const z = clamp(Math.round(Math.log2(pxPerDegree * 360 / 256)), 0, zoomMax);
  const n = 2 ** z, unit = 360 / n;
  const halfW = view.width / 2 / view.scale, halfH = view.height / 2 / view.scale;
  const xs = [view.cx - halfW, view.cx + halfW].map(x => x + view.lon0);
  const ys = [view.cy + halfH, view.cy - halfH];
  const tiles = [];
  const x0 = Math.floor((xs[0] + 180) / unit), x1 = Math.floor((xs[1] + 180) / unit);
  const y0 = Math.floor((180 - ys[0]) / unit), y1 = Math.floor((180 - ys[1]) / unit);
  for (let ty = Math.max(0, y0); ty <= Math.min(n - 1, y1); ty++) {
    for (let tx = x0; tx <= x1; tx++) {
      const wrapped = ((tx % n) + n) % n;
      const west = tx * unit - 180, north = 180 - ty * unit;
      const left = view.width / 2 + (west - view.lon0 - view.cx) * view.scale;
      const top = view.height / 2 - (north - view.cy) * view.scale;
      tiles.push({z, x: wrapped, y: ty, left, top, size: unit * view.scale});
      if (tiles.length > 400) return tiles;
    }
  }
  return tiles;
}

/** Fill a recorded tile template; only {z}, {x}, {y} are substituted. */
export function tileUrl(template, z, x, y) {
  return template.replaceAll('{z}', String(z)).replaceAll('{x}', String(x)).replaceAll('{y}', String(y));
}

/** Linear time scale (microseconds -> pixels) with clamping helpers for the lane strip. */
export function timeScale(t0, t1, x0, x1) {
  const span = Math.max(1, t1 - t0);
  const f = t => x0 + (t - t0) / span * (x1 - x0);
  f.invert = x => t0 + (x - x0) / (x1 - x0) * span;
  return f;
}

/** Thin points to at most max by a deterministic stride (keeps first and last). */
export function thin(points, max) {
  if (points.length <= max) return points.slice();
  const stride = Math.ceil(points.length / max);
  const out = points.filter((_, i) => i % stride === 0);
  if (out[out.length - 1] !== points[points.length - 1]) out.push(points[points.length - 1]);
  return out;
}

export function formatDistance(m) {
  if (!Number.isFinite(m)) return '—';
  return m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${Math.round(m)} m`;
}

/** ISO UTC text of microseconds (second precision unless sub-second parts exist). */
export function isoOfUs(us) {
  const d = new Date(Math.floor(us / 1000));
  return d.toISOString().replace('.000Z', 'Z');
}
