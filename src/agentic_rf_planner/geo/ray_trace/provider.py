from __future__ import annotations

import math
from typing import Any, Dict, List

from ...pipeline.schemas import LatLon
from ..physical_spanning import MapProvider
from ..osm_map_provider import OSMMapProvider
from .engine import OSMRayTraceEngine
from .path_types import RayTraceResult


class RayTraceOSMMapProvider(MapProvider):
    """3D OSM + ray-trace backend.

    Uses the existing OSM provider for obstacle semantics and clutter queries,
    while exposing an additional single-bounce ray-tracing engine for preview and
    future RF path integrations.
    """

    def __init__(self, cache_radius_m: float = 1000.0, tx_height_m: float = 10.0, rx_height_m: float = 1.5):
        self.osm = OSMMapProvider(cache_radius_m=cache_radius_m, slice_height_m=min(tx_height_m, rx_height_m))
        self.tx_height_m = float(tx_height_m)
        self.rx_height_m = float(rx_height_m)
        self.engine = OSMRayTraceEngine(tx_height_m=tx_height_m, rx_height_m=rx_height_m)

    def prefetch_all_data(self, center: LatLon, radius_m: float) -> None:
        self.osm.prefetch_all_data(center, radius_m)

    def get_buildings_along_ray(self, start: LatLon, end: LatLon) -> List[Dict[str, Any]]:
        return self.osm.get_buildings_along_ray(start, end)

    def count_buildings_between(self, start: LatLon, end: LatLon) -> int:
        return self.osm.count_buildings_between(start, end)

    def is_forest_between(self, start: LatLon, end: LatLon) -> bool:
        return self.osm.is_forest_between(start, end)

    def get_clutter_type(self, center: LatLon, radius_m: float = 200.0) -> str:
        return self.osm.get_clutter_type(center, radius_m=radius_m)

    def trace_paths(self, tx: LatLon, rx: LatLon, max_reflections: int = 1) -> RayTraceResult:
        buildings = self.osm._get_buildings_near_line(tx, rx) if not self.osm._prefetched else self.osm._cached_buildings
        return self.engine.trace(tx, rx, buildings, max_reflections=max_reflections)

    def radial_trace_preview(self, tx: LatLon, max_range_m: float, num_bearings: int = 24, max_reflections: int = 1) -> List[RayTraceResult]:
        results: List[RayTraceResult] = []
        m_per_deg_lon = 111_320.0 * max(1e-6, math.cos(math.radians(tx.lat)))
        m_per_deg_lat = 111_320.0
        for i in range(max(1, int(num_bearings))):
            bearing = (360.0 * i) / max(1, int(num_bearings))
            rad = math.radians(bearing)
            north = math.cos(rad) * max_range_m
            east = math.sin(rad) * max_range_m
            rx = LatLon(lat=tx.lat + (north / m_per_deg_lat), lon=tx.lon + (east / m_per_deg_lon))
            results.append(self.trace_paths(tx, rx, max_reflections=max_reflections))
        return results
