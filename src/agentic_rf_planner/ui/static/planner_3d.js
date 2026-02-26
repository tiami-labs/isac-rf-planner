// 3D RF Planner UI (Cesium + Google Photorealistic 3D Tiles)
//
// Goals:
// - Mirror the 2D UI controls (ray mode + heights + sector configs + RF params).
// - Render coverage in 3D from the same per-cell grid that the 2D UI uses (no forced circle).
// - In 3D mode, if mesh profiles are missing, auto-generate+upload them in-browser.

import * as Cesium from "/Cesium/index.js";
import { buildAndUploadProfiles } from "/mesh_profiler_core.js";

let viewer = null;
let txEntity = null;
let points = null; // Cesium.PointPrimitiveCollection
let heatmapEntity = null; // (legacy) Cesium entity (ellipse w/ texture)
let bubblePrimitive = null; // 3D propagation shell
let sectorEntities = []; // visualization overlays
let currentTxLocation = null; // {lat, lon}
let sectorCounter = 0;

// Polygon drawing mode (Cesium)
let polygonDrawingMode = null; // { sectorId, points:[{lat,lon}], polylineEntity, polygonEntity }

function setStatus(msg) {
  const el = document.getElementById("status");
  if (el) el.textContent = msg;
}

function setMeshStatus(msg) {
  const el = document.getElementById("mesh-profile-status");
  if (el) el.textContent = msg;
}

function getNumber(id, fallback) {
  const el = document.getElementById(id);
  if (!el) return fallback;
  const v = parseFloat(el.value);
  return Number.isFinite(v) ? v : fallback;
}

function getString(id, fallback) {
  const el = document.getElementById(id);
  if (!el) return fallback;
  const v = String(el.value ?? "").trim();
  return v.length ? v : fallback;
}

function setInput(id, value) {
  const el = document.getElementById(id);
  if (el) el.value = String(value);
}

async function fetchConfig() {
  const r = await fetch("/api/config");
  if (!r.ok) throw new Error(`GET /api/config failed (${r.status})`);
  return await r.json();
}

function updateTxMarker(lat, lon) {
  const pos = Cesium.Cartesian3.fromDegrees(lon, lat, 20.0);
  if (!txEntity) {
    txEntity = viewer.entities.add({
      position: pos,
      point: {
        pixelSize: 10,
        color: Cesium.Color.YELLOW.withAlpha(0.95),
        outlineColor: Cesium.Color.BLACK.withAlpha(0.8),
        outlineWidth: 2,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
      label: {
        text: "TX",
        font: "14px sans-serif",
        fillColor: Cesium.Color.YELLOW,
        outlineColor: Cesium.Color.BLACK,
        outlineWidth: 2,
        style: Cesium.LabelStyle.FILL_AND_OUTLINE,
        pixelOffset: new Cesium.Cartesian2(0, -24),
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
    });
  } else {
    txEntity.position = pos;
  }
}

function clearOverlay() {
  if (heatmapEntity) {
    try { viewer.entities.remove(heatmapEntity); } catch {}
    heatmapEntity = null;
  }
  if (points) {
    viewer.scene.primitives.remove(points);
    points = null;
  }
  if (bubblePrimitive) {
    try { viewer.scene.primitives.remove(bubblePrimitive); } catch {}
    bubblePrimitive = null;
  }
  for (const e of sectorEntities) {
    try { viewer.entities.remove(e); } catch {}
  }
  sectorEntities = [];
}

function colorForValue(v, vmin, vmax) {
  if (!Number.isFinite(vmin) || !Number.isFinite(vmax) || vmax <= vmin) {
    return Cesium.Color.WHITE.withAlpha(0.65);
  }
  let t = (v - vmin) / (vmax - vmin);
  if (t < 0) t = 0;
  if (t > 1) t = 1;

  // Similar multi-hue gradient as 2D:
  // Blue (weak) → Cyan → Green → Yellow → Orange → Red (strong)
  let r = 0, g = 0, b = 0;
  if (t < 0.2) { // blue -> cyan
    const u = t / 0.2;
    r = 0; g = u; b = 1;
  } else if (t < 0.4) { // cyan -> green
    const u = (t - 0.2) / 0.2;
    r = 0; g = 1; b = 1 - u;
  } else if (t < 0.6) { // green -> yellow
    const u = (t - 0.4) / 0.2;
    r = u; g = 1; b = 0;
  } else if (t < 0.8) { // yellow -> orange
    const u = (t - 0.6) / 0.2;
    r = 1; g = 1 - 0.5 * u; b = 0;
  } else { // orange -> red
    const u = (t - 0.8) / 0.2;
    r = 1; g = 0.5 * (1 - u); b = 0;
  }
  return new Cesium.Color(r, g, b, 0.70);
}

// Render RF coverage using the same per-cell grid that the 2D UI uses.
// This intentionally preserves "missing" cells (no forced circular mask, no interpolation),
// which is what makes the footprint deform and follow streets / blockers.
function renderGridCoverage(grid) {
  clearOverlay();
  if (!grid || !Array.isArray(grid.cell_lat) || !Array.isArray(grid.cell_lon) || !Array.isArray(grid.rsrp_dbm)) return;
  const lats = grid.cell_lat;
  const lons = grid.cell_lon;
  const rsrp = grid.rsrp_dbm;
  if (lats.length === 0 || lons.length !== lats.length || rsrp.length !== lats.length) return;

  let vmin = Infinity;
  let vmax = -Infinity;
  for (let i = 0; i < rsrp.length; i++) {
    const v = rsrp[i];
    if (!Number.isFinite(v)) continue;
    if (v < vmin) vmin = v;
    if (v > vmax) vmax = v;
  }
  if (!Number.isFinite(vmin) || !Number.isFinite(vmax)) return;

  // Point primitives are much faster than entities at this scale.
  points = viewer.scene.primitives.add(new Cesium.PointPrimitiveCollection());
  const disableDepth = Number.POSITIVE_INFINITY;

  // Rough visual match to Leaflet circles: many small semi-transparent points.
  // Pixel size is camera-dependent; scaleByDistance keeps them readable while zooming.
  const basePx = Math.max(3, Math.min(12, Math.round(getNumber("heatmap-point-px", 7))));
  const scaleByDistance = new Cesium.NearFarScalar(500.0, 1.2, 8000.0, 0.4);

  for (let i = 0; i < lats.length; i++) {
    const lat = lats[i];
    const lon = lons[i];
    const v = rsrp[i];
    if (!Number.isFinite(lat) || !Number.isFinite(lon) || !Number.isFinite(v)) continue;

    points.add({
      position: Cesium.Cartesian3.fromDegrees(lon, lat, 0.0),
      color: colorForValue(v, vmin, vmax),
      pixelSize: basePx,
      scaleByDistance,
      heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
      disableDepthTestDistance: disableDepth,
    });
  }
}

// --- 3D propagation visualization (deformed sphere / "bubble") ---
//
// When ray_mode === "3d" we keep the RF semantics/pipeline identical (same /api/plan output),
// but change the visualization from a 2D ground projection to a 3D shell.
//
// Deformation source is deterministic:
// - Per-azimuth extent is taken from the returned grid itself (adaptive ray termination).
// - Optionally clipped by persisted mesh profile obstructions (same data used by 3D MapProvider).

function enuDirToEcef(originEcef, bearingRad, elevRad, outDirEcef) {
  // Bearing is degrees clockwise from North (matches mesh_profiler_core.js).
  const cosEl = Math.cos(elevRad);
  const localEnu = new Cesium.Cartesian3(
    Math.sin(bearingRad) * cosEl, // East
    Math.cos(bearingRad) * cosEl, // North
    Math.sin(elevRad)             // Up
  );
  const enuToFixed = Cesium.Transforms.eastNorthUpToFixedFrame(originEcef);
  Cesium.Matrix4.multiplyByPointAsVector(enuToFixed, localEnu, outDirEcef);
  Cesium.Cartesian3.normalize(outDirEcef, outDirEcef);
  return outDirEcef;
}

function extractPickPosition(hit) {
  if (!hit) return null;
  if (hit instanceof Cesium.Cartesian3) return hit;
  if (hit.position instanceof Cesium.Cartesian3) return hit.position;
  return null;
}

async function pickFirstIntersection(scene, ray) {
  // Prefer synchronous picking against currently-loaded tiles for responsiveness.
  if (typeof scene.pickFromRay === "function") {
    try {
      return scene.pickFromRay(ray);
    } catch {
      // fall through
    }
  }
  // Fallback: MostDetailed (can be slow, but better than nothing if pickFromRay is unavailable).
  if (typeof scene.pickFromRayMostDetailed === "function") {
    try {
      return await scene.pickFromRayMostDetailed(ray);
    } catch {
      return null;
    }
  }
  return null;
}

function rayEllipsoidDistanceMeters(ray, ellipsoid) {
  // Returns nearest positive intersection distance (meters) with ellipsoid, or null.
  if (!Cesium.IntersectionTests || typeof Cesium.IntersectionTests.rayEllipsoid !== "function") return null;
  const interval = Cesium.IntersectionTests.rayEllipsoid(ray, ellipsoid);
  if (!interval) return null;
  const t = interval.start >= 0.0 ? interval.start : interval.stop;
  if (!Number.isFinite(t) || t < 0.0) return null;
  return t;
}

function wrap360(deg) {
  let d = deg % 360.0;
  if (d < 0) d += 360.0;
  return d;
}

function haversineM(lat1, lon1, lat2, lon2) {
  const R = 6371000.0;
  const toRad = Cesium.Math.toRadians;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a = Math.sin(dLat / 2) ** 2
    + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  return R * c;
}

function bearingDeg(lat1, lon1, lat2, lon2) {
  // 0° = North, 90° = East
  const toRad = Cesium.Math.toRadians;
  const toDeg = Cesium.Math.toDegrees;
  const φ1 = toRad(lat1);
  const φ2 = toRad(lat2);
  const λ1 = toRad(lon1);
  const λ2 = toRad(lon2);
  const y = Math.sin(λ2 - λ1) * Math.cos(φ2);
  const x = Math.cos(φ1) * Math.sin(φ2) - Math.sin(φ1) * Math.cos(φ2) * Math.cos(λ2 - λ1);
  return wrap360(toDeg(Math.atan2(y, x)));
}

function quantizeBearing(deg, stepDeg) {
  const s = Math.max(0.5, Number(stepDeg) || 5.0);
  return wrap360(Math.round(wrap360(deg) / s) * s);
}

function inferBearingStepDegFromGrid(txLat, txLon, grid) {
  // Try to infer the azimuth step from returned grid bearings.
  try {
    const lats = grid?.cell_lat;
    const lons = grid?.cell_lon;
    if (!Array.isArray(lats) || !Array.isArray(lons) || lats.length < 20) return 5.0;
    const bs = [];
    for (let i = 0; i < lats.length; i += Math.max(1, Math.floor(lats.length / 200))) {
      bs.push(Math.round(bearingDeg(txLat, txLon, lats[i], lons[i])));
    }
    bs.sort((a, b) => a - b);
    let minDiff = Infinity;
    for (let i = 1; i < bs.length; i++) {
      const d = bs[i] - bs[i - 1];
      if (d > 0.5 && d < minDiff) minDiff = d;
    }
    if (Number.isFinite(minDiff) && minDiff >= 1.0 && minDiff <= 30.0) return minDiff;
  } catch {
    // ignore
  }
  return 5.0;
}

function buildPropagationRadiusByBearing(txLat, txLon, grid, stepDeg) {
  const lats = grid?.cell_lat;
  const lons = grid?.cell_lon;
  if (!Array.isArray(lats) || !Array.isArray(lons) || lats.length === 0) return new Map();
  const rByBin = new Map();
  for (let i = 0; i < lats.length; i++) {
    const lat = lats[i];
    const lon = lons[i];
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
    const b = quantizeBearing(bearingDeg(txLat, txLon, lat, lon), stepDeg);
    const r = haversineM(txLat, txLon, lat, lon);
    const prev = rByBin.get(b);
    if (!Number.isFinite(prev) || r > prev) rByBin.set(b, r);
  }
  return rByBin;
}

async function fetchMeshProfileSetOrNull(txLat, txLon) {
  const txHeightM = getNumber("tx-height-m", 10.0);
  const rxHeightM = getNumber("rx-height-m", 1.5);
  const maxRangeM = getNumber("max-range", 500.0);
  const drM = getNumber("dr-m", 5.0);
  const dthetaDeg = getNumber("dtheta", 5.0);

  const url = `/api/mesh-profiles/get?tx_lat=${encodeURIComponent(txLat)}&tx_lon=${encodeURIComponent(txLon)}`
    + `&tx_height_m=${encodeURIComponent(txHeightM)}&rx_height_m=${encodeURIComponent(rxHeightM)}`
    + `&max_range_m=${encodeURIComponent(maxRangeM)}&dr_m=${encodeURIComponent(drM)}&dtheta_deg=${encodeURIComponent(dthetaDeg)}`;

  try {
    const r = await fetch(url);
    if (!r.ok) return null;
    const j = await r.json();
    return j?.profile_set || null;
  } catch {
    return null;
  }
}

function buildFirstBlockRadiusByBearing(profileSet) {
  const out = new Map();
  if (!profileSet || !Array.isArray(profileSet.profiles)) return out;
  for (const p of profileSet.profiles) {
    const b = wrap360(Number(p.bearing_deg) || 0.0);
    const segs = Array.isArray(p.segments) ? p.segments : [];
    let r0 = Infinity;
    for (const s of segs) {
      const v = Number(s?.r0_m);
      if (Number.isFinite(v) && v > 0 && v < r0) r0 = v;
    }
    if (Number.isFinite(r0) && r0 < Infinity) out.set(b, r0);
  }
  return out;
}

async function computeTxOriginEcef(txLat, txLon, txHeightM) {
  // Anchor the origin to the rendered mesh when possible to keep height semantics consistent.
  const probeAlt = Math.max(10.0, txHeightM + 50.0);
  const probe = Cesium.Cartesian3.fromDegrees(txLon, txLat, probeAlt);
  try {
    const clamped = await viewer.scene.clampToHeightMostDetailed([probe]);
    if (clamped && clamped[0]) {
      const carto = Cesium.Cartographic.fromCartesian(clamped[0]);
      const meshH = Number.isFinite(carto.height) ? carto.height : 0.0;
      return Cesium.Cartesian3.fromDegrees(txLon, txLat, meshH + txHeightM);
    }
  } catch {
    // ignore and fall back
  }
  return Cesium.Cartesian3.fromDegrees(txLon, txLat, txHeightM);
}

async function renderDeformedPropagationBubble({ txLat, txLon, txHeightM, grid }) {
  clearOverlay();

  setStatus("3D: building propagation bubble…");

  const origin = await computeTxOriginEcef(txLat, txLon, txHeightM);
  const scene = viewer.scene;
  const ellipsoid = scene.globe && scene.globe.ellipsoid ? scene.globe.ellipsoid : Cesium.Ellipsoid.WGS84;
  const upDir = new Cesium.Cartesian3();
  try {
    ellipsoid.geodeticSurfaceNormal(origin, upDir);
    Cesium.Cartesian3.normalize(upDir, upDir);
  } catch {
    upDir.x = 0; upDir.y = 0; upDir.z = 1;
  }

  // Bubble radius must be consistent with the returned grid (adaptive ray termination).
  const azStep = inferBearingStepDegFromGrid(txLat, txLon, grid);
  const elStep = Math.max(10.0, azStep);

  const nAz = Math.max(8, Math.round(360.0 / azStep));
  const nEl = Math.max(6, Math.round(180.0 / elStep));
  const cols = nAz + 1; // seam closure
  const rows = nEl + 1;
  const nVerts = cols * rows;

  const maxRangeM = Number(grid?.rf_params?.max_range_m) || getNumber("max-range", 500.0);

  const propR = buildPropagationRadiusByBearing(txLat, txLon, grid, azStep);

  // Optional deterministic obstacle clipping from persisted mesh profiles.
  let firstBlockR = null;
  try {
    const prof = await fetchMeshProfileSetOrNull(txLat, txLon);
    firstBlockR = buildFirstBlockRadiusByBearing(prof);
  } catch {
    firstBlockR = null;
  }

  const positions = new Float64Array(nVerts * 3);
  const valid = new Uint8Array(nVerts);
  const scratchDir = new Cesium.Cartesian3();
  const scratchPos = new Cesium.Cartesian3();

  const eps = 0.5;
  let v = 0;
  for (let iEl = 0; iEl <= nEl; iEl++) {
    const elevDeg = -90.0 + (180.0 * iEl) / nEl;
    const elevRad = Cesium.Math.toRadians(elevDeg);

    for (let iAz = 0; iAz <= nAz; iAz++) {
      const bearingDeg0 = (360.0 * iAz) / nAz;
      const bearingDegQ = quantizeBearing(bearingDeg0, azStep);
      const bearingRad = Cesium.Math.toRadians(bearingDeg0);

      const baseR = propR.get(bearingDegQ);
      if (!Number.isFinite(baseR) || baseR <= 1.0) {
        const base = v * 3;
        positions[base] = origin.x;
        positions[base + 1] = origin.y;
        positions[base + 2] = origin.z;
        valid[v] = 0;
        v++;
        continue;
      }

      enuDirToEcef(origin, bearingRad, elevRad, scratchDir);

      let dist = Math.min(baseR, maxRangeM);

      // If profiles exist, use the first obstruction on this bearing as an additional clip.
      if (firstBlockR && firstBlockR.size) {
        const rBlock = firstBlockR.get(bearingDegQ);
        if (Number.isFinite(rBlock)) dist = Math.min(dist, rBlock);
      }

      // Ground clip: local ground plane at TX mesh height.
      const dotUp = Cesium.Cartesian3.dot(scratchDir, upDir);
      if (dotUp < -1e-3) {
        const t = txHeightM / (-dotUp);
        if (Number.isFinite(t) && t > 0) dist = Math.min(dist, t);
      }

      dist = Math.max(1.0, dist - eps);

      Cesium.Cartesian3.multiplyByScalar(scratchDir, dist, scratchPos);
      Cesium.Cartesian3.add(origin, scratchPos, scratchPos);

      const base = v * 3;
      positions[base] = scratchPos.x;
      positions[base + 1] = scratchPos.y;
      positions[base + 2] = scratchPos.z;
      valid[v] = 1;
      v++;
    }

    if (iEl % 2 === 0) {
      setStatus(`3D: building bubble… ${Math.round((100 * iEl) / nEl)}%`);
      // eslint-disable-next-line no-await-in-loop
      await new Promise((r) => setTimeout(r, 0));
    }
  }

  // Build indices, skipping triangles that touch invalid vertices (e.g. outside sector coverage).
  const idx = [];
  for (let iEl = 0; iEl < nEl; iEl++) {
    for (let iAz = 0; iAz < nAz; iAz++) {
      const i0 = iEl * cols + iAz;
      const i1 = i0 + 1;
      const i2 = i0 + cols;
      const i3 = i2 + 1;
      if (!(valid[i0] && valid[i1] && valid[i2] && valid[i3])) continue;
      idx.push(i0, i2, i1);
      idx.push(i1, i2, i3);
    }
  }
  if (idx.length < 3) {
    setStatus("3D: bubble skipped (no coverage cells for current sector configuration).");
    return;
  }
  const useUint32 = nVerts > 65535;
  const indices = useUint32 ? new Uint32Array(idx) : new Uint16Array(idx);

  const geom = new Cesium.Geometry({
    attributes: {
      position: new Cesium.GeometryAttribute({
        componentDatatype: Cesium.ComponentDatatype.DOUBLE,
        componentsPerAttribute: 3,
        values: positions,
      }),
    },
    indices,
    primitiveType: Cesium.PrimitiveType.TRIANGLES,
    boundingSphere: new Cesium.BoundingSphere(origin, maxRangeM),
  });

  const instance = new Cesium.GeometryInstance({ geometry: geom });
  const mat = Cesium.Material.fromType("Color", {
    color: new Cesium.Color(0.2, 0.8, 1.0, 0.22),
  });
  bubblePrimitive = new Cesium.Primitive({
    geometryInstances: instance,
    appearance: new Cesium.MaterialAppearance({
      material: mat,
      translucent: true,
      closed: false,
      faceForward: true,
    }),
    asynchronous: false,
  });

  viewer.scene.primitives.add(bubblePrimitive);
  setStatus("Plan complete.");
}

function addSectorUI() {
  const container = document.getElementById("sectors-container");
  if (!container) return;

  const sectorId = `sector_${++sectorCounter}`;
  const sectorDiv = document.createElement("div");
  sectorDiv.id = `sector-${sectorId}`;
  sectorDiv.className = "sector-config";
  sectorDiv.style.cssText = "padding: 8px; background: #222; border: 1px solid #444; border-radius: 3px; position: relative;";

  sectorDiv.innerHTML = `
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
      <span style="font-size:11px; font-weight:bold; color:#eee;">Sector ${sectorCounter}</span>
      <button type="button" class="remove-sector-btn"
        style="padding:2px 6px; background:#cc0000; color:white; border:none; border-radius:2px; cursor:pointer; font-size:10px;">
        Remove
      </button>
    </div>

    <div style="margin-bottom:6px;">
      <label style="display:block; font-size:10px; margin-bottom:2px; color:#ccc;">Sector Type:</label>
      <select class="sector-type"
        style="width:100%; padding:4px; box-sizing:border-box; background:#333; color:#eee; border:1px solid #555; border-radius:2px; font-size:11px;">
        <option value="360">360° (Omnidirectional)</option>
        <option value="angle" selected>Angle-based</option>
        <option value="polygon">Abstract Polygon</option>
      </select>
    </div>

    <div class="sector-angle-config" style="display:grid; grid-template-columns:1fr 1fr; gap:6px; font-size:10px;">
      <div>
        <label style="display:block; font-size:10px; margin-bottom:2px; color:#ccc;">Start Angle (°):</label>
        <input type="number" class="sector-start-angle" step="0.1" min="0" max="360" value="0"
          style="width:100%; padding:4px; box-sizing:border-box; background:#333; color:#eee; border:1px solid #555; border-radius:2px; font-size:11px;" />
      </div>
      <div>
        <label style="display:block; font-size:10px; margin-bottom:2px; color:#ccc;">End Angle (°):</label>
        <input type="number" class="sector-end-angle" step="0.1" min="0" max="360" value="120"
          style="width:100%; padding:4px; box-sizing:border-box; background:#333; color:#eee; border:1px solid #555; border-radius:2px; font-size:11px;" />
      </div>
    </div>

    <div class="sector-polygon-config" style="display:none; margin-top:6px; font-size:10px;">
      <div style="margin-bottom:4px; color:#aaa;">
        Set TX (click mesh), then click "Draw Polygon" and click mesh to add vertices (2+). TX is implicit origin.
      </div>
      <button type="button" class="draw-polygon-btn"
        style="width:100%; padding:4px; background:#0066cc; color:white; border:none; border-radius:2px; cursor:pointer; font-size:10px;">
        Draw Polygon
      </button>
      <button type="button" class="finish-polygon-btn"
        style="width:100%; padding:4px; margin-top:4px; background:#666; color:white; border:none; border-radius:2px; cursor:pointer; font-size:10px; display:none;">
        Finish Drawing
      </button>
      <div class="polygon-status" style="margin-top:4px; font-size:9px; color:#888;">Not started</div>
    </div>

    <div style="display:grid; grid-template-columns:1fr 1fr; gap:6px; font-size:10px; margin-top:6px;">
      <div>
        <label style="display:block; font-size:10px; margin-bottom:2px; color:#ccc;">Frequency (MHz):</label>
        <input type="number" class="sector-freq" step="0.1" min="0" value="3500"
          style="width:100%; padding:4px; box-sizing:border-box; background:#333; color:#eee; border:1px solid #555; border-radius:2px; font-size:11px;" />
      </div>
      <div>
        <label style="display:block; font-size:10px; margin-bottom:2px; color:#ccc;">TX Power (dBm):</label>
        <input type="number" class="sector-power" step="0.1" value="43"
          style="width:100%; padding:4px; box-sizing:border-box; background:#333; color:#eee; border:1px solid #555; border-radius:2px; font-size:11px;" />
      </div>
    </div>
  `;

  container.appendChild(sectorDiv);

  sectorDiv.querySelector(".remove-sector-btn").addEventListener("click", () => {
    if (polygonDrawingMode && polygonDrawingMode.sectorId === sectorId) {
      stopPolygonDrawing();
    }
    sectorDiv.remove();
  });

  sectorDiv.querySelector(".sector-type").addEventListener("change", (e) => {
    const type = e.target.value;
    const angleConfig = sectorDiv.querySelector(".sector-angle-config");
    const polyConfig = sectorDiv.querySelector(".sector-polygon-config");
    if (type === "polygon") {
      angleConfig.style.display = "none";
      polyConfig.style.display = "block";
    } else {
      angleConfig.style.display = "grid";
      polyConfig.style.display = "none";
      if (polygonDrawingMode && polygonDrawingMode.sectorId === sectorId) stopPolygonDrawing();
    }
  });

  sectorDiv.querySelector(".draw-polygon-btn").addEventListener("click", () => startPolygonDrawing(sectorId));
  sectorDiv.querySelector(".finish-polygon-btn").addEventListener("click", () => finishPolygonDrawing());
}

function collectSectorConfigs() {
  const sectors = [];
  const sectorDivs = document.querySelectorAll(".sector-config");

  sectorDivs.forEach((div, index) => {
    const sectorType = div.querySelector(".sector-type").value;
    const freq = parseFloat(div.querySelector(".sector-freq").value);
    const power = parseFloat(div.querySelector(".sector-power").value);

    if (!Number.isFinite(freq) || !Number.isFinite(power)) return;

    const sectorId = `sector_${index + 1}`;
    const sectorConfig = {
      sector_id: sectorId,
      sector_type: sectorType,
      freq_mhz: freq,
      tx_power_dbm: power,
      channel_bandwidth_mhz: getNumber("bw-mhz", 20.0),
    };

    if (sectorType === "360") {
      sectors.push(sectorConfig);
      return;
    }

    if (sectorType === "angle") {
      const startAngle = parseFloat(div.querySelector(".sector-start-angle").value);
      const endAngle = parseFloat(div.querySelector(".sector-end-angle").value);
      if (!Number.isFinite(startAngle) || !Number.isFinite(endAngle)) return;
      sectorConfig.start_angle_deg = startAngle;
      sectorConfig.end_angle_deg = endAngle;
      sectors.push(sectorConfig);
      return;
    }

    if (sectorType === "polygon") {
      const polygonPointsData = div.getAttribute("data-polygon-points");
      if (!polygonPointsData) return;
      try {
        const polygonPoints = JSON.parse(polygonPointsData);
        if (Array.isArray(polygonPoints) && polygonPoints.length >= 2) {
          sectorConfig.polygon_points = polygonPoints;
          sectors.push(sectorConfig);
        }
      } catch {
        return;
      }
    }
  });

  return sectors;
}

function startPolygonDrawing(sectorId) {
  if (!currentTxLocation) {
    setStatus("Set TX first (click the mesh), then draw polygon.");
    return;
  }

  if (polygonDrawingMode) stopPolygonDrawing();

  polygonDrawingMode = {
    sectorId,
    points: [],
    polylineEntity: null,
    polygonEntity: null,
  };

  const sectorDiv = document.getElementById(`sector-${sectorId}`);
  if (sectorDiv) {
    sectorDiv.querySelector(".finish-polygon-btn").style.display = "block";
    sectorDiv.querySelector(".polygon-status").textContent = "Drawing: 0 vertices";
  }
  setStatus("Polygon drawing: click mesh to add vertices, then 'Finish Drawing'.");
}

function stopPolygonDrawing() {
  if (!polygonDrawingMode) return;

  if (polygonDrawingMode.polylineEntity) viewer.entities.remove(polygonDrawingMode.polylineEntity);
  if (polygonDrawingMode.polygonEntity) viewer.entities.remove(polygonDrawingMode.polygonEntity);

  const sectorDiv = document.getElementById(`sector-${polygonDrawingMode.sectorId}`);
  if (sectorDiv) {
    sectorDiv.querySelector(".finish-polygon-btn").style.display = "none";
  }

  polygonDrawingMode = null;
}

function updatePolygonPreview() {
  if (!polygonDrawingMode || !currentTxLocation) return;

  const coords = [
    Cesium.Cartesian3.fromDegrees(currentTxLocation.lon, currentTxLocation.lat, 5.0),
    ...polygonDrawingMode.points.map(p => Cesium.Cartesian3.fromDegrees(p.lon, p.lat, 5.0)),
  ];

  // Polyline preview
  if (!polygonDrawingMode.polylineEntity) {
    polygonDrawingMode.polylineEntity = viewer.entities.add({
      polyline: {
        positions: coords,
        width: 3,
        material: Cesium.Color.CYAN.withAlpha(0.9),
        clampToGround: true,
      },
    });
  } else {
    polygonDrawingMode.polylineEntity.polyline.positions = coords;
  }

  // Polygon preview requires TX + >=2 vertices
  if (polygonDrawingMode.points.length >= 2) {
    const hierarchy = new Cesium.PolygonHierarchy(coords);
    if (!polygonDrawingMode.polygonEntity) {
      polygonDrawingMode.polygonEntity = viewer.entities.add({
        polygon: {
          hierarchy,
          material: Cesium.Color.CYAN.withAlpha(0.25),
          outline: true,
          outlineColor: Cesium.Color.CYAN.withAlpha(0.9),
          perPositionHeight: false,
          clampToGround: true,
        },
      });
    } else {
      polygonDrawingMode.polygonEntity.polygon.hierarchy = hierarchy;
    }
  }
}

function finishPolygonDrawing() {
  if (!polygonDrawingMode) return;

  const sectorId = polygonDrawingMode.sectorId;
  const sectorDiv = document.getElementById(`sector-${sectorId}`);
  if (!sectorDiv) {
    stopPolygonDrawing();
    return;
  }

  if (polygonDrawingMode.points.length < 2) {
    sectorDiv.querySelector(".polygon-status").textContent = "Need at least 2 vertices";
    return;
  }

  sectorDiv.setAttribute("data-polygon-points", JSON.stringify(polygonDrawingMode.points));
  sectorDiv.querySelector(".polygon-status").textContent = `Saved: ${polygonDrawingMode.points.length} vertices`;
  sectorDiv.querySelector(".finish-polygon-btn").style.display = "none";

  stopPolygonDrawing();
  setStatus("Polygon sector saved. Click Plan RF.");
}

function addPolygonVertex(lat, lon) {
  if (!polygonDrawingMode) return;

  polygonDrawingMode.points.push({ lat, lon });

  const sectorDiv = document.getElementById(`sector-${polygonDrawingMode.sectorId}`);
  if (sectorDiv) {
    sectorDiv.querySelector(".polygon-status").textContent = `Drawing: ${polygonDrawingMode.points.length} vertices`;
  }

  updatePolygonPreview();
}

function toggleSectorVisibility(show) {
  for (const e of sectorEntities) {
    try { e.show = !!show; } catch {}
  }
}

function clearMap() {
  clearOverlay();
  // Keep TX marker but clear heatmap + sector overlays
  setMeshStatus("");
  setStatus("Cleared overlays.");
}

function drawSectorOverlays(sectors, txLat, txLon, radiusM) {
  const show = document.getElementById("show-sectors-toggle")?.checked ?? true;
  if (!show) return;

  // Quick polar-to-latlon helper
  const earth = 6371000.0;
  const toLatLon = (bearingDeg, rM) => {
    const br = Cesium.Math.toRadians(bearingDeg);
    const d = rM / earth;
    const lat1 = Cesium.Math.toRadians(txLat);
    const lon1 = Cesium.Math.toRadians(txLon);
    const lat2 = Math.asin(Math.sin(lat1) * Math.cos(d) + Math.cos(lat1) * Math.sin(d) * Math.cos(br));
    const lon2 = lon1 + Math.atan2(Math.sin(br) * Math.sin(d) * Math.cos(lat1), Math.cos(d) - Math.sin(lat1) * Math.sin(lat2));
    return { lat: Cesium.Math.toDegrees(lat2), lon: Cesium.Math.toDegrees(lon2) };
  };

  for (const s of sectors || []) {
    const st = Number.isFinite(s.start_angle_deg) ? s.start_angle_deg : 0;
    const en = Number.isFinite(s.end_angle_deg) ? s.end_angle_deg : 360;

    // Only visualize angle sectors (polygon sectors already have explicit shape)
    if (s.sector_type === "polygon" && Array.isArray(s.polygon_points) && s.polygon_points.length >= 2) {
      const coords = [
        Cesium.Cartesian3.fromDegrees(txLon, txLat, 5.0),
        ...s.polygon_points.map(p => Cesium.Cartesian3.fromDegrees(p.lon, p.lat, 5.0)),
      ];
      const poly = viewer.entities.add({
        polygon: {
          hierarchy: coords,
          material: Cesium.Color.WHITE.withAlpha(0.12),
          outline: true,
          outlineColor: Cesium.Color.WHITE.withAlpha(0.5),
          clampToGround: true,
        },
      });
      sectorEntities.push(poly);
      continue;
    }

    if (s.sector_type === "360") continue;

    const steps = 36;
    const pts = [Cesium.Cartesian3.fromDegrees(txLon, txLat, 5.0)];
    for (let k = 0; k <= steps; k++) {
      const a = st + (en - st) * (k / steps);
      const p = toLatLon(a, radiusM);
      pts.push(Cesium.Cartesian3.fromDegrees(p.lon, p.lat, 5.0));
    }

    const ent = viewer.entities.add({
      polygon: {
        hierarchy: pts,
        material: Cesium.Color.WHITE.withAlpha(0.10),
        outline: true,
        outlineColor: Cesium.Color.WHITE.withAlpha(0.5),
        clampToGround: true,
      },
    });
    sectorEntities.push(ent);
  }
}

async function ensureProfiles(txLat, txLon) {
  const txHeightM = getNumber("tx-height-m", 10.0);
  const rxHeightM = getNumber("rx-height-m", 1.5);
  const maxRangeM = getNumber("max-range", 500.0);
  const drM = getNumber("dr-m", 5.0);
  const dthetaDeg = getNumber("dtheta", 5.0);

  const hasUrl = `/api/mesh-profiles/has?tx_lat=${encodeURIComponent(txLat)}&tx_lon=${encodeURIComponent(txLon)}`
    + `&tx_height_m=${encodeURIComponent(txHeightM)}&rx_height_m=${encodeURIComponent(rxHeightM)}`
    + `&max_range_m=${encodeURIComponent(maxRangeM)}&dr_m=${encodeURIComponent(drM)}&dtheta_deg=${encodeURIComponent(dthetaDeg)}`;

  const hasResp = await fetch(hasUrl);
  if (!hasResp.ok) throw new Error(`GET ${hasUrl} failed (${hasResp.status})`);
  const hasJson = await hasResp.json();
  if (hasJson && hasJson.exists) {
    setMeshStatus(`3D profiles cached.\nkey=${hasJson.key}`);
    return { exists: true, key: hasJson.key };
  }

  setMeshStatus("3D profiles missing. Generating + uploading…");

  await buildAndUploadProfiles({
    containerId: "cesiumProfilerHost",
    lat: txLat,
    lon: txLon,
    txHeightM,
    rxHeightM,
    maxRangeM,
    drM,
    dthetaDeg,
    mode: "slice",
    onProgress: (m) => setMeshStatus(String(m)),
  });

  setMeshStatus("3D profiles uploaded.");
  return { exists: true, key: null };
}

async function runPlan() {
  // Prefer current TX if set by clicking mesh; otherwise use input boxes.
  let lat = getNumber("lat-input", NaN);
  let lon = getNumber("lon-input", NaN);
  if ((!Number.isFinite(lat) || !Number.isFinite(lon)) && currentTxLocation) {
    lat = currentTxLocation.lat;
    lon = currentTxLocation.lon;
  }

  if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
    setStatus("Enter coordinates or click on the mesh to set TX.");
    return;
  }

  const rayMode = getString("ray-mode", "3d").toLowerCase();
  const txHeightM = getNumber("tx-height-m", 10.0);
  const rxHeightM = getNumber("rx-height-m", 1.5);

  const sectors = collectSectorConfigs();

  // Auto-provision 3D mesh profiles from the main UI if needed.
  if (rayMode === "3d") {
    try {
      setStatus("3D: checking cached ray profiles…");
      await ensureProfiles(lat, lon);
    } catch (e) {
      setStatus(`3D: failed to build mesh profiles.\n\n${e}`);
      return;
    }
  }

  setStatus("Running RF planning…");

  const body = {
    lat,
    lon,
    freq_mhz: getNumber("freq-mhz", 3500.0),
    tx_power_dbm: getNumber("tx-power-dbm", 43.0),
    noise_figure_db: getNumber("noise-figure-db", 7.0),
    subcarrier_spacing_khz: getNumber("scs-khz", 15.0),
    num_resource_blocks: Math.round(getNumber("num-rb", 100)),
    channel_bandwidth_mhz: getNumber("bw-mhz", 20.0),
    mimo_mode: getString("mimo-mode", "SISO"),
    enable_link_adaptation: getString("link-adapt", "1") === "1",
    fixed_modulation: null,
    sectors: sectors.length ? sectors : null,
    ray_mode: rayMode,
    tx_height_m: txHeightM,
    rx_height_m: rxHeightM,
  };

  let resp;
  try {
    resp = await fetch("/api/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    setStatus(`/api/plan request failed: ${e}`);
    return;
  }

  const text = await resp.text();
  if (!resp.ok) {
    try {
      const j = JSON.parse(text);
      if (resp.status === 409 && j && j.status === "missing_mesh_profiles") {
        setStatus("3D: missing profiles (server). Retrying after generating…");
        await ensureProfiles(lat, lon);
        return await runPlan();
      }
    } catch {
      // ignore JSON parse error
    }
    setStatus(`/api/plan failed (${resp.status}):\n${text}`);
    return;
  }

  let out;
  try {
    out = JSON.parse(text);
  } catch (e) {
    setStatus(`Invalid JSON from /api/plan: ${e}`);
    return;
  }

  // Update TX marker to snapped point (backend always snaps today).
  if (out.snapped_tx && Number.isFinite(out.snapped_tx.lat) && Number.isFinite(out.snapped_tx.lon)) {
    updateTxMarker(out.snapped_tx.lat, out.snapped_tx.lon);
  } else {
    updateTxMarker(lat, lon);
  }

  // Coverage rendering (3D): draw the same per-cell grid as 2D.
  // This preserves missing cells (no forced circle, no interpolation), which is what
  // creates the deformed footprint and road / canyon effects.
  if (out.grid) {
    if (rayMode === "3d") {
      const tx0 = (out.snapped_tx && Number.isFinite(out.snapped_tx.lat) && Number.isFinite(out.snapped_tx.lon))
        ? out.snapped_tx
        : { lat, lon };
      try {
        await renderDeformedPropagationBubble({
          txLat: tx0.lat,
          txLon: tx0.lon,
          txHeightM,
          grid: out.grid,
        });
      } catch (e) {
        // Fall back to the 2D ground projection if bubble construction fails.
        console.warn("Failed to build 3D propagation bubble:", e);
        renderGridCoverage(out.grid);
      }
    } else {
      renderGridCoverage(out.grid);
    }
  }

  // Sector overlays: approximate radius from profile params (matches circular heatmap intent).
  const radiusM = getNumber("max-range", 500.0);
  if (out.sectors && out.snapped_tx) {
    drawSectorOverlays(out.sectors, out.snapped_tx.lat, out.snapped_tx.lon, radiusM);
  }

  const key = out.mesh_profile_key ? `\nmesh_key=${out.mesh_profile_key}` : "";
  setStatus(`Plan complete.${key}`);
}

async function init() {
  if (!window.CESIUM_BASE_URL) window.CESIUM_BASE_URL = "/Cesium/";

  const cfg = await fetchConfig();
  if (!cfg.google_maps_api_key_present) {
    setStatus("Missing GOOGLE_MAPS_API_KEY in server env.\n\nexport GOOGLE_MAPS_API_KEY=… and restart uvicorn.");
    return;
  }
  Cesium.GoogleMaps.defaultApiKey = cfg.google_maps_api_key;

  // Default ray mode from server env
  const rmEl = document.getElementById("ray-mode");
  if (rmEl && cfg.default_ray_mode) rmEl.value = cfg.default_ray_mode;

  viewer = new Cesium.Viewer("cesiumContainer", {
    animation: false,
    timeline: false,
    geocoder: false,
    homeButton: true,
    sceneModePicker: false,
    navigationHelpButton: false,
    baseLayerPicker: false,
    terrainProvider: new Cesium.EllipsoidTerrainProvider(),
  });

  viewer.scene.globe.depthTestAgainstTerrain = true;

  try {
    const tileset = await Cesium.createGooglePhotorealistic3DTileset();
    viewer.scene.primitives.add(tileset);
    if (tileset.readyPromise) await tileset.readyPromise;
  } catch (e) {
    setStatus(`Failed to load Google mesh tileset.\n\n${e}`);
    return;
  }

  // Optional: add OSM buildings tiles for semantic + geometry interaction.
  // (Google photorealistic tiles include buildings too, but OSM buildings are useful for
  // consistent picking/clipping across providers.)
  try {
    const osm = await Cesium.createOsmBuildingsAsync();
    viewer.scene.primitives.add(osm);
    if (osm.readyPromise) await osm.readyPromise;
  } catch {
    // Non-fatal.
  }

  // Start zoomed-in to the default coordinate (matches 2D UX).
  try {
    const lat0 = getNumber("lat-input", 37.7749);
    const lon0 = getNumber("lon-input", -122.4194);
    viewer.camera.setView({
      destination: Cesium.Cartesian3.fromDegrees(lon0, lat0, 1200.0),
      orientation: {
        heading: Cesium.Math.toRadians(0.0),
        pitch: Cesium.Math.toRadians(-45.0),
        roll: 0.0,
      },
    });
  } catch {}

  viewer.screenSpaceEventHandler.setInputAction((click) => {
    const cartesian = viewer.scene.pickPosition(click.position);
    if (!cartesian) return;

    const carto = Cesium.Cartographic.fromCartesian(cartesian);
    const lat = Cesium.Math.toDegrees(carto.latitude);
    const lon = Cesium.Math.toDegrees(carto.longitude);

    if (polygonDrawingMode) {
      addPolygonVertex(lat, lon);
      return;
    }

    currentTxLocation = { lat, lon };
    setInput("lat-input", lat.toFixed(6));
    setInput("lon-input", lon.toFixed(6));
    updateTxMarker(lat, lon);
    setStatus(`TX set:\n  lat=${lat.toFixed(6)}\n  lon=${lon.toFixed(6)}\n\nClick Plan RF.`);
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

  // Wire UI
  document.getElementById("coord-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    await runPlan();
  });

  document.getElementById("add-sector-btn").addEventListener("click", () => addSectorUI());
  document.getElementById("show-sectors-toggle").addEventListener("change", (e) => toggleSectorVisibility(e.target.checked));
  document.getElementById("clear-map-btn").addEventListener("click", () => clearMap());

  setStatus("Click on the 3D mesh to set TX, then click Plan RF.");
}

init().catch((e) => setStatus(`Init failed:\n${e}`));
