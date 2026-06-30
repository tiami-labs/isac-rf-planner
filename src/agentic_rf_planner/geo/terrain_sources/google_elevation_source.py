"""GoogleElevationTerrainSource — Google Maps Elevation API backend.

Available only when a GOOGLE_MAPS_API_KEY environment variable is set.
Uses the same SQLite L2 cache as CopernicusTerrainSource (separate dataset key).
"""

from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional, Sequence, Tuple

import requests

from ...pipeline.schemas import LatLon
from ..dem_cache import DATASET_GOOGLE, ElevationCacheStore
from ..terrain_source import project_bearing_range

logger = logging.getLogger(__name__)

_GOOGLE_ELEV_URL = "https://maps.googleapis.com/maps/api/elevation/json"
_BATCH_SIZE = 50

Point = Tuple[float, float]


def _cache_key(lat: float, lon: float, precision: int = 5) -> Point:
    return (round(float(lat), precision), round(float(lon), precision))


class GoogleElevationTerrainSource:
    """Google Maps Elevation API.

    Resolution: ~10 m in populated areas.  Only available when API key is present.
    """

    name = "google_elevation"

    def __init__(
        self,
        api_key: Optional[str] = None,
        elevation_store: Optional[ElevationCacheStore] = None,
        persist_elevations: bool = True,
    ) -> None:
        self._key = (
            api_key
            or os.environ.get("GOOGLE_MAPS_API_KEY")
            or os.environ.get("GOOGLE_MAPS_APIKEY")
            or ""
        ).strip()
        self._mem: Dict[Point, float] = {}
        self._store = elevation_store if elevation_store is not None else ElevationCacheStore(
            enabled=persist_elevations
        )

    def available(self) -> bool:
        return bool(self._key)

    def elevation_m(self, lat: float, lon: float) -> float:
        key = _cache_key(lat, lon)
        if key in self._mem:
            return self._mem[key]
        self._batch_fetch([(float(lat), float(lon))])
        return self._mem.get(key, 0.0)

    def profile_along_bearing(
        self,
        tx: LatLon,
        bearing_deg: float,
        max_range_m: float,
        step_m: float,
    ) -> Tuple[List[float], List[float]]:
        step = max(1.0, float(step_m))
        max_r = max(step, float(max_range_m))
        u_list: List[float] = []
        points: List[Point] = []
        r = 0.0
        while r <= max_r + 1e-6:
            if r <= 1e-6:
                lat, lon = tx.lat, tx.lon
            else:
                lat, lon = project_bearing_range(tx, bearing_deg, r)
            points.append((lat, lon))
            u_list.append(r)
            r += step
        self._batch_fetch(points)
        z_list = [self.elevation_m(lat, lon) for lat, lon in points]
        return u_list, z_list

    def prefetch(
        self,
        tx: LatLon,
        max_range_m: float,
        step_m: float,
        dtheta_deg: float,
    ) -> None:
        step = max(1.0, float(step_m))
        max_r = max(step, float(max_range_m))
        dtheta = max(0.25, float(dtheta_deg))
        points: List[Point] = [(tx.lat, tx.lon)]
        theta = 0.0
        while theta < 360.0 - 1e-6:
            r = 0.0
            while r <= max_r + 1e-6:
                if r <= 1e-6:
                    points.append((tx.lat, tx.lon))
                else:
                    points.append(project_bearing_range(tx, theta, r))
                r += step
            theta += dtheta
        logger.info("Google Elevation prefetch: %d sample points", len(points))
        self._batch_fetch(points)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _batch_fetch(self, latlon: Sequence[Point]) -> None:
        pending = [pt for pt in latlon if _cache_key(*pt) not in self._mem]
        if not pending:
            return

        # L2: SQLite disk cache
        if self._store.enabled:
            hits = self._store.get_many(DATASET_GOOGLE, pending)
            for (lat, lon), elev in hits.items():
                self._mem[_cache_key(lat, lon)] = float(elev)
            pending = [pt for pt in pending if _cache_key(*pt) not in self._mem]
        if not pending:
            return

        # Remote: Google Elevation API
        for i in range(0, len(pending), _BATCH_SIZE):
            chunk = pending[i: i + _BATCH_SIZE]
            elevations = self._fetch_google(chunk)
            writes: Dict[Point, float] = {}
            for pt, z in zip(chunk, elevations):
                if z is None:
                    continue
                self._mem[_cache_key(*pt)] = float(z)
                writes[pt] = float(z)
            if writes and self._store.enabled:
                self._store.put_many(DATASET_GOOGLE, writes)

    def _fetch_google(self, chunk: Sequence[Point]) -> List[Optional[float]]:
        try:
            locs = "|".join(f"{lat:.6f},{lon:.6f}" for lat, lon in chunk)
            resp = requests.get(
                _GOOGLE_ELEV_URL,
                params={"locations": locs, "key": self._key},
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            if str(data.get("status", "")).upper() != "OK":
                raise RuntimeError(data.get("error_message") or data)
            out: List[Optional[float]] = []
            for item in data.get("results") or []:
                out.append(float(item.get("elevation", 0.0)))
            if len(out) != len(chunk):
                raise RuntimeError(f"Google Elevation returned {len(out)} for {len(chunk)} points")
            return out
        except Exception as exc:
            logger.warning("Google Elevation fetch failed: %s", exc)
            return [None] * len(chunk)
