"""Panorama preprocessing and tiling."""

import logging
from dataclasses import dataclass
from typing import List

import numpy as np
from PIL import Image

from .image_io.tiling import Tile, tile_pano as _tile_pano

logger = logging.getLogger(__name__)


@dataclass
class ProcessedTile:
    """Processed tile with metadata."""

    image: np.ndarray
    tile_id: int
    yaw_center_deg: float
    hfov_deg: float
    vfov_deg: float


def tile_pano(
    pano: np.ndarray,
    num_views: int = 4,
    overlap_deg: float = 30.0,
    max_pixels: int = 512,
) -> List[ProcessedTile]:
    """
    Tile a 360° panorama into overlapping crops.

    Args:
        pano: Panorama image as numpy array (H, W, 3)
        num_views: Number of tiles to extract
        overlap_deg: Overlap between adjacent tiles in degrees
        max_pixels: Maximum pixels per tile (will downscale if needed)

    Returns:
        List of ProcessedTile objects
    """
    tiles = _tile_pano(pano, num_views=num_views, overlap_deg=overlap_deg, max_pixels=max_pixels)

    processed = []
    for tile in tiles:
        # Estimate FOV from tile dimensions
        h, w = tile.image.shape[:2]
        hfov = tile.end_lon - tile.start_lon
        if hfov < 0:
            hfov += 360.0
        vfov = 180.0  # Full vertical FOV for equirectangular

        processed.append(
            ProcessedTile(
                image=tile.image,
                tile_id=tile.index,
                yaw_center_deg=tile.center_azimuth,
                hfov_deg=hfov,
                vfov_deg=vfov,
            )
        )

    return processed

