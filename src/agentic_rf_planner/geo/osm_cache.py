"""Persistent disk cache for OSM data to avoid repeated API calls."""

import json
import logging
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta

from ..pipeline.schemas import LatLon
from .persistent_cache import OSM_NAMESPACE, haversine_m, is_path_fresh, region_cache_key

logger = logging.getLogger(__name__)

CACHE_DIR = OSM_NAMESPACE.root
CACHE_EXPIRY_DAYS = OSM_NAMESPACE.expiry_days


def _get_cache_key(center: LatLon, radius_m: float) -> str:
    """Generate a cache key for a region (grid-bucketed for reuse)."""
    return region_cache_key(center.lat, center.lon, radius_m)


def _get_cache_path(cache_key: str, data_type: str) -> Path:
    """Get the file path for a cached data type."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{cache_key}_{data_type}.json"


def _is_cache_valid(cache_path: Path) -> bool:
    """Check if cache file exists and is not expired."""
    if not is_path_fresh(cache_path, CACHE_EXPIRY_DAYS):
        if not cache_path.exists():
            logger.debug(f"Cache file does not exist: {cache_path}")
        else:
            logger.debug(f"Cache expired: {cache_path}")
        return False
    logger.debug(f"Cache file is valid: {cache_path}")
    return True


def _distance_m(a: LatLon, b: LatLon) -> float:
    return haversine_m(a.lat, a.lon, b.lat, b.lon)


def _load_cache_payload(cache_path: Path) -> Optional[Dict]:
    try:
        with open(cache_path, 'r') as f:
            data = json.load(f)
        if 'center' not in data or 'radius_m' not in data or 'data' not in data:
            logger.warning(f"Invalid cache format: {cache_path}")
            return None
        return data
    except Exception as e:
        logger.warning(f"Failed to load cache {cache_path}: {e}")
        return None


def load_cached_osm_data(center: LatLon, radius_m: float, data_type: str) -> Optional[List[Dict]]:
    """
    Load cached OSM data for a region.
    
    Args:
        center: Center point of the region
        radius_m: Radius in meters
        data_type: 'buildings' or 'landuse'
    
    Returns:
        Cached data if available and valid, None otherwise
    """
    cache_key = _get_cache_key(center, radius_m)
    cache_path = _get_cache_path(cache_key, data_type)
    
    logger.debug(f"Checking cache for {data_type}: key={cache_key}, path={cache_path}")
    logger.debug(f"  Cache path exists: {cache_path.exists()}")
    
    data = None
    if _is_cache_valid(cache_path):
        data = _load_cache_payload(cache_path)
    else:
        if cache_path.exists():
            logger.debug(f"Cache miss for {data_type}: file exists but expired or invalid")
        else:
            logger.debug(f"Cache miss for {data_type}: file doesn't exist")

    exact_empty_data: Optional[List[Dict]] = None
    if data is not None:
        num_items = len(data['data'])
        logger.info(f"✓ Cache HIT: Loaded {num_items} {data_type} from cache")
        logger.debug(f"  Cache file: {cache_path}")
        logger.debug(f"  Cached at: {data.get('cached_at', 'unknown')}")
        logger.debug(f"  Cached center: {data.get('center', {})}")
        logger.debug(f"  Cached radius: {data.get('radius_m', 'unknown')}m")

        if num_items > 0:
            return data['data']

        logger.warning(f"⚠ WARNING: Cache returned 0 {data_type} - this may indicate:")
        logger.warning(f"  1. Previous query found no {data_type} at this location")
        logger.warning(f"  2. Location has no OSM {data_type} data")
        logger.warning(f"  3. Cache file contains empty result from previous fetch")
        logger.info(f"  Exact cache is empty; checking nearby non-empty {data_type} caches before accepting it")
        exact_empty_data = data['data']

    # Exact grid match missed. Reuse a nearby successful cache if its center is close
    # enough to be useful for this request, then seed the exact cache key for later.
    if not CACHE_DIR.exists():
        return None

    best_payload: Optional[Dict] = None
    best_distance_m: Optional[float] = None
    for candidate_path in CACHE_DIR.glob(f"*_{data_type}.json"):
        if candidate_path == cache_path or not _is_cache_valid(candidate_path):
            continue
        payload = _load_cache_payload(candidate_path)
        if payload is None or not payload.get('data'):
            continue
        try:
            candidate_center = LatLon(
                lat=float(payload['center']['lat']),
                lon=float(payload['center']['lon']),
            )
            candidate_radius = float(payload['radius_m'])
        except Exception:
            continue
        distance_m = _distance_m(center, candidate_center)
        # A nearby cache is reusable only when its covered circle fully contains
        # the requested circle. This prevents a small (for example 2 km) cache
        # from being silently relabelled as a much larger (for example 20 km)
        # planning region.
        if distance_m + float(radius_m) > candidate_radius:
            continue
        if best_distance_m is None or distance_m < best_distance_m:
            best_payload = payload
            best_distance_m = distance_m

    if best_payload is None:
        return exact_empty_data

    logger.info(
        f"✓ Nearby cache HIT: Reusing {len(best_payload['data'])} {data_type} "
        f"from {best_distance_m:.1f}m away"
    )
    save_cached_osm_data(center, radius_m, data_type, best_payload['data'])
    return best_payload['data']


def save_cached_osm_data(center: LatLon, radius_m: float, data_type: str, data: List[Dict]) -> None:
    """
    Save OSM data to cache.
    
    Args:
        center: Center point of the region
        radius_m: Radius in meters
        data_type: 'buildings' or 'landuse'
        data: OSM data to cache
    """
    cache_key = _get_cache_key(center, radius_m)
    cache_path = _get_cache_path(cache_key, data_type)
    
    try:
        cache_data = {
            'center': {'lat': center.lat, 'lon': center.lon},
            'radius_m': radius_m,
            'cached_at': datetime.now().isoformat(),
            'data': data,
        }
        
        with open(cache_path, 'w') as f:
            json.dump(cache_data, f, indent=2)
        
        logger.info(f"Cached {len(data)} {data_type} to: {cache_path}")
    
    except Exception as e:
        logger.warning(f"Failed to save cache {cache_path}: {e}")


def clear_cache(older_than_days: Optional[int] = None) -> int:
    """
    Clear cached OSM data.
    
    Args:
        older_than_days: If provided, only clear files older than this many days.
                        If None, clears all cache.
    
    Returns:
        Number of files deleted
    """
    if not CACHE_DIR.exists():
        return 0
    
    deleted = 0
    cutoff_time = datetime.now() - timedelta(days=older_than_days) if older_than_days else None
    
    for cache_file in CACHE_DIR.glob("*.json"):
        if cutoff_time:
            file_time = datetime.fromtimestamp(cache_file.stat().st_mtime)
            if file_time > cutoff_time:
                continue
        
        try:
            cache_file.unlink()
            deleted += 1
        except Exception as e:
            logger.warning(f"Failed to delete cache file {cache_file}: {e}")
    
    logger.info(f"Cleared {deleted} cache file(s)")
    return deleted


def get_cache_stats() -> Dict[str, any]:
    """Get statistics about the cache."""
    if not CACHE_DIR.exists():
        return {
            'cache_dir': str(CACHE_DIR),
            'exists': False,
            'total_files': 0,
            'total_size_mb': 0.0,
        }
    
    files = list(CACHE_DIR.glob("*.json"))
    total_size = sum(f.stat().st_size for f in files)
    
    return {
        'cache_dir': str(CACHE_DIR),
        'exists': True,
        'total_files': len(files),
        'total_size_mb': total_size / (1024 * 1024),
    }

