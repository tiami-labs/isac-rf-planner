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
from ..geo.heatmap import attenuation_grid_to_raster, attenuation_grid_to_png_ellipse

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
    ray_mode: str = "2d",  # "2d" (OSM polygons) or "3d" (Google mesh profiles)
    tx_height_m: float = 0.0,
    rx_height_m: float = 1.5,
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

    # 1) TX placement
    # 2D mode: snap to street (keeps 2D behavior consistent with earlier implementation)
    # 3D mode: DO NOT snap (profiles + Google mesh are computed for the clicked TX)
    ray_mode_eff = str(getattr(rf_params, "ray_mode", ray_mode) or ray_mode).strip().lower()
    if ray_mode_eff in ("3d", "mesh", "google_mesh", "google-mesh", "3d_osm", "3d-osm", "osm3d", "3d_ray_trace", "3d-ray-trace", "ray_trace", "ray-trace"):
        logger.info("STEP 1: 3D mode - skipping street snapping; using clicked point as TX")
        snapped = SnappedPoint(LatLon(lat=lat, lon=lon), 0.0)
        logger.info(f"✓ STEP 1 SUCCESS: TX (no-snap): ({snapped.latlon.lat:.6f}, {snapped.latlon.lon:.6f})")
    else:
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
        logger.info(
            f"✓ STEP 1 SUCCESS: Snapped to street: ({snapped.latlon.lat:.6f}, {snapped.latlon.lon:.6f}), "
            f"distance: {snapped.distance_m:.1f}m"
        )


    # 2) Initialize map provider (PRIMARY: geometry-based)
    # Ray-mode selection is the ONLY place where 2D vs 3D diverges.
    logger.info("STEP 2: Initializing map provider...")

    # Allow RFParams to carry these fields if caller uses the REST API.
    # NOTE: ray_mode may be updated later if we fall back (e.g., missing mesh profiles).
    requested_ray_mode = getattr(rf_params, "ray_mode", ray_mode)
    ray_mode = requested_ray_mode
    tx_height_m = float(getattr(rf_params, "tx_height_m", tx_height_m))
    rx_height_m = float(getattr(rf_params, "rx_height_m", rx_height_m))
    if map_provider is None:
        # Default providers:
        #   - 2D: OSMMapProvider (polygons)
        #   - 3D: GoogleMeshOSMMapProvider (persisted mesh ray profiles, OSM semantics)
        try:
            ray_mode_l = str(ray_mode).lower()
            if ray_mode_l in ("3d", "mesh", "google_mesh", "google-mesh"):
                logger.info("  Creating GoogleMeshOSMMapProvider (3D)...")
                from ..geo.google_mesh import GoogleMeshOSMMapProvider, MeshProfileStore, MissingMeshProfiles

                osm = OSMMapProvider(cache_radius_m=1000.0)
                store = MeshProfileStore()
                map_provider = GoogleMeshOSMMapProvider(
                    profile_store=store,
                    osm_provider=osm,
                    tx_height_m=tx_height_m,
                    rx_height_m=rx_height_m,
                    max_range_m=rf_params.max_range_m,
                    dr_m=rf_params.step_m,
                    dtheta_deg=getattr(rf_params, "dtheta_deg", 5.0),
                )

                # Fail fast if mesh profiles are missing (avoid spending time before erroring).
                try:
                    map_provider.prefetch_all_data(snapped.latlon, rf_params.max_range_m + 50.0)
                except MissingMeshProfiles as e:
                    # 3D mode requires mesh profiles - fall back to 2D OSM mode instead of stub
                    logger.warning(f"  3D mesh profiles missing (key={e.key}), falling back to 2D OSM mode")
                    logger.info("  Using OSM MapProvider (2D) as fallback...")
                    map_provider = osm  # Use the OSM provider we already created
                    # Update ray_mode to 2d for consistency
                    rf_params.ray_mode = "2d"
                    logger.info("✓ Using OSM MapProvider for geometry-based world model (2D fallback)")

                if map_provider is not None and not isinstance(map_provider, OSMMapProvider):
                    logger.info("✓ Using Google-mesh MapProvider (3D ray propagation)")
            elif ray_mode_l in ("3d_osm", "3d-osm", "osm3d"):
                # 3D (OSM-only) is a height-sliced variant of OSM polygons.
                logger.info("  Creating OSMMapProvider (3D OSM-only height slice)...")
                map_provider = OSMMapProvider(cache_radius_m=1000.0, slice_height_m=tx_height_m)
                logger.info("✓ Using OSM MapProvider (3D OSM-only height slice)")
            elif ray_mode_l in ("3d_ray_trace", "3d-ray-trace", "ray_trace", "ray-trace"):
                logger.info("  Creating RayTraceOSMMapProvider (3D OSM + ray trace)...")
                from ..geo.ray_trace import RayTraceOSMMapProvider
                map_provider = RayTraceOSMMapProvider(cache_radius_m=1000.0, tx_height_m=tx_height_m, rx_height_m=rx_height_m)
                logger.info("✓ Using RayTrace OSM MapProvider (3D OSM + ray trace)")
            else:
                logger.info("  Creating OSMMapProvider...")
                map_provider = OSMMapProvider(cache_radius_m=1000.0)
                logger.info("✓ Using OSM MapProvider for geometry-based world model")
        except ValueError:
            # Preserve user-actionable errors (e.g., missing mesh profiles in 3D mode).
            raise
        except Exception as e:
            # Check if this is a MissingMeshProfiles exception (3D mode without profiles)
            # Import here to avoid circular dependency (only needed if 3D mode was attempted)
            try:
                from ..geo.google_mesh.provider import MissingMeshProfiles
                is_missing_profiles = isinstance(e, MissingMeshProfiles)
            except (ImportError, AttributeError):
                is_missing_profiles = False
            
            if is_missing_profiles:
                logger.warning(f"  3D mesh profiles missing (key={e.key}), falling back to 2D OSM mode")
                logger.info("  Using OSM MapProvider (2D) as fallback...")
                try:
                    map_provider = OSMMapProvider(cache_radius_m=1000.0)
                    rf_params.ray_mode = "2d"  # Update ray_mode to 2d
                    logger.info("✓ Using OSM MapProvider (2D fallback from missing 3D profiles)")
                except Exception as e2:
                    logger.error(f"  Failed to create OSM provider: {e2}, using stub")
                    map_provider = StubMapProvider()
            else:
                logger.warning(f"  Failed to initialize map provider ({ray_mode}): {e}, falling back to 2D OSM mode")
                # Fall back to OSM provider instead of stub
                try:
                    map_provider = OSMMapProvider(cache_radius_m=1000.0)
                    rf_params.ray_mode = "2d"  # Update ray_mode to 2d
                    logger.info("✓ Using OSM MapProvider (2D fallback)")
                except Exception as e2:
                    logger.error(f"  Failed to create OSM provider: {e2}, using stub")
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
    ray_mode_eff2 = str(getattr(rf_params, "ray_mode", ray_mode) or ray_mode).strip().lower()

    if ray_mode_eff2 in ("3d", "mesh", "google_mesh", "google-mesh", "3d_osm", "3d-osm", "osm3d", "3d_ray_trace", "3d-ray-trace", "ray_trace", "ray-trace"):
        # Both 3D renderers now use the same pre-colored PNG ellipse drape so the
        # frontend drawing path is visually consistent across OSM-only and Google-mesh modes.
        # Choose texture resolution from range and step, but cap to keep transfers reasonable.
        try:
            step_m = float(getattr(rf_params, "step_m", 5.0) or 5.0)
        except Exception:
            step_m = 5.0
        base = (2.0 * float(rf_params.max_range_m)) / max(5.0, step_m)
        tex_size = int(min(1024, max(512, round(base))))
        logger.debug(f"Generating heatmap PNG texture (size={tex_size})...")
        heatmap_payload = attenuation_grid_to_png_ellipse(grid, size=tex_size, vmin=-140.0, vmax=-60.0)
        logger.info(f"Generated heatmap PNG texture: {heatmap_payload.get('width')}x{heatmap_payload.get('height')}")
    else:
        logger.debug("Generating heatmap raster...")
        lats_2d, lons_2d, rsrp_2d = attenuation_grid_to_raster(grid, width=256, height=256)
        logger.info(f"Generated heatmap raster: {rsrp_2d.shape}")
        heatmap_payload = {
            "lats": lats_2d.tolist(),
            "lons": lons_2d.tolist(),
            "rsrp": rsrp_2d.tolist(),
        }


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
    
    # Reduce payload size for 3D OSM-only mode (front-end uses heatmap PNG, not per-point arrays).
    grid_payload = grid.model_dump()
    if ray_mode_eff2 in ("3d_osm", "3d-osm", "osm3d", "3d_ray_trace", "3d-ray-trace", "ray_trace", "ray-trace"):
        try:
            grid_payload["num_points"] = len(grid.cell_lat)
        except Exception:
            pass
        for k in ("cell_lat", "cell_lon", "rsrp_dbm", "sinr_db", "modulation", "throughput_mbps"):
            if k in grid_payload:
                grid_payload[k] = []

    # Use the effective mode after provider init/fallbacks.
    effective_ray_mode = str(getattr(rf_params, "ray_mode", ray_mode) or ray_mode)

    building_area_sqm = None
    if map_provider is not None and hasattr(map_provider, "get_building_area_sqm"):
        try:
            building_area_sqm = map_provider.get_building_area_sqm(
                snapped.latlon, float(getattr(rf_params, "max_range_m", 2000.0) or 2000.0)
            )
        except Exception as e:
            logger.warning(f"Failed to compute building area: {e}")

    result = {
        "original_point": {"lat": lat, "lon": lon},
        "snapped_tx": snapped.latlon.model_dump(),
        "snap_distance_m": snapped.distance_m,
        "ray_mode": effective_ray_mode,
        "requested_ray_mode": str(requested_ray_mode),
        "tx_height_m": tx_height_m,
        "rx_height_m": rx_height_m,
        "clutter_type": clutter_type,
        "world_model_source": "geometry_only" if not vlm_used else "geometry_vlm_refined",
        "streetview_available": streetview_available,
        "vlm_used": vlm_used,
        "sectors": sectors_info,  # Sector information for visualization
        "grid": grid_payload,
        "heatmap": heatmap_payload,
        "building_area_sqm": building_area_sqm,
    }

    # Surface 3D mesh profile key (if applicable) so the frontend can diagnose/cache.
    try:
        key = getattr(map_provider, "key", None)
        if key:
            result["mesh_profile_key"] = key
    except Exception:
        pass

    # If 3D mode is enabled, include the mesh-profile cache key for traceability.
    try:
        if hasattr(map_provider, "key"):
            k = getattr(map_provider, "key")
            if k:
                result["mesh_profile_key"] = k
    except Exception:
        pass
    
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
