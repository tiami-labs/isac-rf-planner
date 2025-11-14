"""Map-based physical spanning estimation."""

from typing import Protocol

from ..pipeline.schemas import LatLon, MaterialType


class MapProvider(Protocol):
    """
    Abstracts whatever you use: OSM, Google, local tiles, etc.
    Implement later; interface is small.
    """

    def count_buildings_between(self, start: LatLon, end: LatLon) -> int:
        """Count building obstacles between two points."""
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


