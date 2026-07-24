/** Shared terrain + coverage layer params for 2D and 3D planners. */
(function (global) {
  "use strict";

  function getTerrainPlanParams() {
    const enabledEl = document.getElementById("terrain-enabled");
    return {
      terrain_enabled: enabledEl ? !!enabledEl.checked : true,
      dem_source: (document.getElementById("dem-source") || {}).value || "opentopodata",
      terrain_resolution_m: parseFloat((document.getElementById("terrain-resolution-m") || {}).value || "30") || 30,
      earth_curvature_k: parseFloat((document.getElementById("earth-curvature-k") || {}).value || "1.333") || 1.333,
      fresnel_min_clearance: parseFloat((document.getElementById("fresnel-min-clearance") || {}).value || "0.6") || 0.6,
      terrain_clutter_height_m: parseFloat((document.getElementById("terrain-clutter-height-m") || {}).value || "0") || 0,
      terrain_loss_cap_db: parseFloat((document.getElementById("terrain-loss-cap-db") || {}).value || "40") || 40,
      buildings_on_terrain: (document.getElementById("buildings-on-terrain") || {}).checked !== false,
      landcover_clutter_enabled: (document.getElementById("landcover-clutter-enabled") || {}).checked !== false,
      coverage_display_layer: getCoverageDisplayLayer(),
    };
  }

  function getCoverageDisplayLayer() {
    const el = document.getElementById("coverage-display-layer");
    return el ? String(el.value || "rsrp") : "rsrp";
  }

  function pickSampleMetric(grid, index, layer) {
    if (!grid) return NaN;
    layer = layer || getCoverageDisplayLayer();
    if (layer === "sinr" && Array.isArray(grid.sinr_db)) return grid.sinr_db[index];
    if (layer === "field_strength" && Array.isArray(grid.field_strength_dbuv_m)) return grid.field_strength_dbuv_m[index];
    if (layer === "terrain_shadow") {
      if (Array.isArray(grid.terrain_loss_db)) return grid.terrain_loss_db[index];
      if (Array.isArray(grid.los_terrain)) return grid.los_terrain[index] ? 0 : 30;
    }
    if (Array.isArray(grid.rsrp_dbm)) return grid.rsrp_dbm[index];
    return NaN;
  }

  function coverageLayerLabel(layer) {
    if (layer === "sinr") return "SINR (dB)";
    if (layer === "terrain_shadow") return "Terrain loss (dB)";
    if (layer === "field_strength") return "Field strength (dBµV/m)";
    return "RSRP (dBm)";
  }

  global.RFTerrainParams = {
    getTerrainPlanParams,
    getCoverageDisplayLayer,
    pickSampleMetric,
    coverageLayerLabel,
  };
})(typeof window !== "undefined" ? window : globalThis);
