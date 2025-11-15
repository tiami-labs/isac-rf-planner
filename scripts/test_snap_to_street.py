#!/usr/bin/env python3
"""Standalone test script for snap-to-street functionality.
Tests the snapping logic and shows detailed debugging information.
"""

import sys
import logging
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agentic_rf_planner.geo.snapping import snap_to_street

logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)s: %(message)s"
)
logger = logging.getLogger(__name__)


def test_snap(lat: float, lon: float, max_distance_m: float = 20.0):
    """
    Test snapping to street for a given point.
    
    Args:
        lat: Latitude
        lon: Longitude
        max_distance_m: Maximum distance to snap (default 20m)
    """
    print(f"\n{'='*60}")
    print(f"Testing snap-to-street for: ({lat}, {lon})")
    print(f"Max distance: {max_distance_m}m")
    print(f"{'='*60}\n")
    
    try:
        snapped = snap_to_street(lat, lon, max_distance_m=max_distance_m)
        
        if snapped is None:
            print("❌ FAILED: No street found")
            print("\nPossible reasons:")
            print("  1. No roads in OSM within the search radius")
            print("  2. OSM query returned no results")
            print("  3. All roads found were beyond max_distance_m")
            print("\nTry:")
            print(f"  - Increase max_distance_m (current: {max_distance_m}m)")
            print("  - Check if the point is actually near a road in OSM")
            print("  - Check OSM at: https://www.openstreetmap.org/")
            return False
        
        print("✓ SUCCESS: Street found!")
        print(f"\nResults:")
        print(f"  Original point: ({lat:.6f}, {lon:.6f})")
        print(f"  Snapped point:  ({snapped.latlon.lat:.6f}, {snapped.latlon.lon:.6f})")
        print(f"  Distance:       {snapped.distance_m:.2f}m")
        
        if snapped.distance_m > max_distance_m:
            print(f"\n⚠ WARNING: Distance ({snapped.distance_m:.2f}m) exceeds max ({max_distance_m}m)")
        else:
            print(f"\n✓ Distance is within limit ({snapped.distance_m:.2f}m <= {max_distance_m}m)")
        
        return True
        
    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python scripts/test_snap_to_street.py <lat> <lon> [max_distance_m]")
        print("\nExamples:")
        print("  python scripts/test_snap_to_street.py 37.7749 -122.4194")
        print("  python scripts/test_snap_to_street.py 37.7749 -122.4194 50.0")
        print("\nTest points (SF Bay Area):")
        print("  Downtown SF:    37.7749 -122.4194")
        print("  Mission District: 37.7596 -122.4148")
        print("  SOMA:           37.7749 -122.4094")
        sys.exit(1)
    
    try:
        lat = float(sys.argv[1])
        lon = float(sys.argv[2])
        max_distance_m = float(sys.argv[3]) if len(sys.argv) >= 4 else 20.0
    except ValueError:
        print("Error: lat, lon, and max_distance_m must be numbers")
        sys.exit(1)
    
    success = test_snap(lat, lon, max_distance_m)
    sys.exit(0 if success else 1)

