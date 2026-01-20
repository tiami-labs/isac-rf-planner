#!/usr/bin/env python3
"""Import overlay cache from downloaded JSON file.

Usage:
  1. In browser, after overlays load, click "Download overlay cache" button
  2. Save the downloaded JSON file
  3. Run: python3 scripts/import_overlay_cache.py <downloaded_file.json> --cache-dir ./cache/google_maps_3d
"""

import argparse
import json
import logging
from pathlib import Path

from overlay_cache import save_overlay_cache

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Import overlay cache from downloaded JSON")
    parser.add_argument("cache_file", help="Path to downloaded overlay_heights_cache.json file")
    parser.add_argument("--cache-dir", default="./cache/google_maps_3d", help="Cache directory to save to")
    
    args = parser.parse_args()
    
    cache_file = Path(args.cache_file)
    if not cache_file.exists():
        logger.error(f"Cache file not found: {cache_file}")
        return 1
    
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            cache_data = json.load(f)
    except Exception as e:
        logger.error(f"Failed to read cache file: {e}")
        return 1
    
    # Validate cache structure
    if cache_data.get("version") != 1:
        logger.error(f"Unsupported cache version: {cache_data.get('version')}")
        return 1
    
    required_fields = ["center_lat", "center_lon", "building_ids", "forest_ids", "building_base_heights", "forest_base_heights"]
    for field in required_fields:
        if field not in cache_data:
            logger.error(f"Missing required field in cache: {field}")
            return 1
    
    # Save to cache directory
    try:
        cache_path = save_overlay_cache(
            cache_dir=args.cache_dir,
            center_lat=cache_data["center_lat"],
            center_lon=cache_data["center_lon"],
            building_ids=cache_data["building_ids"],
            forest_ids=cache_data["forest_ids"],
            building_heights=cache_data["building_base_heights"],
            forest_heights=cache_data["forest_base_heights"],
        )
        logger.info(f"✓ Imported overlay cache to: {cache_path}")
        logger.info(f"  Next time you generate HTML for this location, overlays will load instantly!")
        return 0
    except Exception as e:
        logger.error(f"Failed to save cache: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


