"""Tile 360° panoramas into overlapping crops."""

import logging
from dataclasses import dataclass
from typing import List

import numpy as np
from PIL import Image

from .pano_geometry import get_tile_center_azimuth, lonlat_to_xy, xy_to_lonlat

logger = logging.getLogger(__name__)


@dataclass
class Tile:
    """Represents a single tile from a panorama."""

    image: np.ndarray  # The cropped image (H, W, 3)
    center_azimuth: float  # Center azimuth in degrees
    start_lon: float  # Starting longitude in degrees
    end_lon: float  # Ending longitude in degrees
    index: int  # Tile index


def tile_pano(
    pano: np.ndarray,
    num_views: int = 4,
    overlap_deg: float = 30.0,
    max_pixels: int = 512,
) -> List[Tile]:
    """
    Tile a 360° panorama into overlapping crops.

    Args:
        pano: Panorama image as numpy array (H, W, 3)
        num_views: Number of tiles to extract
        overlap_deg: Overlap between adjacent tiles in degrees
        max_pixels: Maximum pixels per tile (will downscale if needed)

    Returns:
        List of Tile objects
    """
    h, w = pano.shape[:2]
    logger.info(f"Tiling {w}x{h} panorama into {num_views} views with {overlap_deg}° overlap")

    # Calculate tile width in degrees
    total_deg = 360.0
    tile_width_deg = (total_deg + (num_views - 1) * overlap_deg) / num_views

    tiles = []
    for i in range(num_views):
        # Calculate tile center and bounds
        center_azimuth = get_tile_center_azimuth(i, num_views)
        start_lon = center_azimuth - tile_width_deg / 2
        end_lon = center_azimuth + tile_width_deg / 2

        # Handle wrap-around
        if start_lon < -180:
            start_lon += 360
        if end_lon > 180:
            end_lon -= 360

        # Convert to pixel coordinates
        if start_lon < end_lon:
            # Normal case: no wrap-around
            start_x, _ = lonlat_to_xy(start_lon, 0, w, h)
            end_x, _ = lonlat_to_xy(end_lon, 0, w, h)
            if end_x < start_x:
                end_x = w
            tile_img = pano[:, start_x:end_x]
        else:
            # Wrap-around case: tile spans the -180/180 boundary
            start_x, _ = lonlat_to_xy(start_lon, 0, w, h)
            end_x, _ = lonlat_to_xy(end_lon, 0, w, h)
            # Concatenate left and right parts
            left_part = pano[:, start_x:]
            right_part = pano[:, :end_x]
            tile_img = np.concatenate([left_part, right_part], axis=1)

        # Resize if needed to fit max_pixels constraint
        tile_h, tile_w = tile_img.shape[:2]
        if tile_h * tile_w > max_pixels * max_pixels:
            scale = np.sqrt((max_pixels * max_pixels) / (tile_h * tile_w))
            new_w = int(tile_w * scale)
            new_h = int(tile_h * scale)
            tile_img_pil = Image.fromarray(tile_img)
            tile_img_pil = tile_img_pil.resize((new_w, new_h), Image.Resampling.LANCZOS)
            tile_img = np.array(tile_img_pil)

        tile = Tile(
            image=tile_img,
            center_azimuth=center_azimuth,
            start_lon=start_lon,
            end_lon=end_lon,
            index=i,
        )
        tiles.append(tile)
        logger.debug(
            f"Tile {i}: {tile_img.shape[1]}x{tile_img.shape[0]} pixels, "
            f"azimuth {center_azimuth:.1f}°"
        )

    return tiles

