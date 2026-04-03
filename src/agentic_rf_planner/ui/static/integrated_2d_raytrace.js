(function () {
  if (typeof map === 'undefined' || typeof L === 'undefined') return;

  const FRONT_FACE_EPS = 1e-7;
  const EPS = 1e-9;
  const RAY_EPS = 0.05;
  const OSM_PADDING_M = 130.0;
  const OSM_MIN_RADIUS_M = 450.0;
  /** TX must stay inside the OSM disk (sceneCenter, sceneFetchRadiusM); small relocations do not refetch. */
  const SCENE_TX_COVERAGE_MARGIN_M = 110.0;
  /** Persist scene in the browser so reload / new tab can skip OSM HTTP when coverage still matches (like ref demo: one load, many launches). */
  const RT_SCENE_STORAGE_KEY = 'rf2d_osm_scene_v1';
  const RT_SCENE_MAX_AGE_MS = 24 * 60 * 60 * 1000;

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
  function dot(a, b) { return a.x * b.x + a.y * b.y; }
  function cross(a, b) { return a.x * b.y - a.y * b.x; }
  function sub(a, b) { return { x: a.x - b.x, y: a.y - b.y }; }
  function add(a, b) { return { x: a.x + b.x, y: a.y + b.y }; }
  function mul(a, s) { return { x: a.x * s, y: a.y * s }; }
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
    for (const b of rtState.buildings) {
      if (p.x <= b.bbox.minX || p.x >= b.bbox.maxX || p.y <= b.bbox.minY || p.y >= b.bbox.maxY) continue;
      if (pointInPolygon(p, b.enu)) return true;
    }
    return false;
  }
  function openSegmentCrossesInterior(a, b) {
    const d = sub(b, a);
    const L = Math.hypot(d.x, d.y);
    if (L < 1e-9) return false;
    const steps = Math.max(2, Math.ceil(L / 2.0));
    for (let i = 1; i < steps; i++) {
      const t = i / steps;
      const p = { x: a.x + d.x * t, y: a.y + d.y * t };
      if (pointInAnyBuildingEnu(p)) return true;
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

  const sidebar = document.getElementById('sidebar');
  const mapContainer = document.getElementById('map-container');
  if (!sidebar || !mapContainer) return;

  if (!document.getElementById('rt-marker-styles')) {
    const style = document.createElement('style');
    style.id = 'rt-marker-styles';
    style.textContent = `
      .rt-tx-icon { background: transparent; border: none; }
      .rt-tx-wrap { position: relative; width: 80px; height: 80px; transform-origin: 40px 40px; }
      .rt-tx-wrap svg { overflow: visible; }
      .rt-rx-icon { background: transparent; border: none; }
      .rt-rx-dot { width: 12px; height: 12px; border-radius: 999px; background: #0a41bf; border: 2px solid #ffffff; box-shadow: 0 0 0 1px rgba(10,65,191,0.45); }
    `;
    document.head.appendChild(style);
  }

  const panel = document.createElement('div');
  panel.style.marginTop = '20px';
  panel.style.paddingTop = '20px';
  panel.style.borderTop = '1px solid #444';
  panel.innerHTML = `
    <h3 style="font-size:14px; margin-bottom:10px;">2D Ray Tracer</h3>
    <div style="font-size:11px; color:#bbb; margin-bottom:8px;">Normal click: place TX, move mouse to steer, second click anywhere confirms direction. Shift+click places RX.</div>
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; align-items:center; margin-bottom:8px;">
      <label style="font-size:11px; color:#ccc;">Ray count <span id="rt-ray-count-val">121</span></label>
      <input id="rt-ray-count" type="range" min="1" max="721" step="2" value="121" />
      <label style="font-size:11px; color:#ccc;">Spread <span id="rt-spread-val">50</span>°</label>
      <input id="rt-spread" type="range" min="0" max="180" value="50" />
      <label style="font-size:11px; color:#ccc;">Max bounces <span id="rt-bounces-val">4</span></label>
      <input id="rt-bounces" type="range" min="0" max="12" value="4" />
      <label style="font-size:11px; color:#ccc;">Max range <span id="rt-range-val">500</span> m</label>
      <input id="rt-range" type="range" min="50" max="2000" step="10" value="500" />
      <label style="font-size:11px; color:#ccc;">RX radius <span id="rt-rx-val">8</span> m</label>
      <input id="rt-rx" type="range" min="2" max="30" step="1" value="8" />
    </div>
    <div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:8px;">
      <button id="rt-launch-btn" type="button" style="padding:6px 8px; background:#0a7a2f; color:#fff; border:none; border-radius:3px; cursor:pointer; font-size:11px;">Launch rays</button>
      <button id="rt-autoaim-btn" type="button" style="padding:6px 8px; background:#444; color:#fff; border:none; border-radius:3px; cursor:pointer; font-size:11px;">Auto-aim RX</button>
      <button id="rt-preview-btn" type="button" style="padding:6px 8px; background:#444; color:#fff; border:none; border-radius:3px; cursor:pointer; font-size:11px;">Load OSM preview</button>
      <button id="rt-clear-btn" type="button" style="padding:6px 8px; background:#663; color:#fff; border:none; border-radius:3px; cursor:pointer; font-size:11px;">Clear rays</button>
    </div>
    <div id="rt-angle" style="font-size:11px; color:#bbb; margin-bottom:6px;">Steer: <code>0.0°</code></div>
    <div id="rt-scene" style="font-size:11px; color:#bbb; margin-bottom:6px;">Scene: <code>not loaded</code></div>
    <div id="rt-stats" style="font-size:11px; color:#bbb;">No trace yet.</div>
  `;
  sidebar.appendChild(panel);

  const overlay = document.createElement('canvas');
  overlay.id = 'rt-overlay-canvas';
  overlay.style.position = 'absolute';
  overlay.style.left = '0';
  overlay.style.top = '0';
  overlay.style.width = '100%';
  overlay.style.height = '100%';
  overlay.style.pointerEvents = 'none';
  overlay.style.zIndex = '450';
  mapContainer.appendChild(overlay);
  const ctx = overlay.getContext('2d');

  const rtLayerGroup = L.layerGroup().addTo(map);
  const rtPreviewLayer = L.layerGroup().addTo(map);

  const ui = {
    rayCount: document.getElementById('rt-ray-count'),
    rayCountVal: document.getElementById('rt-ray-count-val'),
    spread: document.getElementById('rt-spread'),
    spreadVal: document.getElementById('rt-spread-val'),
    bounces: document.getElementById('rt-bounces'),
    bouncesVal: document.getElementById('rt-bounces-val'),
    range: document.getElementById('rt-range'),
    rangeVal: document.getElementById('rt-range-val'),
    rx: document.getElementById('rt-rx'),
    rxVal: document.getElementById('rt-rx-val'),
    launch: document.getElementById('rt-launch-btn'),
    autoAim: document.getElementById('rt-autoaim-btn'),
    preview: document.getElementById('rt-preview-btn'),
    clear: document.getElementById('rt-clear-btn'),
    angle: document.getElementById('rt-angle'),
    scene: document.getElementById('rt-scene'),
    stats: document.getElementById('rt-stats'),
  };

  const rtState = {
    sceneOrigin: null,
    sceneCenter: null,
    sceneZoom: null,
    sceneFetchRadiusM: null,
    buildings: [],
    walls: [],
    previewVisible: false,
    tx: null,
    txPending: false,
    steerAngle: 0,
    steerConfirmed: false,
    rx: null,
    rays: [],
    loading: false,
    txArrowMarker: null,
    rxCircle: null,
    rxMarker: null,
  };

  function syncOverlaySize() {
    const size = map.getSize();
    if (overlay.width !== size.x) overlay.width = size.x;
    if (overlay.height !== size.y) overlay.height = size.y;
  }
  function angleDeg() { return ((rtState.steerAngle * 180 / Math.PI) + 360) % 360; }
  function updateUiLabels() {
    ui.rayCountVal.textContent = ui.rayCount.value;
    ui.spreadVal.textContent = ui.spread.value;
    ui.bouncesVal.textContent = ui.bounces.value;
    ui.rangeVal.textContent = ui.range.value;
    ui.rxVal.textContent = ui.rx.value;
    ui.angle.innerHTML = `Steer: <code>${angleDeg().toFixed(1)}°</code>${rtState.txPending && !rtState.steerConfirmed ? ' <span style="color:#ffb86b;">(click again to confirm)</span>' : ''}`;
    ui.scene.innerHTML = `Scene: <code>${rtState.sceneOrigin ? `${rtState.buildings.length} buildings / ${rtState.walls.length} walls` : 'not loaded'}</code>${rtState.previewVisible ? ' <span style="color:#7ecbff;">preview on</span>' : ''}`;
    if (rtState.rxCircle) rtState.rxCircle.setRadius(Number(ui.rx.value));
  }
  function setStatus(msg) {
    if (typeof window.setPlannerStatus === 'function') window.setPlannerStatus(msg);
    else {
      const el = document.getElementById('status');
      if (el) el.textContent = msg;
    }
  }

  function buildSceneFromBuildings(center, buildings) {
    rtState.sceneOrigin = center;
    rtState.sceneCenter = center;
    rtState.sceneZoom = map.getZoom();
    rtState.buildings = [];
    rtState.walls = [];
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
      const idx = rtState.buildings.length;
      rtState.buildings.push(building);
      for (let i = 0; i < enu.length; i++) {
        const a = enu[i];
        const bpt = enu[(i + 1) % enu.length];
        const dx = bpt.x - a.x, dy = bpt.y - a.y;
        const Lseg = Math.hypot(dx, dy);
        if (Lseg < 1e-6) continue;
        const tangent = { x: dx / Lseg, y: dy / Lseg };
        const outward = area >= 0 ? { x: tangent.y, y: -tangent.x } : { x: -tangent.y, y: tangent.x };
        rtState.walls.push({ id: wallId++, a, b: bpt, outward, buildingIndex: idx, len: Lseg, bbox: { minX: Math.min(a.x, bpt.x), minY: Math.min(a.y, bpt.y), maxX: Math.max(a.x, bpt.x), maxY: Math.max(a.y, bpt.y) } });
      }
    }
    updateUiLabels();
  }

  /** OSM query radius from trace geometry only (TX, RX, max path length). Map pan/zoom does not affect this. */
  function requiredOsmRadiusM() {
    const maxRange = Number(ui.range.value);
    let r = maxRange + OSM_PADDING_M;
    if (rtState.tx && rtState.rx) {
      const d = rtState.tx.distanceTo(rtState.rx) + Number(ui.rx.value);
      r = Math.max(r, d + OSM_PADDING_M);
    }
    return Math.max(OSM_MIN_RADIUS_M, r);
  }

  function previewOsmRadiusM(mapCenter) {
    const b = map.getBounds();
    return Math.max(
      OSM_MIN_RADIUS_M,
      Math.max(
        mapCenter.distanceTo(b.getNorthWest()),
        mapCenter.distanceTo(b.getNorthEast()),
        mapCenter.distanceTo(b.getSouthWest()),
        mapCenter.distanceTo(b.getSouthEast())
      ) + 60.0
    );
  }

  function invalidateRayScene() {
    rtState.sceneOrigin = null;
    rtState.sceneCenter = null;
    rtState.sceneZoom = null;
    rtState.sceneFetchRadiusM = null;
    rtState.buildings = [];
    rtState.walls = [];
    rtState.previewVisible = false;
    rtPreviewLayer.clearLayers();
    try { localStorage.removeItem(RT_SCENE_STORAGE_KEY); } catch (_) {}
    updateUiLabels();
  }

  function saveRaySceneToStorage(center, radiusM, buildings) {
    try {
      const pack = {
        v: 1,
        lat: center.lat,
        lng: center.lng,
        radiusM,
        buildings: Array.isArray(buildings) ? buildings : [],
        t: Date.now(),
      };
      localStorage.setItem(RT_SCENE_STORAGE_KEY, JSON.stringify(pack));
    } catch (_) {
      /* quota / private mode */
    }
  }

  /**
   * Restore OSM scene from localStorage if anchor (TX when confirmed, else map center) lies in the
   * saved disk and saved radius still covers current required fetch radius. No network.
   */
  function tryRestoreRaySceneFromStorage() {
    try {
      const raw = localStorage.getItem(RT_SCENE_STORAGE_KEY);
      if (!raw) return false;
      const pack = JSON.parse(raw);
      if (pack.v !== 1 || !Number.isFinite(pack.lat) || !Number.isFinite(pack.lng) || !Number.isFinite(pack.radiusM)) return false;
      if (typeof pack.t === 'number' && Date.now() - pack.t > RT_SCENE_MAX_AGE_MS) return false;

      const center = L.latLng(pack.lat, pack.lng);
      const R = pack.radiusM;
      const innerR = Math.max(0, R - SCENE_TX_COVERAGE_MARGIN_M);

      const anchor = (rtState.tx && rtState.steerConfirmed)
        ? L.latLng(rtState.tx.lat, rtState.tx.lng)
        : map.getCenter();
      if (center.distanceTo(anchor) > innerR) return false;

      const needR = (rtState.tx && rtState.steerConfirmed) ? requiredOsmRadiusM() : previewOsmRadiusM(map.getCenter());
      if (R + 1 < needR) return false;

      buildSceneFromBuildings(center, Array.isArray(pack.buildings) ? pack.buildings : []);
      rtState.sceneFetchRadiusM = R;
      updateUiLabels();
      return true;
    } catch (_) {
      return false;
    }
  }

  async function fetchScene(forcePreviewRender) {
    if (rtState.loading) return;
    rtState.loading = true;
    ui.scene.innerHTML = 'Scene: <code>loading...</code>';
    try {
      const useTx = rtState.tx && rtState.steerConfirmed;
      const center = useTx ? L.latLng(rtState.tx.lat, rtState.tx.lng) : map.getCenter();
      const radius = useTx ? requiredOsmRadiusM() : previewOsmRadiusM(center);
      const url = `/api/osm/buildings-near-point?lat=${encodeURIComponent(center.lat)}&lon=${encodeURIComponent(center.lng)}&radius_m=${encodeURIComponent(Math.ceil(radius))}&max_count=2500`;
      const resp = await fetch(url);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      const blist = Array.isArray(data.buildings) ? data.buildings : [];
      buildSceneFromBuildings(center, blist);
      rtState.sceneFetchRadiusM = radius;
      saveRaySceneToStorage(center, radius, blist);
      if (forcePreviewRender || rtState.previewVisible) renderPreview();
      updateUiLabels();
    } finally {
      rtState.loading = false;
    }
  }

  function sceneIsFreshEnough() {
    if (!rtState.sceneOrigin || !rtState.sceneCenter || rtState.sceneFetchRadiusM == null) return false;
    if (!rtState.tx || !rtState.steerConfirmed) return true;
    const txll = L.latLng(rtState.tx.lat, rtState.tx.lng);
    const innerR = Math.max(0, rtState.sceneFetchRadiusM - SCENE_TX_COVERAGE_MARGIN_M);
    if (rtState.sceneCenter.distanceTo(txll) > innerR) return false;
    if (rtState.sceneFetchRadiusM + 1 < requiredOsmRadiusM()) return false;
    return true;
  }
  async function ensureSceneLoaded(forcePreviewRender = false) {
    if (sceneIsFreshEnough()) {
      if (forcePreviewRender) { rtState.previewVisible = true; renderPreview(); }
      return;
    }
    rtState.previewVisible = rtState.previewVisible && forcePreviewRender;
    if (tryRestoreRaySceneFromStorage()) {
      if (forcePreviewRender || rtState.previewVisible) {
        rtState.previewVisible = true;
        renderPreview();
      }
      updateUiLabels();
      return;
    }
    await fetchScene(forcePreviewRender);
  }

  function renderPreview() {
    rtPreviewLayer.clearLayers();
    if (!rtState.previewVisible) return;
    for (const b of rtState.buildings) {
      L.polygon(b.latlngs, { color: '#8f8f8f', weight: 1, fill: false, opacity: 0.9, interactive: false }).addTo(rtPreviewLayer);
    }
  }

  function updateTxVisual() {
    if (window.txMarker) {
      const grp = window.RFPLANNER_DEBUG?.currentLayerGroup || map;
      try { grp.removeLayer(window.txMarker); } catch (_) {}
      window.txMarker = null;
    }
    if (!rtState.tx) return;
    const grp = window.RFPLANNER_DEBUG?.currentLayerGroup || map;
    const steerDeg = (steerScreenAngleRad() * 180 / Math.PI + 360) % 360;
    const pendingNote = rtState.txPending && !rtState.steerConfirmed ? ' pending' : '';
    const html = `
      <div class="rt-tx-wrap" style="transform: rotate(${steerDeg}deg);">
        <svg width="80" height="80" viewBox="0 0 80 80" aria-hidden="true">
          <polygon points="40,40 24,52 24,28" fill="#d32f2f" stroke="#7a0a0a" stroke-width="1.4" stroke-linejoin="round"></polygon>
          <polygon points="40,22 36,34 44,34" fill="#b71c1c" stroke="#5c0a0a" stroke-width="1"></polygon>
        </svg>
      </div>`;
    window.txMarker = L.marker([rtState.tx.lat, rtState.tx.lng], {
      interactive: false,
      zIndexOffset: 1000,
      icon: L.divIcon({ className: 'rt-tx-icon', html, iconSize: [80, 80], iconAnchor: [40, 40] })
    }).addTo(grp).bindPopup(`TX${pendingNote}`);
    currentTxLocation = { lat: rtState.tx.lat, lon: rtState.tx.lng };
    if (typeof window.replaceSingleTxInputPoint === 'function') {
      window.replaceSingleTxInputPoint(rtState.tx.lat, rtState.tx.lng);
    } else {
      const txInput = document.getElementById('tx-input');
      if (txInput) txInput.value = `${rtState.tx.lat},${rtState.tx.lng}`;
    }
  }
  function updateRxVisual() {
    if (rtState.rxCircle) { map.removeLayer(rtState.rxCircle); rtState.rxCircle = null; }
    if (rtState.rxMarker) { map.removeLayer(rtState.rxMarker); rtState.rxMarker = null; }
    if (!rtState.rx) return;
    rtState.rxCircle = L.circle([rtState.rx.lat, rtState.rx.lng], { radius: Number(ui.rx.value), color: '#1565c0', weight: 2, fillColor: '#1565c0', fillOpacity: 0.08 }).addTo(rtLayerGroup);
    rtState.rxMarker = L.marker([rtState.rx.lat, rtState.rx.lng], {
      interactive: false,
      zIndexOffset: 950,
      icon: L.divIcon({ className: 'rt-rx-icon', html: '<div class="rt-rx-dot"></div>', iconSize: [16, 16], iconAnchor: [8, 8] })
    }).addTo(rtLayerGroup);
  }

  function getActiveWalls(txEnu, maxRange) {
    const pad = maxRange + 25;
    return rtState.walls.filter(w => {
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
      if (openSegmentCrossesInterior(origin, hit.point)) continue;
      if (!best || hit.t < best.t - 1e-5) { best = { ...hit, wall: w }; tied = 1; }
      else if (Math.abs(hit.t - best.t) < 1e-5) tied += 1;
    }
    if (!best) return null;
    return { best, tied };
  }
  function traceOneRay(angle) {
    if (!rtState.tx || !rtState.sceneOrigin) return null;
    const txEnu = enuFromLatLng(rtState.sceneOrigin, rtState.tx);
    const rxEnu = rtState.rx ? enuFromLatLng(rtState.sceneOrigin, rtState.rx) : null;
    const rxRadius = Number(ui.rx.value);
    const maxBounces = Number(ui.bounces.value);
    const maxRange = Number(ui.range.value);
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
      if (remaining <= 0) { status = 'max_range'; break; }
      const wallRes = nearestWallHit(origin, dir, activeWalls, remaining);
      const wallHit = wallRes ? wallRes.best : null;
      const rxHit = rxEnu ? rayCircleIntersection(origin, dir, rxEnu, rxRadius) : null;
      const wallT = wallHit ? wallHit.t : Infinity;
      const rxT = rxHit && rxHit.t <= remaining ? rxHit.t : Infinity;
      if (rxT < wallT) {
        if (openSegmentCrossesInterior(origin, rxHit.point)) { status = 'blocked_rx'; break; }
        total += rxT; pts.push({ ...rxHit.point }); status = 'hit_rx'; hitRx = true; break;
      }
      if (!wallHit) { pts.push(add(origin, mul(dir, remaining))); total += remaining; status = 'max_range'; break; }
      total += wallHit.t; pts.push({ ...wallHit.point });
      if (wallRes.tied > 1) { status = 'corner_hit'; break; }
      if (bounces >= maxBounces) { status = 'max_bounces'; break; }
      dir = reflect(dir, wallHit.wall.outward);
      origin = add(wallHit.point, mul(dir, RAY_EPS));
      if (pointInAnyBuildingEnu(origin)) { status = 'interior_reject'; break; }
      bounces += 1;
    }
    const ptsLatLng = pts.map((pt) => latLngFromEnu(rtState.sceneOrigin, pt));
    return { pts, ptsLatLng, bounceCount: bounces, hitRx, status, angle, pathLengthM: total };
  }

  async function launchRays() {
    if (!rtState.tx || !rtState.steerConfirmed) {
      setStatus('Place TX with first click, move mouse, then click again to confirm direction.');
      return;
    }
    await ensureSceneLoaded(false);
    rtState.rays = [];
    const n = Number(ui.rayCount.value);
    const spread = Number(ui.spread.value) * Math.PI / 180;
    const base = rtState.steerAngle;
    for (let i = 0; i < n; i++) {
      let a = base;
      if (n > 1) {
        const t = i / (n - 1);
        a = base - spread / 2 + t * spread;
      }
      rtState.rays.push(traceOneRay(a));
    }
    updateStats();
    draw();
  }

  function updateStats() {
    if (!rtState.rays.length) { ui.stats.innerHTML = 'No trace yet.'; return; }
    const hits = rtState.rays.filter(r => r && r.hitRx);
    const minB = hits.length ? Math.min(...hits.map(r => r.bounceCount)) : '–';
    const maxB = hits.length ? Math.max(...hits.map(r => r.bounceCount)) : '–';
    const corner = rtState.rays.filter(r => r && r.status === 'corner_hit').length;
    const maxed = rtState.rays.filter(r => r && r.status === 'max_bounces').length;
    const ranged = rtState.rays.filter(r => r && r.status === 'max_range').length;
    ui.stats.innerHTML = `Rays: <code>${rtState.rays.length}</code> &nbsp; Hits: <code>${hits.length}</code><br>Hit bounce min/max: <code>${minB}</code> / <code>${maxB}</code><br>Max-range: <code>${ranged}</code> &nbsp; Corner rejects: <code>${corner}</code> &nbsp; Max-bounce stops: <code>${maxed}</code>`;
  }

  function clearRays() { rtState.rays = []; updateStats(); draw(); }
  function clearAllRt() {
    clearRays();
    rtPreviewLayer.clearLayers();
    rtLayerGroup.clearLayers();
    rtState.previewVisible = false;
    rtState.tx = null; rtState.rx = null; rtState.txPending = false; rtState.steerConfirmed = false;
    invalidateRayScene();
    updateTxVisual();
    updateRxVisual();
    updateUiLabels(); draw();
  }

  /** Canvas angle (radians) for current geographic steer — matches map projection; pan/zoom invariant for bearing. */
  function steerScreenAngleRad() {
    if (!rtState.tx) return 0;
    const m = 90.0;
    const tip = latLngFromEnu(rtState.tx, { x: Math.cos(rtState.steerAngle) * m, y: Math.sin(rtState.steerAngle) * m });
    const a = map.latLngToContainerPoint(rtState.tx);
    const b = map.latLngToContainerPoint(tip);
    return Math.atan2(b.y - a.y, b.x - a.x);
  }

  function drawBeamPreview() {
    if (!rtState.tx) return;
    const txp = map.latLngToContainerPoint(rtState.tx);
    const spread = Number(ui.spread.value) * Math.PI / 180;
    const r = 118;
    const sa = steerScreenAngleRad();
    ctx.save();
    ctx.fillStyle = 'rgba(255, 94, 0, 0.14)';
    ctx.strokeStyle = 'rgba(230, 81, 0, 0.72)';
    ctx.lineWidth = 1.75;
    ctx.beginPath();
    ctx.moveTo(txp.x, txp.y);
    ctx.arc(txp.x, txp.y, r, sa - spread / 2, sa + spread / 2);
    ctx.closePath();
    ctx.fill(); ctx.stroke(); ctx.restore();
  }
  function drawArrow() {
    if (!rtState.tx) return;
    const from = map.latLngToContainerPoint(rtState.tx);
    const len = 92;
    const sa = steerScreenAngleRad();
    const to = { x: from.x + Math.cos(sa) * len, y: from.y + Math.sin(sa) * len };
    ctx.save();
    ctx.strokeStyle = '#e53935'; ctx.fillStyle = '#e53935'; ctx.lineWidth = 2.85;
    ctx.beginPath(); ctx.moveTo(from.x, from.y); ctx.lineTo(to.x, to.y); ctx.stroke();
    const ah = 10, a1 = sa + Math.PI * 0.85, a2 = sa - Math.PI * 0.85;
    ctx.beginPath(); ctx.moveTo(to.x, to.y); ctx.lineTo(to.x + Math.cos(a1) * ah, to.y + Math.sin(a1) * ah); ctx.lineTo(to.x + Math.cos(a2) * ah, to.y + Math.sin(a2) * ah); ctx.closePath(); ctx.fill(); ctx.restore();
  }
  function drawRays() {
    for (const ray of rtState.rays) {
      if (!ray || !ray.pts.length) continue;
      const latlngPath = ray.ptsLatLng || (rtState.sceneOrigin ? ray.pts.map((pt) => latLngFromEnu(rtState.sceneOrigin, pt)) : null);
      if (!latlngPath || !latlngPath.length) continue;
      ctx.save();
      ctx.lineWidth = ray.hitRx ? 2.2 : 1.2;
      ctx.strokeStyle = ray.hitRx ? '#2bb24c' : 'rgba(255,140,66,0.65)';
      if (!ray.hitRx) ctx.setLineDash([6, 4]);
      ctx.beginPath();
      latlngPath.forEach((ll, idx) => {
        const cp = map.latLngToContainerPoint(ll);
        if (idx === 0) ctx.moveTo(cp.x, cp.y); else ctx.lineTo(cp.x, cp.y);
      });
      ctx.stroke();
      ctx.restore();
    }
  }
  function draw() {
    syncOverlaySize();
    ctx.clearRect(0, 0, overlay.width, overlay.height);
    drawRays();
    drawBeamPreview();
    drawArrow();
  }

  function setSteerToLatLng(target) {
    if (!rtState.tx || !target) return;
    const d = enuFromLatLng(rtState.tx, target);
    const L = Math.hypot(d.x, d.y);
    if (L < 0.05) return;
    rtState.steerAngle = Math.atan2(d.y, d.x);
    updateUiLabels(); draw();
  }
  function confirmTxDirection() {
    if (!rtState.tx) return;
    rtState.txPending = false;
    rtState.steerConfirmed = true;
    updateTxVisual();
    updateUiLabels();
    setStatus(`TX fixed at ${rtState.tx.lat.toFixed(6)}, ${rtState.tx.lng.toFixed(6)} with steer ${angleDeg().toFixed(1)}°. Shift+click to place RX or launch rays.`);
  }

  window.rf2dHandleMapClick = function (e) {
    if (typeof polygonDrawingMode !== 'undefined' && polygonDrawingMode) return false;
    if (e.originalEvent && e.originalEvent.shiftKey) {
      rtState.rx = e.latlng;
      updateRxVisual();
      updateUiLabels();
      draw();
      setStatus(`RX placed at ${rtState.rx.lat.toFixed(6)}, ${rtState.rx.lng.toFixed(6)} with capture radius ${Number(ui.rx.value).toFixed(0)} m.`);
      return true;
    }
    if (!rtState.tx || !rtState.txPending) {
      rtState.tx = e.latlng;
      rtState.txPending = true;
      rtState.steerConfirmed = false;
      rtState.rays = [];
      updateTxVisual();
      updateUiLabels();
      draw();
      setStatus('TX point placed. Move mouse to steer, then click anywhere on the map to confirm direction. Shift+click places RX.');
      return true;
    }
    setSteerToLatLng(e.latlng);
    confirmTxDirection();
    return true;
  };
  window.rf2dHandleMouseMove = function (e) {
    if (!rtState.txPending || !rtState.tx) return;
    setSteerToLatLng(e.latlng);
  };
  window.rf2dGetConfirmedSteerDeg = function () {
    return rtState.steerConfirmed ? angleDeg() : NaN;
  };

  window.rf2dClearAll = clearAllRt;
  window.rf2dSetTxFromPlanner = function (lat, lng) {
    rtState.tx = L.latLng(lat, lng);
    rtState.txPending = false;
    rtState.steerConfirmed = true;
    rtState.steerAngle = Math.PI / 2;
    rtState.rays = [];
    if (!sceneIsFreshEnough()) invalidateRayScene();
    updateTxVisual();
    updateUiLabels();
    draw();
  };

  /** OSM footprints already prefetched by POST /api/plan (2D) — same geometry, no second buildings-near-point call. */
  window.rf2dIngestPlannerOsm = function (payload) {
    if (!payload || !Array.isArray(payload.buildings) || !payload.center) return;
    const lat = Number(payload.center.lat);
    const lng = Number(payload.center.lng != null ? payload.center.lng : payload.center.lon);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) return;
    const center = L.latLng(lat, lng);
    const R = Number(payload.radius_m);
    if (!Number.isFinite(R) || R <= 0) return;
    buildSceneFromBuildings(center, payload.buildings);
    rtState.sceneFetchRadiusM = R;
    saveRaySceneToStorage(center, R, payload.buildings);
    if (rtState.previewVisible) renderPreview();
    updateUiLabels();
    draw();
  };

  map.on('move zoom resize', draw);
  map.on('moveend zoomend', () => {
    draw();
  });

  ui.launch.addEventListener('click', () => { launchRays().catch(err => setStatus(`Ray trace failed: ${String(err)}`)); });
  ui.autoAim.addEventListener('click', () => {
    if (!rtState.tx || !rtState.rx) { setStatus('Place both TX and RX first.'); return; }
    rtState.txPending = false; rtState.steerConfirmed = true; setSteerToLatLng(rtState.rx); updateTxVisual(); updateUiLabels(); draw();
    setStatus(`TX auto-aimed to RX at ${angleDeg().toFixed(1)}°.`);
  });
  ui.preview.addEventListener('click', () => {
    rtState.previewVisible = !rtState.previewVisible;
    if (!rtState.previewVisible) {
      rtPreviewLayer.clearLayers();
      updateUiLabels();
      return;
    }
    ensureSceneLoaded(true).catch(err => setStatus(`OSM preview load failed: ${String(err)}`));
  });
  ui.clear.addEventListener('click', clearRays);
  [ui.rayCount, ui.spread, ui.bounces, ui.range, ui.rx].forEach(el => el.addEventListener('input', () => { updateUiLabels(); draw(); }));

  updateUiLabels();
  syncOverlaySize();
  draw();
})();
