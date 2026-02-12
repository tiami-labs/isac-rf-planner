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
const statusEl = document.getElementById("status");

// State for polygon drawing
let currentTxLocation = null; // {lat, lon} - set when TX is selected/typed
let polygonDrawingMode = null; // {sectorId, points: [[lat, lon], ...], polygonLayer: L.polygon}
let polygonMarkers = []; // Markers for polygon points

function setStatus(msg) {
  statusEl.textContent = msg;
}

// Load and restore last results from localStorage on page load
function loadLastResults() {
  try {
    const savedData = localStorage.getItem('rf_planning_last_result');
    const savedTimestamp = localStorage.getItem('rf_planning_timestamp');
    
    if (savedData && savedTimestamp) {
      const data = JSON.parse(savedData);
      const ageMinutes = (Date.now() - parseInt(savedTimestamp)) / (1000 * 60);
      
      console.log(`[RF Planner] Found saved results (${ageMinutes.toFixed(1)} minutes old)`);
      
      // Restore the heatmap
      renderHeatmap(data);
      
      // Restore metadata if available
      if (data.clutter_type || data.world_model_source) {
        displayMetadata(data);
      }
      
      // Restore panorama if available
      if (data.panorama_image) {
        displayPanorama(data.panorama_image, data.panorama_location);
      }
      
      // Show clicked point marker
      if (data.snapped_tx) {
        const tx = data.snapped_tx;
        L.marker([tx.lat, tx.lon], { 
          icon: L.divIcon({ className: "click-marker", html: "📍", iconSize: [20, 20] }) 
        })
          .addTo(currentLayerGroup)
          .bindPopup(`TX: ${tx.lat.toFixed(5)}, ${tx.lon.toFixed(5)}`);
      }
      
      setStatus(`Restored previous RF plan (${ageMinutes.toFixed(1)} min ago). Click map to compute new plan.`);
      return true;
    }
  } catch (e) {
    console.warn("[RF Planner] Failed to load from localStorage:", e);
  }
  return false;
}

// Try to restore last results on page load
window.addEventListener('DOMContentLoaded', () => {
  // Initialize Ray Mode selector (defaults to "2d" from server config).
  fetch("/api/config").then(r => r.ok ? r.json() : null).then(cfg => {
    if (!cfg) return;
    const mode = String(cfg.default_ray_mode || "2d").toLowerCase();
    const sel = document.getElementById("ray-mode");
    if (sel && (mode === "2d" || mode === "3d")) sel.value = mode;
  }).catch(e => console.warn("[RF Planner] Failed to load /api/config for default ray mode:", e));

  if (!loadLastResults()) {
    setStatus("Click on the map to run RF planning.");
  }
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
  console.log("=".repeat(60));
  console.log(`[RF Planner] RF PLANNING REQUEST (source: ${source})`);
  console.log(`  Coordinates: ${lat}, ${lng}`);
  console.log("=".repeat(60));
  
  // Set TX location (origin for polygon sectors)
  currentTxLocation = { lat, lon: lng };
  console.log(`[RF Planner] TX location set: ${lat}, ${lng} (origin for polygon sectors)`);
  
  setStatus(`Selected: ${lat.toFixed(5)}, ${lng.toFixed(5)}. Processing... (this may take 20-30 minutes for full analysis with LOS detection)`);
  
  // Create a NEW layer group for this RF plan (preserves previous heatmaps)
  // Only clear markers/overlays from currentLayerGroup, not heatmaps
  // Remove old TX marker and snapped markers, but keep previous heatmaps
  if (window.txMarker) {
    currentLayerGroup.removeLayer(window.txMarker);
  }
  // Remove any snapped markers and lines (they're in currentLayerGroup)
  // We'll identify them by checking if they have popups with "Snapped" text
  currentLayerGroup.eachLayer((layer) => {
    if (layer instanceof L.Marker || layer instanceof L.Polyline) {
      // Remove markers and polylines (snapped markers, lines) but keep heatmap circles
      if (!(layer instanceof L.Circle)) {
        currentLayerGroup.removeLayer(layer);
      }
    }
  });
  
  // Create new layer group for this RF plan's heatmap
  const newHeatmapLayerGroup = L.layerGroup().addTo(map);
  heatmapLayerGroups.push(newHeatmapLayerGroup);
  
  // Store reference to this heatmap layer group in the result data
  window.currentHeatmapLayerGroup = newHeatmapLayerGroup;

  // Navigate map to coordinates
  map.setView([lat, lng], 15);

  // Show selected point (add to currentLayerGroup for markers/overlays)
  window.txMarker = L.marker([lat, lng], { icon: L.divIcon({ className: "click-marker", html: "📍", iconSize: [20, 20] }) })
    .addTo(currentLayerGroup)
    .bindPopup("TX Location");

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
    const statusEl = document.getElementById('mesh-profile-status');

    // RF parameters
    const rfParams = {
      lat: lat,
      lon: lng,
      freq_mhz: 3500.0,
      tx_power_dbm: 43.0,
      subcarrier_spacing_khz: 15.0,
      num_resource_blocks: 100,
      channel_bandwidth_mhz: 20.0,
      num_tx_antennas: 1,
      num_rx_antennas: 1,
      mimo_mode: "SISO",
      enable_link_adaptation: true,
      fixed_modulation: null,
      sectors: sectors.length > 0 ? sectors : null,  // null = omnidirectional
      // Ray propagation: only geometry differs between 2D and 3D
      ray_mode: rayMode,
      tx_height_m: txHeightM,
      rx_height_m: rxHeightM,
    };
    
    // If 3D mode is selected, ensure a persisted mesh profile exists for this TX/config.
    // This avoids a slow fallback and makes behavior explicit.
    if (rayMode.toLowerCase() === '3d') {
      const maxRangeM = 500.0;
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
        return;
      }
    }

    console.log(`[RF Planner] Sending POST /api/plan...`);
    console.log(`[RF Planner] Request body:`, JSON.stringify(rfParams, null, 2));
    
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 1800000); // 30 minute timeout (backend can take 20-30 min for 7200 cells with Phase 1 LOS detection)
    
    const resp = await fetch("/api/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(rfParams),
      signal: controller.signal,
    });
    
    clearTimeout(timeoutId);
    
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
      return;
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
    
    // Save results to localStorage for persistence across page refreshes
    try {
      localStorage.setItem('rf_planning_last_result', JSON.stringify(data));
      localStorage.setItem('rf_planning_timestamp', Date.now().toString());
      console.log("[RF Planner] Results saved to localStorage");
    } catch (e) {
      console.warn("[RF Planner] Failed to save to localStorage:", e);
    }
    
    setStatus("RF plan computed. Click another point or enter coordinates to re-run. (Results saved - will persist after refresh)");
  } catch (err) {
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
  }
}

// Clear map function - removes all drawings and visualization (but preserves OSM cache)
async function clearMap() {
  console.log("[RF Planner] clearMap() called");
  
  // Remove and recreate the layer groups to ensure everything is cleared
  map.removeLayer(currentLayerGroup);
  map.removeLayer(sectorLayerGroup);
  
  // Remove all heatmap layer groups
  heatmapLayerGroups.forEach(layerGroup => {
    map.removeLayer(layerGroup);
  });
  heatmapLayerGroups = [];
  
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
    
    // Show TX marker
    L.marker([lat, lon], { 
      icon: L.divIcon({ className: "click-marker", html: "📍", iconSize: [20, 20] }) 
    })
      .addTo(currentLayerGroup)
      .bindPopup("TX Location");
    
    // Update status
    const sectorDiv = document.getElementById(`sector-${polygonDrawingMode.sectorId}`);
    if (sectorDiv) {
      const statusEl = sectorDiv.querySelector(".polygon-status");
      if (statusEl) {
        statusEl.textContent = "TX point set. Click map to add polygon vertices. Need 2+ more points (3+ total).";
      }
    }
    
    setStatus(`TX point set at ${lat.toFixed(5)}, ${lon.toFixed(5)}. Click map to add polygon vertices. Need 2+ more points (3+ total).`);
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
  setStatus(`Polygon complete: ${finalTotal} points total. Click "Plan RF" button to run planning.`);
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
    
    const latInput = document.getElementById("lat-input");
    const lonInput = document.getElementById("lon-input");
    
    let lat, lon;
    
    // Try to get coordinates from form inputs first
    const latValue = parseFloat(latInput.value);
    const lonValue = parseFloat(lonInput.value);
    
    if (!isNaN(latValue) && !isNaN(lonValue)) {
      // Form inputs are filled - use them
      lat = latValue;
      lon = lonValue;
      
      // Validate coordinates
      if (lat < -90 || lat > 90) {
        setStatus("Error: Latitude must be between -90 and 90.");
        return;
      }
      
      if (lon < -180 || lon > 180) {
        setStatus("Error: Longitude must be between -180 and 180.");
        return;
      }
      
      // Update TX location
      currentTxLocation = { lat, lon };
    } else if (currentTxLocation) {
      // Form inputs are empty, but TX location is set from map click - use that
      lat = currentTxLocation.lat;
      lon = currentTxLocation.lon;
      console.log(`[RF Planner] Using TX location from map: ${lat}, ${lon}`);
    } else {
      // Neither form inputs nor map TX location available
      setStatus("Error: Please enter coordinates in the form OR click on the map to set TX location.");
      return;
    }
    
    // Run RF planning
    await runRFPlanning(lat, lon, "form");
  });
  
  // Handle clear map button
  const clearBtn = document.getElementById("clear-map-btn");
  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      clearMap();
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
  setStatus(`TX location set: ${lat.toFixed(5)}, ${lng.toFixed(5)}. Add sector and click "Plan RF" to run planning.`);
  console.log(`[RF Planner] TX location set: ${lat}, ${lng} (preserving existing RF heatmap)`);
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
const FIXED_RSRP_MIN = -150.0;  // dBm - effectively no signal (metal-blocked, noise floor)
const FIXED_RSRP_MAX = 50.0;    // dBm - very strong signal (very close to TX, no obstacles)

function renderHeatmap(result) {
  const grid = result.grid;
  const lats = grid.cell_lat;
  const lons = grid.cell_lon;
  const rsrp = grid.rsrp_dbm;

  if (!lats || lats.length === 0) {
    setStatus("No grid cells returned.");
    return;
  }

  // Use fixed scale for consistent comparison across locations
  // Find actual min/max in data - use these for color mapping to show full gradient
  let actualMin = Infinity;
  let actualMax = -Infinity;
  for (const v of rsrp) {
    if (v < actualMin) actualMin = v;
    if (v > actualMax) actualMax = v;
  }
  
  // Use actual data range for color mapping (not fixed scale)
  // This makes FSPL attenuation visible: red near TX (strong) to blue far away (weak)
  const min = actualMin;
  const max = actualMax;
  
  // Update RSRP legend with actual range (for color mapping) and fixed scale (for reference)
  updateRSRPLegend(FIXED_RSRP_MIN, FIXED_RSRP_MAX, actualMin, actualMax);

  // Use the dedicated heatmap layer group for this RF plan (preserves previous heatmaps)
  const heatmapLayerGroup = window.currentHeatmapLayerGroup || currentLayerGroup;
  
  for (let i = 0; i < lats.length; i++) {
    const v = rsrp[i];
    // Use actual data range for color mapping - no clamping needed since v is within [actualMin, actualMax]
    const color = rsrpToColor(v, min, max);
    const lat = lats[i];
    const lon = lons[i];

    L.circle([lat, lon], {
      radius: 10,       // meters
      color: color,
      fillColor: color,
      fillOpacity: 0.6,
      weight: 0,
    }).addTo(heatmapLayerGroup);
  }

  // Center map on snapped TX point
  if (result.snapped_tx) {
    map.setView([result.snapped_tx.lat, result.snapped_tx.lon], map.getZoom());
  }
  
  // Draw sector visualization (cones for partial sectors, circles for 360°)
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
  
  // Default to 500m if no grid data
  if (maxRange === 0) {
    maxRange = 500;
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
    const opacity = 0.15;
    
    if (sectorType === "polygon") {
      // Draw polygon sector
      // Minimum 2 points required (TX origin is the third point)
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
          `Type: Polygon (${sector.polygon_points.length} points + TX origin)<br>` +
          `Frequency: ${sector.freq_mhz} MHz<br>` +
          `TX Power: ${sector.tx_power_dbm} dBm`
        );
      }
    } else if (sectorType === "360") {
      // Draw circle for omnidirectional (360°)
      L.circle([txLat, txLon], {
        radius: maxRange,
        color: color,
        fillColor: color,
        fillOpacity: opacity,
        weight: 2,
        dashArray: '5, 5',
      }).addTo(sectorLayerGroup).bindPopup(
        `Sector: ${sector.sector_id}<br>` +
        `Coverage: 360° (Omnidirectional)<br>` +
        `Frequency: ${sector.freq_mhz} MHz<br>` +
        `TX Power: ${sector.tx_power_dbm} dBm`
      );
    } else {
      // Draw cone for angle-based sector
      const startAngle = sector.start_angle_deg;
      const endAngle = sector.end_angle_deg;
      const points = [];
      points.push([txLat, txLon]); // Center point
      
      // Handle wrap-around case (e.g., 350° to 10°)
      if (endAngle > startAngle) {
        // Normal case: no wrap-around (e.g., 0° to 120°)
        const sectorSpan = endAngle - startAngle;
        const numPoints = Math.max(20, Math.ceil(sectorSpan / 5)); // At least 20 points, or one per 5 degrees
        
        for (let i = 0; i <= numPoints; i++) {
          const currentAngle = startAngle + (i / numPoints) * sectorSpan;
          const point = calculateDestinationPoint(txLat, txLon, currentAngle, maxRange);
          points.push([point.lat, point.lon]);
        }
      } else {
        // Wrap-around case: e.g., 350° to 10° (covers 350-360 and 0-10)
        // First arc: from startAngle to 360°
        const firstSpan = 360 - startAngle;
        const firstNumPoints = Math.max(10, Math.ceil(firstSpan / 5));
        for (let i = 0; i <= firstNumPoints; i++) {
          const currentAngle = startAngle + (i / firstNumPoints) * firstSpan;
          const point = calculateDestinationPoint(txLat, txLon, currentAngle, maxRange);
          points.push([point.lat, point.lon]);
        }
        
        // Second arc: from 0° to endAngle
        const secondSpan = endAngle;
        const secondNumPoints = Math.max(10, Math.ceil(secondSpan / 5));
        for (let i = 0; i <= secondNumPoints; i++) {
          const currentAngle = (i / secondNumPoints) * secondSpan;
          const point = calculateDestinationPoint(txLat, txLon, currentAngle, maxRange);
          points.push([point.lat, point.lon]);
        }
      }
      
      // Close the polygon
      points.push([txLat, txLon]);
      
      // Calculate sector span for popup
      const sectorSpan = endAngle > startAngle 
        ? (endAngle - startAngle) 
        : (360 - startAngle + endAngle);
      
      L.polygon(points, {
        color: color,
        fillColor: color,
        fillOpacity: opacity,
        weight: 2,
        dashArray: '5, 5',
      }).addTo(sectorLayerGroup).bindPopup(
        `Sector: ${sector.sector_id}<br>` +
        `Coverage: ${startAngle.toFixed(1)}° to ${endAngle.toFixed(1)}° (${sectorSpan.toFixed(1)}° span)<br>` +
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
    locationInfo = `<div style="padding: 3px 5px; font-size: 10px; background: #333;">Location: ${panoLocation.lat.toFixed(6)}, ${panoLocation.lon.toFixed(6)}</div>`;
  }
  
  panoDiv.innerHTML = `
    <div style="padding: 5px; font-size: 11px; background: #222;">Street View Panorama</div>
    ${locationInfo}
    <img src="${imgData}" style="width: 100%; display: block;" alt="360° Panorama" />
  `;
}

