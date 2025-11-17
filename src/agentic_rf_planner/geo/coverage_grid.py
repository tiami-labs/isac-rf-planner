"""Build coverage grid around transmitter."""

import math
import logging
from typing import List, Optional

from ..pipeline.schemas import LatLon, RFParams, WorldCell, MaterialType
from .physical_spanning import MapProvider

logger = logging.getLogger(__name__)


def build_coverage_grid(
    tx: LatLon, 
    rf_params: RFParams,
    map_provider: Optional[MapProvider] = None,
    sectors: Optional[List] = None  # List of SectorConfig objects
) -> List[WorldCell]:
    """
    Create a ring/grid of cells around TX with adaptive ray termination.
    
    If sectors are provided, generates cells only for angles covered by sectors.
    If no sectors (or None), generates omnidirectional coverage (360°).
    
    Rays stop when:
    1. Signal strength < noise_floor + margin (too weak)
    2. Metal structure blocks path (100+ dB loss)
    3. Maximum range reached
    
    This ensures rays are physically affected by materials, not just post-processed.
    """
    from ..rf.sector_config import SectorConfig, create_omnidirectional_sector, validate_sectors
    
    cells: List[WorldCell] = []
    max_r = rf_params.max_range_m
    dr = rf_params.step_m
    dtheta = 5.0  # degrees; tweak later
    
    # Convert sector dicts to SectorConfig objects if needed
    sector_configs: List[SectorConfig] = []
    if sectors:
        logger.info(f"Processing {len(sectors)} sector(s) for coverage grid generation")
        for sector_dict in sectors:
            try:
                sector_config = SectorConfig(**sector_dict)
                sector_configs.append(sector_config)
            except Exception as e:
                logger.error(f"Invalid sector config: {sector_dict}, error: {e}")
                raise
        
        validate_sectors(sector_configs)
    else:
        # No sectors specified - use omnidirectional
        logger.info("No sectors specified - using omnidirectional coverage (360°)")
        sector_configs = [create_omnidirectional_sector(rf_params)]
    
    # Signal strength threshold for ray termination
    # Calculate noise floor from bandwidth + NF if not explicitly set
    if rf_params.noise_floor_dbm is not None:
        noise_floor_dbm = rf_params.noise_floor_dbm
    else:
        # N = -174 dBm/Hz + 10*log10(B_Hz) + NF_dB
        bandwidth_hz = rf_params.channel_bandwidth_mhz * 1e6
        noise_floor_dbm = -174.0 + 10.0 * math.log10(bandwidth_hz) + rf_params.noise_figure_db
    
    # Termination threshold: Use realistic value for sparse detection
    # -110 dBm is where cell detection becomes unreliable in practice
    # This ensures:
    # 1. Rays stop at realistic distances (especially higher frequencies)
    # 2. Frequency affects propagation distance (lower freq = longer range via FSPL)
    # 3. FSPL always applies (signal weakens with distance even in free space)
    # 4. Material/NLOS loss makes rays stop even earlier
    min_rsrp_threshold = -110.0  # Realistic threshold for sparse/unreliable detection
    logger.info(f"Ray termination threshold: {min_rsrp_threshold:.1f} dBm (noise floor: {noise_floor_dbm:.1f} dBm)")
    logger.debug(f"  This ensures rays stop when signal becomes too weak for reliable detection")
    logger.debug(f"  Higher frequencies (e.g., 3.5 GHz) will stop earlier than lower frequencies (e.g., 622 MHz)")
    logger.debug(f"  FSPL always applies, material loss adds on top")
    
    # Import here to avoid circular dependency
    from ..rf.material_penetration import get_penetration_loss_for_material, is_material_blocking
    from ..rf.attenuation_models import _free_space_path_loss_db
    
    # Get frequency once (used for all calculations)
    freq_mhz = rf_params.freq_mhz
    
    # Generate cells for each sector
    # For each sector, iterate through angles covered by that sector
    for sector in sector_configs:
        if sector.sector_type == "polygon":
            logger.debug(f"Generating cells for polygon sector {sector.sector_id} "
                        f"(polygon with {len(sector.polygon_points)} points)")
        else:
            logger.debug(f"Generating cells for sector {sector.sector_id} "
                        f"({sector.start_angle_deg:.1f}° to {sector.end_angle_deg:.1f}°)")
        
        # Use sector-specific frequency and power for this sector
        sector_freq_mhz = sector.freq_mhz
        sector_tx_power_dbm = sector.tx_power_dbm
        
        # Determine angle range for this sector
        if sector.sector_type == "polygon":
            # For polygon sectors, iterate through all angles (0-360) but only create cells
            # if the point is inside the polygon
            angle_range = [(0.0, 360.0)]
        elif sector.sector_type == "360":
            # 360° sector: full circle
            angle_range = [(0.0, 360.0)]
        else:
            # Angle-based sector
            start_angle = sector.start_angle_deg
            end_angle = sector.end_angle_deg
            
            # Handle wrap-around: if start > end, sector wraps around 360°
            if start_angle <= end_angle:
                # Normal case: no wrap-around
                angle_range = [(start_angle, end_angle)]
            else:
                # Wrap-around: e.g., 350° to 10° -> [350, 360) and [0, 10]
                angle_range = [(start_angle, 360.0), (0.0, end_angle)]
        
        # Generate cells for each angle range in this sector
        for range_start, range_end in angle_range:
            theta = range_start
            while theta < range_end:
                r = dr
                cumulative_material_loss_db = 0.0  # Track cumulative loss along this ray
                metal_blocked = False
                
                while r <= max_r:
                    lat, lon = _project_from_tx(tx.lat, tx.lon, r, theta)
                    cell_latlon = LatLon(lat=lat, lon=lon)
                    
                    # For polygon sectors, check if point is inside polygon
                    # If not, skip this cell (but continue ray to check further points)
                    if sector.sector_type == "polygon":
                        is_inside = sector.covers_point(lat, lon, tx.lat, tx.lon)
                        if not is_inside:
                            # Point is outside polygon - skip this cell but continue ray
                            r += dr
                            continue
                        # Point is inside polygon - create cell
                        logger.debug(f"Cell at ({lat:.6f}, {lon:.6f}) is INSIDE polygon sector {sector.sector_id}")
                    
                    # Compute cumulative material loss incrementally along ray
                    # IMPORTANT: Loss accumulates along the ray and PERSISTS even after exiting obstacles
                    buildings_along_path = []
                    
                    # Always compute FSPL (signal weakens with distance even in free space)
                    # Use sector-specific frequency
                    fspl_db = _free_space_path_loss_db(r, sector_freq_mhz)
                    
                    # Check if path is LOS or NLOS (for NLOS excess loss)
                    is_los = True
                    cumulative_material_loss_db = 0.0
                    
                    if map_provider is not None:
                        # Get buildings along ray up to this point
                        buildings_along_path = map_provider.get_buildings_along_ray(tx, cell_latlon)
                        
                        # If any buildings block the path, it's NLOS
                        if len(buildings_along_path) > 0:
                            is_los = False
                        
                        # Recompute total loss for this distance (cumulative along entire ray from TX)
                        # This ensures loss persists even after exiting obstacles
                        # Apply harsh attenuation for all materials, including metal
                        # Even metal doesn't completely block - rays can diffract/reflect around
                        # The signal will be very weak, but the RSRP threshold will naturally terminate
                        # if it becomes too weak. This is more physically realistic.
                        for building in buildings_along_path:
                            material = building.get("material", "unknown")
                            
                            # Get frequency-dependent penetration loss (use sector frequency)
                            # Metal causes massive attenuation (100+ dB), but we still apply it and continue
                            penetration_loss = get_penetration_loss_for_material(material, sector_freq_mhz)
                            cumulative_material_loss_db += penetration_loss
                            
                            # Track if metal encountered (for logging), but don't stop ray
                            if is_material_blocking(material, sector_freq_mhz):
                                metal_blocked = True
                                logger.debug(f"Ray at bearing {theta:.1f}° (sector {sector.sector_id}) encounters metal at {r:.1f}m - applying {penetration_loss:.1f}dB attenuation")
                        
                        # Check for trees (use sector frequency)
                        if map_provider.is_forest_between(tx, cell_latlon):
                            tree_loss = get_penetration_loss_for_material("wood", sector_freq_mhz)
                            cumulative_material_loss_db += tree_loss
                
                        # No hard stops for material loss - let RSRP threshold handle termination naturally
                        # This allows rays to continue even with high material loss (e.g., metal),
                        # accounting for diffraction/reflection that allows weak signal behind obstacles
                    else:
                        # No map provider: assume LOS, no material loss
                        cumulative_material_loss_db = 0.0
                    
                    # Add NLOS excess loss if path is blocked (matches attenuation_models.py logic)
                    # This is critical for realistic termination, especially in dense urban
                    nlos_excess_loss_db = 0.0
                    if not is_los:
                        # Base NLOS excess loss: +20 dB (typical for urban environments)
                        nlos_excess_loss_db = 20.0
                        # Distance-dependent component: +0.1 dB/m after 50m
                        if r > 50.0:
                            nlos_excess_loss_db += 0.1 * (r - 50.0)
                    
                    # Compute signal strength: FSPL + NLOS excess + material loss
                    # Use sector-specific TX power
                    total_loss_db = fspl_db + nlos_excess_loss_db + cumulative_material_loss_db
                    estimated_rsrp = sector_tx_power_dbm - total_loss_db
                    
                    # Stop ray if signal too weak (applies even without obstacles due to FSPL)
                    if estimated_rsrp < min_rsrp_threshold:
                        logger.debug(f"Ray at bearing {theta:.1f}° (sector {sector.sector_id}) terminated at {r:.1f}m (RSRP={estimated_rsrp:.1f}dBm < threshold {min_rsrp_threshold:.1f}dBm)")
                        break
                    
                    # Create cell (will be refined in world_builder, but we know it's valid)
                    cells.append(
                        WorldCell(
                            lat=lat,
                            lon=lon,
                            distance_m=r,
                            bearing_deg=theta,
                            dominant_material=MaterialType.UNKNOWN,
                            obstacles_count=0,
                            extra_loss_db=0.0,
                            # Phase 1: Initialize LOS and path length fields
                            is_los=True,  # Will be computed in world_builder
                            actual_path_length_m=0.0,  # Will be computed in world_builder
                            num_buildings=0,
                            num_trees=0,
                            diffraction_flag=False,
                            # Objective 1: Store cumulative loss for this cell
                            cumulative_material_loss_db=cumulative_material_loss_db,
                            metal_blocked=metal_blocked,
                            buildings_along_path=buildings_along_path if map_provider else [],
                        )
                    )
                    
                    r += dr
                
                theta += dtheta
    
    # Note: We've processed all sectors, so we're done
    
    logger.info(f"Generated {len(cells)} cells with adaptive ray termination (max would be {int(360/dtheta) * int(max_r/dr)})")
    return cells


def _project_from_tx(lat: float, lon: float, distance_m: float, bearing_deg: float) -> tuple[float, float]:
    """
    Simple local ENU approximation.
    For more accuracy, use a proper geodesic library later.
    """
    # Convert bearing to radians
    bearing_rad = math.radians(bearing_deg)

    # Earth radius in meters
    R = 6371000.0

    # Convert lat/lon to radians
    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)

    # Simple flat-earth approximation for small distances
    # For distances < 1km, this is reasonably accurate
    d_lat = (distance_m * math.cos(bearing_rad)) / R
    d_lon = (distance_m * math.sin(bearing_rad)) / (R * math.cos(lat_rad))

    new_lat = math.degrees(lat_rad + d_lat)
    new_lon = math.degrees(lon_rad + d_lon)

    return new_lat, new_lon


