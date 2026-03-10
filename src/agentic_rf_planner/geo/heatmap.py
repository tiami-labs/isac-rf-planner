"""Heatmap generation from attenuation grid.

Performance notes:
- The attenuation grid can have tens of thousands of scattered points.
- For 3D OSM-only visualization, we prefer returning a pre-colored PNG texture (base64)
  rather than large JSON arrays.
"""

import base64
import io
import logging
from typing import Tuple, Optional, Dict, Any

import numpy as np

from ..pipeline.schemas import AttenuationGrid

logger = logging.getLogger(__name__)


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
    if not grid.cell_lat or not grid.cell_lon or not grid.rsrp_dbm:
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


def attenuation_grid_to_png_ellipse(
    grid: AttenuationGrid,
    size: int = 768,
    vmin: Optional[float] = None,
    vmax: Optional[float] = None,
    alpha: float = 0.70,
) -> Dict[str, Any]:
    """
    Create a pre-colored PNG (base64 data URL) suitable for Cesium ellipse draping.

    Mapping:
      - The ellipse is centered at TX with semiMajor/semiMinor = max_range_m.
      - Texture coordinates map linearly to local East/North meters in [-R, R].

    Returns dict:
      {
        "png_b64": "data:image/png;base64,...",
        "width": size,
        "height": size,
        "radius_m": max_range_m,
        "vmin": vmin_used,
        "vmax": vmax_used,
        "actual_min": actual_min,
        "actual_max": actual_max,
      }
    """
    if not grid.cell_lat or not grid.cell_lon or not grid.rsrp_dbm:
        raise ValueError("Empty attenuation grid")

    radius_m = float(getattr(grid.rf_params, "max_range_m", 0.0) or 0.0)
    if radius_m <= 0.0:
        raise ValueError("rf_params.max_range_m must be > 0 for ellipse PNG")

    tx_lat = float(grid.tx.lat)
    tx_lon = float(grid.tx.lon)

    lat_arr = np.asarray(grid.cell_lat, dtype=np.float64)
    lon_arr = np.asarray(grid.cell_lon, dtype=np.float64)
    val_arr = np.asarray(grid.rsrp_dbm, dtype=np.float32)

    # Local EN (meters) via equirectangular approximation.
    earth_m = 6371000.0
    lat0 = np.deg2rad(tx_lat)
    dx = np.deg2rad(lon_arr - tx_lon) * np.cos(lat0) * earth_m  # east
    dy = np.deg2rad(lat_arr - tx_lat) * earth_m                 # north

    # Map to pixel coords in [0, size-1]
    u = (dx + radius_m) / (2.0 * radius_m)
    v = (dy + radius_m) / (2.0 * radius_m)

    jj = np.rint(u * (size - 1)).astype(np.int32)
    ii = np.rint((1.0 - v) * (size - 1)).astype(np.int32)

    inb = (
        (jj >= 0) & (jj < size) &
        (ii >= 0) & (ii < size) &
        (dx * dx + dy * dy <= (radius_m * radius_m))
    )

    sums = np.zeros((size, size), dtype=np.float64)
    cnts = np.zeros((size, size), dtype=np.int32)
    np.add.at(sums, (ii[inb], jj[inb]), val_arr[inb].astype(np.float64))
    np.add.at(cnts, (ii[inb], jj[inb]), 1)

    rsrp = np.full((size, size), np.nan, dtype=np.float32)
    m = cnts > 0
    rsrp[m] = (sums[m] / cnts[m]).astype(np.float32)

    # Build a "support" mask so we only fill small holes between nearby rays,
    # and we do NOT smear values into large unsampled regions (e.g. beyond ray termination).
    yy, xx = np.mgrid[0:size, 0:size]
    x_m = (xx / (size - 1) - 0.5) * (2.0 * radius_m)          # east
    y_m = ((size - 1 - yy) / (size - 1) - 0.5) * (2.0 * radius_m)  # north
    r_pix = np.sqrt(x_m * x_m + y_m * y_m)
    circle_mask = r_pix <= radius_m

    # Estimate per-bearing reach based on which samples exist.
    # We bin bearings at the simulation's effective dtheta (or finer) and compute r_max per bin.
    try:
        bin_deg = float(getattr(grid.rf_params, "dtheta_deg", 1.0) or 1.0)
    except Exception:
        bin_deg = 1.0
    bin_deg = max(0.25, min(5.0, bin_deg))
    n_bins = int(max(72, round(360.0 / bin_deg)))

    theta_s = (np.degrees(np.arctan2(dx, dy)) + 360.0) % 360.0
    r_s = np.sqrt(dx * dx + dy * dy)

    bi_s = np.floor(theta_s / (360.0 / n_bins)).astype(np.int32)
    bi_s = np.clip(bi_s, 0, n_bins - 1)

    rmax = np.zeros((n_bins,), dtype=np.float32)
    np.maximum.at(rmax, bi_s, r_s.astype(np.float32))

    # Smooth rmax a bit across neighboring bins to avoid tiny gaps from numeric jitter.
    for k in (1, 2):
        rmax = np.maximum(rmax, np.roll(rmax, k))
        rmax = np.maximum(rmax, np.roll(rmax, -k))

    # Per-pixel bearing bins
    theta_pix = (np.degrees(np.arctan2(x_m, y_m)) + 360.0) % 360.0
    bi_pix = np.floor(theta_pix / (360.0 / n_bins)).astype(np.int32)
    bi_pix = np.clip(bi_pix, 0, n_bins - 1)

    # Allow fill only where the ray reached (plus one step for softness).
    try:
        dr_m = float(getattr(grid.rf_params, "step_m", 5.0) or 5.0)
    except Exception:
        dr_m = 5.0

    support_mask = circle_mask & (rmax[bi_pix] > 0.0) & (r_pix <= (rmax[bi_pix] + dr_m))

    # Fill holes within support only (do not fill beyond reach).
    rsrp = _fill_nans_nearest(rsrp, fill_mask=support_mask)

    # Anything outside the circle stays transparent.
    rsrp[~circle_mask] = np.nan

    finite = np.isfinite(rsrp)
    if not np.any(finite):
        actual_min = float(vmin) if vmin is not None else -140.0
        actual_max = float(vmax) if vmax is not None else -60.0
        vmin_used = float(vmin) if vmin is not None else -140.0
        vmax_used = float(vmax) if vmax is not None else -60.0
    else:
        actual_min = float(np.nanmin(rsrp))
        actual_max = float(np.nanmax(rsrp))
        vmin_used = float(np.nanmin(rsrp)) if vmin is None else float(vmin)
        vmax_used = float(np.nanmax(rsrp)) if vmax is None else float(vmax)
        if not np.isfinite(vmin_used) or not np.isfinite(vmax_used) or vmax_used <= vmin_used:
            vmin_used, vmax_used = -140.0, -60.0

    rgba = _colorize_rsrp(rsrp, vmin_used, vmax_used, alpha=alpha)

    # Soften the circular boundary to avoid a visible faint circle outline.
    # Fade alpha to 0 over the last ~3% of the radius so the edge blends into transparent.
    feather_m = max(30.0, radius_m * 0.03)
    fade = np.clip((radius_m - r_pix) / feather_m, 0.0, 1.0)
    rgba[..., 3] = np.clip(
        np.rint(rgba[..., 3].astype(np.float64) * fade).astype(np.int32), 0, 255
    ).astype(np.uint8)

    # Encode PNG
    try:
        from PIL import Image
        img = Image.fromarray(rgba, mode="RGBA")
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        data_url = f"data:image/png;base64,{b64}"
    except Exception as e:
        logger.exception("Failed to encode heatmap PNG: %s", e)
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
