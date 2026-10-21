import test from 'node:test';
import assert from 'node:assert/strict';
import {wrapLon, mercatorY, inverseMercatorY, arcCentre, planar, fitView, toPixel, metersToPixels, haversine, polylines,
  tilesForView, tileUrl, timeScale, thin, formatDistance, isoOfUs, METERS_PER_DEGREE, MERCATOR_MAX_LAT}
  from '../../app/static/location-math.mjs';

const close = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) <= eps, `${a} != ${b}`);

test('longitude wrapping keeps the antimeridian continuous', () => {
  assert.equal(wrapLon(0), 0);
  close(wrapLon(359.9), -0.1);
  close(wrapLon(-181), 179);
  assert.equal(wrapLon(180), -180);
  close(wrapLon(-179.9997 - 179.999), 0.0013, 1e-9);
});

test('web mercator is symmetric, invertible and clamped at the projection limit', () => {
  close(mercatorY(0), 0, 1e-12);
  close(mercatorY(45), -mercatorY(-45));
  close(inverseMercatorY(mercatorY(52.25)), 52.25, 1e-9);
  close(mercatorY(MERCATOR_MAX_LAT), 180, 1e-9);
  assert.equal(mercatorY(90), mercatorY(MERCATOR_MAX_LAT));
  assert.ok(Number.isFinite(mercatorY(-90)));
});

test('arc centre picks the short arc across the antimeridian', () => {
  close(arcCentre([179.999, -179.9997]), 179.99965, 1e-9);
  close(arcCentre([10, 20]), 15);
  assert.equal(arcCentre([]), 0);
  close(arcCentre([-170, 170]), -180);
});

test('fitted views put every point inside the canvas, also across the antimeridian and near a pole', () => {
  for (const projection of ['equirectangular', 'mercator']) {
    for (const points of [[{lat: 0.001, lon: 0.002}, {lat: 0.02, lon: 0.03}],
                          [{lat: 0.5, lon: 179.999}, {lat: 0.5, lon: -179.9997}],
                          [{lat: 89.9995, lon: 10}, {lat: 89.999, lon: -170}]]) {
      const view = fitView(points, 900, 560, {projection, padding: 24});
      for (const p of points) {
        const px = toPixel(p.lat, p.lon, view);
        assert.ok(px.x >= 23.999 && px.x <= 876.001 && px.y >= 23.999 && px.y <= 536.001, `${projection} ${JSON.stringify(px)}`);
      }
    }
  }
  const dateline = fitView([{lat: 0.5, lon: 179.999}, {lat: 0.5, lon: -179.9997}], 900, 560);
  assert.ok(dateline.scale > 1000, 'a 100 m cluster is not stretched over the globe');
});

test('north is up and east is right', () => {
  const view = fitView([{lat: 0, lon: 0}, {lat: 0.01, lon: 0.01}], 500, 500);
  const sw = toPixel(0, 0, view), ne = toPixel(0.01, 0.01, view);
  assert.ok(ne.x > sw.x && ne.y < sw.y);
});

test('metres to pixels matches the degree scale and grows with latitude under mercator', () => {
  const eq = fitView([{lat: 0, lon: 0}, {lat: 0.01, lon: 0.01}], 500, 500);
  close(metersToPixels(METERS_PER_DEGREE * 0.001, 0, eq), 0.001 * eq.scale, 1e-9);
  const merc = {...eq, projection: 'mercator'};
  assert.ok(metersToPixels(100, 60, merc) > 1.9 * metersToPixels(100, 0, merc));
  assert.ok(Number.isFinite(metersToPixels(100, 89.99, merc)));
});

test('haversine agrees with the server model (meridian and pole crossing)', () => {
  close(haversine(0, 0, 1, 0), METERS_PER_DEGREE, 1e-6);
  close(haversine(89.9995, 10, 89.9995, -170), 0.001 * METERS_PER_DEGREE, 1e-3);
  close(haversine(0, 179.9995, 0, -179.9997), 0.0008 * METERS_PER_DEGREE, 1e-3);
});

test('polylines split by stream and time gap', () => {
  const fixes = [{stream: 'a', t_us: 0}, {stream: 'a', t_us: 60e6}, {stream: 'a', t_us: 7200e6}, {stream: 'a', t_us: 7260e6},
    {stream: 'b', t_us: 30e6}, {stream: 'a', t_us: 9e9}];
  const lines = polylines(fixes, 900e6);
  assert.deepEqual(lines.map(l => [l.stream, l.points.length]), [['a', 2], ['a', 2]]);
});

test('tiles cover a mercator view, wrap x and fill only z/x/y', () => {
  const view = fitView([{lat: 0.001, lon: 0.002}, {lat: 0.02, lon: 0.03}], 900, 560, {projection: 'mercator'});
  const tiles = tilesForView(view);
  assert.ok(tiles.length >= 1 && tiles.length <= 400);
  const z = tiles[0].z, n = 2 ** z;
  assert.ok(tiles.every(t => t.x >= 0 && t.x < n && t.y >= 0 && t.y < n));
  assert.ok(tiles.some(t => t.left <= 0) && tiles.some(t => t.left + t.size >= 900));
  assert.deepEqual(tilesForView({...view, projection: 'equirectangular'}), []);
  assert.equal(tileUrl('https://t.example.org/{z}/{x}/{y}.png?k={z}', 3, 4, 5), 'https://t.example.org/3/4/5.png?k=3');
  const across = fitView([{lat: 0.5, lon: 179.999}, {lat: 0.5, lon: -179.9997}], 900, 560, {projection: 'mercator'});
  const wrapped = tilesForView(across);
  const nz = 2 ** wrapped[0].z;
  assert.ok(wrapped.some(t => t.x === nz - 1) && wrapped.some(t => t.x === 0), 'both sides of the antimeridian');
});

test('time scale, thinning and formatting', () => {
  const x = timeScale(0, 100, 10, 110);
  assert.equal(x(50), 60);
  assert.equal(x.invert(60), 50);
  const t = timeScale(5, 5, 0, 10);
  assert.ok(Number.isFinite(t(5)));
  const points = Array.from({length: 101}, (_, i) => i);
  const thinned = thin(points, 10);
  assert.ok(thinned.length <= 12 && thinned[0] === 0 && thinned[thinned.length - 1] === 100);
  assert.deepEqual(thin([1, 2], 10), [1, 2]);
  assert.equal(formatDistance(999.4), '999 m');
  assert.equal(formatDistance(1234), '1.23 km');
  assert.equal(formatDistance(NaN), '—');
  assert.equal(isoOfUs(1705302000000000), '2024-01-15T07:00:00Z');
  assert.equal(planar(10, 20, {projection: 'equirectangular', lon0: 20, lat0: 60}).x, 0);
});
