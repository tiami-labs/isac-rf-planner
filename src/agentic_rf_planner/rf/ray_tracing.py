"""Lightweight 2.5D specular multipath ray tracing.

This repo's baseline propagation is a polar ray-march that applies scenario path loss
plus deterministic obstruction losses (penetration/shadow/diffraction).

This module adds an *additional* multipath contribution for a sample receiver point:
  - Direct path (already handled elsewhere)
  - Single-bounce specular reflections off vertical building walls derived from OSM

The intent is to provide an "Omniverse-like" ray/path-tracing *mode* without changing
map-provider contracts: we still use the existing OSM/mesh providers for fetching
buildings, but reflections require OSM footprint geometry.

Notes / scope:
  - 2D wall geometry (ENU) with 2.5D heights; reflection is computed in plan-view.
  - Only 1-bounce reflections are implemented here.
  - Path validity checks are conservative: if either segment intersects a building,
    the reflected path is rejected.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

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

    kind: str  # direct | reflect | reflect2
    points: List[LatLon]  # includes tx, (bounce), rx
    rsrp_dbm: float
    meta: Dict[str, Any] = field(default_factory=dict)


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

    # Unit normal to the line.
    nx, ny = -vy, vx

    # Signed distance from p to the line.
    apx, apy = px - ax, py - ay
    dist = _dot(apx, apy, nx, ny)

    # Reflection: p' = p - 2*dist*n
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

        # Ensure closed polygon.
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

    # Incidence factor: keep it bounded.
    ci = max(0.0, min(1.0, abs(cos_incidence)))
    angle_loss = 6.0 * (1.0 - ci)
    return base + mat_loss + angle_loss


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
    """Compute top-N single-bounce reflection paths TX -> wall -> RX.

    The caller provides:
      - is_path_clear_fn(p0, p1, exclude_building_id) -> bool
      - rsrp_for_path_fn(tx, bounce, rx, total_distance_m, extra_loss_db) -> float (dBm)

    This keeps this module independent of MapProvider specifics.
    """

    tx_e, tx_n = 0.0, 0.0
    rx_e, rx_n = enu_from_latlon(tx, rx)

    # Pick nearby wall segments to the receiver to keep this cheap.
    candidates: List[Tuple[float, WallSegment]] = []
    for seg in wall_segments:
        d = _distance_point_to_segment_m(rx_e, rx_n, seg.a_e, seg.a_n, seg.b_e, seg.b_n)
        candidates.append((d, seg))
    candidates.sort(key=lambda x: x[0])
    candidates = candidates[: max(1, int(max_candidates))]

    paths: List[RayPath] = []
    for _, seg in candidates:
        a = (seg.a_e, seg.a_n)
        b = (seg.b_e, seg.b_n)

        tx_ref = _reflect_point_across_line((tx_e, tx_n), a, b)

        # Bounce point is where the mirrored-TX -> RX line hits the wall segment.
        bounce = _segment_intersection_point(tx_ref, (rx_e, rx_n), a, b)
        if bounce is None:
            continue

        bx, by = bounce
        # Reject degenerate bounces.
        if _norm(bx - tx_e, by - tx_n) < 2.0 or _norm(rx_e - bx, rx_n - by) < 2.0:
            continue

        # Conservative clearance test for both legs.
        bounce_ll = latlon_from_enu(tx, bx, by)
        if not is_path_clear_fn(tx, bounce_ll, seg.building_id):
            continue
        if not is_path_clear_fn(bounce_ll, rx, seg.building_id):
            continue

        # Reflection loss (dB).
        # Compute incidence cosine against a wall normal (2D).
        vx, vy = (b[0] - a[0], b[1] - a[1])
        vlen = _norm(vx, vy)
        if vlen < 1e-9:
            continue
        vx /= vlen
        vy /= vlen
        nx, ny = (-vy, vx)
        ix, iy = (bx - tx_e, by - tx_n)
        ilen = _norm(ix, iy)
        if ilen < 1e-9:
            continue
        ix /= ilen
        iy /= ilen
        cos_inc = _dot(ix, iy, nx, ny)
        refl_loss = _reflection_loss_db(seg.material, cos_inc, rf_params)

        d1 = _norm(bx - tx_e, by - tx_n)
        d2 = _norm(rx_e - bx, rx_n - by)
        total_d = d1 + d2
        rsrp = rsrp_for_path_fn(tx, bounce_ll, rx, total_d, refl_loss)
        paths.append(RayPath(
            kind="reflect",
            points=[tx, bounce_ll, rx],
            rsrp_dbm=rsrp,
            meta={
                "wall1": {
                    "a_e": seg.a_e, "a_n": seg.a_n, "b_e": seg.b_e, "b_n": seg.b_n,
                    "building_id": seg.building_id, "material": seg.material,
                },
                "distances_m": [d1, d2],
            },
        ))

    paths.sort(key=lambda p: p.rsrp_dbm, reverse=True)
    return paths[: max(0, int(max_return))]


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
    """Compute top-N two-bounce specular reflection paths TX -> wall1 -> wall2 -> RX.

    2D mirror method in ENU:
      - Reflect TX across wall1, then across wall2 => TX''
      - Line TX'' -> RX intersects wall2 at P2
      - Line TX'  -> P2 intersects wall1 at P1

    Clearance checks are conservative (any building intersection on a leg rejects).
    """

    tx_e, tx_n = 0.0, 0.0
    rx_e, rx_n = enu_from_latlon(tx, rx)

    # Candidate walls near the receiver (keeps O(N^2) manageable).
    ranked: List[Tuple[float, WallSegment]] = []
    for seg in wall_segments:
        d = _distance_point_to_segment_m(rx_e, rx_n, seg.a_e, seg.a_n, seg.b_e, seg.b_n)
        ranked.append((d, seg))
    ranked.sort(key=lambda x: x[0])
    ranked = ranked[: max(2, int(max_candidates))]
    segs = [s for _, s in ranked]

    paths: List[RayPath] = []

    for seg2 in segs:
        a2 = (seg2.a_e, seg2.a_n)
        b2 = (seg2.b_e, seg2.b_n)

        for seg1 in segs:
            # Allow same building but different edge; skip identical segment object.
            if seg1 is seg2:
                continue

            a1 = (seg1.a_e, seg1.a_n)
            b1 = (seg1.b_e, seg1.b_n)

            # Mirror TX across wall1, then wall2.
            tx1 = _reflect_point_across_line((tx_e, tx_n), a1, b1)
            tx2 = _reflect_point_across_line(tx1, a2, b2)

            # Solve bounce points via segment intersections.
            p2 = _segment_intersection_point(tx2, (rx_e, rx_n), a2, b2)
            if p2 is None:
                continue
            p2x, p2y = p2
            p1 = _segment_intersection_point(tx1, (p2x, p2y), a1, b1)
            if p1 is None:
                continue
            p1x, p1y = p1

            # Reject degenerate geometry.
            if _norm(p1x - tx_e, p1y - tx_n) < 2.0:
                continue
            if _norm(p2x - p1x, p2y - p1y) < 2.0:
                continue
            if _norm(rx_e - p2x, rx_n - p2y) < 2.0:
                continue

            p1_ll = latlon_from_enu(tx, p1x, p1y)
            p2_ll = latlon_from_enu(tx, p2x, p2y)

            # Clearance checks (exclude the building for the wall we bounce on).
            if not is_path_clear_fn(tx, p1_ll, seg1.building_id):
                continue
            if not is_path_clear_fn(p1_ll, p2_ll, None):
                continue
            if not is_path_clear_fn(p2_ll, rx, seg2.building_id):
                continue

            # Reflection losses.
            def _seg_loss(seg: WallSegment, a: tuple[float,float], b: tuple[float,float], in_vec: tuple[float,float]) -> float:
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

            loss1 = _seg_loss(seg1, a1, b1, (p1x - tx_e, p1y - tx_n))
            if not math.isfinite(loss1) or loss1 > 1e8:
                continue
            loss2 = _seg_loss(seg2, a2, b2, (p2x - p1x, p2y - p1y))
            if not math.isfinite(loss2) or loss2 > 1e8:
                continue
            refl_loss_total = float(loss1 + loss2)

            d1 = _norm(p1x - tx_e, p1y - tx_n)
            d2 = _norm(p2x - p1x, p2y - p1y)
            d3 = _norm(rx_e - p2x, rx_n - p2y)
            total_d = d1 + d2 + d3

            # Use departure bearing from TX->P1 for pattern loss (approx).
            rsrp = rsrp_for_path_fn(tx, p1_ll, rx, total_d, refl_loss_total)
            paths.append(RayPath(
                kind="reflect2",
                points=[tx, p1_ll, p2_ll, rx],
                rsrp_dbm=rsrp,
                meta={
                    "wall1": {
                        "a_e": seg1.a_e, "a_n": seg1.a_n, "b_e": seg1.b_e, "b_n": seg1.b_n,
                        "building_id": seg1.building_id, "material": seg1.material,
                    },
                    "wall2": {
                        "a_e": seg2.a_e, "a_n": seg2.a_n, "b_e": seg2.b_e, "b_n": seg2.b_n,
                        "building_id": seg2.building_id, "material": seg2.material,
                    },
                    "distances_m": [d1, d2, d3],
                },
            ))

    paths.sort(key=lambda p: p.rsrp_dbm, reverse=True)
    return paths[: max(0, int(max_return))]
