from __future__ import annotations

from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class RayPoint(BaseModel):
    lat: float
    lon: float
    height_m: float = 0.0


class RayPath(BaseModel):
    path_type: str = Field(..., description='direct|reflection')
    points: List[RayPoint]
    total_length_m: float
    path_gain_db: float = 0.0
    blocked: bool = False
    metadata: Optional[Dict[str, Any]] = None


class RayTraceResult(BaseModel):
    tx: RayPoint
    rx: RayPoint
    paths: List[RayPath] = Field(default_factory=list)
    engine: str = 'osm_single_bounce_v1'
