from agentic_rf_planner.api import rest
from agentic_rf_planner.pipeline.schemas import LatLon
from agentic_rf_planner.rf.ray_tracing import RayPath, WallSegment
from agentic_rf_planner.geo import osm_map_provider as osm_mod


def test_strict_validator_rejects_path_through_reflector_building(monkeypatch):
    tx = LatLon(lat=37.0, lon=-122.0)
    rx = LatLon(lat=37.0, lon=-121.998)
    # Rectangle directly between TX and RX.
    building = {
        "id": 1,
        "geometry": [
            {"lat": 36.9998, "lon": -121.9994},
            {"lat": 37.0002, "lon": -121.9994},
            {"lat": 37.0002, "lon": -121.9990},
            {"lat": 36.9998, "lon": -121.9990},
            {"lat": 36.9998, "lon": -121.9994},
        ],
        "tags": {"building:levels": "6"},
        "material": "concrete",
        "height_m": 18.0,
    }

    class FakeOSM:
        def __init__(self, *args, **kwargs):
            self._cached_buildings = [building]

        def prefetch_all_data(self, center, radius_m):
            return None

        def get_buildings_along_ray(self, start, end):
            return [building] if osm_mod._ray_intersects_polygon(start, end, building["geometry"]) else []

    east_wall = WallSegment(
        a_e=88.7,
        a_n=22.2,
        b_e=88.7,
        b_n=-22.2,
        building_id=1,
        material="concrete",
    )
    bad_bounce = LatLon(lat=37.0, lon=-121.9990)
    bad_path = RayPath(
        kind="reflect",
        points=[tx, bad_bounce, rx],
        rsrp_dbm=-95.0,
        meta={
            "wall1": {
                "a_e": east_wall.a_e,
                "a_n": east_wall.a_n,
                "b_e": east_wall.b_e,
                "b_n": east_wall.b_n,
                "building_id": 1,
                "material": "concrete",
            },
            "distances_m": [100.0, 100.0],
        },
    )

    monkeypatch.setattr(osm_mod, "OSMMapProvider", FakeOSM)
    rt_mod = __import__("agentic_rf_planner.rf.ray_tracing", fromlist=["dummy"])
    monkeypatch.setattr(rt_mod, "extract_wall_segments", lambda buildings, origin: [east_wall])
    monkeypatch.setattr(rt_mod, "compute_single_bounce_paths", lambda *args, **kwargs: [bad_path])
    monkeypatch.setattr(rt_mod, "compute_two_bounce_paths", lambda *args, **kwargs: [])

    req = rest.RaytracePathsRequest(tx_lat=tx.lat, tx_lon=tx.lon, rx_lat=rx.lat, rx_lon=rx.lon, max_bounces=1)
    out = rest._compute_raytrace_paths_response(req)

    assert out["diagnostics"]["raw_paths"] == 1
    assert out["diagnostics"]["accepted_reflections"] == 0
    assert out["diagnostics"]["rejected_invalid_footprint"] == 1
    assert all(p["kind"] != "reflect" for p in out["paths"])
