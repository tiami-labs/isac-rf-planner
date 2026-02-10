"""Heatmap generation from attenuation grid."""

import logging
from typing import Tuple

import numpy as np

from ..pipeline.schemas import AttenuationGrid

logger = logging.getLogger(__name__)


def attenuation_grid_to_raster(
    grid: AttenuationGrid,
    width: int = 256,
    height: int = 256,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Convert scattered RSRP points to a simple raster.

    Returns:
      lats_2d, lons_2d, rsrp_2d  (all [H, W])
    MVP: do a simple scatter + nearest neighbor; no need for kriging.
    """
    if not grid.cell_lat or not grid.cell_lon or not grid.rsrp_dbm:
        raise ValueError("Empty attenuation grid")

    # Compute bounding box
    min_lat = min(grid.cell_lat)
    max_lat = max(grid.cell_lat)
    min_lon = min(grid.cell_lon)
    max_lon = max(grid.cell_lon)

    # Create uniform grid
    lat_vals = np.linspace(min_lat, max_lat, height)
    lon_vals = np.linspace(min_lon, max_lon, width)
    lats_2d, lons_2d = np.meshgrid(lat_vals, lon_vals, indexing="ij")

    # Simple nearest neighbor assignment
    rsrp_2d = np.full((height, width), np.nan, dtype=np.float32)

    for i in range(len(grid.cell_lat)):
        lat = grid.cell_lat[i]
        lon = grid.cell_lon[i]
        rsrp = grid.rsrp_dbm[i]

        # Find nearest grid point
        lat_idx = np.argmin(np.abs(lat_vals - lat))
        lon_idx = np.argmin(np.abs(lon_vals - lon))

        # Assign value (or average if multiple points map to same cell)
        if np.isnan(rsrp_2d[lat_idx, lon_idx]):
            rsrp_2d[lat_idx, lon_idx] = rsrp
        else:
            # Average if multiple points
            rsrp_2d[lat_idx, lon_idx] = (rsrp_2d[lat_idx, lon_idx] + rsrp) / 2.0

    # Fill NaN values with nearest neighbor (inside the sampled region only).
    rsrp_filled = _fill_nans(rsrp_2d.copy())

    # Enforce a circular mask so the heatmap is a radius around the TX,
    # not the full bounding-box rectangle.
    #
    # IMPORTANT: The world grid is generated on a square bounding box around TX,
    # so the farthest points are corners (~sqrt(2) * max_range). If we derive the
    # radius from the farthest cell, the mask will NOT be circular.
    # Use the configured RF max_range_m as the authoritative radius.
    try:
        tx_lat = float(grid.tx.lat)
        tx_lon = float(grid.tx.lon)
        earth_m = 6371000.0

        lat_arr = np.asarray(grid.cell_lat, dtype=np.float64)
        lon_arr = np.asarray(grid.cell_lon, dtype=np.float64)

        # Equirectangular approximation (sufficient at city scale).
        x = np.deg2rad(lon_arr - tx_lon) * np.cos(np.deg2rad((lat_arr + tx_lat) * 0.5))
        y = np.deg2rad(lat_arr - tx_lat)
        # Authoritative radius.
        radius_m = float(getattr(grid.rf_params, "max_range_m", 0.0) or 0.0)
        if radius_m <= 0.0:
            dist_m = earth_m * np.sqrt(x * x + y * y)
            radius_m = float(np.nanmax(dist_m)) if dist_m.size else 0.0

        # Compute distances for raster grid
        x2 = np.deg2rad(lons_2d - tx_lon) * np.cos(
            np.deg2rad((lats_2d + tx_lat) * 0.5)
        )
        y2 = np.deg2rad(lats_2d - tx_lat)
        dist2_m = earth_m * np.sqrt(x2 * x2 + y2 * y2)

        # Add a small margin (~1 pixel) to avoid clipping the boundary.
        px_m = max(1.0, radius_m / max(1.0, min(width, height)))
        mask = dist2_m <= (radius_m + 2.0 * px_m)
        rsrp_filled[~mask] = np.nan
    except Exception:
        # If anything goes wrong, fall back to filled raster.
        pass

    return lats_2d, lons_2d, rsrp_filled



def _fill_nans(arr: np.ndarray) -> np.ndarray:
    """
    Fill NaN values efficiently.
    
    Strategy: If few NaNs, use simple mean fill (very fast).
    Otherwise, use limited-radius nearest neighbor search.
    """
    mask = np.isnan(arr)
    if not np.any(mask):
        return arr
    
    valid_values = arr[~mask]
    if len(valid_values) == 0:
        # No valid values, fill with zeros
        arr[mask] = 0.0
        return arr
    
    num_nans = np.sum(mask)
    total_cells = arr.size
    
    # If NaNs are < 10% of cells, use simple mean fill (much faster)
    if num_nans < total_cells * 0.1:
        mean_value = np.nanmean(arr) if np.any(~mask) else 0.0
        arr[mask] = mean_value
        return arr
    
    # Otherwise, use nearest neighbor with limited search radius
    nan_coords = np.argwhere(mask)
    valid_coords = np.argwhere(~mask)
    search_radius = min(16, min(arr.shape[0], arr.shape[1]) // 8)  # Smaller radius for speed
    search_radius_sq = search_radius ** 2
    
    for nan_coord in nan_coords:
        # Calculate distances to valid points
        distances_sq = np.sum((valid_coords - nan_coord) ** 2, axis=1)
        
        # Limit to nearby points
        nearby = distances_sq <= search_radius_sq
        if np.any(nearby):
            nearest_idx = np.argmin(distances_sq[nearby])
            arr[nan_coord[0], nan_coord[1]] = valid_values[nearby][nearest_idx]
        else:
            # Fallback: use global nearest
            nearest_idx = np.argmin(distances_sq)
            arr[nan_coord[0], nan_coord[1]] = valid_values[nearest_idx]
    
    return arr
