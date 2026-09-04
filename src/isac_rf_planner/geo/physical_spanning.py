"""Map-based physical spanning estimation.

This module defines the MapProvider protocol used by the RF planning pipeline.

Critical design constraint:
  - RF math (FSPL / NLOS excess / penetration loss / SINR) lives in rf/.
  - The ONLY difference between 2D and 3D planning is ray propagation / geometry
    intersection queries inside the MapProvider implementation.
"""

from typing import Any, Dict, List, Protocol

from ..pipeline.schemas import LatLon, MaterialType


class MapProvider(Protocol):
    """
    Abstracts whatever you use: OSM, Google, local tiles, etc.
    Implement later; interface is small.
    """

    def count_buildings_between(self, start: LatLon, end: LatLon) -> int:
        """Count building obstacles between two points."""
        ...

    def prefetch_all_data(self, center: LatLon, radius_m: float) -> None:
        """Optional bulk prefetch.

        Implementations may use this to fetch/cache all data needed to answer
        per-ray queries quickly (OSM prefetched polygons, mesh ray profiles, etc.).
        """
        ...

    def get_buildings_along_ray(self, start: LatLon, end: LatLon) -> List[Dict[str, Any]]:
        """Return building/structure obstacles intersecting the ray from start to end.

        The RF pipeline uses this to accumulate material penetration losses along
        the ray (in order is preferred, but not strictly required).
        """
        ...

    def is_forest_between(self, start: LatLon, end: LatLon) -> bool:
        """Check if forest/vegetation exists between two points."""
        ...
    
    def get_clutter_type(self, center: LatLon, radius_m: float = 200.0) -> str:
        """
        Classify area clutter: 'open', 'suburban', 'urban', 'dense_urban'.
        Optional method for advanced classification.
        """
        ...


class StubMapProvider:
    """Stub implementation for MVP - returns zeros/no obstacles."""

    def count_buildings_between(self, start: LatLon, end: LatLon) -> int:
        """Stub: return 0 for now."""
        return 0

    def prefetch_all_data(self, center: LatLon, radius_m: float) -> None:
        """Stub: no-op."""
        return None

    def get_buildings_along_ray(self, start: LatLon, end: LatLon) -> List[Dict[str, Any]]:
        """Stub: return empty obstacle list."""
        return []

    def is_forest_between(self, start: LatLon, end: LatLon) -> bool:
        """Stub: return False for now."""
        return False
    
    def get_clutter_type(self, center: LatLon, radius_m: float = 200.0) -> str:
        """Stub: return 'open' for now."""
        return "open"


def estimate_obstacles_along_ray(
    tx: LatLon,
    target_lat: float,
    target_lon: float,
    material_hint: MaterialType,
    map_provider: MapProvider,
) -> int:
    """
    MVP: only care about number of building faces or "forest" along the ray.
    More detail is overkill up front.
    """
    target = LatLon(lat=target_lat, lon=target_lon)

    if material_hint in (MaterialType.BUILDING, MaterialType.HOUSE, MaterialType.LARGE_STRUCTURE):
        return map_provider.count_buildings_between(tx, target)

    if material_hint == MaterialType.TREES:
        return 1 if map_provider.is_forest_between(tx, target) else 0

    # unknown / fallback
    return 0


