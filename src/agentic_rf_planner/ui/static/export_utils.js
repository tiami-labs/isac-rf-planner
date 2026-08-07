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

  /**
   * Defaults aligned with `agentic_rf_planner/pipeline/schemas.py` `RFParams` (not legacy UI guesswork).
   * Used only when the plan payload is missing a field. Global link defaults are not per-sector; see
   * `sector_export_rows` and `out.sectors` when carriers differ.
   */
  const HEATMAP_EXPORTS = {
    heatmap: "primary_coverage",
    heatmap_terrain: "terrain_shadow_loss",
    heatmap_sinr: "sinr",
    heatmap_carrier_to_noise: "carrier_to_noise",
    heatmap_received_power: "received_power",
    heatmap_incident_power: "incident_power_at_target",
    heatmap_bistatic_echo: "bistatic_echo_power",
    heatmap_bistatic_snr: "bistatic_postprocessing_snr",
    heatmap_bistatic_margin: "bistatic_detection_margin",
    heatmap_bistatic_doppler: "bistatic_doppler",
    heatmap_bistatic_excess_delay: "bistatic_excess_delay",
    heatmap_bistatic_path_range: "bistatic_total_path_range",
    heatmap_bistatic_angle: "bistatic_angle",
    heatmap_bistatic_detectable: "bistatic_detectability",
    heatmap_bistatic_return_path_loss: "target_to_receiver_path_loss",
    heatmap_bistatic_total_path_loss: "total_bistatic_path_loss",
    heatmap_isac_quality: "isac_target_quality",
  };

  function sanitizeFilename(value) {
    return String(value || "item").replace(/[^a-zA-Z0-9_.-]/g, "_");
  }

  function jsonWithoutEmbeddedImages(value) {
    const seen = new WeakSet();
    return JSON.parse(JSON.stringify(value, (key, item) => {
      if (key === "png_b64" && typeof item === "string") {
        return `[embedded image omitted from JSON; exported as PNG, ${item.length} characters]`;
      }
      if (item && typeof item === "object") {
        if (seen.has(item)) return "[circular reference omitted]";
        seen.add(item);
      }
      return item;
    }));
  }

  function heatmapManifest(out, planIndex) {
    const rows = [];
    for (const [key, stem] of Object.entries(HEATMAP_EXPORTS)) {
      const hm = out && out[key];
      if (!hm || !hm.png_b64) continue;
      rows.push({
        response_key: key,
        layer: hm.layer || stem,
        units: hm.units || null,
        file: `heatmaps/TX${planIndex}/${stem}.png`,
        width: hm.width ?? null,
        height: hm.height ?? null,
        radius_m: hm.radius_m ?? null,
        scale_min: hm.vmin ?? null,
        scale_max: hm.vmax ?? null,
        actual_min: hm.actual_min ?? null,
        actual_max: hm.actual_max ?? null,
      });
    }
    return rows;
  }

  async function collectPlanExportArtifacts(planResults) {
    const files = {};
    const plans = [];
    for (let index = 0; index < (planResults || []).length; index++) {
      const pr = planResults[index] || {};
      const out = pr.out || pr.data || {};
      const planIndex = index + 1;
      const planDir = `plans/TX${planIndex}`;
      const heatmaps = heatmapManifest(out, planIndex);
      for (const row of heatmaps) {
        const hm = out[row.response_key];
        const blob = base64DataUrlToBlob(hm && hm.png_b64);
        if (blob) files[row.file] = blob;
      }

      const perSector = out.heatmap_by_sector;
      const sectorFiles = [];
      if (perSector && typeof perSector === "object") {
        for (const [sectorId, hm] of Object.entries(perSector)) {
          if (!hm || !hm.png_b64) continue;
          const name = `heatmaps/TX${planIndex}/sectors/${sanitizeFilename(sectorId)}.png`;
          const blob = base64DataUrlToBlob(hm.png_b64);
          if (blob) {
            files[name] = blob;
            sectorFiles.push({ sector_id: String(sectorId), file: name, layer: hm.layer || "rsrp", units: hm.units || "dBm" });
          }
        }
      }

      const product = out.channel_analysis_product || out.channel_analysis?.data_product || null;
      let productFile = null;
      let productError = null;
      if (out.channel_analysis && (!product || !product.download_url)) {
        throw new Error(`TX${planIndex} has channel analysis but no machine-readable product URL; export stopped rather than producing a partial archive.`);
      }
      if (product && product.download_url) {
        try {
          const response = await fetch(product.download_url);
          if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
          productFile = `channel/TX${planIndex}_complete_rf_channel_grid.npz`;
          files[productFile] = await response.blob();
        } catch (error) {
          productError = String(error && error.message ? error.message : error);
          throw new Error(`TX${planIndex} machine-readable channel product could not be included: ${productError}`);
        }
      }

      const planJsonFile = `${planDir}/complete_plan_response.json`;
      files[planJsonFile] = new Blob([JSON.stringify(jsonWithoutEmbeddedImages(out), null, 2)], { type: "application/json" });
      const settingsFile = `${planDir}/complete_settings.json`;
      files[settingsFile] = new Blob([JSON.stringify({
        requested_tx: { latitude: pr.lat ?? null, longitude: pr.lon ?? null },
        rf_config_used: out.rf_config_used || out.grid?.rf_params || null,
        channel_analysis: out.channel_analysis || null,
        geometry_source: out.geometry_source || null,
      }, null, 2)], { type: "application/json" });

      plans.push({
        plan_index: planIndex,
        response_file: planJsonFile,
        settings_file: settingsFile,
        heatmaps,
        sector_heatmaps: sectorFiles,
        machine_grid_file: productFile,
        machine_grid_download_error: productError,
        machine_grid_descriptor: product,
      });
    }
    const manifest = {
      schema: "rf_planner_complete_ui_export",
      schema_version: "2.0",
      export_scope: "all settings, every available heatmap layer, full plan responses, and machine-readable per-target RF/channel arrays",
      generated_at_iso: new Date().toISOString(),
      plans,
    };
    files["export_manifest.json"] = new Blob([JSON.stringify(manifest, null, 2)], { type: "application/json" });
    return { files, manifest };
  }

  const RF_PARAMS_EXPORT_DEFAULTS = {
    noise_figure_db: 7.0,
    subcarrier_spacing_khz: 15.0,
    num_resource_blocks: 100,
    channel_bandwidth_mhz: 20.0,
    num_tx_antennas: 1,
    num_rx_antennas: 1,
    mimo_mode: "SISO",
    enable_link_adaptation: true,
    electrical_tilt_deg: 0.0,
    mechanical_tilt_deg: 0.0,
    vertical_beamwidth_deg: 8.0,
    max_vertical_attenuation_db: 30.0,
    max_horizontal_attenuation_db: 30.0,
    front_to_back_attenuation_db: 25.0,
    path_loss_model: "3gpp_38901",
    propagation_scenario: "umi_street_canyon",
    max_range_m: 2000.0,
    step_m: 5.0,
    dtheta_deg: 5.0,
    ray_mode: "2d",
    tx_height_m: 0.0,
    rx_height_m: 1.5,
    shadow_loss_db: 6.0,
    shadow_decay_db_per_100m: 4.0,
    shadow_loss_cap_db: 22.0,
    diffraction_base_loss_db: 6.0,
    diffraction_slope_db_per_100m: 3.0,
    diffraction_loss_cap_db: 18.0,
    canyon_recovery_max_db: 8.0,
    canyon_recovery_slope_db_per_100m: 6.0,
    termination_rsrp_dbm: -140.0,
    tx_antenna_gain_dbi: 17.0,
    tx_feeder_loss_db: 2.0,
    reference_signal_offset_db: -18.0,
    ue_antenna_gain_dbi: 0.0,
    max_rsrp_dbm: -62.0,
    rt_max_bounces: 1,
    rt_max_reflections_per_sample: 2,
    rt_max_wall_candidates: 40,
    rt_reflection_loss_db: 8.0,
    rt_debug_sample_stride: 25,
  };

  function buildExportMetadata(planResults, options) {
    const opts = options || {};
    const plannerVersion = opts.plannerVersion || "3d";
    const viewState = opts.viewState || {};
    const roadNames = opts.roadNames || [];

    const plans = (planResults || []).map((pr, idx) => {
      const out = pr.out || pr.data || {};
      const grid = out.grid || {};
      const fromServer = out.rf_config_used && typeof out.rf_config_used === "object" ? out.rf_config_used : null;
      const fromGrid = grid.rf_params && typeof grid.rf_params === "object" ? grid.rf_params : {};
      const r = { ...RF_PARAMS_EXPORT_DEFAULTS, ...fromGrid, ...(fromServer || {}) };
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
        freq_mhz: r.freq_mhz != null ? r.freq_mhz : 3500,
        tx_power_dbm: r.tx_power_dbm != null ? r.tx_power_dbm : 43,
        noise_floor_dbm: r.noise_floor_dbm != null ? r.noise_floor_dbm : null,
        noise_figure_db: r.noise_figure_db,
        subcarrier_spacing_khz: r.subcarrier_spacing_khz,
        num_resource_blocks: r.num_resource_blocks,
        channel_bandwidth_mhz: r.channel_bandwidth_mhz,
        electrical_tilt_deg: r.electrical_tilt_deg,
        mechanical_tilt_deg: r.mechanical_tilt_deg,
        vertical_beamwidth_deg: r.vertical_beamwidth_deg,
        max_vertical_attenuation_db: r.max_vertical_attenuation_db,
        path_loss_model: r.path_loss_model,
        propagation_scenario: r.propagation_scenario,
        max_horizontal_attenuation_db: r.max_horizontal_attenuation_db,
        front_to_back_attenuation_db: r.front_to_back_attenuation_db,
        shadow_loss_db: r.shadow_loss_db,
        shadow_decay_db_per_100m: r.shadow_decay_db_per_100m,
        shadow_loss_cap_db: r.shadow_loss_cap_db,
        diffraction_base_loss_db: r.diffraction_base_loss_db,
        diffraction_slope_db_per_100m: r.diffraction_slope_db_per_100m,
        diffraction_loss_cap_db: r.diffraction_loss_cap_db,
        canyon_recovery_max_db: r.canyon_recovery_max_db,
        canyon_recovery_slope_db_per_100m: r.canyon_recovery_slope_db_per_100m,
        termination_rsrp_dbm: r.termination_rsrp_dbm,
        mimo_mode: r.mimo_mode,
        num_tx_antennas: r.num_tx_antennas,
        num_rx_antennas: r.num_rx_antennas,
        enable_link_adaptation: r.enable_link_adaptation,
        fixed_modulation: r.fixed_modulation ?? null,
        building_attenuation: r.building_attenuation ?? null,
        ray_mode: r.ray_mode,
        dtheta_deg: r.dtheta_deg,
        max_range_m: r.max_range_m,
        step_m: r.step_m,
        tx_antenna_gain_dbi: r.tx_antenna_gain_dbi,
        tx_feeder_loss_db: r.tx_feeder_loss_db,
        reference_signal_offset_db: r.reference_signal_offset_db,
        ue_antenna_gain_dbi: r.ue_antenna_gain_dbi,
        max_rsrp_dbm: r.max_rsrp_dbm,
        rt_max_bounces: r.rt_max_bounces,
        rt_max_reflections_per_sample: r.rt_max_reflections_per_sample,
        rt_max_wall_candidates: r.rt_max_wall_candidates,
        rt_reflection_loss_db: r.rt_reflection_loss_db,
        rt_debug_sample_stride: r.rt_debug_sample_stride,
      };

      const parentHeatmapFile = `heatmap_overlay_TX${idx + 1}.png`;
      const sectorList = Array.isArray(out.sectors) ? out.sectors : [];
      const hasPerSectorHeatmaps = out.heatmap_by_sector && typeof out.heatmap_by_sector === "object" &&
        Object.keys(out.heatmap_by_sector).length > 0;
      const rowNote = hasPerSectorHeatmaps
        ? "Per-sector RSRP raster (this sector’s beam only). heatmap_overlay_TXn.png is the first sector in plan order for a quick preview."
        : "Coverage PNG is best-server RSRP (max over sectors at each point) for this gNodeB; for directional plots per sector_id, the API must return heatmap_by_sector (see 3D planner).";
      const sector_export_rows = sectorList.map((sec, j) => ({
        gnodeb_slot: idx + 1,
        sector_slot: j + 1,
        sector_id: sec.sector_id != null ? String(sec.sector_id) : `sector_${j + 1}`,
        parent_heatmap_raster: parentHeatmapFile,
        per_sector_heatmap: hasPerSectorHeatmaps
          ? `heatmap_gnb${idx + 1}_sector_${j + 1}_${
              String(sec.sector_id || j + 1).replace(/[^a-zA-Z0-9_-]/g, "_")
            }.png`
          : null,
        note: rowNote,
        freq_mhz: sec.freq_mhz,
        tx_power_dbm: sec.tx_power_dbm,
        channel_bandwidth_mhz: sec.channel_bandwidth_mhz,
        azimuth_deg: sec.azimuth_deg,
        beamwidth_h_deg: sec.beamwidth_h_deg,
        beamwidth_v_deg: sec.beamwidth_v_deg,
        start_angle_deg: sec.start_angle_deg,
        end_angle_deg: sec.end_angle_deg,
        electrical_tilt_deg: sec.electrical_tilt_deg,
        mechanical_tilt_deg: sec.mechanical_tilt_deg,
        tx_antenna_gain_dbi: sec.tx_antenna_gain_dbi,
        pci: sec.pci,
      }));

      return {
        plan_index: idx + 1,
        gnodeb_index: idx + 1,
        tx_lat: pr.lat,
        tx_lon: pr.lon,
        snapped_tx: { lat: snapped.lat, lon: snapped.lon },
        heatmap_overlay_file: parentHeatmapFile,
        heatmap_radius_m: heatmap.radius_m ?? r.max_range_m ?? 2000,
        sector_export_rows,
        freq_mhz: advancedRf.freq_mhz,
        tx_power_dbm: advancedRf.tx_power_dbm,
        tx_height_m: out.tx_height_m ?? r.tx_height_m ?? 0,
        rx_height_m: out.rx_height_m ?? r.rx_height_m ?? 1.5,
        rf_config_used: fromServer,
        electrical_tilt_deg: advancedRf.electrical_tilt_deg,
        mechanical_tilt_deg: advancedRf.mechanical_tilt_deg,
        rsrp_min_dbm: rsrpMin,
        rsrp_max_dbm: rsrpMax,
        heatmap_scale: heatmapScale,
        building_area_sqm: out.building_area_sqm ?? null,
        clutter_type: out.clutter_type ?? null,
        ray_mode: out.ray_mode ?? "2d",
        technology: String(r.technology || out.grid?.technology || "5g_nr"),
        waveform: String(out.grid?.waveform || r.dvt?.waveform || (r.technology === "dvt" ? "baseline" : "5g_nr")),
        sectors: out.sectors || [],
        broadcast_antenna: out.broadcast_antenna || r.dvt?.antenna || null,
        complete_settings: fromServer || fromGrid || null,
        channel_analysis: out.channel_analysis || grid.channel_analysis_summary || null,
        channel_analysis_product: out.channel_analysis_product || out.channel_analysis?.data_product || null,
        geometry_source: out.geometry_source || null,
        available_heatmaps: heatmapManifest(out, idx + 1),
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
      schema: "rf_planner_ui_export_metadata",
      schema_version: "2.0",
      export_scope: "all settings, all available heatmap layers, full plan response JSON, and machine-readable channel products",
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

  function createExportZip(fullViewBlob, heatmapBlobs, metadata, filename, extraBlobs, sectorHeatmapBlobs) {
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
        if (blob) {
          const name = `heatmap_overlay_TX${i + 1}.png`;
          zip.file(name, blob);
          const perSec =
            Array.isArray(sectorHeatmapBlobs) && sectorHeatmapBlobs[i] && typeof sectorHeatmapBlobs[i] === "object"
              ? sectorHeatmapBlobs[i]
              : null;
          const plan = metadata && Array.isArray(metadata.plans) ? metadata.plans[i] : null;
          const rows = plan && Array.isArray(plan.sector_export_rows) ? plan.sector_export_rows : [];
          rows.forEach((row, j) => {
            const sid = String(row.sector_id || j + 1).replace(/[^a-zA-Z0-9_-]/g, "_");
            const key = row.sector_id != null ? String(row.sector_id) : null;
            const useBlob = perSec && key && perSec[key] ? perSec[key] : blob;
            zip.file(`heatmap_gnb${i + 1}_sector_${j + 1}_${sid}.png`, useBlob);
          });
        }
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
    collectPlanExportArtifacts,
    createExportZip,
  };
})(typeof window !== "undefined" ? window : this);
