(function () {
  "use strict";

  const DVT_PROFILES = new Set(["atsc1", "atsc3", "dvbt", "baseline"]);
  const PROFILE_LABELS = {
    "5g_nr": "5G NR",
    atsc1: "ATSC 1.0",
    atsc3: "ATSC 3.0",
    dvbt: "DVB-T",
    baseline: "DVT baseline",
  };

  // These controls are valid only for the NR request model. DVT has its own
  // explicit waveform, power-chain, antenna-pattern and receiver controls.
  const NR_ONLY_FIELD_IDS = [
    "freq-mhz",
    "tx-power-dbm",
    "noise-figure-db",
    "scs-khz",
    "num-rb",
    "bw-mhz",
    "electrical-tilt-deg",
    "mechanical-tilt-deg",
    "vertical-beamwidth-deg",
    "max-vertical-atten-db",
    "path-loss-model",
    "propagation-scenario",
    "max-horizontal-atten-db",
    "front-to-back-atten-db",
    "termination-rsrp-dbm",
    "mimo-mode",
    "link-adapt",
    "tx-antenna-gain-dbi",
  ];

  const NR_ONLY_REQUEST_KEYS = [
    "freq_mhz",
    "tx_power_dbm",
    "subcarrier_spacing_khz",
    "num_resource_blocks",
    "num_tx_antennas",
    "num_rx_antennas",
    "mimo_mode",
    "enable_link_adaptation",
    "fixed_modulation",
    "sectors",
    "tx_chain_gain_db",
    "tx_antenna_gain_dbi",
    "tx_feeder_loss_db",
    "reference_signal_offset_db",
    "max_rsrp_dbm",
    "electrical_tilt_deg",
    "mechanical_tilt_deg",
    "vertical_beamwidth_deg",
    "max_vertical_attenuation_db",
    "max_horizontal_attenuation_db",
    "front_to_back_attenuation_db",
    "azimuth_deg",
    "horizontal_beamwidth_deg",
    "path_loss_model",
    "propagation_scenario",
  ];

  function byId(id) {
    return document.getElementById(id);
  }

  function getProfile() {
    return String(byId("waveform-profile")?.value || "5g_nr").trim().toLowerCase();
  }

  function isDvtProfile(profile) {
    return DVT_PROFILES.has(String(profile || "").toLowerCase());
  }

  function requiredNumber(id, label, options) {
    const el = byId(id);
    const value = Number(el?.value);
    const opts = options || {};
    if (!Number.isFinite(value)) throw new Error(`${label} is required.`);
    if (opts.gt != null && !(value > opts.gt)) throw new Error(`${label} must be greater than ${opts.gt}.`);
    if (opts.ge != null && !(value >= opts.ge)) throw new Error(`${label} must be at least ${opts.ge}.`);
    if (opts.le != null && !(value <= opts.le)) throw new Error(`${label} must be at most ${opts.le}.`);
    return value;
  }

  function optionalText(id) {
    const value = String(byId(id)?.value || "").trim();
    return value || null;
  }

  function stationValue(id) {
    const value = optionalText(id);
    return value == null ? undefined : value;
  }

  function setVisible(el, visible) {
    if (!el) return;
    el.hidden = !visible;
    el.style.display = visible ? "" : "none";
  }

  function setFieldVisible(id, visible) {
    const el = byId(id);
    if (!el) return;
    const wrapper = el.closest("div");
    setVisible(wrapper || el, visible);
  }

  function setValueIfDefault(id, oldValues, value) {
    const el = byId(id);
    if (!el) return;
    const current = String(el.value || "").trim();
    if (oldValues.includes(current)) el.value = String(value);
  }

  function syncPowerMode() {
    const mode = String(byId("dvt-power-mode")?.value || "erp");
    setVisible(byId("dvt-erp-fields"), mode === "erp");
    setVisible(byId("dvt-conducted-fields"), mode === "conducted");
  }

  function syncCoverageLayerOptions(dvt) {
    const rsrpOption = byId("coverage-layer-rsrp");
    if (rsrpOption) {
      rsrpOption.hidden = dvt;
      rsrpOption.disabled = dvt;
    }
    const layer = byId("coverage-display-layer");
    if (!layer) return;
    if (dvt && layer.value === "rsrp") layer.value = "field_strength";
    if (!dvt && !String(layer.value || "").trim()) layer.value = "rsrp";
  }

  function syncUi(options) {
    const profile = getProfile();
    const dvt = isDvtProfile(profile);

    setVisible(byId("dvt-transmitter-fields"), dvt);
    NR_ONLY_FIELD_IDS.forEach((id) => setFieldVisible(id, !dvt));
    setVisible(byId("sector-configuration-section"), !dvt);
    setVisible(byId("sector-overlay-control"), !dvt);

    const sectorToggle = byId("show-sectors-toggle");
    if (sectorToggle && dvt) sectorToggle.checked = false;

    const advancedSummary = byId("advanced-rf-summary");
    if (advancedSummary) {
      advancedSummary.textContent = dvt
        ? "DVT propagation calibration"
        : "Advanced RF params";
    }

    const meshHeading = byId("mesh-profile-heading");
    if (meshHeading) meshHeading.textContent = dvt ? "Coverage sampling" : "3D mesh profile params";
    const meshHint = byId("mesh-profile-params-hint");
    if (meshHint) {
      meshHint.textContent = dvt
        ? "Coverage radius, radial spacing, and bearing spacing for the selected DVT transmitter."
        : "Used to generate persisted Google-mesh ray profiles (when ray_mode=3d or 3d_rt and missing).";
    }

    const summary = byId("waveform-summary");
    if (summary) {
      summary.textContent = dvt
        ? `${PROFILE_LABELS[profile]} transmitter. Only DVT and shared physical propagation controls are active.`
        : "5G NR transmitter. NR waveform, resource-grid, link-adaptation and sector controls are active.";
    }

    syncCoverageLayerOptions(dvt);

    if (dvt) {
      const isDvbt = profile === "dvbt";
      setValueIfDefault("dvt-fc-mhz", ["3500", "3500.0", "587", "587.0"], "587");
      setValueIfDefault("dvt-bandwidth-mhz", ["40", "40.0", "6", "6.0", "8", "8.0"], isDvbt ? "8" : "6");
      setValueIfDefault("dvt-fs-mhz", ["", "40", "40.0"], "10");
      setValueIfDefault("tx-height-m", ["10", "10.0"], "320");
      setValueIfDefault("max-range", ["2500", "2500.0", "2000", "2000.0"], "20000");
      setValueIfDefault("dr-m", ["5", "5.0"], "20");
      setValueIfDefault("dvt-dtheta-deg", ["5", "5.0"], "0.25");
      setValueIfDefault("coverage-display-layer", ["rsrp"], "field_strength");
      const standard = byId("dvt-station-standard");
      if (standard && !String(standard.value || "").trim()) standard.value = PROFILE_LABELS[profile];
    }

    syncPowerMode();
    if (typeof options?.onChange === "function") options.onChange({ profile, technology: dvt ? "dvt" : "5g_nr" });
  }

  function getPatternValues() {
    return {
      azimuthDeg: requiredNumber("dvt-azimuth-deg", "DVT azimuth", { ge: 0, le: 360 }),
      elevationDeg: requiredNumber("dvt-elevation-deg", "DVT elevation", { ge: -90, le: 90 }),
      beamwidthHDeg: requiredNumber("dvt-beamwidth-h-deg", "DVT horizontal beamwidth", { gt: 0, le: 360 }),
      beamwidthVDeg: requiredNumber("dvt-beamwidth-v-deg", "DVT vertical beamwidth", { gt: 0, le: 180 }),
      electricalTiltDeg: requiredNumber("dvt-electrical-tilt-deg", "DVT electrical tilt", { ge: -30, le: 30 }),
      mechanicalTiltDeg: requiredNumber("dvt-mechanical-tilt-deg", "DVT mechanical tilt", { ge: -30, le: 30 }),
      maxHorizontalAttenuationDb: requiredNumber("dvt-max-horizontal-atten-db", "DVT maximum horizontal attenuation", { ge: 0, le: 60 }),
      frontToBackAttenuationDb: requiredNumber("dvt-front-to-back-atten-db", "DVT front-to-back attenuation", { ge: 0, le: 60 }),
      maxVerticalAttenuationDb: requiredNumber("dvt-max-vertical-atten-db", "DVT maximum vertical attenuation", { ge: 0, le: 60 }),
    };
  }

  function buildPlanFields(args) {
    const profile = getProfile();
    if (!isDvtProfile(profile)) {
      return {
        technology: "5g_nr",
        waveform: "5g_nr",
      };
    }

    const lat = Number(args?.lat);
    const lon = Number(args?.lon);
    const txHeightM = Number(args?.txHeightM);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) throw new Error("DVT transmitter latitude/longitude are required.");
    if (!Number.isFinite(txHeightM) || txHeightM < 0) throw new Error("DVT antenna height must be zero or greater.");

    const fcMhz = requiredNumber("dvt-fc-mhz", "DVT center frequency", { gt: 0 });
    const fsMhz = requiredNumber("dvt-fs-mhz", "DVT sample rate", { gt: 0 });
    const bandwidthMhz = requiredNumber("dvt-bandwidth-mhz", "DVT channel bandwidth", { gt: 0 });
    if (fsMhz < bandwidthMhz) throw new Error("DVT sample rate must be greater than or equal to channel bandwidth.");

    const pattern = getPatternValues();
    const powerMode = String(byId("dvt-power-mode")?.value || "erp");
    let power;
    if (powerMode === "conducted") {
      power = {
        conductedPowerKw: requiredNumber("dvt-conducted-power-kw", "Conducted transmitter power", { gt: 0 }),
        txGainDb: requiredNumber("dvt-tx-gain-db", "TX-chain gain"),
        feederLossDb: requiredNumber("dvt-feeder-loss-db", "Feeder loss", { ge: 0 }),
        antennaGainDbi: requiredNumber("dvt-antenna-gain-dbi", "Antenna gain"),
        polarization: String(byId("dvt-polarization")?.value || ""),
      };
    } else {
      power = {
        erpKw: requiredNumber("dvt-erp-kw", "ERP", { gt: 0 }),
        polarization: String(byId("dvt-polarization")?.value || ""),
      };
    }

    const station = {
      callSign: stationValue("dvt-call-sign"),
      virtualChannel: stationValue("dvt-virtual-channel"),
      rfChannel: stationValue("dvt-rf-channel"),
      physicalChannel: stationValue("dvt-physical-channel"),
      facilityId: stationValue("dvt-facility-id"),
      city: stationValue("dvt-city"),
      network: stationValue("dvt-network"),
      standard: stationValue("dvt-station-standard") || PROFILE_LABELS[profile],
      band: stationValue("dvt-band"),
      source: stationValue("dvt-source"),
      sourceUrl: stationValue("dvt-source-url"),
    };
    Object.keys(station).forEach((key) => station[key] === undefined && delete station[key]);

    const maxRangeEl = byId("max-range");
    const stepEl = byId("dr-m");
    const maxRangeM = maxRangeEl ? requiredNumber("max-range", "Maximum range", { gt: 0 }) : 20000;
    const stepM = stepEl ? requiredNumber("dr-m", "Radial step", { gt: 0 }) : 20;
    const dthetaDeg = requiredNumber("dvt-dtheta-deg", "Bearing step", { gt: 0, le: 360 });

    return {
      technology: "dvt",
      waveform: profile,
      dvt: {
        fc: fcMhz * 1e6,
        fs: fsMhz * 1e6,
        bandwidth: bandwidthMhz * 1e6,
        waveform: profile,
        tx: {
          latitude: lat,
          longitude: lon,
          altitude: requiredNumber("dvt-site-altitude-m", "Site altitude"),
          antennaHeight: txHeightM,
          name: String(byId("dvt-tx-name")?.value || "DVT TX").trim() || "DVT TX",
          azimuthDeg: pattern.azimuthDeg,
          elevationDeg: pattern.elevationDeg,
          beamwidthHDeg: pattern.beamwidthHDeg,
          beamwidthVDeg: pattern.beamwidthVDeg,
          electricalTiltDeg: pattern.electricalTiltDeg,
          mechanicalTiltDeg: pattern.mechanicalTiltDeg,
          maxHorizontalAttenuationDb: pattern.maxHorizontalAttenuationDb,
          frontToBackAttenuationDb: pattern.frontToBackAttenuationDb,
          maxVerticalAttenuationDb: pattern.maxVerticalAttenuationDb,
        },
        power,
        station,
      },
      noise_figure_db: requiredNumber("dvt-noise-figure-db", "Receiver noise figure"),
      ue_antenna_gain_dbi: requiredNumber("dvt-rx-antenna-gain-dbi", "Receiver antenna gain"),
      termination_rsrp_dbm: requiredNumber("dvt-termination-power-dbm", "Ray termination power"),
      max_range_m: maxRangeM,
      step_m: stepM,
      dtheta_deg: dthetaDeg,
      coverage_display_layer: "field_strength",
      compact_output: true,
      sectors: null,
    };
  }

  function sanitizePlanBody(body) {
    if (!body || typeof body !== "object" || !isDvtProfile(getProfile())) return body;
    NR_ONLY_REQUEST_KEYS.forEach((key) => delete body[key]);
    body.sectors = null;
    return body;
  }

  function init(options) {
    byId("waveform-profile")?.addEventListener("change", () => syncUi(options));
    byId("dvt-power-mode")?.addEventListener("change", syncPowerMode);
    syncUi(options || {});
  }

  window.RFWaveformUI = {
    init,
    syncUi,
    getProfile,
    isDvt: () => isDvtProfile(getProfile()),
    buildPlanFields,
    sanitizePlanBody,
    profileLabel: (value) => PROFILE_LABELS[value] || value,
  };
}());
