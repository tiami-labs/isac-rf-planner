#!/usr/bin/env python3
"""Test script to fetch and save Street View panorama.

Usage:
    python scripts/test_save_streetview.py <lat> <lon> [output_file]
    python scripts/test_save_streetview.py 37.7749 -122.4194
    python scripts/test_save_streetview.py 37.7749 -122.4194 output.jpg
"""

import sys
import logging
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agentic_rf_planner.geo.streetview_provider import create_streetview_provider
from agentic_rf_planner.geo.snapping import snap_to_street
from PIL import Image
import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s"
)
logger = logging.getLogger(__name__)


def save_streetview(lat: float, lon: float, output_file: str = None, initial_radius_m: float = 50.0, fallback_radius_m: float = 500.0):
    """
    Fetch and save Street View panorama for a given point.
    
    Args:
        lat: Latitude
        lon: Longitude
        output_file: Output file path (optional)
        initial_radius_m: Initial search radius in meters (default: 50m)
        fallback_radius_m: Fallback search radius if initial search fails (default: 500m)
    """
    print(f"\n{'='*60}")
    print(f"Fetching Street View for: ({lat}, {lon})")
    print(f"Search strategy: {initial_radius_m}m → {fallback_radius_m}m fallback")
    print(f"{'='*60}\n")
    
    # Step 1: Snap to street
    print("Step 1: Snapping to street...")
    snapped = snap_to_street(lat, lon, max_distance_m=100.0)
    if snapped is None:
        print("❌ No street found within 100m")
        return False
    
    print(f"✓ Snapped to street:")
    print(f"  Original: ({lat}, {lon})")
    print(f"  Snapped:  ({snapped.latlon.lat:.6f}, {snapped.latlon.lon:.6f})")
    print(f"  Distance: {snapped.distance_m:.1f}m\n")
    
    # Step 2: Check availability with progressive radius
    print("Step 2: Checking Street View availability...")
    import os
    api_key = os.environ.get("MAPILLARY_API_KEY")
    if not api_key:
        print("  ❌ MAPILLARY_API_KEY environment variable not set")
        print("  Set it with: export MAPILLARY_API_KEY='your_key_here'")
        return False
    
    # Try initial radius first
    provider = create_streetview_provider("mapillary", api_key=api_key, radius_m=initial_radius_m)
    available = provider.check_availability(snapped.latlon.lat, snapped.latlon.lon)
    print(f"  Search radius: {initial_radius_m}m")
    print(f"  Available: {available}")
    
    # If not available, try fallback radius
    if not available and fallback_radius_m > initial_radius_m:
        print(f"\n  Not found within {initial_radius_m}m, trying {fallback_radius_m}m...")
        # Cap fallback at 2000m to avoid API errors
        effective_fallback = min(fallback_radius_m, 2000.0)
        if effective_fallback < fallback_radius_m:
            print(f"  Note: Capping fallback radius at 2000m (API limit)")
        provider = create_streetview_provider("mapillary", api_key=api_key, radius_m=effective_fallback)
        available = provider.check_availability(snapped.latlon.lat, snapped.latlon.lon)
        print(f"  Search radius: {effective_fallback}m")
        print(f"  Available: {available}")
    
    print()
    
    if not available:
        print(f"❌ Street View not available within {fallback_radius_m}m of this location")
        return False
    
    # Step 3: Fetch panorama (use the provider with the successful radius)
    print("Step 3: Fetching panorama from Mapillary...")
    try:
        # Re-fetch with the correct radius (in case we switched to fallback)
        if not available:  # This shouldn't happen, but just in case
            effective_fallback = min(fallback_radius_m, 2000.0)
            if effective_fallback > initial_radius_m:
                provider = create_streetview_provider("mapillary", api_key=api_key, radius_m=effective_fallback)
            else:
                provider = create_streetview_provider("mapillary", api_key=api_key, radius_m=initial_radius_m)
        
        # Query to get image location info
        images = provider._query_nearby_images(snapped.latlon.lat, snapped.latlon.lon, limit=1)
        if images:
            image_data = images[0]
            image_id = image_data.get("id", "unknown")
            pano_lat = snapped.latlon.lat
            pano_lon = snapped.latlon.lon
            if "geometry" in image_data and "coordinates" in image_data["geometry"]:
                coords = image_data["geometry"]["coordinates"]
                pano_lon = coords[0]
                pano_lat = coords[1]
            print(f"  Image ID: {image_id}")
            print(f"  Panorama location: ({pano_lat:.6f}, {pano_lon:.6f})")
            # Calculate distance from snapped point
            import math
            R = 6371000  # Earth radius in meters
            dlat = math.radians(pano_lat - snapped.latlon.lat)
            dlon = math.radians(pano_lon - snapped.latlon.lon)
            a = math.sin(dlat/2)**2 + math.cos(math.radians(snapped.latlon.lat)) * math.cos(math.radians(pano_lat)) * math.sin(dlon/2)**2
            c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
            distance = R * c
            print(f"  Distance from snapped point: {distance:.1f}m")
        
        pano = provider.fetch_pano(snapped.latlon.lat, snapped.latlon.lon)
        print(f"✓ Panorama fetched successfully!")
        print(f"  Size: {pano.shape[1]}x{pano.shape[0]} pixels")
        print(f"  Total pixels: {pano.shape[0] * pano.shape[1]:,}")
        print(f"  Memory: ~{pano.nbytes / 1024 / 1024:.1f} MB\n")
    except Exception as e:
        print(f"❌ Failed to fetch panorama: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Step 4: Save to file
    if output_file is None:
        output_file = f"streetview_{lat:.4f}_{lon:.4f}.jpg"
    
    output_path = Path(output_file)
    print(f"Step 4: Saving panorama to {output_path}...")
    try:
        img = Image.fromarray(pano)
        img.save(output_path, format="JPEG", quality=95)
        file_size = output_path.stat().st_size
        print(f"✓ Panorama saved successfully!")
        print(f"  File: {output_path}")
        print(f"  Size: {file_size / 1024:.1f} KB")
        print(f"  Dimensions: {img.width}x{img.height} pixels\n")
        return True
    except Exception as e:
        print(f"❌ Failed to save panorama: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    if len(sys.argv) < 3 or len(sys.argv) > 6:
        print("Usage: python scripts/test_save_streetview.py <lat> <lon> [output_file] [initial_radius_m] [fallback_radius_m]")
        print("Example: python scripts/test_save_streetview.py 37.7749 -122.4194")
        print("Example: python scripts/test_save_streetview.py 37.7749 -122.4194 output.jpg")
        print("Example: python scripts/test_save_streetview.py 37.7749 -122.4194 output.jpg 50.0 500.0")
        sys.exit(1)
    
    try:
        lat = float(sys.argv[1])
        lon = float(sys.argv[2])
        output_file = sys.argv[3] if len(sys.argv) >= 4 else None
        initial_radius_m = float(sys.argv[4]) if len(sys.argv) >= 5 else 50.0
        fallback_radius_m = float(sys.argv[5]) if len(sys.argv) >= 6 else 500.0
    except ValueError:
        print("Error: lat, lon, and radius values must be numbers")
        sys.exit(1)
    
    success = save_streetview(lat, lon, output_file, initial_radius_m, fallback_radius_m)
    sys.exit(0 if success else 1)

