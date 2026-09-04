"""Utilities for mesh-profile keying and fast geometry.

Keep these helpers independent of any specific sampling backend.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Tuple

from ...pipeline.schemas import LatLon
from .profile_types import PROFILE_VERSION


# Quantization constants (tuned for cache hits while keeping keys stable)
_LATLON_GRID_DEG = 1e-5  # ~1.1m
_HEIGHT_GRID_M = 0.5     # 0.5m
_RANGE_GRID_M = 10.0     # 10m
_DTHETA_GRID_DEG = 0.1   # 0.1deg
_DR_GRID_M = 1.0         # 1m


@dataclass(frozen=True)
class RayProfileKey:
    """Deterministic key components for a RayProfileSet."""

    lat_q: int
    lon_q: int
    txh_q: int
    rxh_q: int
    maxr_q: int
    dr_q: int
    dtheta_q: int
    version: str = PROFILE_VERSION

    def to_string(self) -> str:
        return f"{self.lat_q}_{self.lon_q}_{self.txh_q}_{self.rxh_q}_{self.maxr_q}_{self.dr_q}_{self.dtheta_q}_{self.version}"

    def to_hash(self) -> str:
        return hashlib.md5(self.to_string().encode("utf-8")).hexdigest()


def compute_profile_key(
    tx: LatLon,
    tx_height_m: float,
    rx_height_m: float,
    max_range_m: float,
    dr_m: float,
    dtheta_deg: float,
    version: str = PROFILE_VERSION,
) -> Tuple[RayProfileKey, str]:
    lat_q = int(round(tx.lat / _LATLON_GRID_DEG))
    lon_q = int(round(tx.lon / _LATLON_GRID_DEG))
    txh_q = int(round(tx_height_m / _HEIGHT_GRID_M))
    rxh_q = int(round(rx_height_m / _HEIGHT_GRID_M))
    maxr_q = int(round(max_range_m / _RANGE_GRID_M))
    dr_q = int(round(dr_m / _DR_GRID_M))
    dtheta_q = int(round(dtheta_deg / _DTHETA_GRID_DEG))

    key = RayProfileKey(
        lat_q=lat_q,
        lon_q=lon_q,
        txh_q=txh_q,
        rxh_q=rxh_q,
        maxr_q=maxr_q,
        dr_q=dr_q,
        dtheta_q=dtheta_q,
        version=version,
    )
    return key, key.to_hash()


def bearing_deg(start: LatLon, end: LatLon) -> float:
    """Initial bearing from start to end (degrees clockwise from north)."""
    lat1 = math.radians(start.lat)
    lat2 = math.radians(end.lat)
    dlon = math.radians(end.lon - start.lon)

    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    brng = math.degrees(math.atan2(y, x))
    return (brng + 360.0) % 360.0


def haversine_m(a: LatLon, b: LatLon) -> float:
    """Great-circle distance between two points in meters."""
    R = 6371000.0
    lat1 = math.radians(a.lat)
    lat2 = math.radians(b.lat)
    dlat = lat2 - lat1
    dlon = math.radians(b.lon - a.lon)

    s = math.sin(dlat / 2.0) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(s), math.sqrt(1.0 - s))
    return R * c


def bearing_bin(bearing: float, dtheta_deg: float) -> int:
    """Quantize a bearing into an integer bin index."""
    if dtheta_deg <= 0:
        raise ValueError("dtheta_deg must be > 0")
    n = int(round(360.0 / dtheta_deg))
    if n <= 0:
        raise ValueError("invalid dtheta_deg")
    idx = int(round(bearing / dtheta_deg)) % n
    return idx
