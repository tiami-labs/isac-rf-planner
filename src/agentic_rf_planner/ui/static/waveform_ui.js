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

  const NR_ONLY_FIELD_IDS = [
    "freq-mhz", "tx-power-dbm", "noise-figure-db", "scs-khz", "num-rb", "bw-mhz",
    "electrical-tilt-deg", "mechanical-tilt-deg", "vertical-beamwidth-deg",
    "max-vertical-atten-db", "path-loss-model", "propagation-scenario",
    "max-horizontal-atten-db", "front-to-back-atten-db", "termination-rsrp-dbm",
    "mimo-mode", "link-adapt", "tx-antenna-gain-dbi",
  ];

  const NR_ONLY_REQUEST_KEYS = [
    "freq_mhz", "tx_power_dbm", "subcarrier_spacing_khz", "num_resource_blocks",
    "channel_bandwidth_mhz", "num_tx_antennas", "num_rx_antennas", "mimo_mode",
    "enable_link_adaptation", "fixed_modulation", "sectors", "tx_chain_gain_db",
    "tx_antenna_gain_dbi", "tx_feeder_loss_db", "reference_signal_offset_db",
    "max_rsrp_dbm", "electrical_tilt_deg", "mechanical_tilt_deg",
    "vertical_beamwidth_deg", "max_vertical_attenuation_db",
    "max_horizontal_attenuation_db", "front_to_back_attenuation_db",
    "azimuth_deg", "horizontal_beamwidth_deg", "path_loss_model",
    "propagation_scenario", "termination_rsrp_dbm",
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
    if (opts.lt != null && !(value < opts.lt)) throw new Error(`${label} must be less than ${opts.lt}.`);
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

  function syncPatternMode() {
    const mode = String(byId("dvt-pattern-type")?.value || "omnidirectional");
    setVisible(byId("dvt-parametric-pattern-fields"), mode === "parametric");
    setVisible(byId("dvt-tabulated-pattern-fields"), mode === "tabulated");
  }

  function channelAnalysisEnabled() {
    return !!byId("channel-analysis-enabled")?.checked;
  }

  function syncChannelAnalysisMode() {
    const enabled = channelAnalysisEnabled();
    setVisible(byId("channel-analysis-fields"), enabled);
    syncCoverageLayerOptions(isDvtProfile(getProfile()));
    const layer = byId("coverage-display-layer");
    const channelLayers = new Set([
      "incident_power", "bistatic_echo", "bistatic_return_loss", "bistatic_total_loss",
      "isac_quality", "bistatic_snr", "bistatic_margin", "bistatic_doppler",
      "bistatic_range", "bistatic_delay", "bistatic_angle", "bistatic_detectable",
    ]);
    if (enabled && layer && !channelLayers.has(String(layer.value || ""))) layer.value = "isac_quality";
  }

  function syncCoverageLayerOptions(dvt) {
    const layer = byId("coverage-display-layer");
    const channel = channelAnalysisEnabled();
    const optionState = (id, visible, label) => {
      const option = byId(id);
      if (!option) return;
      option.hidden = !visible;
      option.disabled = !visible;
      if (label) option.textContent = label;
    };

    optionState("coverage-layer-rsrp", !dvt, "RSRP");
    optionState("coverage-layer-sinr", !dvt, "SINR");
    optionState("coverage-layer-field-strength", dvt, "Field strength (dBµV/m)");
    optionState("coverage-layer-received-power", dvt, "Received carrier power (dBm)");
    optionState("coverage-layer-carrier-to-noise", dvt, "Carrier-to-noise C/N (dB)");
    optionState("coverage-layer-incident-power", channel, "Incident total-carrier power at target, 0 dBi (dBm)");
    optionState("coverage-layer-bistatic-echo", channel, "Bistatic echo power (dBm)");
    optionState("coverage-layer-bistatic-return-loss", channel, "Target→RX environmental path loss per candidate target (dB)");
    optionState("coverage-layer-bistatic-total-loss", channel, "Total TX→target→RX path loss per candidate target (dB)");
    optionState("coverage-layer-isac-quality", channel, "Expected ISAC quality at each candidate target (0–5)");
    optionState("coverage-layer-bistatic-snr", channel, "Post-processing echo SNR (dB)");
    optionState("coverage-layer-bistatic-margin", channel, "Detection margin (dB)");
    optionState("coverage-layer-bistatic-doppler", channel, "Signed bistatic Doppler (Hz)");
    optionState("coverage-layer-bistatic-range", channel, "Total TX→target→RX path length per candidate target (km)");
    optionState("coverage-layer-bistatic-delay", channel, "Bistatic excess delay (µs)");
    optionState("coverage-layer-bistatic-angle", channel, "Bistatic angle (deg)");
    optionState("coverage-layer-bistatic-detectable", channel, "Detectability mask (0/1)");

    if (!layer) return;
    const selected = layer.options?.[layer.selectedIndex];
    if (selected && selected.disabled) layer.value = dvt ? "received_power" : "rsrp";
    if (dvt && layer.value === "rsrp") layer.value = "received_power";
    if (!dvt && !String(layer.value || "").trim()) layer.value = "rsrp";
  }

  function syncUi(options) {
    const profile = getProfile();
    const dvt = isDvtProfile(profile);

    setVisible(byId("dvt-transmitter-fields"), dvt);
    setVisible(byId("advanced-rf-params"), !dvt);
    NR_ONLY_FIELD_IDS.forEach((id) => setFieldVisible(id, !dvt));
    setVisible(byId("sector-configuration-section"), !dvt);
    setVisible(byId("sector-overlay-control"), !dvt);

    const sectorToggle = byId("show-sectors-toggle");
    if (sectorToggle && dvt) sectorToggle.checked = false;

    const advancedSummary = byId("advanced-rf-summary");
    if (advancedSummary) advancedSummary.textContent = dvt ? "DVT propagation calibration" : "Advanced RF params";

    // DVT owns its coverage radius and radial spacing in the transmitter panel.
    // The mesh-profile controls are an NR/Google-mesh surface and must not be the
    // source of DVT coverage geometry.
    setVisible(byId("mesh-profile-params-section"), !dvt);

    const summary = byId("waveform-summary");
    if (summary) {
      summary.textContent = dvt
        ? `${PROFILE_LABELS[profile]} broadcast transmitter. One station antenna radiation pattern is active; cellular sectors are not used.`
        : "5G NR transmitter. NR resource-grid, link-adaptation and sector controls are active.";
    }

    syncCoverageLayerOptions(dvt);

    if (dvt) {
      const isDvbt = profile === "dvbt";
      setValueIfDefault("dvt-fc-mhz", ["3500", "3500.0", "587", "587.0"], "587");
      setValueIfDefault("dvt-bandwidth-mhz", ["40", "40.0", "6", "6.0", "8", "8.0"], isDvbt ? "8" : "6");
      setValueIfDefault("dvt-fs-mhz", ["", "40", "40.0"], "10");
      setValueIfDefault("tx-height-m", ["10", "10.0"], "320");
      setValueIfDefault("dvt-coverage-radius-m", ["2500", "2500.0", "2000", "2000.0", ""], "20000");
      setValueIfDefault("dvt-dr-m", ["5", "5.0", ""], "20");
      setValueIfDefault("dvt-dtheta-deg", ["5", "5.0"], "0.25");
      setValueIfDefault("coverage-display-layer", ["rsrp"], "received_power");
      const standard = byId("dvt-station-standard");
      if (standard && !String(standard.value || "").trim()) standard.value = PROFILE_LABELS[profile];
    }

    syncPowerMode();
    syncPatternMode();
    setVisible(byId("channel-analysis-fields"), channelAnalysisEnabled());
    syncCoverageLayerOptions(dvt);
    if (typeof options?.onChange === "function") options.onChange({ profile, technology: dvt ? "dvt" : "5g_nr" });
  }

  function parseAzimuthPattern() {
    const raw = String(byId("dvt-azimuth-pattern")?.value || "").trim();
    if (!raw) throw new Error("A tabulated broadcast pattern requires azimuth samples.");
    const points = [];
    const seen = new Set();
    raw.split(/\r?\n/).forEach((line, index) => {
      const clean = line.split("#", 1)[0].trim();
      if (!clean) return;
      const columns = clean.split(/[\s,;]+/).filter(Boolean).map(Number);
      if (columns.length !== 2 && columns.length !== 3) {
        throw new Error(`Broadcast pattern line ${index + 1} must have 2 or 3 numeric columns.`);
      }
      if (columns.some((value) => !Number.isFinite(value))) {
        throw new Error(`Broadcast pattern line ${index + 1} contains a non-numeric value.`);
      }
      const azimuth = columns[0];
      if (!(azimuth >= 0 && azimuth < 360)) throw new Error(`Broadcast pattern azimuth on line ${index + 1} must be in [0, 360).`);
      if (seen.has(azimuth)) throw new Error(`Broadcast pattern azimuth ${azimuth} is duplicated.`);
      seen.add(azimuth);
      if (columns[1] < 0 || columns[1] > 1 || (columns.length === 3 && (columns[2] < 0 || columns[2] > 1))) {
        throw new Error(`Broadcast pattern relative-field values on line ${index + 1} must be between 0 and 1.`);
      }
      if (columns.length === 2) {
        points.push({ azimuthDeg: azimuth, relativeField: columns[1] });
      } else {
        points.push({ azimuthDeg: azimuth, relativeFieldH: columns[1], relativeFieldV: columns[2] });
      }
    });
    if (points.length < 2) throw new Error("A tabulated broadcast pattern requires at least two unique azimuth samples.");
    return points;
  }

  function buildBroadcastAntenna() {
    const patternType = String(byId("dvt-pattern-type")?.value || "omnidirectional");
    const antenna = {
      patternType,
      rotationDeg: requiredNumber("dvt-pattern-rotation-deg", "Broadcast pattern rotation", { ge: 0, lt: 360 }),
      beamTiltDeg: requiredNumber("dvt-beam-tilt-deg", "Broadcast beam tilt", { ge: -30, le: 30 }),
      verticalBeamwidthDeg: requiredNumber("dvt-vertical-beamwidth-deg", "Broadcast vertical HPBW", { gt: 0, le: 180 }),
      maxVerticalAttenuationDb: requiredNumber("dvt-max-vertical-atten-db", "Broadcast maximum vertical attenuation", { ge: 0, le: 80 }),
    };
    const manufacturer = optionalText("dvt-antenna-manufacturer");
    const model = optionalText("dvt-antenna-model");
    if (manufacturer) antenna.manufacturer = manufacturer;
    if (model) antenna.model = model;

    if (patternType === "parametric") {
      antenna.mainAzimuthDeg = requiredNumber("dvt-main-azimuth-deg", "Broadcast main-lobe azimuth", { ge: 0, lt: 360 });
      antenna.horizontalBeamwidthDeg = requiredNumber("dvt-horizontal-beamwidth-deg", "Broadcast horizontal HPBW", { gt: 0, le: 360 });
      antenna.maxHorizontalAttenuationDb = requiredNumber("dvt-max-horizontal-atten-db", "Broadcast maximum horizontal attenuation", { ge: 0, le: 80 });
      antenna.frontToBackAttenuationDb = requiredNumber("dvt-front-to-back-atten-db", "Broadcast front-to-back attenuation", { ge: 0, le: 80 });
    } else if (patternType === "tabulated") {
      antenna.azimuthPattern = parseAzimuthPattern();
    }
    return antenna;
  }

  function parseLatLonField(id, label) {
    const raw = String(byId(id)?.value || "").trim();
    const parts = raw.split(/[\s,]+/).filter(Boolean);
    if (parts.length !== 2) throw new Error(`${label} must be entered as lat,lon.`);
    const latitude = Number(parts[0]);
    const longitude = Number(parts[1]);
    if (!Number.isFinite(latitude) || latitude < -90 || latitude > 90) throw new Error(`${label} latitude must be in [-90, 90].`);
    if (!Number.isFinite(longitude) || longitude < -180 || longitude > 180) throw new Error(`${label} longitude must be in [-180, 180].`);
    return { latitude, longitude };
  }

  function buildChannelAnalysis() {
    if (!channelAnalysisEnabled()) return null;
    const processingBandwidthMhz = requiredNumber(
      "channel-processing-bandwidth-mhz",
      "Processing bandwidth",
      { ge: 0 },
    );
    const prfHz = requiredNumber("channel-prf-hz", "Doppler sampling PRF", { ge: 0 });
    const receiverLocation = parseLatLonField("channel-rx-location", "Analysis RX location");
    const config = {
      receiver: {
        latitude: receiverLocation.latitude,
        longitude: receiverLocation.longitude,
        altitudeMamsl: requiredNumber("channel-rx-altitude-m", "Analysis receiver site altitude"),
        antennaHeightMagl: requiredNumber("channel-rx-height-m", "Analysis receiver antenna height", { ge: 0 }),
        directAntennaGainDbi: requiredNumber("channel-direct-gain-dbi", "Direct/reference antenna gain"),
        echoAntennaGainDbi: requiredNumber("channel-echo-gain-dbi", "Echo/surveillance antenna gain"),
        feederLossDb: requiredNumber("channel-rx-feeder-loss-db", "Analysis receiver feeder loss", { ge: 0 }),
        noiseFigureDb: requiredNumber("channel-rx-noise-figure-db", "Analysis receiver noise figure", { ge: 0 }),
        directPathExcessLossDb: requiredNumber("channel-direct-excess-loss-db", "Direct-path excess loss", { ge: 0 }),
        returnPathExcessLossDb: requiredNumber("channel-return-excess-loss-db", "Target-to-receiver excess loss", { ge: 0 }),
      },
      target: {
        heightMagl: requiredNumber("channel-target-height-m", "Target height", { ge: 0 }),
        bistaticRcsM2: requiredNumber("channel-bistatic-rcs-m2", "Bistatic RCS", { gt: 0 }),
      },
      motion: {
        speedMps: requiredNumber("channel-target-speed-mps", "Target speed", { ge: 0 }),
        headingDegTrue: requiredNumber("channel-target-heading-deg", "Target heading", { ge: 0, lt: 360 }),
        climbRateMps: requiredNumber("channel-target-climb-mps", "Target climb rate"),
      },
      processing: {
        coherentIntegrationS: requiredNumber("channel-integration-s", "Coherent integration time", { gt: 0 }),
        processingLossDb: requiredNumber("channel-processing-loss-db", "Processing loss", { ge: 0 }),
        systemLossDb: requiredNumber("channel-system-loss-db", "System loss", { ge: 0 }),
        requiredSnrDb: requiredNumber("channel-required-snr-db", "Required post-processing SNR"),
        directPathCancellationDb: requiredNumber("channel-direct-cancellation-db", "Direct-path cancellation", { ge: 0 }),
        clutterNotchHz: requiredNumber("channel-clutter-notch-hz", "Clutter notch", { ge: 0 }),
        minimumDetectableDopplerHz: requiredNumber("channel-min-doppler-hz", "Minimum detectable Doppler", { ge: 0 }),
      },
      returnPathModel: String(byId("channel-return-path-model")?.value || "environmental_reciprocal_grid"),
    };
    if (processingBandwidthMhz > 0) config.processing.processingBandwidthHz = processingBandwidthMhz * 1e6;
    if (prfHz > 0) config.processing.pulseRepetitionFrequencyHz = prfHz;
    return config;
  }

  function buildPlanFields(args) {
    const profile = getProfile();
    const channelAnalysis = buildChannelAnalysis();
    if (!isDvtProfile(profile)) {
      const nr = { technology: "5g_nr", waveform: "5g_nr" };
      if (channelAnalysis) nr.channel_analysis = channelAnalysis;
      return nr;
    }

    const lat = Number(args?.lat);
    const lon = Number(args?.lon);
    const txHeightM = Number(args?.txHeightM);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) throw new Error("DVT transmitter latitude/longitude are required.");
    if (!Number.isFinite(txHeightM) || txHeightM < 0) throw new Error("DVT antenna height must be zero or greater.");

    const fcMhz = requiredNumber("dvt-fc-mhz", "DVT center frequency", { ge: 40, le: 1000 });
    const fsMhz = requiredNumber("dvt-fs-mhz", "DVT sample rate", { gt: 0 });
    const bandwidthMhz = requiredNumber("dvt-bandwidth-mhz", "DVT channel bandwidth", { ge: 1, le: 10 });
    if (fsMhz < bandwidthMhz) throw new Error("DVT sample rate must be greater than or equal to channel bandwidth.");

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
      const erpH = requiredNumber("dvt-erp-h-kw", "Maximum horizontal ERP", { ge: 0 });
      const erpV = requiredNumber("dvt-erp-v-kw", "Maximum vertical ERP", { ge: 0 });
      if (!(erpH > 0 || erpV > 0)) throw new Error("At least one of horizontal or vertical ERP must be greater than zero.");
      power = { polarization: String(byId("dvt-polarization")?.value || "") };
      if (erpH > 0) power.erpHKw = erpH;
      if (erpV > 0) power.erpVKw = erpV;
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

    const radiusFieldId = byId("dvt-coverage-radius-m") ? "dvt-coverage-radius-m" : "max-range";
    const radialStepFieldId = byId("dvt-dr-m") ? "dvt-dr-m" : "dr-m";
    const maxRangeM = byId(radiusFieldId)
      ? requiredNumber(radiusFieldId, "DVT coverage radius", { gt: 0 })
      : 20000;
    const stepM = byId(radialStepFieldId)
      ? requiredNumber(radialStepFieldId, "DVT radial step", { gt: 0 })
      : 20;
    const dthetaDeg = requiredNumber("dvt-dtheta-deg", "Bearing step", { gt: 0, le: 360 });

    const result = {
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
        },
        antenna: buildBroadcastAntenna(),
        power,
        station,
      },
      noise_figure_db: requiredNumber("dvt-noise-figure-db", "Receiver noise figure"),
      ue_antenna_gain_dbi: requiredNumber("dvt-rx-antenna-gain-dbi", "Receiver antenna gain"),
      termination_power_dbm: requiredNumber("dvt-termination-power-dbm", "Ray termination power"),
      max_range_m: maxRangeM,
      step_m: stepM,
      dtheta_deg: dthetaDeg,
      coverage_display_layer: String(byId("coverage-display-layer")?.value || "received_power"),
      compact_output: true,
    };
    if (channelAnalysis) result.channel_analysis = channelAnalysis;
    return result;
  }

  function sanitizePlanBody(body) {
    if (!body || typeof body !== "object" || !isDvtProfile(getProfile())) return body;
    NR_ONLY_REQUEST_KEYS.forEach((key) => delete body[key]);
    return body;
  }

  function init(options) {
    byId("waveform-profile")?.addEventListener("change", () => syncUi(options));
    byId("dvt-power-mode")?.addEventListener("change", syncPowerMode);
    byId("dvt-pattern-type")?.addEventListener("change", syncPatternMode);
    byId("channel-analysis-enabled")?.addEventListener("change", syncChannelAnalysisMode);
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
