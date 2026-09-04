from isac_rf_planner.geo.google_mesh import BearingProfile, RayBlockSegment, RayProfileSet
from isac_rf_planner.pipeline.schemas import LatLon, RFParams
from isac_rf_planner.rf.ray_tracing import (
    MeshProfileIndex,
    WallSegment,
    compute_multi_bounce_paths,
    compute_single_bounce_paths,
    extract_mesh_profile_wall_segments,
)


def _always_clear(p0, p1, exclude_ids):
    return True


def test_single_bounce_path_exists_for_simple_mirror_geometry():
    tx = LatLon(lat=0.0, lon=0.0)
    rx = LatLon(lat=0.0, lon=10.0 / 6371000.0 * 180.0 / 3.141592653589793)
    wall = WallSegment(a_e=0.0, a_n=5.0, b_e=10.0, b_n=5.0, building_id=1, material="concrete")
    rf_params = RFParams(freq_mhz=3500.0, tx_power_dbm=43.0, termination_rsrp_dbm=-140.0)

    paths = compute_single_bounce_paths(
        tx,
        rx,
        [wall],
        max_candidates=8,
        max_return=4,
        rf_params=rf_params,
        is_path_clear_fn=_always_clear,
        rsrp_for_path_fn=lambda tx_ll, bounce_ll, rx_ll, total_d, extra_loss: -(total_d + extra_loss),
    )

    assert paths
    assert paths[0].kind == "reflect"
    assert len(paths[0].points) == 3
    assert paths[0].total_distance_m > 0.0
    assert paths[0].extra_loss_db > 0.0


def test_multi_bounce_search_prunes_when_rsrp_drops_below_threshold():
    tx = LatLon(lat=0.0, lon=0.0)
    rx = LatLon(lat=0.0, lon=10.0 / 6371000.0 * 180.0 / 3.141592653589793)
    walls = [
        WallSegment(a_e=0.0, a_n=5.0, b_e=10.0, b_n=5.0, building_id=1, material="concrete"),
        WallSegment(a_e=0.0, a_n=-5.0, b_e=10.0, b_n=-5.0, building_id=2, material="concrete"),
    ]
    rf_params = RFParams(freq_mhz=3500.0, tx_power_dbm=43.0, termination_rsrp_dbm=-70.0)

    def rsrp_for_path(points_ll, total_d, extra_loss_db):
        bounce_count = max(0, len(points_ll) - 2)
        return -50.0 - 18.0 * bounce_count

    paths = compute_multi_bounce_paths(
        tx,
        rx,
        walls,
        max_bounces=20,
        max_candidates=12,
        max_return=10,
        rf_params=rf_params,
        is_path_clear_fn=_always_clear,
        rsrp_for_path_fn=rsrp_for_path,
        termination_rsrp_dbm=-70.0,
    )

    assert paths
    assert all(p.kind == "reflect" for p in paths)


def test_mesh_profiles_generate_reflector_segments_and_block_interior():
    tx = LatLon(lat=37.0, lon=-122.0)
    profile_set = RayProfileSet(
        tx_lat=tx.lat,
        tx_lon=tx.lon,
        tx_height_m=10.0,
        rx_height_m=1.5,
        max_range_m=200.0,
        dr_m=5.0,
        dtheta_deg=5.0,
        profiles=[
            BearingProfile(
                bearing_deg=0.0,
                segments=[RayBlockSegment(r0_m=20.0, r1_m=30.0, kind="building", material="concrete")],
            ),
            BearingProfile(
                bearing_deg=5.0,
                segments=[RayBlockSegment(r0_m=21.0, r1_m=31.0, kind="building", material="concrete")],
            ),
            BearingProfile(
                bearing_deg=10.0,
                segments=[RayBlockSegment(r0_m=22.0, r1_m=32.0, kind="building", material="concrete")],
            ),
        ],
    )

    mesh_walls = extract_mesh_profile_wall_segments(profile_set, tx)
    assert mesh_walls
    assert any(w.source == "mesh" for w in mesh_walls)

    idx = MeshProfileIndex(profile_set, tx)
    blocked_pt = LatLon(lat=tx.lat + (25.0 / 6371000.0) * 180.0 / 3.141592653589793, lon=tx.lon)
    clear_pt = LatLon(lat=tx.lat + (60.0 / 6371000.0) * 180.0 / 3.141592653589793, lon=tx.lon)

    assert idx.is_segment_clear(tx, clear_pt, skip_start_m=2.0, skip_end_m=2.0, sample_step_m=5.0) is False
    assert idx.is_segment_clear(tx, LatLon(lat=tx.lat + (15.0 / 6371000.0) * 180.0 / 3.141592653589793, lon=tx.lon), skip_start_m=2.0, skip_end_m=2.0, sample_step_m=5.0) is True
