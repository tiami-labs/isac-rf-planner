"""Forward shooting-and-bouncing (SBR) for **2D infinite-height** building prisms.

Ported from the geometric rules in ``tests/raytrace_2d_infinite_height_demo.html`` (not the UI):

* Opaque footprint polygons extruded to infinite height; **only** specular on **edges** with a
  fixed **outward** facet normal per edge (CCW footprint in ENU).
* Each step: nearest positive-distance event among **Rx capture disk** vs **façade** hits;
  Rx wins iff it occurs **before** the next wall (same ordering as the canvas demo vs circle).
* **Corner / tie**: multiple facets at the same hit distance (within tolerance) → trace stops as
  ``corner_hit`` (no diffraction in this model).
* **Relative interior**: bounce ``u`` along the edge must stay away from endpoints (same spirit
  as ``_bounce_on_wall_relative_interior`` in ``ray_tracing``).
* Optional **max path length** replaces the demo’s canvas boundary (unbounded urban ENU plane).

Use this for **many-ray launch** reachability (beam spread, min bounces among hits) and for
drawings that color **hit-Rx** vs **miss** polylines. Image-chain steering stays in``ray_tracing``; this module is the **forward** ground truth for “does this ray reach the Rx disk?”.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..pipeline.schemas import LatLon

from .ray_tracing import WallSegment, enu_from_latlon


# --- Scene & plan (data structures for the “new problem”) -------------------------------------


@dataclass(frozen=True)
class ForwardFacet2D:
    """One directed façade segment with **outward** unit normal (free space on the +N side)."""

    a_e: float
    a_n: float
    b_e: float
    b_n: float
    outward_n_e: float
    outward_n_n: float
    building_id: Optional[int] = None

    def tangent_un(self) -> Tuple[float, float]:
        dx, dy = self.b_e - self.a_e, self.b_n - self.a_n
        L = math.hypot(dx, dy)
        if L < 1e-12:
            return 0.0, 0.0
        return dx / L, dy / L


@dataclass
class ForwardScene2D:
    """All reflecting facets plus receiver capture disk (2D slice, meters ENU)."""

    origin_ll: LatLon
    facets: List[ForwardFacet2D]
    rx_e: float
    rx_n: float
    rx_capture_radius_m: float = 12.0

    @property
    def rx_center_enu(self) -> Tuple[float, float]:
        return self.rx_e, self.rx_n


@dataclass(frozen=True)
class ForwardPropagationPlan:
    """How a batch of rays from Tx is scheduled (bearings, limits)."""

    tx_e: float
    tx_n: float
    bearing_center_rad: float
    spread_rad: float
    num_rays: int
    max_bounces: int
    max_path_m: float
    ray_origin_epsilon_m: float = 0.05
    corner_tie_tol_m: float = 1e-4


@dataclass
class ForwardTraceResult:
    """Outcome of one forward trace (one launch direction)."""

    angle_rad: float
    points_enu: List[Tuple[float, float]]
    bounce_count: int
    hit_rx: bool
    status: str  # hit_rx | max_bounces | corner_hit | max_path | degenerate
    path_length_m: float = 0.0
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ForwardBeamSummary:
    """Aggregate stats after ``launch_forward_beam`` (demo-style stats box)."""

    num_launched: int
    num_hit_rx: int
    min_bounces_among_hits: Optional[int]
    max_bounces_among_hits: Optional[int]
    num_corner_hit: int
    num_max_bounces: int
    num_max_path: int


# --- Geometry ---------------------------------------------------------------------------------


def _dot(ax: float, ay: float, bx: float, by: float) -> float:
    return ax * bx + ay * by


def _sub(ax: float, ay: float, bx: float, by: float) -> Tuple[float, float]:
    return ax - bx, ay - by


def _norm(ax: float, ay: float) -> float:
    return math.hypot(ax, ay)


def _reflect_dir_across_normal(dx: float, dy: float, nx: float, ny: float) -> Tuple[float, float]:
    dn = _dot(dx, dy, nx, ny)
    rx, ry = dx - 2.0 * dn * nx, dy - 2.0 * dn * ny
    L = _norm(rx, ry)
    if L < 1e-15:
        return dx, dy
    return rx / L, ry / L


def _ray_segment_intersect(
    ox: float,
    oy: float,
    dx: float,
    dy: float,
    ax: float,
    ay: float,
    bx: float,
    by: float,
    *,
    min_t: float,
) -> Optional[Tuple[float, float, float, float]]:
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


def _ray_circle_intersect(
    ox: float,
    oy: float,
    dx: float,
    dy: float,
    cx: float,
    cy: float,
    radius: float,
    *,
    min_t: float,
) -> Optional[Tuple[float, float, float]]:
    """Smallest positive ``t`` with ``|O + t D - C| = R``; ``D`` unit."""
    if radius <= 0.0:
        return None
    oc_e, oc_n = ox - cx, oy - cy
    b = 2.0 * _dot(oc_e, oc_n, dx, dy)
    c = _dot(oc_e, oc_n, oc_e, oc_n) - radius * radius
    disc = b * b - 4.0 * c
    if disc < 0.0:
        return None
    s = math.sqrt(disc)
    t1 = (-b - s) / 2.0
    t2 = (-b + s) / 2.0
    t: Optional[float] = None
    if t1 > min_t:
        t = t1
    elif t2 > min_t:
        t = t2
    if t is None:
        return None
    px, py = ox + t * dx, oy + t * dy
    return t, px, py


def _bounce_u_allowed(
    u: float,
    seg_len_m: float,
    *,
    corner_margin_m: float = 0.85,
    min_frac_from_vertex: float = 0.06,
) -> bool:
    if u < 0.0 or u > 1.0:
        return False
    margin = max(corner_margin_m, min_frac_from_vertex * seg_len_m)
    if u * seg_len_m < margin or (1.0 - u) * seg_len_m < margin:
        return False
    return True


def facets_from_osm_buildings(
    buildings: Sequence[dict],
    origin_ll: LatLon,
) -> List[ForwardFacet2D]:
    """Build outward-normal facets from OSM-style closed polygons (lat/lon vertices)."""
    out: List[ForwardFacet2D] = []
    for b in buildings:
        geom = b.get("geometry") or []
        if not isinstance(geom, list) or len(geom) < 3:
            continue
        try:
            bid = int(b["id"]) if b.get("id") is not None else None
        except Exception:
            bid = None
        pts: List[Tuple[float, float]] = []
        for node in geom:
            if not isinstance(node, dict) or "lat" not in node or "lon" not in node:
                continue
            e, n = enu_from_latlon(
                origin_ll, LatLon(lat=float(node["lat"]), lon=float(node["lon"]))
            )
            pts.append((e, n))
        if len(pts) < 3:
            continue
        # Closed ring: drop duplicate closing vertex if present
        if (
            abs(pts[0][0] - pts[-1][0]) < 1e-9
            and abs(pts[0][1] - pts[-1][1]) < 1e-9
        ):
            pts = pts[:-1]
        nvert = len(pts)
        if nvert < 3:
            continue
        for i in range(nvert):
            ax, ay = pts[i]
            bx, by = pts[(i + 1) % nvert]
            dx, dy = bx - ax, by - ay
            L = math.hypot(dx, dy)
            if L < 1e-6:
                continue
            # CCW polygon: interior left of A→B; outward = right of tangent = (dy/L, -dx/L)
            ne, nn = dy / L, -dx / L
            out.append(
                ForwardFacet2D(
                    a_e=ax,
                    a_n=ay,
                    b_e=bx,
                    b_n=by,
                    outward_n_e=ne,
                    outward_n_n=nn,
                    building_id=bid,
                )
            )
    return out


def facets_from_wall_segments(walls: Sequence[WallSegment]) -> List[ForwardFacet2D]:
    """Derive outward normals from ``WallSegment`` edges using CCW assumption per building ring.

    When only ``WallSegment`` list is available (already oriented as in OSM extract), use the same
    right-hand rule on each directed edge as stored (a→b).
    """
    out: List[ForwardFacet2D] = []
    for w in walls:
        ax, ay, bx, by = w.a_e, w.a_n, w.b_e, w.b_n
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy)
        if L < 1e-9:
            continue
        ne, nn = dy / L, -dx / L
        out.append(
            ForwardFacet2D(
                a_e=ax,
                a_n=ay,
                b_e=bx,
                b_n=by,
                outward_n_e=ne,
                outward_n_n=nn,
                building_id=w.building_id,
            )
        )
    return out


# --- Core trace -------------------------------------------------------------------------------


def trace_forward_specular_ray(
    scene: ForwardScene2D,
    *,
    angle_rad: float,
    max_bounces: int,
    max_path_m: float = 50_000.0,
    ray_origin_epsilon_m: float = 0.05,
    corner_tie_tol_m: float = 1e-4,
    min_ray_step_m: float = 1e-4,
) -> ForwardTraceResult:
    """Single ray from **Tx = scene origin** in ENU ``(0,0)`` at ``angle_rad`` (CCW from +East)."""
    _ = min_ray_step_m  # reserved; fixed inside ``_trace_from_offset``
    return _trace_from_offset(
        scene,
        0.0,
        0.0,
        angle_rad=angle_rad,
        max_bounces=max_bounces,
        max_path_m=max_path_m,
        ray_origin_epsilon_m=ray_origin_epsilon_m,
        corner_tie_tol_m=corner_tie_tol_m,
    )


def launch_forward_beam(
    scene: ForwardScene2D,
    plan: ForwardPropagationPlan,
) -> Tuple[ForwardBeamSummary, List[ForwardTraceResult]]:
    """Launch ``plan.num_rays`` directions from ``(plan.tx_e, plan.tx_n)`` with spread (demo-style)."""
    results: List[ForwardTraceResult] = []
    n = max(1, int(plan.num_rays))
    base = float(plan.bearing_center_rad)
    spread = float(plan.spread_rad)

    for i in range(n):
        if n == 1:
            ang = base
        else:
            t = i / (n - 1)
            ang = base - spread / 2.0 + t * spread
        # Scene origin is origin_ll at ENU (0,0); plan may offset Tx — override start in trace
        r = _trace_from_offset(
            scene,
            plan.tx_e,
            plan.tx_n,
            angle_rad=ang,
            max_bounces=plan.max_bounces,
            max_path_m=plan.max_path_m,
            ray_origin_epsilon_m=plan.ray_origin_epsilon_m,
            corner_tie_tol_m=plan.corner_tie_tol_m,
        )
        results.append(r)

    hits = [r for r in results if r.hit_rx]
    min_b = min((r.bounce_count for r in hits), default=None)
    max_b = max((r.bounce_count for r in hits), default=None)
    summary = ForwardBeamSummary(
        num_launched=len(results),
        num_hit_rx=len(hits),
        min_bounces_among_hits=min_b,
        max_bounces_among_hits=max_b,
        num_corner_hit=sum(1 for r in results if r.status == "corner_hit"),
        num_max_bounces=sum(1 for r in results if r.status == "max_bounces"),
        num_max_path=sum(1 for r in results if r.status == "max_path"),
    )
    return summary, results


def _trace_from_offset(
    scene: ForwardScene2D,
    tx_e: float,
    tx_n: float,
    *,
    angle_rad: float,
    max_bounces: int,
    max_path_m: float,
    ray_origin_epsilon_m: float,
    corner_tie_tol_m: float,
) -> ForwardTraceResult:
    """Like ``trace_forward_specular_ray`` but Tx at arbitrary ENU (e.g. snapped)."""
    ox, oy = tx_e, tx_n
    dx, dy = math.cos(angle_rad), math.sin(angle_rad)
    dlen = _norm(dx, dy)
    if dlen < 1e-12:
        return ForwardTraceResult(
            angle_rad=angle_rad,
            points_enu=[(ox, oy)],
            bounce_count=0,
            hit_rx=False,
            status="degenerate",
        )
    dx, dy = dx / dlen, dy / dlen
    min_ray_step_m = 1e-4

    pts: List[Tuple[float, float]] = [(ox, oy)]
    bounces = 0
    path_accum = 0.0
    status = "max_path"

    while path_accum <= max_path_m + 1e-6:
        rx_t: Optional[float] = None
        rx_pt: Optional[Tuple[float, float]] = None
        if scene.rx_capture_radius_m > 0.0:
            hc = _ray_circle_intersect(
                ox,
                oy,
                dx,
                dy,
                scene.rx_e,
                scene.rx_n,
                scene.rx_capture_radius_m,
                min_t=min_ray_step_m,
            )
            if hc is not None:
                rx_t, rx_px, rx_py = hc[0], hc[1], hc[2]
                rx_pt = (rx_px, rx_py)

        best_t = float("inf")
        best_hits: List[Tuple[ForwardFacet2D, float, float, float, float]] = []

        for w in scene.facets:
            hit = _ray_segment_intersect(
                ox, oy, dx, dy, w.a_e, w.a_n, w.b_e, w.b_n, min_t=min_ray_step_m
            )
            if hit is None:
                continue
            t, u, px, py = hit
            seg_len = _norm(w.b_e - w.a_e, w.b_n - w.a_n)
            if not _bounce_u_allowed(u, seg_len):
                continue
            if t < best_t - corner_tie_tol_m:
                best_t = t
                best_hits = [(w, t, u, px, py)]
            elif abs(t - best_t) <= corner_tie_tol_m:
                best_hits.append((w, t, u, px, py))

        wall_event = best_hits and math.isfinite(best_t)
        nearest_t = float("inf")
        event = "none"
        if wall_event:
            nearest_t = best_t
            event = "wall"
        if rx_t is not None and rx_t < nearest_t:
            nearest_t = rx_t
            event = "rx"

        if not math.isfinite(nearest_t) or event == "none":
            break

        if event == "rx" and rx_pt is not None:
            pts.append(rx_pt)
            path_accum += nearest_t
            plen = sum(
                _norm(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
                for i in range(len(pts) - 1)
            )
            return ForwardTraceResult(
                angle_rad=angle_rad,
                points_enu=pts,
                bounce_count=bounces,
                hit_rx=True,
                status="hit_rx",
                path_length_m=plen,
            )

        if event == "wall":
            if len(best_hits) > 1:
                w0, t0, u0, px0, py0 = best_hits[0]
                pts.append((px0, py0))
                path_accum += t0
                return ForwardTraceResult(
                    angle_rad=angle_rad,
                    points_enu=pts,
                    bounce_count=bounces,
                    hit_rx=False,
                    status="corner_hit",
                    path_length_m=path_accum,
                )
            w0, t0, u0, px0, py0 = best_hits[0]
            pts.append((px0, py0))
            path_accum += t0
            if bounces >= max_bounces:
                return ForwardTraceResult(
                    angle_rad=angle_rad,
                    points_enu=pts,
                    bounce_count=bounces,
                    hit_rx=False,
                    status="max_bounces",
                    path_length_m=path_accum,
                )
            ne, nn = w0.outward_n_e, w0.outward_n_n
            dx, dy = _reflect_dir_across_normal(dx, dy, ne, nn)
            ox, oy = px0 + dx * ray_origin_epsilon_m, py0 + dy * ray_origin_epsilon_m
            bounces += 1
            continue

    plen = sum(
        _norm(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
        for i in range(len(pts) - 1)
    )
    return ForwardTraceResult(
        angle_rad=angle_rad,
        points_enu=pts,
        bounce_count=bounces,
        hit_rx=False,
        status=status,
        path_length_m=plen,
    )
