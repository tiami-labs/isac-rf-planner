"""Core data models for the RF planning pipeline."""

from enum import Enum
from typing import List, Optional, Dict, Any

from pydantic import BaseModel


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
    """RF simulation parameters."""

    freq_mhz: float
    tx_power_dbm: float
    # Noise floor: calculated from bandwidth + noise figure (if not explicitly set)
    # Formula: N = -174 + 10*log10(B_Hz) + NF_dB
    # Default: -100 dBm (equivalent to ~20 MHz, NF=7 dB)
    noise_floor_dbm: Optional[float] = None  # If None, calculated from bandwidth + NF
    noise_figure_db: float = 7.0  # Receiver noise figure (typical: 5-10 dB)
    max_range_m: float = 500.0
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
    tx_height_m: float = 0.0
    rx_height_m: float = 1.5

    # Building attenuation: overall + per-material (config-driven; no code changes needed).
    # Dict with "overall": {scale, reduction_db} and optional "materials": {concrete: {...}, ...}
    building_attenuation: Optional[Dict[str, Any]] = None


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
    
    # Phase 1: LOS detection and path length
    is_los: bool = True  # Line-of-sight flag (True if no buildings block direct path)
    actual_path_length_m: float = 0.0  # Actual path length (>= distance_m, accounts for obstacles)
    num_buildings: int = 0  # Explicit building count (separate from obstacles_count)
    num_trees: int = 0  # Explicit tree/foliage count
    diffraction_flag: bool = False  # True if path involves diffraction around edges
    
    # Objective 1: Material-aware path loss
    cumulative_material_loss_db: float = 0.0  # Cumulative material penetration loss along ray
    metal_blocked: bool = False  # True if metal structure blocks path
    buildings_along_path: List[Dict[str, Any]] = []  # List of buildings along ray (for material info)


class WorldModel(BaseModel):
    """Complete world model for RF simulation."""

    tx: LatLon
    rf_params: RFParams
    cells: List[WorldCell]


class AttenuationGrid(BaseModel):
    """
    Final RF result. You can map this directly to a heatmap.
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


