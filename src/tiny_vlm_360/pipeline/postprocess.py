"""Post-process and normalize model outputs."""

import json
import logging
import re
from typing import Any, Dict, Union

from .schemas import ObjectInfo, SceneAnalysis

logger = logging.getLogger(__name__)


def normalize_output(text: str) -> Union[SceneAnalysis, Dict[str, Any]]:
    """
    Normalize and parse model output into structured format.

    Args:
        text: Raw text output from model

    Returns:
        SceneAnalysis object or dict
    """
    logger.debug(f"Normalizing output: {text[:200]}...")

    # Try to extract JSON from text
    json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if json_match:
        try:
            json_str = json_match.group(0)
            data = json.loads(json_str)
            return _parse_to_schema(data)
        except json.JSONDecodeError:
            logger.warning("Failed to parse JSON from output")

    # Fallback: try to extract structured info from text
    return _extract_from_text(text)


def _parse_to_schema(data: Dict[str, Any]) -> SceneAnalysis:
    """Parse dictionary to SceneAnalysis schema."""
    # Normalize objects
    objects = []
    if "objects" in data:
        for obj in data["objects"]:
            if isinstance(obj, dict):
                objects.append(
                    ObjectInfo(
                        label=obj.get("label", ""),
                        direction=obj.get("direction"),
                        confidence=obj.get("confidence"),
                    )
                )
            elif isinstance(obj, str):
                objects.append(ObjectInfo(label=obj))

    return SceneAnalysis(
        scene_type=data.get("scene_type", "unknown"),
        objects=objects,
        hazards=data.get("hazards", []),
        summary=data.get("summary", ""),
    )


def _extract_from_text(text: str) -> SceneAnalysis:
    """Extract structured information from free-form text."""
    # Simple heuristics to extract info
    scene_type = "unknown"
    objects = []
    hazards = []
    summary = text[:500]  # Use first 500 chars as summary

    # Try to find scene type
    scene_patterns = [
        r"scene[_\s]type[:\s]+([^\n]+)",
        r"(indoor|outdoor|kitchen|bedroom|street|office)",
    ]
    for pattern in scene_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            scene_type = match.group(1).strip()
            break

    return SceneAnalysis(
        scene_type=scene_type,
        objects=objects,
        hazards=hazards,
        summary=summary,
    )

