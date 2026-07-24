// Client-side Google Photorealistic mesh sampling; imported by app.js (/, /2d) and planner_3d.js (/3d).
//
// Exports: buildAndUploadProfiles({containerId, lat, lon, txHeightM, rxHeightM, maxRangeM, drM, dthetaDeg, mode, onProgress})
//
// This uses Cesium + Google Photorealistic mesh (client-side) to generate a RayProfileSet,
// then uploads it to /api/mesh-profiles/put?enrich_osm=true.
//
// Notes:
// - First-time generation is slow; subsequent runs are fast due to SQLite cache on disk.
// - "slice" mode: a segment is blocked where mesh height >= (tx_mesh_height + txHeightM).
//   This matches the current 3D MapProvider consumption in the planner.

import * as Cesium from "/Cesium/index.js";

function deg2rad(d) { return d * Math.PI / 180.0; }
function rad2deg(r) { return r * 180.0 / Math.PI; }

// Great-circle destination (accurate enough for <= few km)
function destinationLatLon(latDeg, lonDeg, bearingDeg, distanceM) {
  const R = 6371000.0;
  const brng = deg2rad(bearingDeg);
  const lat1 = deg2rad(latDeg);
  const lon1 = deg2rad(lonDeg);
  const dr = distanceM / R;

  const lat2 = Math.asin(Math.sin(lat1) * Math.cos(dr) + Math.cos(lat1) * Math.sin(dr) * Math.cos(brng));
  const lon2 = lon1 + Math.atan2(
    Math.sin(brng) * Math.sin(dr) * Math.cos(lat1),
    Math.cos(dr) - Math.sin(lat1) * Math.sin(lat2)
  );
  return { lat: rad2deg(lat2), lon: rad2deg(lon2) };
}

async function fetchConfig() {
  const r = await fetch("/api/config");
  if (!r.ok) throw new Error(`GET /api/config failed (${r.status})`);
  return await r.json();
}

function compressBlocked(distances, blockedFlags) {
  const segs = [];
  let inSeg = false;
  let r0 = 0;
  for (let i = 0; i < blockedFlags.length; i++) {
    const b = blockedFlags[i];
    const r = distances[i];
    if (b && !inSeg) {
      inSeg = true;
      r0 = r;
    } else if (!b && inSeg) {
      inSeg = false;
      const r1 = distances[i - 1];
      if (r1 > r0) segs.push({ r0_m: r0, r1_m: r1 });
    }
  }
  if (inSeg) {
    const r1 = distances[distances.length - 1];
    if (r1 > r0) segs.push({ r0_m: r0, r1_m: r1 });
  }
  return segs;
}

async function clampHeights(viewer, points) {
  // points: array of Cartesian3
  const clamped = await viewer.scene.clampToHeightMostDetailed(points);
  // clamped[i] may be undefined if no intersection
  return clamped.map((c) => {
    if (!c) return null;
    const carto = Cesium.Cartographic.fromCartesian(c);
    return carto.height;
  });
}

export async function buildAndUploadProfiles(opts) {
  const {
    containerId,
    existingViewer = null,
    lat,
    lon,
    txHeightM = 0.0,
    rxHeightM = 1.5,
    maxRangeM = 2500.0,
    drM = 5.0,
    dthetaDeg = 5.0,
    mode = "slice",
    onProgress = null,
  } = opts || {};

  const progress = (msg) => { if (onProgress) onProgress(msg); };

  // If the caller passes an already-initialised Cesium viewer (e.g. the live /3d viewer),
  // use it directly — tiles are already loaded so sampling is near-instant.
  let viewer;
  let ownedViewer = false;

  let prevRequestRenderMode = null;
  if (existingViewer) {
    viewer = existingViewer;
    // Keep continuous rendering on for the entire sample+upload phase so tiles stream.
    prevRequestRenderMode = viewer.scene.requestRenderMode;
    viewer.scene.requestRenderMode = false;
    progress("3D: navigating to TX for tile streaming…");
    viewer.camera.flyTo({
      destination: Cesium.Cartesian3.fromDegrees(lon, lat, 150.0),
      duration: 0.0,
    });
    // Force a render so the scene starts requesting tiles for the new camera position.
    viewer.scene.requestRender();
    // Wait for 3D tileset pending requests to drain (continuous render active).
    await new Promise((resolve) => {
      let waited = 0;
      const maxWait = 15000;
      const interval = 250;
      const check = () => {
        waited += interval;
        if (waited >= maxWait) { resolve(); return; }
        let pending = 0;
        for (let i = 0; i < viewer.scene.primitives.length; i++) {
          const p = viewer.scene.primitives.get(i);
          if (p && p.statistics) {
            pending += (p.statistics.numberOfPendingRequests || 0) + (p.statistics.numberOfTilesProcessing || 0);
          }
        }
        // Need pending>0 seen at least once then draining, or max wait.
        if (pending === 0 && waited >= 3000) { resolve(); return; }
        setTimeout(check, interval);
      };
      setTimeout(check, interval);
    });
    progress("3D: tiles ready, sampling…");
    // DO NOT restore requestRenderMode here — keep continuous rendering throughout sampling.
  } else {
    if (!containerId) throw new Error("containerId or existingViewer required");
    const host = document.getElementById(containerId);
    if (!host) throw new Error(`missing container #${containerId}`);

    const cfg = await fetchConfig();
    if (!cfg.google_maps_api_key_present) {
      throw new Error("Missing GOOGLE_MAPS_API_KEY in server environment");
    }

    if (!window.CESIUM_BASE_URL) window.CESIUM_BASE_URL = "/Cesium/";
    Cesium.GoogleMaps.defaultApiKey = cfg.google_maps_api_key;

    progress("3D: initializing Cesium + Google mesh…");

    viewer = new Cesium.Viewer(containerId, {
      animation: false,
      timeline: false,
      geocoder: false,
      homeButton: false,
      sceneModePicker: false,
      navigationHelpButton: false,
      baseLayerPicker: false,
      infoBox: false,
      selectionIndicator: false,
      terrainProvider: new Cesium.EllipsoidTerrainProvider(),
      requestRenderMode: true,
      maximumRenderTimeChange: Infinity,
    });
    ownedViewer = true;

    viewer.scene.requestRender();

    let tileset = null;
    try {
      tileset = await Cesium.createGooglePhotorealistic3DTileset();
      viewer.scene.primitives.add(tileset);
      if (tileset.readyPromise) await tileset.readyPromise;
    } catch (e) {
      viewer.destroy();
      throw new Error(`Failed to load Google photorealistic tileset: ${e}`);
    }

    // Fly close to TX (150m) so high-detail tiles load before sampling.
    viewer.camera.flyTo({
      destination: Cesium.Cartesian3.fromDegrees(lon, lat, 150.0),
      duration: 0.0,
    });
    viewer.scene.requestRender();
  }

  const bearings = [];
  for (let b = 0; b < 360.0 - 1e-6; b += dthetaDeg) bearings.push(b);
  const nSteps = Math.floor(maxRangeM / drM);

  // Sample TX height first.
  const txProbe = Cesium.Cartesian3.fromDegrees(lon, lat, 500.0);
  const txHeightResult = await clampHeights(viewer, [txProbe]);
  const txMeshH = (txHeightResult[0] == null) ? 0.0 : txHeightResult[0];
  const planeH = txMeshH + txHeightM;
  progress(`3D: TX mesh height=${txMeshH.toFixed(1)}m — sampling ${bearings.length} bearings in chunks…`);

  // Process bearings in chunks of 8 so each clamp call covers a manageable tile area.
  const CHUNK = 8;
  const profiles = [];
  for (let ci = 0; ci < bearings.length; ci += CHUNK) {
    const chunk = bearings.slice(ci, ci + CHUNK);
    const chunkPts = [];
    const chunkOffsets = [];
    for (const bearing of chunk) {
      chunkOffsets.push(chunkPts.length);
      for (let i = 1; i <= nSteps; i++) {
        const r = i * drM;
        const p = destinationLatLon(lat, lon, bearing, r);
        chunkPts.push(Cesium.Cartesian3.fromDegrees(p.lon, p.lat, 500.0));
      }
    }
    const chunkHeights = await clampHeights(viewer, chunkPts);
    for (let j = 0; j < chunk.length; j++) {
      const offset = chunkOffsets[j];
      const distances = [];
      const heights = [];
      for (let i = 0; i < nSteps; i++) {
        distances.push((i + 1) * drM);
        heights.push(chunkHeights[offset + i]);
      }
      const blocked = heights.map((h) => (h == null ? false : (mode === "slice" ? h >= planeH : false)));
      const segs = compressBlocked(distances, blocked);
      const terrainHeights = distances.map((r, i) => [r, heights[i] ?? txMeshH]);
      profiles.push({ bearing_deg: chunk[j], segments: segs, terrain_heights: terrainHeights });
    }
    if (ci % 24 === 0) progress(`3D: ${Math.min(ci + CHUNK, bearings.length)}/${bearings.length} bearings done…`);
  }

  const payload = {
    tx_lat: lat,
    tx_lon: lon,
    tx_height_m: txHeightM,
    rx_height_m: rxHeightM,
    max_range_m: maxRangeM,
    dr_m: drM,
    dtheta_deg: dthetaDeg,
    profiles,
  };

  progress("3D: uploading profiles…");
  const resp = await fetch("/api/mesh-profiles/put?enrich_osm=true", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  let outText, out;
  try {
    outText = await resp.text();
    if (!resp.ok) throw new Error(`Profile upload failed (${resp.status}): ${outText}`);
    out = JSON.parse(outText);
  } finally {
    if (ownedViewer) viewer.destroy();
    else if (prevRequestRenderMode !== null) viewer.scene.requestRenderMode = prevRequestRenderMode;
  }

  progress(`3D: profile ready (key=${out.key})`);
  return out;
}
