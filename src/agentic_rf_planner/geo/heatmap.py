"""Heatmap generation from attenuation grid.

Performance notes:
- The attenuation grid can have tens of thousands of scattered points.
- For 3D OSM-only visualization, we prefer returning a pre-colored PNG texture (base64)
  rather than large JSON arrays.
"""

import base64
import io
import logging
import math
from typing import Tuple, Optional, Dict, Any, List

import numpy as np
from PIL import Image

from ..pipeline.schemas import AttenuationGrid

logger = logging.getLogger(__name__)


def _empty(values: Any) -> bool:
    return values is None or len(values) == 0


def attenuation_grid_to_raster(
    grid: AttenuationGrid,
    width: int = 256,
    height: int = 256,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Convert scattered RSRP points to a raster (lats_2d, lons_2d, rsrp_2d).

    This is used by non-3D-OSM renderers and for debugging. It is intentionally simple,
    but the point-to-pixel assignment is vectorized for speed.
    """
    if _empty(grid.cell_lat) or _empty(grid.cell_lon) or _empty(grid.rsrp_dbm):
        raise ValueError("Empty attenuation grid")

    lat_arr = np.asarray(grid.cell_lat, dtype=np.float64)
    lon_arr = np.asarray(grid.cell_lon, dtype=np.float64)
    val_arr = np.asarray(grid.rsrp_dbm, dtype=np.float32)

    # Bounding box
    min_lat = float(np.min(lat_arr))
    max_lat = float(np.max(lat_arr))
    min_lon = float(np.min(lon_arr))
    max_lon = float(np.max(lon_arr))

    # Uniform grid in lat/lon
    lat_vals = np.linspace(min_lat, max_lat, height, dtype=np.float64)
    lon_vals = np.linspace(min_lon, max_lon, width, dtype=np.float64)
    lats_2d, lons_2d = np.meshgrid(lat_vals, lon_vals, indexing="ij")

    # Vectorized assignment onto nearest bins (via linear index mapping).
    # Since lat_vals/lon_vals are linspace, we can compute indices by scaling.
    # NOTE: lat decreases in array index? Here lat_vals is increasing; we map directly.
    lat_span = max_lat - min_lat
    lon_span = max_lon - min_lon
    if lat_span <= 0 or lon_span <= 0:
        raise ValueError("Degenerate bounds for heatmap rasterization")

    lat_f = (lat_arr - min_lat) / lat_span
    lon_f = (lon_arr - min_lon) / lon_span
    i = np.clip(np.rint(lat_f * (height - 1)).astype(np.int32), 0, height - 1)
    j = np.clip(np.rint(lon_f * (width - 1)).astype(np.int32), 0, width - 1)

    sums = np.zeros((height, width), dtype=np.float64)
    cnts = np.zeros((height, width), dtype=np.int32)
    np.add.at(sums, (i, j), val_arr.astype(np.float64))
    np.add.at(cnts, (i, j), 1)

    rsrp_2d = np.full((height, width), np.nan, dtype=np.float32)
    m = cnts > 0
    rsrp_2d[m] = (sums[m] / cnts[m]).astype(np.float32)

    rsrp_filled = _fill_nans_nearest(rsrp_2d)

    # Circular mask based on RF max range (authoritative).
    _apply_circular_mask_in_latlon(grid, lats_2d, lons_2d, rsrp_filled)

    return lats_2d, lons_2d, rsrp_filled


class EllipseRasterizer:
    """Reusable map-aligned raster geometry for many RF/ISAC layers.

    Coordinate projection, point-to-pixel mapping, support masks, and edge
    feathering are invariant across layers.  Preparing them once avoids
    repeating the most expensive geometry work for every exported heatmap.
    """

    def __init__(self, grid: AttenuationGrid, size: int = 768) -> None:
        if len(grid.cell_lat) == 0 or len(grid.cell_lon) == 0:
            raise ValueError("Empty attenuation grid")
        if len(grid.cell_lat) != len(grid.cell_lon):
            raise ValueError("cell_lat and cell_lon lengths must match")

        self.grid = grid
        self.size = int(size)
        self.radius_m = float(getattr(grid.rf_params, "max_range_m", 0.0) or 0.0)
        if self.radius_m <= 0.0:
            raise ValueError("rf_params.max_range_m must be > 0 for ellipse PNG")

        tx_lat = float(grid.tx.lat)
        tx_lon = float(grid.tx.lon)
        lat_arr = np.asarray(grid.cell_lat, dtype=np.float64)
        lon_arr = np.asarray(grid.cell_lon, dtype=np.float64)
        earth_m = 6_371_000.0
        lat0 = np.deg2rad(tx_lat)
        self.dx = np.deg2rad(lon_arr - tx_lon) * np.cos(lat0) * earth_m
        self.dy = np.deg2rad(lat_arr - tx_lat) * earth_m

        u = (self.dx + self.radius_m) / (2.0 * self.radius_m)
        v = (self.dy + self.radius_m) / (2.0 * self.radius_m)
        jj = np.rint(u * (self.size - 1)).astype(np.int32)
        ii = np.rint((1.0 - v) * (self.size - 1)).astype(np.int32)
        in_bounds = (
            (jj >= 0)
            & (jj < self.size)
            & (ii >= 0)
            & (ii < self.size)
            & (self.dx * self.dx + self.dy * self.dy <= self.radius_m * self.radius_m)
        )
        self.point_indices = np.flatnonzero(in_bounds)
        self.pixel_indices = (
            ii[in_bounds].astype(np.int64) * self.size + jj[in_bounds].astype(np.int64)
        )

        yy, xx = np.mgrid[0 : self.size, 0 : self.size]
        x_m = (xx / (self.size - 1) - 0.5) * (2.0 * self.radius_m)
        y_m = ((self.size - 1 - yy) / (self.size - 1) - 0.5) * (2.0 * self.radius_m)
        self.r_pix = np.sqrt(x_m * x_m + y_m * y_m)
        self.circle_mask = self.r_pix <= self.radius_m

        try:
            bin_deg = float(getattr(grid.rf_params, "dtheta_deg", 1.0) or 1.0)
        except Exception:
            bin_deg = 1.0
        bin_deg = max(0.25, min(5.0, bin_deg))
        n_bins = int(max(72, round(360.0 / bin_deg)))
        theta_s = (np.degrees(np.arctan2(self.dx, self.dy)) + 360.0) % 360.0
        r_s = np.sqrt(self.dx * self.dx + self.dy * self.dy)
        bi_s = np.floor(theta_s / (360.0 / n_bins)).astype(np.int32)
        bi_s = np.clip(bi_s, 0, n_bins - 1)
        rmax = np.zeros(n_bins, dtype=np.float32)
        np.maximum.at(rmax, bi_s, r_s.astype(np.float32))

        theta_pix = (np.degrees(np.arctan2(x_m, y_m)) + 360.0) % 360.0
        bi_pix = np.floor(theta_pix / (360.0 / n_bins)).astype(np.int32)
        bi_pix = np.clip(bi_pix, 0, n_bins - 1)
        try:
            dr_m = float(getattr(grid.rf_params, "step_m", 5.0) or 5.0)
        except Exception:
            dr_m = 5.0
        self.support_mask = (
            self.circle_mask
            & (rmax[bi_pix] > 0.0)
            & (self.r_pix <= rmax[bi_pix] + dr_m)
        )
        feather_m = max(30.0, self.radius_m * 0.03)
        self.fade = np.clip((self.radius_m - self.r_pix) / feather_m, 0.0, 1.0)

    def render(
        self,
        values: Any,
        *,
        vmin: Optional[float] = None,
        vmax: Optional[float] = None,
        alpha: float = 0.70,
    ) -> Dict[str, Any]:
        val_arr = np.asarray(values, dtype=np.float32)
        if val_arr.ndim != 1 or val_arr.shape[0] != len(self.grid.cell_lat):
            raise ValueError("layer values length must match cell_lat / cell_lon")

        selected = val_arr[self.point_indices]
        finite_points = np.isfinite(selected)
        flat_size = self.size * self.size
        if np.any(finite_points):
            pix = self.pixel_indices[finite_points]
            vals = selected[finite_points].astype(np.float64, copy=False)
            sums = np.bincount(pix, weights=vals, minlength=flat_size)
            counts = np.bincount(pix, minlength=flat_size)
            raster = np.full(flat_size, np.nan, dtype=np.float32)
            occupied = counts > 0
            raster[occupied] = (sums[occupied] / counts[occupied]).astype(np.float32)
            raster = raster.reshape((self.size, self.size))
        else:
            raster = np.full((self.size, self.size), np.nan, dtype=np.float32)

        raster = _fill_nans_nearest(raster, fill_mask=self.support_mask)
        raster[~self.circle_mask] = np.nan
        finite = np.isfinite(raster)
        if not np.any(finite):
            actual_min = float(vmin) if vmin is not None else -140.0
            actual_max = float(vmax) if vmax is not None else -60.0
            vmin_used = actual_min
            vmax_used = actual_max
        else:
            actual_min = float(np.nanmin(raster))
            actual_max = float(np.nanmax(raster))
            vmin_used = actual_min if vmin is None else float(vmin)
            vmax_used = actual_max if vmax is None else float(vmax)
            if not np.isfinite(vmin_used) or not np.isfinite(vmax_used) or vmax_used <= vmin_used:
                vmin_used, vmax_used = -140.0, -60.0

        rgba = _colorize_rsrp(raster, vmin_used, vmax_used, alpha=alpha)
        rgba[..., 3] = np.clip(
            np.rint(rgba[..., 3].astype(np.float64) * self.fade).astype(np.int32),
            0,
            255,
        ).astype(np.uint8)
        img = Image.fromarray(rgba, mode="RGBA")
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        return {
            "png_b64": data_url,
            "width": self.size,
            "height": self.size,
            "radius_m": self.radius_m,
            "vmin": vmin_used,
            "vmax": vmax_used,
            "actual_min": actual_min,
            "actual_max": actual_max,
        }


def attenuation_grid_to_png_ellipse(
    grid: AttenuationGrid,
    size: int = 768,
    vmin: Optional[float] = None,
    vmax: Optional[float] = None,
    alpha: float = 0.70,
    rsrp_values: Optional[List[float]] = None,
) -> Dict[str, Any]:
    """Compatibility wrapper for rendering a single ellipse layer."""

    values = grid.rsrp_dbm if rsrp_values is None else rsrp_values
    if values is None or len(values) == 0:
        raise ValueError("Empty attenuation grid")
    return EllipseRasterizer(grid, size=size).render(
        values,
        vmin=vmin,
        vmax=vmax,
        alpha=alpha,
    )


def attenuation_grid_to_png_metric(
    grid: AttenuationGrid,
    size: Optional[int] = None,
    vmin: Optional[float] = None,
    vmax: Optional[float] = None,
    alpha: float = 0.70,
    rsrp_values: Optional[List[float]] = None,
) -> Dict[str, Any]:
    """
    Render a dense metric-grid coverage field 1:1 (step_m pixels) for map-aligned drape.
    """
    rsrp_src = rsrp_values if rsrp_values is not None else grid.rsrp_dbm
    if _empty(grid.cell_lat) or _empty(grid.cell_lon) or _empty(rsrp_src):
        raise ValueError("Empty attenuation grid")

    dr = float(getattr(grid.rf_params, "step_m", 5.0) or 5.0)
    radius_m = float(getattr(grid.rf_params, "max_range_m", 0.0) or 0.0)
    if radius_m <= 0.0 or dr <= 0.0:
        raise ValueError("max_range_m and step_m must be > 0 for metric PNG")

    n = int(math.ceil(radius_m / dr))
    if size is None:
        size = 2 * n + 1
    tx_lat = float(grid.tx.lat)
    tx_lon = float(grid.tx.lon)
    earth_m = 6371000.0
    lat0 = math.radians(tx_lat)

    rsrp = np.full((size, size), np.nan, dtype=np.float32)
    for idx, (lat, lon) in enumerate(zip(grid.cell_lat, grid.cell_lon)):
        east = math.radians(float(lon) - tx_lon) * math.cos(lat0) * earth_m
        north = math.radians(float(lat) - tx_lat) * earth_m
        j = int(round(east / dr)) + n
        i = n - int(round(north / dr))
        if 0 <= i < size and 0 <= j < size:
            rsrp[i, j] = float(rsrp_src[idx])

    yy, xx = np.mgrid[0:size, 0:size]
    x_m = (xx / max(1, size - 1) - 0.5) * (2.0 * radius_m)
    y_m = ((size - 1 - yy) / max(1, size - 1) - 0.5) * (2.0 * radius_m)
    circle_mask = (x_m * x_m + y_m * y_m) <= (radius_m * radius_m)
    rsrp[~circle_mask] = np.nan

    finite = np.isfinite(rsrp)
    if not np.any(finite):
        vmin_used = float(vmin) if vmin is not None else -140.0
        vmax_used = float(vmax) if vmax is not None else -60.0
        actual_min, actual_max = vmin_used, vmax_used
    else:
        actual_min = float(np.nanmin(rsrp))
        actual_max = float(np.nanmax(rsrp))
        vmin_used = float(np.nanmin(rsrp)) if vmin is None else float(vmin)
        vmax_used = float(np.nanmax(rsrp)) if vmax is None else float(vmax)
        if not np.isfinite(vmin_used) or not np.isfinite(vmax_used) or vmax_used <= vmin_used:
            vmin_used, vmax_used = -140.0, -60.0

    rgba = _colorize_rsrp(rsrp, vmin_used, vmax_used, alpha=alpha)
    feather_m = max(30.0, radius_m * 0.03)
    r_pix = np.sqrt(x_m * x_m + y_m * y_m)
    fade = np.clip((radius_m - r_pix) / feather_m, 0.0, 1.0)
    rgba[..., 3] = np.clip(
        np.rint(rgba[..., 3].astype(np.float64) * fade).astype(np.int32), 0, 255
    ).astype(np.uint8)

    try:
        from PIL import Image
        img = Image.fromarray(rgba, mode="RGBA")
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        data_url = f"data:image/png;base64,{b64}"
    except Exception as e:
        logger.exception("Failed to encode metric heatmap PNG: %s", e)
        raise

    return {
        "png_b64": data_url,
        "width": int(size),
        "height": int(size),
        "radius_m": radius_m,
        "vmin": vmin_used,
        "vmax": vmax_used,
        "actual_min": actual_min,
        "actual_max": actual_max,
        "grid_mode": "metric",
        "step_m": dr,
    }


def _colorize_rsrp(rsrp: np.ndarray, vmin: float, vmax: float, alpha: float = 0.70) -> np.ndarray:
    """Vectorized port of planner_3d.js colorForValue()."""
    h, w = rsrp.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)

    finite = np.isfinite(rsrp)
    if not np.any(finite):
        return rgba

    t = (rsrp.astype(np.float64) - vmin) / (vmax - vmin)
    t = np.clip(t, 0.0, 1.0)

    r = np.zeros_like(t)
    g = np.zeros_like(t)
    b = np.zeros_like(t)

    # Piecewise gradient
    m0 = t < 0.2
    u0 = np.zeros_like(t)
    u0[m0] = t[m0] / 0.2
    g[m0] = u0[m0]
    b[m0] = 1.0

    m1 = (t >= 0.2) & (t < 0.4)
    u1 = np.zeros_like(t)
    u1[m1] = (t[m1] - 0.2) / 0.2
    g[m1] = 1.0
    b[m1] = 1.0 - u1[m1]

    m2 = (t >= 0.4) & (t < 0.6)
    u2 = np.zeros_like(t)
    u2[m2] = (t[m2] - 0.4) / 0.2
    r[m2] = u2[m2]
    g[m2] = 1.0

    m3 = (t >= 0.6) & (t < 0.8)
    u3 = np.zeros_like(t)
    u3[m3] = (t[m3] - 0.6) / 0.2
    r[m3] = 1.0
    g[m3] = 1.0 - 0.5 * u3[m3]

    m4 = t >= 0.8
    u4 = np.zeros_like(t)
    u4[m4] = (t[m4] - 0.8) / 0.2
    r[m4] = 1.0
    g[m4] = 0.5 * (1.0 - u4[m4])

    a = np.zeros_like(t)
    a[finite] = alpha

    rgba[..., 0] = np.clip(np.rint(255.0 * r), 0, 255).astype(np.uint8)
    rgba[..., 1] = np.clip(np.rint(255.0 * g), 0, 255).astype(np.uint8)
    rgba[..., 2] = np.clip(np.rint(255.0 * b), 0, 255).astype(np.uint8)
    rgba[..., 3] = np.clip(np.rint(255.0 * a), 0, 255).astype(np.uint8)

    # Transparent outside finite region (NaN)
    rgba[~finite, 3] = 0
    return rgba



def _fill_nans_nearest(arr: np.ndarray, fill_mask: Optional[np.ndarray] = None) -> np.ndarray:
    """Fill NaNs with a cheap nearest-neighbor approximation (no SciPy dependency).

    If fill_mask is provided, NaNs are only filled where fill_mask is True.
    Cells outside fill_mask remain NaN (transparent in the PNG overlay).
    """
    if fill_mask is None:
        mask = np.ones_like(arr, dtype=bool)
    else:
        mask = np.asarray(fill_mask, dtype=bool)
        if mask.shape != arr.shape:
            raise ValueError("fill_mask shape must match arr")

    out = arr.copy()
    # Keep outside-mask region as NaN and never fill it.
    out[~mask] = np.nan

    need = np.isnan(out) & mask
    if not need.any():
        return out

    # Iterative wavefront fill from existing samples.
    # Cap iterations to avoid worst-case slowdowns; higher caps help with sparse rays at large radii.
    max_iter = 256
    for _ in range(max_iter):
        need = np.isnan(out) & mask
        if not need.any():
            break

        filled_any = False
        tmp = out.copy()

        # From north (copy down)
        src = out[:-1, :]
        src_ok = np.isfinite(src) & mask[:-1, :]
        dst_need = np.isnan(out[1:, :]) & mask[1:, :]
        can = dst_need & src_ok
        if np.any(can):
            tmp[1:, :][can] = src[can]
            filled_any = True

        # From south (copy up)
        src = out[1:, :]
        src_ok = np.isfinite(src) & mask[1:, :]
        dst_need = np.isnan(out[:-1, :]) & mask[:-1, :]
        can = dst_need & src_ok
        if np.any(can):
            tmp[:-1, :][can] = src[can]
            filled_any = True

        # From west (copy right)
        src = out[:, :-1]
        src_ok = np.isfinite(src) & mask[:, :-1]
        dst_need = np.isnan(out[:, 1:]) & mask[:, 1:]
        can = dst_need & src_ok
        if np.any(can):
            tmp[:, 1:][can] = src[can]
            filled_any = True

        # From east (copy left)
        src = out[:, 1:]
        src_ok = np.isfinite(src) & mask[:, 1:]
        dst_need = np.isnan(out[:, :-1]) & mask[:, :-1]
        can = dst_need & src_ok
        if np.any(can):
            tmp[:, :-1][can] = src[can]
            filled_any = True

        out = tmp
        if not filled_any:
            break

    # Remaining NaNs inside mask: fill with mean of available values inside mask.
    remain = np.isnan(out) & mask
    if remain.any():
        valid = out[np.isfinite(out) & mask]
        if valid.size:
            out[remain] = float(np.mean(valid))

    return out

def _apply_circular_mask_in_latlon(
    grid: AttenuationGrid,
    lats_2d: np.ndarray,
    lons_2d: np.ndarray,
    rsrp_2d: np.ndarray,
) -> None:
    """Mask raster outside max_range_m, operating directly in lat/lon raster."""
    try:
        tx_lat = float(grid.tx.lat)
        tx_lon = float(grid.tx.lon)
        earth_m = 6371000.0

        radius_m = float(getattr(grid.rf_params, "max_range_m", 0.0) or 0.0)
        if radius_m <= 0.0:
            return

        x2 = np.deg2rad(lons_2d - tx_lon) * np.cos(np.deg2rad((lats_2d + tx_lat) * 0.5))
        y2 = np.deg2rad(lats_2d - tx_lat)
        dist2_m = earth_m * np.sqrt(x2 * x2 + y2 * y2)

        px_m = max(1.0, radius_m / max(1.0, min(lats_2d.shape[0], lats_2d.shape[1])))
        mask = dist2_m <= (radius_m + 2.0 * px_m)
        rsrp_2d[~mask] = np.nan
    except Exception:
        # Best effort only.
        return
