import math

import numpy as np
import pytest

from agentic_rf_planner.geo.heatmap import EllipseRasterizer
from agentic_rf_planner.pipeline.schemas import (
    AttenuationGrid,
    BroadcastWorldCell,
    LatLon,
    MaterialType,
    RFParams,
    WorldCell,
    WorldModel,
)
from agentic_rf_planner.rf.attenuation_models import apply_channel_analysis, compute_attenuation_grid
from agentic_rf_planner.rf.channel_analysis import (
    ChannelAnalysisConfig,
    TargetMotion,
    bistatic_geometry,
    bistatic_geometry_arrays,
)
from agentic_rf_planner.rf.channel_environment import ReturnPathEnvironmentLookup
from agentic_rf_planner.rf.dvt import DVTTransmitter


def _dvt() -> DVTTransmitter:
    return DVTTransmitter.model_validate(
        {
            "fc": 587e6,
            "fs": 10e6,
            "bandwidth": 6e6,
            "waveform": "atsc1",
            "tx": {
                "latitude": 38.271667,
                "longitude": -121.506111,
                "altitude": 5.0,
                "antennaHeight": 320.0,
                "name": "Broadcast TX",
            },
            "power": {"erpHKw": 1000.0, "polarization": "H"},
            "antenna": {"patternType": "omnidirectional", "verticalBeamwidthDeg": 180.0},
        }
    )


def _channel_config() -> ChannelAnalysisConfig:
    return ChannelAnalysisConfig.model_validate(
        {
            "receiver": {
                "latitude": 38.4,
                "longitude": -121.5,
                "altitudeMamsl": 10.0,
                "antennaHeightMagl": 15.0,
                "directAntennaGainDbi": 8.0,
                "echoAntennaGainDbi": 12.0,
                "feederLossDb": 1.0,
                "noiseFigureDb": 5.0,
            },
            "target": {"heightMagl": 1000.0, "bistaticRcsM2": 10.0},
            "motion": {"speedMps": 120.0, "headingDegTrue": 180.0, "climbRateMps": 0.0},
            "processing": {
                "coherentIntegrationS": 1.0,
                "processingLossDb": 3.0,
                "systemLossDb": 3.0,
                "requiredSnrDb": 10.0,
                "directPathCancellationDb": 60.0,
            },
        }
    )


def test_internal_world_cells_are_slotted_and_dict_free():
    nr_cell = WorldCell(
        lat=1.0,
        lon=2.0,
        distance_m=100.0,
        bearing_deg=0.0,
        dominant_material=MaterialType.UNKNOWN,
        obstacles_count=0,
        extra_loss_db=0.0,
    )
    broadcast_cell = BroadcastWorldCell(
        lat=1.0,
        lon=2.0,
        distance_m=100.0,
        bearing_deg=0.0,
        obstacles_count=0,
        extra_loss_db=0.0,
    )
    assert not hasattr(nr_cell, "__dict__")
    assert not hasattr(broadcast_cell, "__dict__")


def test_vector_bistatic_geometry_matches_scalar_geometry():
    target_lat = np.array([38.30, 38.35, 38.42], dtype=np.float64)
    target_lon = np.array([-121.50, -121.42, -121.55], dtype=np.float64)
    target_alt = np.array([100.0, 500.0, 1000.0], dtype=np.float64)
    motion = TargetMotion(speedMps=140.0, headingDegTrue=127.0, climbRateMps=4.0)
    vector = bistatic_geometry_arrays(
        tx_latitude_deg=38.271667,
        tx_longitude_deg=-121.506111,
        tx_altitude_m=325.0,
        target_latitude_deg=target_lat,
        target_longitude_deg=target_lon,
        target_altitude_m=target_alt,
        receiver_latitude_deg=38.41717489626978,
        receiver_longitude_deg=-121.39508190497462,
        receiver_altitude_m=25.0,
        target_motion=motion,
        frequency_hz=587e6,
    )
    for index in range(target_lat.size):
        scalar = bistatic_geometry(
            tx_latitude_deg=38.271667,
            tx_longitude_deg=-121.506111,
            tx_altitude_m=325.0,
            target_latitude_deg=float(target_lat[index]),
            target_longitude_deg=float(target_lon[index]),
            target_altitude_m=float(target_alt[index]),
            receiver_latitude_deg=38.41717489626978,
            receiver_longitude_deg=-121.39508190497462,
            receiver_altitude_m=25.0,
            target_motion=motion,
            frequency_hz=587e6,
        )
        assert vector["tx_target_range_m"][index] == pytest.approx(scalar.tx_target_range_m, rel=1e-10)
        assert vector["target_receiver_range_m"][index] == pytest.approx(
            scalar.target_receiver_range_m, rel=1e-10
        )
        assert vector["bistatic_angle_deg"][index] == pytest.approx(scalar.bistatic_angle_deg, abs=1e-9)
        assert vector["doppler_hz"][index] == pytest.approx(scalar.doppler_hz, rel=1e-10, abs=1e-9)


def test_return_path_batch_lookup_matches_scalar_lookup():
    excess = np.arange(8 * 12, dtype=np.float32).reshape(8, 12)
    valid = np.ones_like(excess, dtype=np.bool_)
    lookup = ReturnPathEnvironmentLookup(
        receiver=LatLon(lat=38.4, lon=-121.5),
        max_range_m=1100.0,
        dr_m=100.0,
        dtheta_deg=45.0,
        excess_loss_db=excess,
        valid=valid,
    )
    lats = np.array([38.401, 38.398, 38.405])
    lons = np.array([-121.5, -121.497, -121.504])
    batched = lookup.sample_many(lats, lons)
    for i, (lat, lon) in enumerate(zip(lats, lons)):
        scalar = lookup.sample(float(lat), float(lon))
        assert batched[0][i] == pytest.approx(scalar[0])
        assert batched[1][i] == pytest.approx(scalar[1], rel=1e-10)
        assert batched[2][i] == pytest.approx(scalar[2], rel=1e-10)
        assert bool(batched[3][i]) is scalar[3]


def test_forward_coverage_can_finish_before_channel_analysis():
    dvt = _dvt()
    rf = RFParams(
        technology="dvt",
        waveform="atsc1",
        dvt=dvt,
        channel_analysis=_channel_config(),
        freq_mhz=1.0,
        tx_power_dbm=0.0,
        max_range_m=20_000.0,
        terrain_enabled=False,
        path_loss_model="fspl",
    )
    world = WorldModel(
        tx=LatLon(lat=dvt.tx.latitude, lon=dvt.tx.longitude),
        rf_params=rf,
        cells=[
            BroadcastWorldCell(
                lat=38.30,
                lon=-121.50,
                distance_m=5000.0,
                bearing_deg=10.0,
                obstacles_count=0,
                extra_loss_db=0.0,
                z_ground_m=5.0,
                z_rx_abs_m=1005.0,
            )
        ],
        z_tx_ground_m=5.0,
        z_tx_abs_m=325.0,
    )
    forward = compute_attenuation_grid(world, include_channel_analysis=False)
    assert forward.channel_analysis_summary is None
    assert len(forward.field_strength_dbuv_m) == 1
    completed = apply_channel_analysis(world, forward)
    assert completed.channel_analysis_summary is not None
    assert len(completed.bistatic_echo_power_dbm) == 1


def test_shared_rasterizer_renders_multiple_array_layers():
    count = 72
    theta = np.linspace(0.0, 2.0 * math.pi, count, endpoint=False)
    radius = np.full(count, 1000.0)
    lat0 = 38.27
    lon0 = -121.50
    earth = 6_371_000.0
    lat = lat0 + np.rad2deg(radius * np.cos(theta) / earth)
    lon = lon0 + np.rad2deg(radius * np.sin(theta) / (earth * math.cos(math.radians(lat0))))
    rf = RFParams(freq_mhz=587.0, tx_power_dbm=79.0, max_range_m=2000.0, step_m=20.0, dtheta_deg=5.0)
    grid = AttenuationGrid(
        tx=LatLon(lat=lat0, lon=lon0),
        rf_params=rf,
        cell_lat=lat,
        cell_lon=lon,
        rsrp_dbm=np.linspace(-100.0, -40.0, count, dtype=np.float32),
        sinr_db=np.zeros(count, dtype=np.float32),
        modulation=["x"] * count,
        throughput_mbps=[0.0] * count,
        serving_sector_id=["omni"] * count,
        interferer_count=[0] * count,
        top_interferer_rsrp_dbm=[-200.0] * count,
        pilot_pollution_metric_db=[0.0] * count,
    )
    rasterizer = EllipseRasterizer(grid, size=128)
    first = rasterizer.render(grid.rsrp_dbm)
    second = rasterizer.render(np.asarray(grid.rsrp_dbm) + 10.0)
    assert first["width"] == second["width"] == 128
    assert first["png_b64"].startswith("data:image/png;base64,")
    assert second["actual_min"] == pytest.approx(first["actual_min"] + 10.0, abs=0.1)
