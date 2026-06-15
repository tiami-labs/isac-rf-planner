"""Shared persistent cache layout and helpers for geo data stores."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Tuple

DEFAULT_CACHE_ROOT = Path.home() / ".rf_planning_cache"


@dataclass(frozen=True)
class CacheNamespace:
    """Logical partition under ``~/.rf_planning_cache/<name>/``."""

    name: str
    expiry_days: int = 30

    @property
    def root(self) -> Path:
        return DEFAULT_CACHE_ROOT / self.name

    def ensure_root(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root


OSM_NAMESPACE = CacheNamespace("osm_data", 30)
DEM_NAMESPACE = CacheNamespace("dem", 90)


def region_cache_key(
    lat: float,
    lon: float,
    radius_m: float,
    *,
    lat_grid_deg: float = 0.001,
    radius_grid_m: float = 50.0,
) -> str:
    """Hash key for circular region caches (OSM-style grid bucketing)."""
    lat_grid = round(lat / lat_grid_deg) * lat_grid_deg
    lon_grid = round(lon / lat_grid_deg) * lat_grid_deg
    radius_grid = round(radius_m / radius_grid_m) * radius_grid_m
    key_str = f"{lat_grid:.6f}_{lon_grid:.6f}_{radius_grid:.0f}"
    return hashlib.md5(key_str.encode()).hexdigest()


def is_path_fresh(path: Path, expiry_days: int) -> bool:
    """Return True when *path* exists and is younger than *expiry_days*."""
    try:
        if not path.exists():
            return False
        age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
        return age <= timedelta(days=expiry_days)
    except OSError:
        return False


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Approximate great-circle distance in meters."""
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * r * math.atan2(math.sqrt(h), math.sqrt(max(0.0, 1.0 - h)))


def coord_e5(lat: float, lon: float) -> Tuple[int, int]:
    """Quantize lat/lon to 1e-5 degree (~1.1 m) integer keys."""
    return (round(float(lat) * 1e5), round(float(lon) * 1e5))


def coord_from_e5(lat_e5: int, lon_e5: int) -> Tuple[float, float]:
    return (lat_e5 / 1e5, lon_e5 / 1e5)
