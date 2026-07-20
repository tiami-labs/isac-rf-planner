// 3D RF Planner UI (Cesium + Google Photorealistic 3D Tiles)
//
// Goals:
// - Mirror the 2D UI controls (ray mode + heights + sector configs + RF params).
// - Render coverage in 3D from the same per-cell grid that the 2D UI uses (no forced circle).
// - In 3D mode, if mesh profiles are missing, auto-generate+upload them in-browser.

import * as Cesium from "/Cesium/index.js";
import { buildAndUploadProfiles } from "/mesh_profiler_core.js";
import { launchRayBatch as launchOsmRayBatch } from "/raytrace_3d_osm.js";
import { buildAutoLockSearch, chooseAutoLockAngles, scoreAutoLockCandidate, yawPitchDegTowardLocalVector } from "/rt_autolock.js";

window.__RFP_3D_BUILD = "rt-google-mesh-fastpath-2";
window.__RF_RT_TRACE = [];

let viewer = null;
let txEntity = null;
let rxEntity = null;
let rxCaptureEntity = null;
// Planned overlays persist until the user presses "Clear Map".
// Keep the selection TX marker (txEntity) separate.
let planEntities = []; // Cesium.Entity[] (heatmaps, planned TX markers, etc.)
let planPrimitives = []; // Cesium.Primitive[] / collections
let rfEntities = []; // RF-only entities (heatmap drapes)
let rfPrimitives = []; // RF-only primitives (point heatmaps)
let planCounter = 0;
/** Dedupe keys for persistent batch/remote TX and RX site markers (id or "lat,lon"). */
const plannedTxSiteKeys = new Set();
const plannedRxSiteKeys = new Set();

let sectorEntities = []; // visualization overlays (entities)
let planResults = []; // { lat, lon, out, cacheCenter } per successful plan (for export)

function recordPlanResultForExport(out, requestLat, requestLon) {
  if (!out || typeof out !== "object") return;
  if (out.raytrace_queue_launch) return;
  const slat = out.snapped_tx && Number.isFinite(out.snapped_tx.lat) ? out.snapped_tx.lat : requestLat;
  const slon = out.snapped_tx && Number.isFinite(out.snapped_tx.lon) ? out.snapped_tx.lon : requestLon;
  const cacheCenter = Number.isFinite(slat) && Number.isFinite(slon) ? { lat: slat, lon: slon } : { lat: requestLat, lon: requestLon };
  planResults.push({ lat: slat, lon: slon, out, cacheCenter });
}
let googleTileset = null;
let googleMeshTileset = null;
const googleTileInspectState = {
  enabled: false,
  limit: 25,
  entries: [],
  seenKeys: new Set(),
  last: null,
};

// Expose for F12 console debugging
window.RFPLANNER_DEBUG = {
  get planEntities() { return planEntities; },
  get rfEntities() { return rfEntities; },
  get sectorEntities() { return sectorEntities; },
  get planPrimitives() { return planPrimitives; },
  get rfPrimitives() { return rfPrimitives; },
  get googleTileset() { return googleTileset; },
  get googleTileInspectState() {
    return {
      enabled: googleTileInspectState.enabled,
      limit: googleTileInspectState.limit,
      entryCount: googleTileInspectState.entries.length,
      last: googleTileInspectState.last,
    };
  },
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
const REMOTE_PLAN_POLL_MS = 1500;
const REMOTE_PLAN_SEQ_STORAGE_KEY = "rfplanner3d_remote_plan_seq";
const REMOTE_PLAN_BOOT_STORAGE_KEY = "rfplanner3d_remote_plan_boot_utc";
const REMOTE_3D_RT_SEQ_STORAGE_KEY = "rfplanner3d_remote_3d_rt_seq";
const REMOTE_3D_RT_BOOT_STORAGE_KEY = "rfplanner3d_remote_3d_rt_boot_utc";
const LOCAL_MESH_EXPORT_RADIUS_M = 50.0;
const LOCAL_MESH_EXPORT_BATCH_SIZE = 256;
const LOCAL_MESH_EXPORT_MAX_BUILDINGS = 64;
const LOCAL_MESH_EXPORT_GROUND_MARGIN_M = 2.5;
const LOCAL_MESH_EXPORT_MIN_HEIGHT_M = 3.0;
const LOCAL_MESH_EXPORT_DEFAULT_HEIGHT_M = 10.0;
let isPlanningQueue = false;
let rtAutoLockWorker = null;

function resetGoogleTileInspectionState() {
  googleTileInspectState.entries = [];
  googleTileInspectState.seenKeys.clear();
  googleTileInspectState.last = null;
}

function summarizeObjectKeys(obj, limit = 20) {
  if (!obj || (typeof obj !== 'object' && typeof obj !== 'function')) return [];
  try {
    return Object.keys(obj).slice(0, limit);
  } catch {
    return [];
  }
}

function googleTileResourceUrl(resource) {
  if (!resource) return null;
  try {
    if (typeof resource.getUrlComponent === 'function') return resource.getUrlComponent(true);
  } catch {}
  if (typeof resource.url === 'string') return resource.url;
  if (typeof resource._url === 'string') return resource._url;
  return null;
}

function getGoogleTileDebugKey(tile) {
  return String(
    tile?.content?.uri
    || tile?.content?.url
    || googleTileResourceUrl(tile?._contentResource)
    || tile?._header?.content?.uri
    || tile?._header?.content?.url
    || `tile_${googleTileInspectState.entries.length + 1}`
  );
}

function summarizeGoogleTileModel(model) {
  const loader = model?._loader;
  const gltf = loader?._gltfJsonLoader?.gltf || loader?._gltfJsonLoader?._gltf || null;
  const sceneGraph = model?._sceneGraph;
  return {
    ready: model?.ready ?? null,
    type: model?.type ?? null,
    hasLoader: !!loader,
    loaderKeys: summarizeObjectKeys(loader),
    hasSceneGraph: !!sceneGraph,
    sceneGraphKeys: summarizeObjectKeys(sceneGraph),
    runtimeNodeCount: Array.isArray(sceneGraph?._runtimeNodes) ? sceneGraph._runtimeNodes.length : null,
    runtimePrimitiveCount: Array.isArray(sceneGraph?._runtimePrimitives) ? sceneGraph._runtimePrimitives.length : null,
    componentKeys: summarizeObjectKeys(sceneGraph?.components),
    imageCount: Array.isArray(gltf?.images) ? gltf.images.length : null,
    textureCount: Array.isArray(gltf?.textures) ? gltf.textures.length : null,
    materialCount: Array.isArray(gltf?.materials) ? gltf.materials.length : null,
    meshCount: Array.isArray(gltf?.meshes) ? gltf.meshes.length : null,
    textureLoaderCount: Array.isArray(loader?._textureLoaders) ? loader._textureLoaders.length : null,
    bufferViewLoaderCount: Array.isArray(loader?._bufferViewLoaders) ? loader._bufferViewLoaders.length : null,
  };
}

function summarizeGoogleTileContent(content) {
  const model = content?._model || null;
  return {
    constructorName: content?.constructor?.name || null,
    keys: summarizeObjectKeys(content),
    url: googleTileResourceUrl(content?._resource) || content?.url || content?.uri || null,
    hasModel: !!model,
    hasInnerContents: Array.isArray(content?.innerContents) && content.innerContents.length > 0,
    innerContentsLength: Array.isArray(content?.innerContents) ? content.innerContents.length : null,
    texturesByteLength: content?.texturesByteLength ?? null,
    geometryByteLength: content?.geometryByteLength ?? null,
    model: summarizeGoogleTileModel(model),
  };
}

function captureGoogleTileInspection(tile) {
  if (!googleTileInspectState.enabled || !tile) return;
  const key = getGoogleTileDebugKey(tile);
  if (googleTileInspectState.seenKeys.has(key)) return;
  googleTileInspectState.seenKeys.add(key);

  const content = tile.content || null;
  const model = content?._model || null;
  const entry = {
    capturedAtIso: new Date().toISOString(),
    key,
    tile,
    content,
    model,
    summary: {
      key,
      content: summarizeGoogleTileContent(content),
      tileKeys: summarizeObjectKeys(tile),
      headerKeys: summarizeObjectKeys(tile?._header),
      boundingVolumeKeys: summarizeObjectKeys(tile?._boundingVolume),
    },
  };
  googleTileInspectState.entries.push(entry);
  if (googleTileInspectState.entries.length > googleTileInspectState.limit) {
    googleTileInspectState.entries.shift();
  }
  googleTileInspectState.last = entry;
  console.info('[RF DEBUG] Captured Google tile', entry.summary);
}

function attachGoogleTileInspectionHook(tileset) {
  if (!tileset || tileset.__rfTileInspectionHookInstalled) return;
  tileset.__rfTileInspectionHookInstalled = true;
  if (tileset.tileVisible && typeof tileset.tileVisible.addEventListener === 'function') {
    tileset.tileVisible.addEventListener((tile) => captureGoogleTileInspection(tile));
  }
}

window.RFPLANNER_DEBUG.enableGoogleTileInspection = function enableGoogleTileInspection(options = {}) {
  const limit = Math.max(1, Number(options?.limit) || 25);
  googleTileInspectState.enabled = true;
  googleTileInspectState.limit = limit;
  resetGoogleTileInspectionState();
  console.info(`[RF DEBUG] Google tile inspection enabled (limit=${limit}).`);
  return window.RFPLANNER_DEBUG.googleTileInspectState;
};

window.RFPLANNER_DEBUG.disableGoogleTileInspection = function disableGoogleTileInspection() {
  googleTileInspectState.enabled = false;
  console.info('[RF DEBUG] Google tile inspection disabled.');
  return window.RFPLANNER_DEBUG.googleTileInspectState;
};

window.RFPLANNER_DEBUG.getGoogleTileInspectionEntries = function getGoogleTileInspectionEntries() {
  return googleTileInspectState.entries.slice();
};

window.RFPLANNER_DEBUG.inspectLastGoogleTile = function inspectLastGoogleTile() {
  return googleTileInspectState.last;
};

window.RFPLANNER_DEBUG.logLastGoogleTile = function logLastGoogleTile() {
  const last = googleTileInspectState.last;
  if (!last) {
    console.warn('[RF DEBUG] No Google tile captured yet.');
    return null;
  }
  console.log('[RF DEBUG] Last Google tile summary', last.summary);
  console.dir(last);
  return last;
};

// Debug overlay: multipath rays (direct + reflections)
let raytraceEntities = [];
let raytraceAuxEntities = [];
let raytraceAuxPrimitives = [];
/** Translucent OSM extrusions used for 3D RT context (separate so "Clear rays" can keep layout). */
let osmRtBuildingEntities = [];
const localRtState = {
  clickMode: "tx",
  buildingsCache: new Map(),
  lastSummary: null,
};

function clearOsmRtBuildingOverlay() {
  if (!viewer) return;
  for (const e of osmRtBuildingEntities) {
    try { viewer.entities.remove(e); } catch { /* ignore */ }
  }
  osmRtBuildingEntities = [];
}

/** 2D-style footprint look: light gray fill + darker stroke (see Leaflet OSM demos). */
const OSM_RT_BUILDING_FILL = Cesium.Color.fromCssColorString("#d2d2d2").withAlpha(0.38);
const OSM_RT_BUILDING_OUTLINE = Cesium.Color.fromCssColorString("#6b7280").withAlpha(0.92);

function drawOsmRtBuildingPrisms(prisms) {
  if (!viewer || !Array.isArray(prisms)) return 0;
  const show = document.getElementById("show-osm-rt-buildings-toggle")?.checked ?? true;
  if (!show) return 0;
  clearOsmRtBuildingOverlay();
  let n = 0;
  for (const prism of prisms) {
    if (!Array.isArray(prism.latLonRing) || prism.latLonRing.length < 3) continue;
    const positions = prism.latLonRing.flatMap((p) => [p.lon, p.lat]);
    const ent = viewer.entities.add({
      polygon: {
        hierarchy: Cesium.Cartesian3.fromDegreesArray(positions),
        height: prism.base,
        extrudedHeight: prism.roof,
        material: OSM_RT_BUILDING_FILL,
        outline: true,
        outlineColor: OSM_RT_BUILDING_OUTLINE,
        perPositionHeight: false,
      },
    });
    osmRtBuildingEntities.push(ent);
    n += 1;
  }
  return n;
}

/**
 * @param {{ clearOsmRtBuildings?: boolean }} opts — default clears OSM shells; set false to keep layout when clearing rays only.
 */
function clearRaytraceOverlay(opts = {}) {
  const clearBldg = opts.clearOsmRtBuildings !== false;
  if (!viewer) return;
  for (const e of raytraceEntities) {
    try { viewer.entities.remove(e); } catch { /* ignore */ }
  }
  for (const e of raytraceAuxEntities) {
    try { viewer.entities.remove(e); } catch { /* ignore */ }
  }
  for (const p of raytraceAuxPrimitives) {
    try { viewer.scene.primitives.remove(p); } catch { /* ignore */ }
  }
  raytraceEntities = [];
  raytraceAuxEntities = [];
  raytraceAuxPrimitives = [];
  if (clearBldg) clearOsmRtBuildingOverlay();
  localRtState.lastSummary = null;
  const rtStatus = document.getElementById("rt-status");
  if (rtStatus) rtStatus.textContent = "";
}

function clamp01(x) {
  return Math.max(0, Math.min(1, x));
}

function rsrpToAlpha(rsrpDbm) {
  // Map [-140, -70] -> [0.15, 0.9]
  const t = clamp01((rsrpDbm + 140) / 70);
  return 0.15 + 0.75 * t;
}

function rfRtTrace(stage, extra = {}) {
  const entry = { t: new Date().toISOString(), stage, extra };
  window.__RF_RT_TRACE.push(entry);
  if (window.__RF_RT_TRACE.length > 200) {
    window.__RF_RT_TRACE.splice(0, window.__RF_RT_TRACE.length - 200);
  }
  console.info("[RF RT]", stage, extra);
  return entry;
}

function formatRtHeartbeat(evt) {
  const elapsed = Number.isFinite(Number(evt?.elapsed_s)) ? `${Number(evt.elapsed_s).toFixed(1)}s` : "?s";
  const stage = String(evt?.stage || "processing");
  const detail = String(evt?.detail || "working");
  return `Launching 3D RT... heartbeat (${elapsed}): ${stage}${detail ? ` — ${detail}` : ""}`;
}

async function fetchRaytracePathsWithHeartbeat(body) {
  const controller = new AbortController();
  let idleTimer = null;
  const idleTimeoutMs = 20000;
  const resetIdleTimer = () => {
    if (idleTimer) window.clearTimeout(idleTimer);
    idleTimer = window.setTimeout(() => controller.abort("rt-heartbeat-timeout"), idleTimeoutMs);
  };
  try {
    resetIdleTimer();
    rfRtTrace("fetch_start", { endpoint: "/api/raytrace_paths/stream" });
    const resp = await fetch("/api/raytrace_paths/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    if (!resp.ok) {
      const t = await resp.text();
      throw new Error(`/api/raytrace_paths/stream ${resp.status}: ${t}`);
    }
    if (!resp.body || !resp.body.getReader) {
      resetIdleTimer();
      const fallback = await resp.json();
      return fallback;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finalPayload = null;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      resetIdleTimer();
      buffer += decoder.decode(value, { stream: true });
      let nl = buffer.indexOf("\n");
      while (nl >= 0) {
        const line = buffer.slice(0, nl).trim();
        buffer = buffer.slice(nl + 1);
        nl = buffer.indexOf("\n");
        if (!line) continue;
        let evt;
        try {
          evt = JSON.parse(line);
        } catch (e) {
          rfRtTrace("stream_parse_error", { line, error: String(e) });
          continue;
        }
        rfRtTrace("stream_event", evt);
        if (evt.type === "heartbeat") {
          setStatus(formatRtHeartbeat(evt));
        } else if (evt.type === "result") {
          finalPayload = evt.payload || null;
        } else if (evt.type === "error") {
          throw new Error(String(evt.error || "3D RT stream failed."));
        }
      }
    }
    if (idleTimer) window.clearTimeout(idleTimer);
    if (!finalPayload) throw new Error("3D RT stream ended without a final result.");
    return finalPayload;
  } catch (e) {
    if (idleTimer) window.clearTimeout(idleTimer);
    if (String(e) === "rt-heartbeat-timeout" || e?.name === "AbortError") {
      throw new Error("3D RT request timed out after backend heartbeats stopped.");
    }
    throw e;
  }
}

function drawRaytracePathsFromPayload(json, { clearExisting = true } = {}) {
  if (!viewer) return { json, rawCount: 0, drawnCount: 0, paths: [] };
  if (clearExisting) clearRaytraceOverlay();
  const paths = Array.isArray(json?.paths) ? json.paths : [];
  let drawnCount = 0;
  for (const p of paths) {
    const pts = Array.isArray(p?.points) ? p.points : [];
    if (pts.length < 2) continue;
    const flat = [];
    for (const q of pts) {
      const lat = Number(q?.lat);
      const lon = Number(q?.lon);
      const h = Number.isFinite(q?.h) ? Number(q.h) : 0.0;
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
      flat.push(lon, lat, h);
    }
    if (flat.length < 6) continue;
    const rsrp = Number(p?.rsrp_dbm);
    const alpha = rsrpToAlpha(Number.isFinite(rsrp) ? rsrp : -140);
    const kind = String(p?.kind || "direct");
    const color = kind === "direct"
      ? Cesium.Color.CYAN.withAlpha(alpha)
      : (kind === "reflect2" ? Cesium.Color.LIME.withAlpha(alpha) : Cesium.Color.YELLOW.withAlpha(alpha));
    const entity = viewer.entities.add({
      polyline: {
        positions: Cesium.Cartesian3.fromDegreesArrayHeights(flat),
        width: kind === "direct" ? 2.0 : 2.5,
        material: color,
        clampToGround: false,
      },
    });
    raytraceEntities.push(entity);
    drawnCount += 1;
  }
  return { json, rawCount: paths.length, drawnCount, paths };
}

// -------- Client-side 3D specular ray tracer on Google mesh with optional OSM preview --------
const RT3 = {
  add: (a, b) => ({ x: a.x + b.x, y: a.y + b.y, z: a.z + b.z }),
  sub: (a, b) => ({ x: a.x - b.x, y: a.y - b.y, z: a.z - b.z }),
  mul: (a, s) => ({ x: a.x * s, y: a.y * s, z: a.z * s }),
  dot: (a, b) => a.x * b.x + a.y * b.y + a.z * b.z,
  len: (a) => Math.hypot(a.x, a.y, a.z),
  norm: (a) => {
    const L = Math.hypot(a.x, a.y, a.z) || 1.0;
    return { x: a.x / L, y: a.y / L, z: a.z / L };
  },
  reflect: (d, n) => {
    const s = 2.0 * (d.x * n.x + d.y * n.y + d.z * n.z);
    return { x: d.x - s * n.x, y: d.y - s * n.y, z: d.z - s * n.z };
  },
};

function pointToRtLocal(origin, lat, lon, absHeightM) {
  const local = localEnuFromOrigin(origin.lat, origin.lon, lat, lon);
  return { x: local.east_m, y: Number(absHeightM || 0.0), z: local.north_m };
}

function rtLocalToCartographic(origin, p) {
  const ll = offsetLatLonMeters(origin.lat, origin.lon, p.x, p.z);
  return { lat: ll.lat, lon: ll.lon, h: p.y };
}

function getRtNumber(id, fallback) {
  const el = document.getElementById(id);
  if (!el) return fallback;
  const v = Number.parseFloat(String(el.value ?? ""));
  return Number.isFinite(v) ? v : fallback;
}

function setRtStatus(message) {
  const el = document.getElementById("rt-status");
  if (el) el.textContent = String(message || "");
}

function isRtRayMode(rayMode) {
  const mode = String(rayMode || "").toLowerCase();
  return mode === "3d_rt" || mode === "3d_rt_google" || mode === "3d_rt_osm";
}

function updateRtControlsVisibility() {
  const section = document.getElementById("rt-controls-section");
  if (!section) return;
  const visible = isRtRayMode(getString("ray-mode", "3d_osm"));
  section.style.display = visible ? "" : "none";
  if (!visible) {
    clearRaytraceOverlay({ clearOsmRtBuildings: false });
    setRtStatus("");
  }
}

function updateRtModeButtons() {
  const modes = [
    ["rt-place-tx-btn", "tx"],
    ["rt-place-rx-btn", "rx"],
    ["rt-steer-btn", "steer"],
  ];
  for (const [id, mode] of modes) {
    const el = document.getElementById(id);
    if (!el) continue;
    const active = localRtState.clickMode === mode;
    el.style.boxShadow = active ? "0 0 0 2px rgba(255,255,255,0.55) inset" : "none";
    el.style.filter = active ? "brightness(1.08)" : "none";
  }
}

function setRtClickMode(mode) {
  localRtState.clickMode = mode;
  updateRtModeButtons();
  const hints = {
    tx: "3D RT click mode: Place TX on the Google mesh.",
    rx: "3D RT click mode: Place RX on the Google mesh.",
    steer: "3D RT click mode: Steer TX by clicking a point on the Google mesh.",
  };
  setRtStatus(hints[mode] || "");
}

function getBuildingHeightM(building) {
  const direct = Number(building?.height_m);
  if (Number.isFinite(direct) && direct > 1.0) return direct;
  const sampled = Number(building?.sampled_height_m);
  if (Number.isFinite(sampled) && sampled > 1.0) return sampled;
  const levels = Number(building?.tags?.["building:levels"] || building?.tags?.levels);
  if (Number.isFinite(levels) && levels > 0) return Math.max(3.0, levels * 3.0);
  return LOCAL_MESH_EXPORT_DEFAULT_HEIGHT_M;
}

function ringSignedAreaXZ(points) {
  let acc = 0.0;
  for (let i = 0; i < points.length; i++) {
    const a = points[i];
    const b = points[(i + 1) % points.length];
    acc += a.x * b.z - b.x * a.z;
  }
  return 0.5 * acc;
}

function pointInPolygonXZ(pt, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const xi = ring[i].x, zi = ring[i].z;
    const xj = ring[j].x, zj = ring[j].z;
    const hit = ((zi > pt.z) !== (zj > pt.z)) && (pt.x < ((xj - xi) * (pt.z - zi)) / ((zj - zi) || 1e-12) + xi);
    if (hit) inside = !inside;
  }
  return inside;
}

function rtWorldFromGeo(pt) {
  return Cesium.Cartesian3.fromDegrees(pt.lon, pt.lat, pt.h);
}

function rtEnuVectorFromLocal(localDir) {
  return new Cesium.Cartesian3(localDir.x, localDir.z, localDir.y);
}

function rtLocalVectorFromEnu(enuVec) {
  return { x: enuVec.x, y: enuVec.z, z: enuVec.y };
}

function rtWorldDirectionFromLocal(originCartesian, localDir) {
  const enu = Cesium.Transforms.eastNorthUpToFixedFrame(originCartesian);
  const w = Cesium.Matrix4.multiplyByPointAsVector(enu, rtEnuVectorFromLocal(localDir), new Cesium.Cartesian3());
  return Cesium.Cartesian3.normalize(w, w);
}

function rtLocalDirectionFromWorld(originCartesian, worldDir) {
  const enu = Cesium.Transforms.eastNorthUpToFixedFrame(originCartesian);
  const inv = Cesium.Matrix4.inverseTransformation(enu, new Cesium.Matrix4());
  const enuVec = Cesium.Matrix4.multiplyByPointAsVector(inv, worldDir, new Cesium.Cartesian3());
  return RT3.norm(rtLocalVectorFromEnu(enuVec));
}

async function pickGoogleMeshPointFromScreen(screenPosition, excludeList) {
  if (!viewer) return null;
  try {
    const cartesian = viewer.scene.pickPosition(screenPosition);
    if (Cesium.defined(cartesian)) return Cesium.Cartesian3.clone(cartesian);
  } catch {}
  if (googleMeshTileset) {
    try {
      const ray = viewer.camera.getPickRay(screenPosition);
      if (ray) {
        const pick = await pickSceneRayHitFast(ray, excludeList || buildRaytraceExclusionList());
        if (Cesium.defined(pick?.position)) return Cesium.Cartesian3.clone(pick.position);
      }
    } catch {}
  }
  return null;
}

function rtSphereHitDistanceWorld(origin, dir, center, radius) {
  const oc = Cesium.Cartesian3.subtract(origin, center, new Cesium.Cartesian3());
  const b = Cesium.Cartesian3.dot(oc, dir);
  const c = Cesium.Cartesian3.dot(oc, oc) - radius * radius;
  const disc = b * b - c;
  if (disc < 0) return null;
  const s = Math.sqrt(disc);
  const t1 = -b - s;
  const t2 = -b + s;
  if (t1 > 1e-6) return t1;
  if (t2 > 1e-6) return t2;
  return null;
}

function rtReflectWorld(dir, normal) {
  const scale = 2.0 * Cesium.Cartesian3.dot(dir, normal);
  const out = Cesium.Cartesian3.subtract(dir, Cesium.Cartesian3.multiplyByScalar(normal, scale, new Cesium.Cartesian3()), new Cesium.Cartesian3());
  return Cesium.Cartesian3.normalize(out, out);
}

function rtMakePerpBasis(dir, refUp) {
  let u = Cesium.Cartesian3.cross(refUp, dir, new Cesium.Cartesian3());
  if (Cesium.Cartesian3.magnitudeSquared(u) < 1e-10) u = Cesium.Cartesian3.cross(Cesium.Cartesian3.UNIT_X, dir, new Cesium.Cartesian3());
  if (Cesium.Cartesian3.magnitudeSquared(u) < 1e-10) u = Cesium.Cartesian3.cross(Cesium.Cartesian3.UNIT_Y, dir, new Cesium.Cartesian3());
  Cesium.Cartesian3.normalize(u, u);
  const v = Cesium.Cartesian3.cross(dir, u, new Cesium.Cartesian3());
  Cesium.Cartesian3.normalize(v, v);
  return { u, v };
}

function buildRaytraceExclusionList() {
  const vals = [];
  if (txEntity) vals.push(txEntity);
  if (rxEntity) vals.push(rxEntity);
  if (rxCaptureEntity) vals.push(rxCaptureEntity);
  for (const e of planEntities) vals.push(e);
  for (const e of raytraceAuxEntities) vals.push(e);
  return vals;
}

async function pickSceneRayHitFast(ray, excludeList) {
  if (!viewer) return null;
  try {
    if (typeof viewer.scene.pickFromRay === "function") {
      return await Promise.resolve(viewer.scene.pickFromRay(ray, excludeList, 0.1));
    }
  } catch {}
  try {
    if (typeof viewer.scene.pickFromRayMostDetailed === "function") {
      return await Promise.resolve(viewer.scene.pickFromRayMostDetailed(ray, excludeList, 0.1));
    }
  } catch {}
  return null;
}

async function pickGoogleMeshSurfaceHit(origin, dir, maxDist, excludeList) {
  if (!viewer || !googleMeshTileset) return null;
  const ray = new Cesium.Ray(origin, dir);
  const pick = await pickSceneRayHitFast(ray, excludeList);
  if (!Cesium.defined(pick) || !Cesium.defined(pick.position)) return null;
  const point = Cesium.Cartesian3.clone(pick.position);
  const t = Cesium.Cartesian3.distance(origin, point);
  if (!Number.isFinite(t) || t <= 0.05 || t > maxDist) return null;
  return { point, t, pick };
}

function estimateGoogleMeshNormalFast(dir, hitPoint) {
  if (viewer) {
    try {
      const screen = Cesium.SceneTransforms.wgs84ToWindowCoordinates(viewer.scene, hitPoint);
      if (Cesium.defined(screen)) {
        const sx = Number(screen.x);
        const sy = Number(screen.y);
        if (Number.isFinite(sx) && Number.isFinite(sy)) {
          const offsets = [
            new Cesium.Cartesian2(sx - 2, sy),
            new Cesium.Cartesian2(sx + 2, sy),
            new Cesium.Cartesian2(sx, sy - 2),
            new Cesium.Cartesian2(sx, sy + 2),
          ];
          const picks = offsets.map((p) => {
            try {
              return viewer.scene.pickPosition(p);
            } catch {
              return null;
            }
          });
          const [left, right, upPt, downPt] = picks;
          if (Cesium.defined(left) && Cesium.defined(right) && Cesium.defined(upPt) && Cesium.defined(downPt)) {
            const dx = Cesium.Cartesian3.subtract(right, left, new Cesium.Cartesian3());
            const dy = Cesium.Cartesian3.subtract(downPt, upPt, new Cesium.Cartesian3());
            const depthNormal = Cesium.Cartesian3.cross(dx, dy, new Cesium.Cartesian3());
            if (Cesium.Cartesian3.magnitudeSquared(depthNormal) > 1e-8) {
              Cesium.Cartesian3.normalize(depthNormal, depthNormal);
              if (Cesium.Cartesian3.dot(depthNormal, dir) > 0.0) {
                Cesium.Cartesian3.negate(depthNormal, depthNormal);
              }
              return depthNormal;
            }
          }
        }
      }
    } catch {}
  }
  const carto = Cesium.Cartographic.fromCartesian(hitPoint);
  const up = Cesium.Ellipsoid.WGS84.geodeticSurfaceNormalCartographic(carto, new Cesium.Cartesian3());
  const refUp = (Cesium.defined(up) && Cesium.Cartesian3.magnitudeSquared(up) >= 1e-10)
    ? Cesium.Cartesian3.normalize(up, up)
    : Cesium.Cartesian3.normalize(hitPoint, new Cesium.Cartesian3());
  const upDot = Cesium.Cartesian3.dot(dir, refUp);
  const horiz = Cesium.Cartesian3.subtract(
    dir,
    Cesium.Cartesian3.multiplyByScalar(refUp, upDot, new Cesium.Cartesian3()),
    new Cesium.Cartesian3(),
  );
  if (Cesium.Cartesian3.magnitudeSquared(horiz) > 1e-8 && Math.abs(upDot) < 0.55) {
    Cesium.Cartesian3.normalize(horiz, horiz);
    const wall = Cesium.Cartesian3.negate(horiz, new Cesium.Cartesian3());
    if (Cesium.Cartesian3.dot(wall, dir) > 0.0) {
      Cesium.Cartesian3.negate(wall, wall);
    }
    return wall;
  }
  if (Cesium.Cartesian3.dot(refUp, dir) > 0.0) {
    Cesium.Cartesian3.negate(refUp, refUp);
  }
  return Cesium.Cartesian3.normalize(refUp, refUp);
}

async function rtTraceSingleRayOnGoogleMesh(origin, dir, rxCenter, rxRadius, maxBounces, maxDist, excludeList) {
  const points = [Cesium.Cartesian3.clone(origin)];
  let pos = Cesium.Cartesian3.clone(origin);
  let d = Cesium.Cartesian3.normalize(Cesium.Cartesian3.clone(dir), new Cesium.Cartesian3());
  let traveled = 0.0;
  let bounces = 0;
  const bounceEps = 0.25;
  while (traveled < maxDist - 1e-6) {
    const remaining = maxDist - traveled;
    const tRx = rtSphereHitDistanceWorld(pos, d, rxCenter, rxRadius);
    const hit = await pickGoogleMeshSurfaceHit(pos, d, remaining, excludeList);
    const hitT = hit ? hit.t : null;
    if (tRx != null && tRx <= remaining && (hitT == null || tRx <= hitT)) {
      points.push(Cesium.Cartesian3.add(pos, Cesium.Cartesian3.multiplyByScalar(d, tRx, new Cesium.Cartesian3()), new Cesium.Cartesian3()));
      return { points, hitRx: true, bounces, totalDistance: traveled + tRx };
    }
    if (!hit) {
      points.push(Cesium.Cartesian3.add(pos, Cesium.Cartesian3.multiplyByScalar(d, remaining, new Cesium.Cartesian3()), new Cesium.Cartesian3()));
      return { points, hitRx: false, bounces, totalDistance: maxDist };
    }
    points.push(hit.point);
    traveled += hitT;
    if (bounces >= maxBounces) {
      return { points, hitRx: false, bounces, totalDistance: traveled };
    }
    const normal = estimateGoogleMeshNormalFast(d, hit.point);
    d = rtReflectWorld(d, normal);
    pos = Cesium.Cartesian3.add(hit.point, Cesium.Cartesian3.multiplyByScalar(d, bounceEps, new Cesium.Cartesian3()), new Cesium.Cartesian3());
    bounces += 1;
  }
  return { points, hitRx: false, bounces, totalDistance: traveled };
}

function rtDirectionFromYawPitchDeg(yawDeg, pitchDeg) {
  const yaw = Cesium.Math.toRadians(yawDeg);
  const pitch = Cesium.Math.toRadians(pitchDeg);
  const cp = Math.cos(pitch);
  return RT3.norm({ x: Math.sin(yaw) * cp, y: Math.sin(pitch), z: Math.cos(yaw) * cp });
}

function rtYawPitchDegTowardWorldTarget(txWorld, targetWorld) {
  const worldVec = Cesium.Cartesian3.subtract(targetWorld, txWorld, new Cesium.Cartesian3());
  const localDir = rtLocalDirectionFromWorld(txWorld, worldVec);
  return yawPitchDegTowardLocalVector(localDir);
}

function rtHammersley(index, count) {
  let bits = index;
  let rev = 0;
  let scale = 0.5;
  while (bits > 0) {
    rev += scale * (bits & 1);
    bits >>= 1;
    scale *= 0.5;
  }
  return [count <= 1 ? 0.5 : index / count, rev];
}

function rtGenerateDirections(rayCount, yawDeg, pitchDeg, hSpreadDeg, vSpreadDeg) {
  const dirs = [];
  const hHalf = 0.5 * Math.max(0.0, hSpreadDeg);
  const vHalf = 0.5 * Math.max(0.0, vSpreadDeg);
  if (rayCount <= 1) return [rtDirectionFromYawPitchDeg(yawDeg, pitchDeg)];
  for (let i = 0; i < rayCount; i++) {
    const [u, v] = rtHammersley(i, rayCount);
    const yaw = yawDeg + (u - 0.5) * 2.0 * hHalf;
    const pitch = pitchDeg + (v - 0.5) * 2.0 * vHalf;
    dirs.push(rtDirectionFromYawPitchDeg(yaw, pitch));
  }
  return dirs;
}

async function terrainHeightAtLatLon(lat, lon) {
  try {
    const c = Cesium.Cartographic.fromDegrees(lon, lat);
    await Cesium.sampleTerrainMostDetailed(viewer.terrainProvider, [c]);
    const h = Number(c.height);
    return Number.isFinite(h) ? h : 0.0;
  } catch {
    return 0.0;
  }
}

function nextAnimationFrame() {
  return new Promise((resolve) => window.requestAnimationFrame(resolve));
}

function getRtAutoLockWorker() {
  if (rtAutoLockWorker) return rtAutoLockWorker;
  rtAutoLockWorker = new Worker("/rt_autolock_worker.js", { type: "module" });
  return rtAutoLockWorker;
}

function runOsmAutoLockInWorker(payload) {
  return new Promise((resolve, reject) => {
    const worker = getRtAutoLockWorker();
    const onMessage = (event) => {
      const msg = event?.data || {};
      cleanup();
      if (msg.ok) resolve(msg.result || null);
      else reject(new Error(String(msg.error || "autolock worker failed")));
    };
    const onError = (event) => {
      cleanup();
      reject(new Error(String(event?.message || event || "autolock worker error")));
    };
    const cleanup = () => {
      worker.removeEventListener("message", onMessage);
      worker.removeEventListener("error", onError);
    };
    worker.addEventListener("message", onMessage);
    worker.addEventListener("error", onError);
    worker.postMessage({ type: "evaluate_osm_autolock", payload });
  });
}

/**
 * OSM prism bases must use street-level elevation, not the building roof.
 * This path is preview-only now; ray bounces use Google mesh directly.
 */
async function get3dRtBuildings(originLat, originLon, radiusM) {
  const key = `${originLat.toFixed(5)}:${originLon.toFixed(5)}:${Math.round(radiusM)}:fast_ground_v1`;
  if (localRtState.buildingsCache.has(key)) return localRtState.buildingsCache.get(key);
  setRtStatus(`Loading optional OSM preview within ${Math.round(radiusM)} m...`);
  const lookup = await fetchOsmBuildingsNearPoint(originLat, originLon, radiusM, 256);
  const raw = Array.isArray(lookup?.buildings) ? lookup.buildings : [];
  const originGroundAbs = Number.isFinite(Number(currentTxLocation?.ground_h))
    ? Number(currentTxLocation.ground_h)
    : await terrainHeightAtLatLon(originLat, originLon);
  const prisms = [];
  for (const building of raw) {
    const ringLatLon = normalizeBuildingRing(building?.geometry || []);
    if (ringLatLon.length < 3) continue;
    const groundAbs = originGroundAbs;
    const ring = ringLatLon.map((p) => {
      const local = localEnuFromOrigin(originLat, originLon, p.lat, p.lon);
      return { x: local.east_m, z: local.north_m };
    });
    const area = ringSignedAreaXZ(ring);
    const roof = groundAbs + getBuildingHeightM(building);
    const walls = [];
    for (let i = 0; i < ring.length; i++) {
      const a2 = ring[i];
      const b2 = ring[(i + 1) % ring.length];
      const edge = { x: b2.x - a2.x, y: 0, z: b2.z - a2.z };
      let normal = area >= 0 ? { x: edge.z, y: 0, z: -edge.x } : { x: -edge.z, y: 0, z: edge.x };
      normal = RT3.norm(normal);
      walls.push({
        a: { x: a2.x, y: groundAbs, z: a2.z },
        b: { x: b2.x, y: groundAbs, z: b2.z },
        base: groundAbs,
        roof,
        normal,
        buildingId: building.id ?? `b_${prisms.length}`,
      });
    }
    prisms.push({
      id: building.id ?? `b_${prisms.length}`,
      material: building.material || "unknown",
      ring,
      latLonRing: ringLatLon,
      base: groundAbs,
      roof,
      walls,
    });
  }
  const result = { origin: { lat: originLat, lon: originLon }, prisms };
  localRtState.buildingsCache.set(key, result);
  return result;
}

function getCurrentTxWorldPoint() {
  if (!currentTxLocation) return null;
  const heightM = getNumber("tx-height-m", 10.0);
  return { lat: currentTxLocation.lat, lon: currentTxLocation.lon, h: Number(currentTxLocation.ground_h || 0.0) + heightM };
}

function getCurrentRxWorldPoint() {
  if (!currentRxLocation) return null;
  const heightM = getNumber("rx-height-m", 1.5);
  return { lat: currentRxLocation.lat, lon: currentRxLocation.lon, h: Number(currentRxLocation.ground_h || 0.0) + heightM };
}

function buildRtCollisionBoxesFromPrisms(prisms) {
  const boxes = [];
  for (const prism of Array.isArray(prisms) ? prisms : []) {
    if (!Array.isArray(prism?.ring) || prism.ring.length < 3) continue;
    let minX = Infinity;
    let maxX = -Infinity;
    let minZ = Infinity;
    let maxZ = -Infinity;
    for (const p of prism.ring) {
      const x = Number(p?.x);
      const z = Number(p?.z);
      if (!Number.isFinite(x) || !Number.isFinite(z)) continue;
      minX = Math.min(minX, x);
      maxX = Math.max(maxX, x);
      minZ = Math.min(minZ, z);
      maxZ = Math.max(maxZ, z);
    }
    const base = Number(prism?.base);
    const roof = Number(prism?.roof);
    if (!Number.isFinite(minX) || !Number.isFinite(maxX) || !Number.isFinite(minZ) || !Number.isFinite(maxZ)) continue;
    if (!Number.isFinite(base) || !Number.isFinite(roof) || roof <= base) continue;
    boxes.push({
      min: { x: minX, y: base, z: minZ },
      max: { x: maxX, y: roof, z: maxZ },
    });
  }
  return boxes;
}

function rtWorldPointFromLocal(origin, p) {
  return rtWorldFromGeo(rtLocalToCartographic(origin, p));
}

function buildRtBeamCornerAngles(yawDeg, pitchDeg, hSpreadDeg, vSpreadDeg) {
  const hHalf = Math.max(0.0, hSpreadDeg) * 0.5;
  const vHalf = Math.max(0.0, vSpreadDeg) * 0.5;
  return [
    { yaw: yawDeg - hHalf, pitch: pitchDeg - vHalf },
    { yaw: yawDeg + hHalf, pitch: pitchDeg - vHalf },
    { yaw: yawDeg + hHalf, pitch: pitchDeg + vHalf },
    { yaw: yawDeg - hHalf, pitch: pitchDeg + vHalf },
  ];
}

function clearRtHeadingOverlay() {
  if (!viewer) return;
  for (const e of raytraceAuxEntities) {
    try { viewer.entities.remove(e); } catch {}
  }
  for (const p of raytraceAuxPrimitives) {
    try { viewer.scene.primitives.remove(p); } catch {}
  }
  raytraceAuxEntities = [];
  raytraceAuxPrimitives = [];
}

function addRtAuxPolylinePrimitive(positions, width, color) {
  if (!viewer || !Array.isArray(positions) || positions.length < 2) return null;
  const primitive = viewer.scene.primitives.add(new Cesium.Primitive({
    geometryInstances: new Cesium.GeometryInstance({
      geometry: new Cesium.PolylineGeometry({
        positions,
        width,
        vertexFormat: Cesium.PolylineColorAppearance.VERTEX_FORMAT,
      }),
      attributes: {
        color: Cesium.ColorGeometryInstanceAttribute.fromColor(color),
      },
    }),
    appearance: new Cesium.PolylineColorAppearance(),
    asynchronous: false,
    allowPicking: false,
  }));
  raytraceAuxPrimitives.push(primitive);
  return primitive;
}

function addRtAuxPolygonPrimitive(positions, color) {
  if (!viewer || !Array.isArray(positions) || positions.length < 3) return null;
  const primitive = viewer.scene.primitives.add(new Cesium.Primitive({
    geometryInstances: new Cesium.GeometryInstance({
      geometry: new Cesium.PolygonGeometry({
        polygonHierarchy: new Cesium.PolygonHierarchy(positions),
        perPositionHeight: true,
        vertexFormat: Cesium.PerInstanceColorAppearance.VERTEX_FORMAT,
      }),
      attributes: {
        color: Cesium.ColorGeometryInstanceAttribute.fromColor(color),
      },
    }),
    appearance: new Cesium.PerInstanceColorAppearance({
      translucent: true,
      closed: false,
    }),
    asynchronous: false,
    allowPicking: false,
  }));
  raytraceAuxPrimitives.push(primitive);
  return primitive;
}

function updateRtHeadingEntity() {
  if (!viewer) return;
  clearRtHeadingOverlay();
  const tx = getCurrentTxWorldPoint();
  if (!tx) return;
  const yaw = getRtNumber("rt-yaw-deg", 0.0);
  const pitch = getRtNumber("rt-pitch-deg", 0.0);
  const hSpread = getRtNumber("rt-h-spread-deg", 30.0);
  const vSpread = getRtNumber("rt-v-spread-deg", 18.0);
  const dirLocal = rtDirectionFromYawPitchDeg(yaw, pitch);
  const txCart = rtWorldFromGeo(tx);
  const dirWorld = rtWorldDirectionFromLocal(txCart, dirLocal);
  const lenM = Math.min(getRtNumber("rt-max-distance-m", 800.0), 140.0);
  const arrowEnd = Cesium.Cartesian3.add(txCart, Cesium.Cartesian3.multiplyByScalar(dirWorld, lenM, new Cesium.Cartesian3()), new Cesium.Cartesian3());
  const previewLenM = Math.max(18.0, Math.min(48.0, lenM * 0.35));
  const cornerAngles = buildRtBeamCornerAngles(yaw, pitch, hSpread, vSpread);
  const cornerPoints = cornerAngles.map((edge) => {
    const edgeLocalDir = rtDirectionFromYawPitchDeg(edge.yaw, edge.pitch);
    const edgeWorldDir = rtWorldDirectionFromLocal(txCart, edgeLocalDir);
    return Cesium.Cartesian3.add(
      txCart,
      Cesium.Cartesian3.multiplyByScalar(edgeWorldDir, previewLenM, new Cesium.Cartesian3()),
      new Cesium.Cartesian3(),
    );
  });
  const edgeAngles = [
    { yaw: yaw - hSpread * 0.5, pitch },
    { yaw: yaw + hSpread * 0.5, pitch },
    { yaw, pitch: pitch - vSpread * 0.5 },
    { yaw, pitch: pitch + vSpread * 0.5 },
  ];

  addRtAuxPolylinePrimitive([txCart, arrowEnd], 3.0, Cesium.Color.RED.withAlpha(0.92));
  addRtAuxPolylinePrimitive([...cornerPoints, cornerPoints[0]], 1.8, Cesium.Color.RED.withAlpha(0.6));
  for (const edge of edgeAngles) {
    const edgeLocalDir = rtDirectionFromYawPitchDeg(edge.yaw, edge.pitch);
    const edgeWorldDir = rtWorldDirectionFromLocal(txCart, edgeLocalDir);
    const edgeEnd = Cesium.Cartesian3.add(
      txCart,
      Cesium.Cartesian3.multiplyByScalar(edgeWorldDir, previewLenM, new Cesium.Cartesian3()),
      new Cesium.Cartesian3(),
    );
    addRtAuxPolylinePrimitive([txCart, edgeEnd], 1.1, Cesium.Color.RED.withAlpha(0.35));
  }
  for (const corner of cornerPoints) {
    addRtAuxPolylinePrimitive([txCart, corner], 1.4, Cesium.Color.RED.withAlpha(0.5));
  }
}

async function loadOsmRtBuildingsLayoutOnly() {
  if (!viewer) return null;
  const show = document.getElementById("show-osm-rt-buildings-toggle")?.checked ?? false;
  if (!show) {
    setRtStatus("Enable OSM preview first if you want footprint shells; Google mesh ray tracing does not need OSM.");
    return null;
  }
  const tx = getCurrentTxWorldPoint();
  if (!tx) {
    setRtStatus("Place TX first (Place TX + click the mesh). OSM preview is centered on TX.");
    return null;
  }
  const maxDist = Math.max(10.0, getRtNumber("rt-max-distance-m", getNumber("max-range", 800.0)));
  try {
    const sceneData = await get3dRtBuildings(tx.lat, tx.lon, maxDist);
    const n = drawOsmRtBuildingPrisms(sceneData.prisms);
    const msg = `OSM preview: ${sceneData.prisms.length} buildings in cache, ${n} shells drawn. Google mesh remains the actual bounce surface.`;
    setRtStatus(msg);
    setStatus(msg);
    return { prisms: sceneData.prisms.length, drawn: n };
  } catch (e) {
    const err = `Load OSM preview failed: ${e}`;
    setRtStatus(err);
    setStatus(err);
    return null;
  }
}

async function simulateLocal3dRaytraceForAngles({
  yawDeg,
  pitchDeg,
  rayCount,
  maxBounces,
  maxDist,
  rxRadius,
  hSpreadDeg,
  vSpreadDeg,
}) {
  if (!viewer) return null;
  const rayMode = getString("ray-mode", "3d_osm").toLowerCase();
  const isGoogleRt = rayMode === "3d_rt" || rayMode === "3d_rt_google";
  const isOsmRt = rayMode === "3d_rt_osm";
  if (!isGoogleRt && !isOsmRt) return null;
  const tx = getCurrentTxWorldPoint();
  const rx = getCurrentRxWorldPoint();
  if (!tx || !rx) return null;

  if (isOsmRt) {
    const sceneData = await get3dRtBuildings(tx.lat, tx.lon, maxDist);
    const boxes = buildRtCollisionBoxesFromPrisms(sceneData.prisms);
    const rxLocalOffset = localEnuFromOrigin(tx.lat, tx.lon, rx.lat, rx.lon);
    const txLocal = { x: 0.0, y: tx.h, z: 0.0 };
    const rxLocal = { x: rxLocalOffset.east_m, y: rx.h, z: rxLocalOffset.north_m };
    const batch = launchOsmRayBatch({
      tx: txLocal,
      rx: rxLocal,
      boxes,
      prisms: sceneData.prisms,
      numRays: rayCount,
      yawDeg,
      pitchDeg,
      hSpreadDeg,
      vSpreadDeg,
      maxBounces,
      maxDistance: maxDist,
      rxRadius,
    });
    let hits = 0;
    let minBounce = Infinity;
    let maxHitBounce = -Infinity;
    for (const ray of batch.rays) {
      if (!ray.hitRx) continue;
      hits += 1;
      minBounce = Math.min(minBounce, ray.bounces);
      maxHitBounce = Math.max(maxHitBounce, ray.bounces);
    }
    return {
      hits,
      minBounce: Number.isFinite(minBounce) ? minBounce : null,
      maxHitBounce: Number.isFinite(maxHitBounce) ? maxHitBounce : null,
      bounceSurface: "osm_collision",
      collisionBoxes: boxes.length,
    };
  }

  const dirsLocal = rtGenerateDirections(rayCount, yawDeg, pitchDeg, hSpreadDeg, vSpreadDeg);
  const originCartesian = rtWorldFromGeo(tx);
  const rxCenter = rtWorldFromGeo(rx);
  const excludeList = buildRaytraceExclusionList();
  let hits = 0;
  let minBounce = Infinity;
  let maxHitBounce = -Infinity;
  const BATCH = 24;
  for (let start = 0; start < dirsLocal.length; start += BATCH) {
    const slice = dirsLocal.slice(start, start + BATCH);
    const results = await Promise.all(slice.map(async (localDir) => {
      const dirWorld = rtWorldDirectionFromLocal(originCartesian, localDir);
      return await rtTraceSingleRayOnGoogleMesh(originCartesian, dirWorld, rxCenter, rxRadius, maxBounces, maxDist, excludeList);
    }));
    for (const result of results) {
      if (!result.hitRx) continue;
      hits += 1;
      minBounce = Math.min(minBounce, result.bounces);
      maxHitBounce = Math.max(maxHitBounce, result.bounces);
    }
  }
  return {
    hits,
    minBounce: Number.isFinite(minBounce) ? minBounce : null,
    maxHitBounce: Number.isFinite(maxHitBounce) ? maxHitBounce : null,
    bounceSurface: "google_mesh",
  };
}

async function autoLockGoogleMeshSteering({
  base,
  hSpreadDeg,
  vSpreadDeg,
  searchRayCount,
  maxBounces,
  maxDist,
  rxRadius,
}) {
  const search = buildAutoLockSearch(base.yawDeg, base.pitchDeg, hSpreadDeg, vSpreadDeg);
  const total = search.coarse.length + search.fineOffsets.length;
  let best = null;
  let checked = 0;

  for (const candidate of search.coarse) {
    checked += 1;
    setRtStatus(`Auto lock: evaluating ${checked}/${total}...`);
    const summary = await simulateLocal3dRaytraceForAngles({
      yawDeg: candidate.yawDeg,
      pitchDeg: candidate.pitchDeg,
      rayCount: searchRayCount,
      maxBounces,
      maxDist,
      rxRadius,
      hSpreadDeg,
      vSpreadDeg,
    });
    const score = scoreAutoLockCandidate(candidate, summary);
    if (!best || score > best.score) best = { candidate, summary, score };
    await nextAnimationFrame();
  }

  for (const [dyaw, dpitch] of search.fineOffsets) {
    checked += 1;
    setRtStatus(`Auto lock: refining ${checked}/${total}...`);
    const candidate = {
      yawDeg: best.candidate.yawDeg + dyaw,
      pitchDeg: best.candidate.pitchDeg + dpitch,
      dyaw: best.candidate.dyaw + dyaw,
      dpitch: best.candidate.dpitch + dpitch,
    };
    const summary = await simulateLocal3dRaytraceForAngles({
      yawDeg: candidate.yawDeg,
      pitchDeg: candidate.pitchDeg,
      rayCount: searchRayCount,
      maxBounces,
      maxDist,
      rxRadius,
      hSpreadDeg,
      vSpreadDeg,
    });
    const score = scoreAutoLockCandidate(candidate, summary);
    if (score > best.score) best = { candidate, summary, score };
    await nextAnimationFrame();
  }

  return { best, chosen: chooseAutoLockAngles(base, best), checked: total };
}

async function autoLockRtSteering() {
  const tx = getCurrentTxWorldPoint();
  const rx = getCurrentRxWorldPoint();
  if (!tx || !rx) {
    setRtStatus("Place both TX and RX before Auto lock.");
    return null;
  }
  const rayMode = getString("ray-mode", "3d_osm").toLowerCase();
  if (!isRtRayMode(rayMode)) {
    setRtStatus("Auto lock is only available for 3D RT modes.");
    return null;
  }
  const txWorld = rtWorldFromGeo(tx);
  const rxWorld = rtWorldFromGeo(rx);
  const base = rtYawPitchDegTowardWorldTarget(txWorld, rxWorld);
  const hSpreadDeg = getRtNumber("rt-h-spread-deg", 30.0);
  const vSpreadDeg = getRtNumber("rt-v-spread-deg", 18.0);
  const searchRayCount = Math.max(24, Math.min(96, Math.round(getRtNumber("rt-ray-count", 240) * 0.35)));
  const maxBounces = Math.max(0, Math.min(20, Math.round(getRtNumber("rt-max-bounces", 3))));
  const maxDist = Math.max(10.0, getRtNumber("rt-max-distance-m", getNumber("max-range", 800.0)));
  const rxRadius = Math.max(0.2, getRtNumber("rt-rx-radius-m", 10.0));
  let evaluation = null;

  if (rayMode === "3d_rt_osm") {
    const sceneData = await get3dRtBuildings(tx.lat, tx.lon, maxDist);
    const boxes = buildRtCollisionBoxesFromPrisms(sceneData.prisms);
    const rxLocalOffset = localEnuFromOrigin(tx.lat, tx.lon, rx.lat, rx.lon);
    setRtStatus("Auto lock: evaluating OSM collision search...");
    evaluation = await runOsmAutoLockInWorker({
      txLocal: { x: 0.0, y: tx.h, z: 0.0 },
      rxLocal: { x: rxLocalOffset.east_m, y: rx.h, z: rxLocalOffset.north_m },
      boxes,
      prisms: sceneData.prisms,
      baseYawDeg: base.yawDeg,
      basePitchDeg: base.pitchDeg,
      hSpreadDeg,
      vSpreadDeg,
      maxBounces,
      maxDistance: maxDist,
      rxRadius,
      searchRayCount,
    });
  } else {
    evaluation = await autoLockGoogleMeshSteering({
      base,
      hSpreadDeg,
      vSpreadDeg,
      searchRayCount,
      maxBounces,
      maxDist,
      rxRadius,
    });
  }

  const chosen = evaluation?.chosen || base;
  const best = evaluation?.best || null;
  setInputValue("rt-yaw-deg", Number(chosen.yawDeg || 0).toFixed(2));
  setInputValue("rt-pitch-deg", Number(chosen.pitchDeg || 0).toFixed(2));
  updateRtHeadingEntity();
  if (best && Number(best.summary?.hits || 0) > 0) {
    setRtStatus(`Auto lock set. Yaw=${Number(chosen.yawDeg).toFixed(1)}°, pitch=${Number(chosen.pitchDeg).toFixed(1)}°. Search rays=${searchRayCount}. Hits=${best.summary.hits}.`);
  } else {
    setRtStatus(`Auto lock fallback to direct RX pointing. Yaw=${Number(chosen.yawDeg).toFixed(1)}°, pitch=${Number(chosen.pitchDeg).toFixed(1)}°. No evaluated candidate reached RX.`);
  }
  return chosen;
}

async function launchLocal3dRaytrace() {
  if (!viewer) return null;
  const rayMode = getString("ray-mode", "3d_osm").toLowerCase();
  const isGoogleRt = rayMode === "3d_rt" || rayMode === "3d_rt_google";
  const isOsmRt = rayMode === "3d_rt_osm";
  if (!isGoogleRt && !isOsmRt) {
    setRtStatus("3D RT launches only when Propagation = 3D RT (Google mesh) or 3D RT (OSM collision).");
    return null;
  }
  const tx = getCurrentTxWorldPoint();
  const rx = getCurrentRxWorldPoint();
  if (!tx || !rx) {
    setRtStatus("Place both TX and RX before launching 3D rays.");
    return null;
  }
  clearRaytraceOverlay({ clearOsmRtBuildings: false });
  updateRtHeadingEntity();
  const maxDist = Math.max(10.0, getRtNumber("rt-max-distance-m", getNumber("max-range", 800.0)));
  const rayCount = Math.max(1, Math.min(4000, Math.round(getRtNumber("rt-ray-count", 240))));
  const maxBounces = Math.max(0, Math.min(20, Math.round(getRtNumber("rt-max-bounces", 3))));
  const rxRadius = Math.max(0.2, getRtNumber("rt-rx-radius-m", 10.0));
  const yawDeg = getRtNumber("rt-yaw-deg", 0.0);
  const pitchDeg = getRtNumber("rt-pitch-deg", 0.0);
  const hSpreadDeg = getRtNumber("rt-h-spread-deg", 30.0);
  const vSpreadDeg = getRtNumber("rt-v-spread-deg", 18.0);
  if (isOsmRt) {
    setRtStatus(`Launching ${rayCount} rays on OSM collision RT…`);
    const sceneData = await get3dRtBuildings(tx.lat, tx.lon, maxDist);
    const boxes = buildRtCollisionBoxesFromPrisms(sceneData.prisms);
    const rxLocalOffset = localEnuFromOrigin(tx.lat, tx.lon, rx.lat, rx.lon);
    const txLocal = { x: 0.0, y: tx.h, z: 0.0 };
    const rxLocal = { x: rxLocalOffset.east_m, y: rx.h, z: rxLocalOffset.north_m };
    const batch = launchOsmRayBatch({
      tx: txLocal,
      rx: rxLocal,
      boxes,
      prisms: sceneData.prisms,
      numRays: rayCount,
      yawDeg,
      pitchDeg,
      hSpreadDeg,
      vSpreadDeg,
      maxBounces,
      maxDistance: maxDist,
      rxRadius,
    });
    let hits = 0;
    let minBounce = Infinity;
    let maxHitBounce = -Infinity;
    for (const ray of batch.rays) {
      if (Array.isArray(ray.points) && ray.points.length >= 2) {
        const worldPoints = ray.points.map((p) => rtWorldPointFromLocal(tx, p));
        const color = ray.hitRx ? Cesium.Color.LIME.withAlpha(0.92) : Cesium.Color.ORANGE.withAlpha(0.68);
        const width = ray.hitRx ? 3.0 : 1.6;
        raytraceEntities.push(viewer.entities.add({
          polyline: {
            positions: worldPoints,
            width,
            material: color,
            clampToGround: false,
          },
        }));
      }
      if (ray.hitRx) {
        hits += 1;
        minBounce = Math.min(minBounce, ray.bounces);
        maxHitBounce = Math.max(maxHitBounce, ray.bounces);
      }
    }
    const previewOn = document.getElementById("show-osm-rt-buildings-toggle")?.checked ?? false;
    const previewText = previewOn && osmRtBuildingEntities.length ? ` OSM preview shells shown=${osmRtBuildingEntities.length}.` : "";
    const summary = hits > 0
      ? `Launched ${rayCount} rays on OSM collision RT. Hits=${hits}. Min hit bounces=${minBounce}. Max hit bounces=${maxHitBounce}. Boxes=${boxes.length}.${previewText}`
      : `Launched ${rayCount} rays on OSM collision RT. No ray reached RX. Adjust steering, spread, ray count, or max bounces. Boxes=${boxes.length}.${previewText}`;
    localRtState.lastSummary = {
      rayCount,
      hits,
      minBounce: Number.isFinite(minBounce) ? minBounce : null,
      maxHitBounce: Number.isFinite(maxHitBounce) ? maxHitBounce : null,
      bounceSurface: "osm_collision",
      collisionBoxes: boxes.length,
    };
    setRtStatus(summary);
    setStatus(summary);
    return localRtState.lastSummary;
  }

  const dirsLocal = rtGenerateDirections(
    rayCount,
    yawDeg,
    pitchDeg,
    hSpreadDeg,
    vSpreadDeg,
  );
  const originCartesian = rtWorldFromGeo(tx);
  const rxCenter = rtWorldFromGeo(rx);
  const excludeList = buildRaytraceExclusionList();
  let hits = 0;
  let minBounce = Infinity;
  let maxHitBounce = -Infinity;
  const BATCH = 24;
  setRtStatus(`Launching ${rayCount} rays on Google mesh…`);

  for (let start = 0; start < dirsLocal.length; start += BATCH) {
    const slice = dirsLocal.slice(start, start + BATCH);
    const results = await Promise.all(slice.map(async (localDir) => {
      const dirWorld = rtWorldDirectionFromLocal(originCartesian, localDir);
      return await rtTraceSingleRayOnGoogleMesh(originCartesian, dirWorld, rxCenter, rxRadius, maxBounces, maxDist, excludeList);
    }));
    for (const result of results) {
      if (Array.isArray(result.points) && result.points.length >= 2) {
        const color = result.hitRx ? Cesium.Color.LIME.withAlpha(0.92) : Cesium.Color.ORANGE.withAlpha(0.68);
        const width = result.hitRx ? 3.0 : 1.6;
        raytraceEntities.push(viewer.entities.add({
          polyline: {
            positions: result.points,
            width,
            material: color,
            clampToGround: false,
          },
        }));
      }
      if (result.hitRx) {
        hits += 1;
        minBounce = Math.min(minBounce, result.bounces);
        maxHitBounce = Math.max(maxHitBounce, result.bounces);
      }
    }
  }
  const previewOn = document.getElementById("show-osm-rt-buildings-toggle")?.checked ?? false;
  const previewText = previewOn && osmRtBuildingEntities.length ? ` OSM preview shells shown=${osmRtBuildingEntities.length}.` : "";
  const summary = hits > 0
    ? `Launched ${rayCount} rays on Google mesh. Hits=${hits}. Min hit bounces=${minBounce}. Max hit bounces=${maxHitBounce}.${previewText}`
    : `Launched ${rayCount} rays on Google mesh. No ray reached RX. Adjust steering, spread, ray count, or max bounces.${previewText}`;
  localRtState.lastSummary = { rayCount, hits, minBounce: Number.isFinite(minBounce) ? minBounce : null, maxHitBounce: Number.isFinite(maxHitBounce) ? maxHitBounce : null, bounceSurface: "google_mesh" };
  setRtStatus(summary);
  setStatus(summary);
  return localRtState.lastSummary;
}

async function renderRaytraceOverlay(txLat, txLon, rxLat, rxLon) {
  if (!viewer) return null;
  const rayMode = getString("ray-mode", "3d_osm").toLowerCase();
  if (rayMode !== "3d_rt" && rayMode !== "3d_rt_google" && rayMode !== "3d_rt_osm") {
    clearRaytraceOverlay({ clearOsmRtBuildings: false });
    return null;
  }
  const txHeightM = getNumber("tx-height-m", 10.0);
  const rxHeightM = getNumber("rx-height-m", 1.5);
  if (Number.isFinite(txLat) && Number.isFinite(txLon)) {
    currentTxLocation = {
      ...(currentTxLocation || {}),
      lat: txLat,
      lon: txLon,
      ground_h: Number.isFinite(Number(currentTxLocation?.ground_h)) ? Number(currentTxLocation.ground_h) : 0.0,
    };
    updateTxMarker(txLat, txLon, Number(currentTxLocation.ground_h || 0.0) + txHeightM);
  }
  if (Number.isFinite(rxLat) && Number.isFinite(rxLon)) {
    currentRxLocation = {
      ...(currentRxLocation || {}),
      lat: rxLat,
      lon: rxLon,
      ground_h: Number.isFinite(Number(currentRxLocation?.ground_h)) ? Number(currentRxLocation.ground_h) : 0.0,
    };
    updateRxMarker(rxLat, rxLon, Number(currentRxLocation.ground_h || 0.0) + rxHeightM);
  }
  return await launchLocal3dRaytrace();
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

/** Keep sidebar “3D mesh profile params” aligned with the plan the API actually ran. */
function syncMeshProfileInputsFromPlan(out) {
  const rp = out?.grid?.rf_params ?? out?.rf_config_used;
  if (!rp || typeof rp !== "object") return;
  const mr = Number(rp.max_range_m);
  const step = Number(rp.step_m);
  const dth = Number(rp.dtheta_deg);
  if (Number.isFinite(mr)) setInputValue("max-range", mr);
  if (Number.isFinite(step)) setInputValue("dr-m", step);
  if (Number.isFinite(dth)) setInputValue("dtheta", dth);
}

async function loadRfParamsDefaults() {
  try {
    const resp = await fetch("/api/rf-params");
    if (!resp.ok) return;
    const cfg = await resp.json();
    if (!cfg) return;
    // Mesh profile section — keep in sync with configs/rf.params.yaml + PlanRequest defaults.
    if (Number.isFinite(Number(cfg.max_range_m))) setInputValue("max-range", Number(cfg.max_range_m));
    if (Number.isFinite(Number(cfg.step_m))) setInputValue("dr-m", Number(cfg.step_m));
    if (Number.isFinite(Number(cfg.dtheta_deg))) setInputValue("dtheta", Number(cfg.dtheta_deg));
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

function syncRxManualFields(lat, lon) {
  const la = document.getElementById("rx-lat-manual");
  const lo = document.getElementById("rx-lon-manual");
  if (!la || !lo) return;
  if (Number.isFinite(lat) && Number.isFinite(lon)) {
    la.value = String(lat);
    lo.value = String(lon);
  }
}

function applyManualRxFromInputsIfNeeded() {
  const la = document.getElementById("rx-lat-manual");
  const lo = document.getElementById("rx-lon-manual");
  if (!la || !lo) return false;
  const rlat = Number.parseFloat(String(la.value || "").trim());
  const rlon = Number.parseFloat(String(lo.value || "").trim());
  if (!Number.isFinite(rlat) || !Number.isFinite(rlon)) return false;
  currentRxLocation = { lat: rlat, lon: rlon };
  if (viewer) updateRxMarker(rlat, rlon);
  return true;
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

function clearCoverageOverlay() {
  for (const p of rfPrimitives) {
    try { viewer.scene.primitives.remove(p); } catch {}
    const idx = planPrimitives.indexOf(p);
    if (idx >= 0) planPrimitives.splice(idx, 1);
  }
  rfPrimitives = [];
  for (const e of rfEntities) {
    try { viewer.entities.remove(e); } catch {}
    const idx = planEntities.indexOf(e);
    if (idx >= 0) planEntities.splice(idx, 1);
  }
  rfEntities = [];
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
  const maxRangeM = getNumber("max-range", 2500.0);
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

function updateTxMarker(lat, lon, heightM = null) {
  const pos = Cesium.Cartesian3.fromDegrees(lon, lat, Number.isFinite(Number(heightM)) ? Number(heightM) : 20.0);
  if (!txEntity) {
    txEntity = viewer.entities.add({
      position: pos,
      point: {
        pixelSize: 12,
        color: Cesium.Color.YELLOW.withAlpha(0.98),
        outlineColor: Cesium.Color.BLACK.withAlpha(0.9),
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
  if (txEntity) txEntity.show = true;
  updateRtHeadingEntity();
}

function syncPlannedTxSelectionMarker() {
  if (txEntity) txEntity.show = plannedTxSiteKeys.size === 0;
}

function plannedSiteCoordKey(lat, lon) {
  return `${Number(lat).toFixed(6)},${Number(lon).toFixed(6)}`;
}

function inferTxSiteLabelFromPlan(out) {
  if (!out || typeof out !== "object") return null;
  if (out.tx_site_id != null) return String(out.tx_site_id).trim() || null;
  if (out.site_id != null) return String(out.site_id).trim() || null;
  if (out.tx_site && out.tx_site.id != null) return String(out.tx_site.id).trim() || null;
  const secs = out.sectors;
  if (Array.isArray(secs) && secs.length) {
    const sid = String(secs[0].sector_id || "");
    const m = sid.match(/^CATX?0*(\d{3,5})/i);
    if (m) return `CAT${m[1]}`;
    if (sid) return sid.replace(/[A-Z]\d*$/i, "").slice(0, 16);
  }
  return null;
}

/** Persistent gNB TX markers (yellow), same pattern as RX site markers. */
function addPlannedTxSiteMarker(lat, lon, opts = {}) {
  const la = Number(lat);
  const lo = Number(lon);
  if (!Number.isFinite(la) || !Number.isFinite(lo)) return;
  const idRaw = opts.id != null ? String(opts.id).trim() : "";
  const key = idRaw || plannedSiteCoordKey(la, lo);
  if (plannedTxSiteKeys.has(key)) return;
  plannedTxSiteKeys.add(key);

  const id = idRaw || `TX${plannedTxSiteKeys.size}`;
  const name = opts.name != null ? String(opts.name).trim() : "";
  const text = name ? `${id}\n${name}` : id;
  const heightM = Number(opts.heightM);
  const h = Number.isFinite(heightM) ? heightM : 20.0;
  const fill = Cesium.Color.YELLOW.withAlpha(0.96);
  const outline = Cesium.Color.BLACK.withAlpha(0.85);
  const ent = viewer.entities.add({
    position: Cesium.Cartesian3.fromDegrees(lo, la, h),
    point: {
      pixelSize: 10,
      color: fill,
      outlineColor: outline,
      outlineWidth: 2,
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
    },
    label: {
      text,
      font: "12px sans-serif",
      fillColor: fill,
      outlineColor: Cesium.Color.BLACK,
      outlineWidth: 2,
      style: Cesium.LabelStyle.FILL_AND_OUTLINE,
      pixelOffset: new Cesium.Cartesian2(0, -28),
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
    },
  });
  planEntities.push(ent);
  syncPlannedTxSelectionMarker();
}

function registerPlannedTxFromPlan(out, lat, lon) {
  const slat = out?.snapped_tx?.lat ?? lat;
  const slon = out?.snapped_tx?.lon ?? lon;
  if (!Number.isFinite(slat) || !Number.isFinite(slon)) return;
  currentTxLocation = { lat: slat, lon: slon };
  const id = inferTxSiteLabelFromPlan(out);
  const txHeightM = Number(out?.tx_height_m ?? out?.rf_config?.tx_height_m);
  addPlannedTxSiteMarker(slat, slon, {
    id: id || undefined,
    name: out?.tx_site_name ?? out?.site_name ?? "",
    heightM: Number.isFinite(txHeightM) ? txHeightM : undefined,
  });
}

function updateRxMarker(lat, lon, heightM = null) {
  const pos = Cesium.Cartesian3.fromDegrees(lon, lat, Number.isFinite(Number(heightM)) ? Number(heightM) : 20.0);
  if (!rxEntity) {
    rxEntity = viewer.entities.add({
      position: pos,
      point: {
        pixelSize: 12,
        color: Cesium.Color.CYAN.withAlpha(0.98),
        outlineColor: Cesium.Color.BLACK.withAlpha(0.9),
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
  const captureR = Math.max(0.2, getRtNumber("rt-rx-radius-m", 10.0));
  if (!rxCaptureEntity) {
    rxCaptureEntity = viewer.entities.add({
      position: pos,
      ellipsoid: {
        radii: new Cesium.Cartesian3(captureR, captureR, captureR),
        fill: false,
        outline: true,
        outlineColor: Cesium.Color.CYAN.withAlpha(0.95),
      },
    });
  } else {
    rxCaptureEntity.position = pos;
    rxCaptureEntity.ellipsoid = {
      radii: new Cesium.Cartesian3(captureR, captureR, captureR),
      fill: false,
      outline: true,
      outlineColor: Cesium.Color.CYAN.withAlpha(0.95),
    };
  }
}

/** RX measurement sites from API (magenta); deduped across batch plans. */
function addPlannedRxSiteMarkers(rxSites) {
  const fill = Cesium.Color.fromCssColorString("#e879f9").withAlpha(0.96);
  const outline = Cesium.Color.BLACK.withAlpha(0.85);
  for (const s of rxSites || []) {
    const la = Number(s.lat);
    const lo = Number(s.lon);
    if (!Number.isFinite(la) || !Number.isFinite(lo)) continue;
    const id = String(s.id != null ? s.id : "RX");
    const key = id || plannedSiteCoordKey(la, lo);
    if (plannedRxSiteKeys.has(key)) continue;
    plannedRxSiteKeys.add(key);
    const name = s.name != null ? String(s.name).trim() : "";
    const text = name ? `${id}\n${name}` : id;
    const ent = viewer.entities.add({
      position: Cesium.Cartesian3.fromDegrees(lo, la, 20.0),
      point: {
        pixelSize: 10,
        color: fill,
        outlineColor: outline,
        outlineWidth: 2,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
      label: {
        text,
        font: "12px sans-serif",
        fillColor: fill,
        outlineColor: Cesium.Color.BLACK,
        outlineWidth: 2,
        style: Cesium.LabelStyle.FILL_AND_OUTLINE,
        pixelOffset: new Cesium.Cartesian2(0, -28),
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
    });
    planEntities.push(ent);
  }
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
  plannedTxSiteKeys.clear();
  plannedRxSiteKeys.clear();
  syncPlannedTxSelectionMarker();

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

  const selectedLayer = (typeof RFTerrainParams !== "undefined")
    ? RFTerrainParams.getCoverageDisplayLayer()
    : "rsrp";
  const legendUnit = selectedLayer === "field_strength" ? "dBµV/m"
    : selectedLayer === "rsrp" ? "dBm"
    : "dB";
  const maxUnit = document.getElementById("legend-unit-max");
  const minUnit = document.getElementById("legend-unit-min");
  if (maxUnit) maxUnit.textContent = legendUnit;
  if (minUnit) minUnit.textContent = legendUnit;

  const titleEl = legend.querySelector("h3");
  if (titleEl && typeof RFTerrainParams !== "undefined") {
    titleEl.textContent = RFTerrainParams.coverageLayerLabel(RFTerrainParams.getCoverageDisplayLayer());
  }
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

/** Pick pre-colored PNG heatmap for the active display layer (always drape, never point grid). */
function pickHeatmapForLayer(plan, layer) {
  const covLayer = layer || (
    typeof RFTerrainParams !== "undefined" ? RFTerrainParams.getCoverageDisplayLayer() : "rsrp"
  );
  if (covLayer === "terrain_shadow" && plan.heatmap_terrain && plan.heatmap_terrain.png_b64) {
    return plan.heatmap_terrain;
  }
  if (covLayer === "sinr" && plan.heatmap_sinr && plan.heatmap_sinr.png_b64) {
    return plan.heatmap_sinr;
  }
  return plan.heatmap;
}

/** Same PNG ellipse drape for 3D OSM city plans — RSRP, SINR, and terrain layers. */
async function renderOsmPlanHeatmapOn3d(out, covLayer) {
  if (out && out.heatmap && out.heatmap.layer === "field_strength_dbuv_m") {
    covLayer = "field_strength";
    const layerEl = document.getElementById("coverage-display-layer");
    if (layerEl) layerEl.value = "field_strength";
  }
  // City policy: one draw path. Always drape the RSRP PNG geometry; layer PNG only updates legend scale.
  const layerHeatmap = pickHeatmapForLayer(out, covLayer);
  const drapeHeatmap = (out.heatmap && out.heatmap.png_b64) ? out.heatmap : layerHeatmap;
  const sectorHm = covLayer === "rsrp" ? out.heatmap_by_sector : null;
  if (drapeHeatmap && drapeHeatmap.png_b64) {
    await renderHeatmapDrapeOsm3d(drapeHeatmap, out.grid, sectorHm, layerHeatmap);
    return true;
  }
  if (sectorHm && typeof sectorHm === "object" && Object.keys(sectorHm).length) {
    await renderHeatmapDrapesPerSectorOsm3d(sectorHm, out.grid);
    return true;
  }
  return false;
}

// Fallback only when backend returned no PNG (legacy payloads). Prefer pickHeatmapForLayer + drape.
function renderGridCoverage(grid) {
  if (!grid || !Array.isArray(grid.cell_lat) || !Array.isArray(grid.cell_lon) || !Array.isArray(grid.rsrp_dbm)) return;
  const lats = grid.cell_lat;
  const lons = grid.cell_lon;
  const layer = (typeof RFTerrainParams !== "undefined" ? RFTerrainParams.getCoverageDisplayLayer() : "rsrp");
  if (lats.length === 0 || lons.length !== lats.length) return;

  let vmin = Infinity;
  let vmax = -Infinity;
  for (let i = 0; i < lats.length; i++) {
    const v = typeof RFTerrainParams !== "undefined"
      ? RFTerrainParams.pickSampleMetric(grid, i, layer)
      : grid.rsrp_dbm[i];
    if (!Number.isFinite(v)) continue;
    if (v < vmin) vmin = v;
    if (v > vmax) vmax = v;
  }
  if (!Number.isFinite(vmin) || !Number.isFinite(vmax)) return;

  if (layer === "terrain_shadow") {
    updateRSRPLegend(0, 40, vmin, vmax);
  } else if (layer === "sinr") {
    updateRSRPLegend(-5, 30, vmin, vmax);
  } else {
    updateRSRPLegend(FIXED_RSRP_MIN, FIXED_RSRP_MAX, vmin, vmax);
  }

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
    const v = typeof RFTerrainParams !== "undefined"
      ? RFTerrainParams.pickSampleMetric(grid, i, layer)
      : grid.rsrp_dbm[i];
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

// 3D OSM-only rendering: one ellipse per sector when `heatmapBySector` is present (true beam RSRP each).
async function renderHeatmapDrapesPerSectorOsm3d(heatmapBySector, grid) {
  if (!viewer || !heatmapBySector || typeof heatmapBySector !== "object") return;
  if (!grid || !grid.tx || !Number.isFinite(grid.tx.lat) || !Number.isFinite(grid.tx.lon)) return;
  const txLat = grid.tx.lat;
  const txLon = grid.tx.lon;
  const radiusM =
    (() => {
      const first = Object.values(heatmapBySector)[0];
      return (first && Number.isFinite(first.radius_m) ? Number(first.radius_m) : NaN) ||
        (grid && grid.rf_params && Number.isFinite(grid.rf_params.max_range_m) ? Number(grid.rf_params.max_range_m) : NaN) ||
        getNumber("max-range", 2500.0);
    })();
  if (!Number.isFinite(radiusM) || radiusM <= 0) return;
  const entries = Object.entries(heatmapBySector);
  if (!entries.length) return;
  const first = entries[0][1];
  const actualMin = (first && Number.isFinite(first.actual_min)) ? Number(first.actual_min) : FIXED_RSRP_MIN;
  const actualMax = (first && Number.isFinite(first.actual_max)) ? Number(first.actual_max) : FIXED_RSRP_MAX;
  updateRSRPLegend(FIXED_RSRP_MIN, FIXED_RSRP_MAX, actualMin, actualMax);
  for (const [sid, hm] of entries) {
    const imgSrc = hm && hm.png_b64;
    if (!imgSrc) continue;
    const sw = offsetEnuToLatLon(txLat, txLon, -radiusM, -radiusM);
    const ne = offsetEnuToLatLon(txLat, txLon, radiusM, radiusM);
    trackRfEntity(viewer.entities.add({
      name: `RSRP sector ${sid}`,
      position: Cesium.Cartesian3.fromDegrees(txLon, txLat),
      ellipse: {
        semiMajorAxis: radiusM,
        semiMinorAxis: radiusM,
        granularity: Cesium.Math.toRadians(0.25),
        material: new Cesium.ImageMaterialProperty({ image: imgSrc, transparent: true }),
        heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
        outline: false,
      },
    }));
  }
}

// 3D OSM-only rendering: map-aligned PNG rectangle drape (same policy as 2D Leaflet + city exports).
async function renderHeatmapDrapeOsm3d(heatmap, grid, heatmapBySector, legendHeatmap) {
  if (heatmapBySector && typeof heatmapBySector === "object" && Object.keys(heatmapBySector).length) {
    await renderHeatmapDrapesPerSectorOsm3d(heatmapBySector, grid);
    return;
  }

  if (!viewer) {
    console.warn("renderHeatmapDrapeOsm3d: Cesium viewer not ready");
    return;
  }
  if (!grid || !grid.tx || !Number.isFinite(grid.tx.lat) || !Number.isFinite(grid.tx.lon)) return;

  const txLat = grid.tx.lat;
  const txLon = grid.tx.lon;

  const radiusM =
    (heatmap && Number.isFinite(heatmap.radius_m) ? Number(heatmap.radius_m) : NaN) ||
    (grid && grid.rf_params && Number.isFinite(grid.rf_params.max_range_m) ? Number(grid.rf_params.max_range_m) : NaN) ||
    getNumber("max-range", 2500.0);

  if (!Number.isFinite(radiusM) || radiusM <= 0) return;

  const legend = legendHeatmap || heatmap;
  const scaleMin = (legend && Number.isFinite(legend.vmin)) ? Number(legend.vmin) : FIXED_RSRP_MIN;
  const scaleMax = (legend && Number.isFinite(legend.vmax)) ? Number(legend.vmax) : FIXED_RSRP_MAX;
  const actualMin = (legend && Number.isFinite(legend.actual_min)) ? Number(legend.actual_min) : scaleMin;
  const actualMax = (legend && Number.isFinite(legend.actual_max)) ? Number(legend.actual_max) : scaleMax;
  updateRSRPLegend(scaleMin, scaleMax, actualMin, actualMax);

  const imgSrc = heatmap && heatmap.png_b64 ? heatmap.png_b64 : null;

  if (!imgSrc) {
    console.warn(
      "renderHeatmapDrapeOsm3d: no heatmap.png_b64 — re-run Plan RF Queue. "
      + "Point-grid fallback is disabled (city/terrain share one PNG drape path).",
    );
    return;
  }

  addHeatmapDrape(imgSrc, txLat, txLon, radiusM);
}

// Local tangent-plane offset (east m, north m) from (latDeg, lonDeg) — same as app.js / 2D Leaflet.
function offsetEnuToLatLon(latDeg, lonDeg, eastM, northM) {
  const R = 6371000.0;
  const φ = (latDeg * Math.PI) / 180.0;
  const dLat = (northM / R) * (180.0 / Math.PI);
  const dLon = (eastM / (R * Math.cos(φ))) * (180.0 / Math.PI);
  return { lat: latDeg + dLat, lon: lonDeg + dLon };
}

/** One city-style PNG drape: Cesium ellipse clamped to ground/tiles (unchanged city geometry). */
function addHeatmapDrape(imgSrc, txLat, txLon, radiusM) {
  return trackRfEntity(viewer.entities.add({
    position: Cesium.Cartesian3.fromDegrees(txLon, txLat),
    ellipse: {
      semiMajorAxis: radiusM,
      semiMinorAxis: radiusM,
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

  const defaultBw = getNumber("bw-mhz", 40.0);

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
        <label style="display:block; font-size:10px; margin-bottom:2px; color:#ccc;">Channel BW (MHz)</label>
        <input type="number" class="sector-bw-mhz" step="0.1" min="0.1" value="${defaultBw}"
          style="width:100%; padding:4px; box-sizing:border-box; background:#333; color:#eee; border:1px solid #555; border-radius:2px; font-size:11px;" />
      </div>
      <div>
        <label style="display:block; font-size:10px; margin-bottom:2px; color:#ccc;">TX ant. gain (dBi, opt.)</label>
        <input type="number" class="sector-tx-gain-dbi" step="0.1" placeholder="global default"
          style="width:100%; padding:4px; box-sizing:border-box; background:#333; color:#eee; border:1px solid #555; border-radius:2px; font-size:11px;" />
      </div>
    </div>
    <div style="margin-top:6px; font-size:10px;">
      <label style="display:block; font-size:10px; margin-bottom:2px; color:#ccc;">PCI (optional, 0–1007)</label>
      <input type="number" class="sector-pci" step="1" min="0" max="1007" placeholder="omit if unknown"
        style="width:100%; max-width:100%; padding:4px; box-sizing:border-box; background:#333; color:#eee; border:1px solid #555; border-radius:2px; font-size:11px;" />
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
    const bwEl = div.querySelector(".sector-bw-mhz");
    const bwParsed = bwEl ? parseFloat(bwEl.value) : NaN;
    const channelBw = Number.isFinite(bwParsed) ? bwParsed : getNumber("bw-mhz", 40.0);

    const sectorConfig = {
      sector_id: sectorId,
      sector_type: sectorType,
      freq_mhz: freq,
      tx_power_dbm: power,
      channel_bandwidth_mhz: channelBw,
    };
    const gainStr = div.querySelector(".sector-tx-gain-dbi")?.value?.trim();
    if (gainStr) {
      const g = parseFloat(gainStr);
      if (Number.isFinite(g)) sectorConfig.tx_antenna_gain_dbi = g;
    }
    const pciStr = div.querySelector(".sector-pci")?.value?.trim();
    if (pciStr) {
      const pci = parseInt(pciStr, 10);
      if (Number.isFinite(pci) && pci >= 0 && pci <= 1007) sectorConfig.pci = pci;
    }

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
  const radiusM = getNumber("max-range", 2500.0);
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
    const sectorHeatmapBlobs = [];
    if (window.RFExportUtils && planResults.length) {
      for (const pr of planResults) {
        const hbs = pr.out && pr.out.heatmap_by_sector;
        const seclist = pr.out && Array.isArray(pr.out.sectors) ? pr.out.sectors : [];
        const sectorBlobs = {};
        if (hbs && typeof hbs === "object" && window.RFExportUtils.base64DataUrlToBlob) {
          for (const [sid, hm] of Object.entries(hbs)) {
            if (hm && hm.png_b64) {
              const b = window.RFExportUtils.base64DataUrlToBlob(hm.png_b64);
              if (b) sectorBlobs[sid] = b;
            }
          }
        }
        if (Object.keys(sectorBlobs).length) {
          sectorHeatmapBlobs.push(sectorBlobs);
          const firstId = seclist[0] && seclist[0].sector_id != null ? String(seclist[0].sector_id) : null;
          const b0 = firstId && sectorBlobs[firstId] ? sectorBlobs[firstId] : Object.values(sectorBlobs)[0];
          heatmapBlobs.push(b0 || null);
        } else {
          sectorHeatmapBlobs.push(null);
          const pngB64 = pr.out?.heatmap?.png_b64;
          if (pngB64) {
            const blob = window.RFExportUtils.base64DataUrlToBlob(pngB64);
            heatmapBlobs.push(blob);
          } else {
            heatmapBlobs.push(null);
          }
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
      await window.RFExportUtils.createExportZip(
        fullViewWithRf,
        heatmapBlobs,
        metadata,
        filename,
        extraBlobs,
        sectorHeatmapBlobs,
      );
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

function getLocalMeshExportCenter() {
  if (currentTxLocation && Number.isFinite(currentTxLocation.lat) && Number.isFinite(currentTxLocation.lon)) {
    return { lat: currentTxLocation.lat, lon: currentTxLocation.lon };
  }
  const queued = getLastQueuedTxPoint();
  if (queued && Number.isFinite(queued.lat) && Number.isFinite(queued.lon)) {
    return { lat: queued.lat, lon: queued.lon };
  }
  return null;
}

function offsetLatLonMeters(originLatDeg, originLonDeg, eastM, northM) {
  const R = 6371000.0;
  const lat0 = Cesium.Math.toRadians(originLatDeg);
  const dLat = northM / R;
  const dLon = eastM / (R * Math.max(Math.cos(lat0), 1e-9));
  return {
    lat: originLatDeg + Cesium.Math.toDegrees(dLat),
    lon: originLonDeg + Cesium.Math.toDegrees(dLon),
  };
}

function localEnuFromOrigin(originLatDeg, originLonDeg, latDeg, lonDeg) {
  const R = 6371000.0;
  const lat0 = Cesium.Math.toRadians(originLatDeg);
  const dLat = Cesium.Math.toRadians(latDeg - originLatDeg);
  const dLon = Cesium.Math.toRadians(lonDeg - originLonDeg);
  return {
    east_m: dLon * R * Math.cos(lat0),
    north_m: dLat * R,
  };
}

async function fetchOsmBuildingsNearPoint(lat, lon, radiusM, maxCount = LOCAL_MESH_EXPORT_MAX_BUILDINGS) {
  const url = `/api/osm/buildings-near-point?lat=${encodeURIComponent(lat)}&lon=${encodeURIComponent(lon)}&radius_m=${encodeURIComponent(radiusM)}&max_count=${encodeURIComponent(maxCount)}`;
  const resp = await fetch(url);
  if (!resp.ok) {
    const txt = await resp.text();
    throw new Error(`GET ${url} failed (${resp.status}): ${txt}`);
  }
  return await resp.json();
}

function normalizeBuildingRing(geometry) {
  if (!Array.isArray(geometry)) return [];
  const ring = [];
  for (const p of geometry) {
    const lat = Number(p?.lat);
    const lon = Number(p?.lon);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
    const prev = ring[ring.length - 1];
    if (prev && Math.abs(prev.lat - lat) < 1e-12 && Math.abs(prev.lon - lon) < 1e-12) continue;
    ring.push({ lat, lon });
  }
  if (ring.length >= 2) {
    const first = ring[0];
    const last = ring[ring.length - 1];
    if (Math.abs(first.lat - last.lat) < 1e-12 && Math.abs(first.lon - last.lon) < 1e-12) {
      ring.pop();
    }
  }
  return ring.length >= 3 ? ring : [];
}

function centroidFromRing(ring) {
  if (!Array.isArray(ring) || ring.length < 3) return null;
  let sumLat = 0.0;
  let sumLon = 0.0;
  for (const p of ring) {
    sumLat += p.lat;
    sumLon += p.lon;
  }
  return { lat: sumLat / ring.length, lon: sumLon / ring.length };
}

function median(values) {
  const nums = values.filter((v) => Number.isFinite(Number(v))).map((v) => Number(v)).sort((a, b) => a - b);
  if (!nums.length) return null;
  const mid = Math.floor(nums.length / 2);
  return nums.length % 2 ? nums[mid] : 0.5 * (nums[mid - 1] + nums[mid]);
}

function slugifyBuildingLabel(text, fallback = 'building') {
  const raw = String(text || '').trim().toLowerCase();
  const slug = raw.replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
  return slug || fallback;
}

function pointInTriangle2D(p, a, b, c) {
  const area = (u, v, w) => (v.x - u.x) * (w.y - u.y) - (v.y - u.y) * (w.x - u.x);
  const s1 = area(p, a, b);
  const s2 = area(p, b, c);
  const s3 = area(p, c, a);
  const hasNeg = (s1 < -1e-9) || (s2 < -1e-9) || (s3 < -1e-9);
  const hasPos = (s1 > 1e-9) || (s2 > 1e-9) || (s3 > 1e-9);
  return !(hasNeg && hasPos);
}

function triangulateSimplePolygon(points) {
  if (!Array.isArray(points) || points.length < 3) return [];
  const signedArea = (() => {
    let acc = 0.0;
    for (let i = 0; i < points.length; i++) {
      const a = points[i];
      const b = points[(i + 1) % points.length];
      acc += a.x * b.y - b.x * a.y;
    }
    return acc * 0.5;
  })();
  const orientation = signedArea >= 0 ? 1 : -1;
  const remaining = points.map((_, i) => i);
  const triangles = [];
  let guard = 0;

  while (remaining.length > 3 && guard < points.length * points.length) {
    let clipped = false;
    for (let i = 0; i < remaining.length; i++) {
      const prevIndex = remaining[(i - 1 + remaining.length) % remaining.length];
      const currIndex = remaining[i];
      const nextIndex = remaining[(i + 1) % remaining.length];
      const prev = points[prevIndex];
      const curr = points[currIndex];
      const next = points[nextIndex];
      const cross = (curr.x - prev.x) * (next.y - prev.y) - (curr.y - prev.y) * (next.x - prev.x);
      if (orientation * cross <= 1e-9) continue;

      let containsOtherPoint = false;
      for (const candidateIndex of remaining) {
        if (candidateIndex === prevIndex || candidateIndex === currIndex || candidateIndex === nextIndex) continue;
        if (pointInTriangle2D(points[candidateIndex], prev, curr, next)) {
          containsOtherPoint = true;
          break;
        }
      }
      if (containsOtherPoint) continue;

      triangles.push([prevIndex, currIndex, nextIndex]);
      remaining.splice(i, 1);
      clipped = true;
      break;
    }
    if (!clipped) break;
    guard += 1;
  }

  if (remaining.length === 3) {
    triangles.push([remaining[0], remaining[1], remaining[2]]);
  }

  if (!triangles.length) {
    for (let i = 1; i < points.length - 1; i++) {
      triangles.push([0, i, i + 1]);
    }
  }
  return triangles;
}

function buildingToGeoJsonFeature(building) {
  const ring = normalizeBuildingRing(building?.geometry || []);
  if (ring.length < 3) return null;
  const coords = ring.map((p) => [p.lon, p.lat]);
  coords.push([ring[0].lon, ring[0].lat]);
  return {
    type: 'Feature',
    properties: {
      id: building.id ?? null,
      material: building.material ?? 'unknown',
      height_m: building.height_m ?? null,
      sampled_height_m: building.sampled_height_m ?? null,
      area_sqm: building.area_sqm ?? null,
      distance_to_point_m: building.distance_to_point_m ?? null,
      match_type: building.match_type ?? null,
      tags: building.tags || {},
    },
    geometry: {
      type: 'Polygon',
      coordinates: [coords],
    },
  };
}

function buildBuildingsGeoJsonCollection(buildings) {
  return {
    type: 'FeatureCollection',
    features: buildings.map((b) => buildingToGeoJsonFeature(b)).filter(Boolean),
  };
}

function buildBuildingHeightSampleSpec(building) {
  const ring = normalizeBuildingRing(building?.geometry || []);
  if (ring.length < 3) return null;
  const centroid = (Number.isFinite(Number(building?.centroid?.lat)) && Number.isFinite(Number(building?.centroid?.lon)))
    ? { lat: Number(building.centroid.lat), lon: Number(building.centroid.lon) }
    : centroidFromRing(ring);
  if (!centroid) return null;

  const dedupe = new Map();
  const addPoint = (arr, lat, lon) => {
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
    const key = `${lat.toFixed(8)},${lon.toFixed(8)}`;
    if (dedupe.has(key)) return;
    dedupe.set(key, true);
    arr.push({ lat, lon });
  };

  const roofPoints = [];
  addPoint(roofPoints, centroid.lat, centroid.lon);
  const supportPoints = [];
  for (let i = 0; i < ring.length; i++) {
    const curr = ring[i];
    const next = ring[(i + 1) % ring.length];
    addPoint(roofPoints, curr.lat, curr.lon);
    const mid = { lat: 0.5 * (curr.lat + next.lat), lon: 0.5 * (curr.lon + next.lon) };
    addPoint(roofPoints, mid.lat, mid.lon);
    supportPoints.push(curr, mid);
  }

  const groundPoints = [];
  for (const sample of supportPoints) {
    const local = localEnuFromOrigin(centroid.lat, centroid.lon, sample.lat, sample.lon);
    const dist = Math.hypot(local.east_m, local.north_m);
    if (!Number.isFinite(dist) || dist < 0.1) continue;
    const scale = (dist + LOCAL_MESH_EXPORT_GROUND_MARGIN_M) / dist;
    const outside = offsetLatLonMeters(centroid.lat, centroid.lon, local.east_m * scale, local.north_m * scale);
    addPoint(groundPoints, outside.lat, outside.lon);
  }

  return { ring, centroid, roofPoints, groundPoints };
}

async function sampleCesiumHeights(samples, progressLabel) {
  if (!samples.length) return [];
  const out = Array(samples.length).fill(null);
  for (let start = 0; start < samples.length; start += LOCAL_MESH_EXPORT_BATCH_SIZE) {
    const batch = samples.slice(start, start + LOCAL_MESH_EXPORT_BATCH_SIZE);
    const probes = batch.map((p) => Cesium.Cartesian3.fromDegrees(p.lon, p.lat, 2000.0));
    const clamped = await viewer.scene.clampToHeightMostDetailed(probes);
    for (let i = 0; i < batch.length; i++) {
      const hit = clamped[i];
      if (!hit) continue;
      const carto = Cesium.Cartographic.fromCartesian(hit);
      const height = Number(carto?.height);
      out[start + i] = Number.isFinite(height) ? height : null;
    }
    if (progressLabel) {
      setStatus(`${progressLabel}: sampled ${Math.min(samples.length, start + batch.length)}/${samples.length} points...`);
    }
  }
  return out;
}

function buildExtrudedBuildingPlan(building, ring, originLat, originLon) {
  const footprint = ring.map((p) => {
    const local = localEnuFromOrigin(originLat, originLon, p.lat, p.lon);
    return { x_m: local.east_m, y_m: local.north_m };
  });
  const triangles = triangulateSimplePolygon(footprint.map((p) => ({ x: p.x_m, y: p.y_m })));
  return {
    footprint,
    triangles,
  };
}

function materialColorForBuilding(material) {
  switch (String(material || 'unknown').toLowerCase()) {
    case 'wood':
      return [0.72, 0.55, 0.38];
    case 'brick':
      return [0.70, 0.29, 0.22];
    case 'concrete':
      return [0.66, 0.66, 0.66];
    case 'metal':
      return [0.58, 0.63, 0.70];
    case 'glass':
      return [0.55, 0.74, 0.86];
    default:
      return [0.78, 0.78, 0.72];
  }
}

function buildBuildingsMtl(buildings) {
  const seen = new Set();
  const lines = ['# RF Planner building export materials'];
  for (const building of buildings) {
    const material = slugifyBuildingLabel(building.material || 'unknown', 'unknown');
    if (seen.has(material)) continue;
    seen.add(material);
    const [r, g, b] = materialColorForBuilding(material);
    lines.push(`newmtl ${material}`);
    lines.push(`Kd ${r.toFixed(4)} ${g.toFixed(4)} ${b.toFixed(4)}`);
    lines.push('Ka 0.1000 0.1000 0.1000');
    lines.push('Ks 0.0500 0.0500 0.0500');
    lines.push('Ns 8.0000');
    lines.push('');
  }
  return lines.join('\n');
}

function appendBuildingObj(lines, building, vertexOffset, originHeightM, objectName) {
  const materialName = slugifyBuildingLabel(building.material || 'unknown', 'unknown');
  const footprint = building.mesh_plan?.footprint || [];
  const triangles = building.mesh_plan?.triangles || [];
  if (footprint.length < 3 || !triangles.length) return vertexOffset;

  const baseZ = Number(building.ground_height_abs_m) - originHeightM;
  const roofZ = Number(building.roof_height_abs_m) - originHeightM;
  lines.push(`o ${objectName}`);
  lines.push(`usemtl ${materialName}`);
  for (const p of footprint) {
    lines.push(`v ${p.x_m.toFixed(4)} ${p.y_m.toFixed(4)} ${baseZ.toFixed(4)}`);
  }
  for (const p of footprint) {
    lines.push(`v ${p.x_m.toFixed(4)} ${p.y_m.toFixed(4)} ${roofZ.toFixed(4)}`);
  }

  const n = footprint.length;
  for (const tri of triangles) {
    lines.push(`f ${vertexOffset + tri[2] + 1} ${vertexOffset + tri[1] + 1} ${vertexOffset + tri[0] + 1}`);
  }
  for (const tri of triangles) {
    lines.push(`f ${vertexOffset + n + tri[0] + 1} ${vertexOffset + n + tri[1] + 1} ${vertexOffset + n + tri[2] + 1}`);
  }
  for (let i = 0; i < n; i++) {
    const next = (i + 1) % n;
    const b1 = vertexOffset + i + 1;
    const b2 = vertexOffset + next + 1;
    const t1 = vertexOffset + n + i + 1;
    const t2 = vertexOffset + n + next + 1;
    lines.push(`f ${b1} ${b2} ${t2}`);
    lines.push(`f ${b1} ${t2} ${t1}`);
  }
  lines.push('');
  return vertexOffset + n * 2;
}

function buildBuildingObjText(building, originHeightM) {
  const objectName = building.file_stem;
  const lines = [
    '# RF Planner building export',
    'mtllib buildings.mtl',
    `# building_id ${building.id ?? 'unknown'}`,
    `# sampled_height_m ${Number(building.sampled_height_m || 0).toFixed(3)}`,
    '',
  ];
  appendBuildingObj(lines, building, 0, originHeightM, objectName);
  return lines.join('\n') + '\n';
}

function buildCombinedBuildingsObj(buildings, originHeightM, metadata) {
  const lines = [
    '# RF Planner building scene export',
    'mtllib buildings.mtl',
    `# center_lat ${metadata.center.lat}`,
    `# center_lon ${metadata.center.lon}`,
    `# radius_m ${metadata.radius_m}`,
    `# building_count ${buildings.length}`,
    '',
  ];
  let vertexOffset = 0;
  for (const building of buildings) {
    vertexOffset = appendBuildingObj(lines, building, vertexOffset, originHeightM, building.file_stem);
  }
  return lines.join('\n') + '\n';
}

async function exportLocalMesh() {
  const center = getLocalMeshExportCenter();
  if (!center) {
    setStatus('Select or queue a TX point first.');
    return;
  }
  if (!viewer) {
    setStatus('Cesium viewer is not initialized.');
    return;
  }

  const btn = document.getElementById('export-local-mesh-btn');
  if (btn) btn.disabled = true;
  try {
    setStatus('Exporting buildings: resolving OSM footprints...');
    const lookup = await fetchOsmBuildingsNearPoint(center.lat, center.lon, LOCAL_MESH_EXPORT_RADIUS_M, LOCAL_MESH_EXPORT_MAX_BUILDINGS);
    const rawBuildings = Array.isArray(lookup.buildings) ? lookup.buildings : [];
    if (!rawBuildings.length) {
      throw new Error('No OSM buildings were found within 50m of the selected point.');
    }

    viewer.scene.requestRender();
    await waitForNextFrame();

    const exportedBuildings = [];
    for (let i = 0; i < rawBuildings.length; i++) {
      const building = rawBuildings[i];
      const sampleSpec = buildBuildingHeightSampleSpec(building);
      if (!sampleSpec) continue;

      setStatus(`Exporting buildings: sampling ${i + 1}/${rawBuildings.length}...`);
      const [roofHeights, groundHeights] = await Promise.all([
        sampleCesiumHeights(sampleSpec.roofPoints, `Exporting buildings ${i + 1}/${rawBuildings.length} roof`),
        sampleCesiumHeights(sampleSpec.groundPoints, `Exporting buildings ${i + 1}/${rawBuildings.length} ground`),
      ]);

      const osmHeight = Number(building.height_m);
      let sampledRoofAbs = median(roofHeights);
      let sampledGroundAbs = median(groundHeights);
      let heightM = Number.isFinite(osmHeight) && osmHeight >= LOCAL_MESH_EXPORT_MIN_HEIGHT_M ? osmHeight : null;
      if (heightM == null && Number.isFinite(sampledRoofAbs) && Number.isFinite(sampledGroundAbs)) {
        heightM = sampledRoofAbs - sampledGroundAbs;
      }
      if (!Number.isFinite(heightM) || heightM < LOCAL_MESH_EXPORT_MIN_HEIGHT_M) {
        heightM = Number.isFinite(osmHeight) && osmHeight > 0.0 ? Math.max(osmHeight, LOCAL_MESH_EXPORT_MIN_HEIGHT_M) : LOCAL_MESH_EXPORT_DEFAULT_HEIGHT_M;
      }
      if (!Number.isFinite(sampledGroundAbs)) {
        sampledGroundAbs = Number.isFinite(sampledRoofAbs) ? sampledRoofAbs - heightM : 0.0;
      }
      if (!Number.isFinite(sampledRoofAbs) || sampledRoofAbs < sampledGroundAbs + LOCAL_MESH_EXPORT_MIN_HEIGHT_M) {
        sampledRoofAbs = sampledGroundAbs + heightM;
      }
      heightM = Math.max(sampledRoofAbs - sampledGroundAbs, LOCAL_MESH_EXPORT_MIN_HEIGHT_M);

      const meshPlan = buildExtrudedBuildingPlan(building, sampleSpec.ring, center.lat, center.lon);
      if (!meshPlan.footprint.length || !meshPlan.triangles.length) continue;

      const fileStem = `building_${String(exportedBuildings.length + 1).padStart(3, '0')}_id_${building.id ?? 'unknown'}`;
      exportedBuildings.push({
        ...building,
        centroid: sampleSpec.centroid,
        ground_height_abs_m: sampledGroundAbs,
        roof_height_abs_m: sampledRoofAbs,
        sampled_height_m: heightM,
        roof_sample_count: roofHeights.filter((v) => Number.isFinite(v)).length,
        ground_sample_count: groundHeights.filter((v) => Number.isFinite(v)).length,
        mesh_plan: meshPlan,
        file_stem: fileStem,
      });
    }

    if (!exportedBuildings.length) {
      throw new Error('OSM buildings were found, but none could be turned into exportable meshes.');
    }

    const originHeightM = Math.min(...exportedBuildings.map((b) => Number(b.ground_height_abs_m)).filter((v) => Number.isFinite(v)));
    const metadata = {
      exported_at_iso: new Date().toISOString(),
      center,
      radius_m: LOCAL_MESH_EXPORT_RADIUS_M,
      source: 'OSM footprints extruded with Cesium Google mesh roof/ground sampling',
      building_count: exportedBuildings.length,
      scene_origin_height_m: originHeightM,
      buildings: exportedBuildings.map((b) => ({
        id: b.id ?? null,
        file: `${b.file_stem}.obj`,
        material: b.material ?? 'unknown',
        area_sqm: b.area_sqm ?? null,
        distance_to_point_m: b.distance_to_point_m ?? null,
        match_type: b.match_type ?? null,
        osm_height_m: b.height_m ?? null,
        sampled_height_m: b.sampled_height_m ?? null,
        ground_height_abs_m: b.ground_height_abs_m ?? null,
        roof_height_abs_m: b.roof_height_abs_m ?? null,
        roof_sample_count: b.roof_sample_count ?? 0,
        ground_sample_count: b.ground_sample_count ?? 0,
        tags: b.tags || {},
      })),
    };

    const geojson = buildBuildingsGeoJsonCollection(exportedBuildings);
    const mtlText = buildBuildingsMtl(exportedBuildings);
    const combinedObjText = buildCombinedBuildingsObj(exportedBuildings, originHeightM, metadata);

    const ts = new Date();
    const stem = `building_objects_${ts.getFullYear()}-${String(ts.getMonth() + 1).padStart(2, '0')}-${String(ts.getDate()).padStart(2, '0')}_${String(ts.getHours()).padStart(2, '0')}${String(ts.getMinutes()).padStart(2, '0')}${String(ts.getSeconds()).padStart(2, '0')}`;
    if (typeof JSZip === 'undefined') {
      throw new Error('JSZip is required for building export ZIP creation.');
    }
    const zip = new JSZip();
    zip.file('buildings_scene.obj', combinedObjText);
    zip.file('buildings.mtl', mtlText);
    zip.file('metadata.json', JSON.stringify(metadata, null, 2));
    zip.file('osm_buildings.geojson', JSON.stringify(geojson, null, 2));
    for (const building of exportedBuildings) {
      zip.file(`${building.file_stem}.obj`, buildBuildingObjText(building, originHeightM));
    }
    const blob = await zip.generateAsync({ type: 'blob' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${stem}.zip`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(a.href);

    const first = exportedBuildings[0];
    setStatus(`Exported building ZIP. Buildings=${exportedBuildings.length} First building id=${first.id ?? 'unknown'} Height=${Number(first.sampled_height_m || 0).toFixed(1)}m`);
  } catch (err) {
    console.error('Building export failed:', err);
    setStatus(`Building export failed: ${err}`);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function clearMap() {
  clearOverlay();
  clearRaytraceOverlay();
  try { localRtState.buildingsCache.clear(); } catch { /* ignore */ }
  if (rxEntity) {
    try { viewer.entities.remove(rxEntity); } catch {}
    rxEntity = null;
  }
  if (rxCaptureEntity) {
    try { viewer.entities.remove(rxCaptureEntity); } catch {}
    rxCaptureEntity = null;
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

    // Draw polygon planning shapes explicitly, but do not present them as hard RF boundaries.
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

    if (s.sector_type === "360") {
      // Do not draw an omnidirectional footprint overlay.
      continue;
    }

    const derivedSpan = en > st ? (en - st) : (360 - st + en);
    const beamwidthH = Number.isFinite(s.beamwidth_h_deg) ? s.beamwidth_h_deg : derivedSpan;
    const azimuth = Number.isFinite(s.azimuth_deg) ? s.azimuth_deg : ((st + derivedSpan / 2.0) % 360.0);
    const markerLength = Math.min(60.0, Math.max(20.0, radiusM * 0.06));
    const center = toLatLon(azimuth, markerLength);

    const centerLine = viewer.entities.add({
      polyline: {
        positions: [
          Cesium.Cartesian3.fromDegrees(txLon, txLat, 5.0),
          Cesium.Cartesian3.fromDegrees(center.lon, center.lat, 5.0),
        ],
        width: 3.0,
        material: Cesium.Color.WHITE.withAlpha(0.85),
        clampToGround: true,
      },
    });
    sectorEntities.push(centerLine);

    // PCI: only show when explicitly provided (0 is valid; do not use placeholder "0" when unknown).
    if (typeof s.pci === "number" && Number.isFinite(s.pci) && s.pci >= 0 && s.pci <= 1007) {
      const pciLabel = viewer.entities.add({
        position: Cesium.Cartesian3.fromDegrees(center.lon, center.lat, 12.0),
        label: {
          text: `PCI ${s.pci}`,
          font: "12px system-ui, sans-serif",
          fillColor: Cesium.Color.WHITE,
          outlineColor: Cesium.Color.BLACK,
          outlineWidth: 2,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
          heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
          showBackground: true,
          backgroundColor: Cesium.Color.BLACK.withAlpha(0.45),
        },
      });
      sectorEntities.push(pciLabel);
    }
  }
}

let _ensureProfilesInProgress = false;
let _ensureProfilesStartedAt = 0;
async function ensureProfiles(txLat, txLon) {
  // Reset stuck guard if previous attempt has been running for over 60s.
  if (_ensureProfilesInProgress && (Date.now() - _ensureProfilesStartedAt) > 60000) {
    _ensureProfilesInProgress = false;
  }
  if (_ensureProfilesInProgress) return;
  _ensureProfilesInProgress = true;
  _ensureProfilesStartedAt = Date.now();
  try {
    return await _ensureProfilesImpl(txLat, txLon);
  } finally {
    _ensureProfilesInProgress = false;
  }
}
async function _ensureProfilesImpl(txLat, txLon) {
  const txHeightM = getNumber("tx-height-m", 10.0);
  const rxHeightM = getNumber("rx-height-m", 1.5);
  const maxRangeM = getNumber("max-range", 2500.0);
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
    existingViewer: viewer,
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

function buildPlanRequestBody(lat, lon, rayMode, txHeightM, rxHeightM, sectors) {
  const body = {
    lat,
    lon,
    freq_mhz: getNumber("freq-mhz", 3500.0),
    tx_power_dbm: getNumber("tx-power-dbm", 43.0),
    noise_figure_db: getNumber("noise-figure-db", 7.0),
    subcarrier_spacing_khz: getNumber("scs-khz", 30.0),
    num_resource_blocks: Math.round(getNumber("num-rb", 100)),
    channel_bandwidth_mhz: getNumber("bw-mhz", 40.0),
    tx_antenna_gain_dbi: getNumber("tx-antenna-gain-dbi", 17.0),
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
    planner_surface: "3d",
    tx_height_m: txHeightM,
    rx_height_m: rxHeightM,

    // Coverage/grid overrides (must stay in sync with 3D mesh-profile params)
    max_range_m: getNumber("max-range", 2500.0),
    step_m: getNumber("dr-m", 5.0),
    dtheta_deg: getNumber("dtheta", 5.0),

    // In-browser Plan already applies the JSON response; skip server publish to avoid double-apply with poll.
    publish_ui: false,
    ...(typeof RFTerrainParams !== "undefined" ? RFTerrainParams.getTerrainPlanParams() : {}),
  };
  const waveformFields = window.RFWaveformUI
    ? window.RFWaveformUI.buildPlanFields({ lat, lon, txHeightM, sectors })
    : { technology: "5g_nr", waveform: "5g_nr" };
  Object.assign(body, waveformFields);
  window.RFWaveformUI?.sanitizePlanBody(body);
  return body;
}

async function applyPlanResponseToViewer(out, {
  requestLat,
  requestLon,
  rayMode: rayModeOpt,
  refreshStreetLabelsOnSuccess = true,
  statusPrefix = "",
  attemptText = "",
} = {}) {
  if (!out || typeof out !== "object") throw new Error("applyPlanResponseToViewer: invalid plan payload");
  syncMeshProfileInputsFromPlan(out);

  if (out.mode === "3d_rt" && out.raytrace && typeof out.raytrace === "object") {
    const txLat = out.snapped_tx?.lat ?? out.original_point?.lat ?? requestLat;
    const txLon = out.snapped_tx?.lon ?? out.original_point?.lon ?? requestLon;
    const rx = out.rx_point;
    if (!Number.isFinite(txLat) || !Number.isFinite(txLon)) throw new Error("applyPlanResponseToViewer: 3d_rt payload missing TX coordinates");
    if (!rx || !Number.isFinite(rx.lat) || !Number.isFinite(rx.lon)) throw new Error("applyPlanResponseToViewer: 3d_rt payload missing rx_point");
    const rmEl = document.getElementById("ray-mode");
    if (rmEl) rmEl.value = "3d_rt";
    currentTxLocation = { lat: txLat, lon: txLon };
    currentRxLocation = { lat: rx.lat, lon: rx.lon };
    clearOverlay();
    clearRaytraceOverlay();
    updateRxMarker(rx.lat, rx.lon);
    registerPlannedTxFromPlan(out, txLat, txLon);
    if (Array.isArray(out.rx_sites) && out.rx_sites.length) addPlannedRxSiteMarkers(out.rx_sites);
    syncRxManualFields(rx.lat, rx.lon);
    const summary = drawRaytracePathsFromPayload(out.raytrace);
    if (refreshStreetLabelsOnSuccess) queueStreetLabelRefresh(true);
    setStatus(`${statusPrefix}${attemptText}: 3D RT applied. Multipath polylines drawn: ${summary.drawnCount} (API paths: ${summary.rawCount}).`);
    console.info("RFPlanner3D apply 3d_rt", { rawCount: summary.rawCount, drawnCount: summary.drawnCount, payload: out });
    recordPlanResultForExport(out, txLat, txLon);
    return { lat: txLat, lon: txLon };
  }

  let lat = requestLat;
  let lon = requestLon;
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
    if (out.original_point && Number.isFinite(out.original_point.lat) && Number.isFinite(out.original_point.lon)) {
      lat = out.original_point.lat;
      lon = out.original_point.lon;
    } else if (out.snapped_tx && Number.isFinite(out.snapped_tx.lat) && Number.isFinite(out.snapped_tx.lon)) {
      lat = out.snapped_tx.lat;
      lon = out.snapped_tx.lon;
    }
  }
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) throw new Error("applyPlanResponseToViewer: need lat/lon in payload");

  const rayMode = String(rayModeOpt || out.ray_mode || getString("ray-mode", "3d_osm")).toLowerCase();
  const isRtMode = rayMode === "3d_rt" || rayMode === "3d_rt_google" || rayMode === "3d_rt_osm";

  registerPlannedTxFromPlan(out, lat, lon);
  if (Array.isArray(out.rx_sites) && out.rx_sites.length) addPlannedRxSiteMarkers(out.rx_sites);

  if (out.grid) {
    const covLayer = typeof RFTerrainParams !== "undefined" ? RFTerrainParams.getCoverageDisplayLayer() : "rsrp";
    const isGoogleMesh = false;
    if (isRtMode) {
      const layerHeatmap = pickHeatmapForLayer(out, covLayer);
      const sectorHm = covLayer === "rsrp" ? out.heatmap_by_sector : null;
      if (layerHeatmap && layerHeatmap.png_b64) {
        await renderHeatmapDrapeOsm3d(layerHeatmap, out.grid, sectorHm);
      } else {
        try {
          await renderDrapedSurfaceCoverage(out.grid);
        } catch (e) {
          console.warn("Surface drape failed, falling back to point grid:", e);
          renderGridCoverage(out.grid);
        }
      }
    } else if (isGoogleMesh) {
      const layerHeatmap = pickHeatmapForLayer(out, covLayer);
      const sectorHm = covLayer === "rsrp" ? out.heatmap_by_sector : null;
      if (layerHeatmap && layerHeatmap.png_b64) {
        await renderHeatmapDrapeOsm3d(layerHeatmap, out.grid, sectorHm);
      } else {
        try {
          await renderDrapedSurfaceCoverage(out.grid);
        } catch (e) {
          console.warn("Surface drape failed, falling back to point grid:", e);
          renderGridCoverage(out.grid);
        }
      }
    } else {
      const drew = await renderOsmPlanHeatmapOn3d(out, covLayer);
      if (!drew) {
        const msg = "Plan has no heatmap PNG for this display layer — re-run Plan RF Queue.";
        console.warn("RFPlanner3D:", msg, { covLayer, rayMode, out });
        setStatus(`${msg} See browser console.`);
      } else if (String(out.ray_mode || "").toLowerCase() === "2d") {
        console.warn(
          "RFPlanner3D: plan ray_mode=2d on /3d — draw uses 3D OSM PNG drape but propagation was 2D. "
          + "Re-run with planner_surface=3d or ray_mode=3d_osm.",
        );
      }
    }
  }

  const radiusM = getNumber("max-range", 2500.0);
  const secLat = (out.snapped_tx && Number.isFinite(out.snapped_tx.lat))
    ? out.snapped_tx.lat
    : (out.original_point && Number.isFinite(out.original_point.lat) ? out.original_point.lat : NaN);
  const secLon = (out.snapped_tx && Number.isFinite(out.snapped_tx.lon))
    ? out.snapped_tx.lon
    : (out.original_point && Number.isFinite(out.original_point.lon) ? out.original_point.lon : NaN);
  if (!window.RFWaveformUI?.isDvt() && out.sectors && out.sectors.length && Number.isFinite(secLat) && Number.isFinite(secLon)) {
    drawSectorOverlays(out.sectors, secLat, secLon, radiusM);
  }

  if (currentTxLocation && currentRxLocation && isRtMode) {
    try {
      await renderRaytraceOverlay(currentTxLocation.lat, currentTxLocation.lon, currentRxLocation.lat, currentRxLocation.lon);
    } catch (e) {
      console.warn("Failed to render raytrace overlay:", e);
    }
  }

  const key = out.mesh_profile_key ? `\nmesh_key=${out.mesh_profile_key}` : "";
  if (refreshStreetLabelsOnSuccess) queueStreetLabelRefresh(true);
  setStatus(`${statusPrefix}${attemptText}: plan complete.${key}`);
  console.info("RFPlanner3D apply plan", { rayMode, payload: out });

  recordPlanResultForExport(out, lat, lon);
  window._lastPlanResult = out;

  return (out.snapped_tx && Number.isFinite(out.snapped_tx.lat) && Number.isFinite(out.snapped_tx.lon))
    ? { lat: out.snapped_tx.lat, lon: out.snapped_tx.lon }
    : { lat, lon };
}

function startRemotePlanRfPolling() {
  let lastSeq = 0;
  let lastScreenshotSeq = 0;
  try {
    const raw = sessionStorage.getItem(REMOTE_PLAN_SEQ_STORAGE_KEY);
    if (raw) lastSeq = Math.max(0, Number(raw) || 0);
  } catch { /* ignore */ }

  const tick = async () => {
    try {
      const r = await fetch(`/api/ui/remote-plan-rf/poll?since_seq=${encodeURIComponent(String(lastSeq))}`);
      if (!r.ok) return;
      const j = await r.json();
      let storedBoot = "";
      const boot = String(j.server_boot_utc || "");
      try {
        storedBoot = sessionStorage.getItem(REMOTE_PLAN_BOOT_STORAGE_KEY) || "";
      } catch { /* ignore */ }
      if (storedBoot && boot && storedBoot !== boot) {
        try {
          sessionStorage.setItem(REMOTE_PLAN_BOOT_STORAGE_KEY, boot);
          lastSeq = 0;
          sessionStorage.removeItem(REMOTE_PLAN_SEQ_STORAGE_KEY);
        } catch { /* ignore */ }
        return;
      }
      if (boot && !storedBoot) {
        try {
          sessionStorage.setItem(REMOTE_PLAN_BOOT_STORAGE_KEY, boot);
          lastSeq = 0;
          sessionStorage.removeItem(REMOTE_PLAN_SEQ_STORAGE_KEY);
        } catch { /* ignore */ }
        return;
      }
      // Screenshot capture request — checked BEFORE the j.new gate so it fires
      // even when no new plan result has been produced (export-zip independent of plan seq).
      if (j.screenshot_request && typeof j.screenshot_request === "object" && Number(j.screenshot_request.seq) > lastScreenshotSeq) {
        lastScreenshotSeq = Number(j.screenshot_request.seq);
        try {
          const withRf = await captureViewBlob(true);
          const withoutRf = await captureViewBlob(false);
          await fetch("/api/ui/upload-screenshot?filename=full_view.png", { method: "POST", body: withRf, headers: { "Content-Type": "image/png" } });
          await fetch("/api/ui/upload-screenshot?filename=full_view_without_rf.png", { method: "POST", body: withoutRf, headers: { "Content-Type": "image/png" } });
        } catch (e) {
          console.warn("Screenshot capture for export failed:", e);
        }
        return;
      }

      // Clear-map command — before j.new gate (independent of plan seq).
      if (j.clear_map && typeof j.clear_map === "object" && Number(j.clear_map.seq) > lastSeq) {
        lastSeq = Number(j.clear_map.seq);
        try { sessionStorage.setItem(REMOTE_PLAN_SEQ_STORAGE_KEY, String(lastSeq)); } catch { /* ignore */ }
        clearMap();
        return;
      }

      // Profile generation request — before j.new gate (server blocks waiting for these).
      if (j.profile_request && typeof j.profile_request === "object" && j.profile_request.lat != null) {
        const pr = j.profile_request;
        setStatus(`Generating Google mesh profiles for server request…`);
        try {
          setInputValue("tx-height-m", pr.tx_height_m ?? 10.0);
          setInputValue("rx-height-m", pr.rx_height_m ?? 1.5);
          setInputValue("max-range", pr.max_range_m ?? 2000.0);
          setInputValue("dr-m", pr.dr_m ?? 5.0);
          setInputValue("dtheta", pr.dtheta_deg ?? 5.0);
          await ensureProfiles(pr.lat, pr.lon);
          setStatus("Mesh profiles generated — server will proceed with plan.");
        } catch (e) {
          setStatus(`Failed to generate mesh profiles: ${e}`);
        }
        return;
      }

      const seq = Number(j.seq) || 0;
      if (!j.new || seq <= lastSeq) return;
      lastSeq = seq;
      try {
        sessionStorage.setItem(REMOTE_PLAN_SEQ_STORAGE_KEY, String(lastSeq));
      } catch { /* ignore */ }

      if (j.status === "ready" && j.plan && typeof j.plan === "object") {
        const plan = j.plan;
        const surface = String(plan.planner_surface || "").toLowerCase();
        const rm = String(plan.ray_mode || "").toLowerCase();
        if (surface === "2d" || (surface !== "3d" && rm === "2d")) {
          console.info("RFPlanner3D remote poll skip (2D plan)", { seq, ray_mode: rm, planner_surface: surface });
          return;
        }
        console.info("RFPlanner3D remote poll apply", { seq, plan });
        await applyPlanResponseToViewer(j.plan, { refreshStreetLabelsOnSuccess: true, statusPrefix: "Remote" });
      } else if (j.status === "error" && j.error) {
        const er = j.error;
        const detail = er.detail != null ? (typeof er.detail === "object" ? JSON.stringify(er.detail) : String(er.detail)) : JSON.stringify(er);
        setStatus(`Remote Plan RF failed (HTTP ${er.status_code ?? "?"}): ${detail}`);
      }
    } catch { /* transient */ }
  };

  setInterval(tick, REMOTE_PLAN_POLL_MS);
  tick();
}

async function applyRemote3dRtCommand(cmd) {
  const seq = Number(cmd?.__seq);
  const report = async (status, extra = {}) => {
    if (!Number.isFinite(seq)) return;
    try {
      await fetch("/api/ui/remote-3d-rt/report", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ seq, status, ...extra }),
      });
    } catch { /* ignore */ }
  };
  const t0 = performance.now();
  await report("started", { detail: "Remote 3D RT command accepted by /3d client" });
  try {
  const txLat = Number(cmd?.tx_lat);
  const txLon = Number(cmd?.tx_lon);
  const rxLat = Number(cmd?.rx_lat);
  const rxLon = Number(cmd?.rx_lon);
  if (!Number.isFinite(txLat) || !Number.isFinite(txLon) || !Number.isFinite(rxLat) || !Number.isFinite(rxLon)) {
    throw new Error("remote 3D RT command missing valid tx/rx coordinates");
  }
  const txHeightM = Number.isFinite(Number(cmd?.tx_height_m)) ? Number(cmd.tx_height_m) : 10.0;
  const rxHeightM = Number.isFinite(Number(cmd?.rx_height_m)) ? Number(cmd.rx_height_m) : 1.5;
  const txGround = await terrainHeightAtLatLon(txLat, txLon);
  const rxGround = await terrainHeightAtLatLon(rxLat, rxLon);
  currentTxLocation = { lat: txLat, lon: txLon, ground_h: txGround };
  currentRxLocation = { lat: rxLat, lon: rxLon, ground_h: rxGround };
  setInputValue("tx-height-m", txHeightM);
  setInputValue("rx-height-m", rxHeightM);
  setInputValue("rt-h-spread-deg", Number.isFinite(Number(cmd?.h_spread_deg)) ? Number(cmd.h_spread_deg) : 30.0);
  setInputValue("rt-v-spread-deg", Number.isFinite(Number(cmd?.v_spread_deg)) ? Number(cmd.v_spread_deg) : 18.0);
  setInputValue("rt-ray-count", Number.isFinite(Number(cmd?.ray_count)) ? Number(cmd.ray_count) : 240);
  setInputValue("rt-max-bounces", Number.isFinite(Number(cmd?.max_bounces)) ? Number(cmd.max_bounces) : 3);
  setInputValue("rt-rx-radius-m", Number.isFinite(Number(cmd?.rx_radius_m)) ? Number(cmd.rx_radius_m) : 10.0);
  setInputValue("rt-max-distance-m", Number.isFinite(Number(cmd?.max_distance_m)) ? Number(cmd.max_distance_m) : 800.0);
  const rayModeEl = document.getElementById("ray-mode");
  const cmdRayMode = String(cmd?.ray_mode || "3d_rt").toLowerCase();
  if (rayModeEl) rayModeEl.value = cmdRayMode;
  const showRaysEl = document.getElementById("show-raytrace-toggle");
  if (showRaysEl) showRaysEl.checked = !!cmd?.show_rays;
  updateTxMarker(txLat, txLon, txGround + txHeightM);
  updateRxMarker(rxLat, rxLon, rxGround + rxHeightM);
  syncRxManualFields(rxLat, rxLon);

  let yawDeg = Number(cmd?.yaw_deg);
  let pitchDeg = Number(cmd?.pitch_deg);
  if (!Number.isFinite(yawDeg) || !Number.isFinite(pitchDeg)) {
    const steerLat = Number(cmd?.steer_lat);
    const steerLon = Number(cmd?.steer_lon);
    const steerHeightM = Number.isFinite(Number(cmd?.steer_height_m)) ? Number(cmd.steer_height_m) : 0.0;
    if (!Number.isFinite(steerLat) || !Number.isFinite(steerLon)) {
      throw new Error("remote 3D RT command missing valid yaw/pitch or steer target");
    }
    const txWorld = rtWorldFromGeo(getCurrentTxWorldPoint());
    const steerGround = await terrainHeightAtLatLon(steerLat, steerLon);
    const targetWorld = rtWorldFromGeo({ lat: steerLat, lon: steerLon, h: steerGround + steerHeightM });
    const worldVec = Cesium.Cartesian3.subtract(targetWorld, txWorld, new Cesium.Cartesian3());
    const localDir = rtLocalDirectionFromWorld(txWorld, worldVec);
    yawDeg = Cesium.Math.toDegrees(Math.atan2(localDir.x, localDir.z));
    const horiz = Math.hypot(localDir.x, localDir.z);
    pitchDeg = Cesium.Math.toDegrees(Math.atan2(localDir.y, Math.max(horiz, 1e-9)));
  }
  setInputValue("rt-yaw-deg", yawDeg.toFixed(2));
  setInputValue("rt-pitch-deg", pitchDeg.toFixed(2));
  updateRtHeadingEntity();
  setRtStatus(`Remote 3D RT (${cmdRayMode}): TX/RX placed. Yaw=${yawDeg.toFixed(1)}°, pitch=${pitchDeg.toFixed(1)}°. Launching…`);
  let launchResult = null;
  if (!!cmd?.show_rays) {
    launchResult = await launchLocal3dRaytrace();
  }
  const durationMs = performance.now() - t0;
  await report("completed", {
    detail: `Remote 3D RT finished in ${durationMs.toFixed(1)} ms`,
    duration_ms: durationMs,
    result: launchResult || {},
  });
  } catch (e) {
    const durationMs = performance.now() - t0;
    await report("error", {
      detail: String(e),
      duration_ms: durationMs,
    });
    throw e;
  }
}

function startRemote3dRtPolling() {
  let lastSeq = 0;
  try {
    const raw = sessionStorage.getItem(REMOTE_3D_RT_SEQ_STORAGE_KEY);
    if (raw) lastSeq = Math.max(0, Number(raw) || 0);
  } catch { /* ignore */ }

  const tick = async () => {
    try {
      const r = await fetch(`/api/ui/remote-3d-rt/poll?since_seq=${encodeURIComponent(String(lastSeq))}`);
      if (!r.ok) return;
      const j = await r.json();
      let storedBoot = "";
      const boot = String(j.server_boot_utc || "");
      try {
        storedBoot = sessionStorage.getItem(REMOTE_3D_RT_BOOT_STORAGE_KEY) || "";
      } catch { /* ignore */ }
      if (storedBoot && boot && storedBoot !== boot) {
        try {
          sessionStorage.setItem(REMOTE_3D_RT_BOOT_STORAGE_KEY, boot);
          lastSeq = 0;
          sessionStorage.removeItem(REMOTE_3D_RT_SEQ_STORAGE_KEY);
        } catch { /* ignore */ }
        return;
      }
      if (boot && !storedBoot) {
        try {
          sessionStorage.setItem(REMOTE_3D_RT_BOOT_STORAGE_KEY, boot);
          lastSeq = 0;
          sessionStorage.removeItem(REMOTE_3D_RT_SEQ_STORAGE_KEY);
        } catch { /* ignore */ }
        return;
      }
      const seq = Number(j.seq) || 0;
      if (!j.new || seq <= lastSeq) return;
      lastSeq = seq;
      try {
        sessionStorage.setItem(REMOTE_3D_RT_SEQ_STORAGE_KEY, String(lastSeq));
      } catch { /* ignore */ }
      if (j.status === "ready" && j.command && typeof j.command === "object") {
        await applyRemote3dRtCommand({ ...j.command, __seq: seq });
      } else if (j.status === "error" && j.error) {
        const er = j.error;
        const detail = er.detail != null ? (typeof er.detail === "object" ? JSON.stringify(er.detail) : String(er.detail)) : JSON.stringify(er);
        setStatus(`Remote 3D RT failed (HTTP ${er.status_code ?? "?"}): ${detail}`);
      }
    } catch { /* transient */ }
  };

  setInterval(tick, REMOTE_PLAN_POLL_MS);
  tick();
}

async function runPlanForTx(lat, lon, {
  queueIndex = 1,
  total = 1,
  attempt = 1,
  refreshStreetLabelsOnSuccess = true,
} = {}) {
  const rayMode = getString("ray-mode", "3d_osm").toLowerCase();
  const txHeightM = getNumber("tx-height-m", 10.0);
  const rxHeightM = getNumber("rx-height-m", 1.5);
  const sectors = window.RFWaveformUI?.isDvt() ? [] : collectSectorConfigs();
  const prefix = total > 1 ? `TX ${queueIndex}/${total}` : "TX";
  const attemptText = attempt > 1 ? ` (retry ${attempt - 1})` : "";

  currentTxLocation = { lat, lon };
  updateTxMarker(lat, lon);

  const demSource = getString("dem-source", "opentopodata");
  const needsMeshProfiles = rayMode === "3d_rt" || rayMode === "3d_rt_google" || rayMode === "3d_rt_osm" || demSource === "copernicus_mesh";
  if (needsMeshProfiles) {
    try {
      setStatus(`${prefix}${attemptText}: checking cached 3D ray profiles…`);
      await ensureProfiles(lat, lon);
    } catch (e) {
      setStatus(`${prefix}${attemptText}: failed to build mesh profiles.\n\n${e}`);
      return { ok: false, error: String(e), cacheCenter: { lat, lon } };
    }
  }

  setStatus(`${prefix}${attemptText}: running RF planning…`);

  let body;
  try {
    body = buildPlanRequestBody(lat, lon, rayMode, txHeightM, rxHeightM, sectors);
  } catch (e) {
    const error = `Invalid waveform/transmitter configuration: ${e.message || e}`;
    setStatus(`${prefix}${attemptText}: ${error}`);
    return { ok: false, error, cacheCenter: { lat, lon } };
  }

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

  if (rayMode === "3d_rt" || rayMode === "3d_rt_google" || rayMode === "3d_rt_osm") applyManualRxFromInputsIfNeeded();
  const cacheCenter = await applyPlanResponseToViewer(out, {
    requestLat: lat,
    requestLon: lon,
    rayMode,
    refreshStreetLabelsOnSuccess,
    statusPrefix: prefix,
    attemptText,
  });
  return { ok: true, out, cacheCenter };
}

function isPropagationRtLaunchMode(rayMode) {
  const m = String(rayMode || "").toLowerCase();
  return m === "3d_rt" || m === "3d_rt_google" || m === "3d_rt_osm";
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
  const retryRadiusM = getNumber("max-range", 2500.0);
  const rayMode = getString("ray-mode", "3d_osm").toLowerCase();
  const rtQueue = isPropagationRtLaunchMode(rayMode);
  let rtQueueRanLaunches = false;

  try {
    if (rtQueue) {
      applyManualRxFromInputsIfNeeded();
      if (!currentRxLocation || !Number.isFinite(currentRxLocation.lat) || !Number.isFinite(currentRxLocation.lon)) {
        setStatus("Propagation is RT: set RX (SHIFT+click the mesh or enter RX lat/lon), then click Plan RF Queue.");
      } else {
        rtQueueRanLaunches = true;
        for (let i = 0; i < queue.length; i++) {
          const point = queue[i];
          const prefix = queue.length > 1 ? `TX ${i + 1}/${queue.length}: ` : "";
          currentTxLocation = { lat: point.lat, lon: point.lon };
          const txHeightM = getNumber("tx-height-m", 10.0);
          updateTxMarker(point.lat, point.lon, Number(currentTxLocation.ground_h || 0.0) + txHeightM);
          updateRtHeadingEntity();
          applyManualRxFromInputsIfNeeded();
          try {
            const summary = await launchLocal3dRaytrace();
            if (summary) {
              successes.push({ lat: point.lat, lon: point.lon });
              planResults.push({
                lat: point.lat,
                lon: point.lon,
                out: { raytrace_queue_launch: true, summary },
                cacheCenter: { lat: point.lat, lon: point.lon },
              });
              setStatus(`${prefix}Ray trace launched.`);
            } else {
              failed.push(point);
            }
          } catch (err) {
            setStatus(`${prefix}Ray trace failed: ${err}`);
            failed.push(point);
          }
          if (i < queue.length - 1) {
            setStatus(`TX ${i + 1}/${queue.length}: done. Waiting ${Math.round(MULTI_TX_PULL_DELAY_MS / 1000)}s…`);
            await sleep(MULTI_TX_PULL_DELAY_MS);
          }
        }
      }
    } else {
      for (let i = 0; i < queue.length; i++) {
        const point = queue[i];
        const result = await runPlanForTx(point.lat, point.lon, {
          queueIndex: i + 1,
          total: queue.length,
          refreshStreetLabelsOnSuccess: false,
        });
        if (result?.ok) {
          successes.push(result.cacheCenter);
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
    }

    if (rtQueue && rtQueueRanLaunches) {
      const totalSuccess = successes.length;
      const totalFailed = failed.length;
      if (totalSuccess && totalFailed) {
        setStatus(`Ray queue: ${totalSuccess}/${queue.length} TX launched, ${totalFailed} failed or skipped.`);
      } else if (totalSuccess) {
        setStatus(`Ray queue: all ${totalSuccess} TX point(s) launched.`);
      } else {
        setStatus("Ray queue: no launches succeeded. Check TX/RX and RT controls.");
      }
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
  if (rmEl) {
    const configuredMode = String(cfg.default_ray_mode || "").toLowerCase();
    // Keep /3d defaulting to OSM-only unless an explicit 3D mode is configured.
    rmEl.value = (configuredMode === "3d" || configuredMode === "3d_osm" || configuredMode === "3d_rt" || configuredMode === "3d_rt_google" || configuredMode === "3d_rt_osm")
      ? configuredMode
      : "3d_osm";
  }

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
    // Interactive RT needs a responsive loaded scene more than max visual fidelity.
    if ("dynamicScreenSpaceError" in tileset) tileset.dynamicScreenSpaceError = true;
    if ("dynamicScreenSpaceErrorFactor" in tileset) tileset.dynamicScreenSpaceErrorFactor = 24.0;
    if ("dynamicScreenSpaceErrorDensity" in tileset) tileset.dynamicScreenSpaceErrorDensity = 2.0e-4;
    if ("skipLevelOfDetail" in tileset) tileset.skipLevelOfDetail = true;
    if ("baseScreenSpaceError" in tileset) tileset.baseScreenSpaceError = 1024;
    if ("skipScreenSpaceErrorFactor" in tileset) tileset.skipScreenSpaceErrorFactor = 16;
    if ("skipLevels" in tileset) tileset.skipLevels = 1;
    if ("immediatelyLoadDesiredLevelOfDetail" in tileset) tileset.immediatelyLoadDesiredLevelOfDetail = false;
    if ("loadSiblings" in tileset) tileset.loadSiblings = false;
    if ("preferLeaves" in tileset) tileset.preferLeaves = true;
    if ("progressiveResolutionHeightFraction" in tileset) tileset.progressiveResolutionHeightFraction = 0.5;
    if ("foveatedScreenSpaceError" in tileset) tileset.foveatedScreenSpaceError = true;
    if ("foveatedConeSize" in tileset) tileset.foveatedConeSize = 0.3;
    if ("foveatedMinimumScreenSpaceErrorRelaxation" in tileset) tileset.foveatedMinimumScreenSpaceErrorRelaxation = 0.0;
    if ("maximumScreenSpaceError" in tileset) tileset.maximumScreenSpaceError = 32;
    googleTileset = tileset;
    googleMeshTileset = tileset;
    attachGoogleTileInspectionHook(tileset);
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

  viewer.screenSpaceEventHandler.setInputAction(async (click) => {
    const cartesian = await pickGoogleMeshPointFromScreen(click.position, buildRaytraceExclusionList());
    if (!cartesian) return;

    const carto = Cesium.Cartographic.fromCartesian(cartesian);
    const lat = Cesium.Math.toDegrees(carto.latitude);
    const lon = Cesium.Math.toDegrees(carto.longitude);
    const groundH = Number.isFinite(Number(carto.height)) ? Number(carto.height) : 0.0;

    if (polygonDrawingMode) {
      addPolygonVertex(lat, lon);
      return;
    }

    if (localRtState.clickMode === "rx") {
      currentRxLocation = { ...(currentRxLocation || {}), lat, lon, ground_h: groundH };
      updateRxMarker(lat, lon, groundH + getNumber("rx-height-m", 1.5));
      syncRxManualFields(lat, lon);
      setRtStatus(`RX placed at ${lat.toFixed(6)}, ${lon.toFixed(6)} (ground ${groundH.toFixed(1)} m).`);
      return;
    }

    if (localRtState.clickMode === "steer") {
      if (!currentTxLocation) {
        setRtStatus("Place TX before steering it.");
        return;
      }
      const txWorld = rtWorldFromGeo(getCurrentTxWorldPoint());
      const targetWorld = rtWorldFromGeo({ lat, lon, h: groundH });
      const worldVec = Cesium.Cartesian3.subtract(targetWorld, txWorld, new Cesium.Cartesian3());
      if (Cesium.Cartesian3.magnitudeSquared(worldVec) < 1e-6) {
        setRtStatus("Steer target is too close to TX. Click farther away on the mesh.");
        return;
      }
      const localDir = rtLocalDirectionFromWorld(txWorld, worldVec);
      const yawDeg = Cesium.Math.toDegrees(Math.atan2(localDir.x, localDir.z));
      const horiz = Math.hypot(localDir.x, localDir.z);
      const pitchDeg = Cesium.Math.toDegrees(Math.atan2(localDir.y, Math.max(horiz, 1e-9)));
      setInputValue("rt-yaw-deg", yawDeg.toFixed(2));
      setInputValue("rt-pitch-deg", pitchDeg.toFixed(2));
      updateRtHeadingEntity();
      setRtStatus(`TX steering set from click. Yaw=${yawDeg.toFixed(1)}°, pitch=${pitchDeg.toFixed(1)}°.`);
      return;
    }

    currentTxLocation = { ...(currentTxLocation || {}), lat, lon, ground_h: groundH };
    const queueCount = appendTxInputPoint(lat, lon);
    updateTxMarker(lat, lon, groundH + getNumber("tx-height-m", 10.0));
    queueStreetLabelRefresh(true);
    setStatus(`TX added (${queueCount} queued):
  lat=${lat}
  lon=${lon}

Click Plan RF Queue.`);
    setRtStatus(`TX placed at ${lat.toFixed(6)}, ${lon.toFixed(6)} (ground ${groundH.toFixed(1)} m).`);
    updateRtHeadingEntity();
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

  // SHIFT+click remains a quick RX shortcut for 3D RT.
  viewer.screenSpaceEventHandler.setInputAction(async (click) => {
    const cartesian = await pickGoogleMeshPointFromScreen(click.position, buildRaytraceExclusionList());
    if (!cartesian) return;

    const carto = Cesium.Cartographic.fromCartesian(cartesian);
    const lat = Cesium.Math.toDegrees(carto.latitude);
    const lon = Cesium.Math.toDegrees(carto.longitude);
    const groundH = Number.isFinite(Number(carto.height)) ? Number(carto.height) : 0.0;

    currentRxLocation = { ...(currentRxLocation || {}), lat, lon, ground_h: groundH };
    updateRxMarker(lat, lon, groundH + getNumber("rx-height-m", 1.5));
    syncRxManualFields(lat, lon);
    setStatus(`RX set (SHIFT+click):
  lat=${lat}
  lon=${lon}`);
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK, Cesium.KeyboardEventModifier.SHIFT);


  // Wire UI
  document.getElementById("coord-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    await runPlan();
  });
  document.getElementById("plan-json-file")?.addEventListener("change", async (ev) => {
    const f = ev.target.files?.[0];
    if (!f) return;
    try {
      const raw = await f.text();
      const ta = document.getElementById("plan-json-import");
      if (ta) ta.value = raw;
      setStatus(`Loaded ${f.name} into plan JSON box — click Apply plan JSON to map.`);
    } catch (err) {
      setStatus(`Failed to read file: ${err}`);
    }
    ev.target.value = "";
  });
  document.getElementById("plan-json-apply-btn")?.addEventListener("click", async () => {
    const raw = document.getElementById("plan-json-import")?.value?.trim();
    if (!raw) {
      setStatus("Paste plan JSON or choose a .json file first.");
      return;
    }
    let j;
    try {
      j = JSON.parse(raw);
    } catch (err) {
      setStatus(`Invalid JSON: ${err}`);
      return;
    }
    if (j && typeof j === "object" && j.plan && !j.grid) j = j.plan;
    try {
      setStatus("Applying plan JSON to viewer…");
      await applyPlanResponseToViewer(j, { refreshStreetLabelsOnSuccess: true });
    } catch (err) {
      setStatus(`Apply failed: ${err}`);
    }
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
  document.getElementById("rt-place-tx-btn")?.addEventListener("click", () => setRtClickMode("tx"));
  document.getElementById("rt-place-rx-btn")?.addEventListener("click", () => setRtClickMode("rx"));
  document.getElementById("rt-steer-btn")?.addEventListener("click", () => setRtClickMode("steer"));
  document.getElementById("rt-autolock-btn")?.addEventListener("click", () => autoLockRtSteering().catch((e) => setRtStatus(`Auto lock failed: ${e}`)));
  document.getElementById("rt-launch-btn")?.addEventListener("click", () => launchLocal3dRaytrace().catch((e) => setRtStatus(`3D RT launch failed: ${e}`)));
  document.getElementById("rt-clear-btn")?.addEventListener("click", () => {
    clearRaytraceOverlay({ clearOsmRtBuildings: false });
    updateRtHeadingEntity();
  });
  document.getElementById("rt-load-osm-layout-btn")?.addEventListener("click", () => {
    loadOsmRtBuildingsLayoutOnly().catch((e) => setRtStatus(`Load layout: ${e}`));
  });
  document.getElementById("show-osm-rt-buildings-toggle")?.addEventListener("change", (e) => {
    if (!e.target.checked) {
      clearOsmRtBuildingOverlay();
      setRtStatus("OSM preview hidden. Google mesh ray tracing continues unchanged.");
      return;
    }
    if (osmRtBuildingEntities.length) {
      for (const ent of osmRtBuildingEntities) ent.show = true;
      setRtStatus("OSM preview re-enabled from cached overlay. Use Load OSM preview to fetch nearby footprints.");
    } else {
      setRtStatus("OSM preview enabled. Click Load OSM preview if you want optional footprint shells; Google mesh tracing does not use OSM.");
    }
  });
  for (const id of ["rt-yaw-deg", "rt-pitch-deg", "tx-height-m", "rx-height-m", "rt-h-spread-deg", "rt-v-spread-deg", "rt-ray-count", "rt-max-bounces", "rt-rx-radius-m", "rt-max-distance-m"]) {
    document.getElementById(id)?.addEventListener("input", () => {
      if (currentTxLocation) updateTxMarker(currentTxLocation.lat, currentTxLocation.lon, Number(currentTxLocation.ground_h || 0.0) + getNumber("tx-height-m", 10.0));
      if (currentRxLocation) updateRxMarker(currentRxLocation.lat, currentRxLocation.lon, Number(currentRxLocation.ground_h || 0.0) + getNumber("rx-height-m", 1.5));
      updateRtHeadingEntity();
    });
  }
  document.getElementById("ray-mode")?.addEventListener("change", () => {
    updateRtControlsVisibility();
    clearRaytraceOverlay();
    updateRtHeadingEntity();
  });
  document.getElementById("coverage-display-layer")?.addEventListener("change", async () => {
    if (!window._lastPlanResult || !window._lastPlanResult.grid) return;
    clearCoverageOverlay();
    await applyPlanResponseToViewer(window._lastPlanResult, {
      refreshStreetLabelsOnSuccess: false,
      statusPrefix: "Layer",
    });
  });
  document.getElementById("export-zip-btn")?.addEventListener("click", () => exportCurrentView());
  document.getElementById("export-local-mesh-btn")?.addEventListener("click", () => exportLocalMesh());
  document.getElementById("clear-map-btn").addEventListener("click", () => clearMap());
  document.getElementById("import-zip-input")?.addEventListener("change", async (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    setStatus("Importing ZIP…");
    try {
      const resp = await fetch("/api/import-zip", { method: "POST", body: file, headers: { "Content-Type": "application/zip" } });
      const j = await resp.json();
      if (!resp.ok) throw new Error(j.detail || resp.statusText);
      setStatus(`Imported: TX (${j.tx_lat?.toFixed(5)}, ${j.tx_lon?.toFixed(5)}). Drawing…`);
    } catch (err) {
      setStatus(`Import failed: ${err}`);
    }
    e.target.value = "";
  });
  await loadRfParamsDefaults();
  window.RFWaveformUI?.init({
    onChange({ technology }) {
      const rayMode = document.getElementById("ray-mode");
      if (rayMode) {
        for (const option of rayMode.options) {
          const mode = String(option.value || "").toLowerCase();
          if (mode === "3d_rt" || mode === "3d_rt_google" || mode === "3d_rt_osm") {
            option.disabled = technology === "dvt";
          }
        }
        if (technology === "dvt" && String(rayMode.value || "").toLowerCase() !== "3d_osm") {
          rayMode.value = "3d_osm";
        }
      }
      updateRtControlsVisibility();
      if (technology === "dvt") {
        setMeshStatus("DVT selected. Large-area planning uses 3D OSM; Google-mesh RT modes are disabled.");
      } else {
        setMeshStatus("");
      }
    },
  });
  updateTxInputSummary();
  updateRtControlsVisibility();
  setRtClickMode("tx");
  updateRtHeadingEntity();
  queueStreetLabelRefresh(true);
  startRemotePlanRfPolling();
  startRemote3dRtPolling();
  if (window.RFAddressLookup) {
    window.RFAddressLookup.bindAddressLookup({
      setStatus,
      onNavigate(lat, lon, result, mode) {
        flyToQueuedPoints([{ lat, lon }]);
        if (mode === "set_tx") {
          appendTxInputPoint(lat, lon);
          currentTxLocation = { lat, lon };
          updateTxMarker(lat, lon, getNumber("tx-height-m", 10.0));
        }
        setStatus(`At ${result.formatted_address || `${lat}, ${lon}`}`);
      },
    });
  }
  setStatus("Paste one or more TX coordinates, or click on the 3D mesh to append them, then click Plan RF Queue.");
}

window.RFPlanner3DApplyPlan = applyPlanResponseToViewer;

init().catch((e) => setStatus(`Init failed:
${e}`));
