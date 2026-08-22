// src/agentic_rf_planner/ui/static/app.js

const map = L.map("map", {
  zoomControl: true,
  attributionControl: true
}).setView([37.7749, -122.4194], 13); // SF default

// Add distance scale control (metric)
// Note: Leaflet's scale uses Web Mercator projection, which has distance distortion
// especially at higher latitudes. The scale is accurate at the map center.
const scaleControl = L.control.scale({
  metric: true,
  imperial: false,
  position: 'bottomleft',
  maxWidth: 200  // Maximum width of the scale bar in pixels
}).addTo(map);

// Verify scale accuracy: Leaflet calculates scale based on map center
// For Web Mercator, scale is most accurate at the equator and has ~1/cos(lat) distortion
// At SF latitude (~37.77°N), distortion factor ≈ 1/cos(37.77°) ≈ 1.26 (26% longer)
// This means distances appear ~26% longer than they actually are at this latitude
// The scale bar accounts for this by showing the projected distance, not true distance

// Bing Aerial satellite imagery
// Convert x/y/z to Bing QuadKey for proper tile access
function getBingQuadKey(x, y, z) {
  let quadKey = '';
  for (let i = z; i > 0; i--) {
    let digit = 0;
    const mask = 1 << (i - 1);
    if ((x & mask) !== 0) digit++;
    if ((y & mask) !== 0) digit += 2;
    quadKey += digit;
  }
  return quadKey;
}

// Bing Aerial tile layer with QuadKey conversion
L.TileLayer.BingAerial = L.TileLayer.extend({
  getTileUrl: function(coords) {
    const quadKey = getBingQuadKey(coords.x, coords.y, coords.z);
    const subdomain = (coords.x + coords.y + coords.z) % 4;
    return `https://ecn.t${subdomain}.tiles.virtualearth.net/tiles/a${quadKey}.jpeg?g=1`;
  }
});

L.tileLayer.bingAerial = function() {
  return new L.TileLayer.BingAerial(null, {
    attribution: '&copy; <a href="https://www.microsoft.com/maps/">Bing Maps</a>',
    maxZoom: 21,
    tileSize: 256,
  });
};

// Use Bing Aerial satellite imagery
L.tileLayer.bingAerial().addTo(map);

let currentLayerGroup = L.layerGroup().addTo(map);
let sectorLayerGroup = L.layerGroup().addTo(map); // Separate layer for sectors (can be toggled)
let isacProbeLayerGroup = L.layerGroup().addTo(map); // Selected target geometry + iso-range ellipse
let heatmapLayerGroups = []; // Array to store multiple heatmap layer groups (one per RF plan)
let planResults = []; // Successful RF plan results for export (lat, lon, data)

// Expose for F12 console debugging (use window.RFPLANNER_DEBUG to avoid cache/scope issues)
window.RFPLANNER_DEBUG = {
  get currentLayerGroup() { return currentLayerGroup; },
  get sectorLayerGroup() { return sectorLayerGroup; },
  get isacProbeLayerGroup() { return isacProbeLayerGroup; },
  get heatmapLayerGroups() { return heatmapLayerGroups; },
  get currentHeatmapLayerGroup() { return window.currentHeatmapLayerGroup; },
};
const statusEl = document.getElementById("status");
const MULTI_TX_PULL_DELAY_MS = 1500;
let isPlanningQueue = false;

// State for polygon drawing
let currentTxLocation = null; // {lat, lon} - set when TX is selected/typed
let polygonDrawingMode = null; // {sectorId, points: [[lat, lon], ...], polygonLayer: L.polygon}
let polygonMarkers = []; // Markers for polygon points
let restorePromptState = null; // { data, ageMinutes }

function setStatus(msg) {
  statusEl.textContent = msg;
}

function getSavedPlanSnapshot() {
  try {
    const savedData = localStorage.getItem('rf_planning_last_result');
    const savedTimestamp = localStorage.getItem('rf_planning_timestamp');
    if (!savedData || !savedTimestamp) return null;
    const data = JSON.parse(savedData);
    const ageMinutes = (Date.now() - parseInt(savedTimestamp, 10)) / (1000 * 60);
    if (!Number.isFinite(ageMinutes) || ageMinutes < 0) return null;
    return { data, ageMinutes };
  } catch (e) {
    console.warn("[RF Planner] Failed to inspect saved session:", e);
    return null;
  }
}

function removeRestorePrompt() {
  const existing = document.getElementById("restore-session-prompt");
  if (existing) existing.remove();
}

function restoreSavedPlan(data, ageMinutes) {
  removeLegacyCurrentLayerCircles();
  removeRestorePrompt();

  heatmapLayerGroups.forEach(layerGroup => map.removeLayer(layerGroup));
  heatmapLayerGroups = [];

  const restoredHeatmapLayerGroup = L.layerGroup().addTo(map);
  heatmapLayerGroups.push(restoredHeatmapLayerGroup);
  window.currentHeatmapLayerGroup = restoredHeatmapLayerGroup;

  window._lastPlanResult = data;
  renderHeatmap(data);

  if (data.osm_buildings_for_client && typeof window.rf2dIngestPlannerOsm === "function") {
    try {
      window.rf2dIngestPlannerOsm(data.osm_buildings_for_client);
    } catch (e) {
      console.warn("[RF Planner] rf2dIngestPlannerOsm (restore):", e);
    }
  }

  if (data.clutter_type || data.world_model_source) {
    displayMetadata(data);
  }

  if (data.panorama_image) {
    displayPanorama(data.panorama_image, data.panorama_location);
  }

  if (data.snapped_tx) {
    const tx = data.snapped_tx;
    L.marker([tx.lat, tx.lon], {
      icon: L.divIcon({ className: "click-marker", html: "📍", iconSize: [20, 20] })
    })
      .addTo(currentLayerGroup)
      .bindPopup(`TX: ${tx.lat}, ${tx.lon}`);
  }

  const clat = data.snapped_tx?.lat ?? data.original_point?.lat;
  const clon = data.snapped_tx?.lon ?? data.original_point?.lon;
  if (Number.isFinite(clat) && Number.isFinite(clon)) {
    planResults = [{ lat: clat, lon: clon, out: data, data, cacheCenter: { lat: clat, lon: clon } }];
  }

  setStatus(`Restored previous RF plan (${ageMinutes.toFixed(1)} min old). Click map or paste TX coordinates to queue another plan.`);
}

function showRestorePrompt(snapshot) {
  removeRestorePrompt();
  restorePromptState = snapshot;
  if (!snapshot) return;

  const sidebar = document.getElementById("sidebar");
  const anchor = document.getElementById("status");
  if (!sidebar || !anchor) return;

  const prompt = document.createElement("div");
  prompt.id = "restore-session-prompt";
  prompt.style.cssText = "margin-top:8px; padding:8px; background:#1f2937; border:1px solid #374151; border-radius:4px; font-size:11px; color:#e5e7eb;";
  prompt.innerHTML = `
    <div style="margin-bottom:6px;"><strong>Restore last session?</strong> Saved ${snapshot.ageMinutes.toFixed(1)} min ago.</div>
    <div style="display:flex; gap:6px;">
      <button type="button" id="restore-session-yes" style="flex:1; padding:6px; background:#2563eb; color:#fff; border:none; border-radius:3px; cursor:pointer; font-size:11px;">Restore</button>
      <button type="button" id="restore-session-no" style="flex:1; padding:6px; background:#4b5563; color:#fff; border:none; border-radius:3px; cursor:pointer; font-size:11px;">Dismiss</button>
    </div>
  `;
  anchor.insertAdjacentElement("afterend", prompt);

  document.getElementById("restore-session-yes")?.addEventListener("click", () => {
    if (!restorePromptState) return;
    restoreSavedPlan(restorePromptState.data, restorePromptState.ageMinutes);
    restorePromptState = null;
  });
  document.getElementById("restore-session-no")?.addEventListener("click", () => {
    restorePromptState = null;
    removeRestorePrompt();
    setStatus("Click on the map or enter coordinates to run RF planning.");
  });
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
  setStatus("Error: Enter one or more TX coordinates or click on the map to add them.");
  return null;
}

function haversineDistanceM(lat1, lon1, lat2, lon2) {
  const r = 6371000.0;
  const p1 = (lat1 * Math.PI) / 180.0;
  const p2 = (lat2 * Math.PI) / 180.0;
  const dLat = ((lat2 - lat1) * Math.PI) / 180.0;
  const dLon = ((lon2 - lon1) * Math.PI) / 180.0;
  const a = Math.sin(dLat / 2) ** 2
    + Math.cos(p1) * Math.cos(p2) * Math.sin(dLon / 2) ** 2;
  return 2 * r * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function flyToQueuedPoints(queue) {
  if (!queue || queue.length === 0) return;
  const FLY_TO_NEAR_THRESHOLD_M = 50000; // 50km - if points farther apart, center on first only
  let maxDist = 0;
  for (let i = 0; i < queue.length; i++) {
    for (let j = i + 1; j < queue.length; j++) {
      const d = haversineDistanceM(queue[i].lat, queue[i].lon, queue[j].lat, queue[j].lon);
      if (d > maxDist) maxDist = d;
    }
  }
  if (queue.length === 1 || maxDist > FLY_TO_NEAR_THRESHOLD_M) {
    const p = queue[0];
    map.setView([p.lat, p.lon], 15);
  } else {
    const bounds = L.latLngBounds(queue.map(p => [p.lat, p.lon]));
    map.fitBounds(bounds, { padding: [50, 50] });
  }
}

// Remove any legacy heatmap circles that were drawn into currentLayerGroup (e.g. from old loadLastResults)
function removeLegacyCurrentLayerCircles() {
  currentLayerGroup.getLayers()
    .filter(layer => layer instanceof L.Circle)
    .forEach(layer => currentLayerGroup.removeLayer(layer));
}

// Inspect saved results on page load, but never auto-restore them.
function loadLastResults() {
  removeLegacyCurrentLayerCircles();
  const snapshot = getSavedPlanSnapshot();
  if (!snapshot) return false;
  console.log(`[RF Planner] Found saved results (${snapshot.ageMinutes.toFixed(1)} minutes old)`);
  showRestorePrompt(snapshot);
  setStatus(`Saved RF plan found (${snapshot.ageMinutes.toFixed(1)} min old). Choose whether to restore it.`);
  return true;
}

function update2dRtPanelVisibility() {
  const sec = document.getElementById("rt-controls-section");
  const rm = document.getElementById("ray-mode");
  if (!sec || !rm) return;
  sec.style.display = rm.value === "3d_rt_osm" ? "" : "none";
}

function updateRayModeHelperText() {
  const sel = document.getElementById("ray-mode");
  const el = document.getElementById("mesh-profile-status");
  if (!el) return;
  const m = sel ? String(sel.value || "2d").toLowerCase() : "2d";
  if (m === "3d_rt_osm") {
    el.textContent =
      "2D OSM RT: in-map ray fan. Plan RF Queue runs rays (not the coverage grid). Use /3d for Google mesh or 3D OSM-only modes.";
  } else {
    el.textContent = "2D coverage uses OSM footprints. For 3D or Google mesh, open the /3d planner.";
  }
  update2dRtPanelVisibility();
}

// Initialize Ray Mode selector on page load (loadLastResults is called once in the main DOMContentLoaded below)
window.addEventListener('DOMContentLoaded', () => {
  fetch("/api/config")
    .then((r) => (r.ok ? r.json() : null))
    .then((cfg) => {
      if (!cfg) return;
      const mode = String(cfg.default_ray_mode || "2d").toLowerCase();
      const sel = document.getElementById("ray-mode");
      if (!sel) return;
      // This page only exposes 2D and 2D OSM RT; map legacy server defaults onto those.
      if (mode === "3d_rt_osm") sel.value = "3d_rt_osm";
      else sel.value = "2d";
    })
    .catch((e) => console.warn("[RF Planner] Failed to load /api/config for default ray mode:", e))
    .finally(() => updateRayModeHelperText());
});

// Extract RF planning logic into reusable function

async function ensure3DMeshProfiles(lat, lng, txHeightM, rxHeightM, maxRangeM, drM, dthetaDeg) {
  // Auto-generate and persist mesh contacts (first-time only).
  // Requires GOOGLE_MAPS_API_KEY on the server environment.
  setStatus("3D: generating Google-mesh ray profiles (first time only)…");
  const mod = await import("/mesh_profiler_core.js");
  await mod.buildAndUploadProfiles({
    containerId: "cesiumProfilerHost",
    lat: lat,
    lon: lng,
    txHeightM: txHeightM,
    rxHeightM: rxHeightM,
    maxRangeM: maxRangeM,
    drM: drM,
    dthetaDeg: dthetaDeg,
    mode: "slice",
    onProgress: (msg) => setStatus(msg),
  });
}

function applyServerPlanTo2dView(data, { requestLat, requestLng, statusMessage } = {}) {
  const fromLat = Number.isFinite(requestLat)
    ? requestLat
    : (data.original_point && Number.isFinite(data.original_point.lat) ? data.original_point.lat : null);
  const fromLng = Number.isFinite(requestLng)
    ? requestLng
    : (data.original_point && Number.isFinite(data.original_point.lon) ? data.original_point.lon : null);

  if (data.snapped_tx) {
    const snapped = data.snapped_tx;
    const snapDist = data.snap_distance_m || 0;
    const svAvailable = data.streetview_available || false;
    const vlmUsed = data.vlm_used || false;
    const worldSource = data.world_model_source || "unknown";
    const clutterType = data.clutter_type || "unknown";

    let popupHtml = `Snapped to street<br>Distance: ${snapDist.toFixed(1)}m<br>`;
    popupHtml += `Clutter: ${clutterType}<br>`;
    popupHtml += `Model: ${worldSource === "geometry_vlm_refined" ? "Geometry + VLM" : "Geometry only"}<br>`;
    popupHtml += `Street View: ${svAvailable ? "✓ Available" : "✗ Not available"}`;

    L.marker([snapped.lat, snapped.lon], {
      icon: L.divIcon({ className: "snapped-marker", html: "📍", iconSize: [24, 24] }),
    })
      .addTo(currentLayerGroup)
      .bindPopup(popupHtml);

    if (Number.isFinite(fromLat) && Number.isFinite(fromLng)) {
      L.polyline([[fromLat, fromLng], [snapped.lat, snapped.lon]], {
        color: "yellow",
        weight: 2,
        dashArray: "5, 5",
      }).addTo(currentLayerGroup);
    }

    let statusMsg = `Snapped: ${snapDist.toFixed(1)}m. Clutter: ${clutterType}. `;
    if (vlmUsed) {
      statusMsg += "Model: Geometry + VLM. ";
    } else {
      statusMsg += "Model: Geometry only. ";
    }
    statusMsg += `Street View: ${svAvailable ? "Available" : "Not available"}. Computing RF...`;
    setStatus(statusMsg);
  }

  displayMetadata(data);

  if (data.panorama_image) {
    displayPanorama(data.panorama_image, data.panorama_location);
  }

  renderHeatmap(data);
  window._lastPlanResult = data;

  if (data.osm_buildings_for_client && typeof window.rf2dIngestPlannerOsm === "function") {
    try {
      window.rf2dIngestPlannerOsm(data.osm_buildings_for_client);
      console.log("[RF Planner] Reused planner OSM footprints for 2D ray tracer (shared cache).");
    } catch (e) {
      console.warn("[RF Planner] rf2dIngestPlannerOsm:", e);
    }
  }

  try {
    const forStorage = { ...data };
    delete forStorage.osm_buildings_for_client;
    localStorage.setItem("rf_planning_last_result", JSON.stringify(forStorage));
    localStorage.setItem("rf_planning_timestamp", Date.now().toString());
    console.log("[RF Planner] Results saved to localStorage");
  } catch (e) {
    console.warn("[RF Planner] Failed to save to localStorage:", e);
  }

  if (statusMessage) {
    setStatus(statusMessage);
  } else {
    setStatus(
      "RF plan computed. Click another point or enter coordinates to re-run. (Results saved - will persist after refresh)"
    );
  }
}

async function runRFPlanning(lat, lng, source = "click") {
  removeLegacyCurrentLayerCircles();
  console.log("=".repeat(60));
  console.log(`[RF Planner] RF PLANNING REQUEST (source: ${source})`);
  console.log(`  Coordinates: ${lat}, ${lng}`);
  console.log("=".repeat(60));
  
  // Set TX location (origin for polygon sectors)
  currentTxLocation = { lat, lon: lng };
  console.log(`[RF Planner] TX location set: ${lat}, ${lng} (origin for polygon sectors)`);
  
  setStatus(`Selected: ${lat}, ${lng}. Processing... (this may take 20-30 minutes for full analysis with LOS detection)`);
  let plannerProgressStopped = true;
  let plannerProgressTimer = null;
  const stopPlannerProgressPolling = () => {
    plannerProgressStopped = true;
    if (plannerProgressTimer != null) {
      window.clearInterval(plannerProgressTimer);
      plannerProgressTimer = null;
    }
  };
  
  // Only clear markers/overlays from currentLayerGroup (keep heatmaps from previous plans in batch)
  if (window.txMarker) {
    currentLayerGroup.removeLayer(window.txMarker);
  }
  currentLayerGroup.eachLayer((layer) => {
    if (layer instanceof L.Marker || layer instanceof L.Polyline) {
      if (!(layer instanceof L.Circle)) {
        currentLayerGroup.removeLayer(layer);
      }
    }
  });
  
  // Create new layer group for this RF plan's heatmap
  const newHeatmapLayerGroup = L.layerGroup().addTo(map);
  heatmapLayerGroups.push(newHeatmapLayerGroup);
  window.currentHeatmapLayerGroup = newHeatmapLayerGroup;

  // Navigate map to coordinates
  map.setView([lat, lng], 15);

  // Show selected point (directional TX when ray UI is loaded)
  if (typeof window.rf2dSetTxFromPlanner === "function") {
    window.rf2dSetTxFromPlanner(lat, lng);
  } else {
    window.txMarker = L.marker([lat, lng], { icon: L.divIcon({ className: "click-marker", html: "📍", iconSize: [20, 20] }) })
      .addTo(currentLayerGroup)
      .bindPopup("TX Location");
  }

  console.log(`[RF Planner] Selected point: ${lat}, ${lng}`);
  
  try {
    // If there's an active polygon drawing, finish it first
    if (polygonDrawingMode && polygonDrawingMode.points.length >= 2) {
      console.log(`[RF Planner] Finishing active polygon drawing before planning...`);
      finishPolygonDrawing();
    }
    
    // DVT uses the transmitter antenna pattern; NR alone uses sector objects.
    const sectors = window.RFWaveformUI?.isDvt() ? [] : collectSectorConfigs();
    
    console.log(`[RF Planner] Collected ${sectors.length} sector(s) from UI`);
    if (sectors.length > 0) {
      sectors.forEach((s, idx) => {
        console.log(`  Sector ${idx + 1}: ${s.sector_id}, type=${s.sector_type}`);
        if (s.sector_type === "polygon") {
          console.log(`    Polygon points: ${s.polygon_points ? s.polygon_points.length : 0} points`);
        } else if (s.sector_type === "angle") {
          console.log(`    Angles: ${s.start_angle_deg}° to ${s.end_angle_deg}°`);
        }
      });
    } else {
      console.log(`[RF Planner] No sectors found - will use omnidirectional (360°)`);
    }
    
    // Ray mode + heights (UI)
    const rayModeEl = document.getElementById('ray-mode');
    const txHeightEl = document.getElementById('tx-height-m');
    const rxHeightEl = document.getElementById('rx-height-m');
    const rayMode = rayModeEl ? String(rayModeEl.value || '2d') : '2d';
    const txHeightM = txHeightEl ? parseFloat(txHeightEl.value || '0') : 0.0;
    const rxHeightM = rxHeightEl ? parseFloat(rxHeightEl.value || '1.5') : 1.5;
    const electricalTiltEl = document.getElementById('electrical-tilt-deg');
    const mechanicalTiltEl = document.getElementById('mechanical-tilt-deg');
    const verticalBeamwidthEl = document.getElementById('vertical-beamwidth-deg');
    const maxVerticalAttenEl = document.getElementById('max-vertical-atten-db');
    const pathLossModelEl = document.getElementById('path-loss-model');
    const propagationScenarioEl = document.getElementById('propagation-scenario');
    const maxHorizontalAttenEl = document.getElementById('max-horizontal-atten-db');
    const frontToBackAttenEl = document.getElementById('front-to-back-atten-db');
    const shadowLossEl = document.getElementById('shadow-loss-db');
    const shadowSlopeEl = document.getElementById('shadow-slope-db-per-100m');
    const shadowLossCapEl = document.getElementById('shadow-loss-cap-db');
    const diffractionBaseEl = document.getElementById('diffraction-base-loss-db');
    const diffractionSlopeEl = document.getElementById('diffraction-slope-db-per-100m');
    const diffractionLossCapEl = document.getElementById('diffraction-loss-cap-db');
    const canyonRecoveryMaxEl = document.getElementById('canyon-recovery-max-db');
    const canyonRecoverySlopeEl = document.getElementById('canyon-recovery-slope-db-per-100m');
    const terminationRsrpEl = document.getElementById('termination-rsrp-dbm');
    const bldgConcreteEl = document.getElementById('bldg-concrete');
    const bldgBrickEl = document.getElementById('bldg-brick');
    const bldgWoodEl = document.getElementById('bldg-wood');
    const bldgGlassEl = document.getElementById('bldg-glass');
    const bldgMetalEl = document.getElementById('bldg-metal');
    const bldgUnknownEl = document.getElementById('bldg-unknown');
    const bldgScaleEl = document.getElementById('bldg-scale');
    const bldgReductionEl = document.getElementById('bldg-reduction-db');
    const statusEl = document.getElementById('mesh-profile-status');
    const freqEl = document.getElementById('freq-mhz');
    const txPowerEl = document.getElementById('tx-power-dbm');
    const noiseFigureEl = document.getElementById('noise-figure-db');
    const scsEl = document.getElementById('scs-khz');
    const numRbEl = document.getElementById('num-rb');
    const bwEl = document.getElementById('bw-mhz');
    const mimoModeEl = document.getElementById('mimo-mode');
    const linkAdaptEl = document.getElementById('link-adapt');

    // RF parameters
    const rfParams = {
      lat: lat,
      lon: lng,
      freq_mhz: freqEl ? parseFloat(freqEl.value || '3500') : 3500.0,
      tx_power_dbm: txPowerEl ? parseFloat(txPowerEl.value || '43') : 43.0,
      noise_figure_db: noiseFigureEl ? parseFloat(noiseFigureEl.value || '7.0') : 7.0,
      subcarrier_spacing_khz: scsEl ? parseFloat(scsEl.value || '30') : 30.0,
      num_resource_blocks: numRbEl ? Math.round(parseFloat(numRbEl.value || '100')) : 100,
      channel_bandwidth_mhz: bwEl ? parseFloat(bwEl.value || '40') : 40.0,
      num_tx_antennas: 1,
      num_rx_antennas: 1,
      mimo_mode: mimoModeEl ? String(mimoModeEl.value || 'MIMO') : 'MIMO',
      enable_link_adaptation: linkAdaptEl ? (linkAdaptEl.value === '1') : true,
      fixed_modulation: null,
      electrical_tilt_deg: electricalTiltEl ? parseFloat(electricalTiltEl.value || '0.0') : 0.0,
      mechanical_tilt_deg: mechanicalTiltEl ? parseFloat(mechanicalTiltEl.value || '0.0') : 0.0,
      vertical_beamwidth_deg: verticalBeamwidthEl ? parseFloat(verticalBeamwidthEl.value || '8.0') : 8.0,
      max_vertical_attenuation_db: maxVerticalAttenEl ? parseFloat(maxVerticalAttenEl.value || '30.0') : 30.0,
      path_loss_model: pathLossModelEl ? String(pathLossModelEl.value || '3gpp_38901') : '3gpp_38901',
      propagation_scenario: propagationScenarioEl ? String(propagationScenarioEl.value || 'umi_street_canyon') : 'umi_street_canyon',
      max_horizontal_attenuation_db: maxHorizontalAttenEl ? parseFloat(maxHorizontalAttenEl.value || '30.0') : 30.0,
      front_to_back_attenuation_db: frontToBackAttenEl ? parseFloat(frontToBackAttenEl.value || '25.0') : 25.0,
      shadow_loss_db: shadowLossEl ? parseFloat(shadowLossEl.value || '6.0') : 6.0,
      shadow_decay_db_per_100m: shadowSlopeEl ? parseFloat(shadowSlopeEl.value || '4.0') : 4.0,
      diffraction_base_loss_db: diffractionBaseEl ? parseFloat(diffractionBaseEl.value || '6.0') : 6.0,
      diffraction_slope_db_per_100m: diffractionSlopeEl ? parseFloat(diffractionSlopeEl.value || '3.0') : 3.0,
      canyon_recovery_max_db: canyonRecoveryMaxEl ? parseFloat(canyonRecoveryMaxEl.value || '8.0') : 8.0,
      canyon_recovery_slope_db_per_100m: canyonRecoverySlopeEl ? parseFloat(canyonRecoverySlopeEl.value || '6.0') : 6.0,
      termination_rsrp_dbm: terminationRsrpEl ? parseFloat(terminationRsrpEl.value || '-140.0') : -140.0,
      shadow_loss_cap_db: shadowLossCapEl ? parseFloat(shadowLossCapEl.value || '20.0') : 20.0,
      diffraction_loss_cap_db: diffractionLossCapEl ? parseFloat(diffractionLossCapEl.value || '16.0') : 16.0,
      building_attenuation: {
        materials: {
          concrete: bldgConcreteEl ? parseFloat(bldgConcreteEl.value || '5.0') : 5.0,
          brick: bldgBrickEl ? parseFloat(bldgBrickEl.value || '3.5') : 3.5,
          wood: bldgWoodEl ? parseFloat(bldgWoodEl.value || '1.25') : 1.25,
          glass: bldgGlassEl ? parseFloat(bldgGlassEl.value || '2.75') : 2.75,
          metal: bldgMetalEl ? parseFloat(bldgMetalEl.value || '14.75') : 14.75,
          unknown: bldgUnknownEl ? parseFloat(bldgUnknownEl.value || '5.0') : 5.0,
        },
        overall: {
          scale: bldgScaleEl ? parseFloat(bldgScaleEl.value || '0.75') : 0.75,
          reduction_db: bldgReductionEl ? parseFloat(bldgReductionEl.value || '6.0') : 6.0,
        },
      },
      sectors: sectors.length > 0 ? sectors : null,  // null = omnidirectional
      // Ray propagation: only geometry differs between 2D and 3D
      ray_mode: rayMode,
      planner_surface: "2d",
      tx_height_m: txHeightM,
      rx_height_m: rxHeightM,
      publish_ui: false,
      ...(window.RFTerrainParams ? RFTerrainParams.getTerrainPlanParams() : {}),
    };

    const waveformFields = window.RFWaveformUI
      ? window.RFWaveformUI.buildPlanFields({ lat, lon: lng, txHeightM, sectors })
      : { technology: "5g_nr", waveform: "5g_nr" };
    Object.assign(rfParams, waveformFields);
    window.RFWaveformUI?.sanitizePlanBody(rfParams);
    
    // If 3D mode is selected, ensure a persisted mesh profile exists for this TX/config.
    // This avoids a slow fallback and makes behavior explicit.
    if (rayMode.toLowerCase() === '3d' || rayMode.toLowerCase() === '3d_rt') {
      const maxRangeM = 2000.0;
      const drM = 5.0;
      const dthetaDeg = 5.0;
      const hasUrl = `/api/mesh-profiles/has?tx_lat=${encodeURIComponent(lat)}&tx_lon=${encodeURIComponent(lng)}`
        + `&tx_height_m=${encodeURIComponent(txHeightM)}&rx_height_m=${encodeURIComponent(rxHeightM)}`
        + `&max_range_m=${encodeURIComponent(maxRangeM)}&dr_m=${encodeURIComponent(drM)}&dtheta_deg=${encodeURIComponent(dthetaDeg)}`;
      try {
        const hasResp = await fetch(hasUrl);
        const hasJson = await hasResp.json();
        if (!hasJson.exists) {
          await ensure3DMeshProfiles(lat, lng, txHeightM, rxHeightM, maxRangeM, drM, dthetaDeg);
        }
      } catch (e) {
        console.warn('[RF Planner] 3D profile generation failed:', e);
        document.getElementById('status').textContent = `3D mode failed to generate mesh profiles: ${e}`;
        return { ok: false, error: String(e), cacheCenter: { lat, lon: lng } };
      }
    }

    console.log(`[RF Planner] Sending POST /api/plan...`);
    console.log(`[RF Planner] Request body:`, JSON.stringify(rfParams, null, 2));

    let plannerDiagBaselineSeq = 0;
    try {
      const plannerStateResp = await fetch("/api/debug/planner-state");
      if (plannerStateResp.ok) {
        const plannerState = await plannerStateResp.json();
        plannerDiagBaselineSeq = Number(plannerState?.seq) || 0;
      }
    } catch (e) {
      console.warn("[RF Planner] Failed to read planner-state baseline:", e);
    }

    plannerProgressStopped = false;
    const pollPlannerProgress = async () => {
      if (plannerProgressStopped) return;
      try {
        const resp = await fetch("/api/debug/planner-state");
        if (!resp.ok) return;
        const diag = await resp.json();
        const seq = Number(diag?.seq) || 0;
        const last = diag?.last || null;
        if (seq <= plannerDiagBaselineSeq || !last) return;
        const phase = String(last.phase || "processing");
        const detail = String(last.detail || "working");
        setStatus(`Planning… ${phase}${detail ? ` — ${detail}` : ""}`);
      } catch {
        // Ignore polling failures; the main plan request remains authoritative.
      }
    };
    plannerProgressTimer = window.setInterval(() => {
      void pollPlannerProgress();
    }, 1500);
    void pollPlannerProgress();
    
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 1800000); // 30 minute timeout (backend can take 20-30 min for 7200 cells with Phase 1 LOS detection)
    
    const resp = await fetch("/api/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(rfParams),
      signal: controller.signal,
    });
    
    clearTimeout(timeoutId);
    stopPlannerProgressPolling();
    
    console.log(`[RF Planner] Response status: ${resp.status}`);
    console.log(`[RF Planner] Response headers:`, Object.fromEntries(resp.headers.entries()));

    if (!resp.ok) {
      let errorMsg = `Error ${resp.status}`;
      let errorData = null;
      try {
        errorData = await resp.json();
        errorMsg = errorData.detail || errorData.message || errorMsg;
        console.error("[RF Planner] Error response JSON:", errorData);
      } catch (e) {
        const text = await resp.text();
        errorMsg = text || errorMsg;
        console.error("[RF Planner] Error response text:", text);
      }
      setStatus(`Error: ${errorMsg}`);
      console.error("[RF Planner] API error:", resp.status, errorMsg);
      return { ok: false, error: errorMsg, cacheCenter: { lat, lon: lng } };
    }

    console.log("[RF Planner] Parsing response JSON...");
    const data = await resp.json();
    console.log("[RF Planner] Response data keys:", Object.keys(data));
    console.log("[RF Planner] Response data:", data);

    applyServerPlanTo2dView(data, { requestLat: lat, requestLng: lng });
    const cacheCenter = (data.snapped_tx && Number.isFinite(data.snapped_tx.lat) && Number.isFinite(data.snapped_tx.lon))
      ? { lat: data.snapped_tx.lat, lon: data.snapped_tx.lon }
      : { lat, lon: lng };
    planResults.push({ lat, lon: lng, out: data, data, cacheCenter });
    return { ok: true, data, cacheCenter };
  } catch (err) {
    try { stopPlannerProgressPolling(); } catch {}
    console.error("[RF Planner] Error:", err);
    console.error("[RF Planner] Error details:", {
      name: err.name,
      message: err.message,
      stack: err.stack
    });
    
    if (err.name === 'AbortError') {
      setStatus("Request timed out (30min). The backend may still be processing. Check backend logs.");
    } else if (err instanceof TypeError && err.message.includes('fetch')) {
      // Network error - connection was dropped
      // This can happen if the server closes the connection during long processing
      setStatus("Network connection lost. Backend may still be processing (check logs). If backend completes, refresh and try again.");
    } else if (err.message && err.message.includes('Failed to fetch')) {
      setStatus("Network error: Connection lost. Backend may still be processing. Check backend logs.");
    } else {
      setStatus(`Request failed: ${err.message}. Check console / backend logs.`);
    }
    return { ok: false, error: err.message || String(err), cacheCenter: { lat, lon: lng } };
  }
}

// Clear map function - removes all drawings and visualization (but preserves OSM cache)
async function clearMap() {
  console.log("[RF Planner] clearMap() called");

  if (typeof window.rf2dClearAll === "function") window.rf2dClearAll();
  removeRestorePrompt();
  restorePromptState = null;
  
  // Remove and recreate the layer groups to ensure everything is cleared
  map.removeLayer(currentLayerGroup);
  map.removeLayer(sectorLayerGroup);
  map.removeLayer(isacProbeLayerGroup);
  
  // Remove all heatmap layer groups
  heatmapLayerGroups.forEach(layerGroup => {
    map.removeLayer(layerGroup);
  });
  heatmapLayerGroups = [];

  planResults = [];
  currentLayerGroup = L.layerGroup().addTo(map);
  sectorLayerGroup = L.layerGroup().addTo(map);
  isacProbeLayerGroup = L.layerGroup().addTo(map);
  window.txMarker = null; // Reset TX marker reference
  window.currentHeatmapLayerGroup = null; // Reset current heatmap layer group
  currentTxLocation = null; // Reset TX location
  console.log("[RF Planner] Cleared all map layers (including all heatmaps)");
  
  // Clear localStorage cache (browser storage - visualization data only)
  // This clears the heatmap visualization data, not the OSM building data
  try {
    localStorage.removeItem('rf_planning_last_result');
    localStorage.removeItem('rf_planning_timestamp');
    console.log("[RF Planner] Cleared localStorage (visualization data only)");
  } catch (e) {
    console.warn("[RF Planner] Failed to clear localStorage:", e);
  }
  
  // NOTE: We do NOT call /api/clear-cache here
  // The OSM cache (buildings, landuse) should persist across map clears
  // to avoid repeated API calls. It will expire after 30 days automatically.
  // To manually clear OSM cache, use a separate admin endpoint or delete files directly.
  console.log("[RF Planner] OSM cache preserved (buildings/landuse data remains cached)");
  
  // Hide legend
  const legend = document.getElementById("rsrp-legend");
  if (legend) {
    legend.style.display = "none";
    console.log("[RF Planner] Hid legend");
  }
  
  // Clear metadata and panorama displays
  const metaDiv = document.getElementById("metadata-display");
  if (metaDiv) {
    metaDiv.remove();
    console.log("[RF Planner] Removed metadata display");
  }
  
  const panoDiv = document.getElementById("panorama-display");
  if (panoDiv) {
    panoDiv.remove();
    console.log("[RF Planner] Removed panorama display");
  }
  
  // Reset status
  setStatus("Map cleared. OSM cache preserved. Click on the map or enter coordinates to run RF planning.");
  
  console.log("[RF Planner] Map cleared - visualization removed, OSM cache preserved");
}

async function appendIsacHypothesisBundleToExport(out, prefix, extraBlobs) {
  const product = out?.channel_analysis?.data_product || out?.channel_analysis_product;
  const productId = String(product?.product_id || "").trim();
  if (!productId) return;
  const summary = out?.channel_analysis || {};
  const active = out?._interactive_isac || (
    window._activeIsacAnalysis?.product_id === productId ? window._activeIsacAnalysis : null
  );
  const source = active || summary;
  if (!source?.receiver || !source?.target || !source?.motion || !source?.processing) return;
  const request = {
    receiver: source.receiver,
    target: source.target,
    motion: source.motion,
    processing: source.processing,
    layers: Array.from(INTERACTIVE_ISAC_LAYERS).filter((name) => name !== "target_measurement_cell"),
    imageSize: 1024,
  };
  const response = await fetch(`/api/channel-analysis/products/${encodeURIComponent(productId)}/evaluate-bundle`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(request),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload?.detail || `ISAC export evaluate HTTP ${response.status}`);
  const manifest = { ...payload, layers: Object.fromEntries(Object.entries(payload.layers || {}).map(([name, hm]) => [name, { ...hm, png_b64: undefined }])) };
  if (active?.selected_target) manifest.selected_target = active.selected_target;
  extraBlobs[`${prefix}/isac_current_hypothesis_bundle.json`] = new Blob(
    [JSON.stringify(manifest, null, 2)], { type: "application/json" },
  );
  for (const [name, hm] of Object.entries(payload.layers || {})) {
    if (!hm?.png_b64) continue;
    const safe = String(name).replace(/[^a-zA-Z0-9_-]/g, "_");
    extraBlobs[`${prefix}/heatmaps/current_hypothesis_${safe}.png`] = window.RFExportUtils.base64DataUrlToBlob(hm.png_b64);
  }
}

async function exportCurrentView() {
  const mapContainer = document.getElementById("map-container");
  if (!mapContainer) return;

  try {
    setStatus("Exporting...");
    const canvas = await html2canvas(mapContainer, {
      useCORS: true,
      allowTaint: true,
      logging: false,
      scale: window.devicePixelRatio || 1,
    });
    const fullViewBlob = await new Promise((res, rej) => {
      canvas.toBlob((b) => (b ? res(b) : rej(new Error("toBlob failed"))), "image/png");
    });

    const center = map.getCenter();
    const zoom = map.getZoom();
    let roadNames = [];
    try {
      const url = `/api/roads/labels?lat=${encodeURIComponent(center.lat)}&lon=${encodeURIComponent(center.lng)}&radius_m=2500`;
      const r = await fetch(url);
      const payload = await r.json();
      if (payload?.labels && Array.isArray(payload.labels)) {
        roadNames = payload.labels.map((l) => ({
          name: String(l.name || "").trim(),
          lat: Number(l.anchor_lat ?? l.lat),
          lon: Number(l.anchor_lon ?? l.lon),
          importance: String(l.importance || "minor"),
        })).filter((l) => l.name && Number.isFinite(l.lat) && Number.isFinite(l.lon));
      }
    } catch (e) {
      console.warn("[RF Planner] Failed to fetch road labels for export:", e);
    }

    const viewState = { center_lat: center.lat, center_lon: center.lng, zoom };
    const metadata = window.RFExportUtils
      ? window.RFExportUtils.buildExportMetadata(planResults, {
          plannerVersion: "2d",
          viewState,
          roadNames,
        })
      : { plans: [], road_names: roadNames };

    const heatmapBlobs = [];
    const extraBlobs = {};
    if (window.RFExportUtils && planResults.length) {
      const heatmapFields = [
        "heatmap_received_power", "heatmap_incident_power", "heatmap_bistatic_echo",
        "heatmap_bistatic_snr", "heatmap_bistatic_margin", "heatmap_bistatic_doppler",
        "heatmap_bistatic_path_range", "heatmap_bistatic_excess_delay",
        "heatmap_bistatic_angle", "heatmap_bistatic_detectable", "heatmap_terrain", "heatmap_sinr",
      ];
      for (let i = 0; i < planResults.length; i++) {
        const pr = planResults[i];
        const out = pr.out || pr.data || {};
        const pngB64 = out.heatmap?.png_b64;
        if (pngB64) {
          const blob = window.RFExportUtils.base64DataUrlToBlob(pngB64);
          heatmapBlobs.push(blob);
        } else {
          heatmapBlobs.push(null);
        }

        const prefix = `plan_${i + 1}`;
        extraBlobs[`${prefix}/rf_config_used.json`] = new Blob(
          [JSON.stringify(out.rf_config_used || out.grid?.rf_params || {}, null, 2)],
          { type: "application/json" },
        );
        if (out.channel_analysis) {
          extraBlobs[`${prefix}/channel_analysis_summary.json`] = new Blob(
            [JSON.stringify(out.channel_analysis, null, 2)],
            { type: "application/json" },
          );
        }
        const activeIsac = out._interactive_isac || (window._activeIsacAnalysis?.product_id === (out.channel_analysis?.data_product?.product_id || out.channel_analysis_product?.product_id) ? window._activeIsacAnalysis : null);
        if (activeIsac) {
          const activeState = { ...activeIsac, layer: activeIsac.layer ? { ...activeIsac.layer, png_b64: undefined } : null };
          extraBlobs[`${prefix}/isac_active_hypothesis.json`] = new Blob(
            [JSON.stringify(activeState, null, 2)], { type: "application/json" },
          );
          if (activeIsac.layer?.png_b64) {
            const activeLayerName = String(activeIsac.layer.layer || "active_isac").replace(/[^a-zA-Z0-9_-]/g, "_");
            extraBlobs[`${prefix}/heatmaps/isac_active_${activeLayerName}.png`] = window.RFExportUtils.base64DataUrlToBlob(activeIsac.layer.png_b64);
          }
          if (activeIsac.target_measurement_overlay?.png_b64) {
            extraBlobs[`${prefix}/heatmaps/selected_target_delay_doppler_cell.png`] = window.RFExportUtils.base64DataUrlToBlob(activeIsac.target_measurement_overlay.png_b64);
          }
        }
        for (const field of heatmapFields) {
          const image = out[field];
          if (!image?.png_b64) continue;
          const layer = String(image.layer || field.replace(/^heatmap_/, "")).replace(/[^a-zA-Z0-9_-]/g, "_");
          extraBlobs[`${prefix}/heatmaps/${layer}.png`] = window.RFExportUtils.base64DataUrlToBlob(image.png_b64);
        }

        try {
          await appendIsacHypothesisBundleToExport(out, prefix, extraBlobs);
        } catch (e) {
          console.warn("[RF Planner] Failed to include complete current-hypothesis ISAC heatmaps:", e);
          extraBlobs[`${prefix}/isac_bundle_export_error.txt`] = new Blob([String(e?.message || e)], { type: "text/plain" });
        }

        const product = out.channel_analysis?.data_product || out.channel_analysis_product;
        if (product?.download_url) {
          try {
            const response = await fetch(product.download_url);
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            extraBlobs[`${prefix}/isac_complete_grid.npz`] = await response.blob();
          } catch (e) {
            console.warn("[RF Planner] Failed to include ISAC NPZ in export:", e);
          }
        }
      }
    }

    const ts = new Date();
    const filename = `rf_planner_export_${ts.getFullYear()}-${String(ts.getMonth() + 1).padStart(2, "0")}-${String(ts.getDate()).padStart(2, "0")}_${String(ts.getHours()).padStart(2, "0")}${String(ts.getMinutes()).padStart(2, "0")}.zip`;

    if (window.RFExportUtils && typeof JSZip !== "undefined") {
      await window.RFExportUtils.createExportZip(fullViewBlob, heatmapBlobs, metadata, filename, extraBlobs);
      setStatus("Exported ZIP with coverage, ISAC heatmaps, complete NPZ grids, configuration and metadata.");
    } else {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(fullViewBlob);
      a.download = "rf_planner_2d.png";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(a.href);
      setStatus("Exported 2D map.");
    }
  } catch (err) {
    console.error("2D export failed:", err);
    setStatus(`Export failed: ${err}`);
  }
}

// Sector management functions
let sectorCounter = 0;

function addSectorUI() {
  const container = document.getElementById("sectors-container");
  if (!container) return;
  
  const sectorId = `sector_${++sectorCounter}`;
  const sectorDiv = document.createElement("div");
  sectorDiv.id = `sector-${sectorId}`;
  sectorDiv.className = "sector-config";
  sectorDiv.style.cssText = "padding: 8px; background: #222; border: 1px solid #444; border-radius: 3px; position: relative;";
  
  sectorDiv.innerHTML = `
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
      <span style="font-size: 11px; font-weight: bold; color: #eee;">Sector ${sectorCounter}</span>
      <button 
        type="button" 
        class="remove-sector-btn"
        style="padding: 2px 6px; background: #cc0000; color: white; border: none; border-radius: 2px; cursor: pointer; font-size: 10px;"
        onmouseover="this.style.background='#aa0000'"
        onmouseout="this.style.background='#cc0000'"
      >
        Remove
      </button>
    </div>
    <div style="margin-bottom: 6px;">
      <label style="display: block; font-size: 10px; margin-bottom: 2px; color: #ccc;">Sector Type:</label>
      <select 
        class="sector-type"
        style="width: 100%; padding: 4px; box-sizing: border-box; background: #333; color: #eee; border: 1px solid #555; border-radius: 2px; font-size: 11px;"
      >
        <option value="360">360° (Omnidirectional)</option>
        <option value="angle" selected>Angle-based</option>
        <option value="polygon">Abstract Polygon</option>
      </select>
    </div>
    <div class="sector-angle-config" style="display: grid; grid-template-columns: 1fr 1fr; gap: 6px; font-size: 10px;">
      <div>
        <label style="display: block; font-size: 10px; margin-bottom: 2px; color: #ccc;">Start Angle (°):</label>
        <input 
          type="number" 
          class="sector-start-angle"
          step="0.1" 
          min="0" 
          max="360" 
          value="0"
          placeholder="0"
          style="width: 100%; padding: 4px; box-sizing: border-box; background: #333; color: #eee; border: 1px solid #555; border-radius: 2px; font-size: 11px;"
        />
      </div>
      <div>
        <label style="display: block; font-size: 10px; margin-bottom: 2px; color: #ccc;">End Angle (°):</label>
        <input 
          type="number" 
          class="sector-end-angle"
          step="0.1" 
          min="0" 
          max="360" 
          value="120"
          placeholder="120"
          style="width: 100%; padding: 4px; box-sizing: border-box; background: #333; color: #eee; border: 1px solid #555; border-radius: 2px; font-size: 11px;"
        />
      </div>
    </div>
    <div class="sector-polygon-config" style="display: none; margin-bottom: 6px; font-size: 10px;">
      <div style="margin-bottom: 4px; color: #aaa;">
        Click "Draw Polygon" then click map to set TX point and add vertices. Need 3+ points total.
      </div>
      <button 
        type="button" 
        class="draw-polygon-btn"
        style="width: 100%; padding: 4px; background: #0066cc; color: white; border: none; border-radius: 2px; cursor: pointer; font-size: 10px;"
        onmouseover="this.style.background='#0052a3'"
        onmouseout="this.style.background='#0066cc'"
      >
        Draw Polygon
      </button>
      <button 
        type="button" 
        class="finish-polygon-btn"
        style="width: 100%; padding: 4px; margin-top: 4px; background: #666; color: white; border: none; border-radius: 2px; cursor: pointer; font-size: 10px; display: none;"
        onmouseover="this.style.background='#555'"
        onmouseout="this.style.background='#666'"
      >
        Finish Drawing
      </button>
      <div class="polygon-status" style="margin-top: 4px; font-size: 9px; color: #888;">
        Not started
      </div>
    </div>
    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 6px; font-size: 10px; margin-top: 6px;">
      <div>
        <label style="display: block; font-size: 10px; margin-bottom: 2px; color: #ccc;">Frequency (MHz):</label>
        <input 
          type="number" 
          class="sector-freq"
          step="0.1" 
          min="0" 
          value="3500"
          placeholder="3500"
          style="width: 100%; padding: 4px; box-sizing: border-box; background: #333; color: #eee; border: 1px solid #555; border-radius: 2px; font-size: 11px;"
        />
      </div>
      <div>
        <label style="display: block; font-size: 10px; margin-bottom: 2px; color: #ccc;">TX Power (dBm):</label>
        <input 
          type="number" 
          class="sector-power"
          step="0.1" 
          value="43"
          placeholder="43"
          style="width: 100%; padding: 4px; box-sizing: border-box; background: #333; color: #eee; border: 1px solid #555; border-radius: 2px; font-size: 11px;"
        />
      </div>
    </div>
    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 6px; font-size: 10px; margin-top: 6px;">
      <div>
        <label style="display: block; font-size: 10px; margin-bottom: 2px; color: #ccc;">Azimuth (deg, optional):</label>
        <input type="number" class="sector-azimuth" step="0.1" min="0" max="360" placeholder="auto"
          style="width: 100%; padding: 4px; box-sizing: border-box; background: #333; color: #eee; border: 1px solid #555; border-radius: 2px; font-size: 11px;" />
      </div>
      <div>
        <label style="display: block; font-size: 10px; margin-bottom: 2px; color: #ccc;">Horiz BW (deg, optional):</label>
        <input type="number" class="sector-beamwidth-h" step="0.1" min="1" max="360" placeholder="auto"
          style="width: 100%; padding: 4px; box-sizing: border-box; background: #333; color: #eee; border: 1px solid #555; border-radius: 2px; font-size: 11px;" />
      </div>
      <div>
        <label style="display: block; font-size: 10px; margin-bottom: 2px; color: #ccc;">Vert BW (deg)</label>
        <input type="number" class="sector-beamwidth-v" step="0.1" min="0.1" max="180" value="8.0"
          style="width: 100%; padding: 4px; box-sizing: border-box; background: #333; color: #eee; border: 1px solid #555; border-radius: 2px; font-size: 11px;" />
      </div>
      <div>
        <label style="display: block; font-size: 10px; margin-bottom: 2px; color: #ccc;">Elec tilt (deg)</label>
        <input type="number" class="sector-electrical-tilt" step="0.1" value="0.0"
          style="width: 100%; padding: 4px; box-sizing: border-box; background: #333; color: #eee; border: 1px solid #555; border-radius: 2px; font-size: 11px;" />
      </div>
      <div>
        <label style="display: block; font-size: 10px; margin-bottom: 2px; color: #ccc;">Mech tilt (deg)</label>
        <input type="number" class="sector-mechanical-tilt" step="0.1" value="0.0"
          style="width: 100%; padding: 4px; box-sizing: border-box; background: #333; color: #eee; border: 1px solid #555; border-radius: 2px; font-size: 11px;" />
      </div>
      <div>
        <label style="display: block; font-size: 10px; margin-bottom: 2px; color: #ccc;">Max horiz atten. (dB)</label>
        <input type="number" class="sector-max-horizontal-atten" step="0.1" value="30.0"
          style="width: 100%; padding: 4px; box-sizing: border-box; background: #333; color: #eee; border: 1px solid #555; border-radius: 2px; font-size: 11px;" />
      </div>
      <div>
        <label style="display: block; font-size: 10px; margin-bottom: 2px; color: #ccc;">Front-to-back (dB)</label>
        <input type="number" class="sector-front-to-back-atten" step="0.1" value="25.0"
          style="width: 100%; padding: 4px; box-sizing: border-box; background: #333; color: #eee; border: 1px solid #555; border-radius: 2px; font-size: 11px;" />
      </div>
    </div>
  `;
  
  container.appendChild(sectorDiv);
  
  // Add remove button handler
  const removeBtn = sectorDiv.querySelector(".remove-sector-btn");
  removeBtn.addEventListener("click", () => {
    // Clean up polygon drawing if active
    if (polygonDrawingMode && polygonDrawingMode.sectorId === sectorId) {
      stopPolygonDrawing();
    }
    sectorDiv.remove();
  });
  
  // Add sector type change handler
  const sectorTypeSelect = sectorDiv.querySelector(".sector-type");
  sectorTypeSelect.addEventListener("change", (e) => {
    const type = e.target.value;
    const angleConfig = sectorDiv.querySelector(".sector-angle-config");
    const polygonConfig = sectorDiv.querySelector(".sector-polygon-config");
    
    if (type === "polygon") {
      angleConfig.style.display = "none";
      polygonConfig.style.display = "block";
    } else {
      angleConfig.style.display = "grid";
      polygonConfig.style.display = "none";
      // Stop polygon drawing if active for this sector
      if (polygonDrawingMode && polygonDrawingMode.sectorId === sectorId) {
        stopPolygonDrawing();
      }
    }
  });
  
  // Add draw polygon button handler
  const drawPolygonBtn = sectorDiv.querySelector(".draw-polygon-btn");
  drawPolygonBtn.addEventListener("click", () => {
    startPolygonDrawing(sectorId);
  });
  
  // Add finish polygon button handler
  const finishPolygonBtn = sectorDiv.querySelector(".finish-polygon-btn");
  finishPolygonBtn.addEventListener("click", () => {
    finishPolygonDrawing();
  });
  
  console.log(`[RF Planner] Added sector ${sectorId}`);
}

// Polygon drawing functions
function startPolygonDrawing(sectorId) {
  // Stop any existing polygon drawing
  if (polygonDrawingMode) {
    stopPolygonDrawing();
  }
  
  polygonDrawingMode = {
    sectorId: sectorId,
    points: [],
    polygonLayer: null,
    txPointSet: currentTxLocation !== null  // Track if TX was already set
  };
  
  // Update status
  const sectorDiv = document.getElementById(`sector-${sectorId}`);
  if (sectorDiv) {
    const statusEl = sectorDiv.querySelector(".polygon-status");
    const finishBtn = sectorDiv.querySelector(".finish-polygon-btn");
    if (statusEl) {
      if (currentTxLocation) {
        statusEl.textContent = "Drawing... Click map to add polygon vertices. Need 2+ more points (3+ total).";
      } else {
        statusEl.textContent = "Drawing... Click map to set TX point (first click). Then add vertices.";
      }
      statusEl.style.color = "#00ff00";
    }
    if (finishBtn) {
      finishBtn.style.display = "none"; // Hide until we have 3+ points
    }
  }
  
  if (currentTxLocation) {
    setStatus("Polygon drawing mode: TX point already set. Click map to add polygon vertices. Need 2+ more points (3+ total).");
    console.log(`[RF Planner] Started polygon drawing for ${sectorId}, TX already set: ${currentTxLocation.lat}, ${currentTxLocation.lon}`);
  } else {
    setStatus("Polygon drawing mode: Click map to set TX point (first click), then add polygon vertices. Need 3+ points total.");
    console.log(`[RF Planner] Started polygon drawing for ${sectorId}, TX point will be set on first click`);
  }
}

function stopPolygonDrawing() {
  if (!polygonDrawingMode) return;
  
  // Remove polygon layer
  if (polygonDrawingMode.polygonLayer) {
    sectorLayerGroup.removeLayer(polygonDrawingMode.polygonLayer);
  }
  
  // Remove markers
  polygonMarkers.forEach(marker => sectorLayerGroup.removeLayer(marker));
  polygonMarkers = [];
  
  // Update status
  const sectorDiv = document.getElementById(`sector-${polygonDrawingMode.sectorId}`);
  if (sectorDiv) {
    const statusEl = sectorDiv.querySelector(".polygon-status");
    const finishBtn = sectorDiv.querySelector(".finish-polygon-btn");
    if (statusEl) {
      statusEl.textContent = "Not started";
      statusEl.style.color = "#888";
    }
    if (finishBtn) {
      finishBtn.style.display = "none";
    }
  }
  
  polygonDrawingMode = null;
  setStatus("Polygon drawing stopped.");
  console.log("[RF Planner] Stopped polygon drawing");
}

function addPolygonPoint(lat, lon) {
  if (!polygonDrawingMode) return;
  
  // If TX point not set yet, first click sets TX point
  if (!polygonDrawingMode.txPointSet) {
    currentTxLocation = { lat, lon: lon };
    polygonDrawingMode.txPointSet = true;
    
    if (typeof window.rf2dSetTxFromPlanner === "function") {
      window.rf2dSetTxFromPlanner(lat, lon);
    } else {
      L.marker([lat, lon], { 
        icon: L.divIcon({ className: "click-marker", html: "📍", iconSize: [20, 20] }) 
      })
        .addTo(currentLayerGroup)
        .bindPopup("TX Location");
    }
    
    // Update status
    const sectorDiv = document.getElementById(`sector-${polygonDrawingMode.sectorId}`);
    if (sectorDiv) {
      const statusEl = sectorDiv.querySelector(".polygon-status");
      if (statusEl) {
        statusEl.textContent = "TX point set. Click map to add polygon vertices. Need 2+ more points (3+ total).";
      }
    }
    
    setStatus(`TX point set at ${lat}, ${lon}. Click map to add polygon vertices. Need 2+ more points (3+ total).`);
    console.log(`[RF Planner] Set TX point: ${lat}, ${lon}`);
    return;
  }
  
  // Add polygon vertex
  polygonDrawingMode.points.push([lat, lon]);
  
  // Add marker for this point
  const marker = L.marker([lat, lon], {
    icon: L.divIcon({ className: "polygon-point-marker", html: "●", iconSize: [8, 8] })
  }).addTo(sectorLayerGroup);
  marker.bindPopup(`Vertex ${polygonDrawingMode.points.length}`);
  polygonMarkers.push(marker);
  
  // Calculate total points (TX + vertices)
  const totalPoints = 1 + polygonDrawingMode.points.length; // TX + vertices
  
  // Update polygon visualization
  if (polygonDrawingMode.points.length >= 1) {
    // Remove old polygon
    if (polygonDrawingMode.polygonLayer) {
      sectorLayerGroup.removeLayer(polygonDrawingMode.polygonLayer);
    }
    
    // Create new polygon (include TX as first point)
    const polygonPoints = [[currentTxLocation.lat, currentTxLocation.lon], ...polygonDrawingMode.points];
    
    // Draw polygon (even with just 1 vertex, show line from TX to point)
    if (polygonDrawingMode.points.length === 1) {
      // Draw a line from TX to the single vertex
      polygonDrawingMode.polygonLayer = L.polyline(polygonPoints, {
        color: "#00ff00",
        weight: 2,
        dashArray: "5, 5"
      }).addTo(sectorLayerGroup);
    } else {
      // Draw polygon
      polygonDrawingMode.polygonLayer = L.polygon(polygonPoints, {
        color: "#00ff00",
        fillColor: "#00ff00",
        fillOpacity: 0.2,
        weight: 2,
        dashArray: "5, 5"
      }).addTo(sectorLayerGroup);
    }
  }
  
  // Update status and finish button
  const sectorDiv = document.getElementById(`sector-${polygonDrawingMode.sectorId}`);
  if (sectorDiv) {
    const statusEl = sectorDiv.querySelector(".polygon-status");
    const finishBtn = sectorDiv.querySelector(".finish-polygon-btn");
    
    if (totalPoints >= 3) {
      // We have 3+ points total - highlight finish button
      if (statusEl) {
        statusEl.textContent = `Ready! ${totalPoints} points total (TX + ${polygonDrawingMode.points.length} vertices). Click "Finish Drawing" or right-click/double-click.`;
        statusEl.style.color = "#00ff00";
      }
      if (finishBtn) {
        finishBtn.style.display = "block";
        finishBtn.style.background = "#00cc00"; // Green when ready
        finishBtn.onmouseover = function() { this.style.background = "#00aa00"; };
        finishBtn.onmouseout = function() { this.style.background = "#00cc00"; };
      }
    } else {
      // Need more points
      const needed = 3 - totalPoints;
      if (statusEl) {
        statusEl.textContent = `Drawing... ${totalPoints} points total. Need ${needed} more point(s) (3+ total required).`;
        statusEl.style.color = "#ffaa00"; // Orange
      }
      if (finishBtn) {
        finishBtn.style.display = "none";
      }
    }
  }
  
  console.log(`[RF Planner] Added polygon vertex ${polygonDrawingMode.points.length}: ${lat}, ${lon} (${totalPoints} points total)`);
}

function finishPolygonDrawing() {
  if (!polygonDrawingMode) return;
  
  // Calculate total points (TX + vertices)
  const totalPoints = 1 + polygonDrawingMode.points.length;
  
  // Minimum 3 points total required (TX + 2 vertices, or 3 vertices if TX was set separately)
  if (totalPoints < 3) {
    const needed = 3 - totalPoints;
    setStatus(`Error: Polygon needs at least 3 points total. Currently have ${totalPoints} point(s). Need ${needed} more. Continue drawing.`);
    return;
  }
  
  // Resolve multiple polygons into a single contiguous polygon
  const resolvedPoints = resolveToSinglePolygon(polygonDrawingMode.points);
  if (!resolvedPoints || resolvedPoints.length < 2) {
    setStatus("Error: Could not resolve polygon. Please try again.");
    return;
  }
  
  // Update points with resolved polygon
  polygonDrawingMode.points = resolvedPoints;
  
  // Store polygon points in sector div for collection
  const sectorDiv = document.getElementById(`sector-${polygonDrawingMode.sectorId}`);
  if (sectorDiv) {
    // Store polygon points as data attribute (without TX origin - backend will add it)
    sectorDiv.setAttribute("data-polygon-points", JSON.stringify(polygonDrawingMode.points));
    
    const statusEl = sectorDiv.querySelector(".polygon-status");
    const finishBtn = sectorDiv.querySelector(".finish-polygon-btn");
    if (statusEl) {
      const finalTotal = 1 + resolvedPoints.length;
      statusEl.textContent = `Complete: ${finalTotal} points total (TX + ${resolvedPoints.length} vertices)`;
      statusEl.style.color = "#00ff00";
    }
    if (finishBtn) {
      finishBtn.style.display = "none"; // Hide after finishing
    }
  }
  
  const finalTotal = 1 + resolvedPoints.length;
  setStatus(`Polygon complete: ${finalTotal} points total. Click "Plan RF Queue" to run planning.`);
  console.log(`[RF Planner] Finished polygon drawing: ${finalTotal} points total (TX + ${resolvedPoints.length} vertices)`);
  console.log(`[RF Planner] Polygon vertices:`, resolvedPoints);
  
  // Keep polygon visible but exit drawing mode
  const points = polygonDrawingMode.points;
  polygonDrawingMode = null;
  
  // Return points for potential use
  return points;
}

/**
 * Resolve multiple polygons into a single contiguous polygon.
 * 
 * This function handles cases where:
 * - Self-intersecting polygons create multiple regions
 * - Points form disconnected shapes
 * 
 * Strategy: Use convex hull to ensure a single contiguous polygon.
 * This preserves the general shape while ensuring connectivity.
 */
function resolveToSinglePolygon(points) {
  if (!points || points.length < 2) {
    return points;
  }
  
  // If we have only 2 points, return as-is (will form a triangle with TX origin)
  if (points.length === 2) {
    return points;
  }
  
  // For 3+ points, check if polygon is self-intersecting or has multiple regions
  // Use convex hull to ensure single contiguous polygon
  const hull = computeConvexHull(points);
  
  // If convex hull has same number of points, polygon was already convex (good)
  // If convex hull has fewer points, we simplified to outer boundary
  return hull;
}

/**
 * Compute convex hull of points using Graham scan algorithm.
 * Returns the outer boundary points in counter-clockwise order.
 * 
 * Note: This uses lat/lon as Cartesian coordinates, which is valid for small geographic areas.
 */
function computeConvexHull(points) {
  if (points.length <= 2) {
    return [...points]; // Return copy
  }
  
  // Create a copy to avoid mutating original
  const pointsCopy = points.map(p => [p[0], p[1]]);
  
  // Find the point with the lowest lat (and leftmost lon if tie)
  // In geographic coordinates, lower lat = south, lower lon = west
  let bottomPoint = pointsCopy[0];
  let bottomIndex = 0;
  for (let i = 1; i < pointsCopy.length; i++) {
    const p = pointsCopy[i];
    if (p[0] < bottomPoint[0] || (p[0] === bottomPoint[0] && p[1] < bottomPoint[1])) {
      bottomPoint = p;
      bottomIndex = i;
    }
  }
  
  // Swap bottom point to first position
  [pointsCopy[0], pointsCopy[bottomIndex]] = [pointsCopy[bottomIndex], pointsCopy[0]];
  
  // Sort points by polar angle with respect to bottom point
  const sortedPoints = [pointsCopy[0], ...pointsCopy.slice(1).sort((a, b) => {
    const angleA = Math.atan2(a[0] - bottomPoint[0], a[1] - bottomPoint[1]);
    const angleB = Math.atan2(b[0] - bottomPoint[0], b[1] - bottomPoint[1]);
    return angleA - angleB;
  })];
  
  // Build convex hull using Graham scan
  const hull = [sortedPoints[0], sortedPoints[1]];
  
  for (let i = 2; i < sortedPoints.length; i++) {
    const point = sortedPoints[i];
    
    // Remove points that create clockwise turns
    while (hull.length > 1 && crossProduct(hull[hull.length - 2], hull[hull.length - 1], point) <= 0) {
      hull.pop();
    }
    
    hull.push(point);
  }
  
  return hull;
}

/**
 * Calculate cross product of vectors (p1->p2) and (p1->p3).
 * Returns positive for counter-clockwise turn, negative for clockwise, 0 for collinear.
 */
function crossProduct(p1, p2, p3) {
  return (p2[1] - p1[1]) * (p3[0] - p2[0]) - (p2[0] - p1[0]) * (p3[1] - p2[1]);
}

function collectSectorConfigs() {
  const sectors = [];
  const sectorDivs = document.querySelectorAll(".sector-config");
  
  console.log(`[RF Planner] collectSectorConfigs: Found ${sectorDivs.length} sector div(s)`);
  
  sectorDivs.forEach((div, index) => {
    const sectorType = div.querySelector(".sector-type").value;
    const freq = parseFloat(div.querySelector(".sector-freq").value);
    const power = parseFloat(div.querySelector(".sector-power").value);
    
    console.log(`[RF Planner] Processing sector ${index + 1}: type=${sectorType}, freq=${freq}, power=${power}`);
    
    // Validate common fields
    if (isNaN(freq) || isNaN(power)) {
      console.warn(`[RF Planner] Skipping invalid sector ${index + 1} (missing freq/power)`);
      return;
    }
    
    const sectorId = `sector_${index + 1}`;
    const sectorConfig = {
      sector_id: sectorId,
      sector_type: sectorType,
      freq_mhz: freq,
      tx_power_dbm: power,
      channel_bandwidth_mhz: 20.0,  // Default, can be made configurable later
    };
    const azimuth = parseFloat(div.querySelector(".sector-azimuth").value);
    const beamwidthH = parseFloat(div.querySelector(".sector-beamwidth-h").value);
    const beamwidthV = parseFloat(div.querySelector(".sector-beamwidth-v").value);
    const electricalTilt = parseFloat(div.querySelector(".sector-electrical-tilt").value);
    const mechanicalTilt = parseFloat(div.querySelector(".sector-mechanical-tilt").value);
    const maxHorizontalAtten = parseFloat(div.querySelector(".sector-max-horizontal-atten").value);
    const frontToBackAtten = parseFloat(div.querySelector(".sector-front-to-back-atten").value);
    if (!isNaN(azimuth)) sectorConfig.azimuth_deg = azimuth;
    if (!isNaN(beamwidthH)) sectorConfig.beamwidth_h_deg = beamwidthH;
    if (!isNaN(beamwidthV)) sectorConfig.beamwidth_v_deg = beamwidthV;
    if (!isNaN(electricalTilt)) sectorConfig.electrical_tilt_deg = electricalTilt;
    if (!isNaN(mechanicalTilt)) sectorConfig.mechanical_tilt_deg = mechanicalTilt;
    if (!isNaN(maxHorizontalAtten)) sectorConfig.max_horizontal_attenuation_db = maxHorizontalAtten;
    if (!isNaN(frontToBackAtten)) sectorConfig.front_to_back_attenuation_db = frontToBackAtten;
    
    if (sectorType === "360") {
      // 360° sector - no additional fields needed
      console.log(`[RF Planner] Added 360° sector ${sectorId}`);
      sectors.push(sectorConfig);
    } else if (sectorType === "angle") {
      // Angle-based sector
      const startAngle = parseFloat(div.querySelector(".sector-start-angle").value);
      const endAngle = parseFloat(div.querySelector(".sector-end-angle").value);
      
      if (isNaN(startAngle) || isNaN(endAngle)) {
        console.warn(`[RF Planner] Skipping invalid sector ${index + 1} (missing angles)`);
        return;
      }
      
      if (startAngle < 0 || startAngle > 360 || endAngle < 0 || endAngle > 360) {
        console.warn(`[RF Planner] Sector ${index + 1} has invalid angle range`);
        return;
      }
      
      sectorConfig.start_angle_deg = startAngle;
      sectorConfig.end_angle_deg = endAngle;
      console.log(`[RF Planner] Added angle-based sector ${sectorId}: ${startAngle}° to ${endAngle}°`);
      sectors.push(sectorConfig);
    } else if (sectorType === "polygon") {
      // Polygon sector - get points from drawing state
      const sectorIdFromDiv = div.id.replace("sector-", "");
      console.log(`[RF Planner] Processing polygon sector ${sectorId}, div ID: ${div.id}, extracted ID: ${sectorIdFromDiv}`);
      
      // Try to get polygon points from a data attribute
      const polygonPointsData = div.getAttribute("data-polygon-points");
      console.log(`[RF Planner] Polygon sector ${sectorId} data-polygon-points attribute:`, polygonPointsData ? "found" : "NOT FOUND");
      
      if (polygonPointsData) {
        try {
          const polygonPoints = JSON.parse(polygonPointsData);
          console.log(`[RF Planner] Parsed polygon points: ${polygonPoints ? polygonPoints.length : 0} points`);
          
          // Minimum 2 points required (TX origin is the third point)
          if (polygonPoints && polygonPoints.length >= 2) {
            // Ensure single contiguous polygon
            const resolvedPoints = resolveToSinglePolygon(polygonPoints);
            if (resolvedPoints && resolvedPoints.length >= 2) {
              sectorConfig.polygon_points = resolvedPoints;
              console.log(`[RF Planner] ✓ Added polygon sector ${sectorId} with ${resolvedPoints.length} points`);
              sectors.push(sectorConfig);
            } else {
              console.error(`[RF Planner] ✗ Sector ${index + 1} polygon resolution failed`);
            }
          } else {
            console.error(`[RF Planner] ✗ Sector ${index + 1} polygon has insufficient points (${polygonPoints ? polygonPoints.length : 0}, need at least 2)`);
          }
        } catch (e) {
          console.error(`[RF Planner] ✗ Sector ${index + 1} has invalid polygon data:`, e);
        }
      } else {
        console.error(`[RF Planner] ✗ Sector ${index + 1} is polygon type but has no polygon points. Please draw the polygon first.`);
        console.error(`[RF Planner]   Div ID: ${div.id}`);
        console.error(`[RF Planner]   All data attributes:`, Array.from(div.attributes).map(a => `${a.name}="${a.value}"`).join(", "));
      }
    }
  });
  
  console.log(`[RF Planner] Final result: Collected ${sectors.length} sector(s)`);
  return sectors;
}

/** When Propagation = `3d_rt_osm`, Plan RF Queue runs the Leaflet 2D OSM ray fan instead of POST /api/plan. */
async function runPlanQueue2dOsmRays(queue) {
  const successes = [];
  const failed = [];
  if (typeof window.rf2dLaunchRays !== "function") {
    setStatus("2D OSM ray tracer is not loaded.");
    return;
  }
  for (let i = 0; i < queue.length; i++) {
    const point = queue[i];
    const prefix = queue.length > 1 ? `TX ${i + 1}/${queue.length}: ` : "";
    if (typeof window.rf2dSetTxFromPlanner === "function") {
      window.rf2dSetTxFromPlanner(point.lat, point.lon);
    }
    let res;
    try {
      res = await window.rf2dLaunchRays();
    } catch (err) {
      setStatus(`${prefix}Ray trace failed: ${String(err)}`);
      failed.push(point);
      continue;
    }
    if (res && res.ok) {
      successes.push(point);
      setStatus(`${prefix}Launched OSM multipath rays.`);
    } else {
      failed.push(point);
      const hint = res && res.error === "tx_steer"
        ? "Confirm TX direction on the map (second click), then try Plan RF Queue again."
        : (res && res.error) || "Ray launch did not complete.";
      setStatus(`${prefix}${hint}`);
    }
    if (i < queue.length - 1) {
      setStatus(`TX ${i + 1}/${queue.length}: done. Waiting ${Math.round(MULTI_TX_PULL_DELAY_MS / 1000)}s before next…`);
      await sleep(MULTI_TX_PULL_DELAY_MS);
    }
  }
  const totalSuccess = successes.length;
  const totalFailed = failed.length;
  if (totalSuccess && totalFailed) {
    setStatus(`Ray queue complete: ${totalSuccess}/${queue.length} TX launched, ${totalFailed} skipped or failed.`);
  } else if (totalSuccess) {
    setStatus(`Ray queue complete: all ${totalSuccess} TX point(s) launched.`);
  } else {
    setStatus("Ray queue: no TX completed. Check TX direction, RX placement, and OSM scene.");
  }
}

// Try to restore last results on page load
window.addEventListener('DOMContentLoaded', () => {
  loadRfParamsDefaults();
  window.RFWaveformUI?.init({
    onChange({ technology }) {
      const rayMode = document.getElementById("ray-mode");
      if (!rayMode) return;
      for (const option of rayMode.options) {
        if (String(option.value).toLowerCase() === "3d_rt_osm") option.disabled = technology === "dvt";
      }
      if (technology === "dvt" && String(rayMode.value).toLowerCase() === "3d_rt_osm") rayMode.value = "2d";
      updateRayModeHelperText();
    },
  });
  if (window.RFAddressLookup) {
    window.RFAddressLookup.bindAddressLookup({
      setStatus,
      onNavigate(lat, lon, result, mode) {
        map.setView([lat, lon], 15);
        if (mode === "set_tx") {
          if (typeof appendTxInputPoint === "function") {
            appendTxInputPoint(lat, lon);
          } else {
            const el = document.getElementById("tx-input");
            if (el) {
              const line = `${lat},${lon}`;
              el.value = el.value.trim() ? `${el.value.trim()}\n${line}` : line;
            }
          }
          if (typeof window.rf2dSetTxFromPlanner === "function") {
            window.rf2dSetTxFromPlanner(lat, lon);
          } else {
            if (window.txMarker) currentLayerGroup.removeLayer(window.txMarker);
            window.txMarker = L.marker([lat, lon], { icon: L.divIcon({ className: "click-marker", html: "📍", iconSize: [20, 20] }) })
              .addTo(currentLayerGroup)
              .bindPopup(result.formatted_address || "TX Location");
          }
        }
        setStatus(`At ${result.formatted_address || `${lat}, ${lon}`}`);
      },
    });
  }
  if (!loadLastResults()) {
    setStatus("Click on the map or enter coordinates to run RF planning.");
  }

  const coverageLayerEl = document.getElementById("coverage-display-layer");
  if (coverageLayerEl) {
    coverageLayerEl.addEventListener("change", async () => {
      if (!window._lastPlanResult) return;
      const layer = String(coverageLayerEl.value || "");
      if (INTERACTIVE_ISAC_LAYERS.has(layer) && currentChannelProduct()?.product_id) {
        try {
          await evaluateIsacScene({ layer, render: true });
          return;
        } catch (err) {
          console.error("ISAC layer reevaluation failed:", err);
          setStatus(`ISAC layer reevaluation failed: ${err}`);
        }
      }
      heatmapLayerGroups.forEach((layerGroup) => map.removeLayer(layerGroup));
      heatmapLayerGroups = [];
      const g = L.layerGroup().addTo(map);
      heatmapLayerGroups.push(g);
      window.currentHeatmapLayerGroup = g;
      renderHeatmap(window._lastPlanResult);
    });
  }

  document.getElementById("channel-apply-hypothesis")?.addEventListener("click", async () => {
    try {
      setStatus("Reevaluating ISAC target/detector hypothesis over the existing scene...");
      await evaluateIsacScene({ render: true });
    } catch (err) {
      console.error("ISAC hypothesis reevaluation failed:", err);
      setStatus(`ISAC hypothesis reevaluation failed: ${err}`);
    }
  });
  
  // Handle add sector button
  const addSectorBtn = document.getElementById("add-sector-btn");
  if (addSectorBtn) {
    addSectorBtn.addEventListener("click", () => {
      addSectorUI();
    });
  }
  
  // Handle show/hide sectors toggle
  const showSectorsToggle = document.getElementById("show-sectors-toggle");
  if (showSectorsToggle) {
    showSectorsToggle.addEventListener("change", (e) => {
      toggleSectorVisibility(e.target.checked);
    });
  }
  
  document.getElementById("ray-mode")?.addEventListener("change", updateRayModeHelperText);
  updateRayModeHelperText();

  // Handle coordinate form submission ("Plan RF" button)
  const coordForm = document.getElementById("coord-form");
  coordForm.addEventListener("submit", async (e) => {
    e.preventDefault();

    if (isPlanningQueue) return;
    const queue = getQueuedTxPoints();
    if (!queue || !queue.length) return;

    const rayMode = String(document.getElementById("ray-mode")?.value || "2d").toLowerCase();
    if (rayMode === "3d_rt_osm") {
      planResults = [];
      flyToQueuedPoints(queue);
      isPlanningQueue = true;
      setPlanButtonBusy(true);
      try {
        await runPlanQueue2dOsmRays(queue);
      } finally {
        isPlanningQueue = false;
        setPlanButtonBusy(false);
      }
      return;
    }

    planResults = [];
    flyToQueuedPoints(queue);

    isPlanningQueue = true;
    setPlanButtonBusy(true);

    const successes = [];
    const failed = [];
    const retryRadiusM = 2000.0;

    try {
      for (let i = 0; i < queue.length; i++) {
        const point = queue[i];
        const result = await runRFPlanning(point.lat, point.lon, "form");
        if (result?.ok) {
          successes.push(result.cacheCenter);
        } else {
          failed.push(point);
        }

        if (i < queue.length - 1) {
          setStatus(`TX ${i + 1}/${queue.length}: processed. Waiting ${Math.round(MULTI_TX_PULL_DELAY_MS / 1000)}s before next pull...`);
          await sleep(MULTI_TX_PULL_DELAY_MS);
        }
      }

      const retryable = failed.filter((point) =>
        successes.some((success) => haversineDistanceM(point.lat, point.lon, success.lat, success.lon) <= retryRadiusM)
      );

      let retriedSuccesses = 0;
      for (let i = 0; i < retryable.length; i++) {
        const point = retryable[i];
        setStatus(`Retrying ${i + 1}/${retryable.length} near a successful TX so cached OSM data can be reused...`);
        await sleep(MULTI_TX_PULL_DELAY_MS);
        const retryResult = await runRFPlanning(point.lat, point.lon, "retry");
        if (retryResult?.ok) {
          retriedSuccesses += 1;
          successes.push(retryResult.cacheCenter);
        }
      }

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
  });

  document.getElementById("tx-input")?.addEventListener("input", updateTxInputSummary);
  updateTxInputSummary();
  
  // Handle clear map button
  const clearBtn = document.getElementById("clear-map-btn");
  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      clearMap();
    });
  }

  // Handle export button
  const exportBtn = document.getElementById("export-btn");
  if (exportBtn) {
    exportBtn.addEventListener("click", () => {
      exportCurrentView();
    });
  }
});

// Map click handler - handle polygon drawing or TX point setting
// NOTE: Map clicks NO LONGER trigger RF planning automatically
// User must explicitly click "Plan RF" button
map.on("click", async (e) => {
  const { lat, lng } = e.latlng;
  
  // Check if we're in polygon drawing mode
  if (polygonDrawingMode) {
    addPolygonPoint(lat, lng);
    return;
  }

  if (await tryIsacTargetProbe(e)) {
    return;
  }

  if (typeof window.rf2dHandleMapClick === "function" && window.rf2dHandleMapClick(e)) {
    return;
  }
  
  // If not in drawing mode, clicking map just sets/updates TX location
  // This allows user to set TX point before starting polygon drawing
  // IMPORTANT: Do NOT clear layers - preserve existing RF heatmap
  currentTxLocation = { lat, lon: lng };
  
  // Remove old TX marker if it exists, then add new one
  // We need to find and remove only the TX marker, not the entire layer group
  // Store TX marker reference so we can remove it later
  if (window.txMarker) {
    currentLayerGroup.removeLayer(window.txMarker);
  }
  
  // Add new TX marker
  window.txMarker = L.marker([lat, lng], { 
    icon: L.divIcon({ className: "click-marker", html: "📍", iconSize: [20, 20] }) 
  })
    .addTo(currentLayerGroup)
    .bindPopup("TX Location");
  
  map.setView([lat, lng], 15);
  const queueCount = appendTxInputPoint(lat, lng);
  const nextStep = window.RFWaveformUI?.isDvt()
    ? 'Configure the DVT transmitter and click "Plan RF Queue".'
    : 'Add sector and click "Plan RF Queue" to run planning.';
  setStatus(`TX added (${queueCount} queued): ${lat}, ${lng}. ${nextStep}`);
  console.log(`[RF Planner] TX location set: ${lat}, ${lng} (preserving existing RF heatmap)`);
});

map.on("mousemove", (e) => {
  if (typeof window.rf2dHandleMouseMove === "function") window.rf2dHandleMouseMove(e);
});

// Map right-click handler - finish polygon drawing (does NOT trigger RF planning)
map.on("contextmenu", (e) => {
  e.originalEvent.preventDefault();
  if (polygonDrawingMode) {
    finishPolygonDrawing();
    // Do NOT trigger RF planning - user must explicitly click "Plan RF" button
  }
});

// Map double-click handler - finish polygon drawing (does NOT trigger RF planning)
map.on("dblclick", (e) => {
  if (polygonDrawingMode) {
    e.originalEvent.preventDefault();
    finishPolygonDrawing();
    // Do NOT trigger RF planning - user must explicitly click "Plan RF" button
  }
});

// Fixed RSRP scale for consistent comparison across locations
// Industry standard: -150 dBm (no signal) to +50 dBm (very strong)
// Typical macro cell range: -140 dBm to +30 dBm
const FIXED_RSRP_MIN = -140.0;  // dBm - weak but still measurable macro-cell coverage
const FIXED_RSRP_MAX = -60.0;   // dBm - strong macro-cell reference-signal level

/** Local tangent-plane offset (east m, north m) from (latDeg, lonDeg). */
function offsetEnuToLatLon(latDeg, lonDeg, eastM, northM) {
  const R = 6371000.0;
  const φ = (latDeg * Math.PI) / 180.0;
  const dLat = (northM / R) * (180.0 / Math.PI);
  const dLon = (eastM / (R * Math.cos(φ))) * (180.0 / Math.PI);
  return { lat: latDeg + dLat, lon: lonDeg + dLon };
}

function latLonToLocalEnu(originLat, originLon, lat, lon) {
  const R = 6371000.0;
  const φ = (originLat * Math.PI) / 180.0;
  return {
    east: ((lon - originLon) * Math.PI / 180.0) * R * Math.cos(φ),
    north: ((lat - originLat) * Math.PI / 180.0) * R,
  };
}

function currentChannelProduct() {
  const result = window._lastPlanResult;
  const channel = result?.channel_analysis;
  return channel?.data_product || result?.channel_analysis_product || null;
}

const INTERACTIVE_ISAC_LAYERS = new Set([
  "bistatic_echo", "bistatic_snr", "bistatic_margin", "rcs_margin",
  "minimum_detectable_rcs", "bistatic_doppler", "doppler_sensitivity",
  "minimum_detectable_speed", "required_cancellation", "direct_residual_margin",
  "static_clutter_delay_separation", "static_clutter_overlap", "static_clutter_path_count", "target_measurement_cell",
  "screening_detectable", "qualified_detectable",
]);

function parseIsacLocationInput(raw) {
  const text = String(raw || "").trim();
  if (!text) return null;
  const parts = text.split(/[\s,;]+/).filter(Boolean).map(Number);
  if (parts.length !== 2 || parts.some((v) => !Number.isFinite(v))) {
    throw new Error("Selected target must be entered as latitude, longitude.");
  }
  if (parts[0] < -90 || parts[0] > 90 || parts[1] < -180 || parts[1] > 180) {
    throw new Error("Selected target is outside the valid latitude/longitude range.");
  }
  return { latitude: parts[0], longitude: parts[1] };
}

function isacPhysicalSceneFingerprint(config) {
  if (!config) return null;
  const receiver = config.receiver || {};
  return JSON.stringify({
    receiverGeometry: {
      latitude: Number(receiver.latitude), longitude: Number(receiver.longitude),
      altitudeMamsl: Number(receiver.altitudeMamsl), antennaHeightMagl: Number(receiver.antennaHeightMagl),
    },
    targetHeightMagl: Number(config.target?.heightMagl),
    returnPathModel: config.returnPathModel,
    returnPathResolutionM: Number(config.returnPathResolutionM),
  });
}

function currentIsacHypothesisRequest(layerOverride, selectedOverride) {
  if (typeof window.RFWaveformBuildChannelAnalysis !== "function") {
    throw new Error("ISAC waveform controls are unavailable.");
  }
  const config = window.RFWaveformBuildChannelAnalysis();
  if (!config) throw new Error("Enable ISAC analysis first.");
  const lastConfig = window._lastPlanResult?.rf_config_used?.channel_analysis;
  if (!lastConfig) throw new Error("Run Plan RF once to build the physical ISAC scene.");
  if (isacPhysicalSceneFingerprint(config) !== isacPhysicalSceneFingerprint(lastConfig)) {
    throw new Error("A physical-scene input changed (RX, target height, or reciprocal-path setting). Run Plan RF before applying this hypothesis.");
  }
  const selected = selectedOverride || parseIsacLocationInput(document.getElementById("channel-selected-target-location")?.value);
  const layer = layerOverride || document.getElementById("coverage-display-layer")?.value || "qualified_detectable";
  const request = {
    receiver: config.receiver,
    target: { heightMagl: config.target.heightMagl, bistaticRcsM2: config.target.bistaticRcsM2 },
    motion: config.motion,
    processing: config.processing,
    layer: INTERACTIVE_ISAC_LAYERS.has(layer) ? layer : "qualified_detectable",
    imageSize: 1024,
  };
  if (selected) {
    request.selectedLatitude = selected.latitude;
    request.selectedLongitude = selected.longitude;
  }
  return request;
}

async function evaluateIsacScene({ layer = null, selected = null, render = true } = {}) {
  const product = currentChannelProduct();
  const productId = String(product?.product_id || "").trim();
  if (!productId) throw new Error("No reusable ISAC scene product is available. Run Plan RF first.");
  const request = currentIsacHypothesisRequest(layer, selected);
  const response = await fetch(`/api/channel-analysis/products/${encodeURIComponent(productId)}/evaluate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload?.detail || `ISAC evaluate HTTP ${response.status}`);
  window._activeIsacAnalysis = payload;
  window._lastPlanResult._interactive_isac = payload;
  if (payload.selected_target) {
    const el = document.getElementById("channel-selected-target-location");
    if (el) el.value = `${Number(payload.selected_target.latitude).toFixed(8)}, ${Number(payload.selected_target.longitude).toFixed(8)}`;
    renderIsacTargetProbe(payload);
  }
  if (render && payload.layer?.png_b64) {
    heatmapLayerGroups.forEach((layerGroup) => map.removeLayer(layerGroup));
    heatmapLayerGroups = [];
    const g = L.layerGroup().addTo(map);
    heatmapLayerGroups.push(g);
    window.currentHeatmapLayerGroup = g;
    renderHeatmap(window._lastPlanResult);
  }
  const c = payload.counts || {};
  const q = payload.processing_assessment || {};
  setStatus(`ISAC scene reused: ${Number(c.qualified_detectable || 0).toLocaleString()} processing-qualified, ${Number(c.screening_detectable || 0).toLocaleString()} screening points; processing ${q.qualified ? "qualified" : "screening-only"}.`);
  return payload;
}

function addIsoBistaticEllipse(tx, rx, pathRangeM) {
  const midLat = (tx.lat + rx.lat) / 2.0;
  const midLon = (tx.lon + rx.lon) / 2.0;
  const txVec = latLonToLocalEnu(midLat, midLon, tx.lat, tx.lon);
  const rxVec = latLonToLocalEnu(midLat, midLon, rx.lat, rx.lon);
  const dx = rxVec.east - txVec.east;
  const dy = rxVec.north - txVec.north;
  const baseline = Math.hypot(dx, dy);
  const a = Number(pathRangeM) / 2.0;
  const c = baseline / 2.0;
  if (!Number.isFinite(a) || a <= c || baseline < 0.01) return;
  const b = Math.sqrt(Math.max(0, a * a - c * c));
  const ux = dx / baseline;
  const uy = dy / baseline;
  const vx = -uy;
  const vy = ux;
  const points = [];
  for (let i = 0; i <= 160; i++) {
    const t = (2.0 * Math.PI * i) / 160.0;
    const major = a * Math.cos(t);
    const minor = b * Math.sin(t);
    points.push(offsetEnuToLatLon(
      midLat,
      midLon,
      major * ux + minor * vx,
      major * uy + minor * vy,
    ));
  }
  L.polyline(points.map((p) => [p.lat, p.lon]), {
    color: "#ffb000",
    weight: 2,
    opacity: 0.9,
    dashArray: "7,5",
  }).addTo(isacProbeLayerGroup);
}

function renderIsacTargetProbe(payload) {
  const selected = payload?.selected_target || null;
  const v = selected || payload?.values || {};
  const target = selected
    ? { latitude: selected.latitude, longitude: selected.longitude }
    : payload?.target;
  const txRaw = payload?.transmitter;
  const rxRaw = payload?.receiver;
  if (!target || !txRaw || !rxRaw) throw new Error("ISAC result is missing TX/RX/target geometry metadata.");
  const tx = { lat: Number(txRaw.latitude), lon: Number(txRaw.longitude) };
  const rx = { lat: Number(rxRaw.latitude), lon: Number(rxRaw.longitude) };
  const p = { lat: Number(target.latitude), lon: Number(target.longitude) };
  if (![tx.lat, tx.lon, rx.lat, rx.lon, p.lat, p.lon].every(Number.isFinite)) {
    throw new Error("ISAC result contains invalid TX/RX/target coordinates.");
  }

  isacProbeLayerGroup.clearLayers();
  const targetCellOverlay = payload?.target_measurement_overlay;
  if (targetCellOverlay?.png_b64 && Number.isFinite(Number(targetCellOverlay.radius_m))) {
    const r = Number(targetCellOverlay.radius_m);
    const sw = offsetEnuToLatLon(tx.lat, tx.lon, -r, -r);
    const ne = offsetEnuToLatLon(tx.lat, tx.lon, r, r);
    L.imageOverlay(targetCellOverlay.png_b64, L.latLngBounds([sw.lat, sw.lon], [ne.lat, ne.lon]), {
      opacity: 0.42, interactive: false, className: "isac-target-measurement-cell",
    }).addTo(isacProbeLayerGroup);
  }
  L.polyline([[tx.lat, tx.lon], [p.lat, p.lon], [rx.lat, rx.lon]], {
    color: "#00e5ff", weight: 3, opacity: 0.95,
  }).addTo(isacProbeLayerGroup);
  L.polyline([[tx.lat, tx.lon], [rx.lat, rx.lon]], {
    color: "#ffffff", weight: 1.5, opacity: 0.7, dashArray: "4,6",
  }).addTo(isacProbeLayerGroup);
  L.circleMarker([p.lat, p.lon], {
    radius: 7, color: "#ffeb3b", fillColor: "#ff5722", fillOpacity: 1, weight: 2,
  }).addTo(isacProbeLayerGroup).bindPopup("Active ISAC target").openPopup();
  L.circleMarker([rx.lat, rx.lon], {
    radius: 6, color: "#00ff88", fillColor: "#003b2a", fillOpacity: 1, weight: 2,
  }).addTo(isacProbeLayerGroup).bindTooltip("Analysis RX");
  addIsoBistaticEllipse(tx, rx, Number(v.bistatic_path_range_m));

  const fmt = (value, digits = 1) => Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "n/a";
  const yesNo = (value) => value === true ? "YES" : value === false ? "NO" : "n/a";
  const row = (name, value, units = "") => `<tr><td style="padding:2px 5px;color:#aaa;">${name}</td><td style="padding:2px 5px;text-align:right;color:#fff;">${value}${units ? ` ${units}` : ""}</td></tr>`;
  const queryDistance = selected?.query_distance_m ?? payload?.query_distance_m;
  const processing = payload?.processing_assessment || {};
  const bg = v?.static_background || {};
  const bgNearest = bg?.nearest_static_path || {};
  const bgContrib = Array.isArray(bg?.same_cell_contributors) ? bg.same_cell_contributors : [];
  const bgContributorText = bgContrib.length
    ? bgContrib.slice(0, 3).map((x) => `OSM ${x.building_id ?? "?"} ${x.material || "unknown"}`).join("; ")
    : "none in same ideal cell";
  const escHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
  const bgPathTable = bgContrib.length ? `<details style="margin:5px 0;"><summary style="cursor:pointer;color:#c9a7ff;">Same-cell facade path table (${bgContrib.length})</summary>
    <div style="overflow-x:auto;"><table style="width:100%;font-size:10px;border-collapse:collapse;margin-top:4px;">
      <thead><tr><th>#</th><th>OSM</th><th>material</th><th>delay µs</th><th>path km</th><th>bounce lat,lon</th></tr></thead>
      <tbody>${bgContrib.slice(0, 10).map((x, i) => {
        const blat = Number(x.bounce_latitude_deg), blon = Number(x.bounce_longitude_deg);
        const bounce = Number.isFinite(blat) && Number.isFinite(blon) ? `${blat.toFixed(6)}, ${blon.toFixed(6)}` : "n/a";
        return `<tr><td>${i + 1}</td><td>${escHtml(x.building_id ?? "?")}</td><td>${escHtml(x.material || "unknown")}</td><td>${fmt(Number(x.excess_delay_s) * 1e6, 3)}</td><td>${fmt(Number(x.total_path_m) / 1000, 3)}</td><td>${bounce}</td></tr>`;
      }).join("")}</tbody>
    </table></div></details>` : "";
  // Draw only mapped facade returns that occupy the selected target's
  // current ideal delay-Doppler cell. These are background-scatter paths, not
  // target paths; keeping them in the same probe layer makes the distinction
  // visible without changing the target hypothesis.
  bgContrib.slice(0, 3).forEach((scatter, rank) => {
    const blat = Number(scatter?.bounce_latitude_deg);
    const blon = Number(scatter?.bounce_longitude_deg);
    if (!Number.isFinite(blat) || !Number.isFinite(blon)) return;
    L.polyline([[tx.lat, tx.lon], [blat, blon], [rx.lat, rx.lon]], {
      color: rank === 0 ? "#d27cff" : "#8d6bb7", weight: rank === 0 ? 2.2 : 1.3,
      opacity: rank === 0 ? 0.82 : 0.55, dashArray: "5,5",
    }).addTo(isacProbeLayerGroup);
    L.circleMarker([blat, blon], {
      radius: rank === 0 ? 5 : 3.5, color: "#e0b3ff", fillColor: "#6c2a8f", fillOpacity: 0.85, weight: 1,
    }).addTo(isacProbeLayerGroup).bindTooltip(`Static facade return: OSM ${scatter.building_id ?? "?"} / ${scatter.material || "unknown"}`);
  });
  const delayDopplerSvg = (() => {
    const paths = Array.isArray(bg?.delay_doppler_paths) ? bg.delay_doppler_paths : [];
    const targetDelayUs = Number(v?.excess_delay_s) * 1e6;
    const targetDopplerHz = Number(v?.doppler_hz);
    if (!paths.length || !Number.isFinite(targetDelayUs) || !Number.isFinite(targetDopplerHz)) return "";
    const delayResUs = Math.max(Number(bg?.delay_resolution_s) * 1e6 || 0, 1e-6);
    const dopplerResHz = Math.max(Number(bg?.doppler_resolution_hz) || 0, 1e-6);
    const delays = paths.map((x) => Number(x.excess_delay_s) * 1e6).filter(Number.isFinite);
    if (!delays.length) return "";
    let xMin = Math.min(targetDelayUs, ...delays);
    let xMax = Math.max(targetDelayUs, ...delays);
    const xPad = Math.max(delayResUs * 2, (xMax - xMin) * 0.06, 0.05);
    xMin -= xPad; xMax += xPad;
    const yAbs = Math.max(Math.abs(targetDopplerHz) * 1.15, dopplerResHz * 2.0, 1.0);
    const W = 360, H = 180, L = 46, R = 10, T = 18, B = 34;
    const sx = (x) => L + (x - xMin) / Math.max(xMax - xMin, 1e-9) * (W - L - R);
    const sy = (y) => T + (yAbs - y) / (2 * yAbs) * (H - T - B);
    const circles = paths.map((x) => {
      const dx = Number(x.excess_delay_s) * 1e6;
      if (!Number.isFinite(dx)) return "";
      return `<circle cx="${sx(dx).toFixed(2)}" cy="${sy(0).toFixed(2)}" r="3.4" fill="#64b5f6" fill-opacity="0.72" stroke="#d7efff" stroke-width="0.6"/>`;
    }).join("");
    const cellX0 = sx(targetDelayUs - 0.5 * delayResUs);
    const cellX1 = sx(targetDelayUs + 0.5 * delayResUs);
    const cellY0 = sy(0.5 * dopplerResHz);
    const cellY1 = sy(-0.5 * dopplerResHz);
    return `<div style="margin:7px 0 3px;color:#aaa;font-size:10px;">Mapped facade specular paths in ideal delay–Doppler coordinates (geometry only)</div>
      <svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:420px;background:#10151d;border:1px solid #2d3c4d;border-radius:4px;">
        <line x1="${L}" y1="${sy(0).toFixed(2)}" x2="${W-R}" y2="${sy(0).toFixed(2)}" stroke="#66788a" stroke-width="1"/>
        <line x1="${L}" y1="${T}" x2="${L}" y2="${H-B}" stroke="#66788a" stroke-width="1"/>
        <rect x="${Math.min(cellX0,cellX1).toFixed(2)}" y="${Math.min(cellY0,cellY1).toFixed(2)}" width="${Math.max(Math.abs(cellX1-cellX0),1).toFixed(2)}" height="${Math.max(Math.abs(cellY1-cellY0),1).toFixed(2)}" fill="#ffeb3b" fill-opacity="0.08" stroke="#ffeb3b" stroke-dasharray="3,2" stroke-width="1"/>
        ${circles}
        <circle cx="${sx(targetDelayUs).toFixed(2)}" cy="${sy(targetDopplerHz).toFixed(2)}" r="5.5" fill="#ff5722" stroke="#ffeb3b" stroke-width="1.5"/>
        <text x="${L}" y="${H-8}" fill="#aebdca" font-size="10">excess delay (µs): ${xMin.toFixed(2)} … ${xMax.toFixed(2)}</text>
        <text x="6" y="12" fill="#aebdca" font-size="10">Doppler ±${yAbs.toFixed(2)} Hz</text>
        <text x="${W-145}" y="${H-8}" fill="#ffdb86" font-size="10">target ${targetDelayUs.toFixed(3)} µs / ${targetDopplerHz.toFixed(2)} Hz</text>
      </svg>`;
  })();
  const output = document.getElementById("channel-target-probe-output");
  if (output) {
    output.hidden = false;
    output.innerHTML = `
      <div style="font-weight:700;color:#fff;margin:6px 0;">Active target analysis</div>
      <table style="width:100%;border-collapse:collapse;font-size:11px;">
        <tbody>
          ${row("Target", `${p.lat.toFixed(6)}, ${p.lon.toFixed(6)}`)}
          ${row("Nearest scene sample", fmt(queryDistance, 1), "m")}
          ${row("Scene interpolation", v?.scene_interpolation?.method || "n/a")}
          ${row("Interpolation max neighbor", fmt(v?.scene_interpolation?.max_neighbor_distance_m, 1), "m")}
          ${row("Delay resolution", fmt(Number(v?.measurement_cell?.delay_resolution_s) * 1e6, 3), "µs")}
          ${row("Doppler resolution", fmt(v?.measurement_cell?.doppler_resolution_hz, 3), "Hz")}
          ${row("Locations in same ideal delay-Doppler cell", v?.measurement_cell?.joint_cell_point_count ?? "n/a")}
          ${row("Target height AGL", fmt(v.target_height_agl_m ?? payload?.target?.heightMagl, 1), "m")}
          ${row("Bistatic RCS", fmt(v.bistatic_rcs_m2 ?? payload?.target?.bistaticRcsM2, 4), "m²")}
          ${row("Motion", `${fmt(payload?.motion?.speedMps, 2)} m/s @ ${fmt(payload?.motion?.headingDegTrue, 1)}° true, climb ${fmt(payload?.motion?.climbRateMps, 2)} m/s`)}
          <tr><td colspan="2" style="padding-top:6px;color:#5fd7ff;font-weight:700;">TX → target illumination</td></tr>
          ${row("Range", fmt(Number(v.tx_target_range_m) / 1000, 3), "km")}
          ${row("Incident isotropic power", fmt(v.incident_isotropic_power_dbm, 2), "dBm")}
          ${row("Propagation path loss", fmt(v.tx_target_path_loss_db, 2), "dB")}
          ${row("Environment / terrain loss", `${fmt(v.tx_target_environment_loss_db, 2)} / ${fmt(v.tx_target_terrain_loss_db, 2)}`, "dB")}
          ${row("TX→target LOS", yesNo(v.tx_target_los))}
          ${row("TX-field sample error", fmt(v.tx_target_sample_error_m, 1), "m")}
          <tr><td colspan="2" style="padding-top:6px;color:#5fd7ff;font-weight:700;">Target → RX environment</td></tr>
          ${row("Range", fmt(Number(v.target_receiver_range_m) / 1000, 3), "km")}
          ${row("Propagation loss", fmt(v.return_path_loss_db, 2), "dB")}
          ${row("Environment loss", fmt(v.return_environment_loss_db, 2), "dB")}
          ${row("Terrain loss", fmt(v.return_terrain_loss_db, 2), "dB")}
          ${row("LOS", yesNo(v.return_los))}
          ${row("Environment sample valid", yesNo(v.return_environment_valid))}
          <tr><td colspan="2" style="padding-top:6px;color:#5fd7ff;font-weight:700;">Bistatic geometry / observability</td></tr>
          ${row("Total path", fmt(Number(v.bistatic_path_range_m) / 1000, 3), "km")}
          ${row("Bistatic angle", fmt(v.bistatic_angle_deg, 2), "deg")}
          ${row("Excess delay", fmt(Number(v.excess_delay_s) * 1e6, 3), "µs")}
          ${row("Signed Doppler", fmt(v.doppler_hz, 2), "Hz")}
          ${row("Doppler sensitivity", fmt(v.doppler_sensitivity_hz_per_mps, 3), "Hz/(m/s)")}
          ${row("Best-heading min speed", fmt(v.minimum_detectable_speed_mps, 2), "m/s")}
          ${row("Doppler resolved", yesNo(v.doppler_resolved))}
          ${row("Doppler ambiguous", yesNo(v.doppler_ambiguous))}
          <tr><td colspan="2" style="padding-top:6px;color:#5fd7ff;font-weight:700;">Mapped static-building background</td></tr>
          ${row("Background status", bg.status || bg.reason || "not available")}
          ${row("Facades scanned", bg.candidate_walls == null ? "n/a" : String(bg.candidate_walls))}
          ${row("Finite specular candidates", bg.geometric_specular_candidates == null ? "n/a" : String(bg.geometric_specular_candidates))}
          ${row("Visibility candidates evaluated", bg.evaluated_wall_candidates == null ? "n/a" : String(bg.evaluated_wall_candidates))}
          ${row("Visibility search complete", yesNo(bg.candidate_search_complete))}
          ${row("Accepted visible specular paths", bg.accepted_paths == null ? "n/a" : String(bg.accepted_paths))}
          ${row("Specular geometry rejected", bg?.rejection_counts?.geometry_rejected ?? "n/a")}
          ${row("TX visibility rejected", bg?.rejection_counts?.visibility_tx_blocked ?? "n/a")}
          ${row("RX visibility rejected", bg?.rejection_counts?.visibility_rx_blocked ?? "n/a")}
          ${row("Target in static Doppler cell", yesNo(bg.target_in_static_doppler_cell))}
          ${row("Same-cell mapped path count", bg.same_cell_path_count == null ? "n/a" : String(bg.same_cell_path_count))}
          ${row("Nearest facade-clutter delay separation", bg.nearest_static_path_delay_separation_s == null ? "n/a" : fmt(Number(bg.nearest_static_path_delay_separation_s) * 1e6, 3), bg.nearest_static_path_delay_separation_s == null ? "" : "µs")}
          ${row("Nearest mapped reflector", bgNearest.building_id == null ? "n/a" : `OSM ${bgNearest.building_id} / ${bgNearest.material || "unknown"}`)}
          ${row("Same-cell contributors", bgContributorText)}
          ${bgPathTable ? `<tr><td colspan="2" style="padding:2px 0;">${bgPathTable}</td></tr>` : ""}
          ${delayDopplerSvg ? `<tr><td colspan="2" style="padding:4px 0;">${delayDopplerSvg}</td></tr>` : ""}
          <tr><td colspan="2" style="padding-top:6px;color:#5fd7ff;font-weight:700;">Echo / detector constraints</td></tr>
          ${row("Echo power", fmt(v.echo_power_dbm, 2), "dBm")}
          ${row("Post-processing SNR", fmt(v.postprocessing_snr_db, 2), "dB")}
          ${row("SNR margin vs effective N+I", fmt(v.detection_margin_db, 2), "dB")}
          ${row("Minimum detectable RCS", fmt(v.minimum_detectable_rcs_m2, 4), "m²")}
          ${row("RCS margin", fmt(v.rcs_margin_db, 2), "dB")}
          ${row("Echo / residual-direct ratio", fmt(v.echo_to_residual_direct_db, 2), "dB")}
          ${row("Direct-path constraint margin", fmt(v.direct_residual_margin_db, 2), "dB")}
          ${row("Required cancellation", fmt(v.required_cancellation_db, 2), "dB")}
          ${row("Required simultaneous dynamic range", fmt(v.required_dynamic_range_db, 2), "dB")}
          ${row("SNR vs noise+interference constraint", yesNo(v.snr_noise_interference_ok))}
          ${row("Direct residual constraint", yesNo(v.direct_residual_ok))}
          ${row("Dynamic-range constraint", yesNo(v.dynamic_range_ok))}
          ${row("Screening detectable", yesNo(v.detectable_screening))}
          ${row("Processing-qualified detectable", yesNo(v.detectable_qualified ?? v.detectable))}
          ${row("Failed constraints", Array.isArray(v.failed_constraints) && v.failed_constraints.length ? v.failed_constraints.join(", ") : "none")}
          <tr><td colspan="2" style="padding-top:6px;color:#5fd7ff;font-weight:700;">Processing / interference basis</td></tr>
          ${row("Thermal noise", fmt(processing.thermal_noise_power_dbm, 2), "dBm")}
          ${row("Interference + clutter", processing.interference_plus_clutter_power_dbm == null ? "not supplied" : fmt(processing.interference_plus_clutter_power_dbm, 2) + " dBm")}
          ${row("Effective N+I", fmt(processing.effective_noise_plus_interference_dbm, 2), "dBm")}
          ${row("Processing gain source", processing.gain_source || "plan-time product")}
          ${row("Ideal BT gain", fmt(processing.ideal_time_bandwidth_gain_db, 2), "dB")}
          ${row("Gain used", fmt(processing.used_processing_gain_db, 2), "dB")}
          ${row("Effective gain qualified", yesNo(processing.effective_gain_qualified))}
          ${row("Interference basis qualified", yesNo(processing.interference_input_qualified))}
          ${row("Qualified detector basis", yesNo(processing.qualified))}
          ${row("Direct RX power", fmt(payload?.direct_path?.received_power_dbm, 2), "dBm")}
          ${row("Residual direct after cancellation", fmt(payload?.direct_path?.residual_after_cancellation_dbm, 2), "dBm")}
          ${row("Direct-path model", payload?.direct_path?.model || "n/a")}
        </tbody>
      </table>`;
  }
}

async function tryIsacTargetProbe(e) {
  const enabled = !!document.getElementById("channel-analysis-enabled")?.checked;
  const productId = String(currentChannelProduct()?.product_id || "").trim();
  if (!enabled || !productId) return false;
  const lat = Number(e?.latlng?.lat);
  const lon = Number(e?.latlng?.lng);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return true;
  try {
    const selected = { latitude: lat, longitude: lon };
    const targetEl = document.getElementById("channel-selected-target-location");
    if (targetEl) targetEl.value = `${lat.toFixed(8)}, ${lon.toFixed(8)}`;
    setStatus("Evaluating active ISAC target against the reusable scene...");
    await evaluateIsacScene({ selected, render: true });
  } catch (err) {
    console.error("ISAC target evaluation failed:", err);
    setStatus(`ISAC target evaluation failed: ${err}`);
  }
  return true;
}

function renderHeatmap(result) {
  const grid = (result && result.grid) || {};
  const lats = grid.cell_lat;
  const lons = grid.cell_lon;
  const rsrp = grid.rsrp_dbm || grid.received_power_dbm || grid.field_strength_dbuv_m;
  let layer = (window.RFTerrainParams && RFTerrainParams.getCoverageDisplayLayer()) || "rsrp";
  const h = result.heatmap;
  if (h && h.layer === "field_strength_dbuv_m" && (layer === "rsrp" || !layer)) {
    layer = "field_strength";
    const layerEl = document.getElementById("coverage-display-layer");
    if (layerEl) layerEl.value = "field_strength";
  }
  const pngSrc = h && h.png_b64 ? h.png_b64 : null;
  const tx = result.snapped_tx || grid.tx || result.original_point;
  const txLat = tx && Number.isFinite(tx.lat) ? tx.lat : null;
  const txLon = tx && Number.isFinite(tx.lon) ? tx.lon : null;
  let radiusM =
    (h && Number.isFinite(h.radius_m) ? Number(h.radius_m) : NaN) ||
    (grid.rf_params && Number.isFinite(grid.rf_params.max_range_m) ? Number(grid.rf_params.max_range_m) : NaN);
  const hasCells = !!(lats && lats.length > 0 && lons && lons.length > 0 && rsrp && rsrp.length > 0);
  const hasLayerCells =
    hasCells &&
    (layer === "rsrp" ||
      (layer === "sinr" && Array.isArray(grid.sinr_db)) ||
      (layer === "carrier_to_noise" && (Array.isArray(grid.carrier_to_noise_db) || Array.isArray(grid.sinr_db))) ||
      (layer === "terrain_shadow" && (Array.isArray(grid.terrain_loss_db) || Array.isArray(grid.los_terrain))) ||
      (layer === "field_strength" && Array.isArray(grid.field_strength_dbuv_m)) ||
      (layer === "received_power" && Array.isArray(grid.received_power_dbm)) ||
      (layer === "incident_power" && Array.isArray(grid.incident_power_isotropic_dbm)) ||
      (layer === "bistatic_echo" && Array.isArray(grid.bistatic_echo_power_dbm)) ||
      (layer === "bistatic_snr" && Array.isArray(grid.bistatic_postprocessing_snr_db)) ||
      (layer === "bistatic_margin" && Array.isArray(grid.bistatic_detection_margin_db)) ||
      (layer === "bistatic_doppler" && Array.isArray(grid.bistatic_doppler_hz)) ||
      (layer === "bistatic_range" && Array.isArray(grid.bistatic_path_range_m)) ||
      (layer === "bistatic_delay" && Array.isArray(grid.bistatic_excess_delay_s)) ||
      (layer === "bistatic_angle" && Array.isArray(grid.bistatic_angle_deg)) ||
      (layer === "bistatic_detectable" && Array.isArray(grid.bistatic_detectable)));
  let layerHeatmap = h;
  if (result?._interactive_isac?.layer?.png_b64 && result._interactive_isac.layer.layer === layer) {
    layerHeatmap = result._interactive_isac.layer;
  } else if (layer === "terrain_shadow" && result.heatmap_terrain?.png_b64) {
    layerHeatmap = result.heatmap_terrain;
  } else if (layer === "sinr" && result.heatmap_sinr?.png_b64) {
    layerHeatmap = result.heatmap_sinr;
  } else if (layer === "carrier_to_noise" && result.heatmap_carrier_to_noise?.png_b64) {
    layerHeatmap = result.heatmap_carrier_to_noise;
  } else if (layer === "received_power" && result.heatmap_received_power?.png_b64) {
    layerHeatmap = result.heatmap_received_power;
  } else if (layer === "incident_power" && result.heatmap_incident_power?.png_b64) {
    layerHeatmap = result.heatmap_incident_power;
  } else if (layer === "bistatic_echo" && result.heatmap_bistatic_echo?.png_b64) {
    layerHeatmap = result.heatmap_bistatic_echo;
  } else if (layer === "bistatic_snr" && result.heatmap_bistatic_snr?.png_b64) {
    layerHeatmap = result.heatmap_bistatic_snr;
  } else if (layer === "bistatic_margin" && result.heatmap_bistatic_margin?.png_b64) {
    layerHeatmap = result.heatmap_bistatic_margin;
  } else if (layer === "bistatic_doppler" && result.heatmap_bistatic_doppler?.png_b64) {
    layerHeatmap = result.heatmap_bistatic_doppler;
  } else if (layer === "bistatic_range" && result.heatmap_bistatic_path_range?.png_b64) {
    layerHeatmap = result.heatmap_bistatic_path_range;
  } else if (layer === "bistatic_delay" && result.heatmap_bistatic_excess_delay?.png_b64) {
    layerHeatmap = result.heatmap_bistatic_excess_delay;
  } else if (layer === "bistatic_angle" && result.heatmap_bistatic_angle?.png_b64) {
    layerHeatmap = result.heatmap_bistatic_angle;
  } else if (layer === "bistatic_detectable" && result.heatmap_bistatic_detectable?.png_b64) {
    layerHeatmap = result.heatmap_bistatic_detectable;
  }
  const layerPngSrc = layerHeatmap && layerHeatmap.png_b64 ? layerHeatmap.png_b64 : null;
  if (layerHeatmap && Number.isFinite(layerHeatmap.radius_m)) {
    radiusM = Number(layerHeatmap.radius_m);
  }
  const hasPngDrape =
    !!(layerPngSrc && Number.isFinite(txLat) && Number.isFinite(txLon) && Number.isFinite(radiusM) && radiusM > 0);

  if (!hasPngDrape && !hasLayerCells) {
    setStatus("No grid cells and no heatmap image returned.");
    return;
  }

  const heatmapLayerGroup = window.currentHeatmapLayerGroup;
  if (!heatmapLayerGroup) {
    console.error("[RF Planner] No currentHeatmapLayerGroup set before renderHeatmap()");
    return;
  }

  let actualMin = Infinity;
  let actualMax = -Infinity;
  if (hasLayerCells) {
    for (let i = 0; i < lats.length; i++) {
      const v = window.RFTerrainParams
        ? RFTerrainParams.pickSampleMetric(grid, i, layer)
        : rsrp[i];
      if (!Number.isFinite(v)) continue;
      if (v < actualMin) actualMin = v;
      if (v > actualMax) actualMax = v;
    }
  }
  if (layerHeatmap && Number.isFinite(layerHeatmap.actual_min)) actualMin = Number(layerHeatmap.actual_min);
  if (layerHeatmap && Number.isFinite(layerHeatmap.actual_max)) actualMax = Number(layerHeatmap.actual_max);
  if (!Number.isFinite(actualMin) || !Number.isFinite(actualMax) || (hasLayerCells && actualMin === Infinity)) {
    if (layer === "terrain_shadow") {
      actualMin = 0;
      actualMax = 40;
    } else if (layer === "field_strength") {
      actualMin = 20;
      actualMax = 120;
    } else if (layer === "sinr" || layer === "carrier_to_noise") {
      actualMin = -5;
      actualMax = 30;
    } else if (layer === "received_power" || layer === "incident_power") {
      actualMin = -140;
      actualMax = -20;
    } else if (layer === "bistatic_echo") {
      actualMin = -200;
      actualMax = -80;
    } else if (layer === "bistatic_snr") {
      actualMin = -40;
      actualMax = 30;
    } else if (layer === "bistatic_margin") {
      actualMin = -40;
      actualMax = 20;
    } else if (layer === "bistatic_doppler") {
      actualMin = -500;
      actualMax = 500;
    } else if (layer === "bistatic_range") {
      actualMin = 0;
      actualMax = 50;
    } else if (layer === "bistatic_delay") {
      actualMin = 0;
      actualMax = 100;
    } else if (layer === "bistatic_angle") {
      actualMin = 0;
      actualMax = 180;
    } else if (layer === "bistatic_detectable" || layer === "screening_detectable" || layer === "qualified_detectable" || layer === "static_clutter_overlap" || layer === "target_measurement_cell") {
      actualMin = 0;
      actualMax = 1;
    } else if (layer === "static_clutter_path_count") {
      actualMin = 0;
      actualMax = 10;
    } else if (layer === "static_clutter_delay_separation") {
      actualMin = 0;
      actualMax = 20;
    } else if (layer === "doppler_sensitivity" || layer === "minimum_detectable_speed" || layer === "required_cancellation") {
      actualMin = 0;
      actualMax = 100;
    } else if (layer === "minimum_detectable_rcs" || layer === "rcs_margin" || layer === "direct_residual_margin") {
      actualMin = -40;
      actualMax = 40;
    } else {
      actualMin = FIXED_RSRP_MIN;
      actualMax = FIXED_RSRP_MAX;
    }
  }

  if (layer === "terrain_shadow") {
    updateRSRPLegend(0, 40, actualMin, actualMax);
  } else if (layer === "field_strength") {
    updateRSRPLegend(20, 120, actualMin, actualMax);
  } else if (layer === "sinr" || layer === "carrier_to_noise") {
    updateRSRPLegend(-5, 30, actualMin, actualMax);
  } else if (layer === "received_power" || layer === "incident_power") {
    updateRSRPLegend(-140, -20, actualMin, actualMax);
  } else if (layer === "bistatic_echo") {
    updateRSRPLegend(-200, -80, actualMin, actualMax);
  } else if (layer === "bistatic_snr") {
    updateRSRPLegend(-40, 30, actualMin, actualMax);
  } else if (layer === "bistatic_margin") {
    updateRSRPLegend(-40, 20, actualMin, actualMax);
  } else if (layer === "bistatic_doppler") {
    const limit = Math.max(Math.abs(actualMin), Math.abs(actualMax), 1);
    updateRSRPLegend(-limit, limit, actualMin, actualMax);
  } else if (layer === "bistatic_range") {
    updateRSRPLegend(Math.min(actualMin, actualMax), Math.max(actualMax, actualMin + 0.001), actualMin, actualMax);
  } else if (layer === "bistatic_delay") {
    updateRSRPLegend(0, Math.max(actualMax, 1), actualMin, actualMax);
  } else if (layer === "bistatic_angle") {
    updateRSRPLegend(0, 180, actualMin, actualMax);
  } else if (["bistatic_detectable", "screening_detectable", "qualified_detectable", "static_clutter_overlap", "target_measurement_cell"].includes(layer)) {
    updateRSRPLegend(0, 1, actualMin, actualMax);
  } else if (layerHeatmap && Number.isFinite(Number(layerHeatmap.vmin)) && Number.isFinite(Number(layerHeatmap.vmax))) {
    updateRSRPLegend(Number(layerHeatmap.vmin), Number(layerHeatmap.vmax), actualMin, actualMax);
  } else if (["minimum_detectable_speed", "doppler_sensitivity", "required_cancellation"].includes(layer)) {
    updateRSRPLegend(Math.min(0, actualMin), Math.max(actualMax, 1), actualMin, actualMax);
  } else if (["minimum_detectable_rcs", "rcs_margin", "direct_residual_margin"].includes(layer)) {
    updateRSRPLegend(actualMin, Math.max(actualMax, actualMin + 0.001), actualMin, actualMax);
  } else {
    updateRSRPLegend(FIXED_RSRP_MIN, FIXED_RSRP_MAX, actualMin, actualMax);
  }

  if (hasPngDrape) {
    const sw = offsetEnuToLatLon(txLat, txLon, -radiusM, -radiusM);
    const ne = offsetEnuToLatLon(txLat, txLon, radiusM, radiusM);
    const bounds = L.latLngBounds([sw.lat, sw.lon], [ne.lat, ne.lon]);
    L.imageOverlay(layerPngSrc, bounds, {
      opacity: 0.78,
      interactive: false,
      className: "rf-heatmap-drape",
    }).addTo(heatmapLayerGroup);
  } else {
    const min = actualMin;
    const max = actualMax;
    for (let i = 0; i < lats.length; i++) {
      const v = window.RFTerrainParams
        ? RFTerrainParams.pickSampleMetric(grid, i, layer)
        : rsrp[i];
      if (!Number.isFinite(v)) continue;
      const color = rsrpToColor(v, min, max);
      L.circle([lats[i], lons[i]], {
        radius: 10,
        color: color,
        fillColor: color,
        fillOpacity: 0.6,
        weight: 0,
      }).addTo(heatmapLayerGroup);
    }
  }

  const viewTx = result.snapped_tx || result.original_point;
  if (viewTx && Number.isFinite(viewTx.lat) && Number.isFinite(viewTx.lon)) {
    map.setView([viewTx.lat, viewTx.lon], map.getZoom());
  }

  if (!window.RFWaveformUI?.isDvt() && result.sectors && result.sectors.length > 0 && viewTx) {
    drawSectorVisualization(result.sectors, viewTx, result.grid);
  }
}

function rsrpToColor(v, min, max) {
  // Multi-hue gradient for better visual distinction
  // Blue (weak) → Cyan → Green → Yellow → Orange → Red (strong)
  // This provides much better contrast than blue→magenta
  const t = Math.max(0, Math.min(1, (v - min) / (max - min + 1e-6)));
  
  let r, g, b;
  
  if (t < 0.2) {
    // Blue to Cyan (0.0 - 0.2)
    const localT = t / 0.2;
    r = 0;
    g = Math.round(255 * localT);
    b = 255;
  } else if (t < 0.4) {
    // Cyan to Green (0.2 - 0.4)
    const localT = (t - 0.2) / 0.2;
    r = 0;
    g = 255;
    b = Math.round(255 * (1 - localT));
  } else if (t < 0.6) {
    // Green to Yellow (0.4 - 0.6)
    const localT = (t - 0.4) / 0.2;
    r = Math.round(255 * localT);
    g = 255;
    b = 0;
  } else if (t < 0.8) {
    // Yellow to Orange (0.6 - 0.8)
    const localT = (t - 0.6) / 0.2;
    r = 255;
    g = Math.round(255 * (1 - localT * 0.5));
    b = 0;
  } else {
    // Orange to Red (0.8 - 1.0)
    const localT = (t - 0.8) / 0.2;
    r = 255;
    g = Math.round(255 * (0.5 - 0.5 * localT));
    b = 0;
  }
  
  return `rgb(${r},${g},${b})`;
}

// Draw sector visualization on map (cones for partial sectors, circles for 360°)
function drawSectorVisualization(sectors, txPoint, grid) {
  if (!sectors || sectors.length === 0 || !txPoint) {
    return;
  }

  // Remove previous sector overlays before drawing current ones
  // Prevents stacked semi-transparent circles when multiple TX plans overlap
  sectorLayerGroup.clearLayers();

  const txLat = txPoint.lat;
  const txLon = txPoint.lon;
  
  // Find max range from grid cells to determine sector radius
  let maxRange = 0;
  if (grid && grid.cell_lat && grid.cell_lat.length > 0) {
    // Calculate max distance from TX to any cell
    for (let i = 0; i < grid.cell_lat.length; i++) {
      const cellLat = grid.cell_lat[i];
      const cellLon = grid.cell_lon[i];
      const dist = L.latLng(txLat, txLon).distanceTo(L.latLng(cellLat, cellLon));
      if (dist > maxRange) {
        maxRange = dist;
      }
    }
  }
  
  // Default to 2000m if no grid data
  if (maxRange === 0) {
    maxRange = (grid && grid.rf_params && Number.isFinite(grid.rf_params.max_range_m))
      ? Number(grid.rf_params.max_range_m)
      : 2000;
  }
  
  // Color palette for sectors
  const sectorColors = [
    '#FF0000', // Red
    '#00FF00', // Green
    '#0000FF', // Blue
    '#FFFF00', // Yellow
    '#FF00FF', // Magenta
    '#00FFFF', // Cyan
    '#FFA500', // Orange
    '#800080', // Purple
  ];
  
  sectors.forEach((sector, index) => {
    const sectorType = sector.sector_type || "angle"; // Default to angle for backward compatibility
    const color = sectorColors[index % sectorColors.length];
    const opacity = 0.12;
    
    if (sectorType === "polygon") {
      // Draw polygon planning shape. This is a design aid, not an RF cutoff.
      if (sector.polygon_points && sector.polygon_points.length >= 2) {
        // Construct full polygon with TX as origin
        const polygonPoints = [[txLat, txLon], ...sector.polygon_points];
        
        L.polygon(polygonPoints, {
          color: color,
          fillColor: color,
          fillOpacity: opacity,
          weight: 2,
          dashArray: '5, 5',
        }).addTo(sectorLayerGroup).bindPopup(
          `Sector: ${sector.sector_id}<br>` +
          `Type: Planning polygon (${sector.polygon_points.length} points + TX origin)<br>` +
          `RF semantics: antenna pattern remains continuous beyond this shape<br>` +
          `Frequency: ${sector.freq_mhz} MHz<br>` +
          `TX Power: ${sector.tx_power_dbm} dBm`
        );
      }
    } else if (sectorType === "360") {
      // Do not draw an omnidirectional footprint overlay.
    } else {
      const startAngle = Number.isFinite(sector.start_angle_deg) ? sector.start_angle_deg : 0.0;
      const endAngle = Number.isFinite(sector.end_angle_deg) ? sector.end_angle_deg : 360.0;
      const derivedSpan = endAngle > startAngle
        ? (endAngle - startAngle)
        : (360 - startAngle + endAngle);
      const beamwidthH = Number.isFinite(sector.beamwidth_h_deg) ? sector.beamwidth_h_deg : derivedSpan;
      const azimuth = Number.isFinite(sector.azimuth_deg)
        ? sector.azimuth_deg
        : ((startAngle + derivedSpan / 2.0) % 360.0);
      // Use a short local tick so the overlay does not imply sector size/footprint.
      const markerLength = Math.min(60.0, Math.max(20.0, maxRange * 0.06));
      const centerPoint = calculateDestinationPoint(txLat, txLon, azimuth, markerLength);

      L.polyline([[txLat, txLon], [centerPoint.lat, centerPoint.lon]], {
        color: color,
        weight: 3,
        opacity: 0.9,
      }).addTo(sectorLayerGroup);

      L.polyline([[txLat, txLon], [centerPoint.lat, centerPoint.lon]], {
        color: color,
        weight: 10,
        opacity: 0.0,
      }).addTo(sectorLayerGroup).bindPopup(
        `Sector: ${sector.sector_id}<br>` +
        `Antenna orientation: azimuth ${azimuth.toFixed(1)}°<br>` +
        `Nominal horizontal beamwidth: ${beamwidthH.toFixed(1)}°<br>` +
        `Overlay intentionally does not show sector size or footprint<br>` +
        `Frequency: ${sector.freq_mhz} MHz<br>` +
        `TX Power: ${sector.tx_power_dbm} dBm`
      );
    }
  });
  
  console.log(`[RF Planner] Drew ${sectors.length} sector(s) visualization`);
  
  // Apply current visibility state
  const showSectorsToggle = document.getElementById("show-sectors-toggle");
  if (showSectorsToggle) {
    toggleSectorVisibility(showSectorsToggle.checked);
  }
}

// Toggle sector visibility
function toggleSectorVisibility(show) {
  if (show) {
    if (!map.hasLayer(sectorLayerGroup)) {
      map.addLayer(sectorLayerGroup);
    }
  } else {
    if (map.hasLayer(sectorLayerGroup)) {
      map.removeLayer(sectorLayerGroup);
    }
  }
  console.log(`[RF Planner] Sector overlays ${show ? 'shown' : 'hidden'}`);
}

// Helper function to calculate destination point from bearing and distance
function calculateDestinationPoint(lat, lon, bearingDeg, distanceM) {
  const R = 6371000; // Earth radius in meters
  const lat1 = lat * Math.PI / 180;
  const lon1 = lon * Math.PI / 180;
  const bearing = bearingDeg * Math.PI / 180;
  
  const lat2 = Math.asin(
    Math.sin(lat1) * Math.cos(distanceM / R) +
    Math.cos(lat1) * Math.sin(distanceM / R) * Math.cos(bearing)
  );
  
  const lon2 = lon1 + Math.atan2(
    Math.sin(bearing) * Math.sin(distanceM / R) * Math.cos(lat1),
    Math.cos(distanceM / R) - Math.sin(lat1) * Math.sin(lat2)
  );
  
  return {
    lat: lat2 * 180 / Math.PI,
    lon: lon2 * 180 / Math.PI
  };
}

function updateRSRPLegend(scaleMinRSRP, scaleMaxRSRP, actualMinRSRP, actualMaxRSRP) {
  const legend = document.getElementById("rsrp-legend");
  if (!legend) return;
  
  // Show legend
  legend.style.display = "block";
  
  // Update gradient (blue at bottom/weak, red at top/strong)
  // Multi-hue gradient: Blue → Cyan → Green → Yellow → Orange → Red
  // This provides much better visual distinction than blue→magenta
  const gradient = document.getElementById("rsrp-legend-gradient");
  if (gradient) {
    // Create a smooth multi-stop gradient
    gradient.style.background = `linear-gradient(to top, 
      rgb(0, 0, 255) 0%,      /* Blue - weak signal */
      rgb(0, 255, 255) 20%,   /* Cyan */
      rgb(0, 255, 0) 40%,     /* Green */
      rgb(255, 255, 0) 60%,   /* Yellow */
      rgb(255, 128, 0) 80%,   /* Orange */
      rgb(255, 0, 0) 100%     /* Red - strong signal */
    )`;
  }
  
  // Update labels on gradient (show fixed scale range)
  const maxLabel = document.getElementById("legend-max");
  const midLabel = document.getElementById("legend-mid");
  const minLabel = document.getElementById("legend-min");
  
  if (maxLabel) maxLabel.textContent = scaleMaxRSRP.toFixed(0);
  if (midLabel) midLabel.textContent = ((scaleMinRSRP + scaleMaxRSRP) / 2).toFixed(0);
  if (minLabel) minLabel.textContent = scaleMinRSRP.toFixed(0);
  
  // Update value display (show actual data range for reference)
  const maxValue = document.getElementById("legend-max-value");
  const minValue = document.getElementById("legend-min-value");
  
  if (maxValue) {
    maxValue.textContent = actualMaxRSRP.toFixed(1);
    // Add indicator if actual max is below scale max
    if (actualMaxRSRP < scaleMaxRSRP - 5) {
      maxValue.textContent += ` (scale: ${scaleMaxRSRP.toFixed(0)})`;
    }
  }
  if (minValue) {
    minValue.textContent = actualMinRSRP.toFixed(1);
    // Add indicator if actual min is above scale min
    if (actualMinRSRP > scaleMinRSRP + 5) {
      minValue.textContent += ` (scale: ${scaleMinRSRP.toFixed(0)})`;
    }
  }
  const selectedLayer = (typeof RFTerrainParams !== "undefined")
    ? RFTerrainParams.getCoverageDisplayLayer()
    : "rsrp";
  const legendUnit = selectedLayer === "field_strength" ? "dBµV/m"
    : ["rsrp", "received_power", "incident_power", "bistatic_echo"].includes(selectedLayer) ? "dBm"
    : selectedLayer === "bistatic_doppler" ? "Hz"
    : selectedLayer === "doppler_sensitivity" ? "Hz/(m/s)"
    : selectedLayer === "minimum_detectable_speed" ? "m/s"
    : selectedLayer === "minimum_detectable_rcs" ? "dBsm"
    : selectedLayer === "static_clutter_delay_separation" ? "µs"
    : selectedLayer === "static_clutter_path_count" ? "count"
    : selectedLayer === "bistatic_range" ? "km"
    : selectedLayer === "bistatic_delay" ? "µs"
    : selectedLayer === "bistatic_angle" ? "deg"
    : ["bistatic_detectable", "screening_detectable", "qualified_detectable", "static_clutter_overlap", "target_measurement_cell"].includes(selectedLayer) ? "flag"
    : "dB";
  const maxUnit = document.getElementById("legend-unit-max");
  const minUnit = document.getElementById("legend-unit-min");
  if (maxUnit) maxUnit.textContent = legendUnit;
  if (minUnit) minUnit.textContent = legendUnit;

  const titleEl = legend.querySelector("h3");
  if (titleEl && window.RFTerrainParams) {
    titleEl.textContent = RFTerrainParams.coverageLayerLabel(
      RFTerrainParams.getCoverageDisplayLayer(),
    );
  }
}

function displayMetadata(data) {
  // Create or update metadata display in sidebar
  let metaDiv = document.getElementById("metadata-display");
  if (!metaDiv) {
    metaDiv = document.createElement("div");
    metaDiv.id = "metadata-display";
    metaDiv.style.marginTop = "10px";
    metaDiv.style.padding = "8px";
    metaDiv.style.backgroundColor = "#1a1a1a";
    metaDiv.style.border = "1px solid #444";
    metaDiv.style.borderRadius = "4px";
    metaDiv.style.fontSize = "12px";
    document.getElementById("sidebar").appendChild(metaDiv);
  }
  
  const clutterType = data.clutter_type || "unknown";
  const worldSource = data.world_model_source || "unknown";
  const vlmUsed = data.vlm_used || false;
  const svAvailable = data.streetview_available || false;
  const technology = String(data.grid?.technology || data.rf_config_used?.technology || "5g_nr");
  const waveform = String(data.grid?.waveform || data.rf_config_used?.dvt?.waveform || (technology === "dvt" ? "baseline" : "5g_nr"));
  const waveformLabel = window.RFWaveformUI?.profileLabel(waveform) || waveform;
  
  let sourceBadge = worldSource === "geometry_vlm_refined" 
    ? '<span style="color: #4CAF50;">●</span> Geometry + VLM'
    : '<span style="color: #FFA500;">●</span> Geometry only';
  
  const channel = data.channel_analysis;
  const direct = channel?.direct_path;
  const best = channel?.best_detectable_point || channel?.best_margin_point;
  const product = channel?.data_product || data.channel_analysis_product;
  const productLink = document.getElementById("channel-product-download");
  if (productLink) {
    if (product?.download_url) {
      productLink.href = product.download_url;
      productLink.hidden = false;
      productLink.style.display = "inline-block";
    } else {
      productLink.hidden = true;
      productLink.style.display = "none";
    }
  }
  const productHtml = product?.download_url
    ? `<div style="margin-top:4px;"><a href="${product.download_url}" download>Download machine-readable channel grid (.npz)</a> <span class="rf-sidebar-muted-sm">(${Number(product.size_bytes || 0).toLocaleString()} bytes)</span></div>`
    : "";
  const channelHtml = channel ? `
    <div style="font-weight:bold; margin-top:8px; margin-bottom:4px;">Channel and bistatic analysis</div>
    <div>Direct path RX: <strong>${Number(direct?.received_power_dbm).toFixed(1)} dBm</strong></div>
    <div>Direct path C/N: <strong>${Number(direct?.carrier_to_noise_db).toFixed(1)} dB</strong></div>
    <div>Best echo: <strong>${Number(best?.echo_power_dbm).toFixed(1)} dBm</strong></div>
    <div>Best margin: <strong>${Number(best?.detection_margin_db).toFixed(1)} dB</strong></div>
    <div>Signed Doppler: <strong>${Number(best?.doppler_hz).toFixed(1)} Hz</strong></div>
    <div>Doppler resolved: <strong>${best?.doppler_resolved ? "yes" : "no"}</strong></div>
    <div>Detectable: <strong>${best?.detectable ? "yes" : "no"}</strong></div>
    <div class="rf-sidebar-muted-sm">${channel?.counts?.detectable_points || 0} detectable grid points; return model: <strong>${String(channel?.return_path_model || "unknown")}</strong>.</div>
    ${productHtml}
  ` : "";

  metaDiv.innerHTML = `
    <div style="font-weight: bold; margin-bottom: 6px;">Model Status</div>
    <div style="margin-bottom: 4px;">${sourceBadge}</div>
    <div style="margin-bottom: 4px;">Waveform: <strong>${waveformLabel}</strong> (${technology})</div>
    <div style="margin-bottom: 4px;">Clutter: <strong>${clutterType}</strong></div>
    <div style="margin-bottom: 4px;">Street View: ${svAvailable ? "✓ Available" : "✗ Not available"}</div>
    <div>VLM Refinement: ${vlmUsed ? "✓ Used" : "✗ Not used"}</div>
    ${channelHtml}
  `;
}

function displayPanorama(imgData, panoLocation) {
  // Create or update panorama display in sidebar
  let panoDiv = document.getElementById("panorama-display");
  if (!panoDiv) {
    panoDiv = document.createElement("div");
    panoDiv.id = "panorama-display";
    panoDiv.style.marginTop = "10px";
    panoDiv.style.maxHeight = "200px";
    panoDiv.style.overflow = "auto";
    panoDiv.style.border = "1px solid #444";
    panoDiv.style.borderRadius = "4px";
    document.getElementById("sidebar").appendChild(panoDiv);
  }
  
  let locationInfo = "";
  if (panoLocation) {
    locationInfo = `<div style="padding: 3px 5px; font-size: 10px; background: #333;">Location: ${panoLocation.lat}, ${panoLocation.lon}</div>`;
  }
  
  panoDiv.innerHTML = `
    <div style="padding: 5px; font-size: 11px; background: #222;">Street View Panorama</div>
    ${locationInfo}
    <img src="${imgData}" style="width: 100%; display: block;" alt="360° Panorama" />
  `;
}

// Same session keys as /3d (planner_3d.js): one queue for open tabs.
const REMOTE_PLAN_POLL_MS = 1500;
const REMOTE_PLAN_SEQ_STORAGE_KEY = "rfplanner3d_remote_plan_seq";
const REMOTE_PLAN_BOOT_STORAGE_KEY = "rfplanner3d_remote_plan_boot_utc";

function startRemotePlanRfPolling2d() {
  let lastSeq = 0;
  try {
    const raw = sessionStorage.getItem(REMOTE_PLAN_SEQ_STORAGE_KEY);
    if (raw) lastSeq = Math.max(0, Number(raw) || 0);
  } catch { /* ignore */ }

  const tick = async () => {
    try {
      const r = await fetch(
        `/api/ui/remote-plan-rf/poll?since_seq=${encodeURIComponent(String(lastSeq))}`
      );
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
      const seq = Number(j.seq) || 0;
      if (!j.new || seq <= lastSeq) return;
      lastSeq = seq;
      try {
        sessionStorage.setItem(REMOTE_PLAN_SEQ_STORAGE_KEY, String(lastSeq));
      } catch { /* ignore */ }
      if (j.status === "ready" && j.plan && typeof j.plan === "object") {
        const plan = j.plan;
        if (String(plan.mode || "") === "3d_rt") {
          setStatus("Remote result is 3d_rt (path trace). Open /3d to view. Use POST /api/plan (coverage) for 2D heatmap here.");
          return;
        }
        const surface = String(plan.planner_surface || "").toLowerCase();
        const rm = String(plan.ray_mode || "").toLowerCase();
        if (surface === "3d" || (surface !== "2d" && rm !== "2d" && rm !== "")) {
          console.info("RFPlanner2D remote poll skip (3D plan)", { seq, ray_mode: rm, planner_surface: surface });
          return;
        }
        console.info("RFPlanner2D remote poll apply", { seq, plan });
        const newHeatmapLayerGroup = L.layerGroup().addTo(map);
        heatmapLayerGroups.push(newHeatmapLayerGroup);
        window.currentHeatmapLayerGroup = newHeatmapLayerGroup;
        const olat = plan.original_point?.lat ?? plan.snapped_tx?.lat;
        const olon = plan.original_point?.lon ?? plan.snapped_tx?.lon;
        if (Number.isFinite(olat) && Number.isFinite(olon)) {
          map.setView([olat, olon], map.getZoom() || 15);
        }
        applyServerPlanTo2dView(plan, { statusMessage: "Remote plan applied to 2D map (from API / poll)." });
        const ola = plan.original_point?.lat ?? plan.snapped_tx?.lat;
        const olo = plan.original_point?.lon ?? plan.snapped_tx?.lon;
        const cacheCenter = Number.isFinite(ola) && Number.isFinite(olo) ? { lat: ola, lon: olo } : { lat: 0, lon: 0 };
        planResults.push({ lat: ola, lon: olo, out: plan, data: plan, cacheCenter });
      } else if (j.status === "error" && j.error) {
        const er = j.error;
        const detail =
          er.detail != null
            ? (typeof er.detail === "object" ? JSON.stringify(er.detail) : String(er.detail))
            : JSON.stringify(er);
        setStatus(`Remote Plan RF failed (HTTP ${er.status_code ?? "?"}): ${detail}`);
      }
    } catch { /* transient */ }
  };

  setInterval(tick, REMOTE_PLAN_POLL_MS);
  tick();
}

startRemotePlanRfPolling2d();
