"""OSM semantic enrichment for mesh ray profiles.

The frontend mesh sampling pipeline can provide blocked segments along each
bearing. This module optionally annotates those segments with OSM-derived
semantics (tree vs building vs house) and a material bucket used by the RF
pipeline (wood/concrete/brick/metal/glass/unknown).

Enrichment is performed once at upload time so that planning runs can load
profiles from disk and answer per-ray queries in O(1).
"""

from __future__ import annotations

import math
from typing import Optional

from shapely.geometry import Point

try:
    from shapely.strtree import STRtree
except Exception:  # pragma: no cover
    STRtree = None  # type: ignore

from ...pipeline.schemas import LatLon
from ..osm_map_provider import OSMMapProvider, _extract_building_material
from .profile_types import RayProfileSet


def _project_from_tx(tx: LatLon, bearing_deg: float, distance_m: float) -> LatLon:
    """Project a point from tx along bearing by distance (meters).

    Uses a small-distance spherical approximation (sufficient at <= few km).
    """
    R = 6371000.0
    br = math.radians(bearing_deg)
    lat1 = math.radians(tx.lat)
    lon1 = math.radians(tx.lon)

    d_over_R = distance_m / R
    lat2 = lat1 + d_over_R * math.cos(br)
    lon2 = lon1 + d_over_R * math.sin(br) / max(1e-12, math.cos(lat1))

    return LatLon(lat=math.degrees(lat2), lon=math.degrees(lon2))


def enrich_profile_set_with_osm(
    profile_set: RayProfileSet,
    osm: Optional[OSMMapProvider] = None,
    *,
    prefetch_margin_m: float = 100.0,
) -> RayProfileSet:
    """Annotate missing kind/material fields on segments using OSM polygons.

    Mutates and returns profile_set.

    Strategy:
      - Prefetch buildings/landuse around TX
      - Build spatial indexes (STRtree) for building polygons and forest polygons
      - For each segment with unknown kind/material:
          - sample midpoint and classify by point-in-polygon
    """
    if osm is None:
        osm = OSMMapProvider(cache_radius_m=1000.0)

    tx = LatLon(lat=profile_set.tx_lat, lon=profile_set.tx_lon)
    osm.prefetch_all_data(tx, radius_m=float(profile_set.max_range_m) + float(prefetch_margin_m))

    # Build building index
    buildings = getattr(osm, "buildings", []) or []
    building_polys = [b.get("polygon") for b in buildings if b.get("polygon") is not None]
    building_by_id = {id(b.get("polygon")): b for b in buildings if b.get("polygon") is not None}

    btree = STRtree(building_polys) if (STRtree is not None and building_polys) else None

    # Forest/trees index
    landuse = getattr(osm, "landuse", []) or []
    forest_polys = [lu.get("polygon") for lu in landuse if lu.get("polygon") is not None and str(lu.get("type", "")).lower() == "forest"]
    ftree = STRtree(forest_polys) if (STRtree is not None and forest_polys) else None

    def _classify_point(pt: Point):
        # Buildings
        if btree is not None:
            for poly in btree.query(pt):
                if poly.contains(pt):
                    b = building_by_id.get(id(poly))
                    if b is not None:
                        tags = b.get("tags", {}) or {}
                        btype = str(tags.get("building", "building")).lower()
                        if btype in ("house", "residential", "detached", "bungalow", "terrace", "semi"):
                            kind = "house"
                        elif btype in ("industrial", "warehouse", "factory", "hangar"):
                            kind = "large_structure"
                        else:
                            kind = "building"
                        material = _extract_building_material(b)
                        return kind, material, b.get("id"), tags

        # Forest/trees
        if ftree is not None:
            for poly in ftree.query(pt):
                if poly.contains(pt):
                    return "trees", "wood", None, None

        return "unknown", "unknown", None, None

    for bp in profile_set.profiles:
        bearing = float(bp.bearing_deg)
        for seg in bp.segments:
            kind = (seg.kind or "unknown").lower()
            material = (seg.material or "unknown").lower()

            if kind != "unknown" and material != "unknown":
                continue

            mid_r = 0.5 * (float(seg.r0_m) + float(seg.r1_m))
            mid_ll = _project_from_tx(tx, bearing, mid_r)
            pt = Point(mid_ll.lon, mid_ll.lat)

            k2, m2, osm_id, tags = _classify_point(pt)
            if kind == "unknown":
                seg.kind = k2
            if material == "unknown":
                seg.material = m2
            if seg.osm_id is None:
                seg.osm_id = osm_id
            if seg.osm_tags is None:
                seg.osm_tags = tags

    return profile_set
