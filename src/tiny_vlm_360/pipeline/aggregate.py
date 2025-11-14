"""Aggregate multiple view analyses using SLM."""

import logging
from typing import Any

from ..models.base_slm import BaseSLM

logger = logging.getLogger(__name__)


def aggregate_views(prompt: str, slm: BaseSLM, **kwargs: Any) -> str:
    """
    Aggregate multiple view analyses into a single result.

    Args:
        prompt: Aggregation prompt containing all view analyses
        slm: Loaded SLM model
        **kwargs: Additional inference options

    Returns:
        Aggregated text response
    """
    logger.debug("Aggregating view analyses with SLM")
    result = slm.infer(prompt, **kwargs)
    logger.debug(f"Aggregation result: {result[:200]}...")
    return result

