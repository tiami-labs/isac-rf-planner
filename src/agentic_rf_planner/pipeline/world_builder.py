"""Build world model from materials and map data."""

import math
import logging
from typing import List, Optional

from .schemas import (
    LatLon,
    RFParams,
    ViewTileDescription,
    WorldModel,
    WorldCell,
    MaterialType,
)
from ..geo.coverage_grid import build_coverage_grid
from ..geo.physical_spanning import MapProvider, estimate_obstacles_along_ray
from ..rf.material_penetration import get_penetration_loss_for_material, is_material_blocking

logger = logging.getLogger(__name__)


def build_world_model(
    tx: LatLon,
    rf_params: RFParams,
    views: List[ViewTileDescription],
    map_provider: Optional[MapProvider] = None,
) -> WorldModel:
    """
    Build world model from geometry (primary) and optional VLM views (refinement).

    Architecture: Geometry-first, VLM-second.
    
    Steps:
    1. Pre-fetch all OSM data for the coverage area (ONE API call instead of thousands).
    2. Build coverage grid.
    3. For each cell: assign material from geometry (buildings/forest/open).
    4. Optionally refine with VLM view segments if available.
    5. Count obstacles along LOS.
    """
    import logging
    logger = logging.getLogger(__name__)
    
    # Pre-fetch all OSM data BEFORE processing cells (critical for performance)
    if map_provider is not None:
        # Check if map_provider has prefetch capability
        if hasattr(map_provider, 'prefetch_all_data'):
            prefetch_radius = rf_params.max_range_m + 50.0  # Add buffer
            logger.info(f"Pre-fetching OSM data for {prefetch_radius}m radius...")
            map_provider.prefetch_all_data(tx, prefetch_radius)
            logger.info("✓ OSM data pre-fetched, processing cells with cached data")

    # Build coverage grid with adaptive ray termination
    # Pass map_provider so rays can stop when signal is too weak or metal blocks
    # Pass sectors if configured (from rf_params.sectors)
    sectors = rf_params.sectors if hasattr(rf_params, 'sectors') and rf_params.sectors else None
    cells = build_coverage_grid(tx, rf_params, map_provider=map_provider, sectors=sectors)
    logger.info(f"Created {len(cells)} cells in coverage grid (with adaptive termination)")
    
    # Process cells with cached data (much faster now)
    logger.info("Processing cells with cached OSM data...")
    cells_processed = 0
    los_count = 0
    nlos_count = 0
    
    for cell in cells:
        bearing = cell.bearing_deg  # 0–360
        
        # PRIMARY: Assign material from geometry (map data)
        material_from_geometry = _estimate_material_from_geometry(
            tx=tx,
            cell=cell,
            map_provider=map_provider,
        )
        
        # SECONDARY: Refine with VLM if available
        if views:
            material_from_vlm = _estimate_material_from_views(views, bearing)
            # Prefer VLM if it found something more specific than UNKNOWN
            if material_from_vlm != MaterialType.UNKNOWN:
                matched_material = material_from_vlm
            else:
                matched_material = material_from_geometry
        else:
            matched_material = material_from_geometry

        # Phase 1: LOS detection and path length
        is_los = True
        num_buildings = 0
        num_trees = 0
        actual_path_length_m = cell.distance_m  # Default to straight-line
        
        # Use pre-computed cumulative loss from coverage_grid if available
        # This ensures loss persists after exiting obstacles - DO NOT RECOMPUTE
        if hasattr(cell, 'cumulative_material_loss_db'):
            # Preserve the pre-computed cumulative loss (critical for persistence)
            cumulative_material_loss_db = cell.cumulative_material_loss_db
            metal_blocked = getattr(cell, 'metal_blocked', False)
            buildings_along_path = getattr(cell, 'buildings_along_path', [])
        else:
            # Fallback: compute it here (shouldn't happen if coverage_grid was called correctly)
            # But if we do compute it, we still need to preserve it
            cumulative_material_loss_db = 0.0
            metal_blocked = False
            buildings_along_path = []
            if map_provider is not None:
                cell_latlon = LatLon(lat=cell.lat, lon=cell.lon)
                buildings_along_path = map_provider.get_buildings_along_ray(tx, cell_latlon)
                for building in buildings_along_path:
                    material = building.get("material", "unknown")
                    if is_material_blocking(material, rf_params.freq_mhz):
                        metal_blocked = True
                        break
                    penetration_loss = get_penetration_loss_for_material(material, rf_params.freq_mhz)
                    cumulative_material_loss_db += penetration_loss
                if map_provider.is_forest_between(tx, cell_latlon):
                    tree_loss = get_penetration_loss_for_material("wood", rf_params.freq_mhz)
                    cumulative_material_loss_db += tree_loss
        
        if map_provider is not None:
            cell_latlon = LatLon(lat=cell.lat, lon=cell.lon)
            
            # Get buildings along ray with material information (for counting/classification)
            # Only if we don't already have it from coverage_grid
            if not buildings_along_path:
                buildings_along_path = map_provider.get_buildings_along_ray(tx, cell_latlon)
            num_buildings = len(buildings_along_path)
            is_los = (num_buildings == 0)
            
            # Count trees separately
            if map_provider.is_forest_between(tx, cell_latlon):
                num_trees = 1
            
            # Fix 1: Drop d_actual - use straight-line distance only
            # All NLOS effects are handled via material loss, not geometric distance
            actual_path_length_m = cell.distance_m  # Use straight-line distance
        
        # Count obstacles along LOS (existing logic)
        obstacles_count = 0
        if map_provider is not None:
            obstacles_count = estimate_obstacles_along_ray(
                tx=tx,
                target_lat=cell.lat,
                target_lon=cell.lon,
                material_hint=matched_material,
                map_provider=map_provider,
            )

        # Update cell with all computed values
        cell.dominant_material = matched_material
        cell.obstacles_count = obstacles_count
        cell.is_los = is_los
        cell.actual_path_length_m = actual_path_length_m
        cell.num_buildings = num_buildings
        cell.num_trees = num_trees
        cell.cumulative_material_loss_db = cumulative_material_loss_db
        cell.metal_blocked = metal_blocked
        cell.buildings_along_path = buildings_along_path
        
        # Track statistics
        if is_los:
            los_count += 1
        else:
            nlos_count += 1
        
        # Fix 1: Path length is now always straight-line (d_actual removed)
        # No need to track path length increase
        
        cells_processed += 1
        if cells_processed % 1000 == 0:
            logger.debug(f"  Processed {cells_processed}/{len(cells)} cells...")

    logger.info(f"✓ Processed all {len(cells)} cells")
    logger.info(f"  LOS cells: {los_count} ({100*los_count/len(cells):.1f}%)")
    logger.info(f"  NLOS cells: {nlos_count} ({100*nlos_count/len(cells):.1f}%)")
    
    return WorldModel(tx=tx, rf_params=rf_params, cells=cells)


def _estimate_material_from_geometry(
    tx: LatLon,
    cell: WorldCell,
    map_provider: Optional[MapProvider],
) -> MaterialType:
    """
    Assign material type based on geometry (buildings, forest, open).
    
    This is the PRIMARY path - works without any VLM/panorama.
    """
    if map_provider is None:
        return MaterialType.UNKNOWN
    
    # Check for forest/vegetation first (trees)
    if map_provider.is_forest_between(tx, LatLon(lat=cell.lat, lon=cell.lon)):
        return MaterialType.TREES
    
    # Check for buildings
    building_count = map_provider.count_buildings_between(
        tx, LatLon(lat=cell.lat, lon=cell.lon)
    )
    
    if building_count > 0:
        # Classify building type based on count and distance
        # More buildings = likely dense urban (LARGE_STRUCTURE)
        # Fewer buildings = likely suburban (HOUSE)
        if building_count >= 3:
            return MaterialType.LARGE_STRUCTURE
        elif building_count >= 2:
            return MaterialType.BUILDING
        else:
            return MaterialType.HOUSE
    
    # Default: open/unknown
    return MaterialType.UNKNOWN


def _estimate_material_from_views(
    views: List[ViewTileDescription],
    bearing_deg: float,
) -> MaterialType:
    """
    Coarse mapping: given LOS bearing, find the most likely material
    from the pano segments.

    MVP: ignore pitch. Just match yaw.
    """
    best_mat = MaterialType.UNKNOWN
    best_conf = 0.0

    for view in views:
        for seg in view.materials:
            if _angle_in_range(bearing_deg, seg.yaw_deg_start, seg.yaw_deg_end):
                if seg.confidence > best_conf:
                    best_conf = seg.confidence
                    best_mat = seg.material

    return best_mat


def _angle_in_range(angle: float, start: float, end: float) -> bool:
    """
    Handles wrap-around (e.g. start=350, end=10).
    """
    angle = angle % 360.0
    start = start % 360.0
    end = end % 360.0

    if start <= end:
        return start <= angle <= end
    else:
        # wrapped
        return angle >= start or angle <= end


def _compute_actual_path_length(
    tx: LatLon,
    cell: WorldCell,
    map_provider: MapProvider,
    num_buildings: int,
) -> float:
    """
    Calculate actual path length accounting for obstacles.
    
    If ray passes through buildings:
    - Estimate path length through building (based on building size)
    - Account for material propagation (slower in dense materials)
    
    Returns: actual_path_length_m (>= straight_line_distance)
    
    Phase 1: Simplified approach - estimate based on building count and distance.
    Future: Compute exact intersection lengths through each building polygon.
    """
    straight_distance = cell.distance_m
    
    # If no buildings, path length equals straight-line distance
    if num_buildings == 0:
        return straight_distance
    
    # Estimate path through buildings
    # For each building, estimate average thickness (simplified: 10-20m per building)
    # This is a rough approximation - in Phase 2 we'll compute exact intersection lengths
    avg_building_thickness_m = 15.0  # Typical building width/depth
    path_through_buildings = num_buildings * avg_building_thickness_m
    
    # Material propagation factor: RF signals travel slower through dense materials
    # Effective path length increases by ~20% for path through concrete/brick
    material_factor = 1.2
    
    # Effective path length = straight + (path_through_buildings × material_factor)
    effective_distance = straight_distance + (path_through_buildings * material_factor)
    
    return effective_distance


