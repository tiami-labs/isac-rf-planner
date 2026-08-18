"""Core data models for the RF planning pipeline."""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Dict, Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from ..rf.dvt import DVTTransmitter
from ..rf.channel_analysis import ChannelAnalysisConfig


class LatLon(BaseModel):
    """Geographic coordinates."""

    lat: float
    lon: float


class MaterialType(str, Enum):
    """Material types detectable by VLM."""

    BUILDING = "building"
    HOUSE = "house"
    LARGE_STRUCTURE = "large_structure"
    TREES = "trees"
    UNKNOWN = "unknown"


class DistanceBand(str, Enum):
    """Coarse distance estimation."""

    NEAR = "near"  # e.g. < 30 m
    MID = "mid"  # e.g. 30–100 m
    FAR = "far"  # e.g. > 100 m


class MaterialSegment(BaseModel):
    """
    Material as seen in one pano tile (view).
    Minimal and easy to infer from VLM.
    """

    tile_id: int  # which tile (0..N-1)
    material: MaterialType
    # angular extent in pano coordinates
    yaw_deg_start: float  # [0, 360)
    yaw_deg_end: float  # [0, 360)
    pitch_deg_start: float  # vertical; can be coarse
    pitch_deg_end: float

    # coarse distance guess; you can derive this from map later if needed
    distance_band: DistanceBand = DistanceBand.MID
    confidence: float = 0.7  # VLM's own confidence, rough


class ViewTileDescription(BaseModel):
    """Description of one pano tile with detected materials."""

    tile_id: int
    # orientation of this tile w.r.t geographic north (deg)
    yaw_center_deg: float
    hfov_deg: float  # horiz FOV of tile
    vfov_deg: float  # vertical FOV
    materials: List[MaterialSegment]


class RFParams(BaseModel):
    """RF simulation parameters for 5G NR and DVT transmitters."""

    model_config = ConfigDict(populate_by_name=True)

    technology: str = "5g_nr"  # 5g_nr | dvt
    waveform: Optional[str] = None  # 5g_nr | atsc1 | atsc3 | dvbt | baseline
    dvt: Optional[DVTTransmitter] = None
    channel_analysis: Optional[ChannelAnalysisConfig] = Field(
        default=None,
        validation_alias=AliasChoices("channel_analysis", "passive_radar"),
        serialization_alias="channel_analysis",
    )

    freq_mhz: float
    tx_power_dbm: float
    # Noise floor: calculated from bandwidth + noise figure (if not explicitly set)
    # Formula: N = -174 + 10*log10(B_Hz) + NF_dB
    # Default: -100 dBm (equivalent to ~20 MHz, NF=7 dB)
    noise_floor_dbm: Optional[float] = None  # If None, calculated from bandwidth + NF
    noise_figure_db: float = 7.0  # Receiver noise figure (typical: 5-10 dB)
    max_range_m: float = 2500.0
    step_m: float = 5.0  # grid resolution
    dtheta_deg: float = 5.0  # bearing step (deg); must match 3D mesh-profile discretization
    
    # OFDM parameters
    subcarrier_spacing_khz: float = 15.0
    num_resource_blocks: int = 100
    channel_bandwidth_mhz: float = 20.0
    
    # MIMO parameters
    num_tx_antennas: int = 1
    num_rx_antennas: int = 1
    mimo_mode: str = "SISO"  # SISO, SIMO, MISO, MIMO
    
    # Link adaptation
    enable_link_adaptation: bool = True  # Auto-select modulation based on SINR
    fixed_modulation: Optional[str] = None  # If set, use this modulation (overrides adaptation)
    
    # Sector configuration (optional - if None, uses omnidirectional)
    sectors: Optional[List[Dict[str, Any]]] = None  # List of sector configs (will be converted to SectorConfig)

    # Ray propagation configuration (2D vs 3D). RF math remains identical.
    # "2d": polygon-based OSM intersections (existing)
    # "3d": mesh-profile-based intersections (persisted Google 3D mesh + OSM semantics)
    ray_mode: str = "2d"

    # Heights above ground (meters), used only by 3D ray geometry.
    # Default aligns with /3d planner (tx-height-m) and PlanRequest.
    tx_height_m: float = 10.0
    rx_height_m: float = 1.5

    # Macro-cell power model:
    # - tx_power_dbm is total carrier power, not per-reference-signal power.
    # - RSRP is derived from a reference-signal-equivalent source term using
    #   EPRE-style spreading plus antenna/feed assumptions and a conservative
    #   reference-signal offset tuned for typical mid-band macro deployments.
    tx_chain_gain_db: float = 0.0
    tx_antenna_gain_dbi: float = 17.0
    tx_feeder_loss_db: float = 2.0
    reference_signal_offset_db: float = -18.0
    ue_antenna_gain_dbi: float = 0.0
    max_rsrp_dbm: float = -62.0
    electrical_tilt_deg: float = 0.0
    mechanical_tilt_deg: float = 0.0
    vertical_beamwidth_deg: float = 8.0
    max_vertical_attenuation_db: float = 30.0
    max_horizontal_attenuation_db: float = 30.0
    front_to_back_attenuation_db: float = 25.0
    azimuth_deg: float = 0.0
    horizontal_beamwidth_deg: float = 360.0
    # Optional site elevation override. For DVT this is populated from tx.altitude.
    site_altitude_m: Optional[float] = None

    # Scenario / calibration controls for the vendor-grade roadmap.
    # We start with deterministic median path loss and deterministic shadow/recovery
    # terms, then expose the knobs in config so calibration can happen without code edits.
    path_loss_model: str = "3gpp_38901"
    propagation_scenario: str = "umi_street_canyon"
    shadow_loss_db: float = 6.0
    shadow_decay_db_per_100m: float = 4.0
    shadow_loss_cap_db: float = 22.0
    diffraction_base_loss_db: float = 6.0
    diffraction_slope_db_per_100m: float = 3.0
    diffraction_loss_cap_db: float = 18.0
    canyon_recovery_max_db: float = 8.0
    canyon_recovery_slope_db_per_100m: float = 6.0
    termination_rsrp_dbm: float = -140.0

    # Building attenuation: overall + per-material (config-driven; no code changes needed).
    # Dict with "overall": {scale, reduction_db} and optional "materials": {concrete: {...}, ...}
    building_attenuation: Optional[Dict[str, Any]] = None

    # --- 3D multipath ray tracing controls (ray_mode = "3d_rt") ---
    # These settings are intentionally conservative defaults so the mode runs
    # without additional configuration.
    rt_max_bounces: int = 1  # currently only 1-bounce is implemented
    rt_max_reflections_per_sample: int = 2  # top-N reflections to combine
    rt_max_wall_candidates: int = 40  # nearest wall segments to consider per sample
    rt_reflection_loss_db: float = 8.0  # base reflection loss (dB), material adds on top
    rt_debug_sample_stride: int = 25  # debug rendering: take every N-th range sample

    # Terrain / environment (Phase 1 terrain core)
    terrain_enabled: bool = True
    dem_source: str = "opentopodata"  # auto | google | opentopodata | local_raster | flat
    terrain_resolution_m: Optional[float] = None
    earth_curvature_k: float = 4.0 / 3.0
    fresnel_min_clearance: float = 0.6
    terrain_clutter_height_m: float = 0.0
    terrain_loss_cap_db: float = 40.0
    buildings_on_terrain: bool = True
    landcover_clutter_enabled: bool = True
    coverage_display_layer: str = "rsrp"  # rsrp | sinr | terrain_shadow | field_strength
    compact_output: Optional[bool] = None  # None=auto for large DVT/3D results

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_channel_analysis(cls, values: Any) -> Any:
        """Accept the previous passive_radar object shape as an input alias only."""
        if not isinstance(values, dict):
            return values
        raw = values.get("channel_analysis", values.get("passive_radar"))
        if raw is None or isinstance(raw, ChannelAnalysisConfig):
            return values
        if hasattr(raw, "model_dump"):
            raw = raw.model_dump(by_alias=True)
        normalized = dict(values)
        normalized.pop("passive_radar", None)
        normalized["channel_analysis"] = raw
        return normalized

    @model_validator(mode="after")
    def apply_dvt_transmitter(self) -> "RFParams":
        """Project the typed DVT transmitter into the shared propagation fields."""
        if self.dvt is None and str(self.technology).strip().lower() != "dvt":
            profile = str(self.waveform or "5g_nr").strip().lower()
            if profile != "5g_nr":
                raise ValueError("non-5G waveform requires technology='dvt' and a dvt transmitter object")
            self.technology = "5g_nr"
            self.waveform = "5g_nr"
            return self
        if self.dvt is None:
            raise ValueError("technology='dvt' requires a dvt transmitter object")

        self.technology = "dvt"
        requested_waveform = str(self.waveform or self.dvt.waveform).strip().lower()
        if requested_waveform not in ("dvt", str(self.dvt.waveform)):
            raise ValueError("waveform must match dvt.waveform")
        if self.sectors:
            raise ValueError("DVT uses one broadcast antenna radiation pattern, not cellular sectors")
        self.sectors = None
        self.waveform = str(self.dvt.waveform)
        self.freq_mhz = self.dvt.frequency_mhz
        self.channel_bandwidth_mhz = self.dvt.bandwidth_mhz
        # Shared scalar fields carry the physical RF chain and site geometry.
        # Broadcast directionality remains exclusively in dvt.antenna; it is
        # not projected into cellular azimuth/beamwidth/sector fields.
        self.tx_power_dbm = self.dvt.power.input_power_dbm
        self.tx_chain_gain_db = self.dvt.power.tx_gain_db
        self.tx_antenna_gain_dbi = self.dvt.power.antenna_gain_dbi
        self.tx_feeder_loss_db = self.dvt.power.feeder_loss_db
        self.reference_signal_offset_db = 0.0
        self.tx_height_m = self.dvt.tx.antenna_height
        self.site_altitude_m = self.dvt.tx.altitude
        if self.channel_analysis is not None:
            # Coverage cells become candidate target locations for any waveform.
            self.rx_height_m = float(self.channel_analysis.target.height_m_agl)
        self.coverage_display_layer = (
            "field_strength" if self.coverage_display_layer == "rsrp" else self.coverage_display_layer
        )
        return self

    def public_config(self) -> Dict[str, Any]:
        """Return a technology-specific configuration payload for API/UI output."""

        if str(self.technology).strip().lower() != "dvt" or self.dvt is None:
            return self.model_dump()

        return {
            "technology": "dvt",
            "waveform": str(self.dvt.waveform),
            "dvt": self.dvt.model_dump(by_alias=True),
            "channel_analysis": (
                self.channel_analysis.model_dump(by_alias=True)
                if self.channel_analysis is not None
                else None
            ),
            "center_frequency_mhz": self.dvt.frequency_mhz,
            "sample_rate_mhz": self.dvt.fs / 1.0e6,
            "channel_bandwidth_mhz": self.dvt.bandwidth_mhz,
            "source_eirp_dbm": self.dvt.power.source_eirp_dbm,
            "noise_floor_dbm": self.noise_floor_dbm,
            "noise_figure_db": self.noise_figure_db,
            "receiver_antenna_gain_dbi": self.ue_antenna_gain_dbi,
            "receiver_height_m": self.rx_height_m,
            "termination_power_dbm": self.termination_rsrp_dbm,
            "max_range_m": self.max_range_m,
            "step_m": self.step_m,
            "dtheta_deg": self.dtheta_deg,
            "ray_mode": self.ray_mode,
            "shadow_loss_db": self.shadow_loss_db,
            "shadow_decay_db_per_100m": self.shadow_decay_db_per_100m,
            "shadow_loss_cap_db": self.shadow_loss_cap_db,
            "diffraction_base_loss_db": self.diffraction_base_loss_db,
            "diffraction_slope_db_per_100m": self.diffraction_slope_db_per_100m,
            "diffraction_loss_cap_db": self.diffraction_loss_cap_db,
            "canyon_recovery_max_db": self.canyon_recovery_max_db,
            "canyon_recovery_slope_db_per_100m": self.canyon_recovery_slope_db_per_100m,
            "building_attenuation": self.building_attenuation,
            "terrain_enabled": self.terrain_enabled,
            "dem_source": self.dem_source,
            "terrain_resolution_m": self.terrain_resolution_m,
            "earth_curvature_k": self.earth_curvature_k,
            "fresnel_min_clearance": self.fresnel_min_clearance,
            "terrain_clutter_height_m": self.terrain_clutter_height_m,
            "terrain_loss_cap_db": self.terrain_loss_cap_db,
            "buildings_on_terrain": self.buildings_on_terrain,
            "landcover_clutter_enabled": self.landcover_clutter_enabled,
            "coverage_display_layer": self.coverage_display_layer,
            "compact_output": self.compact_output,
        }


@dataclass(slots=True)
class WorldCell:
    """One internal 2.5D propagation sample.

    This is deliberately a slotted dataclass rather than a Pydantic model. Coverage
    generation can create millions of cells; a per-instance ``__dict__`` with forty+
    keys dominates memory while providing no value because these objects are never
    part of the public API. Validation remains at RF/request boundaries.
    """

    lat: float
    lon: float
    distance_m: float
    bearing_deg: float

    dominant_material: MaterialType
    obstacles_count: int
    extra_loss_db: float

    sector_id: str = "omnidirectional"
    sector_freq_mhz: Optional[float] = None
    sector_tx_power_dbm: Optional[float] = None
    sector_channel_bandwidth_mhz: Optional[float] = None
    sector_azimuth_deg: Optional[float] = None
    sector_beamwidth_h_deg: Optional[float] = None
    sector_beamwidth_v_deg: Optional[float] = None
    sector_electrical_tilt_deg: Optional[float] = None
    sector_mechanical_tilt_deg: Optional[float] = None
    sector_max_horizontal_attenuation_db: Optional[float] = None
    sector_front_to_back_attenuation_db: Optional[float] = None
    sector_max_vertical_attenuation_db: Optional[float] = None
    sector_tx_antenna_gain_dbi: Optional[float] = None
    sector_pci: Optional[int] = None

    is_los: bool = True
    actual_path_length_m: float = 0.0
    num_buildings: int = 0
    num_trees: int = 0
    blocking_state: str = "los"
    propagation_mode: str = "los"
    first_blocker_distance_m: Optional[float] = None
    diffraction_flag: bool = False

    penetration_loss_db: float = 0.0
    shadow_loss_db: float = 0.0
    diffraction_loss_db: float = 0.0
    canyon_recovery_db: float = 0.0
    metal_blocked: bool = False
    # None means "not retained".  Allocating an empty list in every large-plan
    # cell costs ~56 bytes/sample even when building metadata is intentionally
    # disabled (DVT/3D compact modes).
    buildings_along_path: Optional[List[Dict[str, Any]]] = None
    coverage_precomputed: bool = False

    precomputed_rsrp_dbm: Optional[float] = None

    z_ground_m: float = 0.0
    z_rx_abs_m: Optional[float] = None
    terrain_loss_db: float = 0.0
    los_terrain: bool = True
    fresnel_clearance: Optional[float] = None
    terrain_state: str = "los"


class WorldModel(BaseModel):
    """Complete world model for RF simulation."""

    tx: LatLon
    rf_params: RFParams
    cells: List[WorldCell]
    z_tx_ground_m: Optional[float] = None
    z_tx_abs_m: Optional[float] = None


class AttenuationGrid(BaseModel):
    """
    Final RF result. You can map this directly to a heatmap.

    `rsrp_dbm` is the best-server (max) RSRP per map point for SINR/interference math.
    `rsrp_by_sector` (when present) repeats the same point order as `cell_lat`/`cell_lon`
    with that sector's RSRP only—use for per-sector heatmaps (true beam shape, not max-composite).
    """

    tx: LatLon
    rf_params: RFParams
    # parallel to WorldModel.cells
    cell_lat: List[float]
    cell_lon: List[float]
    rsrp_dbm: List[float]  # Received Signal Received Power (dBm)
    sinr_db: List[float]  # Signal-to-Interference-plus-Noise Ratio (dB)
    modulation: List[str]  # Selected modulation scheme per cell
    throughput_mbps: List[float]  # Estimated throughput (Mbps) per cell
    serving_sector_id: List[str]
    interferer_count: List[int]
    top_interferer_rsrp_dbm: List[float]
    pilot_pollution_metric_db: List[float]
    rsrp_by_sector: Optional[Dict[str, List[float]]] = None
    technology: str = "5g_nr"
    received_power_dbm: Optional[List[float]] = None
    field_strength_dbuv_m: Optional[List[float]] = None
    carrier_to_noise_db: Optional[List[float]] = None
    # Waveform-agnostic channel-analysis arrays.  All arrays follow cell_lat/cell_lon order.
    incident_power_isotropic_dbm: Optional[List[float]] = None
    isac_echo_geometry_base_dbm: Optional[List[float]] = None
    bistatic_doppler_east_hz_per_mps: Optional[List[float]] = None
    bistatic_doppler_north_hz_per_mps: Optional[List[float]] = None
    bistatic_doppler_up_hz_per_mps: Optional[List[float]] = None
    bistatic_doppler_sensitivity_hz_per_mps: Optional[List[float]] = None
    bistatic_motion_doppler_sensitivity_hz_per_mps: Optional[List[float]] = None
    bistatic_minimum_detectable_speed_mps: Optional[List[float]] = None
    bistatic_echo_power_dbm: Optional[List[float]] = None
    bistatic_preprocessing_snr_db: Optional[List[float]] = None
    bistatic_postprocessing_snr_db: Optional[List[float]] = None
    bistatic_detection_margin_db: Optional[List[float]] = None
    bistatic_echo_to_residual_direct_db: Optional[List[float]] = None
    bistatic_direct_residual_margin_db: Optional[List[float]] = None
    bistatic_required_cancellation_db: Optional[List[float]] = None
    bistatic_required_dynamic_range_db: Optional[List[float]] = None
    bistatic_dynamic_range_margin_db: Optional[List[float]] = None
    bistatic_minimum_detectable_rcs_m2: Optional[List[float]] = None
    bistatic_rcs_margin_db: Optional[List[float]] = None
    bistatic_tx_target_range_m: Optional[List[float]] = None
    bistatic_target_receiver_range_m: Optional[List[float]] = None
    bistatic_path_range_m: Optional[List[float]] = None
    bistatic_excess_path_range_m: Optional[List[float]] = None
    bistatic_excess_delay_s: Optional[List[float]] = None
    bistatic_angle_deg: Optional[List[float]] = None
    bistatic_path_range_rate_mps: Optional[List[float]] = None
    bistatic_closing_speed_mps: Optional[List[float]] = None
    bistatic_doppler_hz: Optional[List[float]] = None
    bistatic_snr_noise_interference_ok: Optional[List[bool]] = None
    bistatic_doppler_resolved: Optional[List[bool]] = None
    bistatic_doppler_ambiguous: Optional[List[bool]] = None
    bistatic_direct_residual_ok: Optional[List[bool]] = None
    bistatic_dynamic_range_ok: Optional[List[bool]] = None
    bistatic_detectable_screening: Optional[List[bool]] = None
    bistatic_detectable_qualified: Optional[List[bool]] = None
    bistatic_detectable: Optional[List[bool]] = None
    bistatic_constraint_failure_code: Optional[List[int]] = None
    # Environment-aware reciprocal target -> analysis-RX propagation, aligned to target grid.
    return_environment_valid: Optional[List[bool]] = None
    return_path_loss_db: Optional[List[float]] = None
    return_environment_loss_db: Optional[List[float]] = None
    return_terrain_loss_db: Optional[List[float]] = None
    return_los: Optional[List[bool]] = None
    return_terrain_state: Optional[List[str]] = None
    return_sample_error_m: Optional[List[float]] = None
    channel_analysis_summary: Optional[Dict[str, Any]] = None
    waveform: Optional[str] = None
    terrain_loss_db: Optional[List[float]] = None
    los_terrain: Optional[List[bool]] = None
    terrain_state: Optional[List[str]] = None
    z_ground_m: Optional[List[float]] = None

    # Large ISAC plans keep derived channel layers in compact NumPy arrays instead
    # of materializing dozens of Python-float lists.  Private attrs are intentionally
    # excluded from Pydantic serialization; the API/product layer explicitly reads
    # them through ``channel_array``.  Small plans still populate the public list
    # fields above for backwards compatibility and tests.
    _channel_arrays: Dict[str, Any] = PrivateAttr(default_factory=dict)

    def set_channel_array(self, name: str, values: Any) -> None:
        self._channel_arrays[str(name)] = values

    def channel_array(self, name: str) -> Any:
        """Return an internal array when present, otherwise the public field.

        This keeps numerical layers array-backed during planning while preserving
        the existing AttenuationGrid wire/schema surface for smaller responses.
        """

        if name in self._channel_arrays:
            return self._channel_arrays[name]
        return getattr(self, name, None)

    def clear_channel_arrays(self) -> None:
        self._channel_arrays.clear()


