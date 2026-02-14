// 3D RF Planner UI (Cesium + Google Photorealistic 3D Tiles)
//
// Goals:
// - Mirror the 2D UI controls (ray mode + heights + sector configs + RF params).
// - Heatmap rendering supports scatter (2D-style) and raster (legacy disc).
// - In 3D mode, if mesh profiles are missing, auto-generate+upload them in-browser.

import * as Cesium from "/Cesium/index.js";
import { buildAndUploadProfiles } from "/mesh_profiler_core.js";

let viewer = null;
let txEntity = null;
let points = null; // (legacy) Cesium.PointPrimitiveCollection
let heatmapEntity = null; // Cesium entity (circular ellipse w/ texture)
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



function renderGridPointHeatmap(grid) {
  // Render the SAME scattered cells as the 2D Leaflet UI, but in Cesium.
  // This preserves ray termination + road corridors because missing cells remain missing.
  clearOverlay();
  if (!grid || !grid.cell_lat || !grid.cell_lon || !grid.rsrp_dbm) return;

  const n = Math.min(grid.cell_lat.length, grid.cell_lon.length, grid.rsrp_dbm.length);
  if (n <= 0) return;

  // Compute finite min/max
  let vmin = Infinity;
  let vmax = -Infinity;
  for (let i = 0; i < n; i++) {
    const v = grid.rsrp_dbm[i];
    if (!Number.isFinite(v)) continue;
    if (v < vmin) vmin = v;
    if (v > vmax) vmax = v;
  }
  if (!Number.isFinite(vmin) || !Number.isFinite(vmax)) return;

  const px = Math.max(1, Math.round(getNumber("heatmap-point-size-px", 6)));
  const hM = getNumber("heatmap-point-height-m", 2.0);

  // Use PointPrimitiveCollection for performance (~7k points typical)
  points = viewer.scene.primitives.add(new Cesium.PointPrimitiveCollection());

  for (let i = 0; i < n; i++) {
    const lat = grid.cell_lat[i];
    const lon = grid.cell_lon[i];
    const v = grid.rsrp_dbm[i];
    if (!Number.isFinite(lat) || !Number.isFinite(lon) || !Number.isFinite(v)) continue;

    points.add({
      position: Cesium.Cartesian3.fromDegrees(lon, lat, hM),
      color: colorForValue(v, vmin, vmax),
      pixelSize: px,
      // Ensure overlay remains visible on top of 3D tiles.
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
    });
  }
}

function renderHeatmap(heatmap, txLat, txLon, radiusM) {
  clearOverlay();
  if (!heatmap || !heatmap.lats || !heatmap.lons || !heatmap.rsrp) return;

  // Compute value range (finite only)
  let vmin = Infinity;
  let vmax = -Infinity;
  for (let i = 0; i < heatmap.rsrp.length; i++) {
    const row = heatmap.rsrp[i];
    for (let j = 0; j < row.length; j++) {
      const v = row[j];
      if (!Number.isFinite(v)) continue;
      if (v < vmin) vmin = v;
      if (v > vmax) vmax = v;
    }
  }
  if (!Number.isFinite(vmin) || !Number.isFinite(vmax)) return;

  const rows = heatmap.rsrp.length;
  const cols = heatmap.rsrp[0] ? heatmap.rsrp[0].length : 0;
  if (rows <= 0 || cols <= 0) return;

  // Build an RGBA canvas for the ellipse material.
  // NaN cells are fully transparent; everything else is alpha-blended.
  const canvas = document.createElement("canvas");
  canvas.width = cols;
  canvas.height = rows;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  const img = ctx.createImageData(cols, rows);

  // Use the lat/lon bounding box to compute a true-radius mask in meters.
  // This guarantees the rendered heatmap is a circle (not a square) even when
  // the backend grid is a bounding-box.
  const minLat = heatmap.lats[0][0];
  const maxLat = heatmap.lats[rows - 1][0];
  const minLon = heatmap.lons[0][0];
  const maxLon = heatmap.lons[0][cols - 1];

  const metersPerDegLat = 111320.0;
  const metersPerDegLon = 111320.0 * Math.cos((txLat * Math.PI) / 180.0);
  const mPerPxX = ((maxLon - minLon) * metersPerDegLon) / Math.max(1, (cols - 1));
  const mPerPxY = ((maxLat - minLat) * metersPerDegLat) / Math.max(1, (rows - 1));
  const cx = ((txLon - minLon) / Math.max(1e-12, (maxLon - minLon))) * (cols - 1);
  const cy = ((txLat - minLat) / Math.max(1e-12, (maxLat - minLat))) * (rows - 1);
  const r2 = Math.max(1.0, radiusM) * Math.max(1.0, radiusM);

  for (let y = 0; y < rows; y++) {
    for (let x = 0; x < cols; x++) {
      const v = heatmap.rsrp[y][x];
      const idx = (y * cols + x) * 4;
      if (!Number.isFinite(v)) {
        img.data[idx + 3] = 0;
        continue;
      }

      // Circular radius mask (meters).
      const dxm = (x - cx) * mPerPxX;
      const dym = (y - cy) * mPerPxY;
      if ((dxm * dxm + dym * dym) > r2) {
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

  // Render as a true circular ellipse on the ground, textured with our heatmap.
  heatmapEntity = viewer.entities.add({
    position: Cesium.Cartesian3.fromDegrees(txLon, txLat),
    ellipse: {
      semiMajorAxis: radiusM,
      semiMinorAxis: radiusM,
      material: new Cesium.ImageMaterialProperty({ image: canvas, transparent: true }),
      heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
      outline: false,
    },
  });
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

  // Update TX marker to snapped point when provided (2D snaps; 3D mesh mode may not).
  if (out.snapped_tx && Number.isFinite(out.snapped_tx.lat) && Number.isFinite(out.snapped_tx.lon)) {
    updateTxMarker(out.snapped_tx.lat, out.snapped_tx.lon);
  } else {
    updateTxMarker(lat, lon);
  }


  // Heatmap rendering
  // Default: scatter (same as 2D planner) so the coverage outline deforms based on ray termination.
  const renderMode = getString("heatmap-render-mode", "scatter").toLowerCase();

  if (renderMode === "raster") {
    // Legacy: rasterize to a textured disc (fills gaps; can look overly circular)
    if (out.heatmap) {
      const tx = out.snapped_tx || out.original_point || currentTxLocation;
      const simRadiusM = (out.grid && out.grid.rf_params && out.grid.rf_params.max_range_m)
        ? Number(out.grid.rf_params.max_range_m)
        : getNumber("max-range", 500.0);
      if (tx && Number.isFinite(tx.lat) && Number.isFinite(tx.lon)) {
        renderHeatmap(out.heatmap, tx.lat, tx.lon, simRadiusM);
      }
    }
  } else {
    // Recommended: render scattered cells from the backend attenuation grid.
    // This preserves non-uniform reach (streets vs buildings) because we do not fill missing cells.
    if (out.grid) {
      renderGridPointHeatmap(out.grid);
    } else if (out.heatmap) {
      // Fallback: raster if grid is missing.
      const tx = out.snapped_tx || out.original_point || currentTxLocation;
      const simRadiusM = getNumber("max-range", 500.0);
      if (tx && Number.isFinite(tx.lat) && Number.isFinite(tx.lon)) {
        renderHeatmap(out.heatmap, tx.lat, tx.lon, simRadiusM);
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
