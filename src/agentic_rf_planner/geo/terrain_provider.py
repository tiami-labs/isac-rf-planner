"""DEM elevation provider for terrain-aware propagation."""

from __future__ import annotations

import logging
import math
import os
from typing import Dict, List, Optional, Sequence, Tuple

import requests

from ..pipeline.schemas import LatLon, RFParams

logger = logging.getLogger(__name__)

_OPENTOPO_URL = "https://api.opentopodata.org/v1/aster30m"
_GOOGLE_ELEV_URL = "https://maps.googleapis.com/maps/api/elevation/json"
_BATCH_SIZE = 50


def _project_from_tx(lat: float, lon: float, distance_m: float, bearing_deg: float) -> Tuple[float, float]:
    r = 6371000.0
    br = math.radians(bearing_deg)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    lat2 = math.asin(
        math.sin(lat1) * math.cos(distance_m / r)
        + math.cos(lat1) * math.sin(distance_m / r) * math.cos(br)
    )
    lon2 = lon1 + math.atan2(
        math.sin(br) * math.sin(distance_m / r) * math.cos(lat1),
        math.cos(distance_m / r) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def _cache_key(lat: float, lon: float, precision: int = 5) -> Tuple[float, float]:
    return (round(float(lat), precision), round(float(lon), precision))


class TerrainProvider:
    """Fetch and cache ground elevation (meters AMSL) for planner samples."""

    def __init__(
        self,
        dem_source: str = "auto",
        resolution_m: Optional[float] = None,
    ) -> None:
        self.dem_source = str(dem_source or "auto").strip().lower()
        self.resolution_m = float(resolution_m or 30.0)
        self._cache: Dict[Tuple[float, float], float] = {}
        self.provider_used: str = "none"
        self._google_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()

    def elevation_m(self, lat: float, lon: float) -> float:
        key = _cache_key(lat, lon)
        if key in self._cache:
            return self._cache[key]
        self._batch_fetch([(float(lat), float(lon))])
        return self._cache.get(key, 0.0)

    def profile_along_bearing(
        self,
        tx: LatLon,
        bearing_deg: float,
        max_range_m: float,
        step_m: float,
    ) -> Tuple[List[float], List[float]]:
        """Return (u_m[], z_dem_m[]) from TX outward including endpoints."""
        step = max(1.0, float(step_m))
        max_r = max(step, float(max_range_m))
        u_list: List[float] = []
        z_list: List[float] = []
        points: List[Tuple[float, float]] = []
        r = 0.0
        while r <= max_r + 1e-6:
            if r <= 1e-6:
                lat, lon = tx.lat, tx.lon
            else:
                lat, lon = _project_from_tx(tx.lat, tx.lon, r, bearing_deg)
            points.append((lat, lon))
            u_list.append(r)
            r += step
        self._batch_fetch(points)
        for lat, lon in points:
            z_list.append(self.elevation_m(lat, lon))
        return u_list, z_list

    def prefetch_for_polar_grid(
        self,
        tx: LatLon,
        max_range_m: float,
        step_m: float,
        dtheta_deg: float,
    ) -> None:
        """Warm cache for all bearing/range samples used by coverage_grid."""
        step = max(1.0, float(step_m))
        max_r = max(step, float(max_range_m))
        dtheta = max(0.25, float(dtheta_deg))
        points: List[Tuple[float, float]] = [(tx.lat, tx.lon)]
        theta = 0.0
        while theta < 360.0 - 1e-6:
            r = 0.0
            while r <= max_r + 1e-6:
                if r <= 1e-6:
                    points.append((tx.lat, tx.lon))
                else:
                    points.append(_project_from_tx(tx.lat, tx.lon, r, theta))
                r += step
            theta += dtheta
        logger.info("Terrain prefetch: %d sample points (dem=%s)", len(points), self.dem_source)
        self._batch_fetch(points)

    def _batch_fetch(self, latlon: Sequence[Tuple[float, float]]) -> None:
        pending: List[Tuple[float, float]] = []
        for lat, lon in latlon:
            key = _cache_key(lat, lon)
            if key not in self._cache:
                pending.append((float(lat), float(lon)))
        if not pending:
            return
        for i in range(0, len(pending), _BATCH_SIZE):
            chunk = pending[i : i + _BATCH_SIZE]
            elevations = self._fetch_chunk(chunk)
            for (lat, lon), z in zip(chunk, elevations):
                if z is None:
                    continue
                self._cache[_cache_key(lat, lon)] = float(z)

    def _fetch_chunk(self, chunk: Sequence[Tuple[float, float]]) -> List[Optional[float]]:
        source = self.dem_source
        if source == "auto":
            if self._google_key:
                source = "google"
            else:
                source = "opentopodata"
        if source in ("google", "google_elevation") and self._google_key:
            try:
                out = self._fetch_google(chunk)
                self.provider_used = "google"
                return out
            except Exception as exc:
                logger.warning("Google elevation failed: %s; falling back to OpenTopoData", exc)
        if source in ("flat", "disabled", "none"):
            self.provider_used = "flat"
            return [0.0] * len(chunk)
        try:
            out = self._fetch_opentopodata(chunk)
            self.provider_used = "opentopodata"
            return out
        except Exception as exc:
            logger.warning("OpenTopoData elevation failed: %s; leaving points uncached", exc)
            return [None] * len(chunk)

    def _fetch_opentopodata(self, chunk: Sequence[Tuple[float, float]]) -> List[float]:
        locs = "|".join(f"{lat:.6f},{lon:.6f}" for lat, lon in chunk)
        resp = requests.get(_OPENTOPO_URL, params={"locations": locs}, timeout=45)
        resp.raise_for_status()
        data = resp.json()
        if str(data.get("status", "")).lower() != "ok":
            raise RuntimeError(data.get("error") or data)
        out: List[float] = []
        for item in data.get("results") or []:
            elev = item.get("elevation")
            out.append(0.0 if elev is None else float(elev))
        if len(out) != len(chunk):
            raise RuntimeError(f"OpenTopoData returned {len(out)} elevations for {len(chunk)} points")
        return out

    def _fetch_google(self, chunk: Sequence[Tuple[float, float]]) -> List[float]:
        locs = "|".join(f"{lat:.6f},{lon:.6f}" for lat, lon in chunk)
        resp = requests.get(
            _GOOGLE_ELEV_URL,
            params={"locations": locs, "key": self._google_key},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        if str(data.get("status", "")).upper() != "OK":
            raise RuntimeError(data.get("error_message") or data)
        out: List[float] = []
        for item in data.get("results") or []:
            out.append(float(item.get("elevation", 0.0)))
        if len(out) != len(chunk):
            raise RuntimeError(f"Google elevation returned {len(out)} for {len(chunk)} points")
        return out


def create_terrain_provider(rf_params: RFParams) -> Optional[TerrainProvider]:
    if not bool(getattr(rf_params, "terrain_enabled", True)):
        return None
    source = str(getattr(rf_params, "dem_source", "auto") or "auto")
    res = getattr(rf_params, "terrain_resolution_m", None)
    return TerrainProvider(dem_source=source, resolution_m=res)
