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
    if (layer === "carrier_to_noise") {
      if (Array.isArray(grid.carrier_to_noise_db)) return grid.carrier_to_noise_db[index];
      if (Array.isArray(grid.sinr_db)) return grid.sinr_db[index];
    }
    if (layer === "field_strength" && Array.isArray(grid.field_strength_dbuv_m)) return grid.field_strength_dbuv_m[index];
    if (layer === "received_power" && Array.isArray(grid.received_power_dbm)) return grid.received_power_dbm[index];
    if (layer === "incident_power" && Array.isArray(grid.incident_power_isotropic_dbm)) return grid.incident_power_isotropic_dbm[index];
    if (layer === "bistatic_echo" && Array.isArray(grid.bistatic_echo_power_dbm)) return grid.bistatic_echo_power_dbm[index];
    if (layer === "bistatic_return_loss" && Array.isArray(grid.bistatic_return_path_loss_db)) return grid.bistatic_return_path_loss_db[index];
    if (layer === "bistatic_total_loss" && Array.isArray(grid.bistatic_total_path_loss_db)) return grid.bistatic_total_path_loss_db[index];
    if (layer === "isac_quality" && Array.isArray(grid.bistatic_isac_quality_code)) return grid.bistatic_isac_quality_code[index];
    if (layer === "bistatic_snr" && Array.isArray(grid.bistatic_postprocessing_snr_db)) return grid.bistatic_postprocessing_snr_db[index];
    if (layer === "bistatic_margin" && Array.isArray(grid.bistatic_detection_margin_db)) return grid.bistatic_detection_margin_db[index];
    if (layer === "bistatic_doppler" && Array.isArray(grid.bistatic_doppler_hz)) return grid.bistatic_doppler_hz[index];
    if (layer === "bistatic_range" && Array.isArray(grid.bistatic_path_range_m)) return grid.bistatic_path_range_m[index] / 1000;
    if (layer === "bistatic_delay" && Array.isArray(grid.bistatic_excess_delay_s)) return grid.bistatic_excess_delay_s[index] * 1e6;
    if (layer === "bistatic_angle" && Array.isArray(grid.bistatic_angle_deg)) return grid.bistatic_angle_deg[index];
    if (layer === "bistatic_detectable" && Array.isArray(grid.bistatic_detectable)) return grid.bistatic_detectable[index] ? 1 : 0;
    if (layer === "terrain_shadow") {
      if (Array.isArray(grid.terrain_loss_db)) return grid.terrain_loss_db[index];
      if (Array.isArray(grid.los_terrain)) return grid.los_terrain[index] ? 0 : 30;
    }
    if (Array.isArray(grid.rsrp_dbm)) return grid.rsrp_dbm[index];
    return NaN;
  }

  function coverageLayerLabel(layer) {
    if (layer === "sinr") return "SINR (dB)";
    if (layer === "carrier_to_noise") return "Carrier-to-noise C/N (dB)";
    if (layer === "terrain_shadow") return "Terrain loss (dB)";
    if (layer === "field_strength") return "Field strength (dBµV/m)";
    if (layer === "received_power") return "Received carrier power (dBm)";
    if (layer === "incident_power") return "Incident total-carrier power at target, 0 dBi (dBm)";
    if (layer === "bistatic_echo") return "Bistatic echo power (dBm)";
    if (layer === "bistatic_return_loss") return "Target→RX environmental path loss per target (dB)";
    if (layer === "bistatic_total_loss") return "Total TX→target→RX path loss per target (dB)";
    if (layer === "isac_quality") return "Expected ISAC quality at each target (0–5)";
    if (layer === "bistatic_snr") return "Post-processing echo SNR (dB)";
    if (layer === "bistatic_margin") return "Detection margin (dB)";
    if (layer === "bistatic_doppler") return "Signed bistatic Doppler (Hz)";
    if (layer === "bistatic_range") return "Total TX→target→RX path length per target (km)";
    if (layer === "bistatic_delay") return "Bistatic excess delay (µs)";
    if (layer === "bistatic_angle") return "Bistatic angle (deg)";
    if (layer === "bistatic_detectable") return "Detectability mask (0/1)";
    return "RSRP (dBm)";
  }

  global.RFTerrainParams = {
    getTerrainPlanParams,
    getCoverageDisplayLayer,
    pickSampleMetric,
    coverageLayerLabel,
  };
})(typeof window !== "undefined" ? window : globalThis);
