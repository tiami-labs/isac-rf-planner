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
let heatmapLayerGroups = []; // Array to store multiple heatmap layer groups (one per RF plan)
let planResults = []; // Successful RF plan results for export (lat, lon, data)

// Expose for F12 console debugging (use window.RFPLANNER_DEBUG to avoid cache/scope issues)
window.RFPLANNER_DEBUG = {
  get currentLayerGroup() { return currentLayerGroup; },
  get sectorLayerGroup() { return sectorLayerGroup; },
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

// Initialize Ray Mode selector on page load (loadLastResults is called once in the main DOMContentLoaded below)
window.addEventListener('DOMContentLoaded', () => {
  fetch("/api/config").then(r => r.ok ? r.json() : null).then(cfg => {
    if (!cfg) return;
    const mode = String(cfg.default_ray_mode || "2d").toLowerCase();
    const sel = document.getElementById("ray-mode");
    if (sel && (mode === "2d" || mode === "3d")) sel.value = mode;
  }).catch(e => console.warn("[RF Planner] Failed to load /api/config for default ray mode:", e));
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
    
    // Collect sector configurations from UI
    const sectors = collectSectorConfigs();
    
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
      tx_height_m: txHeightM,
      rx_height_m: rxHeightM,
    };
    
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
    
    // Show snapped point (add to currentLayerGroup for markers/overlays, not heatmap layer)
    if (data.snapped_tx) {
      const snapped = data.snapped_tx;
      const snapDist = data.snap_distance_m || 0;
      const svAvailable = data.streetview_available || false;
      const vlmUsed = data.vlm_used || false;
      const worldSource = data.world_model_source || "unknown";
      const clutterType = data.clutter_type || "unknown";
      
      // Build popup with metadata
      let popupHtml = `Snapped to street<br>Distance: ${snapDist.toFixed(1)}m<br>`;
      popupHtml += `Clutter: ${clutterType}<br>`;
      popupHtml += `Model: ${worldSource === "geometry_vlm_refined" ? "Geometry + VLM" : "Geometry only"}<br>`;
      popupHtml += `Street View: ${svAvailable ? "✓ Available" : "✗ Not available"}`;
      
      L.marker([snapped.lat, snapped.lon], { 
        icon: L.divIcon({ className: "snapped-marker", html: "📍", iconSize: [24, 24] })
      })
        .addTo(currentLayerGroup)
        .bindPopup(popupHtml);
      
      // Draw line from clicked to snapped
      L.polyline([[lat, lng], [snapped.lat, snapped.lon]], {
        color: "yellow",
        weight: 2,
        dashArray: "5, 5",
      }).addTo(currentLayerGroup);
      
      // Update status with model source
      let statusMsg = `Snapped: ${snapDist.toFixed(1)}m. Clutter: ${clutterType}. `;
      if (vlmUsed) {
        statusMsg += "Model: Geometry + VLM. ";
      } else {
        statusMsg += "Model: Geometry only. ";
      }
      statusMsg += `Street View: ${svAvailable ? "Available" : "Not available"}. Computing RF...`;
      setStatus(statusMsg);
    }
    
    // Display metadata in sidebar
    displayMetadata(data);
    
    // Display Street View panorama if available
    if (data.panorama_image) {
      displayPanorama(data.panorama_image, data.panorama_location);
    }
    
    renderHeatmap(data);

    if (data.osm_buildings_for_client && typeof window.rf2dIngestPlannerOsm === "function") {
      try {
        window.rf2dIngestPlannerOsm(data.osm_buildings_for_client);
        console.log("[RF Planner] Reused planner OSM footprints for 2D ray tracer (shared cache).");
      } catch (e) {
        console.warn("[RF Planner] rf2dIngestPlannerOsm:", e);
      }
    }
    
    // Save results to localStorage (omit OSM blob — ray tracer keeps `rf2d_osm_scene_v1`; avoids quota blowups)
    try {
      const forStorage = { ...data };
      delete forStorage.osm_buildings_for_client;
      localStorage.setItem('rf_planning_last_result', JSON.stringify(forStorage));
      localStorage.setItem('rf_planning_timestamp', Date.now().toString());
      console.log("[RF Planner] Results saved to localStorage");
    } catch (e) {
      console.warn("[RF Planner] Failed to save to localStorage:", e);
    }
    
    setStatus("RF plan computed. Click another point or enter coordinates to re-run. (Results saved - will persist after refresh)");
    const cacheCenter = (data.snapped_tx && Number.isFinite(data.snapped_tx.lat) && Number.isFinite(data.snapped_tx.lon))
      ? { lat: data.snapped_tx.lat, lon: data.snapped_tx.lon }
      : { lat, lon: lng };
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
  
  // Remove all heatmap layer groups
  heatmapLayerGroups.forEach(layerGroup => {
    map.removeLayer(layerGroup);
  });
  heatmapLayerGroups = [];

  planResults = [];
  currentLayerGroup = L.layerGroup().addTo(map);
  sectorLayerGroup = L.layerGroup().addTo(map);
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

    const ts = new Date();
    const filename = `rf_planner_export_${ts.getFullYear()}-${String(ts.getMonth() + 1).padStart(2, "0")}-${String(ts.getDate()).padStart(2, "0")}_${String(ts.getHours()).padStart(2, "0")}${String(ts.getMinutes()).padStart(2, "0")}.zip`;

    if (window.RFExportUtils && typeof JSZip !== "undefined") {
      await window.RFExportUtils.createExportZip(fullViewBlob, [], metadata, filename);
      setStatus("Exported ZIP with full view and metadata.");
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

// Try to restore last results on page load
window.addEventListener('DOMContentLoaded', () => {
  loadRfParamsDefaults();
  if (!loadLastResults()) {
    setStatus("Click on the map or enter coordinates to run RF planning.");
  }
  
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
  
  // Handle coordinate form submission ("Plan RF" button)
  const coordForm = document.getElementById("coord-form");
  coordForm.addEventListener("submit", async (e) => {
    e.preventDefault();

    if (isPlanningQueue) return;
    const queue = getQueuedTxPoints();
    if (!queue || !queue.length) return;

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
          planResults.push({ lat: point.lat, lon: point.lon, out: result.data, data: result.data, cacheCenter: result.cacheCenter });
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
          planResults.push({ lat: point.lat, lon: point.lon, out: retryResult.data, data: retryResult.data, cacheCenter: retryResult.cacheCenter });
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
map.on("click", (e) => {
  const { lat, lng } = e.latlng;
  
  // Check if we're in polygon drawing mode
  if (polygonDrawingMode) {
    addPolygonPoint(lat, lng);
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
  setStatus(`TX added (${queueCount} queued): ${lat}, ${lng}. Add sector and click "Plan RF Queue" to run planning.`);
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

function renderHeatmap(result) {
  const grid = result.grid;
  const lats = grid.cell_lat;
  const lons = grid.cell_lon;
  const rsrp = grid.rsrp_dbm;

  if (!lats || lats.length === 0) {
    setStatus("No grid cells returned.");
    return;
  }

  const heatmapLayerGroup = window.currentHeatmapLayerGroup;
  if (!heatmapLayerGroup) {
    console.error("[RF Planner] No currentHeatmapLayerGroup set before renderHeatmap()");
    return;
  }

  let actualMin = Infinity;
  let actualMax = -Infinity;
  for (const v of rsrp) {
    if (v < actualMin) actualMin = v;
    if (v > actualMax) actualMax = v;
  }

  const h = result.heatmap;
  if (h && Number.isFinite(h.actual_min)) actualMin = Number(h.actual_min);
  if (h && Number.isFinite(h.actual_max)) actualMax = Number(h.actual_max);

  updateRSRPLegend(FIXED_RSRP_MIN, FIXED_RSRP_MAX, actualMin, actualMax);

  const pngSrc = h && h.png_b64 ? h.png_b64 : null;
  const tx = result.snapped_tx || grid.tx;
  const txLat = tx && Number.isFinite(tx.lat) ? tx.lat : null;
  const txLon = tx && Number.isFinite(tx.lon) ? tx.lon : null;
  let radiusM =
    (h && Number.isFinite(h.radius_m) ? Number(h.radius_m) : NaN) ||
    (grid.rf_params && Number.isFinite(grid.rf_params.max_range_m) ? Number(grid.rf_params.max_range_m) : NaN);

  if (pngSrc && Number.isFinite(txLat) && Number.isFinite(txLon) && Number.isFinite(radiusM) && radiusM > 0) {
    const sw = offsetEnuToLatLon(txLat, txLon, -radiusM, -radiusM);
    const ne = offsetEnuToLatLon(txLat, txLon, radiusM, radiusM);
    const bounds = L.latLngBounds([sw.lat, sw.lon], [ne.lat, ne.lon]);
    L.imageOverlay(pngSrc, bounds, {
      opacity: 0.78,
      interactive: false,
      className: "rf-heatmap-drape",
    }).addTo(heatmapLayerGroup);
  } else {
    const min = actualMin;
    const max = actualMax;
    for (let i = 0; i < lats.length; i++) {
      const v = rsrp[i];
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

  if (result.snapped_tx) {
    map.setView([result.snapped_tx.lat, result.snapped_tx.lon], map.getZoom());
  }

  if (result.sectors && result.sectors.length > 0 && result.snapped_tx) {
    drawSectorVisualization(result.sectors, result.snapped_tx, result.grid);
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
    maxRange = 2000;
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
  
  let sourceBadge = worldSource === "geometry_vlm_refined" 
    ? '<span style="color: #4CAF50;">●</span> Geometry + VLM'
    : '<span style="color: #FFA500;">●</span> Geometry only';
  
  metaDiv.innerHTML = `
    <div style="font-weight: bold; margin-bottom: 6px;">Model Status</div>
    <div style="margin-bottom: 4px;">${sourceBadge}</div>
    <div style="margin-bottom: 4px;">Clutter: <strong>${clutterType}</strong></div>
    <div style="margin-bottom: 4px;">Street View: ${svAvailable ? "✓ Available" : "✗ Not available"}</div>
    <div>VLM Refinement: ${vlmUsed ? "✓ Used" : "✗ Not used"}</div>
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
