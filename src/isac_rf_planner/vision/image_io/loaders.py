"""Load and validate 360° panoramic images."""

import logging
from pathlib import Path
from typing import Union

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


def load_pano(image_path: Union[Path, str]) -> np.ndarray:
    """
    Load a 360° equirectangular panorama image.

    Args:
        image_path: Path to the image file

    Returns:
        Image as numpy array (H, W, 3) in RGB format, uint8
    """
    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"Image not found: {path}")

    logger.info(f"Loading panorama from {path}")

    # Load image
    img = Image.open(path)
    img = img.convert("RGB")  # Ensure RGB format

    # Convert to numpy array
    img_array = np.array(img, dtype=np.uint8)

    # Validate aspect ratio (equirectangular should be roughly 2:1)
    h, w = img_array.shape[:2]
    aspect_ratio = w / h
    if not (1.8 <= aspect_ratio <= 2.2):
        logger.warning(
            f"Image aspect ratio {aspect_ratio:.2f} is not close to 2:1. "
            "This may not be an equirectangular panorama."
        )

    logger.info(f"Loaded panorama: {w}x{h} pixels")
    return img_array

