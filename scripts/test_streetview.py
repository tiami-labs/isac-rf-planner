#!/usr/bin/env python3
"""Standalone test for Street View fetching.

Usage:
    python scripts/test_streetview.py <lat> <lon> [max_distance_m]
    python scripts/test_streetview.py 37.7749 -122.4194
    python scripts/test_streetview.py 37.7749 -122.4194 50.0
"""

import sys
import logging
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agentic_rf_planner.geo.streetview_provider import create_streetview_provider
from agentic_rf_planner.geo.snapping import snap_to_street

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s"
)
logger = logging.getLogger(__name__)


def test_streetview(lat: float, lon: float, max_distance_m: float = 20.0):
    """Test Street View fetching for a given point."""
    print(f"\n{'='*60}")
    print(f"Testing Street View for: ({lat}, {lon})")
    print(f"Max snap distance: {max_distance_m}m")
    print(f"{'='*60}\n")
    
    # Step 1: Snap to street
    print("Step 1: Snapping to street...")
    snapped = snap_to_street(lat, lon, max_distance_m=max_distance_m)
    if snapped is None:
        print("❌ No street found within 20m")
        return False
    
    print(f"✓ Snapped to street:")
    print(f"  Original: ({lat}, {lon})")
    print(f"  Snapped:  ({snapped.latlon.lat:.6f}, {snapped.latlon.lon:.6f})")
    print(f"  Distance: {snapped.distance_m:.1f}m\n")
    
    # Step 2: Check availability
    print("Step 2: Checking Street View availability...")
    # Use Mapillary (real API) instead of file-based
    import os
    api_key = os.environ.get("MAPILLARY_API_KEY")
    if not api_key:
        print("  ❌ MAPILLARY_API_KEY environment variable not set")
        print("  Set it with: export MAPILLARY_API_KEY='your_key_here'")
        return False
    
    provider = create_streetview_provider("mapillary", api_key=api_key)
    available = provider.check_availability(snapped.latlon.lat, snapped.latlon.lon)
    print(f"  Available: {available}\n")
    
    if not available:
        print("❌ Street View not available at this location")
        return False
    
    # Step 3: Fetch panorama
    print("Step 3: Fetching panorama...")
    try:
        pano = provider.fetch_pano(snapped.latlon.lat, snapped.latlon.lon)
        print(f"✓ Panorama loaded successfully!")
        print(f"  Size: {pano.shape[1]}x{pano.shape[0]} pixels")
        print(f"  Total pixels: {pano.shape[0] * pano.shape[1]:,}")
        print(f"  Memory: ~{pano.nbytes / 1024 / 1024:.1f} MB\n")
        return True
    except Exception as e:
        print(f"❌ Failed to fetch panorama: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    if len(sys.argv) < 3 or len(sys.argv) > 4:
        print("Usage: python scripts/test_streetview.py <lat> <lon> [max_distance_m]")
        print("Example: python scripts/test_streetview.py 37.7749 -122.4194")
        print("Example: python scripts/test_streetview.py 37.7749 -122.4194 50.0")
        sys.exit(1)
    
    try:
        lat = float(sys.argv[1])
        lon = float(sys.argv[2])
        max_distance_m = float(sys.argv[3]) if len(sys.argv) == 4 else 20.0
    except ValueError:
        print("Error: lat, lon, and max_distance_m must be numbers")
        sys.exit(1)
    
    success = test_streetview(lat, lon, max_distance_m)
    sys.exit(0 if success else 1)

