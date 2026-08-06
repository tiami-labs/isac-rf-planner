"""Main orchestrator for RF planning pipeline."""

import base64
import io
import logging
import math
import os
from time import perf_counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, cast

import numpy as np
from PIL import Image

from ..pipeline.schemas import LatLon, RFParams, AttenuationGrid, WorldModel
from ..geo.snapping import snap_to_street, SnappedPoint
from ..geo.streetview_provider import StreetViewProvider, create_streetview_provider
from ..geo.physical_spanning import MapProvider, StubMapProvider
from ..geo.osm_map_provider import OSMMapProvider
from ..geo.local_osm_provider import (
    AutoCachingOSMProvider,
    LocalOSMProvider,
    resolve_local_osm_database,
)
from ..geo.coverage_grid import build_coverage_grid
from ..vision.pano_preprocess import tile_pano
from ..vision.materials_extraction import return_static_material
from ..vision.models.base_vlm import BaseVLM
from ..pipeline.world_builder import build_world_model
from ..rf.attenuation_models import apply_channel_analysis, compute_attenuation_grid
from ..rf.channel_products import write_channel_product
from ..rf.channel_environment import lookup_from_world, return_path_rf_params
from ..geo.google_mesh.utils import haversine_m
from ..rf.sector_config import SectorConfig, create_omnidirectional_sector
from ..geo.heatmap import EllipseRasterizer

logger = logging.getLogger(__name__)


def _current_rss_mb() -> float | None:
    """Return current process resident memory on Linux without extra dependencies."""

    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
        return resident_pages * page_size / (1024.0 * 1024.0)
    except Exception:
        return None


def _has_values(values: Any) -> bool:
    return values is not None and len(values) > 0


def _osm_buildings_for_client_from_map_provider(map_provider: Any) -> Optional[Dict[str, Any]]:
    """Same OSM footprints the planner already prefetched; lets the 2D ray tracer skip a duplicate HTTP fetch."""
    if map_provider is None:
        return None
    osm = map_provider
    inner = getattr(map_provider, "osm", None)
    if inner is not None:
        osm = inner
    if not isinstance(osm, OSMMapProvider):
        return None
    buildings = list(getattr(osm, "_cached_buildings", None) or [])
    if not buildings:
        return None
    cc = getattr(osm, "_cache_center", None)
    if cc is None:
        return None
    try:
        r = float(getattr(osm, "_prefetch_radius_m", 0.0) or 0.0)
    except Exception:
        r = 0.0
    if r <= 0.0:
        return None
    if hasattr(cc, "model_dump"):
        center = cast(Any, cc).model_dump()
    else:
        center = {"lat": float(getattr(cc, "lat")), "lon": float(getattr(cc, "lon"))}
    return {"center": center, "radius_m": r, "buildings": buildings}


def _is_dvt(rf_params: RFParams) -> bool:
    return str(getattr(rf_params, "technology", "") or "").strip().lower() == "dvt"


def _channel_product_arrays(grid: AttenuationGrid) -> Dict[str, Any]:
    """Return the complete per-point RF, environment and bistatic data product."""

    arrays: Dict[str, Any] = {
        "latitude_deg": grid.cell_lat,
        "longitude_deg": grid.cell_lon,
        "ground_elevation_m_amsl": grid.z_ground_m,
        "terrain_loss_db": grid.terrain_loss_db,
        "terrain_los": grid.los_terrain,
        "terrain_state": grid.terrain_state,
        "rsrp_dbm": grid.rsrp_dbm,
        "sinr_db": grid.sinr_db,
        "received_power_dbm": grid.received_power_dbm,
        "field_strength_dbuv_m": grid.field_strength_dbuv_m,
        "carrier_to_noise_db": grid.carrier_to_noise_db,
        "modulation": grid.modulation,
        "throughput_mbps": grid.throughput_mbps,
        "serving_sector_id": grid.serving_sector_id,
        "interferer_count": grid.interferer_count,
        "top_interferer_rsrp_dbm": grid.top_interferer_rsrp_dbm,
        "pilot_pollution_metric_db": grid.pilot_pollution_metric_db,
        "source_eirp_at_target_dbm": grid.source_eirp_at_target_dbm,
        "incident_isotropic_power_dbm": grid.incident_power_isotropic_dbm,
        "tx_target_path_loss_db": grid.tx_target_path_loss_db,
        "tx_target_environment_excess_db": grid.tx_target_environment_excess_db,
        "tx_target_penetration_loss_db": grid.tx_target_penetration_loss_db,
        "tx_target_shadow_loss_db": grid.tx_target_shadow_loss_db,
        "tx_target_diffraction_loss_db": grid.tx_target_diffraction_loss_db,
        "tx_target_canyon_recovery_db": grid.tx_target_canyon_recovery_db,
        "tx_target_horizontal_pattern_loss_db": grid.tx_target_horizontal_pattern_loss_db,
        "tx_target_vertical_pattern_loss_db": grid.tx_target_vertical_pattern_loss_db,
        "tx_target_obstacles_count": grid.tx_target_obstacles_count,
        "tx_target_propagation_mode": grid.tx_target_propagation_mode,
        "echo_power_dbm": grid.bistatic_echo_power_dbm,
        "preprocessing_snr_db": grid.bistatic_preprocessing_snr_db,
        "postprocessing_snr_db": grid.bistatic_postprocessing_snr_db,
        "detection_margin_db": grid.bistatic_detection_margin_db,
        "echo_to_residual_direct_db": grid.bistatic_echo_to_residual_direct_db,
        "required_dynamic_range_db": grid.bistatic_required_dynamic_range_db,
        "tx_target_range_m": grid.bistatic_tx_target_range_m,
        "target_receiver_range_m": grid.bistatic_target_receiver_range_m,
        "return_path_loss_db": grid.bistatic_return_path_loss_db,
        "return_environment_excess_db": grid.bistatic_return_environment_excess_db,
        "total_bistatic_path_loss_db": grid.bistatic_total_path_loss_db,
        "bistatic_path_range_m": grid.bistatic_path_range_m,
        "excess_path_range_m": grid.bistatic_excess_path_range_m,
        "excess_delay_s": grid.bistatic_excess_delay_s,
        "bistatic_angle_deg": grid.bistatic_angle_deg,
        "path_range_rate_mps": grid.bistatic_path_range_rate_mps,
        "closing_speed_mps": grid.bistatic_closing_speed_mps,
        "doppler_hz": grid.bistatic_doppler_hz,
        "doppler_resolved": grid.bistatic_doppler_resolved,
        "doppler_ambiguous": grid.bistatic_doppler_ambiguous,
        "detectable": grid.bistatic_detectable,
        "isac_quality_code": grid.bistatic_isac_quality_code,
    }
    if grid.rsrp_by_sector:
        for sector_id, values in grid.rsrp_by_sector.items():
            safe = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in str(sector_id))
            arrays[f"rsrp_by_sector__{safe}"] = values
    return arrays


def _build_channel_return_path_lookup(
    *,
    forward_world: WorldModel,
    map_provider: Any,
    terrain_provider: Any,
    progress: Callable[[str, str], None],
) -> Any:
    """Build the environment-aware target-to-receiver reciprocal lookup."""

    config = getattr(forward_world.rf_params, "channel_analysis", None)
    if config is None or str(config.return_path_model) != "environmental_reciprocal_grid":
        return None
    receiver = config.receiver
    receiver_point = LatLon(lat=float(receiver.latitude), lon=float(receiver.longitude))
    direct_distance_m = float(haversine_m(forward_world.tx, receiver_point))
    max_return_range_m = (
        direct_distance_m
        + float(forward_world.rf_params.max_range_m)
        + 50.0
    )
    return_rf = return_path_rf_params(
        forward_world.rf_params,
        receiver_antenna_height_m=float(receiver.antenna_height_m_agl),
        target_height_m=float(config.target.height_m_agl),
        max_range_m=max_return_range_m,
    )
    progress(
        "return_path_environment",
        f"Building receiver-centered OSM/terrain return-path grid to {max_return_range_m:.0f} m",
    )
    return_world = build_world_model(
        tx=receiver_point,
        rf_params=return_rf,
        views=[],
        map_provider=map_provider,
        terrain_provider=terrain_provider,
    )
    lookup = lookup_from_world(
        return_world,
        receiver=receiver_point,
        max_range_m=max_return_range_m,
        dr_m=float(return_rf.step_m),
        dtheta_deg=float(return_rf.dtheta_deg),
    )
    return_world.cells.clear()
    progress(
        "return_path_environment",
        f"Return-path environment grid ready ({lookup.metadata()['valid_samples']} samples)",
    )
    return lookup

def _create_dvt_map_provider(rf_params: RFParams, tx_height_m: float) -> MapProvider:
    """Create the automatic persistent AOI cache used by broadcast planning.

    No preparation command is required.  The provider creates the SQLite cache
    on first use, acquires only an uncached AOI, and reuses it on later runs.
    """

    database_path = resolve_local_osm_database()
    provider = AutoCachingOSMProvider(database_path, slice_height_m=None)
    logger.info("✓ Using automatic DVT OSM AOI cache: %s", database_path)
    return provider


def run_rf_planning_for_point(
    lat: float,
    lon: float,
    rf_params: RFParams,
    vlm: Optional[BaseVLM] = None,
    streetview_provider: Optional[StreetViewProvider] = None,
    map_provider: Optional[MapProvider] = None,
    max_snap_distance_m: float = 50.0,  # Increased default for better coverage
    num_views: int = 4,
    ray_mode: str = "2d",  # "2d", "3d_osm", "3d_rt", "3d_rt_osm"
    tx_height_m: float = 0.0,
    rx_height_m: float = 1.5,
    progress_cb: Optional[Callable[[str, str], None]] = None,
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
    pipeline_started = perf_counter()
    stage_timings_s: Dict[str, float] = {}
    memory_snapshots_mb: Dict[str, float] = {}
    rss_start = _current_rss_mb()
    if rss_start is not None:
        memory_snapshots_mb["start"] = round(rss_start, 2)

    logger.info("="*60)
    logger.info(f"STARTING RF PLANNING FOR POINT: ({lat}, {lon})")
    logger.info(f"  max_snap_distance_m: {max_snap_distance_m}")
    logger.info("="*60)

    def progress(phase: str, detail: str) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(phase, detail)
        except Exception:
            logger.debug("progress callback failed", exc_info=True)

    # 1) TX placement
    # Broadcast sites use their configured coordinates directly.  Street snapping
    # is only meaningful for the legacy mobile-site workflow.
    is_dvt = _is_dvt(rf_params)
    ray_mode_eff = str(getattr(rf_params, "ray_mode", ray_mode) or ray_mode).strip().lower()
    if is_dvt:
        progress("tx_placement", "DVT: using configured broadcast-site coordinates")
        logger.info("STEP 1: DVT broadcast site - skipping street snapping")
        snapped = SnappedPoint(LatLon(lat=lat, lon=lon), 0.0)
        logger.info(
            "✓ STEP 1 SUCCESS: DVT TX (no-snap): (%.6f, %.6f)",
            snapped.latlon.lat,
            snapped.latlon.lon,
        )
    elif ray_mode_eff in ("3d", "mesh", "google_mesh", "google-mesh", "3d_rt", "3d-rt", "3d_osm", "3d-osm", "osm3d"):
        progress("tx_placement", "3D mode: using clicked TX point directly")
        logger.info("STEP 1: 3D mode - skipping street snapping; using clicked point as TX")
        snapped = SnappedPoint(LatLon(lat=lat, lon=lon), 0.0)
        logger.info(f"✓ STEP 1 SUCCESS: TX (no-snap): ({snapped.latlon.lat:.6f}, {snapped.latlon.lon:.6f})")
    else:
        progress("tx_placement", f"Snapping TX to nearest street within {max_snap_distance_m:.0f} m")
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
    progress("map_provider", f"Initializing map provider for ray mode {ray_mode_eff}")

    # Allow RFParams to carry these fields if caller uses the REST API.
    # NOTE: ray_mode may be updated later if we fall back (e.g., missing mesh profiles).
    requested_ray_mode = getattr(rf_params, "ray_mode", ray_mode)
    ray_mode = requested_ray_mode
    tx_height_m = float(getattr(rf_params, "tx_height_m", tx_height_m))
    rx_height_m = float(getattr(rf_params, "rx_height_m", rx_height_m))
    if map_provider is None:
        if is_dvt:
            progress("map_provider", "Opening local indexed OSM geometry database")
            map_provider = _create_dvt_map_provider(rf_params, tx_height_m)
        else:
            # Default providers:
            #   - 2D: OSMMapProvider (polygons)
            #   - 3D: GoogleMeshOSMMapProvider (persisted mesh ray profiles, OSM semantics)
            try:
                ray_mode_l = str(ray_mode).lower()
                if ray_mode_l in ("3d_rt", "3d-rt"):
                    logger.info("  Creating GoogleMeshOSMMapProvider (3D RT)...")
                    from ..geo.google_mesh import GoogleMeshOSMMapProvider, MeshProfileStore, MissingMeshProfiles

                    osm = OSMMapProvider(cache_radius_m=1000.0, slice_height_m=tx_height_m)
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

                    try:
                        map_provider.prefetch_all_data(snapped.latlon, rf_params.max_range_m + 50.0)
                    except MissingMeshProfiles as e:
                        logger.warning(f"  3D RT mesh profiles missing (key={e.key}), falling back to OSM")
                        map_provider = osm
                        rf_params.ray_mode = "3d_osm"

                    if map_provider is not None and not isinstance(map_provider, OSMMapProvider):
                        logger.info("✓ Using Google-mesh MapProvider (3D RT)")
                elif ray_mode_l in ("3d_osm", "3d-osm", "osm3d"):
                    # 3D (OSM-only) is a height-sliced variant of OSM polygons.
                    # We keep the same ray-march + material-loss logic, but filter
                    # building/foliage obstacles by an estimated OSM height vs the
                    # configured TX slice height.
                    logger.info("  Creating OSMMapProvider (3D OSM-only height slice)...")
                    map_provider = OSMMapProvider(cache_radius_m=1000.0, slice_height_m=tx_height_m)
                    logger.info("✓ Using OSM MapProvider (3D OSM-only height slice)")
                else:
                    logger.info("  Creating OSMMapProvider...")
                    map_provider = OSMMapProvider(cache_radius_m=1000.0, slice_height_m=tx_height_m)
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
                        map_provider = OSMMapProvider(cache_radius_m=1000.0, slice_height_m=tx_height_m)
                        rf_params.ray_mode = "2d"  # Update ray_mode to 2d
                        logger.info("✓ Using OSM MapProvider (2D fallback from missing 3D profiles)")
                    except Exception as e2:
                        logger.error(f"  Failed to create OSM provider: {e2}, using stub")
                        map_provider = StubMapProvider()
                else:
                    logger.warning(f"  Failed to initialize map provider ({ray_mode}): {e}, falling back to 2D OSM mode")
                    # Fall back to OSM provider instead of stub
                    try:
                        map_provider = OSMMapProvider(cache_radius_m=1000.0, slice_height_m=tx_height_m)
                        rf_params.ray_mode = "2d"  # Update ray_mode to 2d
                        logger.info("✓ Using OSM MapProvider (2D fallback)")
                    except Exception as e2:
                        logger.error(f"  Failed to create OSM provider: {e2}, using stub")
                        map_provider = StubMapProvider()
    else:
        logger.info("  Using provided map_provider")

    # DVT acquires/checks the complete AOI once before any geometry query.
    # world_builder calls prefetch_all_data again, but the provider returns
    # immediately from its in-memory cache.
    if is_dvt and map_provider is not None and hasattr(map_provider, "prefetch_all_data"):
        dvt_prefetch_radius_m = float(getattr(rf_params, "max_range_m", 20000.0) or 20000.0) + 50.0
        progress("osm_aoi", f"Checking local OSM AOI cache for {dvt_prefetch_radius_m:.0f} m")
        map_provider.prefetch_all_data(snapped.latlon, dvt_prefetch_radius_m)
        acquisition = getattr(map_provider, "acquisition_metadata", {}) or {}
        if acquisition.get("cache_hit"):
            progress("osm_aoi", "OSM AOI loaded from the local SQLite cache")
        else:
            progress(
                "osm_aoi",
                f"OSM AOI acquired and cached ({int(acquisition.get('logical_overpass_queries', 0))} logical Overpass queries)",
            )

    # Get clutter classification (non-blocking, can be slow)
    logger.info("STEP 2.1: Getting clutter classification...")
    progress("clutter", "Classifying local clutter around TX")
    clutter_type = "unknown"
    try:
        clutter_type = map_provider.get_clutter_type(snapped.latlon, radius_m=200.0)
        logger.info(f"✓ Clutter type: {clutter_type}")
        progress("clutter", f"Clutter classified as {clutter_type}")
    except Exception as e:
        logger.warning(f"  Could not get clutter type (non-critical): {e}")
        clutter_type = "unknown"
        progress("clutter", "Clutter classification unavailable; continuing")

    # 3) OPTIONAL: Street View refinement is not part of the DVT broadcast path.
    pano = None
    streetview_available = False
    pano_location = None

    if is_dvt:
        streetview_provider = None
        progress("streetview", "DVT: street-level imagery skipped")
        logger.info("STEP 3: DVT broadcast planning - skipping Street View and panorama refinement")
    elif streetview_provider is None:
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

    if (not is_dvt) and streetview_provider is not None:
        progress("streetview", "Checking nearby street-level imagery")
        logger.debug("Checking Street View availability (optional refinement)...")
        try:
            streetview_available = streetview_provider.check_availability(
                snapped.latlon.lat, snapped.latlon.lon
            )
            logger.info(f"Street View available (radius {streetview_provider.radius_m}m): {streetview_available}")
            progress(
                "streetview",
                "Street-level imagery available" if streetview_available else "Street-level imagery not available"
            )

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
                    progress("streetview", "Fetching panorama for optional refinement")
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
        progress("vlm", "Running VLM material refinement on panorama tiles")
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
            progress("vlm", f"VLM material refinement complete ({len(views)} view tiles)")
        except Exception as e:
            logger.warning(f"VLM analysis failed (non-critical): {e}")
            progress("vlm", "VLM refinement failed; continuing with geometry-only model")
    else:
        if vlm is None:
            logger.debug("No VLM provided, using geometry-only model")
        if pano is None:
            logger.debug("No panorama available, using geometry-only model")
        progress("vlm", "Using geometry-only model")

    # 5) Build world model (PRIMARY: geometry, SECONDARY: VLM refinement if available)
    progress("world_model", "Building world model and coverage candidates")
    logger.debug("Building world model from geometry...")
    terrain_provider = None
    if bool(getattr(rf_params, "terrain_enabled", True)):
        from ..geo.terrain_provider import create_terrain_provider, create_terrain_provider_with_mesh
        from ..geo.google_mesh import MeshProfileStore
        from ..geo.google_mesh.profile_types import PROFILE_VERSION

        _dem_source = str(getattr(rf_params, "dem_source", "") or "").strip().lower()

        if _dem_source == "copernicus_mesh":
            _mesh_profile_set = None
            try:
                _store = MeshProfileStore()
                _mesh_profile_set = _store.get(
                    tx=snapped.latlon,
                    tx_height_m=tx_height_m,
                    rx_height_m=rx_height_m,
                    max_range_m=float(rf_params.max_range_m),
                    dr_m=float(rf_params.step_m),
                    dtheta_deg=float(getattr(rf_params, "dtheta_deg", 5.0)),
                    version=PROFILE_VERSION,
                )
            except Exception as _e:
                logger.debug("Mesh profile lookup failed (non-critical): %s", _e)

            _has_mesh_terrain = (
                _mesh_profile_set is not None
                and any(p.terrain_heights for p in _mesh_profile_set.profiles)
            )

            if _has_mesh_terrain:
                terrain_provider = create_terrain_provider_with_mesh(rf_params, _mesh_profile_set)
                if terrain_provider is not None:
                    n_profiles = sum(1 for p in _mesh_profile_set.profiles if p.terrain_heights)
                    logger.info("Terrain: Google mesh (%d bearing profiles) + Copernicus fallback", n_profiles)
                    progress("terrain", f"Google mesh terrain active ({n_profiles} bearing profiles) + Copernicus fallback")
            else:
                terrain_provider = create_terrain_provider(rf_params)
                if terrain_provider is not None:
                    logger.info("Terrain: Copernicus 30m (mesh profiles not yet generated)")
                    progress("terrain", "Loading DEM for terrain-aware propagation")
        else:
            terrain_provider = create_terrain_provider(rf_params)
            if terrain_provider is not None:
                progress("terrain", "Loading DEM for terrain-aware propagation")
    world_stage_started = perf_counter()
    world = build_world_model(
        tx=snapped.latlon,
        rf_params=rf_params,
        views=views,
        map_provider=map_provider,
        terrain_provider=terrain_provider,
    )
    stage_timings_s["physical_world"] = perf_counter() - world_stage_started
    num_world_cells = len(world.cells)
    logger.info("Built world model with %d compact cells", num_world_cells)
    progress("world_model", f"Physical world built ({num_world_cells} candidate points)")
    rss_world = _current_rss_mb()
    if rss_world is not None:
        memory_snapshots_mb["after_physical_world"] = round(rss_world, 2)

    # 5) Complete the one-way waveform coverage before any bistatic work.
    is_dvt_plan = _is_dvt(rf_params)
    if is_dvt_plan:
        forward_detail = "Computing broadcast received power, field strength, and C/N"
    else:
        forward_detail = "Computing NR RSRP, serving cell, interference, and SINR"
    progress("forward_coverage", forward_detail)
    logger.debug("%s...", forward_detail)
    forward_started = perf_counter()
    grid = compute_attenuation_grid(world, include_channel_analysis=False)
    stage_timings_s["forward_coverage"] = perf_counter() - forward_started
    logger.info("Computed forward coverage grid with %d points", len(grid.cell_lat))
    progress("forward_coverage", f"Forward coverage complete ({len(grid.cell_lat)} points)")

    # The forward grid now owns every value needed by channel analysis.  Release
    # the large WorldCell collection before constructing the receiver-centered
    # reciprocal environment so both worlds are never resident together.
    released_forward_cells = len(world.cells)
    world.cells.clear()
    rss_after_release = _current_rss_mb()
    if rss_after_release is not None:
        memory_snapshots_mb["after_forward_world_release"] = round(rss_after_release, 2)

    if rf_params.channel_analysis is not None:
        return_started = perf_counter()
        return_lookup = _build_channel_return_path_lookup(
            forward_world=world,
            map_provider=map_provider,
            terrain_provider=terrain_provider,
            progress=progress,
        )
        stage_timings_s["return_environment"] = perf_counter() - return_started
        rss_return = _current_rss_mb()
        if rss_return is not None:
            memory_snapshots_mb["after_return_environment"] = round(rss_return, 2)

        progress(
            "channel_analysis",
            "Evaluating per-target bistatic geometry, echo power, Doppler, and detectability",
        )
        channel_started = perf_counter()
        grid = apply_channel_analysis(
            world,
            grid,
            channel_context={"return_path_lookup": return_lookup},
        )
        stage_timings_s["channel_analysis"] = perf_counter() - channel_started
        del return_lookup
        progress("channel_analysis", "Bistatic/ISAC channel analysis complete")
        rss_channel = _current_rss_mb()
        if rss_channel is not None:
            memory_snapshots_mb["after_channel_analysis"] = round(rss_channel, 2)

    progress("attenuation", f"RF products complete ({len(grid.cell_lat)} output points)")

    # 6) heatmap / map overlay
    ray_mode_eff2 = str(getattr(rf_params, "ray_mode", ray_mode) or ray_mode).strip().lower()

    try:
        step_m = float(getattr(rf_params, "step_m", 5.0) or 5.0)
    except Exception:
        step_m = 5.0
    base = (2.0 * float(rf_params.max_range_m)) / max(5.0, step_m)
    # A 40 km diameter at 20 m output spacing needs ~2000 pixels to retain the
    # requested display resolution. Existing 5G texture limits remain unchanged.
    texture_cap = 2048 if is_dvt_plan else 1024
    tex_size = int(min(texture_cap, max(512, round(base))))
    # 2D OSM and 3D modes share one prepared raster geometry.  Every
    # coverage/ISAC layer reuses the same point-to-pixel map and support mask.
    rendering_started = perf_counter()
    logger.debug("Preparing shared ellipse rasterizer (size=%d, ray_mode=%s)", tex_size, ray_mode_eff2)
    progress("heatmap", f"Preparing shared heatmap geometry ({tex_size} px)")
    rasterizer = EllipseRasterizer(grid, size=tex_size)

    def render_layer(
        values: Any,
        *,
        vmin: float,
        vmax: float,
        layer: str,
        units: str,
        warning_label: str,
    ) -> Optional[Dict[str, Any]]:
        if not _has_values(values):
            return None
        try:
            payload = rasterizer.render(values, vmin=vmin, vmax=vmax)
            payload["layer"] = layer
            payload["units"] = units
            return payload
        except Exception as exc:
            logger.warning("%s heatmap PNG failed: %s", warning_label, exc)
            return None

    if is_dvt_plan and _has_values(grid.field_strength_dbuv_m):
        heatmap_payload = render_layer(
            grid.field_strength_dbuv_m,
            vmin=20.0,
            vmax=120.0,
            layer="field_strength_dbuv_m",
            units="dBuV/m",
            warning_label="DVT field-strength",
        )
    else:
        heatmap_payload = render_layer(
            grid.rsrp_dbm,
            vmin=-140.0,
            vmax=-60.0,
            layer="rsrp_dbm",
            units="dBm",
            warning_label="RSRP",
        )
    if heatmap_payload is None:
        raise ValueError("primary coverage heatmap could not be rendered")

    heatmap_by_sector: Dict[str, Any] = {}
    if grid.rsrp_by_sector:
        for sid, values in grid.rsrp_by_sector.items():
            payload = render_layer(
                values,
                vmin=-140.0,
                vmax=-60.0,
                layer="rsrp_dbm",
                units="dBm",
                warning_label=f"sector {sid}",
            )
            if payload is not None:
                heatmap_by_sector[str(sid)] = payload

    heatmap_received_power = render_layer(
        grid.received_power_dbm if is_dvt_plan else None,
        vmin=-140.0,
        vmax=-20.0,
        layer="received_power_dbm",
        units="dBm",
        warning_label="received-power",
    )
    heatmap_incident_power = render_layer(
        grid.incident_power_isotropic_dbm,
        vmin=-160.0,
        vmax=-20.0,
        layer="incident_power_isotropic_dbm",
        units="dBm",
        warning_label="incident-power",
    )
    heatmap_bistatic_echo = render_layer(
        grid.bistatic_echo_power_dbm,
        vmin=-200.0,
        vmax=-80.0,
        layer="bistatic_echo_power_dbm",
        units="dBm",
        warning_label="bistatic echo",
    )
    heatmap_bistatic_snr = render_layer(
        grid.bistatic_postprocessing_snr_db,
        vmin=-40.0,
        vmax=30.0,
        layer="bistatic_postprocessing_snr_db",
        units="dB",
        warning_label="bistatic SNR",
    )
    heatmap_bistatic_margin = render_layer(
        grid.bistatic_detection_margin_db,
        vmin=-40.0,
        vmax=20.0,
        layer="bistatic_detection_margin_db",
        units="dB",
        warning_label="bistatic margin",
    )

    heatmap_bistatic_doppler = None
    if _has_values(grid.bistatic_doppler_hz):
        doppler_values = np.asarray(grid.bistatic_doppler_hz, dtype=np.float32)
        finite = np.isfinite(doppler_values)
        doppler_limit = float(np.max(np.abs(doppler_values[finite]))) if np.any(finite) else 1.0
        doppler_limit = max(doppler_limit, 1.0)
        heatmap_bistatic_doppler = render_layer(
            doppler_values,
            vmin=-doppler_limit,
            vmax=doppler_limit,
            layer="bistatic_doppler_hz",
            units="Hz",
            warning_label="bistatic Doppler",
        )

    heatmap_bistatic_excess_delay = None
    if _has_values(grid.bistatic_excess_delay_s):
        delay_us = np.asarray(grid.bistatic_excess_delay_s, dtype=np.float32) * np.float32(1.0e6)
        heatmap_bistatic_excess_delay = render_layer(
            delay_us,
            vmin=0.0,
            vmax=max(float(np.nanmax(delay_us)), 1.0),
            layer="bistatic_excess_delay_us",
            units="us",
            warning_label="bistatic excess-delay",
        )

    def minmax(values: Any, default_min: float, default_max: float) -> tuple[float, float]:
        if not _has_values(values):
            return default_min, default_max
        arr = np.asarray(values, dtype=np.float32)
        finite = arr[np.isfinite(arr)]
        if finite.size == 0:
            return default_min, default_max
        low = float(np.min(finite))
        high = float(np.max(finite))
        return (low, high if high > low else low + 1.0)

    return_min, return_max = minmax(grid.bistatic_return_path_loss_db, 60.0, 180.0)
    heatmap_bistatic_return_path_loss = render_layer(
        grid.bistatic_return_path_loss_db,
        vmin=return_min,
        vmax=return_max,
        layer="bistatic_return_path_loss_db",
        units="dB",
        warning_label="bistatic return-path-loss",
    )
    total_min, total_max = minmax(grid.bistatic_total_path_loss_db, 120.0, 300.0)
    heatmap_bistatic_total_path_loss = render_layer(
        grid.bistatic_total_path_loss_db,
        vmin=total_min,
        vmax=total_max,
        layer="bistatic_total_path_loss_db",
        units="dB",
        warning_label="bistatic total-path-loss",
    )
    heatmap_isac_quality = render_layer(
        grid.bistatic_isac_quality_code,
        vmin=0.0,
        vmax=5.0,
        layer="bistatic_isac_quality_code",
        units="class",
        warning_label="ISAC quality",
    )
    if heatmap_isac_quality is not None:
        heatmap_isac_quality["legend"] = (grid.channel_analysis_summary or {}).get(
            "isac_quality", {}
        ).get("code_legend", {})

    heatmap_bistatic_path_range = None
    if _has_values(grid.bistatic_path_range_m):
        path_km = np.asarray(grid.bistatic_path_range_m, dtype=np.float32) / np.float32(1000.0)
        path_min, path_max = minmax(path_km, 0.0, 1.0)
        heatmap_bistatic_path_range = render_layer(
            path_km,
            vmin=path_min,
            vmax=path_max,
            layer="bistatic_path_range_km",
            units="km",
            warning_label="bistatic path-range",
        )
    heatmap_bistatic_angle = render_layer(
        grid.bistatic_angle_deg,
        vmin=0.0,
        vmax=180.0,
        layer="bistatic_angle_deg",
        units="deg",
        warning_label="bistatic angle",
    )
    heatmap_bistatic_detectable = None
    if _has_values(grid.bistatic_detectable):
        heatmap_bistatic_detectable = render_layer(
            np.asarray(grid.bistatic_detectable, dtype=np.float32),
            vmin=0.0,
            vmax=1.0,
            layer="bistatic_detectable",
            units="flag",
            warning_label="bistatic detectability",
        )

    terrain_cap = float(getattr(rf_params, "terrain_loss_cap_db", 40.0) or 40.0)
    heatmap_terrain = render_layer(
        grid.terrain_loss_db,
        vmin=0.0,
        vmax=terrain_cap,
        layer="terrain_loss_db",
        units="dB",
        warning_label="terrain loss",
    )
    heatmap_sinr = render_layer(
        grid.sinr_db,
        vmin=-5.0,
        vmax=30.0,
        layer="carrier_to_noise_db" if is_dvt_plan else "sinr_db",
        units="dB",
        warning_label="C/N" if is_dvt_plan else "SINR",
    )

    stage_timings_s["heatmap_rendering"] = perf_counter() - rendering_started
    logger.info(
        "Rendered shared heatmap set in %.3f s (%dx%d)",
        stage_timings_s["heatmap_rendering"],
        heatmap_payload.get("width"),
        heatmap_payload.get("height"),
    )
    progress("heatmap", "Coverage and ISAC heatmaps rendered")

    sectors_info: List[Dict[str, Any]] = []
    broadcast_antenna: Optional[Dict[str, Any]] = None
    if is_dvt_plan and rf_params.dvt is not None:
        # DVT has one broadcast antenna radiation system. It is not exposed as
        # a sector, serving cell, sector overlay, or sector heatmap.
        broadcast_antenna = rf_params.dvt.antenna.public_description()
        broadcast_antenna["power"] = rf_params.dvt.power.model_dump(by_alias=True)
    elif rf_params.sectors:
        for sector_dict in rf_params.sectors:
            sc = SectorConfig(**sector_dict)
            sectors_info.append(sc.model_dump())
    else:
        sectors_info.append(create_omnidirectional_sector(rf_params).model_dump())

    # Prepare response

    # Reduce payload size for 3D OSM-only mode (terrain or not — same PNG drape path).
    compact_setting = getattr(rf_params, "compact_output", None)
    compact_large_result = (
        compact_setting is not False
        and (is_dvt_plan or grid.channel_analysis_summary is not None)
        and (compact_setting is True or len(grid.cell_lat) > 100000)
    )
    compact_grid = ray_mode_eff2 in ("3d_osm", "3d-osm", "osm3d") or compact_large_result
    compact_fields = {
        "cell_lat", "cell_lon", "rsrp_dbm", "received_power_dbm",
        "field_strength_dbuv_m", "carrier_to_noise_db", "sinr_db",
        "source_eirp_at_target_dbm", "tx_target_path_loss_db",
        "tx_target_environment_excess_db", "tx_target_penetration_loss_db",
        "tx_target_shadow_loss_db", "tx_target_diffraction_loss_db",
        "tx_target_canyon_recovery_db", "tx_target_horizontal_pattern_loss_db",
        "tx_target_vertical_pattern_loss_db", "tx_target_obstacles_count",
        "tx_target_propagation_mode",
        "incident_power_isotropic_dbm", "bistatic_echo_power_dbm",
        "bistatic_preprocessing_snr_db", "bistatic_postprocessing_snr_db",
        "bistatic_detection_margin_db", "bistatic_echo_to_residual_direct_db",
        "bistatic_required_dynamic_range_db", "bistatic_tx_target_range_m",
        "bistatic_target_receiver_range_m", "bistatic_return_path_loss_db",
        "bistatic_return_environment_excess_db", "bistatic_total_path_loss_db",
        "bistatic_path_range_m", "bistatic_excess_path_range_m", "bistatic_excess_delay_s",
        "bistatic_angle_deg", "bistatic_path_range_rate_mps",
        "bistatic_closing_speed_mps", "bistatic_doppler_hz",
        "bistatic_doppler_resolved", "bistatic_doppler_ambiguous",
        "bistatic_detectable", "bistatic_isac_quality_code",
        "modulation", "throughput_mbps",
        "serving_sector_id", "interferer_count", "top_interferer_rsrp_dbm",
        "pilot_pollution_metric_db", "rsrp_by_sector", "terrain_loss_db",
        "los_terrain", "terrain_state", "z_ground_m",
    }
    # Excluding before model_dump avoids constructing a second multi-million-item
    # copy only to delete it immediately afterward.
    grid_payload = grid.model_dump(exclude=compact_fields if compact_grid else None)
    grid_payload["rf_params"] = rf_params.public_config()
    if is_dvt_plan:
        # Internal arrays retain legacy names for solver compatibility.  Public
        # DVT output uses carrier/field-strength terminology only.
        grid_payload.pop("rsrp_dbm", None)
        grid_payload.pop("sinr_db", None)
        grid_payload.pop("modulation", None)
        grid_payload.pop("throughput_mbps", None)
        grid_payload.pop("serving_sector_id", None)
        grid_payload.pop("interferer_count", None)
        grid_payload.pop("top_interferer_rsrp_dbm", None)
        grid_payload.pop("pilot_pollution_metric_db", None)
        grid_payload.pop("rsrp_by_sector", None)
        if not compact_grid:
            carrier_values = (
                grid.carrier_to_noise_db
                if _has_values(grid.carrier_to_noise_db)
                else grid.sinr_db
            )
            grid_payload["carrier_to_noise_db"] = np.asarray(carrier_values).tolist()
    if compact_grid:
        grid_payload["num_points"] = len(grid.cell_lat)
        grid_payload["compacted"] = True
        if is_dvt_plan:
            for key in (
                "cell_lat",
                "cell_lon",
                "received_power_dbm",
                "field_strength_dbuv_m",
                "carrier_to_noise_db",
                "source_eirp_at_target_dbm",
                "incident_power_isotropic_dbm",
                "tx_target_path_loss_db",
                "tx_target_environment_excess_db",
                "tx_target_penetration_loss_db",
                "tx_target_shadow_loss_db",
                "tx_target_diffraction_loss_db",
                "tx_target_canyon_recovery_db",
                "tx_target_horizontal_pattern_loss_db",
                "tx_target_vertical_pattern_loss_db",
                "tx_target_obstacles_count",
                "tx_target_propagation_mode",
                "bistatic_echo_power_dbm",
                "bistatic_preprocessing_snr_db",
                "bistatic_postprocessing_snr_db",
                "bistatic_detection_margin_db",
                "bistatic_echo_to_residual_direct_db",
                "bistatic_required_dynamic_range_db",
                "bistatic_tx_target_range_m",
                "bistatic_target_receiver_range_m",
                "bistatic_return_path_loss_db",
                "bistatic_return_environment_excess_db",
                "bistatic_total_path_loss_db",
                "bistatic_path_range_m",
                "bistatic_excess_path_range_m",
                "bistatic_excess_delay_s",
                "bistatic_angle_deg",
                "bistatic_path_range_rate_mps",
                "bistatic_closing_speed_mps",
                "bistatic_doppler_hz",
                "bistatic_doppler_resolved",
                "bistatic_doppler_ambiguous",
                "bistatic_detectable",
                "bistatic_isac_quality_code",
                "terrain_loss_db",
                "los_terrain",
                "terrain_state",
                "z_ground_m",
            ):
                grid_payload[key] = []
        else:
            for k in compact_fields:
                grid_payload[k] = {} if k == "rsrp_by_sector" else []

    # Use the effective mode after provider init/fallbacks.
    effective_ray_mode = str(getattr(rf_params, "ray_mode", ray_mode) or ray_mode)

    building_area_sqm = None
    if map_provider is not None and hasattr(map_provider, "get_building_area_sqm"):
        try:
            building_area_sqm = map_provider.get_building_area_sqm(
                snapped.latlon, float(getattr(rf_params, "max_range_m", 2500.0) or 2500.0)
            )
        except Exception as e:
            logger.warning(f"Failed to compute building area: {e}")

    geometry_source: Dict[str, Any]
    if isinstance(map_provider, LocalOSMProvider):
        geometry_source = {
            "type": (
                "automatic_osm_aoi_cache"
                if isinstance(map_provider, AutoCachingOSMProvider)
                else "local_osm_sqlite"
            ),
            "database": str(map_provider.database_path),
            "loaded_buildings": len(getattr(map_provider, "_cached_buildings", []) or []),
            "loaded_landuse": len(getattr(map_provider, "_cached_landuse", []) or []),
        }
        if isinstance(map_provider, AutoCachingOSMProvider):
            geometry_source["acquisition"] = map_provider.acquisition_metadata
    else:
        geometry_source = {"type": type(map_provider).__name__ if map_provider is not None else "none"}

    channel_analysis_product: Optional[Dict[str, Any]] = None
    if grid.channel_analysis_summary is not None:
        product_started = perf_counter()
        try:
            channel_analysis_product = write_channel_product(
                arrays=_channel_product_arrays(grid),
                metadata={
                    "schema": "agentic_rf_planner.channel_analysis_grid",
                    "schema_version": "2.0",
                    "summary": grid.channel_analysis_summary,
                    "array_dimensions": {"point_axis": 0, "point_count": len(grid.cell_lat)},
                    "array_units": {
                        "latitude_deg": "degree", "longitude_deg": "degree",
                        "ground_elevation_m_amsl": "m", "terrain_loss_db": "dB",
                        "terrain_los": "flag", "terrain_state": "category",
                        "rsrp_dbm": "dBm", "sinr_db": "dB", "received_power_dbm": "dBm",
                        "field_strength_dbuv_m": "dBuV/m", "carrier_to_noise_db": "dB",
                        "modulation": "category", "throughput_mbps": "Mbit/s",
                        "serving_sector_id": "identifier", "interferer_count": "count",
                        "top_interferer_rsrp_dbm": "dBm", "pilot_pollution_metric_db": "dB",
                        "source_eirp_at_target_dbm": "dBm",
                        "incident_isotropic_power_dbm": "dBm", "tx_target_path_loss_db": "dB",
                        "tx_target_environment_excess_db": "dB",
                        "tx_target_penetration_loss_db": "dB",
                        "tx_target_shadow_loss_db": "dB",
                        "tx_target_diffraction_loss_db": "dB",
                        "tx_target_canyon_recovery_db": "dB",
                        "tx_target_horizontal_pattern_loss_db": "dB",
                        "tx_target_vertical_pattern_loss_db": "dB",
                        "tx_target_obstacles_count": "count",
                        "tx_target_propagation_mode": "category",
                        "echo_power_dbm": "dBm", "preprocessing_snr_db": "dB",
                        "postprocessing_snr_db": "dB", "detection_margin_db": "dB",
                        "echo_to_residual_direct_db": "dB", "required_dynamic_range_db": "dB",
                        "tx_target_range_m": "m", "target_receiver_range_m": "m",
                        "return_path_loss_db": "dB", "return_environment_excess_db": "dB",
                        "total_bistatic_path_loss_db": "dB", "bistatic_path_range_m": "m",
                        "excess_path_range_m": "m", "excess_delay_s": "s",
                        "bistatic_angle_deg": "degree", "path_range_rate_mps": "m/s",
                        "closing_speed_mps": "m/s", "doppler_hz": "Hz",
                        "doppler_resolved": "flag", "doppler_ambiguous": "flag",
                        "detectable": "flag", "isac_quality_code": "class"
                    },
                    "rf_config": rf_params.public_config(),
                    "geometry_source": geometry_source,
                },
            )
            grid.channel_analysis_summary["data_product"] = channel_analysis_product
            logger.info(
                "Channel-analysis NPZ product written: %s (%d bytes)",
                channel_analysis_product["product_id"],
                channel_analysis_product["size_bytes"],
            )
        except Exception as e:
            logger.exception("Failed to write channel-analysis NPZ product: %s", e)
            grid.channel_analysis_summary["data_product_error"] = str(e)
        finally:
            stage_timings_s["channel_product_export"] = perf_counter() - product_started

    total_elapsed_s = perf_counter() - pipeline_started
    stage_timings_s["total_to_response"] = total_elapsed_s
    rss_before_response = _current_rss_mb()
    if rss_before_response is not None:
        memory_snapshots_mb["before_response"] = round(rss_before_response, 2)
    pipeline_metrics = {
        "stage_seconds": {
            name: round(value, 4) for name, value in stage_timings_s.items()
        },
        "memory_rss_mb": memory_snapshots_mb,
        "candidate_world_cells": num_world_cells,
        "forward_world_cells_released_before_return_path": released_forward_cells,
        "output_points": len(grid.cell_lat),
        "compact_response": bool(compact_grid),
        "heatmap_size_px": tex_size,
        "world_cell_representation": "slotted_dataclass",
        "channel_numeric_representation": "numpy_float32",
        "shared_heatmap_rasterizer": True,
    }

    result = {
        "original_point": {"lat": lat, "lon": lon},
        "snapped_tx": snapped.latlon.model_dump(),
        "snap_distance_m": snapped.distance_m,
        "ray_mode": effective_ray_mode,
        "requested_ray_mode": str(requested_ray_mode),
        "tx_height_m": tx_height_m,
        "rx_height_m": rx_height_m,
        "clutter_type": clutter_type,
        "geometry_source": geometry_source,
        "world_model_source": "geometry_only" if not vlm_used else "geometry_vlm_refined",
        "streetview_available": streetview_available,
        "vlm_used": vlm_used,
        "rf_config_used": rf_params.public_config(),
        "grid": grid_payload,
        "heatmap": heatmap_payload,
        "building_area_sqm": building_area_sqm,
        "pipeline_metrics": pipeline_metrics,
    }
    if is_dvt_plan:
        result["broadcast_antenna"] = broadcast_antenna
    else:
        result["sectors"] = sectors_info
        result["heatmap_by_sector"] = heatmap_by_sector if heatmap_by_sector else None
    if grid.channel_analysis_summary is not None:
        result["channel_analysis"] = grid.channel_analysis_summary
    if channel_analysis_product is not None:
        result["channel_analysis_product"] = channel_analysis_product
    if heatmap_terrain:
        result["heatmap_terrain"] = heatmap_terrain
    if heatmap_sinr:
        result["heatmap_carrier_to_noise" if is_dvt_plan else "heatmap_sinr"] = heatmap_sinr
    if heatmap_received_power:
        result["heatmap_received_power"] = heatmap_received_power
    if heatmap_incident_power:
        result["heatmap_incident_power"] = heatmap_incident_power
    if heatmap_bistatic_echo:
        result["heatmap_bistatic_echo"] = heatmap_bistatic_echo
    if heatmap_bistatic_snr:
        result["heatmap_bistatic_snr"] = heatmap_bistatic_snr
    if heatmap_bistatic_margin:
        result["heatmap_bistatic_margin"] = heatmap_bistatic_margin
    if heatmap_bistatic_doppler:
        result["heatmap_bistatic_doppler"] = heatmap_bistatic_doppler
    if heatmap_bistatic_excess_delay:
        result["heatmap_bistatic_excess_delay"] = heatmap_bistatic_excess_delay
    if heatmap_bistatic_path_range:
        result["heatmap_bistatic_path_range"] = heatmap_bistatic_path_range
    if heatmap_bistatic_angle:
        result["heatmap_bistatic_angle"] = heatmap_bistatic_angle
    if heatmap_bistatic_detectable:
        result["heatmap_bistatic_detectable"] = heatmap_bistatic_detectable
    if heatmap_bistatic_return_path_loss:
        result["heatmap_bistatic_return_path_loss"] = heatmap_bistatic_return_path_loss
    if heatmap_bistatic_total_path_loss:
        result["heatmap_bistatic_total_path_loss"] = heatmap_bistatic_total_path_loss
    if heatmap_isac_quality:
        result["heatmap_isac_quality"] = heatmap_isac_quality

    if bool(getattr(rf_params, "terrain_enabled", True)):
        result["terrain"] = {
            "enabled": True,
            "dem_provider": getattr(terrain_provider, "provider_used", None) if terrain_provider else None,
            "z_tx_ground_m": getattr(world, "z_tx_ground_m", None),
            "z_tx_abs_m": getattr(world, "z_tx_abs_m", None),
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
            "num_cells": num_world_cells,
            "materials_detected": len(views),
        }

    if str(effective_ray_mode).strip().lower() == "2d":
        ob = _osm_buildings_for_client_from_map_provider(map_provider)
        if ob is not None:
            result["osm_buildings_for_client"] = ob

    progress("complete", "RF planning complete")

    return result
