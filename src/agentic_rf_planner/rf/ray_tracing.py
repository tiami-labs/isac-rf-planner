"""Lightweight 2.5D specular multipath ray tracing.

This repo's baseline propagation is a polar ray-march that applies scenario path loss
plus deterministic obstruction losses (penetration/shadow/diffraction).

This module adds a multipath path-search engine that works with the existing OSM and
Google-mesh-backed providers. OSM footprints provide candidate reflecting walls and
material semantics; higher-fidelity mesh checks can be injected through the caller's
clearance callback.

Scope:
  - 2D wall geometry (ENU) with 2.5D heights; reflection is computed in plan-view.
  - Multi-bounce beam search up to a caller-configured limit.
  - Path validity checks are delegated to the caller so the same engine can be used
    with OSM-only, Google-mesh-assisted, or hybrid clearance logic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from ..pipeline.schemas import LatLon, RFParams


EARTH_R_M = 6371000.0


@dataclass(frozen=True)
class WallSegment:
    """One building wall edge."""

    a_e: float
    a_n: float
    b_e: float
    b_n: float
    building_id: Optional[int] = None
    material: str = "unknown"


@dataclass(frozen=True)
class RayPath:
    """A piecewise-linear RF path used for debug rendering."""

    kind: str  # direct | reflect | reflectN
    points: List[LatLon]  # includes tx, (bounce*), rx
    rsrp_dbm: float
    sector_id: Optional[str] = None
    total_distance_m: float = 0.0
    extra_loss_db: float = 0.0


@dataclass(frozen=True)
class SolvedBouncePath:
    """Internal bounce solution in ENU and geographic coordinates."""

    wall_sequence: Tuple[WallSegment, ...]
    bounce_points_enu: Tuple[Tuple[float, float], ...]
    bounce_points_ll: Tuple[LatLon, ...]
    total_distance_m: float
    reflection_loss_db: float


PathClearFn = Callable[[LatLon, LatLon, object], bool]
GenericPathRsrpFn = Callable[[Sequence[LatLon], float, float], float]


def enu_from_latlon(origin: LatLon, p: LatLon) -> Tuple[float, float]:
    """Local equirectangular ENU approximation (east, north) in meters."""
    lat0 = math.radians(origin.lat)
    dlat = math.radians(p.lat - origin.lat)
    dlon = math.radians(p.lon - origin.lon)
    north = dlat * EARTH_R_M
    east = dlon * EARTH_R_M * math.cos(lat0)
    return east, north


def latlon_from_enu(origin: LatLon, east_m: float, north_m: float) -> LatLon:
    """Inverse of enu_from_latlon using the same local approximation."""
    lat0 = math.radians(origin.lat)
    dlat = north_m / EARTH_R_M
    dlon = east_m / (EARTH_R_M * max(math.cos(lat0), 1e-9))
    return LatLon(lat=origin.lat + math.degrees(dlat), lon=origin.lon + math.degrees(dlon))


def _dot(ax: float, ay: float, bx: float, by: float) -> float:
    return ax * bx + ay * by


def _norm(ax: float, ay: float) -> float:
    return math.sqrt(ax * ax + ay * ay)


def _segment_intersection_point(
    p0: Tuple[float, float],
    p1: Tuple[float, float],
    q0: Tuple[float, float],
    q1: Tuple[float, float],
    *,
    eps: float = 1e-9,
) -> Optional[Tuple[float, float]]:
    """Return intersection point of segment P with segment Q, else None."""
    (x1, y1), (x2, y2) = p0, p1
    (x3, y3), (x4, y4) = q0, q1

    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(den) < eps:
        return None

    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den
    u = ((x1 - x3) * (y1 - y2) - (y1 - y3) * (x1 - x2)) / den

    if -eps <= t <= 1.0 + eps and -eps <= u <= 1.0 + eps:
        px = x1 + t * (x2 - x1)
        py = y1 + t * (y2 - y1)
        return px, py
    return None


def _reflect_point_across_line(
    p: Tuple[float, float],
    a: Tuple[float, float],
    b: Tuple[float, float],
) -> Tuple[float, float]:
    """Reflect point p across the infinite line through a->b."""
    px, py = p
    ax, ay = a
    bx, by = b
    vx, vy = bx - ax, by - ay
    vnorm = _norm(vx, vy)
    if vnorm < 1e-9:
        return p
    vx /= vnorm
    vy /= vnorm

    nx, ny = -vy, vx
    apx, apy = px - ax, py - ay
    dist = _dot(apx, apy, nx, ny)
    return (px - 2.0 * dist * nx, py - 2.0 * dist * ny)


def extract_wall_segments(buildings: Sequence[dict], origin: LatLon) -> List[WallSegment]:
    """Extract wall segments (polygon edges) from OSM building geometries."""
    segments: List[WallSegment] = []
    for b in buildings:
        geom = b.get("geometry") or []
        if not isinstance(geom, list) or len(geom) < 2:
            continue
        mat = str(b.get("material", "unknown") or "unknown")
        bid = b.get("id")
        try:
            bid_int = int(bid) if bid is not None else None
        except Exception:
            bid_int = None

        pts: List[Tuple[float, float]] = []
        for node in geom:
            if not isinstance(node, dict) or "lat" not in node or "lon" not in node:
                continue
            e, n = enu_from_latlon(origin, LatLon(lat=float(node["lat"]), lon=float(node["lon"])))
            pts.append((e, n))

        if len(pts) < 2:
            continue

        if _norm(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) > 1.0:
            pts.append(pts[0])

        for i in range(len(pts) - 1):
            (ae, an) = pts[i]
            (be, bn) = pts[i + 1]
            if _norm(be - ae, bn - an) < 0.25:
                continue
            segments.append(WallSegment(a_e=ae, a_n=an, b_e=be, b_n=bn, building_id=bid_int, material=mat))

    return segments


def _distance_point_to_segment_m(
    px: float,
    py: float,
    ax: float,
    ay: float,
    bx: float,
    by: float,
) -> float:
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    vv = vx * vx + vy * vy
    if vv < 1e-9:
        return _norm(px - ax, py - ay)
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / vv))
    cx, cy = ax + t * vx, ay + t * vy
    return _norm(px - cx, py - cy)


def _distance_segment_to_segment_m(
    a0: Tuple[float, float],
    a1: Tuple[float, float],
    b0: Tuple[float, float],
    b1: Tuple[float, float],
) -> float:
    if _segment_intersection_point(a0, a1, b0, b1) is not None:
        return 0.0
    return min(
        _distance_point_to_segment_m(a0[0], a0[1], b0[0], b0[1], b1[0], b1[1]),
        _distance_point_to_segment_m(a1[0], a1[1], b0[0], b0[1], b1[0], b1[1]),
        _distance_point_to_segment_m(b0[0], b0[1], a0[0], a0[1], a1[0], a1[1]),
        _distance_point_to_segment_m(b1[0], b1[1], a0[0], a0[1], a1[0], a1[1]),
    )


def _reflection_loss_db(material: str, cos_incidence: float, rf_params: RFParams) -> float:
    """Simple reflection loss model: base + material + incidence factor."""
    base = float(getattr(rf_params, "rt_reflection_loss_db", 8.0) or 8.0)
    mat = (material or "unknown").lower()
    mat_map = {
        "metal": 3.0,
        "steel": 3.0,
        "concrete": 7.0,
        "brick": 8.0,
        "stone": 8.0,
        "glass": 6.0,
        "wood": 12.0,
        "unknown": 9.0,
    }
    mat_loss = mat_map.get(mat, mat_map["unknown"])
    ci = max(0.0, min(1.0, abs(cos_incidence)))
    angle_loss = 6.0 * (1.0 - ci)
    return base + mat_loss + angle_loss


def _segment_loss(seg: WallSegment, a: Tuple[float, float], b: Tuple[float, float], in_vec: Tuple[float, float], rf_params: RFParams) -> float:
    vx, vy = (b[0] - a[0], b[1] - a[1])
    vlen = _norm(vx, vy)
    if vlen < 1e-9:
        return 1e9
    vx /= vlen
    vy /= vlen
    nx, ny = (-vy, vx)
    ix, iy = in_vec
    ilen = _norm(ix, iy)
    if ilen < 1e-9:
        return 1e9
    ix /= ilen
    iy /= ilen
    cos_inc = _dot(ix, iy, nx, ny)
    return _reflection_loss_db(seg.material, cos_inc, rf_params)


def _coerce_exclude_ids(exclude_ids: object) -> set[int]:
    if exclude_ids is None:
        return set()
    if isinstance(exclude_ids, (list, tuple, set, frozenset)):
        out: set[int] = set()
        for item in exclude_ids:
            try:
                if item is not None:
                    out.add(int(item))
            except Exception:
                continue
        return out
    try:
        return {int(exclude_ids)}
    except Exception:
        return set()


def _solve_bounce_sequence(tx: LatLon, rx: LatLon, wall_sequence: Sequence[WallSegment], rf_params: RFParams) -> Optional[SolvedBouncePath]:
    if not wall_sequence:
        return None

    tx_e, tx_n = 0.0, 0.0
    rx_e, rx_n = enu_from_latlon(tx, rx)

    reflected_sources: List[Tuple[float, float]] = []
    mirrored = (tx_e, tx_n)
    for seg in wall_sequence:
        a = (seg.a_e, seg.a_n)
        b = (seg.b_e, seg.b_n)
        mirrored = _reflect_point_across_line(mirrored, a, b)
        reflected_sources.append(mirrored)

    bounces_enu: List[Tuple[float, float]] = [(-1.0, -1.0)] * len(wall_sequence)
    next_target = (rx_e, rx_n)
    for j in range(len(wall_sequence) - 1, -1, -1):
        seg = wall_sequence[j]
        a = (seg.a_e, seg.a_n)
        b = (seg.b_e, seg.b_n)
        source = reflected_sources[j]
        p = _segment_intersection_point(source, next_target, a, b)
        if p is None:
            return None
        bounces_enu[j] = p
        next_target = p

    enu_points: List[Tuple[float, float]] = [(tx_e, tx_n), *bounces_enu, (rx_e, rx_n)]
    for i in range(len(enu_points) - 1):
        if _norm(enu_points[i + 1][0] - enu_points[i][0], enu_points[i + 1][1] - enu_points[i][1]) < 2.0:
            return None

    reflection_loss_db = 0.0
    total_distance_m = 0.0
    for idx, seg in enumerate(wall_sequence):
        prev_pt = enu_points[idx]
        bounce_pt = enu_points[idx + 1]
        reflection_loss_db += _segment_loss(
            seg,
            (seg.a_e, seg.a_n),
            (seg.b_e, seg.b_n),
            (bounce_pt[0] - prev_pt[0], bounce_pt[1] - prev_pt[1]),
            rf_params,
        )

    for i in range(len(enu_points) - 1):
        total_distance_m += _norm(enu_points[i + 1][0] - enu_points[i][0], enu_points[i + 1][1] - enu_points[i][1])

    if not math.isfinite(reflection_loss_db) or reflection_loss_db > 1e8:
        return None

    bounce_points_ll = tuple(latlon_from_enu(tx, p[0], p[1]) for p in bounces_enu)
    return SolvedBouncePath(
        wall_sequence=tuple(wall_sequence),
        bounce_points_enu=tuple(bounces_enu),
        bounce_points_ll=bounce_points_ll,
        total_distance_m=float(total_distance_m),
        reflection_loss_db=float(reflection_loss_db),
    )


def _path_is_clear(
    tx: LatLon,
    rx: LatLon,
    solved: SolvedBouncePath,
    is_path_clear_fn: PathClearFn,
) -> bool:
    points_ll: List[LatLon] = [tx, *solved.bounce_points_ll, rx]
    walls = solved.wall_sequence
    k = len(walls)
    for leg_idx in range(k + 1):
        exclude_ids: set[int] = set()
        if leg_idx > 0 and walls[leg_idx - 1].building_id is not None:
            exclude_ids.add(int(walls[leg_idx - 1].building_id))
        if leg_idx < k and walls[leg_idx].building_id is not None:
            exclude_ids.add(int(walls[leg_idx].building_id))
        if not is_path_clear_fn(points_ll[leg_idx], points_ll[leg_idx + 1], exclude_ids):
            return False
    return True


def _path_kind(num_bounces: int) -> str:
    if num_bounces <= 0:
        return "direct"
    if num_bounces == 1:
        return "reflect"
    return f"reflect{num_bounces}"


def _rank_wall_candidates(
    wall_segments: Sequence[WallSegment],
    ref_a: Tuple[float, float],
    ref_b: Tuple[float, float],
    *,
    exclude_segments: Optional[set[int]] = None,
    max_candidates: int = 40,
) -> List[int]:
    scored: List[Tuple[float, int]] = []
    exclude_segments = exclude_segments or set()
    for idx, seg in enumerate(wall_segments):
        if idx in exclude_segments:
            continue
        score = _distance_segment_to_segment_m(
            ref_a,
            ref_b,
            (seg.a_e, seg.a_n),
            (seg.b_e, seg.b_n),
        )
        scored.append((score, idx))
    scored.sort(key=lambda item: item[0])
    return [idx for _, idx in scored[: max(1, int(max_candidates))]]


def compute_multi_bounce_paths(
    tx: LatLon,
    rx: LatLon,
    wall_segments: Sequence[WallSegment],
    *,
    max_bounces: int,
    max_candidates: int,
    max_return: int,
    rf_params: RFParams,
    is_path_clear_fn: PathClearFn,
    rsrp_for_path_fn: GenericPathRsrpFn,
    termination_rsrp_dbm: Optional[float] = None,
) -> List[RayPath]:
    """Compute multi-bounce reflection paths via a beam search over wall sequences.

    The search supports up to ``max_bounces`` reflections. It stops expanding any
    partial solution as soon as its current RSRP falls below the configured
    termination threshold, mirroring the planner's adaptive ray stop criterion.
    """

    if not wall_segments or max_bounces <= 0 or max_return <= 0:
        return []

    term_dbm = float(
        termination_rsrp_dbm
        if termination_rsrp_dbm is not None
        else getattr(rf_params, "termination_rsrp_dbm", -140.0)
    )

    tx_e, tx_n = 0.0, 0.0
    rx_e, rx_n = enu_from_latlon(tx, rx)

    global_candidates = _rank_wall_candidates(
        wall_segments,
        (tx_e, tx_n),
        (rx_e, rx_n),
        max_candidates=max_candidates,
    )
    if not global_candidates:
        return []

    branch_factor = max(2, min(8, len(global_candidates), int(max_candidates)))
    beam_width = max(4, min(len(global_candidates), max(8, int(max_return) * 4)))

    frontier: List[Tuple[Tuple[int, ...], Optional[SolvedBouncePath], float]] = [((), None, float("inf"))]
    all_paths: List[RayPath] = []
    seen_sequences: set[Tuple[int, ...]] = set()

    for depth in range(1, max(1, int(max_bounces)) + 1):
        next_frontier: List[Tuple[Tuple[int, ...], Optional[SolvedBouncePath], float]] = []
        for seq_indices, solved_prefix, _ in frontier:
            exclude = set(seq_indices)
            if solved_prefix is None:
                ref_a = (tx_e, tx_n)
                ref_b = (rx_e, rx_n)
                ranked = global_candidates
            else:
                last_bounce = solved_prefix.bounce_points_enu[-1]
                ref_a = last_bounce
                ref_b = (rx_e, rx_n)
                ranked = _rank_wall_candidates(
                    wall_segments,
                    ref_a,
                    ref_b,
                    exclude_segments=exclude,
                    max_candidates=max_candidates,
                )
                if not ranked:
                    ranked = global_candidates

            for seg_idx in ranked[:branch_factor]:
                if seq_indices and seg_idx == seq_indices[-1]:
                    continue
                new_seq = tuple([*seq_indices, seg_idx])
                if new_seq in seen_sequences:
                    continue
                seen_sequences.add(new_seq)

                walls = tuple(wall_segments[i] for i in new_seq)
                solved = _solve_bounce_sequence(tx, rx, walls, rf_params)
                if solved is None:
                    continue
                if not _path_is_clear(tx, rx, solved, is_path_clear_fn):
                    continue

                points_ll: List[LatLon] = [tx, *solved.bounce_points_ll, rx]
                rsrp_dbm = float(rsrp_for_path_fn(points_ll, solved.total_distance_m, solved.reflection_loss_db))
                if not math.isfinite(rsrp_dbm):
                    continue

                path = RayPath(
                    kind=_path_kind(len(walls)),
                    points=points_ll,
                    rsrp_dbm=rsrp_dbm,
                    total_distance_m=solved.total_distance_m,
                    extra_loss_db=solved.reflection_loss_db,
                )
                if rsrp_dbm >= term_dbm:
                    all_paths.append(path)
                    if depth < int(max_bounces):
                        next_frontier.append((new_seq, solved, rsrp_dbm))

        if not next_frontier:
            break
        next_frontier.sort(key=lambda item: item[2], reverse=True)
        frontier = next_frontier[:beam_width]

    all_paths.sort(key=lambda p: p.rsrp_dbm, reverse=True)
    return all_paths[: max(0, int(max_return))]


def compute_single_bounce_paths(
    tx: LatLon,
    rx: LatLon,
    wall_segments: Sequence[WallSegment],
    *,
    max_candidates: int,
    max_return: int,
    rf_params: RFParams,
    is_path_clear_fn,
    rsrp_for_path_fn,
) -> List[RayPath]:
    """Compute top-N single-bounce reflection paths TX -> wall -> RX."""

    def _rsrp(points_ll: Sequence[LatLon], total_distance_m: float, extra_loss_db: float) -> float:
        return float(rsrp_for_path_fn(points_ll[0], points_ll[1], points_ll[-1], total_distance_m, extra_loss_db))

    return compute_multi_bounce_paths(
        tx,
        rx,
        wall_segments,
        max_bounces=1,
        max_candidates=max_candidates,
        max_return=max_return,
        rf_params=rf_params,
        is_path_clear_fn=is_path_clear_fn,
        rsrp_for_path_fn=_rsrp,
        termination_rsrp_dbm=getattr(rf_params, "termination_rsrp_dbm", -140.0),
    )


def compute_two_bounce_paths(
    tx: LatLon,
    rx: LatLon,
    wall_segments: Sequence[WallSegment],
    *,
    max_candidates: int,
    max_return: int,
    rf_params: RFParams,
    is_path_clear_fn,
    rsrp_for_path_fn,
) -> List[RayPath]:
    """Compute top-N two-bounce specular reflection paths TX -> wall1 -> wall2 -> RX."""

    def _rsrp(points_ll: Sequence[LatLon], total_distance_m: float, extra_loss_db: float) -> float:
        return float(rsrp_for_path_fn(points_ll[0], points_ll[1], points_ll[-1], total_distance_m, extra_loss_db))

    return compute_multi_bounce_paths(
        tx,
        rx,
        wall_segments,
        max_bounces=2,
        max_candidates=max_candidates,
        max_return=max_return,
        rf_params=rf_params,
        is_path_clear_fn=is_path_clear_fn,
        rsrp_for_path_fn=_rsrp,
        termination_rsrp_dbm=getattr(rf_params, "termination_rsrp_dbm", -140.0),
    )
