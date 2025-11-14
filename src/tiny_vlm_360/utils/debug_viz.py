"""Debug visualization utilities (optional)."""

import logging
from pathlib import Path
from typing import List

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


def save_tiles(tiles: List, output_dir: Path) -> None:
    """
    Save tile images for debugging.

    Args:
        tiles: List of Tile objects
        output_dir: Directory to save tiles
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    for i, tile in enumerate(tiles):
        img = Image.fromarray(tile.image)
        output_path = output_dir / f"tile_{i:02d}_az{tile.center_azimuth:.0f}.jpg"
        img.save(output_path)
        logger.debug(f"Saved tile {i} to {output_path}")


def stitch_tiles(tiles: List) -> np.ndarray:
    """
    Stitch tiles back together horizontally (for debugging).

    Args:
        tiles: List of Tile objects

    Returns:
        Stitched image as numpy array
    """
    images = [Image.fromarray(tile.image) for tile in tiles]
    widths, heights = zip(*(img.size for img in images))
    total_width = sum(widths)
    max_height = max(heights)

    stitched = Image.new("RGB", (total_width, max_height))
    x_offset = 0
    for img in images:
        stitched.paste(img, (x_offset, 0))
        x_offset += img.size[0]

    return np.array(stitched)

