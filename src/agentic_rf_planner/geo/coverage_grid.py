"""Build coverage grid around transmitter."""

from __future__ import annotations

import math
import logging
from typing import Any, List, Optional, Tuple

from ..pipeline.schemas import LatLon, RFParams, WorldCell, MaterialType
from .physical_spanning import MapProvider

logger = logging.getLogger(__name__)


def _local_post_exit_excess_loss_db(
    *,
    intervals: List[dict[str, Any]],
    sample_distance_m: float,
    base_loss_db: float,
    width_slope_db_per_100m: float,
    cap_db: float,
    recovery_slope_db_per_100m: float,
) -> float:
    """Short-range shadow/diffraction after exiting nearby blockers.

    This deliberately ties post-blocker excess loss to blockers exited close to
    the current sample instead of the first blocker on the whole bearing. The
    old "behind first blocker forever" behavior created wedge artifacts and let a
    single early building poison the rest of the ray. Here each exited blocker
    contributes a local event loss that relaxes as the open gap behind that
    blocker grows.
    """
    total_loss_db = 0.0
    for interval in intervals:
        end_m = float(interval["end_m"])
        if end_m > sample_distance_m + 1e-6:
            continue

        width_m = max(0.0, float(interval["end_m"]) - float(interval["start_m"]))
        event_loss_db = min(
            cap_db,
            max(0.0, base_loss_db) + max(0.0, width_slope_db_per_100m) * (width_m / 100.0),
        )
        open_gap_m = max(0.0, sample_distance_m - end_m)
        recovered_db = max(0.0, recovery_slope_db_per_100m) * (open_gap_m / 100.0)
        total_loss_db += max(0.0, event_loss_db - recovered_db)

    return min(max(0.0, cap_db), total_loss_db)


def build_coverage_grid(
    tx: LatLon, 
    rf_params: RFParams,
    map_provider: Optional[MapProvider] = None,
    sectors: Optional[List] = None  # List of SectorConfig objects
) -> List[WorldCell]:
    """
    Create a ring/grid of cells around TX with adaptive ray termination.
    
    If sectors are provided, each sector is evaluated across the same spatial sample field.
    Serving/interference behavior is resolved later from per-sector RSRP candidates.
    If no sectors (or None), generates a single omnidirectional candidate field.
    
    Rays stop when:
    1. Signal strength drops below the configured termination threshold
    2. Maximum range is reached

    Vendor-grade roadmap notes:
    - penetration is only applied while the ray is actually inside a blocker interval
    - after the first blocker, LOS is lost and the ray transitions into a shadow/recovery state
    - we do not carry every prior wall forever once the ray exits the structure
    """
    from ..rf.sector_config import SectorConfig, create_omnidirectional_sector, validate_sectors
    
    cells: List[WorldCell] = []
    max_r = float(rf_params.max_range_m)
    dr = float(rf_params.step_m)

    # Angle step (deg). In 3D OSM-only mode, the visualization is a map-aligned raster.
    # Using a coarse dtheta (e.g. 5°) produces huge angular gaps at large radii and turns
    # into wedge artifacts beyond a few hundred meters.
    #
    # To keep results usable at city ranges while staying fast, we adapt dtheta to bound
    # the arc-length between adjacent rays at max range.
    dtheta_user = float(getattr(rf_params, "dtheta_deg", 5.0))  # degrees
    
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
    
    min_rsrp_threshold = float(getattr(rf_params, "termination_rsrp_dbm", -140.0))
    logger.info(f"Ray termination threshold: {min_rsrp_threshold:.1f} dBm (noise floor: {noise_floor_dbm:.1f} dBm)")
    logger.debug(f"  This ensures rays stop when signal becomes too weak for reliable detection")
    logger.debug(f"  Higher frequencies (e.g., 3.5 GHz) will stop earlier than lower frequencies (e.g., 622 MHz)")
    logger.debug(f"  FSPL always applies, material loss adds on top")
    
    # Import here to avoid circular dependency
    from ..rf.material_penetration import get_penetration_loss_for_material, is_material_blocking
    from ..rf.attenuation_models import (
        _horizontal_pattern_attenuation_db,
        _reference_signal_eirp_dbm,
        _resolve_sector_params,
        _scenario_path_loss_db,
        _vertical_pattern_attenuation_db,
    )

    # Building attenuation config (overall + per-material from rf.params.yaml)
    bldg_atten_cfg = getattr(rf_params, "building_attenuation", None)

    # Get frequency once (used for all calculations)
    freq_mhz = rf_params.freq_mhz

    # Constant vertical separation for 3D-distance FSPL.
    # (This codebase uses 2.5D ray-march; obstacle queries are done in a slice,
    # but FSPL benefits from a 3D straight-line distance.)
    dz = float(getattr(rf_params, "tx_height_m", 0.0) or 0.0) - float(getattr(rf_params, "rx_height_m", 1.5) or 0.0)
    dz2 = dz * dz
    
    # 3D OSM-only mode: use a map-aligned (cartesian) grid to avoid radial spokes.

    # NOTE (performance): 3D OSM-only mode previously switched to a dense cartesian grid to
    # align the coverage overlay with map coordinates. That approach scales as O((R/dr)^2)
    # and becomes unusable at city ranges.
    #
    # We keep the physically-correct (and fast) polar ray-march here for ALL modes.
    # The 3D OSM-only visualization is generated as a map-aligned raster/PNG later in the
    # pipeline (see geo/heatmap.py), so we do NOT need a cartesian simulation grid here.


    
    # 3D OSM-only mode: the frontend renders from a raster/PNG overlay.
    # Storing per-cell building lists is expensive and unnecessary for that mode.
    ray_mode_eff = str(getattr(rf_params, "ray_mode", "") or "").strip().lower()

    # 3D multipath ray tracing mode (single-bounce reflections).
    multipath_enabled = ray_mode_eff in ("3d_rt", "3d-rt", "rt3d", "3d_raytrace", "raytrace")
    osm_provider_for_rt = None
    if multipath_enabled and map_provider is not None:
        try:
            from .osm_map_provider import OSMMapProvider

            if isinstance(map_provider, OSMMapProvider):
                osm_provider_for_rt = map_provider
            else:
                osm_provider_for_rt = getattr(map_provider, "osm", None)
        except Exception:
            osm_provider_for_rt = getattr(map_provider, "osm", None)
    if multipath_enabled and osm_provider_for_rt is None:
        logger.warning("Multipath ray tracing requested but no OSM geometry provider available; disabling multipath")
        multipath_enabled = False

    if multipath_enabled:
        # Imports only when needed.
        from .spatial_index import BoundingBox
        from ..rf.ray_tracing import compute_single_bounce_paths, extract_wall_segments

        def _bbox_around_point_m(lat: float, lon: float, r_m: float) -> BoundingBox:
            # Approx meters->degrees.
            dlat = r_m / 111000.0
            dlon = r_m / (111000.0 * max(math.cos(math.radians(lat)), 1e-6))
            return BoundingBox(lat - dlat, lat + dlat, lon - dlon, lon + dlon)

        # Cache handle(s) for faster reflection candidate gathering.
        rt_qt = getattr(osm_provider_for_rt, "_building_quadtree", None)
        rt_by_id = getattr(osm_provider_for_rt, "_building_by_id", None)
        rt_cached_buildings = getattr(osm_provider_for_rt, "_cached_buildings", None) or []

        def _nearby_buildings_for_reflection(p: LatLon, radius_m: float) -> list[dict]:
            # Prefer quadtree for locality. Fallback to cached list.
            if rt_qt is not None and rt_by_id is not None:
                ids = rt_qt.query_bbox(_bbox_around_point_m(p.lat, p.lon, radius_m))
                out = []
                for bid in ids:
                    b = rt_by_id.get(bid)
                    if b is not None:
                        out.append(b)
                return out
            # Worst-case fallback.
            return list(rt_cached_buildings)

        def _is_path_clear_osm(p0: LatLon, p1: LatLon, exclude_building_id: int | None) -> bool:
            try:
                hits = osm_provider_for_rt.get_buildings_along_ray(p0, p1)
            except Exception:
                return True
            for b in hits:
                bid = b.get("id")
                try:
                    bid_int = int(bid) if bid is not None else None
                except Exception:
                    bid_int = None
                if exclude_building_id is not None and bid_int == exclude_building_id:
                    continue
                return False
            return True

    # Adaptive dtheta for OSM polygon modes (2d + 3d_osm):
    # target arc-length ~= 4*dr (clamped) at max range — avoids sparse spokes at city range
    # and keeps PNG fill/masking aligned with the simulation.
    if ray_mode_eff in ("3d_osm", "3d-osm", "osm3d", "2d"):
        target_arc_m = max(12.0, min(30.0, 4.0 * dr))
        dtheta_target = math.degrees(target_arc_m / max(1.0, max_r))
        dtheta = max(0.25, min(dtheta_user, dtheta_target))
        # Persist the effective dtheta so downstream PNG masking can match the simulation.
        try:
            rf_params.dtheta_deg = float(dtheta)
        except Exception:
            pass
        logger.info(
            f"OSM polar grid: adaptive dtheta={dtheta:.3f}° "
            f"(user={dtheta_user:.3f}°, target_arc≈{target_arc_m:.1f}m at R={max_r:.0f}m, mode={ray_mode_eff})"
        )
    else:
        dtheta = dtheta_user

    store_building_lists = ray_mode_eff not in ("3d_osm", "3d-osm", "osm3d")

    # Generate cells for each sector.
    #
    # Real deployments do not behave like azimuth-clipped cones. Every sector radiates
    # continuously according to its antenna pattern, then the strongest candidate serves
    # the UE while neighboring same-carrier sectors remain as interference. Angle/polygon
    # sector definitions are therefore treated as configuration/display hints, not as a
    # binary RF admission mask.
    for sector in sector_configs:
        if sector.sector_type == "polygon":
            logger.debug(
                f"Generating cells for polygon-design sector {sector.sector_id} "
                f"(polygon with {len(sector.polygon_points)} points; not used as an RF cutoff)"
            )
        else:
            logger.debug(
                f"Generating full-field candidates for sector {sector.sector_id} "
                f"(nominal orientation {getattr(sector, 'azimuth_deg', 0.0):.1f}°, "
                f"HPBW {getattr(sector, 'beamwidth_h_deg', 360.0):.1f}°)"
            )
        
        # Use sector-specific frequency and power for this sector
        sector_freq_mhz = sector.freq_mhz
        sector_tx_power_dbm = sector.tx_power_dbm
        sector_rs_eirp_dbm = _reference_signal_eirp_dbm(rf_params, tx_power_dbm_override=sector_tx_power_dbm)
        
        # All sectors share the same 360° sample lattice. The antenna pattern later
        # determines the relative strength of each sector candidate at each point.
        angle_range = [(0.0, 360.0)]

        # Generate cells for each angle range in this sector.
        for range_start, range_end in angle_range:
            

            theta = range_start
            while theta < range_end:
                # Per-ray precomputation:
                # - Query buildings ONCE for the full ray (TX -> max_range)
                # - Compute first-intersection distance per building
                # - Incrementally accumulate loss as r increases
                r = dr
                metal_blocked = False

                # When rendering as a raster/PNG (3d_osm), we don't need to attach full building
                # metadata per cell. Avoid per-cell list copies for memory/perf.
                encountered_buildings = [] if store_building_lists else None

                building_intervals: list[dict[str, Any]] = []
                forest_intervals: list[dict[str, Any]] = []
                next_hit_idx = 0

                # Far endpoint for ray queries.
                far_lat, far_lon = _project_from_tx(tx.lat, tx.lon, max_r, theta)
                far_latlon = LatLon(lat=far_lat, lon=far_lon)

                if map_provider is not None:
                    # Buildings along full ray (one query per theta), WITHOUT relying on
                    # OSMMapProvider.get_buildings_along_ray()'s polygon intersection helper.
                    #
                    # We use the provider's quadtree (if available) to get candidate IDs,
                    # then compute a robust first-hit distance ourselves. This makes the
                    # "incremental along the ray" optimization match the per-cell behavior.
                    try:
                        qt = getattr(map_provider, "_building_quadtree", None)
                        by_id = getattr(map_provider, "_building_by_id", None)
                        if qt is not None and by_id is not None:
                            candidate_ids = qt.query_ray(tx, far_latlon)
                        else:
                            candidate_ids = None
                    except Exception:
                        candidate_ids = None

                    if candidate_ids and by_id is not None:
                        # Height-slice support (3d_osm): if the provider exposes a slice height,
                        # we must apply the same filtering here as OSMMapProvider.get_buildings_along_ray().
                        slice_h = getattr(map_provider, "slice_height_m", None)
                        for building_id in candidate_ids:
                            b = by_id.get(building_id)
                            if not b:
                                continue

                            if slice_h is not None:
                                # Cache a best-effort height estimate directly on the building dict.
                                h = b.get("height_m")
                                if h is None:
                                    try:
                                        # Internal helper in osm_map_provider; safe to import here.
                                        from .osm_map_provider import _estimate_osm_height_m  # type: ignore

                                        h = _estimate_osm_height_m(b.get("tags", {}))
                                    except Exception:
                                        h = None
                                    b["height_m"] = h

                                # If we have an estimate and it's below the slice, skip this obstacle.
                                if h is not None and float(h) < float(slice_h):
                                    continue
                            geom = b.get("geometry", [])
                            interval = _intersection_interval_m(tx, far_latlon, geom)
                            if interval is None:
                                continue
                            r0_m, r1_m = interval
                            building_intervals.append(
                                {
                                    "start_m": r0_m,
                                    "end_m": r1_m,
                                    "building": b,
                                    "material": b.get("material", "unknown"),
                                }
                            )
                    else:
                        # Fallback: provider method (may be less robust, but keeps functionality for non-OSM providers)
                        try:
                            buildings_full = map_provider.get_buildings_along_ray(tx, far_latlon)
                        except Exception:
                            buildings_full = []

                        for b in buildings_full:
                            # Google-mesh provider returns obstacle segments with r0_m/r1_m (no polygon geometry).
                            # OSM provider returns polygons with "geometry".
                            if "r0_m" in b:
                                try:
                                    r0_m = float(b.get("r0_m", 0.0))
                                    r1_m = float(b.get("r1_m", r0_m))
                                except Exception:
                                    continue
                                building_intervals.append(
                                    {
                                        "start_m": max(0.0, min(r0_m, r1_m)),
                                        "end_m": max(r0_m, r1_m),
                                        "building": b,
                                        "material": b.get("material", "unknown"),
                                    }
                                )
                                continue

                            geom = b.get("geometry", [])
                            interval = _intersection_interval_m(tx, far_latlon, geom)
                            if interval is None:
                                continue
                            r0_m, r1_m = interval
                            building_intervals.append(
                                {
                                    "start_m": r0_m,
                                    "end_m": r1_m,
                                    "building": b,
                                    "material": b.get("material", "unknown"),
                                }
                            )

                    building_intervals.sort(key=lambda item: item["start_m"])
                    for interval in building_intervals:
                        interval["penetration_loss_db"] = get_penetration_loss_for_material(
                            str(interval["material"]),
                            sector_freq_mhz,
                            attenuation_config=bldg_atten_cfg,
                        )
                        interval["is_blocking"] = is_material_blocking(str(interval["material"]), sector_freq_mhz)

                    # Forest/vegetation intervals (one query per theta).
                    # - OSM provider: use landuse polygons/quadtree
                    # - Google-mesh provider: use persisted tree segments (r0_m)
                    forest_intervals = []

                    # If we're in a height-slice above typical vegetation canopy, do not intersect forests.
                    # (OSMMapProvider.is_forest_between() already implements this, but our fast per-ray
                    # precompute path must match it too.)
                    try:
                        slice_h2 = getattr(map_provider, "slice_height_m", None)
                        forest_disabled = (slice_h2 is not None and float(slice_h2) > 8.0)
                    except Exception:
                        forest_disabled = False
                    try:
                        tree_by_bin = getattr(map_provider, "_tree_segments_by_bin", None)
                        dtheta_p = float(getattr(map_provider, "dtheta_deg", 0.0) or 0.0)
                        if tree_by_bin is not None and dtheta_p > 0.0:
                            n_bins = int(round(360.0 / dtheta_p)) or 1
                            ti = int(round(theta / dtheta_p)) % n_bins
                            segs = tree_by_bin.get(ti, []) or []
                            if segs:
                                for seg in segs:
                                    if seg.get("r0_m") is None:
                                        continue
                                    r0_m = float(seg.get("r0_m", 0.0))
                                    r1_m = float(seg.get("r1_m", r0_m))
                                    forest_intervals.append(
                                        {"start_m": max(0.0, min(r0_m, r1_m)), "end_m": max(r0_m, r1_m)}
                                    )
                    except Exception:
                        forest_intervals = []

                    if not forest_intervals and not forest_disabled:
                        try:
                            forest_intervals = _forest_intervals_m(map_provider, tx, far_latlon)
                        except Exception:
                            forest_intervals = []

                # Precompute wood loss constant for active vegetation intervals.
                wood_loss_db = (
                    get_penetration_loss_for_material("wood", sector_freq_mhz, attenuation_config=bldg_atten_cfg)
                    if map_provider is not None else 0.0
                )
                sector_params = _resolve_sector_params(
                    type(
                        "CoverageCellSector",
                        (),
                        {
                            "sector_id": sector.sector_id,
                            "sector_freq_mhz": sector_freq_mhz,
                            "sector_tx_power_dbm": sector_tx_power_dbm,
                            "sector_channel_bandwidth_mhz": sector.channel_bandwidth_mhz,
                            "sector_azimuth_deg": getattr(sector, "azimuth_deg", None),
                            "sector_beamwidth_h_deg": getattr(sector, "beamwidth_h_deg", None),
                            "sector_beamwidth_v_deg": getattr(sector, "beamwidth_v_deg", None),
                            "sector_electrical_tilt_deg": getattr(sector, "electrical_tilt_deg", None),
                            "sector_mechanical_tilt_deg": getattr(sector, "mechanical_tilt_deg", None),
                            "sector_max_horizontal_attenuation_db": getattr(sector, "max_horizontal_attenuation_db", None),
                            "sector_front_to_back_attenuation_db": getattr(sector, "front_to_back_attenuation_db", None),
                            "sector_max_vertical_attenuation_db": getattr(sector, "max_vertical_attenuation_db", None),
                        },
                    )(),
                    rf_params,
                    {},
                )

                while r <= max_r:
                    lat, lon = _project_from_tx(tx.lat, tx.lon, r, theta)

                    while next_hit_idx < len(building_intervals) and building_intervals[next_hit_idx]["start_m"] <= (r + 1e-6):
                        interval = building_intervals[next_hit_idx]
                        if interval["is_blocking"]:
                            metal_blocked = True
                        if encountered_buildings is not None:
                            encountered_buildings.append(interval["building"])
                        next_hit_idx += 1

                    active_buildings = [
                        interval
                        for interval in building_intervals
                        if interval["start_m"] <= (r + 1e-6) < interval["end_m"]
                    ]
                    active_forest = [
                        interval
                        for interval in forest_intervals
                        if interval["start_m"] <= (r + 1e-6) < interval["end_m"]
                    ]
                    all_started = [
                        interval["start_m"]
                        for interval in building_intervals
                        if interval["start_m"] <= (r + 1e-6)
                    ] + [
                        interval["start_m"]
                        for interval in forest_intervals
                        if interval["start_m"] <= (r + 1e-6)
                    ]
                    all_exited = [
                        interval["end_m"]
                        for interval in building_intervals
                        if interval["end_m"] <= (r + 1e-6)
                    ] + [
                        interval["end_m"]
                        for interval in forest_intervals
                        if interval["end_m"] <= (r + 1e-6)
                    ]
                    exited_intervals = [
                        interval
                        for interval in (building_intervals + forest_intervals)
                        if interval["end_m"] <= (r + 1e-6)
                    ]

                    first_blocker_distance_m = min(all_started) if all_started else None
                    last_exit_distance_m = max(all_exited) if all_exited else None
                    penetration_loss_db = (
                        sum(float(interval["penetration_loss_db"]) for interval in active_buildings)
                        + len(active_forest) * wood_loss_db
                    )
                    # Treat LOS as a local state: if the current sample is not inside a
                    # blocker, the broad outdoor decay resumes and local exited-blocker
                    # shadow handles the nearby building effect.
                    is_los = penetration_loss_db <= 0.0
                    # Hybrid 3GPP tuning:
                    # In dense UMi street-canyon deployments, immediate "full recovery" after
                    # exiting blockers can be too optimistic at longer ranges. We keep local
                    # post-exit recovery but damp the recovery slope to preserve moderate NLOS
                    # memory in cluttered urban paths.
                    recovery_slope_db_per_100m = float(
                        getattr(rf_params, "canyon_recovery_slope_db_per_100m", 6.0) or 6.0
                    )
                    model_name = str(getattr(rf_params, "path_loss_model", "") or "").strip().lower()
                    scenario_name = str(getattr(rf_params, "propagation_scenario", "") or "").strip().lower()
                    if model_name == "3gpp_38901" and scenario_name == "umi_street_canyon":
                        recovery_slope_db_per_100m *= 0.7

                    shadow_loss_db = 0.0
                    if penetration_loss_db <= 0.0 and exited_intervals:
                        shadow_loss_db = _local_post_exit_excess_loss_db(
                            intervals=exited_intervals,
                            sample_distance_m=r,
                            base_loss_db=float(getattr(rf_params, "shadow_loss_db", 6.0) or 6.0),
                            width_slope_db_per_100m=float(
                                getattr(rf_params, "shadow_decay_db_per_100m", 4.0) or 4.0
                            ),
                            cap_db=float(getattr(rf_params, "shadow_loss_cap_db", 22.0) or 22.0),
                            recovery_slope_db_per_100m=recovery_slope_db_per_100m,
                        )
                    open_gap_after_exit_m = (
                        max(0.0, r - last_exit_distance_m)
                        if last_exit_distance_m is not None and penetration_loss_db <= 0.0
                        else 0.0
                    )
                    diffraction_loss_db = 0.0
                    canyon_recovery_db = 0.0
                    if penetration_loss_db <= 0.0 and exited_intervals:
                        diffraction_loss_db = _local_post_exit_excess_loss_db(
                            intervals=exited_intervals,
                            sample_distance_m=r,
                            base_loss_db=float(
                                getattr(rf_params, "diffraction_base_loss_db", 6.0) or 6.0
                            ),
                            width_slope_db_per_100m=float(
                                getattr(rf_params, "diffraction_slope_db_per_100m", 3.0) or 3.0
                            ),
                            cap_db=float(
                                getattr(rf_params, "diffraction_loss_cap_db", 18.0) or 18.0
                            ),
                            recovery_slope_db_per_100m=recovery_slope_db_per_100m,
                        )

                    extra_loss_db = max(
                        0.0,
                        penetration_loss_db + shadow_loss_db + diffraction_loss_db - canyon_recovery_db,
                    )
                    d3 = math.sqrt(r * r + dz2)
                    scenario_path_loss_db = _scenario_path_loss_db(
                        distance_2d_m=r,
                        distance_3d_m=d3,
                        freq_mhz=sector_freq_mhz,
                        tx_height_m=float(getattr(rf_params, "tx_height_m", 0.0) or 0.0),
                        rx_height_m=float(getattr(rf_params, "rx_height_m", 1.5) or 0.0),
                        is_los=is_los,
                        rf_params=rf_params,
                    )
                    vertical_pattern_loss_db = _vertical_pattern_attenuation_db(
                        distance_m=r,
                        tx_height_m=float(getattr(rf_params, "tx_height_m", 0.0) or 0.0),
                        rx_height_m=float(getattr(rf_params, "rx_height_m", 1.5) or 0.0),
                        rf_params=rf_params,
                        sector_params=sector_params,
                    )
                    horizontal_pattern_loss_db = _horizontal_pattern_attenuation_db(
                        bearing_deg=theta,
                        sector_params=sector_params,
                        rf_params=rf_params,
                    )
                    estimated_rsrp = sector_rs_eirp_dbm - (
                        scenario_path_loss_db
                        + extra_loss_db
                        + horizontal_pattern_loss_db
                        + vertical_pattern_loss_db
                    )

                    # UE gain (used in attenuation_grid). Include it here so termination is consistent.
                    rx_combining_gain_db = float(getattr(rf_params, "ue_antenna_gain_dbi", 0.0) or 0.0)
                    estimated_rsrp += rx_combining_gain_db

                    estimated_rsrp_total = estimated_rsrp
                    mp_is_reflect = False
                    if multipath_enabled:
                        # Compute single-bounce reflections from nearby building walls.
                        rx_ll = LatLon(lat=lat, lon=lon)
                        nearby_buildings = _nearby_buildings_for_reflection(
                            rx_ll,
                            radius_m=max(60.0, min(160.0, 12.0 * dr)),
                        )
                        wall_segments = extract_wall_segments(nearby_buildings, tx)

                        def _rsrp_for_reflection(
                            tx_ll: LatLon,
                            bounce_ll: LatLon,
                            rx_ll2: LatLon,
                            total_distance_m: float,
                            reflection_loss_db: float,
                        ) -> float:
                            # Bearing is from TX to bounce (departure angle).
                            be, bn = _enu_from_tx(tx_ll, bounce_ll)
                            brg = (math.degrees(math.atan2(be, bn)) + 360.0) % 360.0
                            d2r = max(1.0, float(total_distance_m))
                            d3r = math.sqrt(d2r * d2r + dz2)
                            pl = _scenario_path_loss_db(
                                distance_2d_m=d2r,
                                distance_3d_m=d3r,
                                freq_mhz=sector_freq_mhz,
                                tx_height_m=float(getattr(rf_params, "tx_height_m", 0.0) or 0.0),
                                rx_height_m=float(getattr(rf_params, "rx_height_m", 1.5) or 0.0),
                                is_los=True,
                                rf_params=rf_params,
                            )
                            vpat = _vertical_pattern_attenuation_db(
                                distance_m=d2r,
                                tx_height_m=float(getattr(rf_params, "tx_height_m", 0.0) or 0.0),
                                rx_height_m=float(getattr(rf_params, "rx_height_m", 1.5) or 0.0),
                                rf_params=rf_params,
                                sector_params=sector_params,
                            )
                            hpat = _horizontal_pattern_attenuation_db(
                                bearing_deg=brg,
                                sector_params=sector_params,
                                rf_params=rf_params,
                            )
                            return sector_rs_eirp_dbm - (pl + reflection_loss_db + hpat + vpat) + rx_combining_gain_db

                        refl_paths = compute_single_bounce_paths(
                            tx,
                            rx_ll,
                            wall_segments,
                            max_candidates=int(getattr(rf_params, "rt_max_wall_candidates", 40) or 40),
                            max_return=int(getattr(rf_params, "rt_max_reflections_per_sample", 2) or 2),
                            rf_params=rf_params,
                            is_path_clear_fn=_is_path_clear_osm,
                            rsrp_for_path_fn=_rsrp_for_reflection,
                            footprint_buildings=rt_cached_buildings,
                        )

                        # Combine direct + reflected contributions in linear power domain.
                        def _dbm_to_mw(x_dbm: float) -> float:
                            return 10.0 ** (x_dbm / 10.0)

                        def _mw_to_dbm(x_mw: float) -> float:
                            return 10.0 * math.log10(max(x_mw, 1e-15))

                        total_mw = _dbm_to_mw(estimated_rsrp)
                        for p in refl_paths:
                            total_mw += _dbm_to_mw(p.rsrp_dbm)
                        estimated_rsrp_total = _mw_to_dbm(total_mw)

                        # If reflections dominate, expose it as propagation mode.
                        if (not is_los) and refl_paths and estimated_rsrp_total > estimated_rsrp + 0.5:
                            mp_is_reflect = True

                    # Stop ray if signal too weak
                    if estimated_rsrp_total < min_rsrp_threshold:
                        logger.debug(
                            f"Ray at bearing {theta:.1f}° (sector {sector.sector_id}) terminated at {r:.1f}m "
                            f"(RSRP={estimated_rsrp_total:.1f}dBm < threshold {min_rsrp_threshold:.1f}dBm)"
                        )
                        break

                    cells.append(
                        WorldCell(
                            lat=lat,
                            lon=lon,
                            distance_m=r,
                            bearing_deg=theta,
                            dominant_material=MaterialType.UNKNOWN,
                            obstacles_count=0,
                            extra_loss_db=extra_loss_db,
                            sector_id=sector.sector_id,
                            sector_freq_mhz=sector_freq_mhz,
                            sector_tx_power_dbm=sector_tx_power_dbm,
                            sector_channel_bandwidth_mhz=sector.channel_bandwidth_mhz,
                            sector_azimuth_deg=getattr(sector, "azimuth_deg", None),
                            sector_beamwidth_h_deg=getattr(sector, "beamwidth_h_deg", None),
                            sector_beamwidth_v_deg=getattr(sector, "beamwidth_v_deg", None),
                            sector_electrical_tilt_deg=getattr(sector, "electrical_tilt_deg", None),
                            sector_mechanical_tilt_deg=getattr(sector, "mechanical_tilt_deg", None),
                            sector_max_horizontal_attenuation_db=getattr(sector, "max_horizontal_attenuation_db", None),
                            sector_front_to_back_attenuation_db=getattr(sector, "front_to_back_attenuation_db", None),
                            sector_max_vertical_attenuation_db=getattr(sector, "max_vertical_attenuation_db", None),
                            is_los=is_los,
                            actual_path_length_m=r,
                            num_buildings=sum(1 for interval in building_intervals if interval["start_m"] <= (r + 1e-6)),
                            num_trees=sum(1 for interval in forest_intervals if interval["start_m"] <= (r + 1e-6)),
                            blocking_state=("los" if is_los else ("penetration" if penetration_loss_db > 0.0 else "shadow")),
                            propagation_mode=(
                                "los"
                                if is_los
                                else (
                                    "reflect"
                                    if mp_is_reflect
                                    else (
                                        "penetration"
                                        if penetration_loss_db > 0.0
                                        else ("nlos_recovery" if canyon_recovery_db > 0.0 else "shadow")
                                    )
                                )
                            ),
                            first_blocker_distance_m=first_blocker_distance_m,
                            diffraction_flag=(diffraction_loss_db > 0.0),
                            penetration_loss_db=penetration_loss_db,
                            shadow_loss_db=shadow_loss_db,
                            diffraction_loss_db=diffraction_loss_db,
                            canyon_recovery_db=canyon_recovery_db,
                            metal_blocked=metal_blocked,
                            buildings_along_path=(list(encountered_buildings) if encountered_buildings is not None else []),

                            precomputed_rsrp_dbm=(estimated_rsrp_total if multipath_enabled else None),
                        )
                    )

                    r += dr

                theta += dtheta
    
    # Note: We've processed all sectors, so we're done
    
    logger.info(f"Generated {len(cells)} cells with adaptive ray termination (max would be {int(360/dtheta) * int(max_r/dr)})")
    return cells



def _enu_from_tx(tx: LatLon, p: LatLon) -> Tuple[float, float]:
    """Local equirectangular ENU approximation (east, north) in meters."""
    R = 6371000.0
    lat0 = math.radians(tx.lat)
    dlat = math.radians(p.lat - tx.lat)
    dlon = math.radians(p.lon - tx.lon)
    north = dlat * R
    east = dlon * R * math.cos(lat0)
    return east, north


def _cross2(ax: float, ay: float, bx: float, by: float) -> float:
    return ax * by - ay * bx


def _segment_intersection_t(
    p0x: float, p0y: float, p1x: float, p1y: float,
    q0x: float, q0y: float, q1x: float, q1y: float,
) -> Optional[float]:
    """
    Return t in [0,1] where segment P(t)=p0 + t*(p1-p0) intersects Q(u)=q0+u*(q1-q0),
    or None if no intersection.

    Handles the common colinear-overlap case by returning the smallest t of any overlap.
    """
    rx = p1x - p0x
    ry = p1y - p0y
    sx = q1x - q0x
    sy = q1y - q0y

    rxs = _cross2(rx, ry, sx, sy)
    qmpx = q0x - p0x
    qmpy = q0y - p0y

    eps = 1e-12
    if abs(rxs) < eps:
        # Parallel. If not colinear, no intersection.
        if abs(_cross2(qmpx, qmpy, rx, ry)) >= eps:
            return None

        # Colinear: project q endpoints onto r and check overlap in t.
        rr = rx * rx + ry * ry
        if rr < eps:
            return None

        t0 = (qmpx * rx + qmpy * ry) / rr
        t1 = ((q1x - p0x) * rx + (q1y - p0y) * ry) / rr

        tmin = min(t0, t1)
        tmax = max(t0, t1)

        # Overlap with [0,1]
        if tmax < 0.0 or tmin > 1.0:
            return None

        return max(0.0, tmin)

    t = _cross2(qmpx, qmpy, sx, sy) / rxs
    u = _cross2(qmpx, qmpy, rx, ry) / rxs

    if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
        return float(t)
    return None


def _intersection_interval_m(start: LatLon, end: LatLon, polygon: List[dict]) -> Optional[Tuple[float, float]]:
    """Return the [entry, exit] distances where the ray overlaps a polygon."""
    if not polygon or len(polygon) < 3:
        return None

    # Build polygon points in ENU (east, north) relative to start.
    pts: List[Tuple[float, float]] = []
    for node in polygon:
        lat = node.get("lat")
        lon = node.get("lon")
        if lat is None or lon is None:
            continue
        try:
            e, n = _enu_from_tx(start, LatLon(lat=float(lat), lon=float(lon)))
        except Exception:
            continue
        pts.append((e, n))

    if len(pts) < 3:
        return None

    def _point_in_poly(x: float, y: float, poly: List[Tuple[float, float]]) -> bool:
        # Ray-casting (even/odd) test.
        inside = False
        n = len(poly)
        j = n - 1
        for i in range(n):
            xi, yi = poly[i]
            xj, yj = poly[j]
            intersect = ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) + 1e-30) + xi)
            if intersect:
                inside = not inside
            j = i
        return inside

    end_e, end_n = _enu_from_tx(start, end)
    seg_len = math.hypot(end_e, end_n)
    if seg_len <= 1e-6:
        return None

    ts: list[float] = []
    npts = len(pts)
    for i in range(npts):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % npts]
        t = _segment_intersection_t(0.0, 0.0, end_e, end_n, ax, ay, bx, by)
        if t is None:
            continue
        ts.append(float(t))

    ts = sorted(max(0.0, min(1.0, t)) for t in ts)
    inside_at_start = _point_in_poly(0.0, 0.0, pts)
    if inside_at_start:
        ts = [0.0] + ts
    if len(ts) % 2 == 1:
        ts.append(1.0)
    if len(ts) < 2:
        return None
    return (ts[0] * seg_len, ts[1] * seg_len)


def _first_intersection_distance_m(start: LatLon, end: LatLon, polygon: List[dict]) -> Optional[float]:
    interval = _intersection_interval_m(start, end, polygon)
    if interval is None:
        return None
    return interval[0]


def _first_forest_intersection_distance_m(map_provider: object, start: LatLon, end: LatLon) -> Optional[float]:
    """
    Best-effort: compute first intersection distance to any forest/vegetation polygon along start->end.
    Uses OSMMapProvider internal caches/quadtree if present; otherwise returns None (caller falls back).
    """
    landuse_qt = getattr(map_provider, "_landuse_quadtree", None)
    landuse_by_id = getattr(map_provider, "_landuse_by_id", None)
    cached_landuse = getattr(map_provider, "_cached_landuse", None)

    candidates = None
    if landuse_qt is not None and landuse_by_id is not None:
        try:
            candidates = [landuse_by_id.get(area_id) for area_id in landuse_qt.query_ray(start, end)]
        except Exception:
            candidates = None

    if candidates is None and cached_landuse:
        candidates = list(cached_landuse)

    if not candidates:
        return None

    best = None
    for area in candidates:
        if not area:
            continue
        tags = area.get("tags", {}) or {}
        landuse_type = str(tags.get("landuse", "")).lower()
        natural_type = str(tags.get("natural", "")).lower()

        is_forest = landuse_type in ("forest", "wood", "meadow") or natural_type in ("wood", "forest", "tree_row")
        if not is_forest:
            continue

        geom = area.get("geometry", [])
        d = _first_intersection_distance_m(start, end, geom)
        if d is None:
            continue
        if best is None or d < best:
            best = d

    return best


def _forest_intervals_m(map_provider: object, start: LatLon, end: LatLon) -> list[dict[str, float]]:
    """Best-effort forest overlap intervals along the ray."""
    landuse_qt = getattr(map_provider, "_landuse_quadtree", None)
    landuse_by_id = getattr(map_provider, "_landuse_by_id", None)
    cached_landuse = getattr(map_provider, "_cached_landuse", None)

    candidates = None
    if landuse_qt is not None and landuse_by_id is not None:
        try:
            candidates = [landuse_by_id.get(area_id) for area_id in landuse_qt.query_ray(start, end)]
        except Exception:
            candidates = None
    if candidates is None and cached_landuse:
        candidates = list(cached_landuse)
    if not candidates:
        return []

    intervals: list[dict[str, float]] = []
    for area in candidates:
        if not area:
            continue
        tags = area.get("tags", {}) or {}
        landuse_type = str(tags.get("landuse", "")).lower()
        natural_type = str(tags.get("natural", "")).lower()
        is_forest = landuse_type in ("forest", "wood", "meadow") or natural_type in ("wood", "forest", "tree_row")
        if not is_forest:
            continue
        geom = area.get("geometry", [])
        interval = _intersection_interval_m(start, end, geom)
        if interval is None:
            continue
        intervals.append({"start_m": interval[0], "end_m": interval[1]})
    intervals.sort(key=lambda item: item["start_m"])
    return intervals

def _project_from_tx(lat: float, lon: float, distance_m: float, bearing_deg: float) -> Tuple[float, float]:
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
