"""Deterministic, geometry-only static background from mapped building facades.

The sensing-background product in this module deliberately stops at quantities
that are supported by mapped geometry.  It does **not** assign absolute clutter
power or use guessed facade RCS/roughness, land-cover sigma0, traffic state,
vegetation motion, receiver leakage statistics, waveform ambiguity sidelobes,
or probability-of-detection curves.

For one fixed TX/RX pair the product contains feasible static specular paths:

    TX -> mapped OSM facade -> RX

Each accepted path carries reflector identity/material label (when OSM provides
it), bounce location, horizontal path lengths, excess path/delay, and the fact
that a static reflector has zero physical Doppler.  These paths can therefore be
used for delay/Doppler *geometry and occupancy* analysis without pretending that
OSM supplies calibrated scattering amplitudes.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..pipeline.schemas import LatLon, RFParams
from .channel_analysis import SPEED_OF_LIGHT_M_S
from .ray_tracing import (
    WallSegment,
    _bounce_on_wall_relative_interior,
    _reflect_point_across_line,
    _segment_intersection_point,
    _wall_segment_link_relevance_m,
    enu_from_latlon,
    latlon_from_enu,
)


@dataclass(frozen=True, slots=True)
class StaticBackgroundPath:
    building_id: int | None
    material: str
    bounce_latitude_deg: float
    bounce_longitude_deg: float
    tx_to_bounce_range_m: float
    bounce_to_rx_range_m: float
    total_range_m: float
    excess_range_m: float
    excess_delay_s: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "building_id": self.building_id,
            "material": self.material,
            "bounce_latitude_deg": self.bounce_latitude_deg,
            "bounce_longitude_deg": self.bounce_longitude_deg,
            "tx_to_bounce_range_m": self.tx_to_bounce_range_m,
            "bounce_to_rx_range_m": self.bounce_to_rx_range_m,
            "total_range_m": self.total_range_m,
            "excess_range_m": self.excess_range_m,
            "excess_delay_s": self.excess_delay_s,
            "doppler_hz": 0.0,
            "power_available": False,
        }


@dataclass(frozen=True, slots=True)
class StaticBackgroundChannel:
    paths: tuple[StaticBackgroundPath, ...]
    candidate_buildings: int
    candidate_walls: int
    geometric_specular_candidates: int
    retained_visibility_candidates: int
    evaluated_wall_candidates: int
    visibility_search_complete: bool
    direct_range_m: float
    rejection_counts: dict[str, int] = field(default_factory=dict)
    model: str = "osm_2d_single_bounce_specular_geometry"

    def summary(self) -> dict[str, Any]:
        visibility_complete = bool(self.visibility_search_complete)
        if self.paths:
            status = "paths_available"
        elif visibility_complete:
            status = "complete_no_visible_specular_paths"
        else:
            status = "incomplete_no_visible_specular_paths"
        return {
            "enabled": True,
            "status": status,
            "model": self.model,
            "scope": "static_mapped_building_facades_geometry_only",
            "power_basis": "none_geometry_only",
            "absolute_scatter_power_available": False,
            "calibrated_absolute_clutter_power": False,
            "doppler_model": "static_objects_zero_physical_doppler",
            "delay_basis": "2d_horizontal_path_geometry",
            "delay_doppler_processing": "ideal_resolution_cell_occupancy_only_no_ambiguity_sidelobe_model",
            "candidate_buildings": int(self.candidate_buildings),
            "candidate_walls": int(self.candidate_walls),
            "geometry_search_complete": True,
            "geometric_specular_candidates": int(self.geometric_specular_candidates),
            "candidate_selection": "all_facades_geometry_test_then_bounded_shortest_specular_paths",
            "retained_visibility_candidates": int(self.retained_visibility_candidates),
            "evaluated_wall_candidates": int(self.evaluated_wall_candidates),
            "candidate_search_complete": visibility_complete,
            "accepted_paths": len(self.paths),
            "direct_tx_rx_horizontal_range_m": float(self.direct_range_m),
            "rejection_counts": {str(k): int(v) for k, v in self.rejection_counts.items()},
            "paths": [p.as_dict() for p in self.paths],
            "unsupported_without_additional_evidence": [
                "exact_facade_electromagnetic_properties",
                "surface_roughness_or_diffuse_scatter_calibration",
                "landcover_bistatic_sigma0_calibration",
                "real_traffic_state",
                "wind_driven_vegetation_doppler_spectrum",
                "measured_receiver_leakage_or_cancellation_statistics",
                "waveform_ambiguity_sidelobe_response",
                "calibrated_probability_of_detection_curves",
            ],
        }


def _unwrap_osm_provider(map_provider: Any) -> Any | None:
    """Return the OSM-compatible provider carrying the active prefetched AOI."""
    if map_provider is None:
        return None
    current = map_provider
    inner = getattr(current, "osm", None)
    if inner is not None:
        current = inner
    if hasattr(current, "get_buildings_along_ray") and hasattr(current, "_cached_buildings"):
        return current
    return None


def _surface_distance_m(a: LatLon, b: LatLon) -> float:
    lat1 = math.radians(float(a.lat))
    lat2 = math.radians(float(b.lat))
    dlat = lat2 - lat1
    dlon = math.radians(float(b.lon) - float(a.lon))
    h = math.sin(dlat / 2.0) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0) ** 2
    return 2.0 * 6_371_000.0 * math.asin(min(1.0, math.sqrt(max(0.0, h))))


def _iter_wall_segments(buildings: Sequence[dict], origin: LatLon):
    """Yield OSM wall segments in stable source order without materializing them."""
    wall_order = 0
    for building in buildings:
        geom = building.get("geometry") or []
        if not isinstance(geom, list) or len(geom) < 2:
            continue
        material = str(building.get("material", "unknown") or "unknown")
        raw_bid = building.get("id")
        try:
            building_id = int(raw_bid) if raw_bid is not None else None
        except Exception:
            building_id = None

        pts: list[tuple[float, float]] = []
        for node in geom:
            if not isinstance(node, dict) or "lat" not in node or "lon" not in node:
                continue
            pts.append(
                enu_from_latlon(
                    origin,
                    LatLon(lat=float(node["lat"]), lon=float(node["lon"])),
                )
            )
        if len(pts) < 2:
            continue
        if math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) > 1.0:
            pts.append(pts[0])

        for i in range(len(pts) - 1):
            ae, an = pts[i]
            be, bn = pts[i + 1]
            if math.hypot(be - ae, bn - an) < 0.25:
                continue
            yield wall_order, WallSegment(
                a_e=ae,
                a_n=an,
                b_e=be,
                b_n=bn,
                building_id=building_id,
                material=material,
                source="osm",
            )
            wall_order += 1


def _select_candidate_wall_segments(
    buildings: Sequence[dict],
    origin: LatLon,
    rx: LatLon,
    max_candidates: int,
) -> tuple[list[WallSegment], int]:
    """Legacy stable link-relevance selector retained for regression/debug use."""
    k = max(1, int(max_candidates))
    rx_e, rx_n = enu_from_latlon(origin, rx)
    heap: list[tuple[float, int, WallSegment]] = []
    wall_count = 0
    for wall_order, seg in _iter_wall_segments(buildings, origin):
        relevance = _wall_segment_link_relevance_m(seg, 0.0, 0.0, rx_e, rx_n)
        item = (-float(relevance), -wall_order, seg)
        if len(heap) < k:
            heapq.heappush(heap, item)
        else:
            worst_key = (-heap[0][0], -heap[0][1])
            new_key = (float(relevance), wall_order)
            if new_key < worst_key:
                heapq.heapreplace(heap, item)
        wall_count = wall_order + 1
    retained = [(-neg_d, -neg_order, seg) for neg_d, neg_order, seg in heap]
    retained.sort(key=lambda item: (item[0], item[1]))
    return [seg for _, _, seg in retained], wall_count


def _specular_geometry(
    seg: WallSegment,
    *,
    rx_e: float,
    rx_n: float,
) -> tuple[float, float, float, float, float] | None:
    """Return bounce ENU, d1, d2, total for a finite 2-D specular wall, else None."""
    a = (float(seg.a_e), float(seg.a_n))
    b = (float(seg.b_e), float(seg.b_n))
    rx_ref = _reflect_point_across_line((rx_e, rx_n), a, b)
    bounce = _segment_intersection_point((0.0, 0.0), rx_ref, a, b)
    if bounce is None:
        return None
    bx, by = float(bounce[0]), float(bounce[1])
    d1 = math.hypot(bx, by)
    d2 = math.hypot(rx_e - bx, rx_n - by)
    if d1 < 2.0 or d2 < 2.0:
        return None
    if not _bounce_on_wall_relative_interior(bx, by, a[0], a[1], b[0], b[1]):
        return None
    return bx, by, d1, d2, d1 + d2


def _select_specular_wall_candidates(
    buildings: Sequence[dict],
    origin: LatLon,
    rx: LatLon,
    max_candidates: int,
) -> tuple[list[tuple[WallSegment, float, float, float, float, float]], dict[str, int]]:
    """Scan every facade cheaply, then retain the shortest feasible specular paths.

    This reverses the old search order.  We never spend an expensive OSM visibility
    query on a wall until the image-method geometry says the finite facade can
    actually reflect TX to RX.  The all-facade geometry pass is O(M log K) time and
    O(K) retained memory for M walls and bounded K visibility candidates.
    """
    k = max(1, int(max_candidates))
    rx_e, rx_n = enu_from_latlon(origin, rx)
    direct = max(_surface_distance_m(origin, rx), 1.0)
    heap: list[tuple[float, int, WallSegment, float, float, float, float, float]] = []
    stats = {
        "facades_scanned": 0,
        "geometry_rejected": 0,
        "geometric_specular_candidates": 0,
        "visibility_tx_blocked": 0,
        "visibility_rx_blocked": 0,
    }
    for order, seg in _iter_wall_segments(buildings, origin):
        stats["facades_scanned"] = order + 1
        geom = _specular_geometry(seg, rx_e=rx_e, rx_n=rx_n)
        if geom is None:
            stats["geometry_rejected"] += 1
            continue
        stats["geometric_specular_candidates"] += 1
        bx, by, d1, d2, total = geom
        # Shortest excess path is the correct calibration-free priority: it keeps
        # the strongest geometric candidates near the direct-path delay without
        # inventing a reflection coefficient.
        excess = max(0.0, total - direct)
        item = (-excess, -order, seg, bx, by, d1, d2, total)
        if len(heap) < k:
            heapq.heappush(heap, item)
        else:
            worst_key = (-heap[0][0], -heap[0][1])
            new_key = (excess, order)
            if new_key < worst_key:
                heapq.heapreplace(heap, item)

    retained = [
        (-neg_excess, -neg_order, seg, bx, by, d1, d2, total)
        for neg_excess, neg_order, seg, bx, by, d1, d2, total in heap
    ]
    retained.sort(key=lambda row: (row[0], row[1]))
    return [(seg, bx, by, d1, d2, total) for _, _, seg, bx, by, d1, d2, total in retained], stats


def _visible_except_reflector(
    osm: Any,
    p0: LatLon,
    p1: LatLon,
    excluded_building_id: int | None,
) -> bool:
    try:
        buildings = osm.get_buildings_along_ray(p0, p1) or []
    except Exception:
        return False
    for building in buildings:
        bid = building.get("id") if isinstance(building, dict) else None
        try:
            bid_int = int(bid) if bid is not None else None
        except Exception:
            bid_int = None
        if excluded_building_id is not None and bid_int == int(excluded_building_id):
            continue
        return False
    return True


def build_static_background_channel(
    *,
    map_provider: Any,
    tx: LatLon,
    rf_params: RFParams,
    receiver: Any,
    max_wall_candidates: int | None = None,
    max_paths: int | None = None,
) -> StaticBackgroundChannel | None:
    """Build a reusable geometry-only static facade channel for one TX/RX pair."""
    osm = _unwrap_osm_provider(map_provider)
    if osm is None:
        return None
    buildings: Sequence[dict] = tuple(getattr(osm, "_cached_buildings", None) or ())
    if not buildings:
        return None

    rx = LatLon(lat=float(receiver.latitude), lon=float(receiver.longitude))

    # Algorithmic budgets only.  Geometry is evaluated for every facade; this
    # budget limits the expensive OSM visibility checks after that filter.
    configured = int(getattr(rf_params, "rt_max_wall_candidates", 40) or 40)
    candidate_budget = int(
        max_wall_candidates
        or max(1024, min(8192, configured * 64))
    )
    path_budget = int(
        max_paths
        or max(64, min(256, int(getattr(rf_params, "rt_max_reflections_per_sample", 2) or 2) * 64))
    )

    candidates, stats = _select_specular_wall_candidates(
        buildings,
        tx,
        rx,
        candidate_budget,
    )
    direct_range_m = _surface_distance_m(tx, rx)
    paths: list[StaticBackgroundPath] = []
    evaluated = 0

    for seg, bx, by, d1, d2, total in candidates:
        if len(paths) >= path_budget:
            break
        evaluated += 1
        bounce_ll = latlon_from_enu(tx, bx, by)
        if not _visible_except_reflector(osm, tx, bounce_ll, seg.building_id):
            stats["visibility_tx_blocked"] += 1
            continue
        if not _visible_except_reflector(osm, bounce_ll, rx, seg.building_id):
            stats["visibility_rx_blocked"] += 1
            continue
        excess = max(0.0, float(total) - direct_range_m)
        paths.append(
            StaticBackgroundPath(
                building_id=seg.building_id,
                material=str(seg.material or "unknown"),
                bounce_latitude_deg=float(bounce_ll.lat),
                bounce_longitude_deg=float(bounce_ll.lon),
                tx_to_bounce_range_m=float(d1),
                bounce_to_rx_range_m=float(d2),
                total_range_m=float(total),
                excess_range_m=float(excess),
                excess_delay_s=float(excess / SPEED_OF_LIGHT_M_S),
            )
        )

    # A bounded priority pass is an optimization, not evidence that the scene has
    # no reflectors. If it found nothing, stream the remaining geometrically
    # feasible facades and test visibility until we find a useful small path set
    # or exhaust the feasible scene. This prevents an empty background from being
    # caused solely by the candidate budget.
    visibility_complete = evaluated >= int(stats.get("geometric_specular_candidates", 0))
    if not paths and not visibility_complete:
        primary_keys = {
            (seg.building_id, seg.a_e, seg.a_n, seg.b_e, seg.b_n)
            for seg, *_ in candidates
        }
        rx_e, rx_n = enu_from_latlon(tx, rx)
        fallback_evaluated = 0
        fallback_target = max(1, min(path_budget, 16))
        exhausted = True
        for _order, seg in _iter_wall_segments(buildings, tx):
            key = (seg.building_id, seg.a_e, seg.a_n, seg.b_e, seg.b_n)
            if key in primary_keys:
                continue
            geom = _specular_geometry(seg, rx_e=rx_e, rx_n=rx_n)
            if geom is None:
                continue
            bx, by, d1, d2, total = geom
            fallback_evaluated += 1
            evaluated += 1
            bounce_ll = latlon_from_enu(tx, bx, by)
            if not _visible_except_reflector(osm, tx, bounce_ll, seg.building_id):
                stats["visibility_tx_blocked"] += 1
                continue
            if not _visible_except_reflector(osm, bounce_ll, rx, seg.building_id):
                stats["visibility_rx_blocked"] += 1
                continue
            excess = max(0.0, float(total) - direct_range_m)
            paths.append(StaticBackgroundPath(
                building_id=seg.building_id, material=str(seg.material or "unknown"),
                bounce_latitude_deg=float(bounce_ll.lat), bounce_longitude_deg=float(bounce_ll.lon),
                tx_to_bounce_range_m=float(d1), bounce_to_rx_range_m=float(d2), total_range_m=float(total),
                excess_range_m=float(excess), excess_delay_s=float(excess / SPEED_OF_LIGHT_M_S),
            ))
            if len(paths) >= fallback_target:
                exhausted = False
                break
        stats["fallback_visibility_scan_used"] = 1
        stats["fallback_visibility_candidates_evaluated"] = fallback_evaluated
        visibility_complete = exhausted
    else:
        stats["fallback_visibility_scan_used"] = 0
        stats["fallback_visibility_candidates_evaluated"] = 0

    # Geometry was scanned exhaustively even when visibility is intentionally
    # bounded.  These diagnostics make a zero-path result falsifiable.
    total_walls = int(stats.get("facades_scanned", 0))
    feasible = int(stats.get("geometric_specular_candidates", 0))
    retained = len(candidates)
    return StaticBackgroundChannel(
        paths=tuple(paths),
        candidate_buildings=len(buildings),
        candidate_walls=total_walls,
        geometric_specular_candidates=feasible,
        retained_visibility_candidates=retained,
        evaluated_wall_candidates=evaluated,
        visibility_search_complete=visibility_complete,
        direct_range_m=direct_range_m,
        rejection_counts=stats,
    )


def attach_static_background_summary(grid: Any, channel: StaticBackgroundChannel | None) -> None:
    if channel is None or getattr(grid, "channel_analysis_summary", None) is None:
        return
    grid.channel_analysis_summary["static_background_channel"] = channel.summary()
    fidelity = grid.channel_analysis_summary.setdefault("model_fidelity", {})
    fidelity["static_building_background_channel"] = True
    fidelity["static_background_absolute_power_calibrated"] = False
    fidelity["static_background_geometry_only"] = True
