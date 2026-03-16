from agentic_rf_planner.pipeline.schemas import LatLon
from agentic_rf_planner.geo.ray_trace.engine import OSMRayTraceEngine
from agentic_rf_planner.geo.ray_trace.provider import RayTraceOSMMapProvider


def _building(bid, pts, material='concrete', height=20.0):
    return {
        'id': bid,
        'tags': {'building': 'yes', 'building:material': material},
        'geometry': [{'lat': lat, 'lon': lon} for lat, lon in pts],
        'material': material,
        'height_m': height,
    }


def test_direct_path_blocked_and_reflection_found():
    tx = LatLon(lat=37.0, lon=-122.0)
    rx = LatLon(lat=37.0, lon=-121.999)
    # Vertical wall rectangle crossing the direct path, with a second nearby wall for a reflection candidate
    b1 = _building(1, [
        (36.99995, -121.99965),
        (37.00005, -121.99965),
        (37.00005, -121.99955),
        (36.99995, -121.99955),
    ], 'concrete')
    b2 = _building(2, [
        (37.00025, -121.99950),
        (37.00035, -121.99950),
        (37.00035, -121.99940),
        (37.00025, -121.99940),
    ], 'glass')
    engine = OSMRayTraceEngine(tx_height_m=10.0, rx_height_m=1.5)
    out = engine.trace(tx, rx, [b1, b2], max_reflections=1)
    assert out.paths[0].path_type == 'direct'
    assert out.paths[0].blocked is True
    assert any(p.path_type == 'reflection' for p in out.paths)


def test_provider_preview_uses_cached_osm_geometry():
    tx = LatLon(lat=37.0, lon=-122.0)
    p = RayTraceOSMMapProvider(tx_height_m=10.0, rx_height_m=1.5)
    p.osm._prefetched = True
    p.osm._cached_buildings = [
        _building(1, [
            (36.99995, -121.99965),
            (37.00005, -121.99965),
            (37.00005, -121.99955),
            (36.99995, -121.99955),
        ])
    ]
    traces = p.radial_trace_preview(tx, max_range_m=100.0, num_bearings=8, max_reflections=1)
    assert len(traces) == 8
    assert all(t.paths for t in traces)
