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
from dataclasses import dataclass
from typing import Tuple, Optional, Dict, Any, Sequence

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
    lat_src = grid.channel_array("cell_lat")
    lon_src = grid.channel_array("cell_lon")
    value_src = grid.channel_array("rsrp_dbm")
    if lat_src is None or lon_src is None or value_src is None or len(lat_src) == 0 or len(lon_src) == 0 or len(value_src) == 0:
        raise ValueError("Empty attenuation grid")

    lat_arr = np.asarray(lat_src, dtype=np.float64)
    lon_arr = np.asarray(lon_src, dtype=np.float64)
    val_arr = np.asarray(value_src, dtype=np.float32)

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


@dataclass(slots=True)
class EllipseHeatmapGeometry:
    """Reusable point-to-texture mapping shared by every rendered RF layer."""

    size: int
    radius_m: float
    point_count: int
    sample_indices: np.ndarray
    linear_pixel_indices: np.ndarray
    support_mask: np.ndarray
    feather_alpha: np.ndarray


def prepare_ellipse_heatmap_geometry(
    grid: AttenuationGrid,
    *,
    size: int,
) -> EllipseHeatmapGeometry:
    """Precompute map geometry once for many aligned heatmap layers.

    A large ISAC result renders many arrays over identical coordinates. Rebuilding
    local ENU coordinates, pixel bins, support masks, and edge feathering for each
    layer wastes both CPU and large temporary matrices.
    """

    lat_src = grid.channel_array("cell_lat")
    lon_src = grid.channel_array("cell_lon")
    if lat_src is None or lon_src is None or len(lat_src) == 0 or len(lon_src) == 0:
        raise ValueError("Empty attenuation grid")
    if len(lat_src) != len(lon_src):
        raise ValueError("cell_lat/cell_lon lengths differ")

    radius_m = float(getattr(grid.rf_params, "max_range_m", 0.0) or 0.0)
    if radius_m <= 0.0:
        raise ValueError("rf_params.max_range_m must be > 0 for ellipse PNG")

    tx_lat = float(grid.tx.lat)
    tx_lon = float(grid.tx.lon)
    earth_m = 6_371_000.0
    lat0 = math.radians(tx_lat)
    lon_arr = np.asarray(lon_src, dtype=np.float64)
    lat_arr = np.asarray(lat_src, dtype=np.float64)
    dx = ((lon_arr - tx_lon) * (math.pi / 180.0) * math.cos(lat0) * earth_m).astype(np.float32)
    dy = ((lat_arr - tx_lat) * (math.pi / 180.0) * earth_m).astype(np.float32)

    pixel_scale = np.float32((size - 1) / (2.0 * radius_m))
    jj = np.rint((dx + np.float32(radius_m)) * pixel_scale).astype(np.int32)
    ii = np.rint((np.float32(radius_m) - dy) * pixel_scale).astype(np.int32)
    inb = (
        (jj >= 0) & (jj < size)
        & (ii >= 0) & (ii < size)
        & (dx * dx + dy * dy <= np.float32(radius_m * radius_m))
    )
    sample_indices = np.flatnonzero(inb).astype(np.int32, copy=False)
    linear_pixel_indices = (ii[inb] * np.int32(size) + jj[inb]).astype(np.int32, copy=False)

    # Group writes by destination pixel once. Stable ordering preserves the exact
    # per-pixel accumulation order of the original point sequence while improving
    # cache locality for every subsequently rendered aligned layer.
    if linear_pixel_indices.size > 1:
        order = np.argsort(linear_pixel_indices, kind="stable")
        sample_indices = sample_indices[order]
        linear_pixel_indices = linear_pixel_indices[order]
        del order

    try:
        bin_deg = float(getattr(grid.rf_params, "dtheta_deg", 1.0) or 1.0)
    except Exception:
        bin_deg = 1.0
    bin_deg = max(0.25, min(5.0, bin_deg))
    n_bins = int(max(72, round(360.0 / bin_deg)))
    bin_width = np.float32(360.0 / n_bins)

    dx_in = dx[inb]
    dy_in = dy[inb]
    theta_s = np.arctan2(dx_in, dy_in).astype(np.float32, copy=False)
    theta_s *= np.float32(180.0 / math.pi)
    theta_s += np.float32(360.0)
    np.remainder(theta_s, np.float32(360.0), out=theta_s)
    bi_s = np.floor(theta_s / bin_width).astype(np.int16)
    np.clip(bi_s, 0, n_bins - 1, out=bi_s)
    r_s = np.hypot(dx_in, dy_in).astype(np.float32, copy=False)
    rmax = np.zeros(n_bins, dtype=np.float32)
    np.maximum.at(rmax, bi_s, r_s)
    del dx_in, dy_in, theta_s, bi_s, r_s, ii, jj, inb, dx, dy

    # Build full-pixel support using float32 broadcast grids rather than np.mgrid
    # int64 matrices. r_pix is later reused in-place to produce feather alpha.
    axis = np.linspace(-radius_m, radius_m, size, dtype=np.float32)
    y_axis = axis[::-1]
    r_pix = np.add(np.square(y_axis[:, None]), np.square(axis[None, :]), dtype=np.float32)
    np.sqrt(r_pix, out=r_pix)
    theta_pix = np.arctan2(axis[None, :], y_axis[:, None]).astype(np.float32, copy=False)
    theta_pix *= np.float32(180.0 / math.pi)
    theta_pix += np.float32(360.0)
    np.remainder(theta_pix, np.float32(360.0), out=theta_pix)
    bi_pix = np.floor(theta_pix / bin_width).astype(np.int16)
    np.clip(bi_pix, 0, n_bins - 1, out=bi_pix)
    reach = rmax[bi_pix]

    try:
        dr_m = float(getattr(grid.rf_params, "step_m", 5.0) or 5.0)
    except Exception:
        dr_m = 5.0
    support_mask = (
        (r_pix <= np.float32(radius_m))
        & (reach > 0.0)
        & (r_pix <= reach + np.float32(dr_m))
    )
    del theta_pix, bi_pix, reach, rmax, axis, y_axis

    feather_m = max(30.0, radius_m * 0.03)
    np.subtract(np.float32(radius_m), r_pix, out=r_pix)
    r_pix /= np.float32(feather_m)
    np.clip(r_pix, 0.0, 1.0, out=r_pix)
    r_pix *= np.float32(255.0)
    feather_alpha = np.rint(r_pix).astype(np.uint8)
    del r_pix

    return EllipseHeatmapGeometry(
        size=int(size),
        radius_m=radius_m,
        point_count=len(lat_src),
        sample_indices=sample_indices,
        linear_pixel_indices=linear_pixel_indices,
        support_mask=support_mask,
        feather_alpha=feather_alpha,
    )


def attenuation_grid_to_png_ellipse(
    grid: AttenuationGrid,
    size: int = 768,
    vmin: Optional[float] = None,
    vmax: Optional[float] = None,
    alpha: float = 0.70,
    rsrp_values: Optional[Sequence[float] | np.ndarray] = None,
    geometry: Optional[EllipseHeatmapGeometry] = None,
) -> Dict[str, Any]:
    """Create a pre-colored PNG for map-aligned draping.

    ``geometry`` should be reused when rendering multiple aligned ISAC layers.
    This bounds per-layer scratch memory and avoids recomputing the same spatial
    mapping for echo/SNR/margin/Doppler/range/etc.
    """

    rsrp_src = rsrp_values if rsrp_values is not None else grid.channel_array("rsrp_dbm")
    if rsrp_src is None or len(rsrp_src) == 0:
        raise ValueError("Empty attenuation grid")
    geom = geometry or prepare_ellipse_heatmap_geometry(grid, size=size)
    if geom.size != size:
        raise ValueError("heatmap geometry size does not match requested size")
    if len(rsrp_src) != geom.point_count:
        raise ValueError("rsrp_values length must match cell coordinates")

    val_arr = np.asarray(rsrp_src, dtype=np.float32)
    pixel_count = size * size
    sums = np.zeros(pixel_count, dtype=np.float32)
    counts = np.zeros(pixel_count, dtype=np.uint16)

    # Preserve the exact point order used by the original np.add.at path while
    # bounding source-side scratch.  A million-point layer previously materialized
    # sample_values + finite mask + pixel indices + filtered values all at once.
    # Chunking keeps the same accumulation order/pixel values with O(chunk) scratch.
    sample_indices = geom.sample_indices
    linear_pixels = geom.linear_pixel_indices
    raster_chunk = 262_144
    for start in range(0, sample_indices.size, raster_chunk):
        stop = min(start + raster_chunk, sample_indices.size)
        sample_values = val_arr[sample_indices[start:stop]]
        finite_samples = np.isfinite(sample_values)
        if np.any(finite_samples):
            pixel_indices = linear_pixels[start:stop][finite_samples]
            values = sample_values[finite_samples]
            np.add.at(sums, pixel_indices, values)
            np.add.at(counts, pixel_indices, np.uint16(1))
        del sample_values, finite_samples

    rsrp_flat = np.full(pixel_count, np.nan, dtype=np.float32)
    populated = counts > 0
    rsrp_flat[populated] = sums[populated] / counts[populated]
    rsrp = rsrp_flat.reshape((size, size))
    del sums, counts, populated

    rsrp = _fill_nans_nearest(rsrp, fill_mask=geom.support_mask)
    finite = np.isfinite(rsrp)
    if not np.any(finite):
        actual_min = float(vmin) if vmin is not None else -140.0
        actual_max = float(vmax) if vmax is not None else -60.0
        vmin_used = float(vmin) if vmin is not None else -140.0
        vmax_used = float(vmax) if vmax is not None else -60.0
    else:
        actual_min = float(np.nanmin(rsrp))
        actual_max = float(np.nanmax(rsrp))
        vmin_used = actual_min if vmin is None else float(vmin)
        vmax_used = actual_max if vmax is None else float(vmax)
        if not np.isfinite(vmin_used) or not np.isfinite(vmax_used) or vmax_used <= vmin_used:
            vmin_used, vmax_used = -140.0, -60.0

    rgba = _colorize_rsrp(rsrp, vmin_used, vmax_used, alpha=alpha)
    alpha_product = np.multiply(
        rgba[..., 3], geom.feather_alpha, dtype=np.uint16
    )
    alpha_product += np.uint16(127)
    alpha_product //= np.uint16(255)
    rgba[..., 3] = alpha_product.astype(np.uint8)
    del alpha_product, rsrp, finite

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
        "radius_m": geom.radius_m,
        "vmin": vmin_used,
        "vmax": vmax_used,
        "actual_min": actual_min,
        "actual_max": actual_max,
    }


def attenuation_grid_to_png_metric(
    grid: AttenuationGrid,
    size: Optional[int] = None,
    vmin: Optional[float] = None,
    vmax: Optional[float] = None,
    alpha: float = 0.70,
    rsrp_values: Optional[Sequence[float] | np.ndarray] = None,
) -> Dict[str, Any]:
    """
    Render a dense metric-grid coverage field 1:1 (step_m pixels) for map-aligned drape.
    """
    lat_src = grid.channel_array("cell_lat")
    lon_src = grid.channel_array("cell_lon")
    rsrp_src = rsrp_values if rsrp_values is not None else grid.channel_array("rsrp_dbm")
    if (
        lat_src is None or lon_src is None or rsrp_src is None
        or len(lat_src) == 0 or len(lon_src) == 0 or len(rsrp_src) == 0
    ):
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
    for idx, (lat, lon) in enumerate(zip(lat_src, lon_src)):
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
    """Memory-bounded vectorized port of planner_3d.js colorForValue()."""

    h, w = rsrp.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    finite = np.isfinite(rsrp)
    if not np.any(finite):
        return rgba

    t = np.empty_like(rsrp, dtype=np.float32)
    np.subtract(rsrp, np.float32(vmin), out=t)
    t /= np.float32(vmax - vmin)
    np.clip(t, 0.0, 1.0, out=t)
    r = rgba[..., 0]
    g = rgba[..., 1]
    b = rgba[..., 2]

    m = finite & (t < 0.2)
    u = t[m] / np.float32(0.2)
    g[m] = np.rint(np.float32(255.0) * u).astype(np.uint8)
    b[m] = 255
    del u, m

    m = finite & (t >= 0.2) & (t < 0.4)
    u = (t[m] - np.float32(0.2)) / np.float32(0.2)
    g[m] = 255
    b[m] = np.rint(np.float32(255.0) * (np.float32(1.0) - u)).astype(np.uint8)
    del u, m

    m = finite & (t >= 0.4) & (t < 0.6)
    u = (t[m] - np.float32(0.4)) / np.float32(0.2)
    r[m] = np.rint(np.float32(255.0) * u).astype(np.uint8)
    g[m] = 255
    del u, m

    m = finite & (t >= 0.6) & (t < 0.8)
    u = (t[m] - np.float32(0.6)) / np.float32(0.2)
    r[m] = 255
    g[m] = np.rint(np.float32(255.0) * (np.float32(1.0) - np.float32(0.5) * u)).astype(np.uint8)
    del u, m

    m = finite & (t >= 0.8)
    u = (t[m] - np.float32(0.8)) / np.float32(0.2)
    r[m] = 255
    g[m] = np.rint(np.float32(127.5) * (np.float32(1.0) - u)).astype(np.uint8)
    del u, m, t

    rgba[..., 3][finite] = np.uint8(max(0, min(255, round(255.0 * alpha))))
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

    # Iterative wavefront fill from existing samples.  Reuse one destination
    # buffer across iterations; np.copyto preserves the previous synchronous
    # north/south/west/east update semantics but avoids allocating a full raster
    # on every wavefront step.
    max_iter = 256
    tmp = np.empty_like(out)
    for _ in range(max_iter):
        need = np.isnan(out) & mask
        if not need.any():
            break

        filled_any = False
        np.copyto(tmp, out)

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

        out, tmp = tmp, out
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
