"""Configuration loading and merging from YAML files and environment variables."""

from pathlib import Path
from typing import Any, Dict, Optional

import yaml


def load_yaml(path: Path) -> Dict[str, Any]:
    """Load a YAML configuration file."""
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _find_rf_config_path(rf_config_path: str) -> Optional[Path]:
    """Resolve rf.params.yaml from cwd, package location, or parent dirs."""
    path = Path(rf_config_path)
    if path.is_absolute() and path.exists():
        return path
    # Try cwd
    cand = Path.cwd() / path
    if cand.exists():
        return cand
    # Try relative to package: walk up from isac_rf_planner/ to find project root (has configs/)
    try:
        import isac_rf_planner
        current = Path(isac_rf_planner.__file__).resolve().parent
        for _ in range(5):
            cand = current / path
            if cand.exists():
                return cand
            parent = current.parent
            if parent == current:
                break
            current = parent
    except Exception:
        pass
    return None


def load_rf_config(rf_config_path: str = "configs/rf.params.yaml") -> Dict[str, Any]:
    """Load RF params config only (for API etc. when full config not needed)."""
    path = _find_rf_config_path(rf_config_path)
    if path is None:
        return {}
    return load_yaml(path)


def load_config(
    default_config_path: str = "configs/default.yaml",
    model_config_path: Optional[str] = None,
    rf_config_path: str = "configs/rf.params.yaml",
) -> Dict[str, Any]:
    """
    Load and merge configuration files.

    Args:
        default_config_path: Path to default configuration (relative to cwd or absolute)
        model_config_path: Path to model-specific configuration (optional)
        rf_config_path: Path to RF parameters configuration

    Returns:
        Merged configuration dictionary
    """
    # Use Path directly - let caller handle path resolution
    default_path = Path(default_config_path)
    if not default_path.is_absolute():
        default_path = Path.cwd() / default_path

    config = load_yaml(default_path)

    # Load model config if provided
    if model_config_path:
        model_path = Path(model_config_path)
        if not model_path.is_absolute():
            model_path = Path.cwd() / model_path
        model_config = load_yaml(model_path)
        # Merge model config into vlm section
        if "vlm" not in config:
            config["vlm"] = {}
        config["vlm"].update(model_config)

    # Load RF config
    rf_path = Path(rf_config_path)
    if not rf_path.is_absolute():
        rf_path = Path.cwd() / rf_path
    if rf_path.exists():
        rf_config = load_yaml(rf_path)
        config["rf"] = rf_config

    # Apply environment variable overrides
    config = _apply_env_overrides(config)

    return config


def _apply_env_overrides(config: Dict[str, Any]) -> Dict[str, Any]:
    """Apply environment variable overrides to configuration."""
    import os
    
    if "TINY_VLM_360_DEVICE" in os.environ:
        device = os.environ["TINY_VLM_360_DEVICE"]
        if "vlm" in config:
            config["vlm"]["device"] = device

    if "TINY_VLM_360_LOG_LEVEL" in os.environ:
        log_level = os.environ["TINY_VLM_360_LOG_LEVEL"]
        if "logging" in config:
            config["logging"]["level"] = log_level

    # Mapillary API key from environment variable
    if "MAPILLARY_API_KEY" in os.environ:
        if "streetview" not in config:
            config["streetview"] = {}
        config["streetview"]["api_key"] = os.environ["MAPILLARY_API_KEY"]

    return config
