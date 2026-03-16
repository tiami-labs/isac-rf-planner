"""Targeted tests for the vendor-grade RF roadmap pieces."""

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
