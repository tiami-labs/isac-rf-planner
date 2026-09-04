"""Snap user point to nearest street using OpenStreetMap."""

from __future__ import annotations

import logging
import math
from typing import Optional

import requests

from ..pipeline.schemas import LatLon

logger = logging.getLogger(__name__)

# OSM Overpass API endpoint (free, no API key required)
OVERPASS_API_URL = "https://overpass-api.de/api/interpreter"


class SnappedPoint:
    """Result of snapping to street."""

    def __init__(self, latlon: LatLon, distance_m: float):
        self.latlon = latlon
        self.distance_m = distance_m


def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate distance between two points in meters using Haversine formula.

    Args:
        lat1, lon1: First point coordinates
        lat2, lon2: Second point coordinates

    Returns:
        Distance in meters
    """
    R = 6371000  # Earth radius in meters

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return R * c


def _point_to_line_distance(
    point_lat: float, point_lon: float, line_start_lat: float, line_start_lon: float, line_end_lat: float, line_end_lon: float
) -> tuple[float, float, float]:
    """
    Calculate distance from point to line segment and find nearest point on line.

    Returns:
        (distance_m, nearest_lat, nearest_lon)
    """
    # Convert to radians
    p_lat = math.radians(point_lat)
    p_lon = math.radians(point_lon)
    a_lat = math.radians(line_start_lat)
    a_lon = math.radians(line_start_lon)
    b_lat = math.radians(line_end_lat)
    b_lon = math.radians(line_end_lon)

    # Simple approximation for small distances (good enough for street snapping)
    # For more accuracy, use proper geodesic calculations
    R = 6371000  # Earth radius in meters

    # Vector from A to B
    dx = (b_lon - a_lon) * R * math.cos((a_lat + b_lat) / 2)
    dy = (b_lat - a_lat) * R

    # Vector from A to P
    px = (p_lon - a_lon) * R * math.cos((a_lat + p_lat) / 2)
    py = (p_lat - a_lat) * R

    # Project P onto AB
    ab_sq = dx * dx + dy * dy
    if ab_sq < 1e-6:  # A and B are the same point
        dist = math.sqrt(px * px + py * py)
        return dist, line_start_lat, line_start_lon

    t = max(0, min(1, (px * dx + py * dy) / ab_sq))

    # Nearest point on line segment
    nearest_x = a_lon + t * (b_lon - a_lon)
    nearest_y = a_lat + t * (b_lat - a_lat)

    nearest_lat = math.degrees(nearest_y)
    nearest_lon = math.degrees(nearest_x)

    # Distance from point to nearest point on line
    dist = _haversine_distance(point_lat, point_lon, nearest_lat, nearest_lon)

    return dist, nearest_lat, nearest_lon


def snap_to_street(lat: float, lon: float, max_distance_m: float = 20.0) -> Optional[SnappedPoint]:
    """
    Snap (lat, lon) to nearest street point using OpenStreetMap.

    Args:
        lat: Latitude
        lon: Longitude
        max_distance_m: Maximum distance to snap (default 20m)

    Returns:
        SnappedPoint with snapped coordinates and distance, or None if no street found
    """
    # Calculate bounding box around point (roughly max_distance_m radius)
    # 1 degree latitude ≈ 111km, so for 20m we need ~0.00018 degrees
    # Add generous buffer for the query to ensure we catch nearby roads
    buffer_deg = max_distance_m / 111000.0 * 3  # 3x buffer for better coverage

    bbox = f"{lat - buffer_deg},{lon - buffer_deg},{lat + buffer_deg},{lon + buffer_deg}"

    # Overpass QL query: get ALL roads/streets in bounding box
    # Query for ANY way with highway tag (includes all road types: residential, primary, track, etc.)
    # This gives Google Maps-level coverage of all roads in OSM
    query = f"""
    [out:json][timeout:10];
    (
      way["highway"]({bbox});
    );
    out geom;
    """

    try:
        logger.debug(f"Querying OSM Overpass API for roads near ({lat}, {lon})")
        logger.debug(f"Bounding box: {bbox}")
        logger.debug(f"Query: {query.strip()}")
        
        response = requests.post(OVERPASS_API_URL, data={"data": query}, timeout=15)
        response.raise_for_status()
        data = response.json()

        if "elements" not in data:
            logger.error(f"Invalid response from OSM API: missing 'elements' key")
            logger.debug(f"Response keys: {list(data.keys())}")
            return None
            
        num_elements = len(data["elements"])
        logger.debug(f"OSM API returned {num_elements} elements")
        
        if num_elements == 0:
            logger.warning(f"No roads found near ({lat}, {lon}) in bounding box {bbox}")
            logger.debug("This could mean:")
            logger.debug("  1. No roads exist in OSM at this location")
            logger.debug("  2. Bounding box is too small")
            logger.debug("  3. OSM data is incomplete for this area")
            return None

        # Find nearest point on any road segment
        best_distance = float("inf")
        best_lat = lat
        best_lon = lon
        elements_processed = 0
        segments_checked = 0

        for element in data["elements"]:
            if "geometry" not in element:
                logger.debug(f"Skipping element {element.get('id', 'unknown')}: no geometry")
                continue

            geometry = element["geometry"]
            if len(geometry) < 2:
                logger.debug(f"Skipping element {element.get('id', 'unknown')}: geometry has < 2 points")
                continue

            elements_processed += 1
            highway_type = element.get("tags", {}).get("highway", "unknown")
            logger.debug(f"Processing element {element.get('id', 'unknown')} (highway={highway_type}, {len(geometry)} points)")

            # Check each segment of the road
            for i in range(len(geometry) - 1):
                seg_start = geometry[i]
                seg_end = geometry[i + 1]
                
                if "lat" not in seg_start or "lon" not in seg_start:
                    continue
                if "lat" not in seg_end or "lon" not in seg_end:
                    continue

                dist, snap_lat, snap_lon = _point_to_line_distance(
                    lat,
                    lon,
                    seg_start["lat"],
                    seg_start["lon"],
                    seg_end["lat"],
                    seg_end["lon"],
                )

                segments_checked += 1
                if dist < best_distance:
                    best_distance = dist
                    best_lat = snap_lat
                    best_lon = snap_lon
                    logger.debug(f"  New best: {best_distance:.2f}m at ({best_lat:.6f}, {best_lon:.6f})")

        logger.debug(f"Processed {elements_processed} elements, checked {segments_checked} segments")
        
        if best_distance == float("inf"):
            logger.warning("No valid road segments found (all geometries were invalid)")
            return None

        if best_distance > max_distance_m:
            logger.warning(
                f"Nearest street is {best_distance:.1f}m away (max: {max_distance_m}m)"
            )
            logger.debug(f"Consider increasing max_distance_m or checking if point is actually near a road")
            return None

        logger.info(
            f"Snapped ({lat}, {lon}) to street at ({best_lat:.6f}, {best_lon:.6f}), "
            f"distance: {best_distance:.1f}m"
        )

        return SnappedPoint(LatLon(lat=best_lat, lon=best_lon), distance_m=best_distance)

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to query OSM Overpass API: {e}")
        # Fallback to stub behavior
        logger.warning("Falling back to stub implementation (no snapping)")
        return SnappedPoint(LatLon(lat=lat, lon=lon), distance_m=0.0)
    except Exception as e:
        logger.error(f"Error in snap_to_street: {e}")
        # Fallback to stub behavior
        logger.warning("Falling back to stub implementation (no snapping)")
        return SnappedPoint(LatLon(lat=lat, lon=lon), distance_m=0.0)


