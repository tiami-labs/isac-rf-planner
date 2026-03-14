/**
 * Shared RF Planner export utilities.
 * Used by both 2D and 3D planners for ZIP export and metadata.
 */
(function (global) {
  "use strict";

  function base64DataUrlToBlob(dataUrl) {
    if (!dataUrl || typeof dataUrl !== "string") return null;
    const m = dataUrl.match(/^data:([^;]+);base64,(.+)$/);
    if (!m) return null;
    const b64 = m[2];
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new Blob([bytes], { type: m[1] || "image/png" });
  }

  function buildExportMetadata(planResults, options) {
    const opts = options || {};
    const plannerVersion = opts.plannerVersion || "3d";
    const viewState = opts.viewState || {};
    const roadNames = opts.roadNames || [];

    const plans = (planResults || []).map((pr, idx) => {
      const out = pr.out || pr.data || {};
      const grid = out.grid || {};
      const rfParams = grid.rf_params || {};
      const heatmap = out.heatmap || {};
      const snapped = out.snapped_tx || { lat: pr.lat, lon: pr.lon };

      let rsrpMin = null;
      let rsrpMax = null;
      if (Array.isArray(grid.rsrp_dbm) && grid.rsrp_dbm.length) {
        rsrpMin = Math.min(...grid.rsrp_dbm.filter(Number.isFinite));
        rsrpMax = Math.max(...grid.rsrp_dbm.filter(Number.isFinite));
      }

      const heatmapScale = {
        vmin: heatmap.vmin ?? -140,
        vmax: heatmap.vmax ?? -60,
        actual_min: heatmap.actual_min ?? rsrpMin,
        actual_max: heatmap.actual_max ?? rsrpMax,
      };

      const advancedRf = {
        freq_mhz: rfParams.freq_mhz ?? 3500,
        tx_power_dbm: rfParams.tx_power_dbm ?? 43,
        noise_figure_db: rfParams.noise_figure_db ?? 7.0,
        subcarrier_spacing_khz: rfParams.subcarrier_spacing_khz ?? 30,
        num_resource_blocks: rfParams.num_resource_blocks ?? 100,
        channel_bandwidth_mhz: rfParams.channel_bandwidth_mhz ?? 40,
        electrical_tilt_deg: rfParams.electrical_tilt_deg ?? 0,
        mechanical_tilt_deg: rfParams.mechanical_tilt_deg ?? 0,
        vertical_beamwidth_deg: rfParams.vertical_beamwidth_deg ?? 8,
        max_vertical_attenuation_db: rfParams.max_vertical_attenuation_db ?? 30,
        path_loss_model: rfParams.path_loss_model ?? "3gpp_38901",
        propagation_scenario: rfParams.propagation_scenario ?? "umi_street_canyon",
        max_horizontal_attenuation_db: rfParams.max_horizontal_attenuation_db ?? 30,
        front_to_back_attenuation_db: rfParams.front_to_back_attenuation_db ?? 25,
        shadow_loss_db: rfParams.shadow_loss_db ?? 6,
        shadow_decay_db_per_100m: rfParams.shadow_decay_db_per_100m ?? 4,
        diffraction_base_loss_db: rfParams.diffraction_base_loss_db ?? 6,
        diffraction_slope_db_per_100m: rfParams.diffraction_slope_db_per_100m ?? 3,
        canyon_recovery_max_db: rfParams.canyon_recovery_max_db ?? 8,
        canyon_recovery_slope_db_per_100m: rfParams.canyon_recovery_slope_db_per_100m ?? 6,
        termination_rsrp_dbm: rfParams.termination_rsrp_dbm ?? -140,
        mimo_mode: rfParams.mimo_mode ?? "MIMO",
        enable_link_adaptation: rfParams.enable_link_adaptation ?? true,
      };

      return {
        plan_index: idx + 1,
        tx_lat: pr.lat,
        tx_lon: pr.lon,
        snapped_tx: { lat: snapped.lat, lon: snapped.lon },
        heatmap_overlay_file: `heatmap_overlay_TX${idx + 1}.png`,
        heatmap_radius_m: heatmap.radius_m ?? rfParams.max_range_m ?? 2000,
        freq_mhz: advancedRf.freq_mhz,
        tx_power_dbm: advancedRf.tx_power_dbm,
        tx_height_m: out.tx_height_m ?? rfParams.tx_height_m ?? 0,
        rx_height_m: out.rx_height_m ?? rfParams.rx_height_m ?? 1.5,
        electrical_tilt_deg: advancedRf.electrical_tilt_deg,
        mechanical_tilt_deg: advancedRf.mechanical_tilt_deg,
        rsrp_min_dbm: rsrpMin,
        rsrp_max_dbm: rsrpMax,
        heatmap_scale: heatmapScale,
        building_area_sqm: out.building_area_sqm ?? null,
        clutter_type: out.clutter_type ?? null,
        ray_mode: out.ray_mode ?? "2d",
        sectors: out.sectors || [],
        tx_phy_params: advancedRf,
        nr_params: {
          subcarrier_spacing_khz: advancedRf.subcarrier_spacing_khz,
          num_resource_blocks: advancedRf.num_resource_blocks,
          channel_bandwidth_mhz: advancedRf.channel_bandwidth_mhz,
          path_loss_model: advancedRf.path_loss_model,
          propagation_scenario: advancedRf.propagation_scenario,
        },
      };
    });

    const firstPlan = plans[0];
    const vmin = firstPlan?.heatmap_scale?.vmin ?? -140;
    const vmax = firstPlan?.heatmap_scale?.vmax ?? -60;

    return {
      export_timestamp_iso: new Date().toISOString(),
      planner_version: plannerVersion,
      view_state: viewState,
      heatmap_hue_definition: {
        vmin_dbm: vmin,
        vmax_dbm: vmax,
        actual_min_dbm: firstPlan?.heatmap_scale?.actual_min ?? null,
        actual_max_dbm: firstPlan?.heatmap_scale?.actual_max ?? null,
        gradient: "blue->cyan->green->yellow->orange->red",
        formula: "t=(rsrp-vmin)/(vmax-vmin)",
      },
      road_names: roadNames,
      plans: plans,
    };
  }

  function createExportZip(fullViewBlob, heatmapBlobs, metadata, filename, extraBlobs) {
    if (typeof JSZip === "undefined") {
      throw new Error("JSZip is not loaded. Add script tag for jszip.min.js");
    }
    const zip = new JSZip();
    if (fullViewBlob) {
      zip.file("full_view.png", fullViewBlob);
    }
    if (extraBlobs && typeof extraBlobs === "object") {
      for (const [name, blob] of Object.entries(extraBlobs)) {
        if (blob) zip.file(name, blob);
      }
    }
    if (Array.isArray(heatmapBlobs)) {
      heatmapBlobs.forEach((blob, i) => {
        if (blob) zip.file(`heatmap_overlay_TX${i + 1}.png`, blob);
      });
    }
    zip.file("metadata.json", JSON.stringify(metadata, null, 2));
    return zip.generateAsync({ type: "blob" }).then((blob) => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = filename || "rf_planner_export.zip";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(a.href);
      return blob;
    });
  }

  global.RFExportUtils = {
    base64DataUrlToBlob,
    buildExportMetadata,
    createExportZip,
  };
})(typeof window !== "undefined" ? window : this);
