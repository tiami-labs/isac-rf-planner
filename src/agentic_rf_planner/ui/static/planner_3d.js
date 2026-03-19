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
let rxEntity = null;
// Planned overlays persist until the user presses "Clear Map".
// Keep the selection TX marker (txEntity) separate.
let planEntities = []; // Cesium.Entity[] (heatmaps, planned TX markers, etc.)
let planPrimitives = []; // Cesium.Primitive[] / collections
let rfEntities = []; // RF-only entities (heatmap drapes)
let rfPrimitives = []; // RF-only primitives (point heatmaps)
let planCounter = 0;

let sectorEntities = []; // visualization overlays (entities)
let planResults = []; // { lat, lon, out, cacheCenter } per successful plan (for export)

// Expose for F12 console debugging
window.RFPLANNER_DEBUG = {
  get planEntities() { return planEntities; },
  get rfEntities() { return rfEntities; },
  get sectorEntities() { return sectorEntities; },
  get planPrimitives() { return planPrimitives; },
  get rfPrimitives() { return rfPrimitives; },
};

let streetLabelEntities = []; // street-name labels
let streetLabelCache = new Map(); // rounded center/radius -> { center, radiusM, labels }
let streetLabelRefreshTimer = null;
let streetLabelRequestSeq = 0;
let currentTxLocation = null; // {lat, lon}
let currentRxLocation = null; // {lat, lon}
let sectorCounter = 0;
let osmHeatmapMeshCache = new Map(); // key -> { geometry }

// Fixed RSRP scale (used for legend labels / cross-plan comparability).
const FIXED_RSRP_MIN = -140.0;
const FIXED_RSRP_MAX = -60.0;
const STREET_LABEL_MAJOR_FAR_M = 32000.0;
const STREET_LABEL_MINOR_FAR_M = 14000.0;
const STREET_LABEL_RADIUS_MIN_M = 1500.0;
const STREET_LABEL_RADIUS_MAX_M = 5000.0;
const STREET_LABEL_FETCH_DEBOUNCE_MS = 350;
const STREET_LABEL_MAJOR_SCALE = new Cesium.NearFarScalar(800.0, 1.15, 24000.0, 0.7);
const STREET_LABEL_MINOR_SCALE = new Cesium.NearFarScalar(400.0, 0.95, 12000.0, 0.65);
const STREET_LABEL_MAJOR_ALPHA = new Cesium.NearFarScalar(800.0, 1.0, 26000.0, 0.35);
const STREET_LABEL_MINOR_ALPHA = new Cesium.NearFarScalar(400.0, 1.0, 14000.0, 0.25);
const MULTI_TX_PULL_DELAY_MS = 1500;
let isPlanningQueue = false;

// Debug overlay: multipath rays (direct + reflections)
let raytraceEntities = [];

function clearRaytraceOverlay() {
  if (!viewer) return;
  for (const e of raytraceEntities) {
    try { viewer.entities.remove(e); } catch { /* ignore */ }
  }
  raytraceEntities = [];
}

function clamp01(x) {
  return Math.max(0, Math.min(1, x));
}

function rsrpToAlpha(rsrpDbm) {
  // Map [-140, -70] -> [0.15, 0.9]
  const t = clamp01((rsrpDbm + 140) / 70);
  return 0.15 + 0.75 * t;
}

async function fetchRaytracePaths(txLat, txLon, rxLat, rxLon) {
  const sectors = collectSectorConfigs();
  const rtProfile = getRtProfileParams(txLat, txLon, rxLat, rxLon);
  const body = {
    tx_lat: txLat,
    tx_lon: txLon,
    rx_lat: rxLat,
    rx_lon: rxLon,
    ray_mode: "3d_rt",
    tx_height_m: getNumber("tx-height-m", 10.0),
    rx_height_m: getNumber("rx-height-m", 1.5),
    freq_mhz: getNumber("freq-mhz", 3500.0),
    tx_power_dbm: getNumber("tx-power-dbm", 43.0),
    noise_figure_db: getNumber("noise-figure-db", 7.0),
    channel_bandwidth_mhz: getNumber("bw-mhz", 40.0),
    num_resource_blocks: Math.round(getNumber("num-rb", 100)),
    mimo_mode: getString("mimo-mode", "MIMO"),
    electrical_tilt_deg: getNumber("electrical-tilt-deg", 0.0),
    mechanical_tilt_deg: getNumber("mechanical-tilt-deg", 0.0),
    vertical_beamwidth_deg: getNumber("vertical-beamwidth-deg", 8.0),
    max_vertical_attenuation_db: getNumber("max-vertical-atten-db", 30.0),
    max_horizontal_attenuation_db: getNumber("max-horizontal-atten-db", 30.0),
    front_to_back_attenuation_db: getNumber("front-to-back-atten-db", 25.0),
    path_loss_model: getString("path-loss-model", "3gpp_38901"),
    propagation_scenario: getString("propagation-scenario", "umi_street_canyon"),
    termination_rsrp_dbm: getNumber("termination-rsrp-dbm", -140.0),
    sectors: sectors.length ? sectors : null,
    max_bounces: 20,
    max_wall_candidates: 120,
    max_paths: 24,
    reflection_loss_db: 8.0,
    profile_max_range_m: rtProfile.maxRangeM,
    profile_dr_m: rtProfile.drM,
    profile_dtheta_deg: rtProfile.dthetaDeg,
  };

  const resp = await fetch("/api/raytrace_paths", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const t = await resp.text();
    throw new Error(`/api/raytrace_paths failed (${resp.status}): ${t}`);
  }
  return await resp.json();
}

async function isSegmentBlockedByGoogleMesh(p0, p1, sampleStepM = 2.0, clearanceM = 0.6) {
  if (!viewer || !viewer.scene) return false;

  const totalDistM = haversineDistanceM(p0.lat, p0.lon, p1.lat, p1.lon);
  if (!Number.isFinite(totalDistM) || totalDistM < 6.0) return false;

  const steps = Math.max(2, Math.ceil(totalDistM / sampleStepM));
  const probeHeightM = Math.max(Number(p0.h || 0.0), Number(p1.h || 0.0)) + 250.0;
  const sampleCartesians = [];
  const lineHeights = [];

  for (let i = 1; i < steps; i++) {
    const t = i / steps;
    const distFromEnds = Math.min(t, 1.0 - t) * totalDistM;
    if (distFromEnds < 2.5) continue;
    const lat = p0.lat + (p1.lat - p0.lat) * t;
    const lon = p0.lon + (p1.lon - p0.lon) * t;
    const lineH = Number(p0.h || 0.0) + (Number(p1.h || 0.0) - Number(p0.h || 0.0)) * t;
    sampleCartesians.push(Cesium.Cartesian3.fromDegrees(lon, lat, probeHeightM));
    lineHeights.push(lineH);
  }

  if (!sampleCartesians.length) return false;

  const clamped = await viewer.scene.clampToHeightMostDetailed(sampleCartesians);
  for (let i = 0; i < clamped.length; i++) {
    const c = clamped[i];
    if (!c) continue;
    const carto = Cesium.Cartographic.fromCartesian(c);
    const meshH = carto.height;
    if (Number.isFinite(meshH) && meshH > lineHeights[i] + clearanceM) {
      return true;
    }
  }
  return false;
}

async function filterRaytracePathsWithGoogleMesh(paths) {
  const valid = [];
  const rejected = [];

  for (const path of Array.isArray(paths) ? paths : []) {
    const pts = Array.isArray(path?.points) ? path.points : [];
    if (pts.length < 2) continue;

    let blocked = false;
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[i];
      const p1 = pts[i + 1];
      try {
        if (await isSegmentBlockedByGoogleMesh(p0, p1)) {
          blocked = true;
          break;
        }
      } catch (e) {
        console.warn("Google-mesh path validation failed on segment", i, e);
      }
    }

    if (blocked) {
      rejected.push(path);
    } else {
      valid.push(path);
    }
  }

  return { valid, rejected };
}

async function renderRaytraceOverlay(txLat, txLon, rxLat, rxLon) {
  if (!viewer) return { rawCount: 0, drawnCount: 0, filteredCount: 0, paths: [], json: null };
  clearRaytraceOverlay();

  const rayMode = getString("ray-mode", "3d").toLowerCase();
  const toggle = document.getElementById("show-raytrace-toggle");
  const enabled = !!(toggle && toggle.checked);
  if (!enabled || rayMode !== "3d_rt") return { rawCount: 0, drawnCount: 0, filteredCount: 0, paths: [], json: null };
  if (!Number.isFinite(rxLat) || !Number.isFinite(rxLon)) return { rawCount: 0, drawnCount: 0, filteredCount: 0, paths: [], json: null };

  let json;
  try {
    json = await fetchRaytracePaths(txLat, txLon, rxLat, rxLon);
  } catch (e) {
    console.warn("/api/raytrace_paths failed:", e);
    throw e;
  }

  const rawPaths = Array.isArray(json?.paths) ? json.paths : [];
  let filtered = { valid: rawPaths, rejected: [] };
  try {
    filtered = await filterRaytracePathsWithGoogleMesh(rawPaths);
  } catch (e) {
    console.warn("Google mesh validation failed; falling back to unfiltered paths", e);
  }

  const paths = filtered.valid;
  for (const p of paths) {
    const pts = Array.isArray(p?.points) ? p.points : [];
    if (pts.length < 2) continue;
    const flat = [];
    for (const q of pts) {
      const lat = Number(q?.lat);
      const lon = Number(q?.lon);
      const h = Number.isFinite(Number(q?.h)) ? Number(q.h) : 0.0;
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
      flat.push(lon, lat, h);
    }
    if (flat.length < 6) continue;

    const rsrp = Number(p?.rsrp_dbm);
    const alpha = rsrpToAlpha(Number.isFinite(rsrp) ? rsrp : -140.0);
    const kind = String(p?.kind || "direct");
    const color = kind === "direct"
      ? Cesium.Color.CYAN.withAlpha(alpha)
      : (kind === "reflect" ? Cesium.Color.YELLOW.withAlpha(alpha) : Cesium.Color.LIME.withAlpha(alpha));

    const entity = viewer.entities.add({
      polyline: {
        positions: Cesium.Cartesian3.fromDegreesArrayHeights(flat),
        width: kind === "direct" ? 2.0 : 2.5,
        material: color,
        clampToGround: false,
      },
    });
    raytraceEntities.push(entity);
  }

  const outJson = { ...(json || {}), paths };
  return {
    rawCount: rawPaths.length,
    drawnCount: paths.length,
    filteredCount: filtered.rejected.length,
    paths,
    json: outJson,
  };
}

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

function setInputValue(id, value) {
  const el = document.getElementById(id);
  if (el && value != null && value !== "") el.value = String(value);
}

async function loadRfParamsDefaults() {
  try {
    const resp = await fetch("/api/rf-params");
    if (!resp.ok) return;
    const cfg = await resp.json();
    if (!cfg) return;
    setInputValue("shadow-loss-cap-db", cfg.shadow_loss_cap_db);
    setInputValue("diffraction-loss-cap-db", cfg.diffraction_loss_cap_db);
    setInputValue("canyon-recovery-max-db", cfg.canyon_recovery_max_db);
    setInputValue("canyon-recovery-slope-db-per-100m", cfg.canyon_recovery_slope_db_per_100m);
    const bldg = cfg.building_attenuation;
    if (bldg) {
      const m = bldg.materials;
      if (m) {
        setInputValue("bldg-concrete", m.concrete);
        setInputValue("bldg-brick", m.brick);
        setInputValue("bldg-wood", m.wood);
        setInputValue("bldg-glass", m.glass);
        setInputValue("bldg-metal", m.metal);
        setInputValue("bldg-unknown", m.unknown);
      }
      const o = bldg.overall;
      if (o) {
        setInputValue("bldg-scale", o.scale);
        setInputValue("bldg-reduction-db", o.reduction_db);
      }
    }
  } catch (_) { /* keep HTML defaults */ }
}

function setInput(id, value) {
  const el = document.getElementById(id);
  if (el) el.value = String(value);
}

function sleep(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function setPlanButtonBusy(busy) {
  const btn = document.querySelector('#coord-form button[type="submit"]');
  if (!btn) return;
  btn.disabled = !!busy;
  btn.style.opacity = busy ? "0.7" : "1";
  btn.style.cursor = busy ? "wait" : "pointer";
}

function formatTxCoordinate(lat, lon) {
  return `${lat},${lon}`;
}

function parseTxInput() {
  const raw = String(document.getElementById("tx-input")?.value || "");
  const points = [];
  const errors = [];
  const lines = raw.split(/\r?\n/);

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line) continue;
    const parts = line.split(",");
    if (parts.length !== 2) {
      errors.push(`Line ${i + 1}: expected "lat,lon".`);
      continue;
    }
    const lat = Number.parseFloat(parts[0].trim());
    const lon = Number.parseFloat(parts[1].trim());
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
      errors.push(`Line ${i + 1}: invalid latitude/longitude.`);
      continue;
    }
    if (lat < -90 || lat > 90) {
      errors.push(`Line ${i + 1}: latitude must be between -90 and 90.`);
      continue;
    }
    if (lon < -180 || lon > 180) {
      errors.push(`Line ${i + 1}: longitude must be between -180 and 180.`);
      continue;
    }
    points.push({ lat, lon, lineNumber: i + 1 });
  }

  return { points, errors };
}

function updateTxInputSummary() {
  const el = document.getElementById("tx-input-summary");
  if (!el) return;
  const { points, errors } = parseTxInput();
  if (errors.length) {
    el.textContent = errors.slice(0, 2).join(" ");
    el.style.color = "#ff9b9b";
    return;
  }
  if (!points.length) {
    el.textContent = "No TX queued yet.";
    el.style.color = "#bbb";
    return;
  }
  el.textContent = points.length === 1
    ? "1 TX queued."
    : `${points.length} TX points queued. They will run one at a time.`;
  el.style.color = "#bbb";
}

function setTxInputPoints(points) {
  const el = document.getElementById("tx-input");
  if (!el) return;
  el.value = points.map((p) => formatTxCoordinate(p.lat, p.lon)).join("\n");
  updateTxInputSummary();
}

function appendTxInputPoint(lat, lon) {
  const parsed = parseTxInput();
  const points = parsed.points.slice();
  const exists = points.some((p) => Math.abs(p.lat - lat) < 1e-10 && Math.abs(p.lon - lon) < 1e-10);
  if (!exists) {
    points.push({ lat, lon });
    setTxInputPoints(points);
    return points.length;
  }
  updateTxInputSummary();
  return points.length;
}

function getQueuedTxPoints() {
  const parsed = parseTxInput();
  if (parsed.errors.length) {
    setStatus(parsed.errors.slice(0, 3).join("\n"));
    return null;
  }
  if (parsed.points.length) return parsed.points;
  if (currentTxLocation && Number.isFinite(currentTxLocation.lat) && Number.isFinite(currentTxLocation.lon)) {
    return [{ lat: currentTxLocation.lat, lon: currentTxLocation.lon, lineNumber: 1 }];
  }
  setStatus("Enter one or more TX coordinates or click on the mesh to add them.");
  return null;
}

function getLastQueuedTxPoint() {
  const parsed = parseTxInput();
  return parsed.points.length ? parsed.points[parsed.points.length - 1] : null;
}

async function fetchConfig() {
  const r = await fetch("/api/config");
  if (!r.ok) throw new Error(`GET /api/config failed (${r.status})`);
  return await r.json();
}

function trackRfEntity(ent) {
  rfEntities.push(ent);
  planEntities.push(ent);
  return ent;
}

function trackRfPrimitive(prim) {
  rfPrimitives.push(prim);
  planPrimitives.push(prim);
  return prim;
}

function isStreetLabelsEnabled() {
  return document.getElementById("show-street-labels-toggle")?.checked ?? true;
}

function clearStreetLabels() {
  for (const e of streetLabelEntities) {
    try { viewer.entities.remove(e); } catch {}
  }
  streetLabelEntities = [];
}

function setStreetLabelsVisible(show) {
  for (const e of streetLabelEntities) {
    try { e.show = !!show; } catch {}
  }
}

function setRfOverlayVisible(show) {
  for (const e of rfEntities) {
    try { e.show = !!show; } catch {}
  }
  for (const p of rfPrimitives) {
    try { p.show = !!show; } catch {}
  }
}

function getCameraHeightM() {
  try {
    return Number(viewer?.camera?.positionCartographic?.height) || 0.0;
  } catch {}
  return 0.0;
}

function getStreetLabelCenterLatLon() {
  if (currentTxLocation && Number.isFinite(currentTxLocation.lat) && Number.isFinite(currentTxLocation.lon)) {
    return currentTxLocation;
  }
  const queued = getLastQueuedTxPoint();
  if (queued) {
    return { lat: queued.lat, lon: queued.lon };
  }
  return null;
}

function getCurrentViewCenterLatLon() {
  if (!viewer) return null;
  try {
    const w = viewer.canvas.clientWidth || viewer.canvas.width;
    const h = viewer.canvas.clientHeight || viewer.canvas.height;
    if (w > 0 && h > 0) {
      const pos = _pickGlobePosition(new Cesium.Cartesian2(Math.round(w / 2), Math.round(h / 2)));
      if (pos) {
        const carto = Cesium.Cartographic.fromCartesian(pos);
        return {
          lat: Cesium.Math.toDegrees(carto.latitude),
          lon: Cesium.Math.toDegrees(carto.longitude),
        };
      }
    }
  } catch {}
  try {
    const carto = viewer.camera.positionCartographic;
    return {
      lat: Cesium.Math.toDegrees(carto.latitude),
      lon: Cesium.Math.toDegrees(carto.longitude),
    };
  } catch {}
  return null;
}

function getStreetLabelRadiusM() {
  const maxRangeM = getNumber("max-range", 2000.0);
  return Math.max(
    STREET_LABEL_RADIUS_MIN_M,
    Math.min(STREET_LABEL_RADIUS_MAX_M, maxRangeM)
  );
}

function buildStreetLabelCacheKey(lat, lon, radiusM) {
  const latKey = (Math.round(lat * 10000) / 10000).toFixed(4);
  const lonKey = (Math.round(lon * 10000) / 10000).toFixed(4);
  const radiusKey = String(Math.round(radiusM / 100) * 100);
  return `${latKey}:${lonKey}:${radiusKey}`;
}

function haversineDistanceM(lat1, lon1, lat2, lon2) {
  const r = 6371000.0;
  const p1 = Cesium.Math.toRadians(lat1);
  const p2 = Cesium.Math.toRadians(lat2);
  const dLat = Cesium.Math.toRadians(lat2 - lat1);
  const dLon = Cesium.Math.toRadians(lon2 - lon1);
  const a = Math.sin(dLat / 2) ** 2
    + Math.cos(p1) * Math.cos(p2) * Math.sin(dLon / 2) ** 2;
  return 2 * r * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function getRtProfileParams(txLat, txLon, rxLat, rxLon) {
  const distM = (Number.isFinite(txLat) && Number.isFinite(txLon) && Number.isFinite(rxLat) && Number.isFinite(rxLon))
    ? haversineDistanceM(txLat, txLon, rxLat, rxLon)
    : getNumber("max-range", 2000.0);
  const requestedRangeM = getNumber("max-range", 2000.0);
  return {
    maxRangeM: Math.max(600.0, Math.min(4000.0, Math.max(requestedRangeM, distM + 250.0))),
    drM: Math.min(getNumber("dr-m", 5.0), 2.0),
    dthetaDeg: Math.min(getNumber("dtheta", 5.0), 1.0),
  };
}

function flyToQueuedPoints(queue) {
  if (!viewer || !queue || queue.length === 0) return;
  const FLY_TO_NEAR_THRESHOLD_M = 50000; // 50km - if points farther apart, center on first only
  let maxDist = 0;
  for (let i = 0; i < queue.length; i++) {
    for (let j = i + 1; j < queue.length; j++) {
      const d = haversineDistanceM(queue[i].lat, queue[i].lon, queue[j].lat, queue[j].lon);
      if (d > maxDist) maxDist = d;
    }
  }
  const p = queue[0];
  const heightM = getViewModeHeightM("perspective");
  if (queue.length === 1 || maxDist > FLY_TO_NEAR_THRESHOLD_M) {
    const destination = Cesium.Cartesian3.fromDegrees(p.lon, p.lat, heightM);
    viewer.camera.flyTo({
      destination,
      orientation: {
        heading: Cesium.Math.toRadians(0.0),
        pitch: Cesium.Math.toRadians(-45.0),
        roll: 0.0,
      },
      duration: 0.9,
    });
  } else {
    const west = Math.min(...queue.map(q => q.lon));
    const south = Math.min(...queue.map(q => q.lat));
    const east = Math.max(...queue.map(q => q.lon));
    const north = Math.max(...queue.map(q => q.lat));
    const paddingDeg = 0.002;
    const rectangle = Cesium.Rectangle.fromDegrees(
      west - paddingDeg, south - paddingDeg, east + paddingDeg, north + paddingDeg
    );
    let destination;
    if (typeof viewer.camera.getRectangleCameraCoordinates === "function") {
      destination = viewer.camera.getRectangleCameraCoordinates(rectangle);
      viewer.camera.flyTo({ destination, convert: false, duration: 0.9 });
    } else {
      destination = Cesium.Cartesian3.fromDegrees((west + east) / 2, (south + north) / 2, heightM);
      viewer.camera.flyTo({
        destination,
        orientation: {
          heading: Cesium.Math.toRadians(0.0),
          pitch: Cesium.Math.toRadians(-45.0),
          roll: 0.0,
        },
        duration: 0.9,
      });
    }
  }
}

function findNearbyStreetLabelCacheEntry(center, radiusM) {
  let best = null;
  for (const entry of streetLabelCache.values()) {
    if (!entry || !entry.center || !Array.isArray(entry.labels)) continue;
    const distM = haversineDistanceM(
      center.lat,
      center.lon,
      entry.center.lat,
      entry.center.lon
    );
    const reuseThresholdM = Math.max(radiusM, entry.radiusM || 0.0);
    if (distM > reuseThresholdM) continue;
    if (!best || distM < best.distM) {
      best = { entry, distM };
    }
  }
  return best ? best.entry : null;
}

function renderStreetLabels(labels) {
  clearStreetLabels();
  if (!viewer || !Array.isArray(labels) || !labels.length) return;

  for (const item of labels) {
    const lat = Number(item.anchor_lat);
    const lon = Number(item.anchor_lon);
    const name = String(item.name || "").trim();
    const importance = String(item.importance || "minor");
    if (!Number.isFinite(lat) || !Number.isFinite(lon) || !name) continue;

    const isMajor = importance === "major";
    const far = isMajor ? STREET_LABEL_MAJOR_FAR_M : STREET_LABEL_MINOR_FAR_M;
    const ent = viewer.entities.add({
      position: Cesium.Cartesian3.fromDegrees(lon, lat, 8.0),
      label: {
        text: name,
        font: isMajor ? "600 15px sans-serif" : "12px sans-serif",
        fillColor: Cesium.Color.WHITE,
        outlineColor: Cesium.Color.BLACK.withAlpha(0.95),
        outlineWidth: 3,
        style: Cesium.LabelStyle.FILL_AND_OUTLINE,
        showBackground: true,
        backgroundColor: Cesium.Color.BLACK.withAlpha(isMajor ? 0.28 : 0.22),
        backgroundPadding: new Cesium.Cartesian2(5, 3),
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
        distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0.0, far),
        scaleByDistance: isMajor ? STREET_LABEL_MAJOR_SCALE : STREET_LABEL_MINOR_SCALE,
        translucencyByDistance: isMajor ? STREET_LABEL_MAJOR_ALPHA : STREET_LABEL_MINOR_ALPHA,
        pixelOffset: new Cesium.Cartesian2(0, -5),
      },
    });
    streetLabelEntities.push(ent);
  }
  setStreetLabelsVisible(isStreetLabelsEnabled());
}

async function refreshStreetLabels(force = false) {
  if (!viewer) return;
  if (!isStreetLabelsEnabled()) {
    setStreetLabelsVisible(false);
    return;
  }

  const center = getStreetLabelCenterLatLon();
  if (!center || !Number.isFinite(center.lat) || !Number.isFinite(center.lon)) return;
  const radiusM = getStreetLabelRadiusM();
  const cacheKey = buildStreetLabelCacheKey(center.lat, center.lon, radiusM);

  if (!force && streetLabelCache.has(cacheKey)) {
    renderStreetLabels(streetLabelCache.get(cacheKey).labels);
    return;
  }

  if (!force) {
    const nearbyEntry = findNearbyStreetLabelCacheEntry(center, radiusM);
    if (nearbyEntry) {
      renderStreetLabels(nearbyEntry.labels);
      streetLabelCache.set(cacheKey, {
        center: { lat: center.lat, lon: center.lon },
        radiusM,
        labels: nearbyEntry.labels,
      });
      if (streetLabelCache.size > 24) {
        const oldestKey = streetLabelCache.keys().next().value;
        if (oldestKey) streetLabelCache.delete(oldestKey);
      }
      console.debug("Street labels cache hit (nearby TX reuse).");
      return;
    }
  }

  console.debug("Street labels cache miss; fetching labels.");
  const reqSeq = ++streetLabelRequestSeq;
  const url = `/api/roads/labels?lat=${encodeURIComponent(center.lat)}`
    + `&lon=${encodeURIComponent(center.lon)}`
    + `&radius_m=${encodeURIComponent(radiusM)}`
    + `&major_limit=60&minor_limit=160`;

  try {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`GET ${url} failed (${resp.status})`);
    const payload = await resp.json();
    if (reqSeq !== streetLabelRequestSeq) return;
    const labels = Array.isArray(payload?.labels) ? payload.labels : [];
    streetLabelCache.set(cacheKey, {
      center: { lat: center.lat, lon: center.lon },
      radiusM,
      labels,
    });
    if (streetLabelCache.size > 24) {
      const oldestKey = streetLabelCache.keys().next().value;
      if (oldestKey) streetLabelCache.delete(oldestKey);
    }
    renderStreetLabels(labels);
  } catch (err) {
    console.warn("Street label fetch failed:", err);
    if (reqSeq === streetLabelRequestSeq) clearStreetLabels();
  }
}

function queueStreetLabelRefresh(force = false) {
  if (streetLabelRefreshTimer) {
    window.clearTimeout(streetLabelRefreshTimer);
    streetLabelRefreshTimer = null;
  }
  streetLabelRefreshTimer = window.setTimeout(() => {
    streetLabelRefreshTimer = null;
    refreshStreetLabels(force);
  }, STREET_LABEL_FETCH_DEBOUNCE_MS);
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

function updateRxMarker(lat, lon) {
  const pos = Cesium.Cartesian3.fromDegrees(lon, lat, 20.0);
  if (!rxEntity) {
    rxEntity = viewer.entities.add({
      position: pos,
      point: {
        pixelSize: 10,
        color: Cesium.Color.CYAN.withAlpha(0.95),
        outlineColor: Cesium.Color.BLACK.withAlpha(0.8),
        outlineWidth: 2,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
      label: {
        text: "RX",
        font: "14px sans-serif",
        fillColor: Cesium.Color.CYAN,
        outlineColor: Cesium.Color.BLACK,
        outlineWidth: 2,
        style: Cesium.LabelStyle.FILL_AND_OUTLINE,
        pixelOffset: new Cesium.Cartesian2(0, -24),
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
    });
  } else {
    rxEntity.position = pos;
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
  rfEntities = [];

  for (const p of planPrimitives) {
    try { viewer.scene.primitives.remove(p); } catch {}
  }
  planPrimitives = [];
  rfPrimitives = [];
  for (const e of sectorEntities) {
    try { viewer.entities.remove(e); } catch {}
  }
  sectorEntities = [];
  planResults = [];

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
  const points = trackRfPrimitive(viewer.scene.primitives.add(new Cesium.PointPrimitiveCollection()));
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
    getNumber("max-range", 2000.0);

  if (!Number.isFinite(radiusM) || radiusM <= 0) return;

  // Legend: use the fixed scale for consistency, but show actual min/max from the grid if provided.
  const actualMin = (heatmap && Number.isFinite(heatmap.actual_min)) ? Number(heatmap.actual_min) :
    ((heatmap && Number.isFinite(heatmap.vmin)) ? Number(heatmap.vmin) : FIXED_RSRP_MIN);
  const actualMax = (heatmap && Number.isFinite(heatmap.actual_max)) ? Number(heatmap.actual_max) :
    ((heatmap && Number.isFinite(heatmap.vmax)) ? Number(heatmap.vmax) : FIXED_RSRP_MAX);
  updateRSRPLegend(FIXED_RSRP_MIN, FIXED_RSRP_MAX, actualMin, actualMax);

  const imgSrc = heatmap && heatmap.png_b64 ? heatmap.png_b64 : null;

  if (!imgSrc) {
    // Fallback: if backend didn't provide a PNG, fall back to point grid (if available).
    if (grid && Array.isArray(grid.cell_lat) && grid.cell_lat.length) {
      renderGridCoverage(grid);
    }
    return;
  }

  const ent = trackRfEntity(viewer.entities.add({
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
  }));
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

// Legacy 3D renderer that projects the raw polar grid into a canvas on the client.
// We keep it as a fallback/debug path, but the main 3D renderer now uses the same
// backend-generated PNG ellipse drape as 3D OSM-only mode for visual consistency.
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

  const ent = trackRfEntity(viewer.entities.add({
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
  }));
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
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:6px; font-size:10px; margin-top:6px;">
      <div>
        <label style="display:block; font-size:10px; margin-bottom:2px; color:#ccc;">Azimuth (deg, optional):</label>
        <input type="number" class="sector-azimuth" step="0.1" min="0" max="360" placeholder="auto"
          style="width:100%; padding:4px; box-sizing:border-box; background:#333; color:#eee; border:1px solid #555; border-radius:2px; font-size:11px;" />
      </div>
      <div>
        <label style="display:block; font-size:10px; margin-bottom:2px; color:#ccc;">Horiz BW (deg, optional):</label>
        <input type="number" class="sector-beamwidth-h" step="0.1" min="1" max="360" placeholder="auto"
          style="width:100%; padding:4px; box-sizing:border-box; background:#333; color:#eee; border:1px solid #555; border-radius:2px; font-size:11px;" />
      </div>
      <div>
        <label style="display:block; font-size:10px; margin-bottom:2px; color:#ccc;">Vert BW (deg)</label>
        <input type="number" class="sector-beamwidth-v" step="0.1" min="0.1" max="180" value="8.0"
          style="width:100%; padding:4px; box-sizing:border-box; background:#333; color:#eee; border:1px solid #555; border-radius:2px; font-size:11px;" />
      </div>
      <div>
        <label style="display:block; font-size:10px; margin-bottom:2px; color:#ccc;">Elec tilt (deg)</label>
        <input type="number" class="sector-electrical-tilt" step="0.1" value="0.0"
          style="width:100%; padding:4px; box-sizing:border-box; background:#333; color:#eee; border:1px solid #555; border-radius:2px; font-size:11px;" />
      </div>
      <div>
        <label style="display:block; font-size:10px; margin-bottom:2px; color:#ccc;">Mech tilt (deg)</label>
        <input type="number" class="sector-mechanical-tilt" step="0.1" value="0.0"
          style="width:100%; padding:4px; box-sizing:border-box; background:#333; color:#eee; border:1px solid #555; border-radius:2px; font-size:11px;" />
      </div>
      <div>
        <label style="display:block; font-size:10px; margin-bottom:2px; color:#ccc;">Max horiz atten. (dB)</label>
        <input type="number" class="sector-max-horizontal-atten" step="0.1" value="30.0"
          style="width:100%; padding:4px; box-sizing:border-box; background:#333; color:#eee; border:1px solid #555; border-radius:2px; font-size:11px;" />
      </div>
      <div>
        <label style="display:block; font-size:10px; margin-bottom:2px; color:#ccc;">Front-to-back (dB)</label>
        <input type="number" class="sector-front-to-back-atten" step="0.1" value="25.0"
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
    const azimuth = parseFloat(div.querySelector(".sector-azimuth").value);
    const beamwidthH = parseFloat(div.querySelector(".sector-beamwidth-h").value);
    const beamwidthV = parseFloat(div.querySelector(".sector-beamwidth-v").value);
    const electricalTilt = parseFloat(div.querySelector(".sector-electrical-tilt").value);
    const mechanicalTilt = parseFloat(div.querySelector(".sector-mechanical-tilt").value);
    const maxHorizontalAtten = parseFloat(div.querySelector(".sector-max-horizontal-atten").value);
    const frontToBackAtten = parseFloat(div.querySelector(".sector-front-to-back-atten").value);
    if (Number.isFinite(azimuth)) sectorConfig.azimuth_deg = azimuth;
    if (Number.isFinite(beamwidthH)) sectorConfig.beamwidth_h_deg = beamwidthH;
    if (Number.isFinite(beamwidthV)) sectorConfig.beamwidth_v_deg = beamwidthV;
    if (Number.isFinite(electricalTilt)) sectorConfig.electrical_tilt_deg = electricalTilt;
    if (Number.isFinite(mechanicalTilt)) sectorConfig.mechanical_tilt_deg = mechanicalTilt;
    if (Number.isFinite(maxHorizontalAtten)) sectorConfig.max_horizontal_attenuation_db = maxHorizontalAtten;
    if (Number.isFinite(frontToBackAtten)) sectorConfig.front_to_back_attenuation_db = frontToBackAtten;

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
          sectorConfig.polygon_points = polygonPoints.map((p) => Array.isArray(p) ? p : [p.lat, p.lon]);
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
  setStatus("Polygon sector saved. Click Plan RF Queue.");
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

function getPreferredViewCenter() {
  return getCurrentViewCenterLatLon()
    || currentTxLocation
    || getLastQueuedTxPoint()
    || { lat: 37.7749, lon: -122.4194 };
}

function getViewModeHeightM(mode) {
  const radiusM = getNumber("max-range", 2000.0);
  const baseHeight = mode === "birdseye" ? radiusM * 2.2 : radiusM * 1.4;
  return Math.max(mode === "birdseye" ? 1400.0 : 1000.0, baseHeight);
}

function applyViewMode(mode, { animate = true } = {}) {
  if (!viewer) return;
  const center = getPreferredViewCenter();
  if (!center || !Number.isFinite(center.lat) || !Number.isFinite(center.lon)) return;

  const pitchDeg = mode === "birdseye" ? -89.0 : -45.0;
  const orientation = {
    heading: Number.isFinite(viewer.camera.heading) ? viewer.camera.heading : Cesium.Math.toRadians(0.0),
    pitch: Cesium.Math.toRadians(pitchDeg),
    roll: 0.0,
  };
  const destination = Cesium.Cartesian3.fromDegrees(center.lon, center.lat, getViewModeHeightM(mode));

  if (animate) {
    viewer.camera.flyTo({ destination, orientation, duration: 0.9 });
  } else {
    viewer.camera.setView({ destination, orientation });
  }
}

function getAttributionText() {
  const candidates = [
    viewer?.bottomContainer,
    document.querySelector(".cesium-viewer-bottom"),
    document.querySelector(".cesium-credit-textContainer"),
  ];
  for (const node of candidates) {
    const text = String(node?.innerText || "").replace(/\s+/g, " ").trim();
    if (text) return text;
  }
  return "Google Maps and Cesium attribution required.";
}

function wrapCanvasText(ctx, text, x, y, maxWidth, lineHeight) {
  const words = String(text || "").split(/\s+/).filter(Boolean);
  let line = "";
  let lineCount = 0;
  for (const word of words) {
    const testLine = line ? `${line} ${word}` : word;
    if (ctx.measureText(testLine).width > maxWidth && line) {
      ctx.fillText(line, x, y + lineCount * lineHeight);
      line = word;
      lineCount += 1;
    } else {
      line = testLine;
    }
  }
  if (line) {
    ctx.fillText(line, x, y + lineCount * lineHeight);
    lineCount += 1;
  }
  return lineCount;
}

function waitForNextFrame() {
  return new Promise((resolve) => window.requestAnimationFrame(() => resolve()));
}

function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = src;
  });
}

function restoreRfVisibility(entityStates, primitiveStates) {
  for (let i = 0; i < rfEntities.length; i++) {
    if (entityStates[i] == null) continue;
    try { rfEntities[i].show = entityStates[i]; } catch {}
  }
  for (let i = 0; i < rfPrimitives.length; i++) {
    if (primitiveStates[i] == null) continue;
    try { rfPrimitives[i].show = primitiveStates[i]; } catch {}
  }
}

function collectRoadNamesFromCache() {
  const roadNames = [];
  const seen = new Set();
  for (const entry of streetLabelCache.values()) {
    if (!entry || !Array.isArray(entry.labels)) continue;
    for (const item of entry.labels) {
      const name = String(item.name || "").trim();
      const lat = Number(item.anchor_lat);
      const lon = Number(item.anchor_lon);
      const importance = String(item.importance || "minor");
      if (!name || !Number.isFinite(lat) || !Number.isFinite(lon)) continue;
      const key = `${name}|${lat}|${lon}`;
      if (seen.has(key)) continue;
      seen.add(key);
      roadNames.push({ name, lat, lon, importance });
    }
  }
  return roadNames;
}

function getViewState() {
  if (!viewer || !viewer.camera) return {};
  try {
    const pos = viewer.camera.positionCartographic;
    if (!pos) return {};
    return {
      center_lat: Cesium.Math.toDegrees(pos.latitude),
      center_lon: Cesium.Math.toDegrees(pos.longitude),
      height_m: pos.height,
      heading_deg: Cesium.Math.toDegrees(viewer.camera.heading),
      pitch_deg: Cesium.Math.toDegrees(viewer.camera.pitch),
    };
  } catch {
    return {};
  }
}

async function captureViewBlob(includeRf) {
  const entityStates = rfEntities.map((e) => (e?.show ?? true));
  const primitiveStates = rfPrimitives.map((p) => (p?.show ?? true));
  try {
    if (!includeRf) setRfOverlayVisible(false);
    viewer.scene.requestRender();
    await waitForNextFrame();
    await waitForNextFrame();
    const sceneUrl = viewer.scene.canvas.toDataURL("image/png");
    const sceneImage = await loadImage(sceneUrl);
    const footerHeight = 54;
    const out = document.createElement("canvas");
    out.width = sceneImage.width;
    out.height = sceneImage.height + footerHeight;
    const ctx = out.getContext("2d");
    ctx.drawImage(sceneImage, 0, 0);
    ctx.fillStyle = "rgba(0, 0, 0, 0.92)";
    ctx.fillRect(0, sceneImage.height, out.width, footerHeight);
    ctx.fillStyle = "#ffffff";
    ctx.font = "bold 14px sans-serif";
    ctx.fillText(
      includeRf ? "3D RF Planner export: with RF overlay" : "3D RF Planner export: without RF overlay",
      12,
      sceneImage.height + 18
    );
    ctx.font = "11px sans-serif";
    wrapCanvasText(ctx, getAttributionText(), 12, sceneImage.height + 36, out.width - 24, 13);
    return await new Promise((res, rej) => {
      out.toBlob((b) => (b ? res(b) : rej(new Error("toBlob failed"))), "image/png");
    });
  } finally {
    restoreRfVisibility(entityStates, primitiveStates);
    viewer.scene.requestRender();
  }
}

async function exportCurrentView() {
  if (!viewer) return;

  try {
    setStatus("Exporting (capturing with RF)...");
    const fullViewWithRf = await captureViewBlob(true);
    setStatus("Exporting (capturing without RF)...");
    const fullViewWithoutRf = await captureViewBlob(false);

    const heatmapBlobs = [];
    if (window.RFExportUtils && planResults.length) {
      for (const pr of planResults) {
        const pngB64 = pr.out?.heatmap?.png_b64;
        if (pngB64) {
          const blob = window.RFExportUtils.base64DataUrlToBlob(pngB64);
          heatmapBlobs.push(blob);
        } else {
          heatmapBlobs.push(null);
        }
      }
    }

    const roadNames = collectRoadNamesFromCache();
    const metadata = window.RFExportUtils
      ? window.RFExportUtils.buildExportMetadata(planResults, {
          plannerVersion: "3d",
          viewState: getViewState(),
          roadNames,
        })
      : { plans: [], road_names: roadNames };

    const ts = new Date();
    const filename = `rf_planner_export_${ts.getFullYear()}-${String(ts.getMonth() + 1).padStart(2, "0")}-${String(ts.getDate()).padStart(2, "0")}_${String(ts.getHours()).padStart(2, "0")}${String(ts.getMinutes()).padStart(2, "0")}.zip`;

    const extraBlobs = { "full_view_without_rf.png": fullViewWithoutRf };

    if (window.RFExportUtils && typeof JSZip !== "undefined") {
      await window.RFExportUtils.createExportZip(fullViewWithRf, heatmapBlobs, metadata, filename, extraBlobs);
      setStatus("Exported ZIP with full views (with/without RF), heatmap overlays, and metadata.");
    } else {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(fullViewWithRf);
      a.download = "rf_planner_3d_with_rf.png";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(a.href);
      setStatus("Exported 3D map with RF overlay.");
    }
  } catch (err) {
    console.error("3D export failed:", err);
    setStatus(`Export failed: ${err}`);
  }
}

function clearMap() {
  clearOverlay();
  clearRaytraceOverlay();
  if (rxEntity) {
    try { viewer.entities.remove(rxEntity); } catch {}
    rxEntity = null;
  }
  currentRxLocation = null;
  // Keep TX marker but clear heatmap + sector overlays
  setMeshStatus("");
  setStatus("Cleared overlays.");
}

function drawSectorOverlays(sectors, txLat, txLon, radiusM) {
  const show = document.getElementById("show-sectors-toggle")?.checked ?? true;
  if (!show) return;

  // Clear only sector overlays (white semi-transparent circles) before drawing new ones.
  // Keeps heatmaps from previous plans in the batch; prevents sector overlay stacking.
  for (const e of sectorEntities) {
    try { viewer.entities.remove(e); } catch {}
  }
  sectorEntities = [];

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
        ...s.polygon_points.map((p) => Array.isArray(p)
          ? Cesium.Cartesian3.fromDegrees(p[1], p[0], 5.0)
          : Cesium.Cartesian3.fromDegrees(p.lon, p.lat, 5.0)),
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

async function ensureProfiles(txLat, txLon, overrides = {}) {
  const txHeightM = getNumber("tx-height-m", 10.0);
  const rxHeightM = getNumber("rx-height-m", 1.5);
  const maxRangeM = Number.isFinite(Number(overrides.maxRangeM)) ? Number(overrides.maxRangeM) : getNumber("max-range", 2000.0);
  const drM = Number.isFinite(Number(overrides.drM)) ? Number(overrides.drM) : getNumber("dr-m", 5.0);
  const dthetaDeg = Number.isFinite(Number(overrides.dthetaDeg)) ? Number(overrides.dthetaDeg) : getNumber("dtheta", 5.0);

  const hasUrl = `/api/mesh-profiles/has?tx_lat=${encodeURIComponent(txLat)}&tx_lon=${encodeURIComponent(txLon)}`
    + `&tx_height_m=${encodeURIComponent(txHeightM)}&rx_height_m=${encodeURIComponent(rxHeightM)}`
    + `&max_range_m=${encodeURIComponent(maxRangeM)}&dr_m=${encodeURIComponent(drM)}&dtheta_deg=${encodeURIComponent(dthetaDeg)}`;

  const hasResp = await fetch(hasUrl);
  if (!hasResp.ok) throw new Error(`GET ${hasUrl} failed (${hasResp.status})`);
  const hasJson = await hasResp.json();
  if (hasJson && hasJson.exists) {
    setMeshStatus(`3D profiles cached.
key=${hasJson.key}`);
    return { exists: true, key: hasJson.key, maxRangeM, drM, dthetaDeg };
  }

  setMeshStatus(`3D profiles missing. Generating + uploading…
range=${Math.round(maxRangeM)}m dr=${drM}m dθ=${dthetaDeg}°`);

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
  return { exists: true, key: null, maxRangeM, drM, dthetaDeg };
}

function buildPlanRequestBody(lat, lon, rayMode, txHeightM, rxHeightM, sectors) {
  return {
    lat,
    lon,
    freq_mhz: getNumber("freq-mhz", 3500.0),
    tx_power_dbm: getNumber("tx-power-dbm", 43.0),
    noise_figure_db: getNumber("noise-figure-db", 7.0),
    subcarrier_spacing_khz: getNumber("scs-khz", 30.0),
    num_resource_blocks: Math.round(getNumber("num-rb", 100)),
    channel_bandwidth_mhz: getNumber("bw-mhz", 40.0),
    mimo_mode: getString("mimo-mode", "MIMO"),
    enable_link_adaptation: getString("link-adapt", "1") === "1",
    fixed_modulation: null,
    electrical_tilt_deg: getNumber("electrical-tilt-deg", 0.0),
    mechanical_tilt_deg: getNumber("mechanical-tilt-deg", 0.0),
    vertical_beamwidth_deg: getNumber("vertical-beamwidth-deg", 8.0),
    max_vertical_attenuation_db: getNumber("max-vertical-atten-db", 30.0),
    path_loss_model: getString("path-loss-model", "3gpp_38901"),
    propagation_scenario: getString("propagation-scenario", "umi_street_canyon"),
    max_horizontal_attenuation_db: getNumber("max-horizontal-atten-db", 30.0),
    front_to_back_attenuation_db: getNumber("front-to-back-atten-db", 25.0),
    shadow_loss_db: getNumber("shadow-loss-db", 6.0),
    shadow_decay_db_per_100m: getNumber("shadow-slope-db-per-100m", 4.0),
    diffraction_base_loss_db: getNumber("diffraction-base-loss-db", 6.0),
    diffraction_slope_db_per_100m: getNumber("diffraction-slope-db-per-100m", 3.0),
    canyon_recovery_max_db: getNumber("canyon-recovery-max-db", 8.0),
    canyon_recovery_slope_db_per_100m: getNumber("canyon-recovery-slope-db-per-100m", 6.0),
    termination_rsrp_dbm: getNumber("termination-rsrp-dbm", -140.0),
    shadow_loss_cap_db: getNumber("shadow-loss-cap-db", 20.0),
    diffraction_loss_cap_db: getNumber("diffraction-loss-cap-db", 16.0),
    building_attenuation: {
      materials: {
        concrete: getNumber("bldg-concrete", 5.0),
        brick: getNumber("bldg-brick", 3.5),
        wood: getNumber("bldg-wood", 1.25),
        glass: getNumber("bldg-glass", 2.75),
        metal: getNumber("bldg-metal", 14.75),
        unknown: getNumber("bldg-unknown", 5.0),
      },
      overall: {
        scale: getNumber("bldg-scale", 0.75),
        reduction_db: getNumber("bldg-reduction-db", 6.0),
      },
    },
    sectors: sectors.length ? sectors : null,
    ray_mode: rayMode,
    tx_height_m: txHeightM,
    rx_height_m: rxHeightM,

    // Coverage/grid overrides (must stay in sync with 3D mesh-profile params)
    max_range_m: getNumber("max-range", 2000.0),
    step_m: getNumber("dr-m", 5.0),
    dtheta_deg: getNumber("dtheta", 5.0),
  };
}

async function runRaytracePlanForTx(lat, lon, {
  queueIndex = 1,
  total = 1,
  attempt = 1,
  refreshStreetLabelsOnSuccess = true,
} = {}) {
  const prefix = total > 1 ? `TX ${queueIndex}/${total}` : "TX";
  const attemptText = attempt > 1 ? ` (retry ${attempt - 1})` : "";

  if (!currentRxLocation) {
    const error = "3D RT mode requires an RX. Hold SHIFT and click the Google mesh to set RX first.";
    setStatus(`${prefix}${attemptText}: ${error}`);
    return { ok: false, error, cacheCenter: { lat, lon } };
  }

  currentTxLocation = { lat, lon };
  updateTxMarker(lat, lon);
  document.getElementById("show-raytrace-toggle").checked = true;

  const rtProfile = getRtProfileParams(lat, lon, currentRxLocation.lat, currentRxLocation.lon);
  try {
    setStatus(`${prefix}${attemptText}: preparing 3D RT profiles…`);
    await ensureProfiles(lat, lon, rtProfile);
  } catch (e) {
    const error = `failed to build RT mesh profiles: ${e}`;
    setStatus(`${prefix}${attemptText}: ${error}`);
    return { ok: false, error, cacheCenter: { lat, lon } };
  }

  clearOverlay();
  clearRaytraceOverlay();
  updateTxMarker(lat, lon);
  updateRxMarker(currentRxLocation.lat, currentRxLocation.lon);

  let summary;
  try {
    setStatus(`${prefix}${attemptText}: tracing rays…`);
    summary = await renderRaytraceOverlay(lat, lon, currentRxLocation.lat, currentRxLocation.lon);
  } catch (e) {
    const error = `3D RT failed: ${e}`;
    setStatus(`${prefix}${attemptText}: ${error}`);
    return { ok: false, error, cacheCenter: { lat, lon } };
  }

  addPlannedTxMarker(lat, lon);
  if (refreshStreetLabelsOnSuccess) queueStreetLabelRefresh(true);

  const best = Array.isArray(summary.paths) && summary.paths.length ? summary.paths[0] : null;
  const bestText = best
    ? ` Best=${String(best.kind || "path")} ${Number(best.rsrp_dbm).toFixed(1)} dBm${best.sector_id ? ` via ${best.sector_id}` : ""}.`
    : " No valid path survived Google-mesh validation.";
  setStatus(`${prefix}${attemptText}: RT complete. ${summary.drawnCount}/${summary.rawCount} paths kept after Google-mesh validation; ${summary.filteredCount} rejected.${bestText}`);

  return {
    ok: true,
    out: {
      mode: "3d_rt",
      raytrace: summary.json,
      raw_path_count: summary.rawCount,
      drawn_path_count: summary.drawnCount,
      rejected_path_count: summary.filteredCount,
      profile_resolution: rtProfile,
    },
    cacheCenter: { lat, lon },
  };
}

async function runPlanForTx(lat, lon, {
  queueIndex = 1,
  total = 1,
  attempt = 1,
  refreshStreetLabelsOnSuccess = true,
} = {}) {
  const rayMode = getString("ray-mode", "3d").toLowerCase();
  const txHeightM = getNumber("tx-height-m", 10.0);
  const rxHeightM = getNumber("rx-height-m", 1.5);
  const sectors = collectSectorConfigs();
  const prefix = total > 1 ? `TX ${queueIndex}/${total}` : "TX";
  const attemptText = attempt > 1 ? ` (retry ${attempt - 1})` : "";

  currentTxLocation = { lat, lon };
  updateTxMarker(lat, lon);

  if (rayMode === "3d_rt") {
    return await runRaytracePlanForTx(lat, lon, {
      queueIndex,
      total,
      attempt,
      refreshStreetLabelsOnSuccess,
    });
  }

  if (rayMode === "3d") {
    try {
      setStatus(`${prefix}${attemptText}: checking cached 3D ray profiles…`);
      await ensureProfiles(lat, lon);
    } catch (e) {
      setStatus(`${prefix}${attemptText}: failed to build mesh profiles.\n\n${e}`);
      return { ok: false, error: String(e), cacheCenter: { lat, lon } };
    }
  }

  setStatus(`${prefix}${attemptText}: running RF planning…`);

  const body = buildPlanRequestBody(lat, lon, rayMode, txHeightM, rxHeightM, sectors);

  let resp;
  try {
    resp = await fetch("/api/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    const error = `/api/plan request failed: ${e}`;
    setStatus(`${prefix}${attemptText}: ${error}`);
    return { ok: false, error, cacheCenter: { lat, lon } };
  }

  const text = await resp.text();
  if (!resp.ok) {
    try {
      const j = JSON.parse(text);
      if (resp.status === 409 && j && j.status === "missing_mesh_profiles") {
        setStatus(`${prefix}${attemptText}: missing profiles on server. Retrying after generating…`);
        await ensureProfiles(lat, lon);
        return await runPlanForTx(lat, lon, {
          queueIndex,
          total,
          attempt: attempt + 1,
          refreshStreetLabelsOnSuccess,
        });
      }
    } catch {
      // ignore JSON parse error
    }
    const error = `/api/plan failed (${resp.status}):\n${text}`;
    setStatus(`${prefix}${attemptText}: ${error}`);
    return { ok: false, error, cacheCenter: { lat, lon } };
  }

  let out;
  try {
    out = JSON.parse(text);
  } catch (e) {
    const error = `Invalid JSON from /api/plan: ${e}`;
    setStatus(`${prefix}${attemptText}: ${error}`);
    return { ok: false, error, cacheCenter: { lat, lon } };
  }

  // Update TX marker to snapped point (backend always snaps today).
  if (out.snapped_tx && Number.isFinite(out.snapped_tx.lat) && Number.isFinite(out.snapped_tx.lon)) {
    currentTxLocation = { lat: out.snapped_tx.lat, lon: out.snapped_tx.lon };
    updateTxMarker(out.snapped_tx.lat, out.snapped_tx.lon);
    addPlannedTxMarker(out.snapped_tx.lat, out.snapped_tx.lon);
  } else {
    currentTxLocation = { lat, lon };
    updateTxMarker(lat, lon);
    addPlannedTxMarker(lat, lon);
  }

  // Coverage rendering:
  // - 2D mode: point grid (legacy)
  // - 3D modes: backend-generated PNG ellipse drape for consistent visual rendering
  //   across OSM-only and Google-mesh propagation.
  if (out.grid) {
      if (rayMode === "3d" || rayMode === "3d_rt") {
      if (out.heatmap) {
        await renderHeatmapDrapeOsm3d(out.heatmap, out.grid);
      } else {
        try {
          await renderDrapedSurfaceCoverage(out.grid);
        } catch (e) {
          // Fallback to point grid if draping fails or the PNG payload is unavailable.
          console.warn("Surface drape failed, falling back to point grid:", e);
          renderGridCoverage(out.grid);
        }
      }
    } else if (rayMode === "3d_osm") {
      if (out.heatmap) {
        await renderHeatmapDrapeOsm3d(out.heatmap, out.grid);
      } else {
        renderGridCoverage(out.grid);
      }
    } else {
      renderGridCoverage(out.grid);
    }
  }

  // Sector overlays: approximate radius from profile params (matches circular heatmap intent).
  const radiusM = getNumber("max-range", 2000.0);
  if (out.sectors && out.snapped_tx) {
    drawSectorOverlays(out.sectors, out.snapped_tx.lat, out.snapped_tx.lon, radiusM);
  }

  // Optional debug overlay for multipath ray tracing mode.
  if (currentTxLocation) {
    try {
      await renderRaytraceOverlay(currentTxLocation.lat, currentTxLocation.lon);
    } catch (e) {
      console.warn("Failed to render raytrace overlay:", e);
    }
  }

  const key = out.mesh_profile_key ? `\nmesh_key=${out.mesh_profile_key}` : "";
  if (refreshStreetLabelsOnSuccess) queueStreetLabelRefresh(true);
  setStatus(`${prefix}${attemptText}: plan complete.${key}`);
  const cacheCenter = (out.snapped_tx && Number.isFinite(out.snapped_tx.lat) && Number.isFinite(out.snapped_tx.lon))
    ? { lat: out.snapped_tx.lat, lon: out.snapped_tx.lon }
    : { lat, lon };
  return { ok: true, out, cacheCenter };
}

async function runPlan() {
  if (isPlanningQueue) return;
  const queue = getQueuedTxPoints();
  if (!queue || !queue.length) return;

  flyToQueuedPoints(queue);

  planResults = [];
  isPlanningQueue = true;
  setPlanButtonBusy(true);

  const successes = [];
  const failed = [];
  const retryRadiusM = getNumber("max-range", 2000.0);

  try {
    for (let i = 0; i < queue.length; i++) {
      const point = queue[i];
      const result = await runPlanForTx(point.lat, point.lon, {
        queueIndex: i + 1,
        total: queue.length,
        refreshStreetLabelsOnSuccess: false,
      });
      if (result?.ok) {
        successes.push(result.cacheCenter);
        planResults.push({ lat: point.lat, lon: point.lon, out: result.out, cacheCenter: result.cacheCenter });
      } else {
        failed.push(point);
      }

      if (i < queue.length - 1) {
        setStatus(`TX ${i + 1}/${queue.length}: processed. Waiting ${Math.round(MULTI_TX_PULL_DELAY_MS / 1000)}s before next pull…`);
        await sleep(MULTI_TX_PULL_DELAY_MS);
      }
    }

    const retryable = failed.filter((point) =>
      successes.some((success) => haversineDistanceM(point.lat, point.lon, success.lat, success.lon) <= retryRadiusM)
    );

    let retriedSuccesses = 0;
    for (let i = 0; i < retryable.length; i++) {
      const point = retryable[i];
      setStatus(`Retrying ${i + 1}/${retryable.length} near a successful TX so cached OSM data can be reused…`);
      await sleep(MULTI_TX_PULL_DELAY_MS);
      const retryResult = await runPlanForTx(point.lat, point.lon, {
        queueIndex: i + 1,
        total: retryable.length,
        attempt: 2,
        refreshStreetLabelsOnSuccess: false,
      });
      if (retryResult?.ok) {
        retriedSuccesses += 1;
        successes.push(retryResult.cacheCenter);
        planResults.push({ lat: point.lat, lon: point.lon, out: retryResult.out, cacheCenter: retryResult.cacheCenter });
      }
    }

    if (successes.length) queueStreetLabelRefresh(true);

    const totalSuccess = successes.length;
    const totalFailed = Math.max(0, failed.length - retriedSuccesses);
    if (totalSuccess && totalFailed) {
      setStatus(`Batch complete: ${totalSuccess}/${queue.length} TX succeeded, ${totalFailed} failed.`);
    } else if (totalSuccess) {
      setStatus(`Batch complete: all ${totalSuccess} TX points succeeded.`);
    } else {
      setStatus("Batch failed: no TX points completed successfully.");
    }
  } finally {
    isPlanningQueue = false;
    setPlanButtonBusy(false);
  }
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
    const initialTx = getLastQueuedTxPoint() || { lat: 37.7749, lon: -122.4194 };
    const lat0 = initialTx.lat;
    const lon0 = initialTx.lon;
    viewer.camera.setView({
      destination: Cesium.Cartesian3.fromDegrees(lon0, lat0, 1200.0),
      orientation: {
        heading: Cesium.Math.toRadians(0.0),
        pitch: Cesium.Math.toRadians(-45.0),
        roll: 0.0,
      },
    });
  } catch {}

  viewer.camera.moveEnd.addEventListener(() => {
    if (!currentTxLocation) queueStreetLabelRefresh(false);
  });

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
    const queueCount = appendTxInputPoint(lat, lon);
    updateTxMarker(lat, lon);
    queueStreetLabelRefresh(true);
    setStatus(`TX added (${queueCount} queued):
  lat=${lat}
  lon=${lon}

Click Plan RF Queue.`);

    const rtEnabled = document.getElementById("show-raytrace-toggle")?.checked ?? false;
    if (rtEnabled && currentRxLocation) {
      renderRaytraceOverlay(lat, lon, currentRxLocation.lat, currentRxLocation.lon).catch(() => {});
    } else {
      clearRaytraceOverlay();
    }
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

  // SHIFT+click sets RX for multipath debugging (used by 3d_rt mode).
  viewer.screenSpaceEventHandler.setInputAction((click) => {
    const cartesian = viewer.scene.pickPosition(click.position);
    if (!cartesian) return;

    const carto = Cesium.Cartographic.fromCartesian(cartesian);
    const lat = Cesium.Math.toDegrees(carto.latitude);
    const lon = Cesium.Math.toDegrees(carto.longitude);

    currentRxLocation = { lat, lon };
    updateRxMarker(lat, lon);

    const rtEnabled = document.getElementById("show-raytrace-toggle")?.checked ?? false;
    if (rtEnabled && currentTxLocation) {
      renderRaytraceOverlay(currentTxLocation.lat, currentTxLocation.lon, lat, lon).catch(() => {});
      setStatus(`RX set (SHIFT+click).\n\nTX=(${currentTxLocation.lat.toFixed(6)}, ${currentTxLocation.lon.toFixed(6)})\nRX=(${lat.toFixed(6)}, ${lon.toFixed(6)})`);
    } else {
      setStatus(`RX set (SHIFT+click):\n  lat=${lat}\n  lon=${lon}`);
    }
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK, Cesium.KeyboardEventModifier.SHIFT);


  // Wire UI
  document.getElementById("coord-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    await runPlan();
  });
  document.getElementById("tx-input")?.addEventListener("input", updateTxInputSummary);

  document.getElementById("add-sector-btn").addEventListener("click", () => addSectorUI());
  document.getElementById("view-mode-select").addEventListener("change", (e) => {
    applyViewMode(String(e.target.value || "perspective"));
  });
  document.getElementById("show-street-labels-toggle").addEventListener("change", (e) => {
    if (e.target.checked) {
      queueStreetLabelRefresh(true);
    } else {
      setStreetLabelsVisible(false);
    }
  });
  document.getElementById("show-sectors-toggle").addEventListener("change", (e) => toggleSectorVisibility(e.target.checked));
  document.getElementById("show-raytrace-toggle")?.addEventListener("change", async () => {
    if (!currentTxLocation || !currentRxLocation) {
      clearRaytraceOverlay();
      return;
    }
    await renderRaytraceOverlay(currentTxLocation.lat, currentTxLocation.lon, currentRxLocation.lat, currentRxLocation.lon);
  });
  document.getElementById("ray-mode")?.addEventListener("change", () => {
    // Only show overlay in 3d_rt mode.
    clearRaytraceOverlay();
  });
  document.getElementById("export-zip-btn")?.addEventListener("click", () => exportCurrentView());
  document.getElementById("clear-map-btn").addEventListener("click", () => clearMap());

  await loadRfParamsDefaults();
  updateTxInputSummary();
  queueStreetLabelRefresh(true);
  setStatus("Paste one or more TX coordinates, or click on the 3D mesh to append them, then click Plan RF Queue.");
}

init().catch((e) => setStatus(`Init failed:\n${e}`));
