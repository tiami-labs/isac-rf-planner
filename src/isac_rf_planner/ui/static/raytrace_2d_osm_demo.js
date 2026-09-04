const map = L.map('map', { zoomControl: true, attributionControl: true }).setView([37.7749, -122.4194], 16);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 20,
  attribution: '&copy; OpenStreetMap contributors'
}).addTo(map);

const canvas = document.getElementById('rayCanvas');
const ctx = canvas.getContext('2d');
const buildingLayer = L.layerGroup().addTo(map);

const ui = {
  loadOsmBtn: document.getElementById('loadOsmBtn'),
  clearBtn: document.getElementById('clearBtn'),
  resetBtn: document.getElementById('resetBtn'),
  launchBtn: document.getElementById('launchBtn'),
  autoAimBtn: document.getElementById('autoAimBtn'),
  rayCount: document.getElementById('rayCount'),
  rayCountVal: document.getElementById('rayCountVal'),
  spreadDeg: document.getElementById('spreadDeg'),
  spreadDegVal: document.getElementById('spreadDegVal'),
  maxBounces: document.getElementById('maxBounces'),
  maxBouncesVal: document.getElementById('maxBouncesVal'),
  maxRange: document.getElementById('maxRange'),
  maxRangeVal: document.getElementById('maxRangeVal'),
  rxRadius: document.getElementById('rxRadius'),
  rxRadiusVal: document.getElementById('rxRadiusVal'),
  statsBox: document.getElementById('statsBox'),
  angleStat: document.getElementById('angleStat'),
  sceneStats: document.getElementById('sceneStats'),
  placementStat: document.getElementById('placementStat'),
};

const state = {
  sceneOrigin: null,
  buildings: [],
  walls: [],
  tx: null,
  rx: null,
  steerAngle: 0,
  rays: [],
  loading: false,
};

const EPS = 1e-9;
const RAY_EPS = 0.05;
const FRONT_FACE_EPS = 1e-7;

function syncCanvasSize() {
  const size = map.getSize();
  if (canvas.width !== size.x) canvas.width = size.x;
  if (canvas.height !== size.y) canvas.height = size.y;
  canvas.style.width = `${size.x}px`;
  canvas.style.height = `${size.y}px`;
}
map.on('resize zoom move', () => { syncCanvasSize(); draw(); });
setTimeout(() => { syncCanvasSize(); draw(); }, 0);

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
function dot(a, b) { return a.x * b.x + a.y * b.y; }
function cross(a, b) { return a.x * b.y - a.y * b.x; }
function sub(a, b) { return { x: a.x - b.x, y: a.y - b.y }; }
function add(a, b) { return { x: a.x + b.x, y: a.y + b.y }; }
function mul(a, s) { return { x: a.x * s, y: a.y * s }; }
function length(v) { return Math.hypot(v.x, v.y); }
function normalize(v) { const L = Math.hypot(v.x, v.y) || 1; return { x: v.x / L, y: v.y / L }; }
function angleToDir(a) { return { x: Math.cos(a), y: Math.sin(a) }; }
function reflect(dir, normal) {
  const dn = dot(dir, normal);
  return normalize({ x: dir.x - 2 * dn * normal.x, y: dir.y - 2 * dn * normal.y });
}

function enuFromLatLng(origin, ll) {
  const lat0 = origin.lat * Math.PI / 180;
  const dLat = (ll.lat - origin.lat) * Math.PI / 180;
  const dLon = (ll.lng - origin.lng) * Math.PI / 180;
  const R = 6371000.0;
  return { x: dLon * Math.cos(lat0) * R, y: dLat * R };
}

function latLngFromEnu(origin, p) {
  const lat0 = origin.lat * Math.PI / 180;
  const R = 6371000.0;
  const dLat = p.y / R;
  const dLon = p.x / (R * Math.cos(lat0));
  return L.latLng(origin.lat + dLat * 180 / Math.PI, origin.lng + dLon * 180 / Math.PI);
}

function signedArea(poly) {
  let a = 0;
  for (let i = 0; i < poly.length; i++) {
    const p = poly[i];
    const q = poly[(i + 1) % poly.length];
    a += p.x * q.y - q.x * p.y;
  }
  return 0.5 * a;
}

function bboxOfPoints(poly) {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const p of poly) {
    if (p.x < minX) minX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.x > maxX) maxX = p.x;
    if (p.y > maxY) maxY = p.y;
  }
  return { minX, minY, maxX, maxY };
}

function buildSceneFromBuildings(center, buildings) {
  state.sceneOrigin = center;
  state.buildings = [];
  state.walls = [];
  let wallId = 0;

  for (const b of buildings) {
    const geom = Array.isArray(b.geometry) ? b.geometry : [];
    let pts = geom.map(node => L.latLng(Number(node.lat), Number(node.lon))).filter(ll => Number.isFinite(ll.lat) && Number.isFinite(ll.lng));
    if (pts.length < 3) continue;
    if (pts[0].distanceTo(pts[pts.length - 1]) < 0.01) pts = pts.slice(0, -1);
    if (pts.length < 3) continue;
    const enu = pts.map(ll => enuFromLatLng(center, ll));
    const area = signedArea(enu);
    const bbox = bboxOfPoints(enu);
    const building = { id: b.id ?? null, latlngs: pts, enu, bbox, area };
    const idx = state.buildings.length;
    state.buildings.push(building);

    for (let i = 0; i < enu.length; i++) {
      const a = enu[i];
      const bpt = enu[(i + 1) % enu.length];
      const dx = bpt.x - a.x, dy = bpt.y - a.y;
      const L = Math.hypot(dx, dy);
      if (L < 1e-6) continue;
      const tangent = { x: dx / L, y: dy / L };
      const outward = area >= 0 ? { x: tangent.y, y: -tangent.x } : { x: -tangent.y, y: tangent.x };
      state.walls.push({
        id: wallId++,
        a, b: bpt,
        outward,
        buildingIndex: idx,
        len: L,
        bbox: { minX: Math.min(a.x, bpt.x), minY: Math.min(a.y, bpt.y), maxX: Math.max(a.x, bpt.x), maxY: Math.max(a.y, bpt.y) },
      });
    }
  }
}

async function loadOsmForView() {
  if (state.loading) return;
  state.loading = true;
  ui.sceneStats.innerHTML = 'Loading OSM buildings for current view...';
  try {
    const center = map.getCenter();
    const bounds = map.getBounds();
    const radius = Math.max(
      center.distanceTo(bounds.getNorthWest()),
      center.distanceTo(bounds.getNorthEast()),
      center.distanceTo(bounds.getSouthWest()),
      center.distanceTo(bounds.getSouthEast())
    ) + 80.0;
    const url = `/api/osm/buildings-near-point?lat=${encodeURIComponent(center.lat)}&lon=${encodeURIComponent(center.lng)}&radius_m=${encodeURIComponent(Math.ceil(radius))}&max_count=2500`;
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const buildings = Array.isArray(data.buildings) ? data.buildings : [];
    buildSceneFromBuildings(center, buildings);
    renderBuildings();
    state.rays = [];
    updateSceneStats(radius);
    updateStats();
    draw();
  } catch (err) {
    ui.sceneStats.innerHTML = `OSM load failed: <code>${String(err)}</code>`;
  } finally {
    state.loading = false;
  }
}

function renderBuildings() {
  buildingLayer.clearLayers();
  for (const b of state.buildings) {
    L.polygon(b.latlngs, {
      color: '#8f8f8f',
      weight: 1,
      fillColor: '#d2d2d2',
      fillOpacity: 0.35,
      interactive: false,
    }).addTo(buildingLayer);
  }
}

function updateSceneStats(radiusM = null) {
  ui.sceneStats.innerHTML = `Scene origin: <code>${state.sceneOrigin ? `${state.sceneOrigin.lat.toFixed(6)}, ${state.sceneOrigin.lng.toFixed(6)}` : 'unset'}</code><br>` +
    `Buildings: <code>${state.buildings.length}</code> &nbsp; Walls: <code>${state.walls.length}</code>` +
    (radiusM != null ? ` &nbsp; Radius: <code>${Math.round(radiusM)} m</code>` : '');
}

function updatePlacementStat() {
  const tx = state.tx ? `${state.tx.lat.toFixed(6)}, ${state.tx.lng.toFixed(6)}` : 'unset';
  const rx = state.rx ? `${state.rx.lat.toFixed(6)}, ${state.rx.lng.toFixed(6)}` : 'unset';
  ui.placementStat.innerHTML = `Tx: <code>${tx}</code><br>Rx: <code>${rx}</code>`;
}

function pointInPolygon(p, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const xi = poly[i].x, yi = poly[i].y;
    const xj = poly[j].x, yj = poly[j].y;
    const intersect = ((yi > p.y) !== (yj > p.y)) &&
      (p.x < (xj - xi) * (p.y - yi) / ((yj - yi) || 1e-12) + xi);
    if (intersect) inside = !inside;
  }
  return inside;
}

function pointInAnyBuildingEnu(p) {
  for (const b of state.buildings) {
    if (p.x <= b.bbox.minX || p.x >= b.bbox.maxX || p.y <= b.bbox.minY || p.y >= b.bbox.maxY) continue;
    if (pointInPolygon(p, b.enu)) return true;
  }
  return false;
}

function raySegmentIntersection(origin, dir, segA, segB) {
  const s = sub(segB, segA);
  const denom = cross(dir, s);
  if (Math.abs(denom) < EPS) return null;
  const ao = sub(segA, origin);
  const t = cross(ao, s) / denom;
  const u = cross(ao, dir) / denom;
  if (t <= RAY_EPS) return null;
  if (u < -1e-8 || u > 1 + 1e-8) return null;
  return { t, u, point: add(origin, mul(dir, t)) };
}

function rayCircleIntersection(origin, dir, center, radius) {
  if (!center || radius <= 0) return null;
  const oc = sub(origin, center);
  const a = dot(dir, dir);
  const b = 2 * dot(oc, dir);
  const c = dot(oc, oc) - radius * radius;
  const disc = b * b - 4 * a * c;
  if (disc < 0) return null;
  const s = Math.sqrt(disc);
  const t1 = (-b - s) / (2 * a);
  const t2 = (-b + s) / (2 * a);
  const t = t1 > RAY_EPS ? t1 : (t2 > RAY_EPS ? t2 : null);
  if (t == null) return null;
  return { t, point: add(origin, mul(dir, t)) };
}

function getActiveWalls(txEnu, maxRange) {
  const pad = maxRange + 25;
  return state.walls.filter(w => {
    if (w.bbox.maxX < txEnu.x - pad || w.bbox.minX > txEnu.x + pad || w.bbox.maxY < txEnu.y - pad || w.bbox.minY > txEnu.y + pad) return false;
    const mid = { x: 0.5 * (w.a.x + w.b.x), y: 0.5 * (w.a.y + w.b.y) };
    return Math.hypot(mid.x - txEnu.x, mid.y - txEnu.y) <= maxRange + 50;
  });
}

function nearestWallHit(origin, dir, activeWalls, remainingRange) {
  let best = null;
  let tied = 0;
  for (const w of activeWalls) {
    const hit = raySegmentIntersection(origin, dir, w.a, w.b);
    if (!hit || hit.t > remainingRange + 1e-6) continue;
    if (dot(dir, w.outward) >= -FRONT_FACE_EPS) continue;
    const edgeMargin = Math.min(hit.u, 1 - hit.u) * w.len;
    if (edgeMargin < 0.35) continue;
    if (!best || hit.t < best.t - 1e-5) {
      best = { ...hit, wall: w };
      tied = 1;
    } else if (Math.abs(hit.t - best.t) < 1e-5) {
      tied += 1;
    }
  }
  if (!best) return null;
  return { best, tied };
}

function traceOneRay(angle) {
  if (!state.tx || !state.sceneOrigin) return null;
  const txEnu = enuFromLatLng(state.sceneOrigin, state.tx);
  const rxEnu = state.rx ? enuFromLatLng(state.sceneOrigin, state.rx) : null;
  const rxRadius = parseFloat(ui.rxRadius.value);
  const maxBounces = parseInt(ui.maxBounces.value, 10);
  const maxRange = parseFloat(ui.maxRange.value);
  const activeWalls = getActiveWalls(txEnu, maxRange);

  let origin = { ...txEnu };
  let dir = angleToDir(angle);
  const pts = [{ ...txEnu }];
  let total = 0;
  let bounces = 0;
  let status = 'max_range';
  let hitRx = false;

  while (true) {
    const remaining = maxRange - total;
    if (remaining <= 0) {
      status = 'max_range';
      break;
    }

    const wallRes = nearestWallHit(origin, dir, activeWalls, remaining);
    const wallHit = wallRes ? wallRes.best : null;
    const rxHit = rxEnu ? rayCircleIntersection(origin, dir, rxEnu, rxRadius) : null;
    const wallT = wallHit ? wallHit.t : Infinity;
    const rxT = rxHit && rxHit.t <= remaining ? rxHit.t : Infinity;

    if (rxT < wallT) {
      total += rxT;
      pts.push({ ...rxHit.point });
      status = 'hit_rx';
      hitRx = true;
      break;
    }

    if (!wallHit) {
      pts.push(add(origin, mul(dir, remaining)));
      total += remaining;
      status = 'max_range';
      break;
    }

    total += wallHit.t;
    pts.push({ ...wallHit.point });

    if (wallRes.tied > 1) {
      status = 'corner_hit';
      break;
    }
    if (bounces >= maxBounces) {
      status = 'max_bounces';
      break;
    }

    dir = reflect(dir, wallHit.wall.outward);
    origin = add(wallHit.point, mul(dir, RAY_EPS));
    bounces += 1;
  }

  return { pts, bounceCount: bounces, hitRx, status, angle, pathLengthM: total };
}

function launchRays() {
  state.rays = [];
  if (!state.sceneOrigin || !state.tx) {
    ui.statsBox.innerHTML = 'Load OSM and place <code>Tx</code> first.';
    draw();
    return;
  }
  const n = parseInt(ui.rayCount.value, 10);
  const spread = parseFloat(ui.spreadDeg.value) * Math.PI / 180;
  const base = state.steerAngle;
  for (let i = 0; i < n; i++) {
    let a = base;
    if (n > 1) {
      const t = i / (n - 1);
      a = base - spread / 2 + t * spread;
    }
    state.rays.push(traceOneRay(a));
  }
  updateStats();
  draw();
}

function updateStats() {
  if (!state.rays.length) {
    ui.statsBox.innerHTML = 'No trace yet.';
    return;
  }
  const hits = state.rays.filter(r => r && r.hitRx);
  const minB = hits.length ? Math.min(...hits.map(r => r.bounceCount)) : '–';
  const maxB = hits.length ? Math.max(...hits.map(r => r.bounceCount)) : '–';
  const corner = state.rays.filter(r => r && r.status === 'corner_hit').length;
  const maxed = state.rays.filter(r => r && r.status === 'max_bounces').length;
  const ranged = state.rays.filter(r => r && r.status === 'max_range').length;
  ui.statsBox.innerHTML =
    `Rays launched: <code>${state.rays.length}</code><br>` +
    `Rays reaching Rx: <code>${hits.length}</code><br>` +
    `Min / max bounces among hits: <code>${minB}</code> / <code>${maxB}</code><br>` +
    `Stopped by range: <code>${ranged}</code> &nbsp;|&nbsp; Corner hits: <code>${corner}</code> &nbsp;|&nbsp; Stopped at max-bounces: <code>${maxed}</code>`;
}

function updateAngleLabel() {
  const deg = (state.steerAngle * 180 / Math.PI + 360) % 360;
  ui.angleStat.innerHTML = `Steer angle: <code>${deg.toFixed(1)}°</code>`;
}

function autoAim() {
  if (!state.tx || !state.rx || !state.sceneOrigin) return;
  const tx = enuFromLatLng(state.sceneOrigin, state.tx);
  const rx = enuFromLatLng(state.sceneOrigin, state.rx);
  state.steerAngle = Math.atan2(rx.y - tx.y, rx.x - tx.x);
  updateAngleLabel();
  draw();
}

function markerLatLng(pEnu) { return latLngFromEnu(state.sceneOrigin, pEnu); }

function drawTriangle(ll, color, up = true, size = 12) {
  if (!ll) return;
  const p = map.latLngToContainerPoint(ll);
  ctx.fillStyle = color;
  ctx.beginPath();
  if (up) {
    ctx.moveTo(p.x, p.y - size);
    ctx.lineTo(p.x - size * 0.9, p.y + size * 0.8);
    ctx.lineTo(p.x + size * 0.9, p.y + size * 0.8);
  } else {
    ctx.moveTo(p.x, p.y + size);
    ctx.lineTo(p.x - size * 0.9, p.y - size * 0.8);
    ctx.lineTo(p.x + size * 0.9, p.y - size * 0.8);
  }
  ctx.closePath();
  ctx.fill();
  ctx.strokeStyle = 'rgba(0,0,0,0.25)';
  ctx.stroke();
}

function drawArrow(fromLl, angle, lenPx = 85, color = '#ff5566') {
  if (!fromLl) return;
  const from = map.latLngToContainerPoint(fromLl);
  const to = { x: from.x + Math.cos(angle) * lenPx, y: from.y + Math.sin(angle) * lenPx };
  ctx.save();
  ctx.strokeStyle = color;
  ctx.fillStyle = color;
  ctx.lineWidth = 2.2;
  ctx.beginPath();
  ctx.moveTo(from.x, from.y);
  ctx.lineTo(to.x, to.y);
  ctx.stroke();
  const ah = 10;
  const a1 = angle + Math.PI * 0.85;
  const a2 = angle - Math.PI * 0.85;
  ctx.beginPath();
  ctx.moveTo(to.x, to.y);
  ctx.lineTo(to.x + Math.cos(a1) * ah, to.y + Math.sin(a1) * ah);
  ctx.lineTo(to.x + Math.cos(a2) * ah, to.y + Math.sin(a2) * ah);
  ctx.closePath();
  ctx.fill();
  ctx.restore();
}

function drawBeamPreview() {
  if (!state.tx) return;
  const spread = parseFloat(ui.spreadDeg.value) * Math.PI / 180;
  const r = 120;
  const c = map.latLngToContainerPoint(state.tx);
  ctx.save();
  ctx.fillStyle = 'rgba(255, 120, 60, 0.08)';
  ctx.strokeStyle = 'rgba(255, 120, 60, 0.55)';
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(c.x, c.y);
  ctx.arc(c.x, c.y, r, state.steerAngle - spread / 2, state.steerAngle + spread / 2);
  ctx.closePath();
  ctx.fill();
  ctx.stroke();
  ctx.restore();
}

function draw() {
  syncCanvasSize();
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  drawBeamPreview();

  for (const ray of state.rays) {
    if (!ray) continue;
    ctx.save();
    ctx.lineWidth = ray.hitRx ? 2.4 : 1.4;
    ctx.strokeStyle = ray.hitRx ? '#2bb24c' : '#ff8c42';
    if (!ray.hitRx) ctx.setLineDash([6, 4]);
    ctx.beginPath();
    const p0 = map.latLngToContainerPoint(markerLatLng(ray.pts[0]));
    ctx.moveTo(p0.x, p0.y);
    for (let i = 1; i < ray.pts.length; i++) {
      const pi = map.latLngToContainerPoint(markerLatLng(ray.pts[i]));
      ctx.lineTo(pi.x, pi.y);
    }
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = ray.hitRx ? '#2bb24c' : '#ff8c42';
    for (let i = 1; i < ray.pts.length - (ray.hitRx ? 1 : 0); i++) {
      const pi = map.latLngToContainerPoint(markerLatLng(ray.pts[i]));
      ctx.beginPath();
      ctx.arc(pi.x, pi.y, 3, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();
  }

  if (state.rx) {
    const rr = parseFloat(ui.rxRadius.value);
    const p = map.latLngToContainerPoint(state.rx);
    const edge = map.latLngToContainerPoint(L.latLng(state.rx.lat, state.rx.lng + (rr / (6371000.0 * Math.cos(state.rx.lat * Math.PI / 180))) * 180 / Math.PI));
    const radiusPx = Math.max(4, Math.abs(edge.x - p.x));
    ctx.save();
    ctx.strokeStyle = 'rgba(21, 101, 192, 0.35)';
    ctx.fillStyle = 'rgba(21, 101, 192, 0.08)';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(p.x, p.y, radiusPx, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    ctx.restore();
  }

  drawArrow(state.tx, state.steerAngle, 85, '#ff5566');
  drawTriangle(state.tx, '#b50000', true, 12);
  drawTriangle(state.rx, '#0a41bf', false, 12);
}

function trySetPoint(latlng, isRx) {
  if (!state.sceneOrigin) return;
  const p = enuFromLatLng(state.sceneOrigin, latlng);
  if (pointInAnyBuildingEnu(p)) return;
  if (isRx) state.rx = latlng; else state.tx = latlng;
  if (state.tx && state.rx) autoAim();
  updatePlacementStat();
  state.rays = [];
  updateStats();
  draw();
}

map.on('click', (evt) => {
  if (evt.originalEvent && evt.originalEvent.shiftKey) trySetPoint(evt.latlng, true);
  else trySetPoint(evt.latlng, false);
});

map.on('mousemove', (evt) => {
  if (!state.tx || !state.sceneOrigin) return;
  const tx = enuFromLatLng(state.sceneOrigin, state.tx);
  const p = enuFromLatLng(state.sceneOrigin, evt.latlng);
  state.steerAngle = Math.atan2(p.y - tx.y, p.x - tx.x);
  updateAngleLabel();
  draw();
});

ui.loadOsmBtn.addEventListener('click', loadOsmForView);
ui.clearBtn.addEventListener('click', () => { state.rays = []; updateStats(); draw(); });
ui.resetBtn.addEventListener('click', () => { state.tx = null; state.rx = null; state.rays = []; updatePlacementStat(); updateStats(); draw(); });
ui.launchBtn.addEventListener('click', launchRays);
ui.autoAimBtn.addEventListener('click', autoAim);

['rayCount', 'spreadDeg', 'maxBounces', 'maxRange', 'rxRadius'].forEach((id) => {
  ui[id].addEventListener('input', () => {
    ui[`${id}Val`].textContent = ui[id].value;
    if (id === 'spreadDeg' || id === 'rxRadius') draw();
  });
});

ui.rayCountVal.textContent = ui.rayCount.value;
ui.spreadDegVal.textContent = ui.spreadDeg.value;
ui.maxBouncesVal.textContent = ui.maxBounces.value;
ui.maxRangeVal.textContent = ui.maxRange.value;
ui.rxRadiusVal.textContent = ui.rxRadius.value;
updateAngleLabel();
updatePlacementStat();
updateStats();
loadOsmForView();
