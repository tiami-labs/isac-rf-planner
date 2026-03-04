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
// Planned overlays persist until the user presses "Clear Map".
// Keep the selection TX marker (txEntity) separate.
let planEntities = []; // Cesium.Entity[] (heatmaps, planned TX markers, etc.)
let planPrimitives = []; // Cesium.Primitive[] / collections
let planCounter = 0;

let sectorEntities = []; // visualization overlays (entities)
let currentTxLocation = null; // {lat, lon}
let sectorCounter = 0;
let osmHeatmapMeshCache = new Map(); // key -> { geometry }

// Fixed RSRP scale (used for legend labels / cross-plan comparability).
const FIXED_RSRP_MIN = -150.0;
const FIXED_RSRP_MAX = 50.0;

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

function _pickGlobePosition(windowPos) {
  // Use globe pick (ellipsoid) for a stable scale even when 3D tiles aren't fully loaded.
  try {
    const ray = viewer.camera.getPickRay(windowPos);
    if (ray) {
      const p = viewer.scene.globe.pick(ray, viewer.scene);
      if (p) return p;
    }
  } catch {}
  try {
    return viewer.camera.pickEllipsoid(windowPos, viewer.scene.globe.ellipsoid);
  } catch {}
  return null;
}

function _niceDistanceMeters(maxMeters) {
  if (!Number.isFinite(maxMeters) || maxMeters <= 0) return 0;
  const pow10 = Math.pow(10, Math.floor(Math.log10(maxMeters)));
  const steps = [1, 2, 5, 10];
  let best = pow10;
  for (const s of steps) {
    const d = s * pow10;
    if (d <= maxMeters) best = d;
  }
  return best;
}

function _formatDistance(m) {
  if (!Number.isFinite(m) || m <= 0) return "-";
  if (m >= 1000) {
    const km = m / 1000.0;
    const digits = km >= 10 ? 0 : 1;
    return `${km.toFixed(digits)} km`;
  }
  return `${Math.round(m)} m`;
}

function initDistanceScale() {
  const labelEl = document.getElementById("distance-scale-label");
  const barEl = document.getElementById("distance-scale-bar");
  if (!labelEl || !barEl) return;

  const maxBarPx = 100;
  let last = 0;

  const update = () => {
    const now = performance.now();
    if (now - last < 250) return;
    last = now;

    const w = viewer.canvas.clientWidth || viewer.canvas.width;
    const h = viewer.canvas.clientHeight || viewer.canvas.height;
    if (!w || !h) return;

    // Measure at the bottom center of the viewport.
    const y = h - 2;
    const x1 = Math.max(0, Math.round(w / 2 - maxBarPx / 2));
    const x2 = Math.min(w - 1, Math.round(w / 2 + maxBarPx / 2));

    const p1 = _pickGlobePosition(new Cesium.Cartesian2(x1, y));
    const p2 = _pickGlobePosition(new Cesium.Cartesian2(x2, y));
    if (!p1 || !p2) {
      labelEl.textContent = "-";
      barEl.style.width = `${maxBarPx}px`;
      return;
    }

    const c1 = Cesium.Cartographic.fromCartesian(p1);
    const c2 = Cesium.Cartographic.fromCartesian(p2);
    const geo = new Cesium.EllipsoidGeodesic(c1, c2);
    const dist = geo.surfaceDistance;
    if (!Number.isFinite(dist) || dist <= 0) return;

    const nice = _niceDistanceMeters(dist);
    const px = Math.max(10, Math.min(maxBarPx, (nice / dist) * maxBarPx));
    barEl.style.width = `${px.toFixed(0)}px`;
    labelEl.textContent = _formatDistance(nice);
  };

  viewer.scene.postRender.addEventListener(update);
  update();
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

function addPlannedTxMarker(lat, lon) {
  // Persist planned TX markers so multiple plans remain visible.
  const n = ++planCounter;
  const pos = Cesium.Cartesian3.fromDegrees(lon, lat, 20.0);
  const ent = viewer.entities.add({
    position: pos,
    point: {
      pixelSize: 9,
      color: Cesium.Color.YELLOW.withAlpha(0.85),
      outlineColor: Cesium.Color.BLACK.withAlpha(0.8),
      outlineWidth: 2,
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
    },
    label: {
      text: `TX${n}`,
      font: "12px sans-serif",
      fillColor: Cesium.Color.YELLOW,
      outlineColor: Cesium.Color.BLACK,
      outlineWidth: 2,
      style: Cesium.LabelStyle.FILL_AND_OUTLINE,
      pixelOffset: new Cesium.Cartesian2(0, -22),
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
    },
  });
  planEntities.push(ent);
}

function clearOverlay() {
  for (const e of planEntities) {
    try { viewer.entities.remove(e); } catch {}
  }
  planEntities = [];

  for (const p of planPrimitives) {
    try { viewer.scene.primitives.remove(p); } catch {}
  }
  planPrimitives = [];
  for (const e of sectorEntities) {
    try { viewer.entities.remove(e); } catch {}
  }
  sectorEntities = [];

  planCounter = 0;

  // Hide legend when overlay is cleared.
  const legend = document.getElementById("rsrp-legend");
  if (legend) legend.style.display = "none";
}

function updateRSRPLegend(scaleMinRSRP, scaleMaxRSRP, actualMinRSRP, actualMaxRSRP) {
  const legend = document.getElementById("rsrp-legend");
  if (!legend) return;

  legend.style.display = "block";

  // Match the multi-hue mapping in colorForValue().
  const gradient = document.getElementById("rsrp-legend-gradient");
  if (gradient) {
    gradient.style.background = `linear-gradient(to top,
      rgb(0, 0, 255) 0%,
      rgb(0, 255, 255) 20%,
      rgb(0, 255, 0) 40%,
      rgb(255, 255, 0) 60%,
      rgb(255, 128, 0) 80%,
      rgb(255, 0, 0) 100%
    )`;
  }

  const maxLabel = document.getElementById("legend-max");
  const midLabel = document.getElementById("legend-mid");
  const minLabel = document.getElementById("legend-min");
  if (maxLabel) maxLabel.textContent = scaleMaxRSRP.toFixed(0);
  if (midLabel) midLabel.textContent = ((scaleMinRSRP + scaleMaxRSRP) / 2).toFixed(0);
  if (minLabel) minLabel.textContent = scaleMinRSRP.toFixed(0);

  const maxValue = document.getElementById("legend-max-value");
  const minValue = document.getElementById("legend-min-value");
  if (maxValue) maxValue.textContent = Number.isFinite(actualMaxRSRP) ? actualMaxRSRP.toFixed(1) : "-";
  if (minValue) minValue.textContent = Number.isFinite(actualMinRSRP) ? actualMinRSRP.toFixed(1) : "-";
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

  // Show legend with dBm scale.
  updateRSRPLegend(FIXED_RSRP_MIN, FIXED_RSRP_MAX, vmin, vmax);

  // Point primitives are much faster than entities at this scale.
  const points = viewer.scene.primitives.add(new Cesium.PointPrimitiveCollection());
  planPrimitives.push(points);
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

// 3D OSM-only rendering: map-aligned raster drape (no radial spokes).
async function renderHeatmapDrapeOsm3d(heatmap, grid) {
  // Fast path for 3D OSM-only mode:
  // - Backend returns a pre-colored PNG texture (base64 data URL).
  // - We drape it as a single Cesium ellipse clamped to ground/tiles.
  // This avoids per-vertex clampToHeightMostDetailed, which is too slow for interactive use.

  if (!grid || !grid.tx || !Number.isFinite(grid.tx.lat) || !Number.isFinite(grid.tx.lon)) return;

  const txLat = grid.tx.lat;
  const txLon = grid.tx.lon;

  const radiusM =
    (heatmap && Number.isFinite(heatmap.radius_m) ? Number(heatmap.radius_m) : NaN) ||
    (grid && grid.rf_params && Number.isFinite(grid.rf_params.max_range_m) ? Number(grid.rf_params.max_range_m) : NaN) ||
    getNumber("max-range", 500.0);

  if (!Number.isFinite(radiusM) || radiusM <= 0) return;

  // Legend: use the fixed scale for consistency, but show actual min/max from the grid if provided.
  const actualMin = (heatmap && Number.isFinite(heatmap.vmin)) ? Number(heatmap.vmin) : FIXED_RSRP_MIN;
  const actualMax = (heatmap && Number.isFinite(heatmap.vmax)) ? Number(heatmap.vmax) : FIXED_RSRP_MAX;
  updateRSRPLegend(FIXED_RSRP_MIN, FIXED_RSRP_MAX, actualMin, actualMax);

  const imgSrc = heatmap && heatmap.png_b64 ? heatmap.png_b64 : null;

  if (!imgSrc) {
    // Fallback: if backend didn't provide a PNG, fall back to point grid (if available).
    if (grid && Array.isArray(grid.cell_lat) && grid.cell_lat.length) {
      renderGridCoverage(grid);
    }
    return;
  }

  const ent = viewer.entities.add({
    position: Cesium.Cartesian3.fromDegrees(txLon, txLat),
    ellipse: {
      semiMajorAxis: radiusM,
      semiMinorAxis: radiusM,
      // Smaller granularity reduces visible faceting.
      granularity: Cesium.Math.toRadians(0.25),
      material: new Cesium.ImageMaterialProperty({ image: imgSrc, transparent: true }),
      heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
      outline: false,
    },
  });
  planEntities.push(ent);
}

function normalizeAngleDeg(a) {
  let x = a % 360.0;
  if (x < 0) x += 360.0;
  return x;
}

// Reconstruct ENU distance + bearing using the same small-distance approximation
// as coverage_grid._project_from_tx (flat earth for <= ~1km).
function enuRangeBearing(txLatDeg, txLonDeg, latDeg, lonDeg) {
  const R = 6371000.0;
  const txLat = Cesium.Math.toRadians(txLatDeg);
  const dLat = Cesium.Math.toRadians(latDeg - txLatDeg);
  const dLon = Cesium.Math.toRadians(lonDeg - txLonDeg);
  const north = dLat * R;
  const east = dLon * R * Math.cos(txLat);
  const range = Math.sqrt(north * north + east * east);
  const bearing = normalizeAngleDeg(Cesium.Math.toDegrees(Math.atan2(east, north)));
  return { range_m: range, bearing_deg: bearing };
}

async function clampToMeshHeights(cartesians, heightOffsetM = 0.5) {
  const clamped = await viewer.scene.clampToHeightMostDetailed(cartesians);
  const out = new Array(clamped.length);
  for (let i = 0; i < clamped.length; i++) {
    const c = clamped[i];
    if (!c) {
      out[i] = null;
      continue;
    }
    const carto = Cesium.Cartographic.fromCartesian(c);
    carto.height = (carto.height || 0.0) + heightOffsetM;
    out[i] = Cesium.Cartesian3.fromRadians(carto.longitude, carto.latitude, carto.height);
  }
  return out;
}

// "Fabric" surface rendering for 3D mode:
// - Uses the same per-cell grid data (no forced circle, preserves missing cells).
// - Builds a polar mesh (rings x bearings) and drapes it onto the Google mesh using clampToHeightMostDetailed.
async function renderDrapedSurfaceCoverage(grid) {
  // Render coverage as a single textured ellipse (old approach), but rasterized from the
  // current per-cell polar grid output (preserves adaptive ray termination + blocking).
  //
  // This avoids the jagged topology that comes from clamping a triangulated surface where
  // adjacent vertices snap to very different mesh heights (roof edges / facades / streets).
  if (!grid || !Array.isArray(grid.cell_lat) || !Array.isArray(grid.cell_lon) || !Array.isArray(grid.rsrp_dbm)) return;
  if (!grid.tx || !Number.isFinite(grid.tx.lat) || !Number.isFinite(grid.tx.lon)) return;

  const lats = grid.cell_lat;
  const lons = grid.cell_lon;
  const rsrp = grid.rsrp_dbm;
  if (lats.length === 0 || lons.length !== lats.length || rsrp.length !== lats.length) return;

  // Polar discretization must match coverage_grid.py (dtheta=5°) and rf_params.step_m.
  const drM = Number(grid.rf_params?.step_m) || 5.0;
  const dthetaDeg = Number(grid.rf_params?.dtheta_deg) || 5.0;
  const nTheta = Math.max(1, Math.round(360.0 / dthetaDeg));

  let vmin = Infinity;
  let vmax = -Infinity;
  for (let i = 0; i < rsrp.length; i++) {
    const v = rsrp[i];
    if (!Number.isFinite(v)) continue;
    if (v < vmin) vmin = v;
    if (v > vmax) vmax = v;
  }
  if (!Number.isFinite(vmin) || !Number.isFinite(vmax)) return;

  // 3D mode uses the draped surface path (not point primitives), so the legend
  // must be enabled here as well.
  updateRSRPLegend(FIXED_RSRP_MIN, FIXED_RSRP_MAX, vmin, vmax);

  const txLat = grid.tx.lat;
  const txLon = grid.tx.lon;

  // Bin samples into (ring, theta) slots; also capture per-theta termination envelope.
  const slotToValue = new Map(); // key: `${ring}_${ti}` -> rsrp
  const maxRingByTheta = new Int32Array(nTheta);
  for (let i = 0; i < nTheta; i++) maxRingByTheta[i] = 0;

  let maxRangeM = 0.0;

  for (let i = 0; i < lats.length; i++) {
    const lat = lats[i];
    const lon = lons[i];
    const v = rsrp[i];
    if (!Number.isFinite(lat) || !Number.isFinite(lon) || !Number.isFinite(v)) continue;

    const rb = enuRangeBearing(txLat, txLon, lat, lon);
    if (!Number.isFinite(rb.range_m) || !Number.isFinite(rb.bearing_deg)) continue;
    if (rb.range_m > maxRangeM) maxRangeM = rb.range_m;

    const ring = Math.max(1, Math.round(rb.range_m / drM));
    const ti = ((Math.round(normalizeAngleDeg(rb.bearing_deg) / dthetaDeg) % nTheta) + nTheta) % nTheta;
    const key = `${ring}_${ti}`;

    const prev = slotToValue.get(key);
    if (prev == null || v > prev) slotToValue.set(key, v);
    if (ring > maxRingByTheta[ti]) maxRingByTheta[ti] = ring;
  }

  if (!Number.isFinite(maxRangeM) || maxRangeM <= 0.0) return;

  // Ellipse radius should cover all samples; the non-circular boundary comes from alpha masking.
  const radiusM = Math.max(drM, maxRangeM);

  // Texture resolution: tie to rings so it stays smooth as max range changes.
  const approxRings = Math.max(1, Math.round(radiusM / drM));
  let texSize = Math.round(approxRings * 4); // ~4 px per ring
  texSize = Math.max(512, Math.min(2048, texSize));

  const canvas = document.createElement("canvas");
  canvas.width = texSize;
  canvas.height = texSize;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  const img = ctx.createImageData(texSize, texSize);

  // Rasterize by sampling the polar bin at each pixel. Pixels with no sample (missing cell)
  // are fully transparent. Pixels beyond the per-theta envelope are fully transparent.
  //
  // Texture coordinate mapping: X = east, Y = north.
  for (let y = 0; y < texSize; y++) {
    const ny = (0.5 - y / Math.max(1, (texSize - 1))) * 2.0; // +north at top
    for (let x = 0; x < texSize; x++) {
      const ex = (x / Math.max(1, (texSize - 1)) - 0.5) * 2.0; // +east to the right
      const dxm = ex * radiusM;
      const dym = ny * radiusM;
      const r = Math.hypot(dxm, dym);
      const idx = (y * texSize + x) * 4;

      if (r > radiusM) {
        img.data[idx + 3] = 0;
        continue;
      }

      // Center pixel: show vmax under TX marker (avoids a tiny hole).
      if (r < drM * 0.5) {
        const c = colorForValue(vmax, vmin, vmax);
        img.data[idx + 0] = Math.round(255 * c.red);
        img.data[idx + 1] = Math.round(255 * c.green);
        img.data[idx + 2] = Math.round(255 * c.blue);
        img.data[idx + 3] = Math.round(255 * c.alpha);
        continue;
      }

      const bearingDeg = normalizeAngleDeg((Math.atan2(dxm, dym) * 180.0) / Math.PI);
      const ti = ((Math.round(bearingDeg / dthetaDeg) % nTheta) + nTheta) % nTheta;

      const maxRing = maxRingByTheta[ti];
      if (maxRing <= 0) {
        img.data[idx + 3] = 0;
        continue;
      }

      // Use a half-step margin so the alpha boundary matches the discrete ring termination.
      if (r > (maxRing + 0.5) * drM) {
        img.data[idx + 3] = 0;
        continue;
      }

      const ring = Math.max(1, Math.round(r / drM));
      const v = slotToValue.get(`${ring}_${ti}`);
      if (!Number.isFinite(v)) {
        img.data[idx + 3] = 0;
        continue;
      }

      const c = colorForValue(v, vmin, vmax);
      img.data[idx + 0] = Math.round(255 * c.red);
      img.data[idx + 1] = Math.round(255 * c.green);
      img.data[idx + 2] = Math.round(255 * c.blue);
      img.data[idx + 3] = Math.round(255 * c.alpha);
    }
  }

  ctx.putImageData(img, 0, 0);

  const ent = viewer.entities.add({
    position: Cesium.Cartesian3.fromDegrees(txLon, txLat),
    ellipse: {
      semiMajorAxis: radiusM,
      semiMinorAxis: radiusM,
      // Cesium will tessellate the ellipse; smaller granularity reduces visible faceting.
      granularity: Cesium.Math.toRadians(0.25),
      material: new Cesium.ImageMaterialProperty({ image: canvas, transparent: true }),
      heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
      outline: false,
    },
  });
  planEntities.push(ent);
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

    // Coverage/grid overrides (must stay in sync with 3D mesh-profile params)
    max_range_m: getNumber("max-range", 500.0),
    step_m: getNumber("dr-m", 5.0),
    dtheta_deg: getNumber("dtheta", 5.0),
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
    addPlannedTxMarker(out.snapped_tx.lat, out.snapped_tx.lon);
  } else {
    updateTxMarker(lat, lon);
    addPlannedTxMarker(lat, lon);
  }

  // Coverage rendering:
  // All 3D modes MUST use the same render strategy: a single clamped ellipse textured
  // with a backend-provided PNG (fast + visually consistent across OSM-only vs mesh).
  // If a PNG isn't available, fall back to legacy grid rendering.
  if (out.grid) {
    if (out.heatmap && out.heatmap.png_b64) {
      await renderHeatmapDrapeOsm3d(out.heatmap, out.grid);
    } else {
      // Debug/legacy fallback (should be rare after backend PNG changes).
      if (rayMode === "3d") {
        try {
          await renderDrapedSurfaceCoverage(out.grid);
        } catch (e) {
          console.warn("Surface drape failed, falling back to point grid:", e);
          renderGridCoverage(out.grid);
        }
      } else {
        renderGridCoverage(out.grid);
      }
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

  // Distance scale (2D mode has Leaflet scale; 3D mode needs its own).
  initDistanceScale();

  try {
    const tileset = await Cesium.createGooglePhotorealistic3DTileset();
    viewer.scene.primitives.add(tileset);
    if (tileset.readyPromise) await tileset.readyPromise;
  } catch (e) {
    setStatus(`Failed to load Google mesh tileset.\n\n${e}`);
    return;
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
