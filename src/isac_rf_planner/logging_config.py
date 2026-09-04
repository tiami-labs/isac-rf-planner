"""Logging configuration setup."""

import logging
import sys
from typing import Any, Dict, Optional


def setup_logging(config: Optional[Dict[str, Any]] = None) -> None:
    """
    Configure logging based on config dictionary.

    Args:
        config: Configuration dict with 'logging' section containing 'level' and 'format'
    """
    log_config = config.get("logging", {}) if config else {}
    level = log_config.get("level", "INFO")
    fmt = log_config.get("format", "%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
        stream=sys.stdout,
    )
