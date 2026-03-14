// Shared mesh profiling core used by both /mesh-profiler and the main planner UI.
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
    lat,
    lon,
    txHeightM = 0.0,
    rxHeightM = 1.5,
    maxRangeM = 2000.0,
    drM = 5.0,
    dthetaDeg = 5.0,
    mode = "slice",
    onProgress = null,
  } = opts || {};

  if (!containerId) throw new Error("containerId required");
  const host = document.getElementById(containerId);
  if (!host) throw new Error(`missing container #${containerId}`);

  const cfg = await fetchConfig();
  if (!cfg.google_maps_api_key_present) {
    throw new Error("Missing GOOGLE_MAPS_API_KEY in server environment");
  }

  // Cesium uses this for worker/asset URLs
  if (!window.CESIUM_BASE_URL) window.CESIUM_BASE_URL = "/Cesium/";

  // Required for photorealistic tiles
  Cesium.GoogleMaps.defaultApiKey = cfg.google_maps_api_key;

  const progress = (msg) => { if (onProgress) onProgress(msg); };

  progress("3D: initializing Cesium + Google mesh…");

  const viewer = new Cesium.Viewer(containerId, {
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

  // Ensure the canvas renders even if host is offscreen
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

  // Fly near TX to ensure tiles stream in.
  viewer.camera.flyTo({
    destination: Cesium.Cartesian3.fromDegrees(lon, lat, 2000.0),
    duration: 0.0,
  });
  viewer.scene.requestRender();

  // Sample mesh height at TX by clamping a high probe.
  const probeH = 2000.0;
  const txProbe = Cesium.Cartesian3.fromDegrees(lon, lat, probeH);
  const txHeights = await clampHeights(viewer, [txProbe]);
  const txMeshH = (txHeights[0] == null) ? 0.0 : txHeights[0];

  const planeH = txMeshH + txHeightM;
  const clampProbeH = planeH + 250.0;

  const bearings = [];
  for (let b = 0; b < 360.0 - 1e-6; b += dthetaDeg) bearings.push(b);

  progress(`3D: building profiles (bearings=${bearings.length}, maxRange=${maxRangeM}m, dr=${drM}m)…`);

  const profiles = [];
  const nSteps = Math.floor(maxRangeM / drM);

  for (let bi = 0; bi < bearings.length; bi++) {
    const bearing = bearings[bi];

    const distances = [];
    const pts = [];

    for (let i = 1; i <= nSteps; i++) {
      const r = i * drM;
      distances.push(r);
      const p = destinationLatLon(lat, lon, bearing, r);
      pts.push(Cesium.Cartesian3.fromDegrees(p.lon, p.lat, clampProbeH));
    }

    const heights = await clampHeights(viewer, pts);
    const blocked = heights.map((h) => (h == null ? false : (mode === "slice" ? h >= planeH : false)));

    const segs = compressBlocked(distances, blocked);
    profiles.push({ bearing_deg: bearing, segments: segs });

    if ((bi + 1) % 5 === 0) {
      progress(`3D: profiling… ${bi + 1}/${bearings.length} bearings`);
    }
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

  const outText = await resp.text();
  if (!resp.ok) {
    viewer.destroy();
    throw new Error(`Profile upload failed (${resp.status}): ${outText}`);
  }

  const out = JSON.parse(outText);
  progress(`3D: profile ready (key=${out.key})`);

  viewer.destroy();
  return out;
}
