"""Lightweight 2D specular multipath ray tracing (plan-view façades, **modeling choice**).

**2D city = infinite-height prisms (abstraction, not a claim about the real city).**
Each footprint is extruded to ``footprint × ℝ_z``: rays live in one horizontal slice;
rooftop paths, over-the-building clearance, and interior penetration are **out of scope**.
Specular interactions are only with **vertical façades**, which appear as polygon **edges**.

**k_min (minimum reflection order):** search ``k = 0, 1, 2, …`` until the first ``k``
with at least one Ω-valid path (open segments in free space; bounces on wall relative
interiors). That is the smallest number of **horizontal façade reflections** admitted by
this model—not a forced 2-bounce solution.

**k_max (modeling cutoff):** upper bound is a **significance / budget** choice (path loss,
enumeration cost), not a unique geometric “maximum necessary” bounce count in a dense city.

**Boundary vs volume:** free space Ω = ℝ² minus building interiors; specular contacts are
boundary events. The image method generates candidates; only the folded polyline in Ω is
physical here. Pass ``footprint_buildings`` for strict interior checks.

Scope: OSM wall segments in ENU; path clearing via ``is_path_clear_fn`` plus optional
footprint validation. For large ``P(n,k)``, multi-bounce search uses a **coarse azimuth ray
march** from Tx to propose ordered façade chains, then **Rx-image steering** and Ω validation
(same acceptance as exhaustive permutations on a smaller pool).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, replace
from itertools import permutations
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..geo.google_mesh.profile_types import BearingProfile, RayProfileSet
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
    source: Optional[str] = None  # "osm" | "mesh" | None


@dataclass(frozen=True)
class RayPath:
    """A piecewise-linear RF path used for debug rendering."""

    kind: str  # direct | reflect | reflect2 | reflect_k
    points: List[LatLon]  # includes tx, (bounce), rx
    rsrp_dbm: float
    meta: Dict[str, Any] = field(default_factory=dict)
    total_distance_m: float = 0.0
    extra_loss_db: float = 0.0


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


def _reflect_unit_vector_across_normal(ix: float, iy: float, nx: float, ny: float) -> Tuple[float, float]:
    """Reflect unit direction (ix,iy) across unit normal (nx,ny)."""
    d = _dot(ix, iy, nx, ny)
    rx, ry = ix - 2.0 * d * nx, iy - 2.0 * d * ny
    rl = _norm(rx, ry)
    if rl < 1e-12:
        return ix, iy
    return rx / rl, ry / rl


def _distance_point_to_forward_ray(
    ax: float,
    ay: float,
    ux: float,
    uy: float,
    px: float,
    py: float,
) -> float:
    """Closest distance from P to ray ``A + t u``, ``t >= 0``, with ``u`` unit."""
    t = max(0.0, _dot(px - ax, py - ay, ux, uy))
    qx, qy = ax + t * ux, ay + t * uy
    return _norm(px - qx, py - qy)


def _max_specular_residual_deg_chain(
    bounce_chain: List[Tuple[float, float]],
    walls: Sequence[WallSegment],
) -> float:
    """Max angle (deg) between geometric outgoing leg and ideal specular for each bounce.

    ``bounce_chain`` is ``[(tx_e,tx_n), B1, ..., Bk, (rx_e,rx_n)]`` in ENU; ``walls`` has
    length ``k`` (edge ``j`` carries bounce ``B_{j+1}``).
    """
    if len(bounce_chain) < 3 or len(walls) != len(bounce_chain) - 2:
        return 180.0
    worst = 0.0
    k = len(walls)
    for j in range(k):
        px, py = bounce_chain[j]
        bx, by = bounce_chain[j + 1]
        nx_, ny_ = bounce_chain[j + 2]
        wj = walls[j]
        ax, ay = wj.a_e, wj.a_n
        bxw, byw = wj.b_e, wj.b_n
        vx, vy = bxw - ax, byw - ay
        wl = _norm(vx, vy)
        if wl < 1e-9:
            return 180.0
        tx_, ty_ = vx / wl, vy / wl
        n1x, n1y = -ty_, tx_
        n2x, n2y = ty_, -tx_
        ihx, ihy = bx - px, by - py
        il = _norm(ihx, ihy)
        if il < 1e-9:
            return 180.0
        ihx, ihy = ihx / il, ihy / il
        ohx, ohy = nx_ - bx, ny_ - by
        ol = _norm(ohx, ohy)
        if ol < 1e-9:
            return 180.0
        ohx, ohy = ohx / ol, ohy / ol
        r1x, r1y = _reflect_unit_vector_across_normal(ihx, ihy, n1x, n1y)
        r2x, r2y = _reflect_unit_vector_across_normal(ihx, ihy, n2x, n2y)
        c1 = max(-1.0, min(1.0, _dot(r1x, r1y, ohx, ohy)))
        c2 = max(-1.0, min(1.0, _dot(r2x, r2y, ohx, ohy)))
        ang = min(math.degrees(math.acos(c1)), math.degrees(math.acos(c2)))
        worst = max(worst, ang)
    return worst


def _k_specular_bounce_points_rx_chain(
    tx_e: float,
    tx_n: float,
    rx_e: float,
    rx_n: float,
    walls: Sequence[WallSegment],
) -> Optional[List[Tuple[float, float]]]:
    """Steered image method: unfold **Rx** across ``e_k, e_{k-1}, …, e_1``, fold to bounce points.

    For ordered façade chain ``(e1,…,ek)``, the launch direction ``Tx → R_image`` is the unique
    candidate; folding recovers ``B1,…,Bk`` on the finite edges.
    """
    k = len(walls)
    if k == 0:
        return []
    seq: List[Tuple[float, float]] = [(rx_e, rx_n)]
    for i in range(k - 1, -1, -1):
        w = walls[i]
        seq.append(
            _reflect_point_across_line(seq[-1], (w.a_e, w.a_n), (w.b_e, w.b_n))
        )
    out_b: List[Tuple[float, float]] = []
    prev = (tx_e, tx_n)
    for j in range(k):
        img = seq[k - j]
        wj = walls[j]
        pj = _segment_intersection_point(prev, img, (wj.a_e, wj.a_n), (wj.b_e, wj.b_n))
        if pj is None:
            return None
        out_b.append(pj)
        prev = pj
    return out_b


def _wall_tuple_key_one(w: WallSegment) -> Tuple[Any, ...]:
    return (w.building_id, round(w.a_e, 4), round(w.a_n, 4), round(w.b_e, 4), round(w.b_n, 4))


def _wall_chain_key_tuple(walls: Sequence[WallSegment]) -> Tuple[Any, ...]:
    return tuple(_wall_tuple_key_one(w) for w in walls)


def _falling_perm_count(n: int, k: int) -> int:
    if k < 0 or k > n:
        return 0
    r = 1
    for i in range(k):
        r *= n - i
    return r


def _ray_segment_intersect_param(
    ox: float,
    oy: float,
    dx: float,
    dy: float,
    ax: float,
    ay: float,
    bx: float,
    by: float,
    *,
    min_t: float = 1e-6,
) -> Optional[Tuple[float, float, float, float]]:
    """Ray ``O + t D`` hits segment ``A + u(B-A)�[0,1]``, ``t≥min_t``. Returns ``(t,u,px,py)``."""
    vx, vy = bx - ax, by - ay
    wx, wy = ax - ox, ay - oy
    den = dx * vy - dy * vx
    if abs(den) < 1e-14:
        return None
    t = (wx * vy - wy * vx) / den
    u = (wx * dy - wy * dx) / den
    if t < min_t:
        return None
    if u < -1e-9 or u > 1.0 + 1e-9:
        return None
    px, py = ox + t * dx, oy + t * dy
    return t, u, px, py


def _specular_reflect_direction_from_wall(
    dx: float,
    dy: float,
    ax: float,
    ay: float,
    bx: float,
    by: float,
) -> Optional[Tuple[float, float]]:
    """Outgoing unit direction after ideal specular reflection on the infinite line through AB."""
    vx, vy = bx - ax, by - ay
    vl = _norm(vx, vy)
    if vl < 1e-12:
        return None
    tx_, ty_ = vx / vl, vy / vl
    n1x, n1y = -ty_, tx_
    n2x, n2y = ty_, -tx_
    if _dot(dx, dy, n1x, n1y) <= _dot(dx, dy, n2x, n2y):
        nx, ny = n1x, n1y
    else:
        nx, ny = n2x, n2y
    if _dot(dx, dy, nx, ny) > 0:
        nx, ny = -nx, -ny
    return _reflect_unit_vector_across_normal(dx, dy, nx, ny)


def _discover_wall_chains_ray_march(
    tx_e: float,
    tx_n: float,
    wall_segments: Sequence[WallSegment],
    *,
    bounce_count: int,
    num_azimuths: int,
    max_path_m: float = 8000.0,
) -> List[Tuple[WallSegment, ...]]:
    """Coarse azimuth sweep from Tx: record ordered façade chains seen along specular ray marches.

    Used when ``P(n,k)`` wall permutations are too large: launch-only **discovers** candidate
    sequences; steering + Ω checks still decide acceptance.
    """
    if bounce_count < 1 or not wall_segments:
        return []
    found: Dict[Tuple[Any, ...], Tuple[WallSegment, ...]] = {}
    n_azi = max(8, int(num_azimuths))
    for i in range(n_azi):
        th = 2.0 * math.pi * (i + 0.5) / float(n_azi)
        dx, dy = math.cos(th), math.sin(th)
        ox, oy = tx_e, tx_n
        chain: List[WallSegment] = []
        path_accum = 0.0
        last_key: Optional[Tuple[Any, ...]] = None
        for _step in range(bounce_count):
            best: Optional[Tuple[float, WallSegment, float, float]] = None
            for w in wall_segments:
                wkey = _wall_tuple_key_one(w)
                if wkey == last_key:
                    continue
                hit = _ray_segment_intersect_param(
                    ox, oy, dx, dy, w.a_e, w.a_n, w.b_e, w.b_n, min_t=1e-4
                )
                if hit is None:
                    continue
                t, _u, px, py = hit
                if path_accum + t > max_path_m:
                    continue
                if not _bounce_on_wall_relative_interior(px, py, w.a_e, w.a_n, w.b_e, w.b_n):
                    continue
                if best is None or t < best[0]:
                    best = (t, w, px, py)
            if best is None:
                break
            t, w, px, py = best
            chain.append(w)
            path_accum += t
            ox, oy = px + dx * 2e-2, py + dy * 2e-2
            nd = _specular_reflect_direction_from_wall(dx, dy, w.a_e, w.a_n, w.b_e, w.b_n)
            if nd is None:
                break
            dx, dy = nd
            last_key = _wall_tuple_key_one(w)
        if len(chain) == bounce_count:
            ck = _wall_chain_key_tuple(chain)
            if ck not in found:
                found[ck] = tuple(chain)
    return list(found.values())


def _try_multi_bounce_rx_chain(
    tx: LatLon,
    rx: LatLon,
    tx_e: float,
    tx_n: float,
    rx_e: float,
    rx_n: float,
    walls: Tuple[WallSegment, ...],
    *,
    rf_params: RFParams,
    is_path_clear_fn,
    rsrp_for_path_fn,
    termination_rsrp_dbm: float,
    footprint_buildings: Optional[Sequence[dict]],
    rx_disk_radius_m: float,
    reflection_residual_max_deg: float,
) -> Optional[RayPath]:
    """One ordered chain: Rx-image steer, then clearance + optional Ω + RSRP threshold."""
    k = len(walls)
    b_pts = _k_specular_bounce_points_rx_chain(tx_e, tx_n, rx_e, rx_n, walls)
    if b_pts is None or len(b_pts) != k:
        return None

    ok = True
    for j, w in enumerate(walls):
        bx, by = b_pts[j]
        if not _bounce_on_wall_relative_interior(bx, by, w.a_e, w.a_n, w.b_e, w.b_n):
            ok = False
            break
    if not ok:
        return None

    if _norm(b_pts[0][0] - tx_e, b_pts[0][1] - tx_n) < 2.0:
        return None
    if _norm(rx_e - b_pts[-1][0], rx_n - b_pts[-1][1]) < 2.0:
        return None
    for j in range(k - 1):
        if _norm(b_pts[j + 1][0] - b_pts[j][0], b_pts[j + 1][1] - b_pts[j][1]) < 2.0:
            return None

    chain_enu = [(tx_e, tx_n)] + list(b_pts) + [(rx_e, rx_n)]
    if _max_specular_residual_deg_chain(chain_enu, walls) > float(reflection_residual_max_deg):
        return None
    lb0, lb1 = b_pts[-1]
    ux, uy = rx_e - lb0, rx_n - lb1
    ul = _norm(ux, uy)
    if ul < 1e-9:
        return None
    ux, uy = ux / ul, uy / ul
    eff_rx_r = float(rx_disk_radius_m) if float(rx_disk_radius_m) > 0.0 else 1e-4
    if _distance_point_to_forward_ray(lb0, lb1, ux, uy, rx_e, rx_n) > eff_rx_r + 1e-6:
        return None

    pts_ll: List[LatLon] = [tx]
    for bx, by in b_pts:
        pts_ll.append(latlon_from_enu(tx, bx, by))
    pts_ll.append(rx)

    if not is_path_clear_fn(tx, pts_ll[1], walls[0].building_id):
        return None
    for i in range(1, k):
        if not is_path_clear_fn(pts_ll[i], pts_ll[i + 1], None):
            return None
    if not is_path_clear_fn(pts_ll[k], rx, walls[k - 1].building_id):
        return None

    extra_loss = 0.0
    prev = (tx_e, tx_n)
    for j, w in enumerate(walls):
        bx, by = b_pts[j]
        a = (w.a_e, w.a_n)
        b = (w.b_e, w.b_n)
        lv = _wall_reflection_loss_db(w, a, b, (bx - prev[0], by - prev[1]), rf_params)
        if not math.isfinite(lv) or lv > 1e8:
            return None
        extra_loss += float(lv)
        prev = (bx, by)

    total_d = 0.0
    prev = (tx_e, tx_n)
    for bx, by in b_pts:
        total_d += _norm(bx - prev[0], by - prev[1])
        prev = (bx, by)
    total_d += _norm(rx_e - prev[0], rx_n - prev[1])

    skip_for_val: Optional[List[Optional[set]]] = None
    omega_skip_meta: Optional[List[Optional[List[int]]]] = None
    if footprint_buildings is not None:
        skips_list: List[Optional[set]] = []
        omega_rows: List[Optional[List[int]]] = []
        for i in range(k + 1):
            acc: set = set()
            if i == 0:
                if walls[0].building_id is not None:
                    acc.add(int(walls[0].building_id))
            elif i < k:
                for w in (walls[i - 1], walls[i]):
                    if w.building_id is not None:
                        acc.add(int(w.building_id))
            else:
                if walls[k - 1].building_id is not None:
                    acc.add(int(walls[k - 1].building_id))
            skips_list.append(acc if acc else None)
            omega_rows.append(sorted(acc) if acc else None)
        skip_for_val = skips_list
        omega_skip_meta = omega_rows

        from .raytrace_2d_validity import validate_specular_polyline_2d_infinite_height

        ok_fp, _why = validate_specular_polyline_2d_infinite_height(
            pts_ll,
            footprint_buildings,
            tx,
            per_segment_skip_building_ids=skip_for_val,
        )
        if not ok_fp:
            return None

    term = float(termination_rsrp_dbm)
    rsrp = float(rsrp_for_path_fn(pts_ll, total_d, extra_loss))
    if rsrp < term:
        return None

    meta: Dict[str, Any] = {"distances_m": [], "k": k, "extra_loss_db": extra_loss}
    prev = (tx_e, tx_n)
    for bx, by in b_pts:
        meta["distances_m"].append(_norm(bx - prev[0], by - prev[1]))
        prev = (bx, by)
    meta["distances_m"].append(_norm(rx_e - prev[0], rx_n - prev[1]))
    if omega_skip_meta is not None:
        meta["omega_segment_skip_bids"] = omega_skip_meta

    return RayPath(
        kind="reflect",
        points=pts_ll,
        rsrp_dbm=rsrp,
        meta=meta,
        total_distance_m=total_d,
        extra_loss_db=extra_loss,
    )


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
            segments.append(
                WallSegment(a_e=ae, a_n=an, b_e=be, b_n=bn, building_id=bid_int, material=mat, source="osm")
            )

    return segments


def _bearing_deg_enu(east: float, north: float) -> float:
    """Bearing clockwise from north, degrees in [0,360)."""
    if abs(east) < 1e-12 and abs(north) < 1e-12:
        return 0.0
    ang = math.degrees(math.atan2(east, north))
    return ang % 360.0


def _nearest_bearing_profile(profiles: Sequence[BearingProfile], bearing_deg: float) -> BearingProfile:
    best = profiles[0]
    best_d = 1e9
    for p in profiles:
        d = abs(((p.bearing_deg - bearing_deg + 180.0) % 360.0) - 180.0)
        if d < best_d:
            best_d = d
            best = p
    return best


class MeshProfileIndex:
    """Radial mesh occupancy from a persisted ``RayProfileSet`` (blocked intervals per bearing)."""

    def __init__(self, profile_set: RayProfileSet, origin: LatLon):
        if not profile_set.profiles:
            raise ValueError("RayProfileSet has no profiles")
        self._set = profile_set
        self._origin = origin
        self._profiles: List[BearingProfile] = list(profile_set.profiles)

    def _dist_blocked_on_profile(self, dist_m: float, prof: BearingProfile) -> bool:
        r = float(dist_m)
        for blk in prof.segments:
            if blk.r0_m - 1e-6 <= r <= blk.r1_m + 1e-6:
                return True
        return False

    def is_segment_clear(
        self,
        tx: LatLon,
        end: LatLon,
        *,
        skip_start_m: float,
        skip_end_m: float,
        sample_step_m: float,
    ) -> bool:
        """True if chord TX–END stays out of mesh-profile blocked radial intervals (sampled).

        For a straight chord from ``tx``, bearing from ``tx`` to points on that chord is
        constant, so one nearest bearing profile applies; distances along the chord are
        compared to that profile's blocked ``[r0_m, r1_m]`` intervals.
        """
        tx_e, tx_n = enu_from_latlon(self._origin, tx)
        end_e, end_n = enu_from_latlon(self._origin, end)
        de, dn = end_e - tx_e, end_n - tx_n
        chord_len = _norm(de, dn)
        if chord_len < 1e-9:
            return True
        bearing = _bearing_deg_enu(de, dn)
        prof = _nearest_bearing_profile(self._profiles, bearing)
        d0 = float(skip_start_m)
        d1 = max(0.0, chord_len - float(skip_end_m))
        if d0 > d1:
            return True
        step = max(0.5, float(sample_step_m))
        s = d0
        while s <= d1 + 1e-6:
            if self._dist_blocked_on_profile(s, prof):
                return False
            s += step
        return True


def extract_mesh_profile_wall_segments(profile_set: RayProfileSet, origin: LatLon) -> List[WallSegment]:
    """Approximate mesh radial blocks as short reflecting wall segments (``source=\"mesh\"``)."""
    walls: List[WallSegment] = []
    if not profile_set.profiles:
        return walls
    e0, n0 = enu_from_latlon(origin, LatLon(lat=profile_set.tx_lat, lon=profile_set.tx_lon))
    dtheta = max(0.5, float(profile_set.dtheta_deg))
    dr = max(1.0, float(profile_set.dr_m))

    for bi, prof in enumerate(profile_set.profiles):
        theta = math.radians(prof.bearing_deg)
        ue = math.sin(theta)
        un = math.cos(theta)
        te = -un
        tn = ue
        for si, seg in enumerate(prof.segments):
            if seg.r1_m <= seg.r0_m + 1e-6:
                continue
            r_mid = 0.5 * (float(seg.r0_m) + float(seg.r1_m))
            half_len = max(3.0, r_mid * math.tan(0.5 * math.radians(min(85.0, dtheta))))
            half_len = max(half_len, dr)
            cx = e0 + r_mid * ue
            cy = n0 + r_mid * un
            bid = int((hash((prof.bearing_deg, seg.r0_m, seg.r1_m, si, bi)) & 0x7FFFFFFF) or 1)
            mat = str(seg.material or "unknown")
            walls.append(
                WallSegment(
                    a_e=cx - half_len * te,
                    a_n=cy - half_len * tn,
                    b_e=cx + half_len * te,
                    b_n=cy + half_len * tn,
                    building_id=bid,
                    material=mat,
                    source="mesh",
                )
            )
    return walls


def _wall_reflection_loss_db(
    seg: WallSegment,
    a: Tuple[float, float],
    b: Tuple[float, float],
    in_vec: Tuple[float, float],
    rf_params: RFParams,
) -> float:
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


def _bounce_on_wall_relative_interior(
    px: float,
    py: float,
    ax: float,
    ay: float,
    bx: float,
    by: float,
    *,
    corner_margin_m: float = 0.85,
    min_frac_from_vertex: float = 0.06,
    on_line_tol_m: float = 0.65,
) -> bool:
    """True if (px,py) lies on the closed segment [a,b] and away from endpoints (no corner specular).

    Pure specular in2D uses wall facets; vertices belong to diffraction models.
    """
    vx, vy = bx - ax, by - ay
    L = _norm(vx, vy)
    if L < 1e-6:
        return False
    wx, wy = px - ax, py - ay
    t = (wx * vx + wy * vy) / (L * L)
    if t < 0.0 or t > 1.0:
        return False
    qx, qy = ax + t * vx, ay + t * vy
    if _norm(px - qx, py - qy) > on_line_tol_m:
        return False
    margin = max(corner_margin_m, min_frac_from_vertex * L)
    if t * L < margin or (1.0 - t) * L < margin:
        return False
    return True


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


def _wall_segment_link_relevance_m(
    seg: WallSegment,
    tx_e: float,
    tx_n: float,
    rx_e: float,
    rx_n: float,
) -> float:
    """How close a wall is to the Tx–Rx corridor (min distance to Tx, Rx, or chord midpoint).

    Ranking only by distance-to-Rx misses facades between the endpoints; urban specular often
    reflects off walls near the link midsection.
    """
    mx, my = 0.5 * (tx_e + rx_e), 0.5 * (tx_n + rx_n)
    d_tx = _distance_point_to_segment_m(tx_e, tx_n, seg.a_e, seg.a_n, seg.b_e, seg.b_n)
    d_rx = _distance_point_to_segment_m(rx_e, rx_n, seg.a_e, seg.a_n, seg.b_e, seg.b_n)
    d_mid = _distance_point_to_segment_m(mx, my, seg.a_e, seg.a_n, seg.b_e, seg.b_n)
    return min(d_tx, d_rx, d_mid)


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
    footprint_buildings: Optional[Sequence[dict]] = None,
    rx_disk_radius_m: float = 0.0,
    reflection_residual_max_deg: float = 2.0,
) -> List[RayPath]:
    """Compute top-N single-bounce reflection paths TX -> wall -> RX.

    The caller provides:
      - is_path_clear_fn(p0, p1, exclude_building_id) -> bool
      - rsrp_for_path_fn(tx, bounce, rx, total_distance_m, extra_loss_db) -> float (dBm)

    This keeps this module independent of MapProvider specifics.
    """

    tx_e, tx_n = 0.0, 0.0
    rx_e, rx_n = enu_from_latlon(tx, rx)

    # Rank walls by relevance to the whole Tx–Rx link (not only near Rx).
    candidates: List[Tuple[float, WallSegment]] = []
    for seg in wall_segments:
        d = _wall_segment_link_relevance_m(seg, tx_e, tx_n, rx_e, rx_n)
        candidates.append((d, seg))
    candidates.sort(key=lambda x: x[0])
    candidates = candidates[: max(1, int(max_candidates))]

    paths: List[RayPath] = []
    for _, seg in candidates:
        a = (seg.a_e, seg.a_n)
        b = (seg.b_e, seg.b_n)

        # Steered image: reflect Rx across the wall; Tx → Rx' intersects the finite edge at B.
        rx_ref = _reflect_point_across_line((rx_e, rx_n), a, b)
        bounce = _segment_intersection_point((tx_e, tx_n), rx_ref, a, b)
        if bounce is None:
            continue

        bx, by = bounce
        # Reject degenerate bounces.
        if _norm(bx - tx_e, by - tx_n) < 2.0 or _norm(rx_e - bx, rx_n - by) < 2.0:
            continue

        if not _bounce_on_wall_relative_interior(bx, by, a[0], a[1], b[0], b[1]):
            continue

        bounce_chain = [(tx_e, tx_n), (bx, by), (rx_e, rx_n)]
        if _max_specular_residual_deg_chain(bounce_chain, [seg]) > float(reflection_residual_max_deg):
            continue

        ux, uy = rx_e - bx, rx_n - by
        ul = _norm(ux, uy)
        if ul < 1e-9:
            continue
        ux, uy = ux / ul, uy / ul
        eff_rx_r = float(rx_disk_radius_m) if float(rx_disk_radius_m) > 0.0 else 1e-4
        if _distance_point_to_forward_ray(bx, by, ux, uy, rx_e, rx_n) > eff_rx_r + 1e-6:
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

        omega_skip_meta: Optional[List[Optional[List[int]]]] = None
        skip_for_val: Optional[List[Optional[set]]] = None
        if seg.building_id is not None:
            _bid = int(seg.building_id)
            bs = {_bid}
            skip_for_val = [bs, bs]
            omega_skip_meta = [[_bid], [_bid]]

        if footprint_buildings is not None:
            from .raytrace_2d_validity import validate_specular_polyline_2d_infinite_height

            ok_fp, _why = validate_specular_polyline_2d_infinite_height(
                [tx, bounce_ll, rx],
                footprint_buildings,
                tx,
                per_segment_skip_building_ids=skip_for_val,
            )
            if not ok_fp:
                continue

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
                "omega_segment_skip_bids": omega_skip_meta,
            },
            total_distance_m=total_d,
            extra_loss_db=refl_loss,
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
    footprint_buildings: Optional[Sequence[dict]] = None,
    rx_disk_radius_m: float = 0.0,
    reflection_residual_max_deg: float = 2.0,
) -> List[RayPath]:
    """Compute top-N two-bounce specular reflection paths TX -> wall1 -> wall2 -> RX.

    Steered image method in ENU: unfold **Rx** across ``e2`` then ``e1``; launch ``Tx → R_image``;
    fold to ``P1`` on ``e1``, ``P2`` on ``e2``.

    Clearance checks are conservative (any building intersection on a leg rejects).

    Optional ``footprint_buildings`` adds a strict 2D infinite-height test: no open segment
    may pass through the interior of an OSM footprint (invalid folded path).

    Bounces must lie on the **relative interior** of each wall segment (endpoints / corners
    are rejected here; treat those with a diffraction model instead).
    """

    tx_e, tx_n = 0.0, 0.0
    rx_e, rx_n = enu_from_latlon(tx, rx)

    # Candidate walls relevant to the Tx–Rx corridor (keeps O(N^2) manageable).
    ranked: List[Tuple[float, WallSegment]] = []
    for seg in wall_segments:
        d = _wall_segment_link_relevance_m(seg, tx_e, tx_n, rx_e, rx_n)
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

            folded = _k_specular_bounce_points_rx_chain(tx_e, tx_n, rx_e, rx_n, (seg1, seg2))
            if folded is None or len(folded) != 2:
                continue
            (p1x, p1y), (p2x, p2y) = folded

            # Reject degenerate geometry.
            if _norm(p1x - tx_e, p1y - tx_n) < 2.0:
                continue
            if _norm(p2x - p1x, p2y - p1y) < 2.0:
                continue
            if _norm(rx_e - p2x, rx_n - p2y) < 2.0:
                continue

            p1_ll = latlon_from_enu(tx, p1x, p1y)
            p2_ll = latlon_from_enu(tx, p2x, p2y)

            if not _bounce_on_wall_relative_interior(p1x, p1y, a1[0], a1[1], b1[0], b1[1]):
                continue
            if not _bounce_on_wall_relative_interior(p2x, p2y, a2[0], a2[1], b2[0], b2[1]):
                continue

            bounce_chain = [(tx_e, tx_n), (p1x, p1y), (p2x, p2y), (rx_e, rx_n)]
            if _max_specular_residual_deg_chain(bounce_chain, (seg1, seg2)) > float(
                reflection_residual_max_deg
            ):
                continue
            ux, uy = rx_e - p2x, rx_n - p2y
            ul = _norm(ux, uy)
            if ul < 1e-9:
                continue
            ux, uy = ux / ul, uy / ul
            eff_rx_r = float(rx_disk_radius_m) if float(rx_disk_radius_m) > 0.0 else 1e-4
            if _distance_point_to_forward_ray(p2x, p2y, ux, uy, rx_e, rx_n) > eff_rx_r + 1e-6:
                continue

            # Clearance checks (exclude the building for the wall we bounce on).
            if not is_path_clear_fn(tx, p1_ll, seg1.building_id):
                continue
            if not is_path_clear_fn(p1_ll, p2_ll, None):
                continue
            if not is_path_clear_fn(p2_ll, rx, seg2.building_id):
                continue

            # Reflection losses.
            loss1 = _wall_reflection_loss_db(seg1, a1, b1, (p1x - tx_e, p1y - tx_n), rf_params)
            if not math.isfinite(loss1) or loss1 > 1e8:
                continue
            loss2 = _wall_reflection_loss_db(seg2, a2, b2, (p2x - p1x, p2y - p1y), rf_params)
            if not math.isfinite(loss2) or loss2 > 1e8:
                continue
            refl_loss_total = float(loss1 + loss2)

            d1 = _norm(p1x - tx_e, p1y - tx_n)
            d2 = _norm(p2x - p1x, p2y - p1y)
            d3 = _norm(rx_e - p2x, rx_n - p2y)
            total_d = d1 + d2 + d3

            omega_skip_meta: Optional[List[Optional[List[int]]]] = None
            skip_for_val: Optional[List[Optional[set]]] = None
            bid1 = seg1.building_id
            bid2 = seg2.building_id
            if bid1 is not None or bid2 is not None:
                i1 = int(bid1) if bid1 is not None else None
                i2 = int(bid2) if bid2 is not None else None
                s0 = {i1} if i1 is not None else set()
                s2 = {i2} if i2 is not None else set()
                mid_ids: set = set()
                if i1 is not None:
                    mid_ids.add(i1)
                if i2 is not None:
                    mid_ids.add(i2)
                skip_for_val = [
                    s0 if s0 else None,
                    mid_ids if mid_ids else None,
                    s2 if s2 else None,
                ]
                omega_skip_meta = [
                    [i1] if i1 is not None else None,
                    sorted(mid_ids) if mid_ids else None,
                    [i2] if i2 is not None else None,
                ]

            if footprint_buildings is not None:
                from .raytrace_2d_validity import validate_specular_polyline_2d_infinite_height

                ok_fp, _why = validate_specular_polyline_2d_infinite_height(
                    [tx, p1_ll, p2_ll, rx],
                    footprint_buildings,
                    tx,
                    per_segment_skip_building_ids=skip_for_val,
                )
                if not ok_fp:
                    continue

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
                    "omega_segment_skip_bids": omega_skip_meta,
                },
                total_distance_m=total_d,
                extra_loss_db=refl_loss_total,
            ))

    paths.sort(key=lambda p: p.rsrp_dbm, reverse=True)
    return paths[: max(0, int(max_return))]


def compute_multi_bounce_paths(
    tx: LatLon,
    rx: LatLon,
    wall_segments: Sequence[WallSegment],
    *,
    min_bounces: int = 1,
    max_bounces: int = 8,
    max_candidates: int,
    max_return: int,
    rf_params: RFParams,
    is_path_clear_fn,
    rsrp_for_path_fn,
    termination_rsrp_dbm: float,
    footprint_buildings: Optional[Sequence[dict]] = None,
    rx_disk_radius_m: float = 0.0,
    reflection_residual_max_deg: float = 2.0,
    max_permutation_enumeration: int = 120_000,
    ray_discovery_azimuths: int = 1536,
    max_random_chains_when_sampling: int = 14_000,
) -> List[RayPath]:
    """Specular paths for bounce counts in ``[min_bounces, max_bounces]``.

    * Small ``P(n,k)``: enumerate permutations of the ranked wall pool (exact for that pool).
    * Large ``P(n,k)``: **ray-march discovery** (coarse azimuth from Tx) plus random ``k``-samples
      from the pool—launch finds candidate chains; **Rx-image steering** + Ω checks accept them.

    ``rsrp_for_path_fn(points_ll, total_distance_m, extra_loss_db) -> float`` receives the full
    polyline including Tx and Rx. Paths weaker than ``termination_rsrp_dbm`` are dropped.
    """

    tx_e, tx_n = 0.0, 0.0
    rx_e, rx_n = enu_from_latlon(tx, rx)

    ranked: List[Tuple[float, WallSegment]] = []
    for seg in wall_segments:
        d = _wall_segment_link_relevance_m(seg, tx_e, tx_n, rx_e, rx_n)
        ranked.append((d, seg))
    ranked.sort(key=lambda x: x[0])
    pool = [s for _, s in ranked[: max(2, int(max_candidates))]]

    k_lo = max(1, int(min_bounces))
    k_hi = min(int(max_bounces), len(pool))
    paths: List[RayPath] = []
    perm_cap = max(1000, int(max_permutation_enumeration))
    n_azi = max(64, int(ray_discovery_azimuths))
    rnd_cap = max(800, int(max_random_chains_when_sampling))
    stop_n = max(1, int(max_return))

    if k_lo > k_hi:
        return []

    for k in range(k_lo, k_hi + 1):
        n = len(pool)
        pcount = _falling_perm_count(n, k)
        seen_keys: set = set()

        def _emit(walls_tuple: Tuple[WallSegment, ...]) -> bool:
            """Return True if enough valid paths collected for this ``k``."""
            ck = _wall_chain_key_tuple(walls_tuple)
            if ck in seen_keys:
                return len(paths) >= stop_n
            seen_keys.add(ck)
            rp = _try_multi_bounce_rx_chain(
                tx,
                rx,
                tx_e,
                tx_n,
                rx_e,
                rx_n,
                walls_tuple,
                rf_params=rf_params,
                is_path_clear_fn=is_path_clear_fn,
                rsrp_for_path_fn=rsrp_for_path_fn,
                termination_rsrp_dbm=termination_rsrp_dbm,
                footprint_buildings=footprint_buildings,
                rx_disk_radius_m=rx_disk_radius_m,
                reflection_residual_max_deg=reflection_residual_max_deg,
            )
            if rp is not None:
                paths.append(rp)
            return len(paths) >= stop_n

        if pcount <= perm_cap and pcount > 0:
            for combo in permutations(pool, k):
                if _emit(tuple(combo)):
                    break
        else:
            for chain in _discover_wall_chains_ray_march(
                tx_e,
                tx_n,
                pool,
                bounce_count=k,
                num_azimuths=n_azi,
            ):
                if _emit(chain):
                    break
            rnd_tries = 0
            while rnd_tries < rnd_cap:
                if len(paths) >= stop_n:
                    break
                rnd_tries += 1
                try:
                    combo = tuple(random.sample(pool, k))
                except ValueError:
                    break
                if _emit(combo):
                    break

    paths.sort(key=lambda p: p.rsrp_dbm, reverse=True)
    return paths[: max(0, int(max_return))]


def find_minimum_order_specular_paths(
    tx: LatLon,
    rx: LatLon,
    wall_segments: Sequence[WallSegment],
    *,
    max_k: int,
    max_candidates: int,
    max_return: int,
    rf_params: RFParams,
    is_path_clear_fn,
    footprint_buildings: Optional[Sequence[dict]] = None,
    path_rsrp_dbm: float = -80.0,
    termination_rsrp_dbm: float = -200.0,
    rx_disk_radius_m: float = 2.0,
    reflection_residual_max_deg: float = 2.0,
    multi_bounce_ray_azimuths: int = 1536,
    multi_bounce_max_random_chains: int = 12_000,
    multi_bounce_max_permutation_enum: int = 120_000,
) -> Tuple[Optional[int], List[RayPath]]:
    """Smallest ``k ∈ {0,…,max_k}`` with at least one Ω-valid path (2D infinite-prism model).

    * ``k=0``: line-of-sight; open ``Tx–Rx`` segment lies in free space (no interior samples).
    * ``k≥1``: specular chains with ``k`` façade contacts, same validity as ``compute_*_bounce``.

    Returns ``(k_min, paths)``. If no order admits a path, ``(None, [])``.

    ``path_rsrp_dbm`` is a uniform placeholder when only **existence / order** matters; re-score
    with a link budget elsewhere for RF output.

    For ``k≥1``, ``rx_disk_radius_m`` (default 2 m) treats the receiver as a disk in the 2D slice so
    steered paths need not pass through Rx to machine precision; use ``0`` for a near-point check
    via ``compute_*_bounce_paths`` directly.
    """

    def _tag(p: RayPath, order: int) -> RayPath:
        m = dict(p.meta)
        m["specular_order"] = order
        m["k_min_search"] = True
        return replace(p, meta=m)

    mk = int(max_k)
    if mk < 0:
        return None, []

    rx_e, rx_n = enu_from_latlon(tx, rx)
    d_los = _norm(rx_e, rx_n)
    score = float(path_rsrp_dbm)
    term = float(termination_rsrp_dbm)

    if mk >= 0:
        ok_los = True
        if footprint_buildings is not None:
            from .raytrace_2d_validity import validate_specular_polyline_2d_infinite_height

            ok_los, _ = validate_specular_polyline_2d_infinite_height([tx, rx], footprint_buildings, tx)
        else:
            ok_los = is_path_clear_fn(tx, rx, None)
        if ok_los:
            p0 = RayPath(
                kind="direct",
                points=[tx, rx],
                rsrp_dbm=score,
                meta={"specular_order": 0, "k_min_search": True},
                total_distance_m=d_los,
                extra_loss_db=0.0,
            )
            return 0, [p0][: max(0, int(max_return))]

    if mk < 1:
        return None, []

    def _rf_single(_tx_b: LatLon, _bounce_b: LatLon, _rx_b: LatLon, _td: float, _el: float) -> float:
        return score

    p1 = compute_single_bounce_paths(
        tx,
        rx,
        wall_segments,
        max_candidates=max_candidates,
        max_return=max_return,
        rf_params=rf_params,
        is_path_clear_fn=is_path_clear_fn,
        rsrp_for_path_fn=_rf_single,
        footprint_buildings=footprint_buildings,
        rx_disk_radius_m=rx_disk_radius_m,
        reflection_residual_max_deg=reflection_residual_max_deg,
    )
    if p1:
        return 1, [_tag(p, 1) for p in p1]

    if mk < 2:
        return None, []

    def _rf_two(_tx_b: LatLon, _p1: LatLon, _rx_b: LatLon, _td: float, _el: float) -> float:
        return score

    p2 = compute_two_bounce_paths(
        tx,
        rx,
        wall_segments,
        max_candidates=max_candidates,
        max_return=max_return,
        rf_params=rf_params,
        is_path_clear_fn=is_path_clear_fn,
        rsrp_for_path_fn=_rf_two,
        footprint_buildings=footprint_buildings,
        rx_disk_radius_m=rx_disk_radius_m,
        reflection_residual_max_deg=reflection_residual_max_deg,
    )
    if p2:
        return 2, [_tag(p, 2) for p in p2]

    for k in range(3, mk + 1):
        pk = compute_multi_bounce_paths(
            tx,
            rx,
            wall_segments,
            min_bounces=k,
            max_bounces=k,
            max_candidates=max_candidates,
            max_return=max_return,
            rf_params=rf_params,
            is_path_clear_fn=is_path_clear_fn,
            rsrp_for_path_fn=lambda _pts, _td, _el: score,
            termination_rsrp_dbm=term,
            footprint_buildings=footprint_buildings,
            rx_disk_radius_m=rx_disk_radius_m,
            reflection_residual_max_deg=reflection_residual_max_deg,
            max_permutation_enumeration=multi_bounce_max_permutation_enum,
            ray_discovery_azimuths=multi_bounce_ray_azimuths,
            max_random_chains_when_sampling=multi_bounce_max_random_chains,
        )
        if pk:
            return k, [_tag(p, k) for p in pk]

    return None, []
