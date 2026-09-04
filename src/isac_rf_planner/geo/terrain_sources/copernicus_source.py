"""CopernicusTerrainSource — local COG raster with OpenTopoData API fallback.

This is the Copernicus GLO-30 DEM implementation extracted from the original
TerrainProvider.  It owns its own in-memory cache and SQLite L2 cache so it
is independently usable and independently testable.
"""

from __future__ import annotations

import logging
import math
from typing import Dict, List, Optional, Sequence, Tuple

import requests

from ...pipeline.schemas import LatLon
from ..dem_cache import DATASET_LOCAL_RASTER, DATASET_OPENTOPO, ElevationCacheStore
from ..dem_raster import DemRasterWindow
from ..terrain_source import project_bearing_range

logger = logging.getLogger(__name__)

_OPENTOPO_URL = "https://api.opentopodata.org/v1/aster30m"
_BATCH_SIZE = 50

Point = Tuple[float, float]


def _cache_key(lat: float, lon: float, precision: int = 5) -> Point:
    return (round(float(lat), precision), round(float(lon), precision))


class CopernicusTerrainSource:
    """Copernicus GLO-30 COG raster with OpenTopoData HTTP fallback.

    Resolution: ~30 m.  Always available (falls back to API, then 0.0).
    """

    name = "copernicus"

    def __init__(
        self,
        elevation_store: Optional[ElevationCacheStore] = None,
        persist_elevations: bool = True,
        max_range_m: Optional[float] = None,
    ) -> None:
        self._mem: Dict[Point, float] = {}
        self._store = elevation_store if elevation_store is not None else ElevationCacheStore(
            enabled=persist_elevations
        )
        self._raster = DemRasterWindow()
        self._max_range_m = float(max_range_m) if max_range_m is not None else None
        self._window_lat: Optional[float] = None
        self._window_lon: Optional[float] = None
        self._window_radius_m: Optional[float] = None
        self._provider_used: str = "none"

    def available(self) -> bool:
        return True

    @property
    def provider_detail(self) -> str:
        """Which sub-provider last served a query (raster / opentopodata / none)."""
        return self._provider_used

    # ------------------------------------------------------------------
    # Public TerrainHeightSource interface
    # ------------------------------------------------------------------

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
        if self._raster_preferred():
            self.ensure_raster_window(tx.lat, tx.lon, max_r)
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
        """Warm in-memory cache for all bearing/range samples used by coverage_grid."""
        step = max(1.0, float(step_m))
        max_r = max(step, float(max_range_m))
        dtheta = max(0.25, float(dtheta_deg))
        if self._raster_preferred():
            self.ensure_raster_window(tx.lat, tx.lon, max_r)
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
        logger.info("Copernicus prefetch: %d sample points", len(points))
        self._batch_fetch(points)

    # ------------------------------------------------------------------
    # Raster window management (kept public for TerrainProvider delegation)
    # ------------------------------------------------------------------

    def ensure_raster_window(self, lat: float, lon: float, radius_m: float) -> bool:
        """Load Copernicus COG window for this TX/radius if not already loaded."""
        radius_m = max(50.0, float(radius_m))
        if (
            self._raster.loaded
            and self._window_lat is not None
            and self._window_lon is not None
            and self._window_radius_m is not None
            and abs(self._window_lat - lat) < 1e-6
            and abs(self._window_lon - lon) < 1e-6
            and radius_m <= self._window_radius_m + 1.0
        ):
            return True
        ok = self._raster.load_for_disk(lat, lon, radius_m)
        if ok:
            self._window_lat = float(lat)
            self._window_lon = float(lon)
            self._window_radius_m = radius_m
            self._provider_used = "local_raster"
        return ok

    # ------------------------------------------------------------------
    # Internal fetch logic
    # ------------------------------------------------------------------

    def _raster_preferred(self) -> bool:
        return True

    def _batch_fetch(self, latlon: Sequence[Point]) -> None:
        pending = [pt for pt in latlon if _cache_key(*pt) not in self._mem]
        if not pending:
            return

        # L2: SQLite disk cache
        self._lookup_disk(pending)
        pending = [pt for pt in pending if _cache_key(*pt) not in self._mem]
        if not pending:
            return

        # L1: local COG raster
        if self._raster.loaded:
            self._sample_from_raster(pending)
            pending = [pt for pt in pending if _cache_key(*pt) not in self._mem]
        if not pending:
            return

        # Remote: OpenTopoData API
        for i in range(0, len(pending), _BATCH_SIZE):
            chunk = pending[i: i + _BATCH_SIZE]
            elevations = self._fetch_opentopodata(chunk)
            writes: Dict[Point, float] = {}
            for pt, z in zip(chunk, elevations):
                if z is None:
                    continue
                self._mem[_cache_key(*pt)] = float(z)
                writes[pt] = float(z)
            if writes and self._store.enabled:
                self._store.put_many(DATASET_OPENTOPO, writes)

    def _lookup_disk(self, pending: Sequence[Point]) -> None:
        if not self._store.enabled or not pending:
            return
        for dataset in (DATASET_LOCAL_RASTER, DATASET_OPENTOPO):
            remaining = [pt for pt in pending if _cache_key(*pt) not in self._mem]
            if not remaining:
                break
            hits = self._store.get_many(dataset, remaining)
            for (lat, lon), elev in hits.items():
                self._mem[_cache_key(lat, lon)] = float(elev)

    def _sample_from_raster(self, pending: Sequence[Point]) -> None:
        samples = self._raster.sample_many(list(pending))
        writes: Dict[Point, float] = {}
        for pt, z in zip(pending, samples):
            if z is None:
                continue
            self._mem[_cache_key(*pt)] = float(z)
            writes[pt] = float(z)
        if writes and self._store.enabled:
            self._store.put_many(DATASET_LOCAL_RASTER, writes)
        if writes:
            self._provider_used = "local_raster"

    def _fetch_opentopodata(self, chunk: Sequence[Point]) -> List[Optional[float]]:
        try:
            locs = "|".join(f"{lat:.6f},{lon:.6f}" for lat, lon in chunk)
            resp = requests.get(_OPENTOPO_URL, params={"locations": locs}, timeout=45)
            resp.raise_for_status()
            data = resp.json()
            if str(data.get("status", "")).lower() != "ok":
                raise RuntimeError(data.get("error") or data)
            out: List[Optional[float]] = []
            for item in data.get("results") or []:
                elev = item.get("elevation")
                out.append(0.0 if elev is None else float(elev))
            if len(out) != len(chunk):
                raise RuntimeError(f"OpenTopoData returned {len(out)} for {len(chunk)} points")
            self._provider_used = "opentopodata"
            return out
        except Exception as exc:
            logger.warning("OpenTopoData fetch failed: %s", exc)
            return [None] * len(chunk)
