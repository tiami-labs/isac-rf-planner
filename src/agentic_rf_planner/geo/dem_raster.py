"""Local DEM raster window: load COG tiles once, sample elevations in memory."""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import numpy as np

from .persistent_cache import DEM_NAMESPACE, region_cache_key

logger = logging.getLogger(__name__)

# Copernicus DEM GLO-30 public COGs on AWS (no auth required).
_COP30_BUCKET = "copernicus-dem-30m"
_WINDOWS_DIR = DEM_NAMESPACE.root / "windows"

Point = Tuple[float, float]


def _copernicus_tile_path(lat: float, lon: float) -> str:
    """Return vsis3 path for the 1°×1° Copernicus COG containing (lat, lon)."""
    lat_n = int(math.floor(float(lat)))
    if lat_n < -90:
        lat_n = -90
    if lat_n > 89:
        lat_n = 89
    if float(lon) >= 0.0:
        lon_e = int(math.floor(float(lon)))
        if lon_e > 179:
            lon_e = 179
        hemi = f"E{lon_e:03d}"
    else:
        w = int(math.ceil(-float(lon) - 1e-9))
        if w < 1:
            w = 1
        if w > 180:
            w = 180
        hemi = f"W{w:03d}"
    lat_tag = f"N{lat_n:02d}" if lat_n >= 0 else f"S{abs(lat_n):02d}"
    folder = f"Copernicus_DSM_COG_10_{lat_tag}_00_{hemi}_00_DEM"
    fname = f"{folder}.tif"
    return f"/vsis3/{_COP30_BUCKET}/{folder}/{fname}"


def _bbox_for_radius(lat: float, lon: float, radius_m: float, pad_m: float = 200.0) -> Tuple[float, float, float, float]:
    r = max(1.0, float(radius_m) + float(pad_m))
    dlat = r / 111_000.0
    cos_lat = max(math.cos(math.radians(lat)), 1e-6)
    dlon = r / (111_000.0 * cos_lat)
    west = float(lon) - dlon
    east = float(lon) + dlon
    south = float(lat) - dlat
    north = float(lat) + dlat
    return (west, south, east, north)


def _tiles_for_bbox(west: float, south: float, east: float, north: float) -> List[str]:
    """List Copernicus COG paths intersecting WGS84 bbox (west,south,east,north)."""
    lat0 = int(math.floor(south))
    lat1 = int(math.floor(north))
    lon0 = int(math.floor(west))
    lon1 = int(math.floor(east))
    paths: List[str] = []
    for lat_i in range(lat0, lat1 + 1):
        for lon_i in range(lon0, lon1 + 1):
            # Sample center of each 1° cell for tile lookup.
            lon_c = lon_i + 0.5 if lon_i >= 0 else lon_i + 0.5
            paths.append(_copernicus_tile_path(float(lat_i) + 0.5, lon_c))
    # Deduplicate while preserving order.
    seen = set()
    out: List[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


class DemRasterWindow:
    """In-memory elevation grid sampled from local/remote COG tiles."""

    def __init__(self) -> None:
        self._data: Optional[np.ndarray] = None
        self._transform = None
        self._nodata: Optional[float] = None
        self._loaded = False

    @property
    def loaded(self) -> bool:
        return self._loaded and self._data is not None and self._transform is not None

    def load_for_disk(
        self,
        lat: float,
        lon: float,
        radius_m: float,
        *,
        pad_m: float = 200.0,
    ) -> bool:
        """Load DEM window covering circle(lat,lon,radius). Returns True on success."""
        try:
            import rasterio
            from rasterio.merge import merge
            from rasterio.windows import from_bounds
        except ImportError:
            logger.warning("rasterio not installed; local DEM raster unavailable")
            return False

        os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")
        os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")

        west, south, east, north = _bbox_for_radius(lat, lon, radius_m, pad_m=pad_m)
        cache_key = region_cache_key(lat, lon, radius_m, radius_grid_m=100.0)
        cache_path = _WINDOWS_DIR / f"{cache_key}_cop30.tif"

        if cache_path.exists() and cache_path.stat().st_size > 0:
            try:
                with rasterio.open(cache_path) as ds:
                    data = ds.read(1, masked=True)
                    self._data = np.asarray(data, dtype=np.float32)
                    self._transform = ds.transform
                    self._nodata = ds.nodata
                    self._loaded = True
                logger.info(
                    "DEM raster window cache HIT: %s (%dx%d)",
                    cache_path.name,
                    self._data.shape[1],
                    self._data.shape[0],
                )
                return True
            except Exception as exc:
                logger.warning("DEM window cache read failed (%s): %s", cache_path, exc)

        tile_paths = _tiles_for_bbox(west, south, east, north)
        datasets = []
        try:
            for path in tile_paths:
                try:
                    datasets.append(rasterio.open(path))
                except Exception as exc:
                    logger.warning("DEM COG open failed %s: %s", path, exc)
            if not datasets:
                return False

            merged_data, merged_transform = merge(datasets, nodata=-9999.0)
            merged = np.asarray(merged_data[0], dtype=np.float32)
            # Crop to bbox in merged pixel space.
            win = from_bounds(west, south, east, north, transform=merged_transform)
            row_off = max(0, int(math.floor(win.row_off)))
            col_off = max(0, int(math.floor(win.col_off)))
            row_end = min(merged.shape[0], int(math.ceil(win.row_off + win.height)))
            col_end = min(merged.shape[1], int(math.ceil(win.col_off + win.width)))
            if row_end <= row_off or col_end <= col_off:
                cropped = merged
                transform = merged_transform
            else:
                cropped = merged[row_off:row_end, col_off:col_end]
                transform = merged_transform * rasterio.Affine.translation(col_off, row_off)

            self._data = cropped
            self._transform = transform
            self._nodata = -9999.0
            self._loaded = True

            _WINDOWS_DIR.mkdir(parents=True, exist_ok=True)
            try:
                profile = datasets[0].profile.copy()
                profile.update(
                    driver="GTiff",
                    height=cropped.shape[0],
                    width=cropped.shape[1],
                    transform=transform,
                    dtype="float32",
                    count=1,
                    nodata=-9999.0,
                    compress="deflate",
                )
                with rasterio.open(cache_path, "w", **profile) as dst:
                    dst.write(cropped, 1)
                logger.info("DEM raster window cached: %s", cache_path)
            except Exception as exc:
                logger.warning("DEM window cache write failed: %s", exc)

            logger.info(
                "DEM raster loaded from %d COG tile(s): %dx%d px, radius=%.0fm",
                len(datasets),
                cropped.shape[1],
                cropped.shape[0],
                radius_m,
            )
            return True
        finally:
            for ds in datasets:
                try:
                    ds.close()
                except Exception:
                    pass

    def sample_many(self, points: Sequence[Point]) -> List[Optional[float]]:
        """Bilinear sample elevations (m AMSL) for WGS84 points."""
        if not self.loaded or not points:
            return [None] * len(points)
        try:
            import rasterio.transform
        except ImportError:
            return [None] * len(points)

        out: List[Optional[float]] = []
        data = self._data
        transform = self._transform
        nodata = self._nodata
        h, w = data.shape

        for lat, lon in points:
            try:
                col_f, row_f = rasterio.transform.rowcol(transform, lon, lat)
            except Exception:
                out.append(None)
                continue
            if not math.isfinite(col_f) or not math.isfinite(row_f):
                out.append(None)
                continue
            c0 = int(math.floor(col_f))
            r0 = int(math.floor(row_f))
            if c0 < 0 or r0 < 0 or c0 >= w - 1 or r0 >= h - 1:
                out.append(None)
                continue
            dc = col_f - c0
            dr = row_f - r0
            z00 = float(data[r0, c0])
            z01 = float(data[r0, c0 + 1])
            z10 = float(data[r0 + 1, c0])
            z11 = float(data[r0 + 1, c0 + 1])
            vals = (z00, z01, z10, z11)
            if nodata is not None and any(abs(v - nodata) < 1e-3 for v in vals):
                out.append(None)
                continue
            if any(not math.isfinite(v) for v in vals):
                out.append(None)
                continue
            z0 = z00 * (1.0 - dc) + z01 * dc
            z1 = z10 * (1.0 - dc) + z11 * dc
            out.append(float(z0 * (1.0 - dr) + z1 * dr))
        return out

    def sample(self, lat: float, lon: float) -> Optional[float]:
        vals = self.sample_many([(lat, lon)])
        return vals[0] if vals else None
