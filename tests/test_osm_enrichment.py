import math

from agentic_rf_planner.geo.google_mesh.osm_enrichment import enrich_profile_set_with_osm
from agentic_rf_planner.geo.google_mesh.profile_types import BearingProfile, RayBlockSegment, RayProfileSet
from agentic_rf_planner.pipeline.schemas import LatLon


def _shift_ll(tx: LatLon, east_m: float, north_m: float) -> LatLon:
    """Small-distance equirectangular shift (sufficient for unit tests)."""
    R = 6371000.0
    dlat = north_m / R
    dlon = east_m / (R * max(1e-12, math.cos(math.radians(tx.lat))))
    return LatLon(lat=tx.lat + math.degrees(dlat), lon=tx.lon + math.degrees(dlon))


class FakeOSM:
    """Minimal OSMMapProvider-like object for enrichment unit tests."""

    def __init__(self, buildings, landuse=None):
        self._cached_buildings = buildings
        self._cached_landuse = landuse or []

    def prefetch_all_data(self, center, radius_m):
        # No-op for unit tests
        return


def test_enrichment_splits_blocked_segment_into_buildings():
    tx = LatLon(lat=37.0, lon=-122.0)

    # Two small buildings separated along the east direction.
    # Building A around ~15m east, Building B around ~55m east.
    def square(center_east_m, size_m=10.0):
        c = _shift_ll(tx, center_east_m, 0.0)
        half = size_m / 2.0
        pts = [
            _shift_ll(c, -half, -half),
            _shift_ll(c, +half, -half),
            _shift_ll(c, +half, +half),
            _shift_ll(c, -half, +half),
            _shift_ll(c, -half, -half),
        ]
        return [{"lat": p.lat, "lon": p.lon} for p in pts]

    buildings = [
        {
            "id": 111,
            "tags": {"building": "industrial", "building:material": "steel"},
            "geometry": square(15.0, size_m=12.0),
        },
        {
            "id": 222,
            "tags": {"building": "commercial", "building:material": "brick"},
            "geometry": square(55.0, size_m=12.0),
        },
    ]

    # One blocked segment from 0..100m along bearing 90 degrees (east).
    prof = RayProfileSet(
        tx_lat=tx.lat,
        tx_lon=tx.lon,
        tx_height_m=10.0,
        rx_height_m=1.5,
        max_range_m=100.0,
        dr_m=5.0,
        dtheta_deg=5.0,
        profiles=[
            BearingProfile(
                bearing_deg=90.0,
                segments=[RayBlockSegment(r0_m=0.0, r1_m=100.0)],
            )
        ],
    )

    out = enrich_profile_set_with_osm(prof, osm=FakeOSM(buildings))

    segs = out.profiles[0].segments
    # Expect at least two distinct building ids to appear after splitting.
    osm_ids = [s.osm_id for s in segs if s.osm_id is not None]
    assert 111 in osm_ids and 222 in osm_ids

    # And materials should not remain "unknown" for those building segments.
    mats_by_id = {s.osm_id: (s.material or "").lower() for s in segs if s.osm_id is not None}
    assert mats_by_id[111] == "metal"  # steel bucketed to metal
    assert mats_by_id[222] == "brick"
