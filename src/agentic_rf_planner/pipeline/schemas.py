"""Core data models for the RF planning pipeline."""

from enum import Enum
from typing import List, Optional, Dict, Any

from pydantic import BaseModel, model_validator

from ..rf.dvt import DVTTransmitter


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

    technology: str = "5g_nr"  # 5g_nr | dvt
    waveform: Optional[str] = None  # 5g_nr | atsc1 | atsc3 | dvbt | baseline
    dvt: Optional[DVTTransmitter] = None

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
        self.waveform = str(self.dvt.waveform)
        self.freq_mhz = self.dvt.frequency_mhz
        self.channel_bandwidth_mhz = self.dvt.bandwidth_mhz
        # Project the physical transmitter chain into shared fields. For ERP
        # input these values are zero by validation; for conducted power they
        # are applied exactly once by the DVT source-power calculation.
        self.tx_power_dbm = self.dvt.power.input_power_dbm
        self.tx_chain_gain_db = self.dvt.power.tx_gain_db
        self.tx_antenna_gain_dbi = self.dvt.power.antenna_gain_dbi
        self.tx_feeder_loss_db = self.dvt.power.feeder_loss_db
        self.reference_signal_offset_db = 0.0
        self.tx_height_m = self.dvt.tx.antenna_height
        self.site_altitude_m = self.dvt.tx.altitude
        self.azimuth_deg = self.dvt.tx.azimuth_deg
        self.horizontal_beamwidth_deg = self.dvt.tx.beamwidth_h_deg
        self.vertical_beamwidth_deg = self.dvt.tx.beamwidth_v_deg
        self.electrical_tilt_deg = self.dvt.tx.effective_down_tilt_deg
        self.mechanical_tilt_deg = 0.0
        self.max_horizontal_attenuation_db = self.dvt.tx.max_horizontal_attenuation_db
        self.front_to_back_attenuation_db = self.dvt.tx.front_to_back_attenuation_db
        self.max_vertical_attenuation_db = self.dvt.tx.max_vertical_attenuation_db
        self.coverage_display_layer = (
            "field_strength" if self.coverage_display_layer == "rsrp" else self.coverage_display_layer
        )
        return self


class WorldCell(BaseModel):
    """
    One cell in the 2D coverage grid around TX.
    We stay in 2.5D: no full 3D ray-tracing.
    """

    lat: float
    lon: float
    distance_m: float  # Straight-line distance from TX to cell
    bearing_deg: float

    # aggregated info along LOS from TX to cell
    dominant_material: MaterialType
    obstacles_count: int  # e.g. number of building "faces" crossed
    extra_loss_db: float  # precomputed extra attenuation vs free space

    # Sector/sample identity.
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
    # When set, overrides `RFParams.tx_antenna_gain_dbi` for this sector/candidate; None = use global default.
    sector_tx_antenna_gain_dbi: Optional[float] = None
    # NR/LTE physical cell id; None = not specified (UIs should not show a placeholder; 0 is valid when set).
    sector_pci: Optional[int] = None

    # LOS / obstruction-state model.
    is_los: bool = True
    actual_path_length_m: float = 0.0
    num_buildings: int = 0
    num_trees: int = 0
    blocking_state: str = "los"  # los | penetration | shadow
    propagation_mode: str = "los"  # los | penetration | shadow | nlos_recovery
    first_blocker_distance_m: Optional[float] = None
    diffraction_flag: bool = False

    # Split-loss model:
    # - penetration_loss_db applies only while the current ray segment is actually inside
    #   a blocker or foliage interval.
    # - shadow_loss_db represents behind-blocker attenuation after LOS has been lost.
    # - diffraction_loss_db and canyon_recovery_db are continuation terms used once LOS
    #   is gone, instead of stacking every prior wall forever.
    penetration_loss_db: float = 0.0
    shadow_loss_db: float = 0.0
    diffraction_loss_db: float = 0.0
    canyon_recovery_db: float = 0.0
    metal_blocked: bool = False
    buildings_along_path: List[Dict[str, Any]] = []
    # True when coverage_grid already evaluated all map intersections and loss states.
    # world_builder uses this to avoid repeating OSM queries for every output cell.
    coverage_precomputed: bool = False

    # Optional precomputed RSRP for this sector candidate.
    # Used by multipath ray tracing mode to avoid re-deriving RSRP from only
    # "extra loss" scalars.
    precomputed_rsrp_dbm: Optional[float] = None

    # Terrain-aware sample metadata
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
    waveform: Optional[str] = None
    terrain_loss_db: Optional[List[float]] = None
    los_terrain: Optional[List[bool]] = None
    terrain_state: Optional[List[str]] = None
    z_ground_m: Optional[List[float]] = None


