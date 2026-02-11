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

from shapely.geometry import Point, Polygon

try:
    from shapely.strtree import STRtree
except Exception:  # pragma: no cover
    STRtree = None  # type: ignore

from ...pipeline.schemas import LatLon
from ..osm_map_provider import OSMMapProvider, _extract_building_material
from .profile_types import RayBlockSegment, RayProfileSet


def _geometry_to_polygon(geom: object) -> Optional[Polygon]:
    """Convert an OSMMapProvider geometry array to a Shapely Polygon.

    OSMMapProvider stores polygon rings as a list of nodes like:
        [{"lat": ..., "lon": ...}, ...]

    This helper returns None when geometry is missing/invalid.
    """
    if not isinstance(geom, list) or len(geom) < 3:
        return None

    coords = []
    for node in geom:
        if isinstance(node, dict) and "lat" in node and "lon" in node:
            try:
                coords.append((float(node["lon"]), float(node["lat"])))
            except Exception:
                continue
        elif isinstance(node, (list, tuple)) and len(node) >= 2:
            try:
                coords.append((float(node[0]), float(node[1])))
            except Exception:
                continue

    if len(coords) < 3:
        return None

    try:
        poly = Polygon(coords)
        # Attempt to repair minor self-intersections.
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty:
            return None
        return poly
    except Exception:
        return None


def _is_forest_tags(tags: object) -> bool:
    if not isinstance(tags, dict):
        return False
    landuse = str(tags.get("landuse", "")).lower()
    natural = str(tags.get("natural", "")).lower()
    return landuse in ("forest", "wood", "meadow") or natural in ("wood", "forest", "tree_row")


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

    # NOTE: OSMMapProvider stores prefetched data in _cached_buildings/_cached_landuse.
    # Older versions of this module expected public attributes (buildings/landuse)
    # with Shapely polygons; that mismatch caused enrichment to be a no-op.
    buildings_raw = getattr(osm, "_cached_buildings", None)
    if buildings_raw is None:
        buildings_raw = getattr(osm, "buildings", None)
    buildings_raw = buildings_raw or []

    landuse_raw = getattr(osm, "_cached_landuse", None)
    if landuse_raw is None:
        landuse_raw = getattr(osm, "landuse", None)
    landuse_raw = landuse_raw or []

    # Build building index (Polygon -> OSM dict)
    building_entries = []
    for b in buildings_raw:
        if not isinstance(b, dict):
            continue
        poly = b.get("polygon")
        if poly is None:
            poly = _geometry_to_polygon(b.get("geometry"))
        if poly is None:
            continue
        building_entries.append((poly, b))

    building_polys = [p for p, _ in building_entries]
    building_by_poly_id = {id(p): b for p, b in building_entries}
    btree = STRtree(building_polys) if (STRtree is not None and building_polys) else None

    # Forest/trees index (use OSM tags)
    forest_entries = []
    for lu in landuse_raw:
        if not isinstance(lu, dict):
            continue
        if not _is_forest_tags(lu.get("tags")):
            continue
        poly = lu.get("polygon")
        if poly is None:
            poly = _geometry_to_polygon(lu.get("geometry"))
        if poly is None:
            continue
        forest_entries.append(poly)

    forest_polys = forest_entries
    ftree = STRtree(forest_polys) if (STRtree is not None and forest_polys) else None

    def _classify_point(pt: Point):
        # Buildings
        if btree is not None:
            for poly in btree.query(pt):
                if poly.contains(pt):
                    b = building_by_poly_id.get(id(poly))
                    if b is not None:
                        tags = b.get("tags", {}) or {}
                        btype = str(tags.get("building", "building")).lower()
                        if btype in ("house", "residential", "detached", "bungalow", "terrace", "semi"):
                            kind = "house"
                        elif btype in ("industrial", "warehouse", "factory", "hangar"):
                            kind = "large_structure"
                        else:
                            kind = "building"
                        # Ensure material is present even if the upstream fetcher didn't.
                        material = str(b.get("material") or "").lower() or _extract_building_material(b)
                        return kind, material, b.get("id"), tags

        # Forest/trees
        if ftree is not None:
            for poly in ftree.query(pt):
                if poly.contains(pt):
                    return "trees", "wood", None, None

        return "unknown", "unknown", None, None

    # Split segments using point classification along the blocked interval.
    # This helps approximate progressive attenuation (building A then building B).
    step_m = float(profile_set.dr_m) / 2.0 if float(profile_set.dr_m) > 0 else 2.5
    step_m = max(1.0, min(5.0, step_m))

    for bp in profile_set.profiles:
        bearing = float(bp.bearing_deg)
        new_segments: list[RayBlockSegment] = []

        for seg in bp.segments:
            kind0 = (seg.kind or "unknown").lower()
            mat0 = (seg.material or "unknown").lower()

            # If this segment already has a stable OSM id, just fill missing fields.
            if seg.osm_id is not None or (kind0 != "unknown" and mat0 != "unknown"):
                if kind0 == "unknown" or mat0 == "unknown" or seg.osm_tags is None:
                    mid_r = 0.5 * (float(seg.r0_m) + float(seg.r1_m))
                    mid_ll = _project_from_tx(tx, bearing, mid_r)
                    pt = Point(mid_ll.lon, mid_ll.lat)
                    k2, m2, osm_id, tags = _classify_point(pt)
                    if kind0 == "unknown":
                        seg.kind = k2
                    if mat0 == "unknown":
                        seg.material = m2
                    if seg.osm_id is None:
                        seg.osm_id = osm_id
                    if seg.osm_tags is None:
                        seg.osm_tags = tags
                new_segments.append(seg)
                continue

            # Otherwise, split into smaller chunks and classify each chunk.
            r0 = float(seg.r0_m)
            r1 = float(seg.r1_m)
            if r1 <= r0 + 1e-6:
                new_segments.append(seg)
                continue

            chunks = []
            r = r0
            while r < r1 - 1e-6:
                r_next = min(r1, r + step_m)
                mid_r = 0.5 * (r + r_next)
                mid_ll = _project_from_tx(tx, bearing, mid_r)
                pt = Point(mid_ll.lon, mid_ll.lat)
                k2, m2, osm_id, tags = _classify_point(pt)
                chunks.append((r, r_next, k2, m2, osm_id, tags))
                r = r_next

            # Merge adjacent chunks with identical semantics.
            merged: list[dict] = []
            for cr0, cr1, k2, m2, osm_id, tags in chunks:
                if not merged:
                    merged.append(
                        {
                            "r0_m": cr0,
                            "r1_m": cr1,
                            "kind": k2,
                            "material": m2,
                            "osm_id": osm_id,
                            "osm_tags": tags,
                        }
                    )
                    continue

                last = merged[-1]
                if (
                    last.get("kind") == k2
                    and last.get("material") == m2
                    and last.get("osm_id") == osm_id
                    and abs(float(last.get("r1_m", 0.0)) - cr0) <= 1e-6
                ):
                    last["r1_m"] = cr1
                else:
                    merged.append(
                        {
                            "r0_m": cr0,
                            "r1_m": cr1,
                            "kind": k2,
                            "material": m2,
                            "osm_id": osm_id,
                            "osm_tags": tags,
                        }
                    )

            for m in merged:
                new_segments.append(RayBlockSegment(**m))

        # Keep deterministic ordering.
        bp.segments = sorted(new_segments, key=lambda s: (float(s.r0_m), float(s.r1_m)))

    return profile_set
