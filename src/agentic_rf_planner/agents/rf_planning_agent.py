"""Main orchestrator for RF planning pipeline."""

import base64
import io
import logging
import math
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
from ..rf.reciprocal_propagation import build_reciprocal_propagation_field, build_target_illumination_field
from ..rf.channel_products import write_channel_product
from ..rf.background_scatter import build_static_background_channel, attach_static_background_summary
from ..rf.channel_arrays import LARGE_ARRAY_THRESHOLD_POINTS
from ..rf.sector_config import SectorConfig, create_omnidirectional_sector
from ..geo.heatmap import attenuation_grid_to_png_ellipse, prepare_ellipse_heatmap_geometry

logger = logging.getLogger(__name__)


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
    # Do not duplicate the potentially very large cached-building reference list.
    # The response builder either consumes this cache directly or omits it for
    # large compact plans.
    buildings = getattr(osm, "_cached_buildings", None) or []
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


def _grid_series(grid: AttenuationGrid, name: str) -> Any:
    """Read a numerical layer from compact internal storage or its public field."""

    return grid.channel_array(name)


def _has_series(values: Any) -> bool:
    return values is not None and len(values) > 0


def _grid_point_count(grid: AttenuationGrid) -> int:
    values = _grid_series(grid, "cell_lat")
    return len(values) if values is not None else 0


def _compact_internal_grid_storage(grid: AttenuationGrid) -> None:
    """Move large solver lists into compact arrays and release Python containers.

    The API already compacts large DVT/ISAC responses, so retaining millions of
    Python float/bool/string objects after attenuation only increases peak memory.
    Aliased DVT layers reuse the same ndarray instead of being copied twice.
    """

    float64_fields = {"cell_lat", "cell_lon"}
    bool_fields = {"los_terrain"}
    numeric_fields = (
        "cell_lat", "cell_lon", "rsrp_dbm", "sinr_db", "received_power_dbm",
        "field_strength_dbuv_m", "carrier_to_noise_db", "incident_power_isotropic_dbm",
        "terrain_loss_db", "z_ground_m",
    )
    array_by_source_id: dict[int, np.ndarray] = {}
    for name in (*numeric_fields, *bool_fields):
        current = grid.channel_array(name)
        if not _has_series(current):
            continue
        source_id = id(current)
        arr = array_by_source_id.get(source_id)
        if arr is None:
            dtype = np.float64 if name in float64_fields else np.bool_ if name in bool_fields else np.float32
            arr = np.asarray(current, dtype=dtype)
            array_by_source_id[source_id] = arr
        grid.set_channel_array(name, arr)

    # Replace, do not mutate, because DVT aliases can point to the same source list.
    required_list_fields = {
        "cell_lat", "cell_lon", "rsrp_dbm", "sinr_db", "modulation",
        "throughput_mbps", "serving_sector_id", "interferer_count",
        "top_interferer_rsrp_dbm", "pilot_pollution_metric_db",
    }
    for name in (
        *numeric_fields,
        *bool_fields,
        "terrain_state", "modulation", "throughput_mbps", "serving_sector_id",
        "interferer_count", "top_interferer_rsrp_dbm", "pilot_pollution_metric_db",
    ):
        if hasattr(grid, name):
            setattr(grid, name, [] if name in required_list_fields else None)


def _channel_product_arrays(grid: AttenuationGrid) -> Dict[str, Any]:
    """Return stable, documented per-target arrays for the NPZ data product."""

    return {
        "latitude_deg": _grid_series(grid, "cell_lat"),
        "longitude_deg": _grid_series(grid, "cell_lon"),
        # Primary one-way communication / illumination field.
        "rsrp_dbm": _grid_series(grid, "rsrp_dbm") if grid.technology != "dvt" else None,
        "sinr_db": _grid_series(grid, "sinr_db") if grid.technology != "dvt" else None,
        "received_power_dbm": _grid_series(grid, "received_power_dbm"),
        "field_strength_dbuv_m": _grid_series(grid, "field_strength_dbuv_m"),
        "carrier_to_noise_db": _grid_series(grid, "carrier_to_noise_db"),
        "tx_terrain_loss_db": _grid_series(grid, "terrain_loss_db"),
        "tx_los_terrain": _grid_series(grid, "los_terrain"),
        "z_ground_m": _grid_series(grid, "z_ground_m"),
        "incident_isotropic_power_dbm": (
            _grid_series(grid, "isac_incident_power_isotropic_dbm")
            if _grid_series(grid, "isac_incident_power_isotropic_dbm") is not None
            else _grid_series(grid, "incident_power_isotropic_dbm")
        ),
        "tx_target_path_loss_db": _grid_series(grid, "isac_tx_target_path_loss_db"),
        "tx_target_environment_loss_db": _grid_series(grid, "isac_tx_target_environment_loss_db"),
        "tx_target_terrain_loss_db": _grid_series(grid, "isac_tx_target_terrain_loss_db"),
        "tx_target_los": _grid_series(grid, "isac_tx_target_los"),
        "tx_target_sample_error_m": _grid_series(grid, "isac_tx_target_sample_error_m"),
        # Reusable ISAC scene basis: target RCS/motion/processing changes do not
        # require rebuilding OSM/terrain/propagation.
        "echo_geometry_base_dbm": _grid_series(grid, "isac_echo_geometry_base_dbm"),
        "doppler_east_hz_per_mps": _grid_series(grid, "bistatic_doppler_east_hz_per_mps"),
        "doppler_north_hz_per_mps": _grid_series(grid, "bistatic_doppler_north_hz_per_mps"),
        "doppler_up_hz_per_mps": _grid_series(grid, "bistatic_doppler_up_hz_per_mps"),
        "doppler_sensitivity_hz_per_mps": _grid_series(grid, "bistatic_doppler_sensitivity_hz_per_mps"),
        "motion_doppler_sensitivity_hz_per_mps": _grid_series(grid, "bistatic_motion_doppler_sensitivity_hz_per_mps"),
        "minimum_detectable_speed_mps": _grid_series(grid, "bistatic_minimum_detectable_speed_mps"),
        "echo_power_dbm": _grid_series(grid, "bistatic_echo_power_dbm"),
        "preprocessing_snr_db": _grid_series(grid, "bistatic_preprocessing_snr_db"),
        "postprocessing_snr_db": _grid_series(grid, "bistatic_postprocessing_snr_db"),
        "detection_margin_db": _grid_series(grid, "bistatic_detection_margin_db"),
        "echo_to_residual_direct_db": _grid_series(grid, "bistatic_echo_to_residual_direct_db"),
        "direct_residual_margin_db": _grid_series(grid, "bistatic_direct_residual_margin_db"),
        "required_cancellation_db": _grid_series(grid, "bistatic_required_cancellation_db"),
        "required_dynamic_range_db": _grid_series(grid, "bistatic_required_dynamic_range_db"),
        "dynamic_range_margin_db": _grid_series(grid, "bistatic_dynamic_range_margin_db"),
        "minimum_detectable_rcs_m2": _grid_series(grid, "bistatic_minimum_detectable_rcs_m2"),
        "rcs_margin_db": _grid_series(grid, "bistatic_rcs_margin_db"),
        "tx_target_range_m": _grid_series(grid, "bistatic_tx_target_range_m"),
        "target_receiver_range_m": _grid_series(grid, "bistatic_target_receiver_range_m"),
        "bistatic_path_range_m": _grid_series(grid, "bistatic_path_range_m"),
        "excess_path_range_m": _grid_series(grid, "bistatic_excess_path_range_m"),
        "excess_delay_s": _grid_series(grid, "bistatic_excess_delay_s"),
        "bistatic_angle_deg": _grid_series(grid, "bistatic_angle_deg"),
        "path_range_rate_mps": _grid_series(grid, "bistatic_path_range_rate_mps"),
        "closing_speed_mps": _grid_series(grid, "bistatic_closing_speed_mps"),
        "doppler_hz": _grid_series(grid, "bistatic_doppler_hz"),
        "snr_noise_interference_ok": _grid_series(grid, "bistatic_snr_noise_interference_ok"),
        "doppler_resolved": _grid_series(grid, "bistatic_doppler_resolved"),
        "doppler_ambiguous": _grid_series(grid, "bistatic_doppler_ambiguous"),
        "direct_residual_ok": _grid_series(grid, "bistatic_direct_residual_ok"),
        "dynamic_range_ok": _grid_series(grid, "bistatic_dynamic_range_ok"),
        "return_environment_valid": _grid_series(grid, "return_environment_valid"),
        "detectable_screening": _grid_series(grid, "bistatic_detectable_screening"),
        "detectable_qualified": _grid_series(grid, "bistatic_detectable_qualified"),
        "detectable": _grid_series(grid, "bistatic_detectable"),
        "constraint_failure_code": _grid_series(grid, "bistatic_constraint_failure_code"),
        "return_path_loss_db": _grid_series(grid, "return_path_loss_db"),
        "return_environment_loss_db": _grid_series(grid, "return_environment_loss_db"),
        "return_terrain_loss_db": _grid_series(grid, "return_terrain_loss_db"),
        "return_los": _grid_series(grid, "return_los"),
        "return_terrain_state_code": _grid_series(grid, "return_terrain_state_code"),
        "return_sample_error_m": _grid_series(grid, "return_sample_error_m"),
    }


def _iter_channel_product_arrays(grid: AttenuationGrid, *, release_after_write: bool = False):
    """Yield NPZ members in the historical order without retaining a second ref table.

    When ``release_after_write`` is true, each private solver array is removed from
    the grid only after the writer resumes the generator, i.e. after that member has
    been fully encoded. This keeps the external NPZ schema/values unchanged while
    making export a consuming final stage for compact large plans.
    """

    is_dvt = grid.technology == "dvt"
    specs = (
        ("latitude_deg", "cell_lat", True),
        ("longitude_deg", "cell_lon", True),
        ("rsrp_dbm", "rsrp_dbm", not is_dvt),
        ("sinr_db", "sinr_db", not is_dvt),
        ("received_power_dbm", "received_power_dbm", True),
        ("field_strength_dbuv_m", "field_strength_dbuv_m", True),
        ("carrier_to_noise_db", "carrier_to_noise_db", True),
        ("tx_terrain_loss_db", "terrain_loss_db", True),
        ("tx_los_terrain", "los_terrain", True),
        ("z_ground_m", "z_ground_m", True),
        ("tx_target_path_loss_db", "isac_tx_target_path_loss_db", True),
        ("tx_target_environment_loss_db", "isac_tx_target_environment_loss_db", True),
        ("tx_target_terrain_loss_db", "isac_tx_target_terrain_loss_db", True),
        ("tx_target_los", "isac_tx_target_los", True),
        ("tx_target_sample_error_m", "isac_tx_target_sample_error_m", True),
        ("echo_geometry_base_dbm", "isac_echo_geometry_base_dbm", True),
        ("doppler_east_hz_per_mps", "bistatic_doppler_east_hz_per_mps", True),
        ("doppler_north_hz_per_mps", "bistatic_doppler_north_hz_per_mps", True),
        ("doppler_up_hz_per_mps", "bistatic_doppler_up_hz_per_mps", True),
        ("doppler_sensitivity_hz_per_mps", "bistatic_doppler_sensitivity_hz_per_mps", True),
        ("motion_doppler_sensitivity_hz_per_mps", "bistatic_motion_doppler_sensitivity_hz_per_mps", True),
        ("minimum_detectable_speed_mps", "bistatic_minimum_detectable_speed_mps", True),
        ("echo_power_dbm", "bistatic_echo_power_dbm", True),
        ("preprocessing_snr_db", "bistatic_preprocessing_snr_db", True),
        ("postprocessing_snr_db", "bistatic_postprocessing_snr_db", True),
        ("detection_margin_db", "bistatic_detection_margin_db", True),
        ("echo_to_residual_direct_db", "bistatic_echo_to_residual_direct_db", True),
        ("direct_residual_margin_db", "bistatic_direct_residual_margin_db", True),
        ("required_cancellation_db", "bistatic_required_cancellation_db", True),
        ("required_dynamic_range_db", "bistatic_required_dynamic_range_db", True),
        ("dynamic_range_margin_db", "bistatic_dynamic_range_margin_db", True),
        ("minimum_detectable_rcs_m2", "bistatic_minimum_detectable_rcs_m2", True),
        ("rcs_margin_db", "bistatic_rcs_margin_db", True),
        ("tx_target_range_m", "bistatic_tx_target_range_m", True),
        ("target_receiver_range_m", "bistatic_target_receiver_range_m", True),
        ("bistatic_path_range_m", "bistatic_path_range_m", True),
        ("excess_path_range_m", "bistatic_excess_path_range_m", True),
        ("excess_delay_s", "bistatic_excess_delay_s", True),
        ("bistatic_angle_deg", "bistatic_angle_deg", True),
        ("path_range_rate_mps", "bistatic_path_range_rate_mps", True),
        ("closing_speed_mps", "bistatic_closing_speed_mps", True),
        ("doppler_hz", "bistatic_doppler_hz", True),
        ("snr_noise_interference_ok", "bistatic_snr_noise_interference_ok", True),
        ("doppler_resolved", "bistatic_doppler_resolved", True),
        ("doppler_ambiguous", "bistatic_doppler_ambiguous", True),
        ("direct_residual_ok", "bistatic_direct_residual_ok", True),
        ("dynamic_range_ok", "bistatic_dynamic_range_ok", True),
        ("return_environment_valid", "return_environment_valid", True),
        ("detectable_screening", "bistatic_detectable_screening", True),
        ("detectable_qualified", "bistatic_detectable_qualified", True),
        ("detectable", "bistatic_detectable", True),
        ("constraint_failure_code", "bistatic_constraint_failure_code", True),
        ("return_path_loss_db", "return_path_loss_db", True),
        ("return_environment_loss_db", "return_environment_loss_db", True),
        ("return_terrain_loss_db", "return_terrain_loss_db", True),
        ("return_los", "return_los", True),
        ("return_terrain_state_code", "return_terrain_state_code", True),
        ("return_sample_error_m", "return_sample_error_m", True),
    )

    # Historical position: incident power follows z_ground and precedes target-path fields.
    for position, (product_name, source_name, enabled) in enumerate(specs):
        if position == 10:
            source_name_incident = (
                "isac_incident_power_isotropic_dbm"
                if _has_series(_grid_series(grid, "isac_incident_power_isotropic_dbm"))
                else "incident_power_isotropic_dbm"
            )
            values = _grid_series(grid, source_name_incident)
            yield "incident_isotropic_power_dbm", values
            if release_after_write:
                grid._channel_arrays.pop(source_name_incident, None)
        if not enabled:
            yield product_name, None
            continue
        values = _grid_series(grid, source_name)
        yield product_name, values
        if release_after_write:
            grid._channel_arrays.pop(source_name, None)

    if release_after_write:
        # Release any non-product solver aliases left in the compact private store.
        grid.clear_channel_arrays()


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
    world = build_world_model(
        tx=snapped.latlon,
        rf_params=rf_params,
        views=views,
        map_provider=map_provider,
        terrain_provider=terrain_provider,
    )
    num_world_cells = len(world.cells)
    logger.info(f"Built world model with {num_world_cells} cells")
    progress("world_model", f"World model built ({num_world_cells} candidate cells)")

    # 5) RF attenuation and optional waveform-independent channel analysis
    is_dvt_plan = _is_dvt(rf_params)
    if is_dvt_plan:
        attenuation_detail = "Computing broadcast carrier power and field strength"
    else:
        attenuation_detail = "Computing NR RSRP, serving cell, interference, and SINR"
    if rf_params.channel_analysis is not None:
        attenuation_detail += "; evaluating bistatic range, echo, Doppler, and detectability"
    progress("attenuation", attenuation_detail)
    logger.debug("%s...", attenuation_detail)

    # Preserve the TX-centered OSM payload before an RX-centered reciprocal solve
    # updates the provider's active cache window.
    retain_primary_osm_for_client = not (
        is_dvt_plan
        and (
            getattr(rf_params, "compact_output", None) is True
            or float(getattr(rf_params, "max_range_m", 0.0) or 0.0) > 5000.0
        )
    )
    primary_osm_buildings_for_client = (
        _osm_buildings_for_client_from_map_provider(map_provider)
        if retain_primary_osm_for_client
        else None
    )

    reciprocal_field = None
    illumination_field = None
    static_background_channel = None
    channel_cfg = rf_params.channel_analysis
    if channel_cfg is not None:
        # Build static mapped-facade background while the provider still owns the
        # TX-centered OSM AOI. Reciprocal/target-height solves may move that cache
        # window. The result is a small reusable path table, not an N-cell field.
        progress("background_channel", "Computing mapped static-building specular background channel")
        try:
            static_background_channel = build_static_background_channel(
                map_provider=map_provider,
                tx=snapped.latlon,
                rf_params=rf_params,
                receiver=channel_cfg.receiver,
            )
            if static_background_channel is not None:
                progress(
                    "background_channel",
                    f"Static background complete ({len(static_background_channel.paths)} accepted facade paths)",
                )
        except Exception as exc:
            logger.exception("Static mapped-background channel failed: %s", exc)
            progress("background_channel", "Static mapped-building background unavailable; continuing")
            static_background_channel = None
    if channel_cfg is not None and str(channel_cfg.return_path_model) == "environment_reciprocal":
        # First calculate the ordinary one-way TX field.  Then calculate a second,
        # RX-centered propagation field over the same physical environment and fuse
        # both fields point-for-point in the bistatic layer.
        grid = compute_attenuation_grid(world, apply_channel=False)
        # The TX-centered WorldCell lattice has been reduced to the one-way grid.
        # Reciprocal propagation needs only TX/RF metadata from ``world``; retaining
        # millions of TX cells while constructing millions of RX cells nearly doubles
        # the environmental solver peak for no analytical benefit.
        world.cells.clear()
        progress("target_field", "Computing target-height TX-to-target illumination field")
        illumination_field = build_target_illumination_field(
            primary_world=world,
            primary_grid=grid,
            map_provider=map_provider,
            terrain_provider=terrain_provider,
        )
        progress(
            "target_field",
            f"Target-height TX field complete ({illumination_field.metadata.get('sample_count', 0)} samples)",
        )
        progress("return_field", "Computing environment-aware reciprocal target-to-RX field")
        reciprocal_field = build_reciprocal_propagation_field(
            primary_world=world,
            primary_grid=grid,
            map_provider=map_provider,
            terrain_provider=terrain_provider,
        )
        progress(
            "return_field",
            f"Reciprocal RX field complete ({reciprocal_field.metadata.get('sample_count', 0)} samples)",
        )
        grid = apply_channel_analysis(
            world, grid, reciprocal_field=reciprocal_field, illumination_field=illumination_field
        )
        attach_static_background_summary(grid, static_background_channel)
        illumination_field = None
        # apply_channel_analysis copies the aligned return-field values into the
        # grid's compact channel arrays. Drop the source field before rendering
        # heatmaps/export so both copies are not retained for the rest of the plan.
        reciprocal_field = None
    else:
        grid = compute_attenuation_grid(world)
        attach_static_background_summary(grid, static_background_channel)

    # The attenuation grid now owns all output values; release per-cell Pydantic
    # objects before raster encoding and response serialization.
    world.cells.clear()
    point_count = _grid_point_count(grid)
    compact_setting = getattr(rf_params, "compact_output", None)
    compact_large_result = (
        compact_setting is not False
        and (is_dvt_plan or grid.channel_analysis_summary is not None)
        and (compact_setting is True or point_count > LARGE_ARRAY_THRESHOLD_POINTS)
    )
    if compact_large_result:
        _compact_internal_grid_storage(grid)
    logger.info(f"Computed attenuation grid with {point_count} points")
    progress("attenuation", f"RF attenuation complete ({point_count} output points)")

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
    # 2D OSM and 3D modes share the same pre-colored ellipse PNG (local ENU → texture),
    # so Leaflet and Cesium both get a continuous drape instead of radial spoke circles.
    logger.debug(f"Generating ellipse heatmap PNG (size={tex_size}, ray_mode={ray_mode_eff2})...")
    progress("heatmap", f"Rendering heatmap texture ({tex_size} px)")
    heatmap_geometry = prepare_ellipse_heatmap_geometry(grid, size=tex_size)

    def render_ellipse(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        kwargs["geometry"] = heatmap_geometry
        return attenuation_grid_to_png_ellipse(*args, **kwargs)

    field_strength_values = _grid_series(grid, "field_strength_dbuv_m")
    if is_dvt_plan and _has_series(field_strength_values):
        heatmap_payload = render_ellipse(
            grid,
            size=tex_size,
            vmin=20.0,
            vmax=120.0,
            rsrp_values=field_strength_values,
        )
        heatmap_payload["layer"] = "field_strength_dbuv_m"
        heatmap_payload["units"] = "dBuV/m"
    else:
        heatmap_payload = render_ellipse(grid, size=tex_size, vmin=-140.0, vmax=-60.0)
        heatmap_payload["layer"] = "rsrp_dbm"
        heatmap_payload["units"] = "dBm"
    logger.info(f"Generated heatmap PNG texture: {heatmap_payload.get('width')}x{heatmap_payload.get('height')}")
    progress("heatmap", "Heatmap rendering complete")

    # Per-sector PNGs: true beam RSRP for each sector (not best-server / max across sectors at a point).
    heatmap_by_sector: Dict[str, Any] = {}
    if grid.rsrp_by_sector:
        for sid, rlist in grid.rsrp_by_sector.items():
            try:
                heatmap_by_sector[str(sid)] = render_ellipse(
                    grid,
                    size=tex_size,
                    vmin=-140.0,
                    vmax=-60.0,
                    rsrp_values=rlist,
                )
            except Exception as e:
                logger.warning("Per-sector heatmap failed for %s: %s", sid, e)
    if not is_dvt_plan:
        progress("heatmap", f"Per-sector rasters: {len(heatmap_by_sector)}")
    if compact_large_result:
        # Per-sector numeric grids are no longer needed after their PNGs are
        # rendered and are not part of the compact API/channel product.
        grid.rsrp_by_sector = None

    # Alternate layer PNGs (same ellipse drape as the primary coverage layer).
    heatmap_terrain: Optional[Dict[str, Any]] = None
    heatmap_sinr: Optional[Dict[str, Any]] = None
    heatmap_received_power: Optional[Dict[str, Any]] = None
    heatmap_incident_power: Optional[Dict[str, Any]] = None
    heatmap_bistatic_echo: Optional[Dict[str, Any]] = None
    heatmap_bistatic_snr: Optional[Dict[str, Any]] = None
    heatmap_bistatic_margin: Optional[Dict[str, Any]] = None
    heatmap_bistatic_doppler: Optional[Dict[str, Any]] = None
    heatmap_bistatic_excess_delay: Optional[Dict[str, Any]] = None
    heatmap_bistatic_path_range: Optional[Dict[str, Any]] = None
    heatmap_bistatic_angle: Optional[Dict[str, Any]] = None
    heatmap_bistatic_detectable: Optional[Dict[str, Any]] = None

    received_power_values = _grid_series(grid, "received_power_dbm")
    incident_values = _grid_series(grid, "isac_incident_power_isotropic_dbm")
    if incident_values is None:
        incident_values = _grid_series(grid, "incident_power_isotropic_dbm")
    echo_values = _grid_series(grid, "bistatic_echo_power_dbm")
    post_snr_values = _grid_series(grid, "bistatic_postprocessing_snr_db")
    margin_values = _grid_series(grid, "bistatic_detection_margin_db")
    doppler_values = _grid_series(grid, "bistatic_doppler_hz")
    delay_values = _grid_series(grid, "bistatic_excess_delay_s")
    path_range_values = _grid_series(grid, "bistatic_path_range_m")
    angle_values = _grid_series(grid, "bistatic_angle_deg")
    detectable_values_src = _grid_series(grid, "bistatic_detectable")
    doppler_arr = None
    finite = None
    detectable_values = None
    terrain_values = _grid_series(grid, "terrain_loss_db")
    sinr_values = _grid_series(grid, "sinr_db")

    if is_dvt_plan and _has_series(received_power_values):
        try:
            heatmap_received_power = render_ellipse(
                grid,
                size=tex_size,
                vmin=-140.0,
                vmax=-20.0,
                rsrp_values=received_power_values,
            )
            heatmap_received_power["layer"] = "received_power_dbm"
            heatmap_received_power["units"] = "dBm"
        except Exception as e:
            logger.warning("DVT received-power heatmap PNG failed: %s", e)
    if _has_series(incident_values):
        try:
            heatmap_incident_power = render_ellipse(
                grid, size=tex_size, vmin=-160.0, vmax=-20.0,
                rsrp_values=incident_values,
            )
            heatmap_incident_power["layer"] = "incident_power_isotropic_dbm"
            heatmap_incident_power["units"] = "dBm"
        except Exception as e:
            logger.warning("Channel incident-power heatmap PNG failed: %s", e)
    if _has_series(echo_values):
        try:
            heatmap_bistatic_echo = render_ellipse(
                grid, size=tex_size, vmin=-200.0, vmax=-80.0,
                rsrp_values=echo_values,
            )
            heatmap_bistatic_echo["layer"] = "bistatic_echo_power_dbm"
            heatmap_bistatic_echo["units"] = "dBm"
        except Exception as e:
            logger.warning("Bistatic echo heatmap PNG failed: %s", e)
    if _has_series(post_snr_values):
        try:
            heatmap_bistatic_snr = render_ellipse(
                grid, size=tex_size, vmin=-40.0, vmax=30.0,
                rsrp_values=post_snr_values,
            )
            heatmap_bistatic_snr["layer"] = "bistatic_postprocessing_snr_db"
            heatmap_bistatic_snr["units"] = "dB"
        except Exception as e:
            logger.warning("Bistatic SNR heatmap PNG failed: %s", e)
    if _has_series(margin_values):
        try:
            heatmap_bistatic_margin = render_ellipse(
                grid, size=tex_size, vmin=-40.0, vmax=20.0,
                rsrp_values=margin_values,
            )
            heatmap_bistatic_margin["layer"] = "bistatic_detection_margin_db"
            heatmap_bistatic_margin["units"] = "dB"
        except Exception as e:
            logger.warning("Bistatic margin heatmap PNG failed: %s", e)
    if _has_series(doppler_values):
        try:
            doppler_arr = np.asarray(doppler_values, dtype=np.float32)
            finite = np.isfinite(doppler_arr)
            doppler_limit = float(np.max(np.abs(doppler_arr[finite]))) if np.any(finite) else 1.0
            doppler_limit = max(doppler_limit, 1.0e-6)
            heatmap_bistatic_doppler = render_ellipse(
                grid, size=tex_size, vmin=-doppler_limit, vmax=doppler_limit,
                rsrp_values=doppler_arr,
            )
            heatmap_bistatic_doppler["layer"] = "bistatic_doppler_hz"
            heatmap_bistatic_doppler["units"] = "Hz"
        except Exception as e:
            logger.warning("Bistatic Doppler heatmap PNG failed: %s", e)
    if _has_series(delay_values):
        try:
            delay_us = np.asarray(delay_values, dtype=np.float32) * np.float32(1.0e6)
            heatmap_bistatic_excess_delay = render_ellipse(
                grid, size=tex_size, vmin=0.0, vmax=max(float(np.nanmax(delay_us)), 1.0),
                rsrp_values=delay_us,
            )
            heatmap_bistatic_excess_delay["layer"] = "bistatic_excess_delay_us"
            heatmap_bistatic_excess_delay["units"] = "us"
            del delay_us
        except Exception as e:
            logger.warning("Bistatic excess-delay heatmap PNG failed: %s", e)

    if _has_series(path_range_values):
        try:
            range_km = np.asarray(path_range_values, dtype=np.float32) / np.float32(1000.0)
            heatmap_bistatic_path_range = render_ellipse(
                grid, size=tex_size, vmin=float(np.nanmin(range_km)), vmax=float(np.nanmax(range_km)),
                rsrp_values=range_km,
            )
            heatmap_bistatic_path_range["layer"] = "bistatic_path_range_km"
            heatmap_bistatic_path_range["units"] = "km"
            del range_km
        except Exception as e:
            logger.warning("Bistatic path-range heatmap PNG failed: %s", e)
    if _has_series(angle_values):
        try:
            heatmap_bistatic_angle = render_ellipse(
                grid, size=tex_size, vmin=0.0, vmax=180.0,
                rsrp_values=angle_values,
            )
            heatmap_bistatic_angle["layer"] = "bistatic_angle_deg"
            heatmap_bistatic_angle["units"] = "deg"
        except Exception as e:
            logger.warning("Bistatic-angle heatmap PNG failed: %s", e)
    if _has_series(detectable_values_src):
        try:
            detectable_values = np.asarray(detectable_values_src, dtype=np.float32)
            heatmap_bistatic_detectable = render_ellipse(
                grid, size=tex_size, vmin=0.0, vmax=1.0,
                rsrp_values=detectable_values,
            )
            heatmap_bistatic_detectable["layer"] = "bistatic_detectable"
            heatmap_bistatic_detectable["units"] = "flag"
        except Exception as e:
            logger.warning("Bistatic detectability heatmap PNG failed: %s", e)

    if _has_series(terrain_values):
        try:
            terrain_cap = float(getattr(rf_params, "terrain_loss_cap_db", 40.0) or 40.0)
            heatmap_terrain = render_ellipse(
                grid,
                size=tex_size,
                vmin=0.0,
                vmax=terrain_cap,
                rsrp_values=terrain_values,
            )
        except Exception as e:
            logger.warning("Terrain loss heatmap PNG failed: %s", e)
    if _has_series(sinr_values):
        try:
            heatmap_sinr = render_ellipse(
                grid,
                size=tex_size,
                vmin=-5.0,
                vmax=30.0,
                rsrp_values=sinr_values,
            )
            heatmap_sinr["layer"] = "carrier_to_noise_db" if is_dvt_plan else "sinr_db"
            heatmap_sinr["units"] = "dB"
        except Exception as e:
            logger.warning("%s heatmap PNG failed: %s", "C/N" if is_dvt_plan else "SINR", e)

    # Geometry is shared across all aligned layers but no longer needed once the
    # PNG set is complete. Releasing it here trims peak memory before NPZ/export.
    del render_ellipse
    del heatmap_geometry
    received_power_values = incident_values = echo_values = post_snr_values = None
    margin_values = doppler_values = delay_values = path_range_values = angle_values = None
    detectable_values_src = terrain_values = sinr_values = None
    doppler_arr = finite = detectable_values = None

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
    compact_grid = ray_mode_eff2 in ("3d_osm", "3d-osm", "osm3d") or compact_large_result
    compact_fields = {
        "cell_lat", "cell_lon", "rsrp_dbm", "received_power_dbm",
        "field_strength_dbuv_m", "carrier_to_noise_db", "sinr_db",
        "incident_power_isotropic_dbm", "isac_incident_power_isotropic_dbm",
        "isac_tx_target_path_loss_db", "isac_tx_target_environment_loss_db",
        "isac_tx_target_terrain_loss_db", "isac_tx_target_los", "isac_tx_target_sample_error_m",
        "isac_echo_geometry_base_dbm",
        "bistatic_doppler_east_hz_per_mps", "bistatic_doppler_north_hz_per_mps",
        "bistatic_doppler_up_hz_per_mps", "bistatic_doppler_sensitivity_hz_per_mps",
        "bistatic_motion_doppler_sensitivity_hz_per_mps", "bistatic_minimum_detectable_speed_mps",
        "bistatic_echo_power_dbm",
        "bistatic_preprocessing_snr_db", "bistatic_postprocessing_snr_db",
        "bistatic_detection_margin_db", "bistatic_echo_to_residual_direct_db",
        "bistatic_direct_residual_margin_db", "bistatic_required_cancellation_db",
        "bistatic_required_dynamic_range_db", "bistatic_dynamic_range_margin_db",
        "bistatic_minimum_detectable_rcs_m2", "bistatic_rcs_margin_db",
        "bistatic_tx_target_range_m",
        "bistatic_target_receiver_range_m", "bistatic_path_range_m",
        "bistatic_excess_path_range_m", "bistatic_excess_delay_s",
        "bistatic_angle_deg", "bistatic_path_range_rate_mps",
        "bistatic_closing_speed_mps", "bistatic_doppler_hz",
        "bistatic_snr_noise_interference_ok", "bistatic_doppler_resolved", "bistatic_doppler_ambiguous",
        "bistatic_direct_residual_ok", "bistatic_dynamic_range_ok",
        "bistatic_detectable_screening", "bistatic_detectable_qualified",
        "bistatic_constraint_failure_code", "return_environment_valid",
        "bistatic_detectable", "return_path_loss_db",
        "return_environment_loss_db", "return_terrain_loss_db", "return_los",
        "return_terrain_state", "return_sample_error_m",
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
            grid_payload["carrier_to_noise_db"] = list(grid.carrier_to_noise_db or grid.sinr_db)
    if compact_grid:
        grid_payload["num_points"] = point_count
        grid_payload["compacted"] = True
        if is_dvt_plan:
            for key in (
                "cell_lat",
                "cell_lon",
                "received_power_dbm",
                "field_strength_dbuv_m",
                "carrier_to_noise_db",
                "incident_power_isotropic_dbm",
                "bistatic_echo_power_dbm",
                "bistatic_preprocessing_snr_db",
                "bistatic_postprocessing_snr_db",
                "bistatic_detection_margin_db",
                "bistatic_echo_to_residual_direct_db",
                "bistatic_required_dynamic_range_db",
                "bistatic_tx_target_range_m",
                "bistatic_target_receiver_range_m",
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
        try:
            channel_analysis_product = write_channel_product(
                arrays=(
                    _iter_channel_product_arrays(grid, release_after_write=True)
                    if compact_large_result
                    else _channel_product_arrays(grid)
                ),
                metadata={
                    "schema": "agentic_rf_planner.channel_analysis_grid",
                    "schema_version": "2.0",
                    "capabilities": {
                        "reusable_scene_basis": True,
                        "hypothesis_reanalysis_endpoint": "/api/channel-analysis/products/{product_id}/evaluate",
                        "hypothesis_bundle_endpoint": "/api/channel-analysis/products/{product_id}/evaluate-bundle",
                        "scene_rebuild_parameters": [
                            "target.heightMagl", "receiver position/height", "TX/RF/environment configuration"
                        ],
                    },
                    "summary": grid.channel_analysis_summary,
                    "transmitter": {
                        "latitude": float(grid.tx.lat),
                        "longitude": float(grid.tx.lon),
                        "absolute_height_m": float(
                            (rf_params.site_altitude_m or 0.0) + float(rf_params.tx_height_m)
                        ),
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

    if str(effective_ray_mode).strip().lower() == "2d" and not compact_large_result:
        ob = primary_osm_buildings_for_client or _osm_buildings_for_client_from_map_provider(map_provider)
        if ob is not None:
            result["osm_buildings_for_client"] = ob

    progress("complete", "RF planning complete")

    return result
