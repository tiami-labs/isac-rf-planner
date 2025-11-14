"""Pydantic schemas for structured output."""

from typing import List, Optional

from pydantic import BaseModel, Field


class ObjectInfo(BaseModel):
    """Information about a detected object."""

    label: str = Field(..., description="Object label/name")
    direction: Optional[str] = Field(None, description="Direction: front, left, right, behind, etc.")
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0, description="Confidence score")


class SceneAnalysis(BaseModel):
    """Complete scene analysis result."""

    scene_type: str = Field(..., description="Type of scene (e.g., 'indoor kitchen', 'outdoor street')")
    objects: List[ObjectInfo] = Field(default_factory=list, description="List of detected objects")
    hazards: List[str] = Field(default_factory=list, description="List of identified hazards")
    summary: str = Field(..., description="Brief summary of the scene")

