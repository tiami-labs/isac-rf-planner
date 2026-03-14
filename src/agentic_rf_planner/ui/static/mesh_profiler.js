// Mesh Profiler (optional debug page)
//
// This page should render the Google Photorealistic 3D mesh and let you click to set TX,
// then generate + upload persisted profiles.
//
// The main planner UI (/) auto-generates profiles when Ray Mode=3D and cache is missing,
// so you generally do NOT need to use this page.

import * as Cesium from "/Cesium/index.js";
import { buildAndUploadProfiles } from "/mesh_profiler_core.js";

let viewer = null;
let tx = null; // {lat, lon}

function setStatus(msg) {
  const el = document.getElementById("status");
  if (el) el.textContent = msg;
}

function getNumber(id, fallback) {
  const el = document.getElementById(id);
  if (!el) return fallback;
  const v = parseFloat(el.value);
  return Number.isFinite(v) ? v : fallback;
}

async function fetchConfig() {
  const r = await fetch("/api/config");
  if (!r.ok) throw new Error(`GET /api/config failed (${r.status})`);
  return await r.json();
}

async function initCesium() {
  if (!window.CESIUM_BASE_URL) window.CESIUM_BASE_URL = "/Cesium/";

  const cfg = await fetchConfig();
  if (!cfg.google_maps_api_key_present) {
    setStatus("Missing GOOGLE_MAPS_API_KEY in server env.\n\nSet it and restart uvicorn:\n  export GOOGLE_MAPS_API_KEY=...\n  uvicorn agentic_rf_planner.api.rest:app --reload --app-dir src");
    return;
  }

  Cesium.GoogleMaps.defaultApiKey = cfg.google_maps_api_key;

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
    setStatus(`Failed to load Google mesh tileset: ${e}\n\nChecks:\n1) GOOGLE_MAPS_API_KEY is valid and has 3D Tiles enabled.\n2) Browser console: no 404s for /Cesium/* assets.`);
    return;
  }

  viewer.screenSpaceEventHandler.setInputAction((click) => {
    const cartesian = viewer.scene.pickPosition(click.position);
    if (!cartesian) return;
    const carto = Cesium.Cartographic.fromCartesian(cartesian);
    const lat = Cesium.Math.toDegrees(carto.latitude);
    const lon = Cesium.Math.toDegrees(carto.longitude);
    tx = { lat, lon };
    const btn = document.getElementById("generate");
    if (btn) btn.disabled = false;
    setStatus(`TX set:\n  lat=${lat}\n  lon=${lon}\n\nClick Generate + Upload.`);
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

  viewer.camera.flyTo({
    destination: Cesium.Cartesian3.fromDegrees(-122.4194, 37.7749, 2000.0),
    duration: 0.0,
  });

  setStatus("Loaded Google mesh. Click to set TX.");
}

async function generate() {
  if (!tx) {
    setStatus("Set TX first by clicking on the mesh.");
    return;
  }

  const txHeightM = getNumber("tx-height", 10.0);
  const rxHeightM = getNumber("rx-height", 1.5);
  const maxRangeM = getNumber("max-range", 2000.0);
  const drM = getNumber("step-m", 5.0);
  const dthetaDeg = getNumber("dtheta", 5.0);
  const modeEl = document.getElementById("mode");
  const mode = modeEl ? String(modeEl.value || "slice") : "slice";

  const btn = document.getElementById("generate");
  if (btn) btn.disabled = true;

  try {
    // Free the picking viewer to avoid double WebGL contexts.
    if (viewer) { viewer.destroy(); viewer = null; }

    await buildAndUploadProfiles({
      containerId: "cesiumContainer",
      lat: tx.lat,
      lon: tx.lon,
      txHeightM,
      rxHeightM,
      maxRangeM,
      drM,
      dthetaDeg,
      mode,
      onProgress: setStatus,
    });

    setStatus("Upload complete.\n\nReturn to / (main UI) and click TX with Ray Mode=3D.");
  } catch (e) {
    setStatus(`Failed: ${e}`);
  } finally {
    // Don't auto-reinit; keep the page quiet after generation.
    if (btn) btn.disabled = false;
  }
}

window.addEventListener("DOMContentLoaded", async () => {
  const btn = document.getElementById("generate");
  if (btn) btn.addEventListener("click", generate);
  await initCesium();
});
