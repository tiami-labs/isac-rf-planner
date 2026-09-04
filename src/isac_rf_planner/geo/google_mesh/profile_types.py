"""Types for persisted Google-mesh ray profiles.

These types capture ONLY ray propagation / geometry intersection results plus
OSM-derived semantics ("what" the obstruction is). RF math stays in
`isac_rf_planner/rf`.

A RayProfileSet is keyed by:
  - TX lat/lon (quantized)
  - tx_height_m / rx_height_m (quantized)
  - max_range_m, dr_m, dtheta_deg
  - PROFILE_VERSION

The backend MapProvider uses these profiles to answer:
  - get_buildings_along_ray(tx, end)
  - is_forest_between(tx, end)
  - count_buildings_between(tx, end)

without recomputing mesh intersections.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field


PROFILE_VERSION = "mesh_profile_v1"


class RayBlockSegment(BaseModel):
    """One contiguous blocked interval along a bearing."""

    r0_m: float = Field(..., ge=0.0, description="Segment start distance (m) from TX")
    r1_m: float = Field(..., ge=0.0, description="Segment end distance (m) from TX")

    # Semantics (from OSM classification). These drive penetration loss selection.
    kind: str = Field("unknown", description="building|house|large_structure|trees|unknown")
    material: str = Field("unknown", description="material bucket used by rf/material_penetration.py")

    # Optional provenance/debug fields
    osm_id: Optional[int] = None
    osm_tags: Optional[Dict[str, Any]] = None


class BearingProfile(BaseModel):
    """All blocked segments for a single bearing."""

    bearing_deg: float = Field(..., ge=0.0, lt=360.0)
    segments: List[RayBlockSegment] = Field(default_factory=list)
    terrain_heights: Optional[List[Tuple[float, float]]] = Field(
        default=None,
        description="(range_m, height_m_amsl) DSM samples from Google mesh surface, sorted by range_m",
    )


class RayProfileSet(BaseModel):
    """Profiles for all bearings for a TX/config."""

    tx_lat: float
    tx_lon: float
    tx_height_m: float = 0.0
    rx_height_m: float = 1.5

    max_range_m: float
    dr_m: float
    dtheta_deg: float

    profiles: List[BearingProfile] = Field(default_factory=list)
    version: str = PROFILE_VERSION

    created_at_ms: Optional[int] = None
