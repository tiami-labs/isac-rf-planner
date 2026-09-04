"""Geographic and map-related modules."""

from .snapping import snap_to_street, SnappedPoint
from .streetview_provider import StreetViewProvider, create_streetview_provider
from .coverage_grid import build_coverage_grid
from .physical_spanning import MapProvider, StubMapProvider, estimate_obstacles_along_ray
from .heatmap import attenuation_grid_to_raster

__all__ = [
    "snap_to_street",
    "SnappedPoint",
    "StreetViewProvider",
    "create_streetview_provider",
    "build_coverage_grid",
    "MapProvider",
    "StubMapProvider",
    "estimate_obstacles_along_ray",
    "attenuation_grid_to_raster",
]

