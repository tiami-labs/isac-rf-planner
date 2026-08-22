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
      // Server configuration is authoritative. NR defaults are applied only to NR;
      // they must never leak into broadcast/DVT metadata.
      const raw = { ...fromGrid, ...(fromServer || {}) };
      const technology = String(raw.technology || grid.technology || "5g_nr").toLowerCase();
      const isDvt = technology === "dvt";
      const r = isDvt ? raw : { ...RF_PARAMS_EXPORT_DEFAULTS, ...raw };
      const heatmap = out.heatmap || {};
      const snapped = out.snapped_tx || { lat: pr.lat, lon: pr.lon };
      const channelAnalysis = out.channel_analysis || grid.channel_analysis_summary || null;

      let actualMin = Number.isFinite(Number(heatmap.actual_min)) ? Number(heatmap.actual_min) : null;
      let actualMax = Number.isFinite(Number(heatmap.actual_max)) ? Number(heatmap.actual_max) : null;
      if (actualMin == null || actualMax == null) {
        const series = isDvt
          ? (grid.field_strength_dbuv_m || grid.received_power_dbm)
          : grid.rsrp_dbm;
        if (Array.isArray(series) && series.length && series.length < 500000) {
          const finite = series.filter(Number.isFinite);
          if (finite.length) {
            actualMin = Math.min(...finite);
            actualMax = Math.max(...finite);
          }
        }
      }

      const heatmapLayer = String(heatmap.layer || (isDvt ? "field_strength_dbuv_m" : "rsrp_dbm"));
      const layerUnits = String(heatmap.units || (
        heatmapLayer.includes("field_strength") ? "dBµV/m" :
        heatmapLayer.includes("power") || heatmapLayer.includes("rsrp") ? "dBm" : ""
      ));
      const heatmapScale = {
        layer: heatmapLayer,
        units: layerUnits,
        vmin: Number.isFinite(Number(heatmap.vmin)) ? Number(heatmap.vmin) : null,
        vmax: Number.isFinite(Number(heatmap.vmax)) ? Number(heatmap.vmax) : null,
        actual_min: actualMin,
        actual_max: actualMax,
      };

      const sectorList = !isDvt && Array.isArray(out.sectors) ? out.sectors : [];
      const hasPerSectorHeatmaps = !isDvt && out.heatmap_by_sector && typeof out.heatmap_by_sector === "object" &&
        Object.keys(out.heatmap_by_sector).length > 0;
      const parentHeatmapFile = `heatmap_overlay_TX${idx + 1}.png`;
      const sectorExportRows = sectorList.map((sec, j) => ({
        transmitter_slot: idx + 1,
        sector_slot: j + 1,
        sector_id: sec.sector_id != null ? String(sec.sector_id) : `sector_${j + 1}`,
        parent_heatmap_raster: parentHeatmapFile,
        per_sector_heatmap: hasPerSectorHeatmaps
          ? `heatmap_gnb${idx + 1}_sector_${j + 1}_${String(sec.sector_id || j + 1).replace(/[^a-zA-Z0-9_-]/g, "_")}.png`
          : null,
        freq_mhz: sec.freq_mhz,
        tx_power_dbm: sec.tx_power_dbm,
        channel_bandwidth_mhz: sec.channel_bandwidth_mhz,
        azimuth_deg: sec.azimuth_deg,
        beamwidth_h_deg: sec.beamwidth_h_deg,
        beamwidth_v_deg: sec.beamwidth_v_deg,
        electrical_tilt_deg: sec.electrical_tilt_deg,
        mechanical_tilt_deg: sec.mechanical_tilt_deg,
        tx_antenna_gain_dbi: sec.tx_antenna_gain_dbi,
        pci: sec.pci,
      }));

      let frequencyMhz = Number(r.freq_mhz);
      let bandwidthMhz = Number(r.channel_bandwidth_mhz);
      let transmitterParameters;
      if (isDvt) {
        const dvt = r.dvt || {};
        const fcHz = Number(dvt.fc);
        const bwHz = Number(dvt.bandwidth);
        if (Number.isFinite(fcHz) && fcHz > 0) frequencyMhz = fcHz / 1e6;
        if (Number.isFinite(bwHz) && bwHz > 0) bandwidthMhz = bwHz / 1e6;
        transmitterParameters = {
          technology: "dvt",
          waveform: r.waveform || dvt.waveform || grid.waveform || null,
          frequency_mhz: Number.isFinite(frequencyMhz) ? frequencyMhz : null,
          occupied_bandwidth_mhz: Number.isFinite(bandwidthMhz) ? bandwidthMhz : null,
          transmitter: dvt.tx || null,
          power: dvt.power || null,
          antenna: dvt.antenna || null,
          propagation_model: r.path_loss_model || null,
          max_range_m: r.max_range_m ?? null,
          step_m: r.step_m ?? null,
          dtheta_deg: r.dtheta_deg ?? null,
          terrain_enabled: r.terrain_enabled ?? null,
        };
      } else {
        transmitterParameters = {
          technology: "5g_nr",
          waveform: r.waveform || grid.waveform || "5g_nr",
          frequency_mhz: Number.isFinite(frequencyMhz) ? frequencyMhz : null,
          tx_power_dbm: r.tx_power_dbm ?? null,
          channel_bandwidth_mhz: Number.isFinite(bandwidthMhz) ? bandwidthMhz : null,
          noise_figure_db: r.noise_figure_db,
          subcarrier_spacing_khz: r.subcarrier_spacing_khz,
          num_resource_blocks: r.num_resource_blocks,
          path_loss_model: r.path_loss_model,
          propagation_scenario: r.propagation_scenario,
          mimo_mode: r.mimo_mode,
          num_tx_antennas: r.num_tx_antennas,
          num_rx_antennas: r.num_rx_antennas,
          tx_antenna_gain_dbi: r.tx_antenna_gain_dbi,
          tx_feeder_loss_db: r.tx_feeder_loss_db,
          max_range_m: r.max_range_m,
          step_m: r.step_m,
          dtheta_deg: r.dtheta_deg,
        };
      }

      const plan = {
        plan_index: idx + 1,
        transmitter_index: idx + 1,
        technology,
        waveform: r.waveform || grid.waveform || null,
        tx_lat: pr.lat,
        tx_lon: pr.lon,
        snapped_tx: { lat: snapped.lat, lon: snapped.lon },
        heatmap_overlay_file: parentHeatmapFile,
        heatmap_radius_m: heatmap.radius_m ?? r.max_range_m ?? null,
        heatmap_scale: heatmapScale,
        frequency_mhz: Number.isFinite(frequencyMhz) ? frequencyMhz : null,
        bandwidth_mhz: Number.isFinite(bandwidthMhz) ? bandwidthMhz : null,
        tx_height_m: out.tx_height_m ?? r.tx_height_m ?? null,
        // In ISAC mode the solver's rf_params.rx_height_m may be repurposed as
        // the candidate-target propagation height. Do not export that value as
        // though it were the sensing receiver antenna height.
        rx_height_m: channelAnalysis?.receiver ? null : (out.rx_height_m ?? r.rx_height_m ?? null),
        analysis_receiver_antenna_height_m: channelAnalysis?.receiver?.antennaHeightMagl ?? null,
        analysis_receiver_site_altitude_m_amsl: channelAnalysis?.receiver?.altitudeMamsl ?? null,
        isac_target_height_m_agl: channelAnalysis?.target?.heightMagl ?? null,
        rf_config_used: fromServer,
        transmitter_parameters: transmitterParameters,
        channel_analysis: channelAnalysis,
        channel_analysis_product: out.channel_analysis?.data_product || out.channel_analysis_product || null,
        selected_isac_hypothesis: out._interactive_isac ? {
          target: out._interactive_isac.target,
          motion: out._interactive_isac.motion,
          processing: out._interactive_isac.processing,
          processing_assessment: out._interactive_isac.processing_assessment,
          counts: out._interactive_isac.counts,
          selected_target: out._interactive_isac.selected_target,
          layer: out._interactive_isac.layer ? {
            layer: out._interactive_isac.layer.layer,
            label: out._interactive_isac.layer.label,
            units: out._interactive_isac.layer.units,
            vmin: out._interactive_isac.layer.vmin,
            vmax: out._interactive_isac.layer.vmax,
          } : null,
        } : null,
        building_area_sqm: out.building_area_sqm ?? null,
        clutter_type: out.clutter_type ?? null,
        ray_mode: out.ray_mode ?? r.ray_mode ?? null,
        sectors: sectorList,
        sector_export_rows: sectorExportRows,
      };
      // Preserve NR-specific compatibility keys only for an actual NR plan.
      if (!isDvt) {
        plan.gnodeb_index = idx + 1;
        plan.nr_params = {
          subcarrier_spacing_khz: r.subcarrier_spacing_khz,
          num_resource_blocks: r.num_resource_blocks,
          channel_bandwidth_mhz: r.channel_bandwidth_mhz,
          path_loss_model: r.path_loss_model,
          propagation_scenario: r.propagation_scenario,
        };
      }
      return plan;
    });

    const firstScale = plans[0]?.heatmap_scale || {};
    return {
      export_schema: "agentic_rf_planner.plan_export",
      export_schema_version: "2.0",
      export_timestamp_iso: new Date().toISOString(),
      planner_version: plannerVersion,
      view_state: viewState,
      heatmap_color_definition: {
        layer: firstScale.layer ?? null,
        units: firstScale.units ?? null,
        vmin: firstScale.vmin ?? null,
        vmax: firstScale.vmax ?? null,
        actual_min: firstScale.actual_min ?? null,
        actual_max: firstScale.actual_max ?? null,
        gradient: "blue->cyan->green->yellow->orange->red",
        formula: "t=(value-vmin)/(vmax-vmin)",
      },
      road_names: roadNames,
      plans,
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
    createExportZip,
  };
})(typeof window !== "undefined" ? window : this);
