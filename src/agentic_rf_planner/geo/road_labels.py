"""Fetch and rank named road labels for the 3D Cesium UI."""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from ..geo.osm_cache import load_cached_osm_data, save_cached_osm_data
from ..pipeline.schemas import LatLon


logger = logging.getLogger(__name__)

# OSM Overpass API endpoints (tried in order)
OVERPASS_API_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

OVERPASS_HEADERS = {
    "User-Agent": "agentic-rf-planner/1.0 (+local)",
    "Accept": "application/json, text/plain, */*",
}


def _post_overpass(query: str, *, timeout: int, context: str) -> dict:
    last_exc: Optional[Exception] = None
    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        for url in OVERPASS_API_URLS:
            try:
                response = requests.post(
                    url,
                    data={"data": query},
                    headers=OVERPASS_HEADERS,
                    timeout=timeout,
                )
                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as exc:
                logger.warning(
                    "%s failed via %s (attempt %s/%s): %s",
                    context,
                    url,
                    attempt,
                    max_attempts,
                    exc,
                )
                last_exc = exc
        if attempt < max_attempts:
            time.sleep(0.6)
    if last_exc:
        raise last_exc
    raise RuntimeError(f"{context} failed without an exception")

MAJOR_HIGHWAY_TYPES = {
    "motorway",
    "motorway_link",
    "trunk",
    "trunk_link",
    "primary",
    "primary_link",
    "secondary",
    "secondary_link",
    "tertiary",
    "tertiary_link",
}

HIGHWAY_PRIORITY = {
    "motorway": 0,
    "motorway_link": 1,
    "trunk": 2,
    "trunk_link": 3,
    "primary": 4,
    "primary_link": 5,
    "secondary": 6,
    "secondary_link": 7,
    "tertiary": 8,
    "tertiary_link": 9,
    "residential": 10,
    "unclassified": 11,
    "living_street": 12,
    "service": 13,
    "road": 14,
}


def classify_road_importance(highway: Optional[str]) -> str:
    """Classify an OSM road class into a major/minor label bucket."""
    if (highway or "").strip().lower() in MAJOR_HIGHWAY_TYPES:
        return "major"
    return "minor"


def _normalize_name(name: Any) -> str:
    return " ".join(str(name or "").split()).strip()


def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two points in meters using Haversine formula."""
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def polyline_length_m(geometry: List[Dict[str, Any]]) -> float:
    """Compute total polyline length in meters for an OSM way geometry."""
    if len(geometry) < 2:
        return 0.0

    total = 0.0
    for i in range(len(geometry) - 1):
        a = geometry[i]
        b = geometry[i + 1]
        if not all(k in a for k in ("lat", "lon")) or not all(k in b for k in ("lat", "lon")):
            continue
        total += _haversine_distance(a["lat"], a["lon"], b["lat"], b["lon"])
    return total


def polyline_anchor_point(geometry: List[Dict[str, Any]]) -> Optional[Tuple[float, float]]:
    """Pick a stable label anchor at the midpoint of the road geometry."""
    if not geometry:
        return None

    if len(geometry) == 1:
        pt = geometry[0]
        if "lat" in pt and "lon" in pt:
            return float(pt["lat"]), float(pt["lon"])
        return None

    total_len = polyline_length_m(geometry)
    if total_len <= 0.0:
        pt = geometry[len(geometry) // 2]
        if "lat" in pt and "lon" in pt:
            return float(pt["lat"]), float(pt["lon"])
        return None

    target = total_len / 2.0
    walked = 0.0
    for i in range(len(geometry) - 1):
        a = geometry[i]
        b = geometry[i + 1]
        if not all(k in a for k in ("lat", "lon")) or not all(k in b for k in ("lat", "lon")):
            continue
        seg_len = _haversine_distance(a["lat"], a["lon"], b["lat"], b["lon"])
        if seg_len <= 0.0:
            continue
        if walked + seg_len >= target:
            t = (target - walked) / seg_len
            lat = float(a["lat"]) + (float(b["lat"]) - float(a["lat"])) * t
            lon = float(a["lon"]) + (float(b["lon"]) - float(a["lon"])) * t
            return lat, lon
        walked += seg_len

    pt = geometry[-1]
    if "lat" in pt and "lon" in pt:
        return float(pt["lat"]), float(pt["lon"])
    return None


def _point_along_polyline(
    geometry: List[Dict[str, Any]], target_m: float
) -> Optional[Tuple[float, float]]:
    """Return a point at a target distance along the polyline."""
    if not geometry:
        return None
    if len(geometry) == 1:
        pt = geometry[0]
        if "lat" in pt and "lon" in pt:
            return float(pt["lat"]), float(pt["lon"])
        return None

    walked = 0.0
    for i in range(len(geometry) - 1):
        a = geometry[i]
        b = geometry[i + 1]
        if not all(k in a for k in ("lat", "lon")) or not all(k in b for k in ("lat", "lon")):
            continue
        seg_len = _haversine_distance(a["lat"], a["lon"], b["lat"], b["lon"])
        if seg_len <= 0.0:
            continue
        if walked + seg_len >= target_m:
            t = max(0.0, min(1.0, (target_m - walked) / seg_len))
            lat = float(a["lat"]) + (float(b["lat"]) - float(a["lat"])) * t
            lon = float(a["lon"]) + (float(b["lon"]) - float(a["lon"])) * t
            return lat, lon
        walked += seg_len

    return polyline_anchor_point(geometry)


def polyline_anchor_points(
    geometry: List[Dict[str, Any]], length_m: float, importance: str
) -> List[Tuple[float, float]]:
    """Generate one or more anchor points for long roads."""
    if length_m <= 0.0:
        anchor = polyline_anchor_point(geometry)
        return [anchor] if anchor is not None else []

    spacing_m = 700.0 if importance == "major" else 350.0
    if length_m < spacing_m * 1.25:
        anchor = polyline_anchor_point(geometry)
        return [anchor] if anchor is not None else []

    target_count = max(1, min(4 if importance == "major" else 3, int(round(length_m / spacing_m))))
    anchors: List[Tuple[float, float]] = []
    for idx in range(target_count):
        fraction = (idx + 1) / (target_count + 1)
        anchor = _point_along_polyline(geometry, length_m * fraction)
        if anchor is not None:
            anchors.append(anchor)

    if not anchors:
        anchor = polyline_anchor_point(geometry)
        return [anchor] if anchor is not None else []
    return anchors


def build_road_label_candidates(elements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert raw Overpass way elements into label candidates."""
    candidates: List[Dict[str, Any]] = []
    for element in elements:
        tags = element.get("tags") or {}
        geometry = element.get("geometry") or []
        highway = str(tags.get("highway") or "").strip().lower()
        name = _normalize_name(tags.get("name"))
        if not name or not highway or not geometry:
            continue

        length_m = polyline_length_m(geometry)
        importance = classify_road_importance(highway)
        anchor = polyline_anchor_point(geometry)
        if anchor is None:
            continue
        candidates.append(
            {
                "name": name,
                "highway": highway,
                "importance": importance,
                "anchor_lat": float(anchor[0]),
                "anchor_lon": float(anchor[1]),
                "length_m": float(length_m),
                "priority": HIGHWAY_PRIORITY.get(highway, 999),
                "anchor_index": 0,
            }
        )
    return candidates


def rank_road_labels(
    candidates: List[Dict[str, Any]],
    *,
    major_limit: int = 24,
    minor_limit: int = 40,
) -> List[Dict[str, Any]]:
    """De-duplicate and rank label candidates for frontend display."""
    buckets = {"major": [], "minor": []}
    for item in candidates:
        buckets[item["importance"]].append(item)

    for items in buckets.values():
        items.sort(
            key=lambda item: (
                item["priority"],
                -item["length_m"],
                item["name"],
                item.get("anchor_index", 0),
            )
        )

    out: List[Dict[str, Any]] = []
    accepted_names: set[str] = set()

    for bucket_name, limit in (("major", major_limit), ("minor", minor_limit)):
        accepted: List[Dict[str, Any]] = []
        for item in buckets[bucket_name]:
            folded = item["name"].casefold()
            if folded in accepted_names:
                continue
            accepted.append(item)
            accepted_names.add(folded)
            if len(accepted) >= max(0, int(limit)):
                break
        out.extend(accepted)
    return out


def fetch_road_labels(
    lat: float,
    lon: float,
    *,
    radius_m: float = 2500.0,
    major_limit: int = 24,
    minor_limit: int = 40,
) -> List[Dict[str, Any]]:
    """Fetch named OSM roads around a point and return ranked label anchors."""
    radius_m = max(200.0, min(5000.0, float(radius_m)))
    center = LatLon(lat=lat, lon=lon)
    cache_type = "road_label_ways"
    elements = load_cached_osm_data(center, radius_m, cache_type)
    if elements is not None:
        candidates = build_road_label_candidates(elements)
        return rank_road_labels(
            candidates,
            major_limit=major_limit,
            minor_limit=minor_limit,
        )

    query = f"""
    [out:json][timeout:15];
    (
      way["highway"]["name"](around:{radius_m:.0f},{lat:.7f},{lon:.7f});
    );
    out tags geom;
    """

    try:
        data = _post_overpass(query, timeout=20, context="road label fetch")
        elements = data.get("elements") or []
        save_cached_osm_data(center, radius_m, cache_type, elements)
        candidates = build_road_label_candidates(elements)
        return rank_road_labels(
            candidates,
            major_limit=major_limit,
            minor_limit=minor_limit,
        )
    except requests.exceptions.RequestException as exc:
        logger.warning(
            "Failed to fetch road labels from Overpass endpoints %s: %s",
            OVERPASS_API_URLS,
            exc,
        )
        return []
    except Exception as exc:  # pragma: no cover - defensive fallback
        logger.warning("Failed to build road labels: %s", exc)
        return []
