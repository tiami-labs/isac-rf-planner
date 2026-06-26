"""TerrainHeightSource — the only contract the rest of the system depends on.

All terrain backends implement this Protocol.  TerrainProvider composes them
into an ordered fallback chain.  No caller outside geo/terrain_sources/ should
import a concrete implementation; import this module instead.
"""

from __future__ import annotations

import math
from typing import List, Protocol, Tuple, runtime_checkable

from ..pipeline.schemas import LatLon

EARTH_R_M = 6_371_000.0


def project_bearing_range(tx: LatLon, bearing_deg: float, distance_m: float) -> Tuple[float, float]:
    """Return (lat, lon) projected from tx along bearing by distance_m."""
    r = EARTH_R_M
    br = math.radians(bearing_deg)
    lat1 = math.radians(tx.lat)
    lon1 = math.radians(tx.lon)
    lat2 = math.asin(
        math.sin(lat1) * math.cos(distance_m / r)
        + math.cos(lat1) * math.sin(distance_m / r) * math.cos(br)
    )
    lon2 = lon1 + math.atan2(
        math.sin(br) * math.sin(distance_m / r) * math.cos(lat1),
        math.cos(distance_m / r) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


@runtime_checkable
class TerrainHeightSource(Protocol):
    """Contract for any terrain elevation backend.

    Implementations supply ground height (m AMSL) at arbitrary points and along
    bearing profiles.  TerrainProvider composes them into a fallback chain;
    callers never reference a concrete class.
    """

    @property
    def name(self) -> str:
        """Short identifier used in logs and diagnostics."""
        ...

    def available(self) -> bool:
        """Return True if this source can currently serve queries."""
        ...

    def elevation_m(self, lat: float, lon: float) -> float:
        """Ground elevation (m AMSL) for a single WGS-84 point."""
        ...

    def profile_along_bearing(
        self,
        tx: LatLon,
        bearing_deg: float,
        max_range_m: float,
        step_m: float,
    ) -> Tuple[List[float], List[float]]:
        """Terrain height profile from TX outward along bearing.

        Returns ``(u_m, z_m)`` arrays of equal length.  ``u_m[i]`` is the
        range from TX in metres; ``z_m[i]`` is ground height in metres AMSL.
        First sample is at range 0 (TX location); samples continue at step_m
        intervals up to and including max_range_m.
        """
        ...

    def prefetch(
        self,
        tx: LatLon,
        max_range_m: float,
        step_m: float,
        dtheta_deg: float,
    ) -> None:
        """Optional warm-up for all bearing/range samples in coverage grid.

        Backends that require bulk loading (raster windows, API batches) should
        implement this.  The default no-op is sufficient for in-memory sources.
        """
        ...
