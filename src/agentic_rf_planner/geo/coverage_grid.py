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
    
    # Termination threshold: stop rays once RSRP drops below -150 dBm
    # (or at max range, whichever comes first).
    # This ensures:
    # 1. Rays stop at realistic distances (especially higher frequencies)
    # 2. Frequency affects propagation distance (lower freq = longer range via FSPL)
    # 3. FSPL always applies (signal weakens with distance even in free space)
    # 4. Material/NLOS loss makes rays stop even earlier
    min_rsrp_threshold = float(getattr(rf_params, "termination_rsrp_dbm", -140.0))
    logger.info(f"Ray termination threshold: {min_rsrp_threshold:.1f} dBm (noise floor: {noise_floor_dbm:.1f} dBm)")
    logger.debug(f"  This ensures rays stop when signal becomes too weak for reliable detection")
    logger.debug(f"  Higher frequencies (e.g., 3.5 GHz) will stop earlier than lower frequencies (e.g., 622 MHz)")
    logger.debug(f"  FSPL always applies, material loss adds on top")
    
    # Import here to avoid circular dependency
    from ..rf.material_penetration import get_penetration_loss_for_material, is_material_blocking
    from ..rf.attenuation_models import _free_space_path_loss_db

    # Building attenuation config (overall + per-material from rf.params.yaml)
    bldg_atten_cfg = getattr(rf_params, "building_attenuation", None)

    # Get frequency once (used for all calculations)
    freq_mhz = rf_params.freq_mhz
    
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

    # Adaptive dtheta for 3D OSM-only mode:
    # target arc-length ~= 4*dr (clamped) at max range.
    if ray_mode_eff in ("3d_osm", "3d-osm", "osm3d"):
        target_arc_m = max(12.0, min(30.0, 4.0 * dr))
        dtheta_target = math.degrees(target_arc_m / max(1.0, max_r))
        dtheta = max(0.25, min(dtheta_user, dtheta_target))
        # Persist the effective dtheta so downstream PNG masking can match the simulation.
        try:
            rf_params.dtheta_deg = float(dtheta)
        except Exception:
            pass
        logger.info(
            f"3D OSM-only: using adaptive dtheta={dtheta:.3f}° "
            f"(user={dtheta_user:.3f}°, target_arc≈{target_arc_m:.1f}m at R={max_r:.0f}m)"
        )
    else:
        dtheta = dtheta_user

    store_building_lists = ray_mode_eff not in ("3d_osm", "3d-osm", "osm3d")

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
                # Per-ray precomputation:
                # - Query buildings ONCE for the full ray (TX -> max_range)
                # - Compute first-intersection distance per building
                # - Incrementally accumulate loss as r increases
                r = dr
                cumulative_building_loss_db = 0.0
                metal_blocked = False

                # When rendering as a raster/PNG (3d_osm), we don't need to attach full building
                # metadata per cell. Avoid per-cell list copies for memory/perf.
                encountered_buildings = [] if store_building_lists else None

                building_hits = []  # list of (dist_m, building_dict)
                next_hit_idx = 0

                # Forest: compute first intersection once per ray (OSM provider fast-path).
                forest_hit_dist_m = None

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
                        for building_id in candidate_ids:
                            b = by_id.get(building_id)
                            if not b:
                                continue
                            geom = b.get("geometry", [])
                            d_hit = _first_intersection_distance_m(tx, far_latlon, geom)
                            if d_hit is None:
                                continue
                            building_hits.append((d_hit, b))
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
                                    d_hit = float(b.get("r0_m", 0.0))
                                except Exception:
                                    continue
                                building_hits.append((d_hit, b))
                                continue

                            geom = b.get("geometry", [])
                            d_hit = _first_intersection_distance_m(tx, far_latlon, geom)
                            if d_hit is None:
                                continue
                            building_hits.append((d_hit, b))

                    building_hits.sort(key=lambda t: t[0])

                    # Forest/vegetation: compute first hit if possible (one query per theta).
                    # - OSM provider: use landuse polygons/quadtree
                    # - Google-mesh provider: use persisted tree segments (r0_m)
                    forest_hit_dist_m = None
                    try:
                        tree_by_bin = getattr(map_provider, "_tree_segments_by_bin", None)
                        dtheta_p = float(getattr(map_provider, "dtheta_deg", 0.0) or 0.0)
                        if tree_by_bin is not None and dtheta_p > 0.0:
                            n_bins = int(round(360.0 / dtheta_p)) or 1
                            ti = int(round(theta / dtheta_p)) % n_bins
                            segs = tree_by_bin.get(ti, []) or []
                            if segs:
                                forest_hit_dist_m = min(float(s.get("r0_m", 0.0)) for s in segs if s.get("r0_m") is not None)
                    except Exception:
                        forest_hit_dist_m = None

                    if forest_hit_dist_m is None:
                        try:
                            forest_hit_dist_m = _first_forest_intersection_distance_m(map_provider, tx, far_latlon)
                        except Exception:
                            forest_hit_dist_m = None

                # Precompute wood loss constant (applied once when forest is encountered).
                wood_loss_db = (
                    get_penetration_loss_for_material("wood", sector_freq_mhz, attenuation_config=bldg_atten_cfg)
                    if map_provider is not None else 0.0
                )

                while r <= max_r:
                    lat, lon = _project_from_tx(tx.lat, tx.lon, r, theta)
                    cell_latlon = LatLon(lat=lat, lon=lon)

                    # Update cumulative building loss for this r (even if this cell is skipped by a polygon sector),
                    # so farther points still include all prior obstruction loss.
                    while next_hit_idx < len(building_hits) and building_hits[next_hit_idx][0] <= (r + 1e-6):
                        b = building_hits[next_hit_idx][1]
                        material = b.get("material", "unknown")
                        penetration_loss = get_penetration_loss_for_material(
                            material, sector_freq_mhz, attenuation_config=bldg_atten_cfg
                        )
                        cumulative_building_loss_db += penetration_loss

                        if is_material_blocking(material, sector_freq_mhz):
                            metal_blocked = True

                        if encountered_buildings is not None:
                            encountered_buildings.append(b)

                        next_hit_idx += 1

                    # For polygon sectors, check if point is inside polygon. If not, skip creating a cell.
                    if sector.sector_type == "polygon":
                        is_inside = sector.covers_point(lat, lon, tx.lat, tx.lon)
                        if not is_inside:
                            r += dr
                            continue

                    # LOS depends only on buildings.
                    is_los = (next_hit_idx == 0)

                    # Forest encountered?
                    has_forest = False
                    if map_provider is not None:
                        if forest_hit_dist_m is not None:
                            has_forest = forest_hit_dist_m <= (r + 1e-6)
                        else:
                            # Fallback (slower): query per cell.
                            try:
                                has_forest = map_provider.is_forest_between(tx, cell_latlon)
                            except Exception:
                                has_forest = False

                    # NLOS excess loss (matches attenuation_models.py)
                    nlos_excess_loss_db = 0.0
                    if not is_los:
                        nlos_excess_loss_db = 12.0
                        if r > 50.0:
                            nlos_excess_loss_db += 0.02 * (r - 50.0)

                    # Total material loss persists after exiting obstacles.
                    cumulative_material_loss_db = cumulative_building_loss_db + (wood_loss_db if has_forest else 0.0)

                    # Always compute FSPL (signal weakens with distance in free space)
                    fspl_db = _free_space_path_loss_db(r, sector_freq_mhz)

                    # Compute signal strength: FSPL + NLOS excess + material loss
                    total_loss_db = fspl_db + nlos_excess_loss_db + cumulative_material_loss_db
                    estimated_rsrp = sector_tx_power_dbm - total_loss_db

                    # Stop ray if signal too weak
                    if estimated_rsrp < min_rsrp_threshold:
                        logger.debug(
                            f"Ray at bearing {theta:.1f}° (sector {sector.sector_id}) terminated at {r:.1f}m "
                            f"(RSRP={estimated_rsrp:.1f}dBm < threshold {min_rsrp_threshold:.1f}dBm)"
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
                            extra_loss_db=0.0,
                            is_los=is_los,
                            actual_path_length_m=r,
                            num_buildings=next_hit_idx,
                            num_trees=(1 if has_forest else 0),
                            diffraction_flag=False,
                            cumulative_material_loss_db=cumulative_material_loss_db,
                            metal_blocked=metal_blocked,
                            buildings_along_path=(list(encountered_buildings) if encountered_buildings is not None else []),
                        )
                    )

                    r += dr

                theta += dtheta
    
    # Note: We've processed all sectors, so we're done
    
    logger.info(f"Generated {len(cells)} cells with adaptive ray termination (max would be {int(360/dtheta) * int(max_r/dr)})")
    return cells



def _enu_from_tx(tx: LatLon, p: LatLon) -> tuple[float, float]:
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


def _first_intersection_distance_m(start: LatLon, end: LatLon, polygon: List[dict]) -> Optional[float]:
    """Return earliest intersection distance along (start->end) with polygon boundary, in meters.

    Returns 0.0 if the start point lies inside the polygon (attenuation begins immediately).
    """
    if not polygon or len(polygon) < 3:
        return None

    # Build polygon points in ENU (east, north) relative to start.
    pts: List[tuple[float, float]] = []
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

    def _point_in_poly(x: float, y: float, poly: List[tuple[float, float]]) -> bool:
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

    # If TX is inside the polygon, treat the first hit as distance 0.
    if _point_in_poly(0.0, 0.0, pts):
        return 0.0

    end_e, end_n = _enu_from_tx(start, end)
    seg_len = math.hypot(end_e, end_n)
    if seg_len <= 1e-6:
        return None

    best_t: Optional[float] = None
    npts = len(pts)
    for i in range(npts):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % npts]
        t = _segment_intersection_t(0.0, 0.0, end_e, end_n, ax, ay, bx, by)
        if t is None:
            continue
        if best_t is None or t < best_t:
            best_t = t

    if best_t is None:
        return None
    return best_t * seg_len


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
