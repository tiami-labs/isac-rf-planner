"""Configuration loading and merging from YAML files and environment variables."""

import os
from pathlib import Path
from typing import Any, Dict, Optional, Union

import yaml


def load_yaml(path: Path) -> Dict[str, Any]:
    """Load a YAML configuration file."""
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(
    default_config_path: str = "configs/default.yaml",
    model_config_path: Optional[str] = None,
    prompts_config_path: str = "configs/prompts.yaml",
) -> Dict[str, Any]:
    """
    Load and merge configuration files.

    Args:
        default_config_path: Path to default configuration
        model_config_path: Path to model-specific configuration (optional)
        prompts_config_path: Path to prompts configuration

    Returns:
        Merged configuration dictionary
    """
    # Determine base directory (project root)
    if Path(default_config_path).is_absolute():
        base_dir = Path(default_config_path).parent.parent
    else:
        # Try to find project root
        current = Path(__file__).parent
        while current.parent != current:
            if (current / "configs").exists():
                base_dir = current
                break
            current = current.parent
        else:
            base_dir = Path.cwd()

    # Load default config
    default_path = base_dir / default_config_path
    config = load_yaml(default_path)

    # Load model config if provided
    if model_config_path:
        model_path = base_dir / model_config_path
        model_config = load_yaml(model_path)
        # Merge model config into vlm section
        if "vlm" not in config:
            config["vlm"] = {}
        config["vlm"].update(model_config)

    # Load prompts config
    prompts_path = base_dir / prompts_config_path
    if prompts_path.exists():
        prompts_config = load_yaml(prompts_path)
        config["prompts"] = prompts_config

    # Apply environment variable overrides
    config = _apply_env_overrides(config)

    return config


def _apply_env_overrides(config: Dict[str, Any]) -> Dict[str, Any]:
    """Apply environment variable overrides to configuration."""
    # Example: TINY_VLM_360_DEVICE=cuda would override device setting
    if "TINY_VLM_360_DEVICE" in os.environ:
        device = os.environ["TINY_VLM_360_DEVICE"]
        if "vlm" in config:
            config["vlm"]["device"] = device
        if "slm" in config:
            config["slm"]["device"] = device

    if "TINY_VLM_360_LOG_LEVEL" in os.environ:
        log_level = os.environ["TINY_VLM_360_LOG_LEVEL"]
        if "logging" in config:
            config["logging"]["level"] = log_level

    return config

