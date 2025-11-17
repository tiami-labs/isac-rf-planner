"""Main orchestrator for RF planning pipeline."""

import base64
import io
import logging
from typing import Dict, Any, Optional

import numpy as np
from PIL import Image

from ..pipeline.schemas import LatLon, RFParams, AttenuationGrid, WorldModel
from ..geo.snapping import snap_to_street, SnappedPoint
from ..geo.streetview_provider import StreetViewProvider, create_streetview_provider
from ..geo.physical_spanning import MapProvider, StubMapProvider
from ..geo.osm_map_provider import OSMMapProvider
from ..geo.coverage_grid import build_coverage_grid
from ..vision.pano_preprocess import tile_pano
from ..vision.materials_extraction import return_static_material
from ..vision.models.base_vlm import BaseVLM
from ..pipeline.world_builder import build_world_model
from ..rf.attenuation_models import compute_attenuation_grid
from ..geo.heatmap import attenuation_grid_to_raster

logger = logging.getLogger(__name__)


def run_rf_planning_for_point(
    lat: float,
    lon: float,
    rf_params: RFParams,
    vlm: Optional[BaseVLM] = None,
    streetview_provider: Optional[StreetViewProvider] = None,
    map_provider: Optional[MapProvider] = None,
    max_snap_distance_m: float = 50.0,  # Increased default for better coverage
    num_views: int = 4,
) -> Dict[str, Any]:
    """
    Run complete RF planning pipeline for a point.

    Args:
        lat: Latitude
        lon: Longitude
        rf_params: RF simulation parameters
        vlm: Loaded VLM model
        streetview_provider: Street View provider (defaults to file-based)
        map_provider: Map provider for building/forest data (defaults to stub)
        max_snap_distance_m: Maximum distance to snap to street
        num_views: Number of pano tiles to extract

    Returns:
        Dictionary with results including snapped_tx, grid, and heatmap
    """
    logger.info("="*60)
    logger.info(f"STARTING RF PLANNING FOR POINT: ({lat}, {lon})")
    logger.info(f"  max_snap_distance_m: {max_snap_distance_m}")
    logger.info("="*60)

    # 1) Snap to street (required for TX placement) - EXACT SAME AS TEST SCRIPT
    logger.info(f"STEP 1: Snapping to street (max distance: {max_snap_distance_m}m)...")
    logger.info(f"  Calling snap_to_street({lat}, {lon}, max_distance_m={max_snap_distance_m})")
    snapped = snap_to_street(lat, lon, max_distance_m=max_snap_distance_m)
    logger.info(f"  snap_to_street returned: {snapped}")
    if snapped is None:
        logger.error(f"STEP 1 FAILED: No street found within {max_snap_distance_m}m of ({lat}, {lon})")
        raise ValueError(
            f"No street found within {max_snap_distance_m}m of ({lat}, {lon}). "
            "Please pick another point closer to a road."
        )
    if snapped.distance_m > max_snap_distance_m:
        logger.error(f"STEP 1 FAILED: Nearest street is {snapped.distance_m:.1f}m away (max: {max_snap_distance_m}m)")
        raise ValueError(
            f"No street within {max_snap_distance_m}m; pick another point. "
            f"Distance: {snapped.distance_m:.1f}m"
        )
    logger.info(f"✓ STEP 1 SUCCESS: Snapped to street: ({snapped.latlon.lat:.6f}, {snapped.latlon.lon:.6f}), distance: {snapped.distance_m:.1f}m")

    # 2) Initialize map provider (PRIMARY: geometry-based)
    logger.info("STEP 2: Initializing map provider...")
    if map_provider is None:
        # Use OSM by default (real geometry data)
        try:
            logger.info("  Creating OSMMapProvider...")
            map_provider = OSMMapProvider(cache_radius_m=1000.0)
            logger.info("✓ Using OSM MapProvider for geometry-based world model")
        except Exception as e:
            logger.warning(f"  Failed to initialize OSM MapProvider: {e}, falling back to stub")
            map_provider = StubMapProvider()
    else:
        logger.info("  Using provided map_provider")
    
    # Get clutter classification (non-blocking, can be slow)
    logger.info("STEP 2.1: Getting clutter classification...")
    clutter_type = "unknown"
    try:
        clutter_type = map_provider.get_clutter_type(snapped.latlon, radius_m=200.0)
        logger.info(f"✓ Clutter type: {clutter_type}")
    except Exception as e:
        logger.warning(f"  Could not get clutter type (non-critical): {e}")
        clutter_type = "unknown"

    # 3) OPTIONAL: Try to fetch Street View for refinement (non-blocking)
    pano = None
    streetview_available = False
    pano_location = None
    
    if streetview_provider is None:
        # Try to get config from environment
        import os
        provider_type = os.environ.get("STREETVIEW_PROVIDER", "mapillary")
        api_key = os.environ.get("MAPILLARY_API_KEY")
        initial_radius_m = float(os.environ.get("STREETVIEW_INITIAL_RADIUS_M", "50.0"))
        fallback_radius_m = float(os.environ.get("STREETVIEW_FALLBACK_RADIUS_M", "500.0"))
        
        if api_key or provider_type == "file":
            streetview_provider = create_streetview_provider(
                provider_type=provider_type,
                api_key=api_key,
                radius_m=initial_radius_m,
            )
        else:
            logger.debug("No Street View provider configured, skipping panorama fetch")
            streetview_provider = None

    if streetview_provider is not None:
        logger.debug("Checking Street View availability (optional refinement)...")
        try:
            streetview_available = streetview_provider.check_availability(
                snapped.latlon.lat, snapped.latlon.lon
            )
            logger.info(f"Street View available (radius {streetview_provider.radius_m}m): {streetview_available}")

            # Try fallback radius if not available
            if not streetview_available:
                import os
                fallback_radius_m = float(os.environ.get("STREETVIEW_FALLBACK_RADIUS_M", "500.0"))
                effective_fallback = min(fallback_radius_m, 2000.0)
                
                if effective_fallback > streetview_provider.radius_m:
                    logger.debug(f"Trying fallback radius {effective_fallback}m...")
                    streetview_provider = create_streetview_provider(
                        provider_type=os.environ.get("STREETVIEW_PROVIDER", "mapillary"),
                        api_key=os.environ.get("MAPILLARY_API_KEY"),
                        radius_m=effective_fallback,
                    )
                    streetview_available = streetview_provider.check_availability(
                        snapped.latlon.lat, snapped.latlon.lon
                    )

            if streetview_available:
                try:
                    logger.debug("Fetching panorama for optional VLM refinement...")
                    pano = streetview_provider.fetch_pano(snapped.latlon.lat, snapped.latlon.lon)
                    logger.info(f"Loaded panorama: {pano.shape[1]}x{pano.shape[0]} pixels")
                    
                    # Try to get panorama location
                    if hasattr(streetview_provider, '_query_nearby_images'):
                        images = streetview_provider._query_nearby_images(snapped.latlon.lat, snapped.latlon.lon, limit=1)
                        if images and "geometry" in images[0] and "coordinates" in images[0]["geometry"]:
                            coords = images[0]["geometry"]["coordinates"]
                            pano_location = {"lat": coords[1], "lon": coords[0]}
                except Exception as e:
                    logger.warning(f"Failed to fetch panorama (non-critical): {e}")
                    streetview_available = False
        except Exception as e:
            logger.debug(f"Street View check failed (non-critical): {e}")
    else:
        logger.debug("No Street View provider available, using geometry-only model")

    # 4) OPTIONAL: VLM analysis (only if VLM is provided AND panorama is available)
    views = []
    vlm_used = False
    if vlm is not None and pano is not None:
        logger.debug("Running VLM analysis for material refinement...")
        try:
            tiles = tile_pano(pano, num_views=num_views)
            logger.info(f"Created {len(tiles)} tiles for VLM analysis")

            for tile in tiles:
                logger.debug(f"Processing tile {tile.tile_id} with VLM")
                view = return_static_material(
                    vlm=vlm,
                    tile_img=tile.image,
                    tile_id=tile.tile_id,
                    yaw_center_deg=tile.yaw_center_deg,
                    hfov_deg=tile.hfov_deg,
                    vfov_deg=tile.vfov_deg,
                )
                views.append(view)
                logger.debug(f"Tile {tile.tile_id}: found {len(view.materials)} material segments")
            vlm_used = True
            logger.info("VLM analysis complete, materials will refine geometry-based model")
        except Exception as e:
            logger.warning(f"VLM analysis failed (non-critical): {e}")
    else:
        if vlm is None:
            logger.debug("No VLM provided, using geometry-only model")
        if pano is None:
            logger.debug("No panorama available, using geometry-only model")

    # 5) Build world model (PRIMARY: geometry, SECONDARY: VLM refinement if available)
    logger.debug("Building world model from geometry...")
    world = build_world_model(
        tx=snapped.latlon,
        rf_params=rf_params,
        views=views,  # Empty list if no VLM/pano - geometry will be used
        map_provider=map_provider,
    )
    logger.info(f"Built world model with {len(world.cells)} cells")

    # 5) RF attenuation
    logger.debug("Computing RF attenuation...")
    grid = compute_attenuation_grid(world)
    logger.info(f"Computed attenuation grid with {len(grid.cell_lat)} points")

    # 6) heatmap / map overlay
    logger.debug("Generating heatmap raster...")
    lats_2d, lons_2d, rsrp_2d = attenuation_grid_to_raster(grid, width=256, height=256)
    logger.info(f"Generated heatmap raster: {rsrp_2d.shape}")

    # Prepare sector information for visualization
    sectors_info = []
    if rf_params.sectors:
        for sector_dict in rf_params.sectors:
            sectors_info.append({
                "sector_id": sector_dict.get("sector_id", "unknown"),
                "start_angle_deg": sector_dict.get("start_angle_deg", 0.0),
                "end_angle_deg": sector_dict.get("end_angle_deg", 360.0),
                "freq_mhz": sector_dict.get("freq_mhz", rf_params.freq_mhz),
                "tx_power_dbm": sector_dict.get("tx_power_dbm", rf_params.tx_power_dbm),
            })
    else:
        # Omnidirectional (360°)
        sectors_info.append({
            "sector_id": "omnidirectional",
            "start_angle_deg": 0.0,
            "end_angle_deg": 360.0,
            "freq_mhz": rf_params.freq_mhz,
            "tx_power_dbm": rf_params.tx_power_dbm,
        })
    
    # Prepare response
    result = {
        "original_point": {"lat": lat, "lon": lon},
        "snapped_tx": snapped.latlon.model_dump(),
        "snap_distance_m": snapped.distance_m,
        "clutter_type": clutter_type,
        "world_model_source": "geometry_only" if not vlm_used else "geometry_vlm_refined",
        "streetview_available": streetview_available,
        "vlm_used": vlm_used,
        "sectors": sectors_info,  # Sector information for visualization
        "grid": grid.model_dump(),
        "heatmap": {
            "lats": lats_2d.tolist(),
            "lons": lons_2d.tolist(),
            "rsrp": rsrp_2d.tolist(),
        },
    }
    
    # Add panorama location if available
    if pano_location:
        result["panorama_location"] = pano_location

    # Add panorama image if available (for display in UI)
    if pano is not None:
        logger.debug("Encoding panorama for web display...")
        # Convert numpy array to base64-encoded JPEG for JSON response
        # Note: pano may already be resized by streetview_provider
        img = Image.fromarray(pano)
        # Resize for web display (max 1024px width to keep JSON response small and fast)
        max_width = 1024  # Reduced from 2048 for faster encoding/transmission
        if img.width > max_width:
            scale = max_width / img.width
            new_height = int(img.height * scale)
            logger.info(f"Resizing panorama for web display: {img.width}x{img.height} -> {max_width}x{new_height}")
            img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
        
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=75)  # Reduced quality for faster encoding
        img_bytes = buffer.getvalue()
        logger.debug(f"Panorama encoded: {len(img_bytes) / 1024:.1f} KB")
        img_base64 = base64.b64encode(img_bytes).decode("utf-8")
        result["panorama_image"] = f"data:image/jpeg;base64,{img_base64}"
        logger.info("Panorama added to response")

    # Add VLM results if available
    if views:
        result["world_model"] = {
            "num_cells": len(world.cells),
            "materials_detected": len(views),
        }

    return result
