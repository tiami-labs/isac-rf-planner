"""Targeted tests for the vendor-grade RF roadmap pieces."""

from agentic_rf_planner.geo.coverage_grid import build_coverage_grid
from agentic_rf_planner.geo.physical_spanning import StubMapProvider
from agentic_rf_planner.pipeline.schemas import AttenuationGrid, LatLon, RFParams, WorldCell, WorldModel
from agentic_rf_planner.rf.attenuation_models import (
    _horizontal_pattern_attenuation_db,
    _scenario_path_loss_db,
    compute_attenuation_grid,
)


def test_3gpp_umi_nlos_exceeds_los():
    rf_params = RFParams(
        freq_mhz=3500.0,
        tx_power_dbm=43.0,
        path_loss_model="3gpp_38901",
        propagation_scenario="umi_street_canyon",
        tx_height_m=10.0,
        rx_height_m=1.5,
    )

    los = _scenario_path_loss_db(
        distance_2d_m=200.0,
        distance_3d_m=200.2,
        freq_mhz=3500.0,
        tx_height_m=10.0,
        rx_height_m=1.5,
        is_los=True,
        rf_params=rf_params,
    )
    nlos = _scenario_path_loss_db(
        distance_2d_m=200.0,
        distance_3d_m=200.2,
        freq_mhz=3500.0,
        tx_height_m=10.0,
        rx_height_m=1.5,
        is_los=False,
        rf_params=rf_params,
    )

    assert nlos > los


def test_horizontal_pattern_penalizes_off_boresight():
    rf_params = RFParams(freq_mhz=3500.0, tx_power_dbm=43.0)
    sector = {
        "azimuth_deg": 0.0,
        "beamwidth_h_deg": 65.0,
        "max_horizontal_attenuation_db": 30.0,
        "front_to_back_attenuation_db": 25.0,
    }

    on_axis = _horizontal_pattern_attenuation_db(0.0, sector, rf_params)
    side = _horizontal_pattern_attenuation_db(60.0, sector, rf_params)
    back = _horizontal_pattern_attenuation_db(180.0, sector, rf_params)

    assert on_axis == 0.0
    assert side > on_axis
    assert back >= 25.0


def test_same_location_sector_aggregation_becomes_interference_aware():
    rf_params = RFParams(
        freq_mhz=3500.0,
        tx_power_dbm=43.0,
        noise_floor_dbm=-110.0,
        path_loss_model="legacy",
        channel_bandwidth_mhz=20.0,
        tx_height_m=10.0,
        rx_height_m=1.5,
    )
    world = WorldModel(
        tx=LatLon(lat=0.0, lon=0.0),
        rf_params=rf_params,
        cells=[
            WorldCell(
                lat=1.0,
                lon=1.0,
                distance_m=150.0,
                bearing_deg=0.0,
                dominant_material="unknown",
                obstacles_count=0,
                extra_loss_db=0.0,
                sector_id="sector_a",
                sector_freq_mhz=3500.0,
                sector_tx_power_dbm=43.0,
                sector_channel_bandwidth_mhz=20.0,
                sector_azimuth_deg=0.0,
                sector_beamwidth_h_deg=360.0,
            ),
            WorldCell(
                lat=1.0,
                lon=1.0,
                distance_m=150.0,
                bearing_deg=0.0,
                dominant_material="unknown",
                obstacles_count=0,
                extra_loss_db=0.0,
                sector_id="sector_b",
                sector_freq_mhz=3500.0,
                sector_tx_power_dbm=42.0,
                sector_channel_bandwidth_mhz=20.0,
                sector_azimuth_deg=0.0,
                sector_beamwidth_h_deg=360.0,
            ),
        ],
    )

    grid: AttenuationGrid = compute_attenuation_grid(world)

    assert len(grid.cell_lat) == 1
    assert grid.serving_sector_id == ["sector_a"]
    assert grid.interferer_count == [1]
    assert grid.pilot_pollution_metric_db[0] < 3.0
    assert grid.sinr_db[0] < (grid.rsrp_dbm[0] - rf_params.noise_floor_dbm)


def test_angle_sector_generates_full_field_candidates_before_pattern_selection():
    rf_params = RFParams(
        freq_mhz=3500.0,
        tx_power_dbm=43.0,
        path_loss_model="legacy",
        max_range_m=20.0,
        step_m=20.0,
        dtheta_deg=90.0,
    )

    cells = build_coverage_grid(
        tx=LatLon(lat=0.0, lon=0.0),
        rf_params=rf_params,
        sectors=[
            {
                "sector_id": "sector_a",
                "sector_type": "angle",
                "start_angle_deg": 0.0,
                "end_angle_deg": 120.0,
                "freq_mhz": 3500.0,
                "tx_power_dbm": 43.0,
                "channel_bandwidth_mhz": 20.0,
                "azimuth_deg": 60.0,
                "beamwidth_h_deg": 65.0,
            }
        ],
    )

    bearings = [float(cell.bearing_deg) for cell in cells]
    assert any(0.0 <= bearing <= 120.0 for bearing in bearings)
    assert any(bearing > 150.0 for bearing in bearings)


class _SingleSegmentProvider(StubMapProvider):
    def get_buildings_along_ray(self, start: LatLon, end: LatLon):
        return [{"r0_m": 20.0, "r1_m": 40.0, "material": "concrete"}]


def test_post_exit_samples_resume_los_with_local_shadow():
    rf_params = RFParams(
        freq_mhz=3500.0,
        tx_power_dbm=43.0,
        path_loss_model="3gpp_38901",
        propagation_scenario="umi_street_canyon",
        tx_height_m=10.0,
        rx_height_m=1.5,
        max_range_m=200.0,
        step_m=20.0,
        dtheta_deg=360.0,
    )

    cells = build_coverage_grid(
        tx=LatLon(lat=0.0, lon=0.0),
        rf_params=rf_params,
        map_provider=_SingleSegmentProvider(),
    )
    by_distance = {cell.distance_m: cell for cell in cells}

    inside = by_distance[20.0]
    just_after_exit = by_distance[40.0]
    far_after_exit = by_distance[160.0]

    assert inside.is_los is False
    assert inside.penetration_loss_db > 0.0

    assert just_after_exit.is_los is True
    assert just_after_exit.penetration_loss_db == 0.0
    assert just_after_exit.shadow_loss_db > 0.0
    assert just_after_exit.diffraction_loss_db > 0.0

    assert far_after_exit.is_los is True
    assert 0.0 < far_after_exit.shadow_loss_db < just_after_exit.shadow_loss_db
    assert 0.0 < far_after_exit.diffraction_loss_db < just_after_exit.diffraction_loss_db
