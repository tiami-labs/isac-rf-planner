"""Geometric utilities for equirectangular panoramas."""

import numpy as np


def lonlat_to_xy(lon: float, lat: float, width: int, height: int) -> tuple[int, int]:
    """
    Convert longitude/latitude to pixel coordinates in equirectangular projection.

    Args:
        lon: Longitude in degrees (-180 to 180)
        lat: Latitude in degrees (-90 to 90)
        width: Image width in pixels
        height: Image height in pixels

    Returns:
        (x, y) pixel coordinates
    """
    x = int((lon + 180) / 360 * width)
    y = int((90 - lat) / 180 * height)
    x = max(0, min(width - 1, x))
    y = max(0, min(height - 1, y))
    return x, y


def xy_to_lonlat(x: int, y: int, width: int, height: int) -> tuple[float, float]:
    """
    Convert pixel coordinates to longitude/latitude in equirectangular projection.

    Args:
        x: X pixel coordinate
        y: Y pixel coordinate
        width: Image width in pixels
        height: Image height in pixels

    Returns:
        (longitude, latitude) in degrees
    """
    lon = (x / width) * 360 - 180
    lat = 90 - (y / height) * 180
    return lon, lat


def get_tile_center_azimuth(tile_index: int, num_tiles: int) -> float:
    """
    Get the azimuth (longitude) of the center of a tile.

    Args:
        tile_index: Index of the tile (0 to num_tiles-1)
        num_tiles: Total number of tiles

    Returns:
        Azimuth in degrees (-180 to 180)
    """
    # Distribute tiles evenly around 360 degrees
    step = 360 / num_tiles
    center = -180 + (tile_index + 0.5) * step
    return center


def get_direction_label(azimuth: float) -> str:
    """
    Convert azimuth to a direction label.

    Args:
        azimuth: Azimuth in degrees (-180 to 180)

    Returns:
        Direction label (e.g., "front", "left", "right", "behind")
    """
    # Normalize to 0-360
    if azimuth < 0:
        azimuth += 360

    if 315 <= azimuth or azimuth < 45:
        return "front"
    elif 45 <= azimuth < 135:
        return "right"
    elif 135 <= azimuth < 225:
        return "behind"
    else:  # 225 <= azimuth < 315
        return "left"

