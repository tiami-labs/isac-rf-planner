"""Extract materials from pano tiles using VLM."""

import json
import logging
import re
from typing import List, Union

import numpy as np

from ..pipeline.schemas import (
    MaterialSegment,
    MaterialType,
    DistanceBand,
    ViewTileDescription,
)
from .models.base_vlm import BaseVLM

logger = logging.getLogger(__name__)


def return_static_material(
    vlm: BaseVLM,
    tile_img: np.ndarray,
    tile_id: int,
    yaw_center_deg: float,
    hfov_deg: float,
    vfov_deg: float,
) -> ViewTileDescription:
    """
    Run VLM on a pano tile and parse materials + angular extents.

    You keep the prompt simple; ask explicitly for left/center/right bands and
    approximate vertical span. Don't chase pixel-perfect segmentation here.
    """

    prompt = (
        "You are analyzing a static outdoor scene from a 360 panorama tile.\n"
        "- Ignore people, cars, buses, bikes and all moving objects.\n"
        "- Only care about static materials: buildings, houses, large structures, trees.\n"
        "- Split the horizontal view into up to 5 segments.\n"
        "For each segment, return JSON with: "
        "[{material: one of [building, house, large_structure, trees, unknown], "
        "yaw_deg_start, yaw_deg_end, pitch_deg_start, pitch_deg_end, distance_band}]."
    )

    raw_text = vlm.infer(tile_img, prompt)
    # TODO: robust JSON parsing with fallback; keep it pragmatic
    segments_json = _safe_parse_json_list(raw_text)

    segments: List[MaterialSegment] = []
    for item in segments_json:
        material = _map_material(item.get("material", "unknown"))
        distance_band = _map_distance_band(item.get("distance_band", "mid"))

        seg = MaterialSegment(
            tile_id=tile_id,
            material=material,
            yaw_deg_start=float(item.get("yaw_deg_start", 0.0)),
            yaw_deg_end=float(item.get("yaw_deg_end", 360.0)),
            pitch_deg_start=float(item.get("pitch_deg_start", -90.0)),
            pitch_deg_end=float(item.get("pitch_deg_end", 90.0)),
            distance_band=distance_band,
            confidence=float(item.get("confidence", 0.7)),
        )
        segments.append(seg)

    return ViewTileDescription(
        tile_id=tile_id,
        yaw_center_deg=yaw_center_deg,
        hfov_deg=hfov_deg,
        vfov_deg=vfov_deg,
        materials=segments,
    )


def _safe_parse_json_list(text: str) -> List[dict]:
    """Parse JSON list from VLM output, with fallback."""
    # Try to extract JSON array
    json_match = re.search(r"\[.*\]", text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            logger.warning("Failed to parse JSON from VLM output")

    # Fallback: try to parse as single object
    json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if json_match:
        try:
            obj = json.loads(json_match.group(0))
            return [obj] if isinstance(obj, dict) else obj
        except json.JSONDecodeError:
            pass

    # Last resort: return empty list
    logger.warning("Could not parse any JSON from VLM output")
    return []


def _map_material(label: Union[str, None]) -> MaterialType:
    """Map VLM label to MaterialType."""
    if not label:
        return MaterialType.UNKNOWN
    label = label.lower()
    if "build" in label:
        return MaterialType.BUILDING
    if "house" in label:
        return MaterialType.HOUSE
    if "tree" in label or "forest" in label:
        return MaterialType.TREES
    if "structure" in label or "tower" in label or "bridge" in label:
        return MaterialType.LARGE_STRUCTURE
    return MaterialType.UNKNOWN


def _map_distance_band(label: Union[str, None]) -> DistanceBand:
    """Map VLM distance label to DistanceBand."""
    if not label:
        return DistanceBand.MID
    label = label.lower()
    if "near" in label:
        return DistanceBand.NEAR
    if "far" in label:
        return DistanceBand.FAR
    return DistanceBand.MID


