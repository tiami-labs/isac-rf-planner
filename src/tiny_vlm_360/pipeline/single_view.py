"""Process a single view/tile with VLM."""

import logging
from typing import Any, Dict

import numpy as np

from ..models.base_vlm import BaseVLM

logger = logging.getLogger(__name__)


def analyze_single_view(
    image: np.ndarray, prompt: str, vlm: BaseVLM, **kwargs: Any
) -> str:
    """
    Run VLM inference on a single image tile.

    Args:
        image: Image tile as numpy array (H, W, 3)
        prompt: Text prompt
        vlm: Loaded VLM model
        **kwargs: Additional inference options

    Returns:
        Raw text response from VLM
    """
    logger.debug(f"Analyzing view with prompt: {prompt[:100]}...")
    result = vlm.infer(image, prompt, **kwargs)
    logger.debug(f"View analysis result: {result[:200]}...")
    return result

