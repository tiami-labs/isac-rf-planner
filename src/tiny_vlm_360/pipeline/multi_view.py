"""Orchestrate multi-view panorama analysis."""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from ..image_io.loaders import load_pano
from ..image_io.tiling import tile_pano
from ..models import minicpm_v2, qwen2_vl_2b, slm_backend
from .aggregate import aggregate_views
from .postprocess import normalize_output
from .prompts import build_aggregation_prompt, build_view_prompt
from .schemas import SceneAnalysis
from .single_view import analyze_single_view

logger = logging.getLogger(__name__)


def analyze_pano_multi_view(image_path: Path, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Analyze a 360° panorama using multi-view approach.

    Args:
        image_path: Path to panorama image
        cfg: Configuration dictionary

    Returns:
        Scene analysis as dictionary (compatible with SceneAnalysis schema)
    """
    logger.info(f"Starting multi-view analysis of {image_path}")

    # Load panorama
    pano = load_pano(image_path)

    # Tile panorama
    tiling_cfg = cfg.get("tiling", {})
    tiles = tile_pano(
        pano,
        num_views=tiling_cfg.get("num_views", 4),
        overlap_deg=tiling_cfg.get("overlap_deg", 30.0),
        max_pixels=tiling_cfg.get("max_pixels", 512),
    )

    # Load VLM
    vlm_cfg = cfg.get("vlm", {})
    backend = vlm_cfg.get("backend", "transformers")
    model_id = vlm_cfg.get("model_id", "")

    if "minicpm" in model_id.lower() or "minicpm" in str(vlm_cfg.get("model_path", "")).lower():
        vlm = minicpm_v2.MiniCPMV2.load_from_config(vlm_cfg)
    elif "qwen" in model_id.lower() or "qwen" in str(vlm_cfg.get("model_path", "")).lower():
        vlm = qwen2_vl_2b.Qwen2VL2B.load_from_config(vlm_cfg)
    else:
        # Default to MiniCPM
        logger.warning(f"Unknown model, defaulting to MiniCPM-V 2.0")
        vlm = minicpm_v2.MiniCPMV2.load_from_config(vlm_cfg)

    # Load SLM
    slm_cfg = cfg.get("slm", {})
    slm = slm_backend.SLMBackend.load_from_config(slm_cfg)

    # Analyze each view
    prompts_cfg = cfg.get("prompts", {})
    per_view_results: List[str] = []

    for i, tile in enumerate(tiles):
        logger.info(f"Processing view {i+1}/{len(tiles)}")
        prompt = build_view_prompt(i, len(tiles), prompts_cfg)
        text = analyze_single_view(tile.image, prompt, vlm)
        per_view_results.append(text)

    # Aggregate results
    logger.info("Aggregating view analyses")
    agg_prompt = build_aggregation_prompt(per_view_results, prompts_cfg)
    merged_text = aggregate_views(agg_prompt, slm)

    # Post-process and normalize
    scene = normalize_output(merged_text)

    # Convert to dict if it's a Pydantic model
    if isinstance(scene, SceneAnalysis):
        return scene.model_dump()
    elif isinstance(scene, dict):
        return scene
    else:
        # Try to parse as JSON if it's a string
        try:
            return json.loads(merged_text)
        except json.JSONDecodeError:
            # Fallback: return as simple dict
            return {
                "scene_type": "unknown",
                "objects": [],
                "hazards": [],
                "summary": merged_text,
            }

