"""2D infinite-height (plan-view) validity checks for specular ray polylines.

**Modeling choice:** footprints represent opaque **vertical prisms** (extruded to infinite
height in z). This is an abstraction for horizontal slice / façade-only specular; it is not a
literal claim about rooftops or 3D clearance. See ``ray_tracing`` module doc for ``k_min`` vs
model ``k_max`` cutoff.

Solver budgeting (design reference)
---------------------------------
**Exact image / enumeration:** There is no launch-ray budget. You enumerate ordered
wall (2D) or face (3D) chains up to depth ``K`` and validate. Cost grows roughly as
``M^K`` for ``M`` reflectors.

**Shooting-and-bouncing (SBR):** Choose (1) angular sampling and (2) max interactions.
In 2D, with sector span ``Omega`` rad, spacing ``Delta_phi``, and farthest range
``R_max``, a resolution target for feature width ``w_min`` is
``Delta_phi ~ w_min / R_max``, hence ``N_2D ~ Omega / Delta_phi``. In 3D, over solid
angle ``Omega_launch`` with spacing ``Delta``, ``N_3D ~ Omega_launch / Delta^2``.

This module implements the **physical-space** check that every **open** segment of a
folded candidate lies in Omega (free space): no interior sample may lie inside a
building footprint (2D infinite-height slice). **Specular contacts are boundary events:**
bounce points must lie on wall segments (enforced in ``ray_tracing`` via relative
interior of edges; polygon **vertices** are for diffraction models, not pure specular).

For **many-ray** forward shooting-and-bouncing with an **Rx capture disk** (same event
ordering as ``tests/raytrace_2d_infinite_height_demo.html``), see ``raytrace_2d_forward``.
"""

from __future__ import annotations

import math
from typing import AbstractSet, List, Optional, Sequence, Tuple

from ..geo.osm_map_provider import _polygon_contains_point
from ..pipeline.schemas import LatLon
from .ray_tracing import enu_from_latlon, latlon_from_enu


def estimate_num_azimuth_rays_2d_sbr(omega_rad: float, r_max_m: float, w_min_m: float) -> int:
    """Geometric estimate of azimuth ray count for fixed spacing (SBR-style).

    Uses ``Delta_phi = w_min / R_max`` and ``N ~ Omega / Delta_phi`` (at least 1).
    """
    if omega_rad <= 0.0 or r_max_m <= 0.0 or w_min_m <= 0.0:
        return 1
    dphi = w_min_m / r_max_m
    return max(1, int(math.ceil(omega_rad / dphi)))


def estimate_num_directions_3d_sbr(solid_angle_sr: float, r_max_m: float, w_min_m: float) -> int:
    """Geometric estimate of direction count over a solid angle (SBR-style).

    With ``Delta ~ w_min / R_max`` on a small patch, ``N ~ Omega_launch / Delta^2``.
    """
    if solid_angle_sr <= 0.0 or r_max_m <= 0.0 or w_min_m <= 0.0:
        return 1
    delta = w_min_m / r_max_m
    return max(1, int(math.ceil(solid_angle_sr / (delta * delta))))


def _sample_points_on_open_segment(
    origin: LatLon,
    p0: LatLon,
    p1: LatLon,
    *,
    eps_m: float,
    min_samples: int = 12,
    max_samples: int = 320,
    max_sample_spacing_m: float = 4.0,
) -> List[LatLon]:
    e0, n0 = enu_from_latlon(origin, p0)
    e1, n1 = enu_from_latlon(origin, p1)
    dx, dy = e1 - e0, n1 - n0
    length = math.hypot(dx, dy)
    if length < 2.0 * eps_m:
        return []
    t_lo = eps_m / length
    t_hi = 1.0 - eps_m / length
    if t_hi <= t_lo:
        return []
    # Adaptive count so long legs do not “jump over” narrow footprints (fixed N was too coarse).
    n = max(min_samples, min(max_samples, int(math.ceil(length / max(max_sample_spacing_m, 0.5))) + 3))
    out: List[LatLon] = []
    for i in range(1, n + 1):
        t = t_lo + (t_hi - t_lo) * (i / (n + 1))
        out.append(latlon_from_enu(origin, e0 + t * dx, n0 + t * dy))
    return out


def open_segment_has_interior_sample_in_footprint(
    origin: LatLon,
    p0: LatLon,
    p1: LatLon,
    geometry: List[dict],
    *,
    eps_m: float = 0.5,
    min_samples: int = 12,
    max_samples: int = 320,
    max_sample_spacing_m: float = 4.0,
) -> bool:
    """True if some strictly-interior sample of the segment lies inside the footprint."""
    if len(geometry) < 3:
        return False
    for ll in _sample_points_on_open_segment(
        origin,
        p0,
        p1,
        eps_m=eps_m,
        min_samples=min_samples,
        max_samples=max_samples,
        max_sample_spacing_m=max_sample_spacing_m,
    ):
        if _polygon_contains_point(geometry, ll):
            return True
    return False


def _closest_boundary_point_enu(
    origin: LatLon,
    e: float,
    n: float,
    geometry: List[dict],
) -> Optional[Tuple[float, float]]:
    """Closest point on closed polygon boundary to (e,n) in ENU meters."""
    pts: List[Tuple[float, float]] = []
    for node in geometry:
        if "lat" not in node or "lon" not in node:
            continue
        ee, nn = enu_from_latlon(origin, LatLon(lat=float(node["lat"]), lon=float(node["lon"])))
        pts.append((ee, nn))
    if len(pts) < 2:
        return None
    if abs(pts[0][0] - pts[-1][0]) > 1e-6 or abs(pts[0][1] - pts[-1][1]) > 1e-6:
        pts.append(pts[0])
    best_d = float("inf")
    best_xy = (0.0, 0.0)
    px, py = e, n
    for i in range(len(pts) - 1):
        ax, ay = pts[i]
        bx, by = pts[i + 1]
        vx, vy = bx - ax, by - ay
        vv = vx * vx + vy * vy
        if vv < 1e-18:
            continue
        t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / vv))
        cx, cy = ax + t * vx, ay + t * vy
        d = math.hypot(px - cx, py - cy)
        if d < best_d:
            best_d = d
            best_xy = (cx, cy)
    if best_d >=1e17:
        return None
    return best_xy


def snap_latlon_out_of_building_interiors(
    p: LatLon,
    buildings: Sequence[dict],
    origin: LatLon,
    *,
    margin_m: float = 2.5,
    max_passes: int = 12,
) -> LatLon:
    """If ``p`` lies inside any footprint, nudge it to just outside along the shortest axis to the boundary.

    OSM footprints are often mis-aligned with map clicks; this keeps Tx/Rx in Omega for 2D tracing.
    """
    cur = p
    for _ in range(max_passes):
        moved = False
        for b in buildings:
            geom = b.get("geometry") or []
            if len(geom) < 3:
                continue
            if not _polygon_contains_point(geom, cur):
                continue
            e, n = enu_from_latlon(origin, cur)
            cb = _closest_boundary_point_enu(origin, e, n, geom)
            if cb is None:
                continue
            ce, cn = cb
            dx, dy = ce - e, cn - n
            d = math.hypot(dx, dy)
            if d < 1e-4:
                continue
            cur = latlon_from_enu(
                origin,
                ce + (dx / d) * margin_m,
                cn + (dy / d) * margin_m,
            )
            moved = True
            break
        if not moved:
            break
    return cur


def validate_specular_polyline_2d_infinite_height(
    points: Sequence[LatLon],
    buildings: Sequence[dict],
    origin: LatLon,
    *,
    eps_m: float = 0.5,
    min_samples: int = 12,
    max_samples: int = 320,
    max_sample_spacing_m: float = 4.0,
    per_segment_skip_building_ids: Optional[Sequence[Optional[AbstractSet[int]]]] = None,
) -> Tuple[bool, str]:
    """Reject if any open segment has an interior sample inside any building footprint.

    ``per_segment_skip_building_ids`` (length ``len(points)-1``): for segment ``i``, do not
    count interior hits against these OSM building ids. Used for specular legs that **end**
    on a wall: point-in-polygon sampling can false-flag the approach to that façade as
    ``int(building)`` when Tx/Rx are in Omega.
    """
    if len(points) < 2:
        return False, "degenerate_polyline"
    geoms: List[Tuple[Optional[int], List[dict]]] = []
    for b in buildings:
        geom = b.get("geometry") or []
        if len(geom) < 3:
            continue
        bid = b.get("id")
        try:
            bid_i = int(bid) if bid is not None else None
        except Exception:
            bid_i = None
        geoms.append((bid_i, geom))

    for i in range(len(points) - 1):
        a, c = points[i], points[i + 1]
        skip_ids: Optional[AbstractSet[int]] = None
        if per_segment_skip_building_ids is not None and i < len(per_segment_skip_building_ids):
            skip_ids = per_segment_skip_building_ids[i]
        for bid, geom in geoms:
            if skip_ids and bid is not None and bid in skip_ids:
                continue
            if open_segment_has_interior_sample_in_footprint(
                origin,
                a,
                c,
                geom,
                eps_m=eps_m,
                min_samples=min_samples,
                max_samples=max_samples,
                max_sample_spacing_m=max_sample_spacing_m,
            ):
                return False, "segment_%d_interior_in_footprint_bid=%s" % (i, bid)
    return True, "ok"
