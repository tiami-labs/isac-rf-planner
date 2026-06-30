"""GoogleMeshTerrainSource — high-resolution DSM from persisted Google mesh profiles.

Reads BearingProfile.terrain_heights populated by the Cesium mesh profiler
frontend.  Each entry is a (range_m, height_m_amsl) pair sampled from the
Google 3D tile surface along that bearing.

Resolution matches the frontend sample step (typically 1–5 m), which is a
substantial improvement over the 30 m Copernicus raster.  Buildings and
terrain are fused in the mesh surface; 3D OSM remains the separate semantic
layer for building-specific RF physics.

If no bearing profiles carry terrain_heights (profiles not yet generated, or
fetched before this field was added), available() returns False and the
TerrainProvider fallback chain advances to the next source transparently.
"""

from __future__ import annotations

import bisect
import logging
import math
from typing import Dict, List, Optional, Tuple

from ...pipeline.schemas import LatLon
from ..google_mesh.profile_types import BearingProfile, RayProfileSet
from ..terrain_source import project_bearing_range

logger = logging.getLogger(__name__)

EARTH_R_M = 6_371_000.0


def _bearing_to(tx: LatLon, lat: float, lon: float) -> float:
    """Great-circle initial bearing from tx to (lat, lon) in [0, 360)."""
    lat1 = math.radians(tx.lat)
    lat2 = math.radians(lat)
    dlon = math.radians(lon - tx.lon)
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return math.degrees(math.atan2(x, y)) % 360.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2.0 * EARTH_R_M * math.asin(math.sqrt(max(0.0, a)))


def _angular_delta(a: float, b: float) -> float:
    """Smallest angular difference between two bearings (degrees)."""
    return abs(((a - b + 180.0) % 360.0) - 180.0)


def _nearest_profile(
    profiles: Dict[float, BearingProfile], bearing_deg: float
) -> Optional[BearingProfile]:
    if not profiles:
        return None
    best = min(profiles.keys(), key=lambda b: _angular_delta(b, bearing_deg))
    return profiles[best]


def _interpolate_height(
    terrain_heights: List[Tuple[float, float]], range_m: float
) -> float:
    """Linear interpolation into sorted (range_m, height_m) pairs."""
    if not terrain_heights:
        return 0.0
    ranges = [r for r, _ in terrain_heights]
    if range_m <= ranges[0]:
        return float(terrain_heights[0][1])
    if range_m >= ranges[-1]:
        return float(terrain_heights[-1][1])
    i = bisect.bisect_right(ranges, range_m) - 1
    r0, z0 = terrain_heights[i]
    r1, z1 = terrain_heights[i + 1]
    if r1 <= r0 + 1e-9:
        return float(z0)
    t = (range_m - r0) / (r1 - r0)
    return float(z0 + t * (z1 - z0))


class GoogleMeshTerrainSource:
    """Terrain height source backed by persisted Google mesh DSM profiles.

    Construction is cheap — profiles are already in memory from the SQLite
    profile store.  No I/O occurs at query time.
    """

    name = "google_mesh"

    def __init__(self, profile_set: RayProfileSet) -> None:
        self._tx = LatLon(lat=profile_set.tx_lat, lon=profile_set.tx_lon)
        # Index only profiles that actually carry terrain height samples.
        self._profiles: Dict[float, BearingProfile] = {
            p.bearing_deg: p
            for p in profile_set.profiles
            if p.terrain_heights
        }
        if self._profiles:
            logger.info(
                "GoogleMeshTerrainSource: %d bearing profiles with terrain heights (TX %.5f, %.5f)",
                len(self._profiles),
                profile_set.tx_lat,
                profile_set.tx_lon,
            )
        else:
            logger.debug(
                "GoogleMeshTerrainSource: no terrain_heights in profiles — source unavailable"
            )

    def available(self) -> bool:
        return bool(self._profiles)

    def elevation_m(self, lat: float, lon: float) -> float:
        bearing = _bearing_to(self._tx, lat, lon)
        range_m = _haversine_m(self._tx.lat, self._tx.lon, lat, lon)
        prof = _nearest_profile(self._profiles, bearing)
        if prof is None or not prof.terrain_heights:
            return 0.0
        return _interpolate_height(prof.terrain_heights, range_m)

    def profile_along_bearing(
        self,
        tx: LatLon,
        bearing_deg: float,
        max_range_m: float,
        step_m: float,
    ) -> Tuple[List[float], List[float]]:
        prof = _nearest_profile(self._profiles, bearing_deg)
        step = max(1.0, float(step_m))
        max_r = max(step, float(max_range_m))
        u_list: List[float] = []
        z_list: List[float] = []
        r = 0.0
        while r <= max_r + 1e-6:
            u_list.append(r)
            if prof is not None and prof.terrain_heights:
                z_list.append(_interpolate_height(prof.terrain_heights, r))
            else:
                z_list.append(0.0)
            r += step
        return u_list, z_list

    def prefetch(
        self,
        tx: LatLon,
        max_range_m: float,
        step_m: float,
        dtheta_deg: float,
    ) -> None:
        pass  # Profiles are already in memory; no warm-up needed.
