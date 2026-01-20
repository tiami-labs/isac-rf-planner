#!/usr/bin/env python3
"""Overlay cache manager for building/forest base heights.

Caches the expensive clampToHeight results so overlays can load instantly
on subsequent page loads.
"""

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _compute_cache_key(center_lat: float, center_lon: float, building_ids: List[int], forest_ids: List[int]) -> str:
    """Compute a stable cache key from location and feature IDs."""
    # Use location (rounded to ~10m precision) + sorted feature IDs
    lat_key = round(center_lat, 5)  # ~1m precision
    lon_key = round(center_lon, 5)
    ids_str = ",".join(str(x) for x in sorted(building_ids)) + "|" + ",".join(str(x) for x in sorted(forest_ids))
    key_str = f"{lat_key:.5f},{lon_key:.5f}|{len(building_ids)},{len(forest_ids)}|{ids_str}"
    return hashlib.md5(key_str.encode()).hexdigest()[:16]


def get_cache_path(cache_dir: str, center_lat: float, center_lon: float, building_ids: List[int], forest_ids: List[int]) -> Path:
    """Get the cache file path for a given location and feature set."""
    cache_dir_path = Path(cache_dir)
    cache_dir_path.mkdir(parents=True, exist_ok=True)
    cache_key = _compute_cache_key(center_lat, center_lon, building_ids, forest_ids)
    return cache_dir_path / f"overlay_heights_{cache_key}.json"


def load_overlay_cache(
    cache_dir: str,
    center_lat: float,
    center_lon: float,
    building_ids: List[int],
    forest_ids: List[int],
) -> Optional[Dict[str, Any]]:
    """
    Load cached base heights for buildings and forests.
    
    Returns:
        Dict with 'building_heights' and 'forest_heights' lists, or None if cache miss/invalid.
    """
    cache_path = get_cache_path(cache_dir, center_lat, center_lon, building_ids, forest_ids)
    
    if not cache_path.exists():
        return None
    
    try:
        cache_data = json.loads(cache_path.read_text(encoding="utf-8"))
        
        # Validate cache version
        if cache_data.get("version") != 1:
            logger.debug(f"Cache version mismatch: {cache_path}")
            return None
        
        # Validate location (allow small drift)
        if abs(cache_data.get("center_lat", 0) - center_lat) > 0.0001:
            logger.debug(f"Cache location mismatch: {cache_path}")
            return None
        if abs(cache_data.get("center_lon", 0) - center_lon) > 0.0001:
            logger.debug(f"Cache location mismatch: {cache_path}")
            return None
        
        # Validate feature counts
        cached_building_count = cache_data.get("building_count", 0)
        cached_forest_count = cache_data.get("forest_count", 0)
        if cached_building_count != len(building_ids) or cached_forest_count != len(forest_ids):
            logger.debug(f"Cache feature count mismatch: {cache_path}")
            return None
        
        # Validate feature IDs match
        cached_building_ids = set(cache_data.get("building_ids", []))
        cached_forest_ids = set(cache_data.get("forest_ids", []))
        if cached_building_ids != set(building_ids) or cached_forest_ids != set(forest_ids):
            logger.debug(f"Cache feature ID mismatch: {cache_path}")
            return None
        
        building_heights = cache_data.get("building_base_heights", [])
        forest_heights = cache_data.get("forest_base_heights", [])
        
        if len(building_heights) != len(building_ids) or len(forest_heights) != len(forest_ids):
            logger.debug(f"Cache height array length mismatch: {cache_path}")
            return None
        
        logger.info(f"✓ Loaded overlay cache: {len(building_heights)} buildings, {len(forest_heights)} forests")
        return {
            "building_heights": building_heights,
            "forest_heights": forest_heights,
        }
        
    except Exception as e:
        logger.warning(f"Failed to load overlay cache {cache_path}: {e}")
        return None


def save_overlay_cache(
    cache_dir: str,
    center_lat: float,
    center_lon: float,
    building_ids: List[int],
    forest_ids: List[int],
    building_heights: List[float],
    forest_heights: List[float],
) -> Path:
    """
    Save cached base heights for buildings and forests.
    
    Args:
        cache_dir: Directory to save cache files
        center_lat: Center latitude
        center_lon: Center longitude
        building_ids: List of building OSM IDs (must match heights order)
        forest_ids: List of forest OSM IDs (must match heights order)
        building_heights: List of base heights for buildings (meters)
        forest_heights: List of base heights for forests (meters)
    
    Returns:
        Path to saved cache file
    """
    if len(building_heights) != len(building_ids):
        raise ValueError(f"building_heights length ({len(building_heights)}) != building_ids length ({len(building_ids)})")
    if len(forest_heights) != len(forest_ids):
        raise ValueError(f"forest_heights length ({len(forest_heights)}) != forest_ids length ({len(forest_ids)})")
    
    cache_path = get_cache_path(cache_dir, center_lat, center_lon, building_ids, forest_ids)
    
    cache_data = {
        "version": 1,
        "center_lat": center_lat,
        "center_lon": center_lon,
        "building_count": len(building_ids),
        "forest_count": len(forest_ids),
        "building_ids": building_ids,
        "forest_ids": forest_ids,
        "building_base_heights": building_heights,
        "forest_base_heights": forest_heights,
        "timestamp": datetime.now().isoformat(),
    }
    
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache_data, indent=2), encoding="utf-8")
    logger.info(f"✓ Saved overlay cache: {cache_path}")
    
    return cache_path


