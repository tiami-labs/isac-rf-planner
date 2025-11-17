"""Persistent disk cache for OSM data to avoid repeated API calls."""

import json
import logging
import hashlib
import os
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta

from ..pipeline.schemas import LatLon

logger = logging.getLogger(__name__)

# Cache directory (in user's home directory to avoid permission issues)
CACHE_DIR = Path.home() / ".rf_planning_cache" / "osm_data"
CACHE_EXPIRY_DAYS = 30  # Cache expires after 30 days


def _get_cache_key(center: LatLon, radius_m: float) -> str:
    """
    Generate a cache key for a region.
    
    Uses a grid-based approach: rounds coordinates to ~100m grid cells
    and radius to nearest 50m to maximize cache hits.
    """
    # Round to ~100m grid (approximately 0.001 degrees at mid-latitudes)
    grid_size_deg = 0.001
    lat_grid = round(center.lat / grid_size_deg) * grid_size_deg
    lon_grid = round(center.lon / grid_size_deg) * grid_size_deg
    
    # Round radius to nearest 50m
    radius_grid = round(radius_m / 50.0) * 50.0
    
    # Create hash of the grid cell
    key_str = f"{lat_grid:.6f}_{lon_grid:.6f}_{radius_grid:.0f}"
    key_hash = hashlib.md5(key_str.encode()).hexdigest()
    
    return key_hash


def _get_cache_path(cache_key: str, data_type: str) -> Path:
    """Get the file path for a cached data type."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{cache_key}_{data_type}.json"


def _is_cache_valid(cache_path: Path) -> bool:
    """Check if cache file exists and is not expired."""
    try:
        if not cache_path.exists():
            logger.debug(f"Cache file does not exist: {cache_path}")
            return False
        
        # Check file age
        file_age = datetime.now() - datetime.fromtimestamp(cache_path.stat().st_mtime)
        if file_age > timedelta(days=CACHE_EXPIRY_DAYS):
            logger.debug(f"Cache expired: {cache_path} (age: {file_age.days} days)")
            return False
        
        logger.debug(f"Cache file is valid: {cache_path} (age: {file_age.total_seconds():.1f} seconds)")
        return True
    except Exception as e:
        logger.warning(f"Error checking cache validity for {cache_path}: {e}")
        return False


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
    
    if not _is_cache_valid(cache_path):
        if cache_path.exists():
            logger.debug(f"Cache miss for {data_type}: file exists but expired or invalid")
        else:
            logger.debug(f"Cache miss for {data_type}: file doesn't exist")
        return None
    
    try:
        with open(cache_path, 'r') as f:
            data = json.load(f)
        
        # Validate cache metadata
        if 'center' not in data or 'radius_m' not in data or 'data' not in data:
            logger.warning(f"Invalid cache format: {cache_path}")
            return None
        
        # Check if cache covers the requested region (with some tolerance)
        cached_center = LatLon(lat=data['center']['lat'], lon=data['center']['lon'])
        cached_radius = data['radius_m']
        
        # Simple distance check (if requested center is within cached region)
        # For now, we require exact match (grid-based)
        logger.info(f"✓ Cache HIT: Loaded {len(data['data'])} {data_type} from cache")
        logger.debug(f"  Cache file: {cache_path}")
        logger.debug(f"  Cached at: {data.get('cached_at', 'unknown')}")
        logger.debug(f"  Cached center: {data.get('center', {})}")
        logger.debug(f"  Cached radius: {data.get('radius_m', 'unknown')}m")
        return data['data']
    
    except Exception as e:
        logger.warning(f"Failed to load cache {cache_path}: {e}")
        return None


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

