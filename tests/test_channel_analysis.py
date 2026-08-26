import json
import math
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from agentic_rf_planner.api.rest import PlanRequest, app
from agentic_rf_planner.pipeline.schemas import LatLon, MaterialType, RFParams, WorldCell, WorldModel
from agentic_rf_planner.rf.attenuation_models import apply_channel_analysis, compute_attenuation_grid
from agentic_rf_planner.rf.channel_analysis import (
    ChannelAnalysisConfig,
    TargetMotion,
    bistatic_echo_power_dbm,
    bistatic_geometry,
    free_space_path_loss_db,
)
from agentic_rf_planner.rf.channel_products import write_channel_product
from agentic_rf_planner.rf.isac_reanalysis import _horizontal_excess_delay_scalar
from agentic_rf_planner.rf.dvt import DVTTransmitter


def _channel_config(*, speed_mps: float = 120.0, heading_deg: float = 180.0, prf_hz=None):
    processing = {
        "coherentIntegrationS": 1.0,
        "processingLossDb": 3.0,
        "systemLossDb": 3.0,
        "requiredSnrDb": 10.0,
        "directPathCancellationDb": 60.0,
        "clutterNotchHz": 0.5,
        "minimumDetectableDopplerHz": 0.5,
    }
    if prf_hz is not None:
        processing["pulseRepetitionFrequencyHz"] = prf_hz
    return ChannelAnalysisConfig.model_validate(
        {
            "receiver": {
                "latitude": 38.40,
                "longitude": -121.50,
                "altitudeMamsl": 10.0,
                "antennaHeightMagl": 15.0,
                "directAntennaGainDbi": 8.0,
                "echoAntennaGainDbi": 12.0,
                "feederLossDb": 1.0,
                "noiseFigureDb": 5.0,
                "directPathExcessLossDb": 2.0,
                "returnPathExcessLossDb": 4.0,
            },
            "target": {"heightMagl": 1000.0, "bistaticRcsM2": 10.0},
            "motion": {
                "speedMps": speed_mps,
                "headingDegTrue": heading_deg,
                "climbRateMps": 0.0,
            },
            "processing": processing,
        }
    )


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
                "name": "KTX",
            },
            "power": {"erpHKw": 1000.0, "erpVKw": 250.0, "polarization": "DA (E)"},
            "antenna": {"patternType": "omnidirectional", "verticalBeamwidthDeg": 180.0},
        }
    )


def _one_cell_world(rf: RFParams) -> WorldModel:
    return WorldModel(
        tx=LatLon(
            lat=rf.dvt.tx.latitude if rf.dvt else 38.271667,
            lon=rf.dvt.tx.longitude if rf.dvt else -121.506111,
        ),
        rf_params=rf,
        cells=[
            WorldCell(
                lat=38.30,
                lon=-121.50,
                distance_m=5000.0,
                bearing_deg=10.0,
                dominant_material=MaterialType.UNKNOWN,
                obstacles_count=0,
                extra_loss_db=0.0,
                z_ground_m=5.0,
                sector_id="main",
                sector_freq_mhz=rf.freq_mhz,
                sector_tx_power_dbm=rf.tx_power_dbm,
                sector_channel_bandwidth_mhz=rf.channel_bandwidth_mhz,
                sector_azimuth_deg=0.0,
                sector_beamwidth_h_deg=360.0,
            )
        ],
        z_tx_ground_m=5.0,
        z_tx_abs_m=325.0,
    )


def test_bistatic_echo_matches_expanded_radar_equation():
    fc = 587e6
    wavelength = 299_792_458.0 / fc
    incident_dbm = -40.0
    return_loss = free_space_path_loss_db(20_000.0, fc)
    actual = bistatic_echo_power_dbm(
        incident_isotropic_power_dbm=incident_dbm,
        return_path_loss_db=return_loss,
        receiver_gain_dbi=12.0,
        receiver_feeder_loss_db=1.0,
        bistatic_rcs_m2=10.0,
        frequency_hz=fc,
        system_loss_db=3.0,
    )
    expected = (
        incident_dbm
        - return_loss
        + 12.0
        - 1.0
        + 10.0 * math.log10(10.0)
        + 10.0 * math.log10(4.0 * math.pi)
        - 20.0 * math.log10(wavelength)
        - 3.0
    )
    assert actual == pytest.approx(expected)


def test_colocated_tx_rx_closing_target_has_positive_double_leg_doppler():
    frequency_hz = 1.0e9
    geometry = bistatic_geometry(
        tx_latitude_deg=0.0,
        tx_longitude_deg=0.0,
        tx_altitude_m=0.0,
        target_latitude_deg=0.01,
        target_longitude_deg=0.0,
        target_altitude_m=0.0,
        receiver_latitude_deg=0.0,
        receiver_longitude_deg=0.0,
        receiver_altitude_m=0.0,
        target_motion=TargetMotion(speedMps=100.0, headingDegTrue=180.0, climbRateMps=0.0),
        frequency_hz=frequency_hz,
    )
    expected_hz = 2.0 * 100.0 * frequency_hz / 299_792_458.0
    assert geometry.path_range_rate_mps == pytest.approx(-200.0, abs=0.05)
    assert geometry.closing_speed_mps == pytest.approx(200.0, abs=0.05)
    assert geometry.doppler_hz == pytest.approx(expected_hz, rel=5e-4)


@pytest.mark.parametrize(
    "technology,waveform",
    [("dvt", "atsc1"), ("5g_nr", "5g_nr")],
)
def test_channel_analysis_runs_for_broadcast_and_nr(technology, waveform):
    if technology == "dvt":
        rf = RFParams(
            technology="dvt",
            waveform="atsc1",
            dvt=_dvt(),
            channel_analysis=_channel_config(),
            freq_mhz=1.0,
            tx_power_dbm=0.0,
            terrain_enabled=False,
            path_loss_model="fspl",
        )
    else:
        rf = RFParams(
            technology="5g_nr",
            waveform="5g_nr",
            channel_analysis=_channel_config(),
            freq_mhz=3500.0,
            tx_power_dbm=43.0,
            channel_bandwidth_mhz=40.0,
            terrain_enabled=False,
            path_loss_model="legacy",
            sectors=[
                {
                    "sector_id": "main",
                    "sector_type": "omnidirectional",
                    "freq_mhz": 3500.0,
                    "tx_power_dbm": 43.0,
                    "channel_bandwidth_mhz": 40.0,
                    "azimuth_deg": 0.0,
                    "beamwidth_h_deg": 360.0,
                }
            ],
        )
    grid = compute_attenuation_grid(_one_cell_world(rf))
    assert grid.channel_analysis_summary is not None
    assert grid.channel_analysis_summary["model"] == "waveform_agnostic_bistatic_channel"
    assert grid.channel_analysis_summary["technology"] == technology
    assert grid.channel_analysis_summary["waveform"] == waveform
    assert grid.channel_analysis_summary["source_power_basis"] == "total_carrier_eirp"
    assert len(grid.incident_power_isotropic_dbm or []) == 1
    assert len(grid.bistatic_echo_power_dbm or []) == 1
    assert len(grid.bistatic_excess_delay_s or []) == 1
    assert len(grid.bistatic_doppler_hz or []) == 1
    assert len(grid.bistatic_detectable or []) == 1


def test_plan_request_accepts_generic_analysis_for_nr_and_dvt_and_emits_new_key():
    config = _channel_config().model_dump(by_alias=True)
    nr = PlanRequest.model_validate(
        {
            "lat": 38.27,
            "lon": -121.50,
            "technology": "5g_nr",
            "waveform": "5g_nr",
            "channel_analysis": config,
        }
    )
    assert nr.channel_analysis is not None
    assert nr.rx_height_m == pytest.approx(1000.0)

    dvt = PlanRequest.model_validate(
        {
            "waveform": "atsc1",
            "dvt": _dvt().model_dump(by_alias=True),
            "channel_analysis": config,
        }
    )
    assert dvt.channel_analysis is not None
    assert dvt.technology == "dvt"

    legacy = PlanRequest.model_validate(
        {
            "lat": 38.27,
            "lon": -121.50,
            "passive_radar": {
                "receiver": {
                    "latitude": 38.4,
                    "longitude": -121.5,
                    "altitude": 10,
                    "antennaHeightM": 15,
                    "referenceAntennaGainDbi": 8,
                    "surveillanceAntennaGainDbi": 12,
                },
                "target": {"heightAglM": 1000, "bistaticRcsM2": 10},
            },
        }
    )
    dumped = legacy.model_dump(by_alias=True)
    assert legacy.channel_analysis is not None
    assert "channel_analysis" in dumped
    assert "passive_radar" not in dumped


def test_channel_product_is_compressed_machine_readable_and_downloadable(tmp_path, monkeypatch):
    monkeypatch.setenv("RF_CHANNEL_PRODUCT_DIR", str(tmp_path))
    info = write_channel_product(
        arrays={
            "latitude_deg": [1.0, 2.0],
            "longitude_deg": [3.0, 4.0],
            "echo_power_dbm": [-100.0, -110.0],
            "doppler_hz": [12.5, -8.0],
            "detectable": [True, False],
        },
        metadata={"schema": "test", "schema_version": "1.0"},
    )
    path = tmp_path / f"{info['product_id']}.npz"
    assert path.is_file()
    with np.load(path, allow_pickle=False) as product:
        assert product["echo_power_dbm"].tolist() == [-100.0, -110.0]
        assert product["detectable"].tolist() == [True, False]
        metadata = json.loads(str(product["metadata_json"]))
        assert metadata["schema"] == "test"

    response = TestClient(app).get(info["download_url"])
    assert response.status_code == 200
    assert response.content == path.read_bytes()
    assert response.headers["content-disposition"].endswith('.npz"')


def test_environment_reciprocal_field_is_used_for_target_return_loss():
    from types import SimpleNamespace

    rf = RFParams(
        technology="dvt",
        waveform="atsc1",
        dvt=_dvt(),
        channel_analysis=_channel_config().model_copy(update={"return_path_model": "environment_reciprocal"}),
        freq_mhz=587.0,
        tx_power_dbm=0.0,
        terrain_enabled=False,
        path_loss_model="fspl",
    )
    world = _one_cell_world(rf)
    base = compute_attenuation_grid(world, apply_channel=False)
    frequency_hz = rf.dvt.fc
    # Deliberately make the environmental return 20 dB worse than free space.
    target_range = bistatic_geometry(
        tx_latitude_deg=world.tx.lat, tx_longitude_deg=world.tx.lon, tx_altitude_m=325.0,
        target_latitude_deg=base.cell_lat[0], target_longitude_deg=base.cell_lon[0], target_altitude_m=1005.0,
        receiver_latitude_deg=rf.channel_analysis.receiver.latitude,
        receiver_longitude_deg=rf.channel_analysis.receiver.longitude,
        receiver_altitude_m=rf.channel_analysis.receiver.absolute_height_m,
        target_motion=rf.channel_analysis.motion, frequency_hz=frequency_hz,
    ).target_receiver_range_m
    env_loss = free_space_path_loss_db(target_range, frequency_hz) + 20.0
    reciprocal = SimpleNamespace(
        path_loss_db=[env_loss], environment_loss_db=[20.0], terrain_loss_db=[7.0],
        los=[False], terrain_state=["terrain_shadow"], sample_error_m=[12.0],
        metadata={"model": "environment_reciprocal", "resolution_m": 50.0},
    )
    fused = apply_channel_analysis(world, base, reciprocal_field=reciprocal)
    assert fused.return_path_loss_db[0] == pytest.approx(env_loss + 4.0)
    assert fused.return_environment_loss_db[0] == pytest.approx(20.0)
    assert fused.return_terrain_loss_db[0] == pytest.approx(7.0)
    assert fused.return_los[0] is False
    assert fused.channel_analysis_summary["return_path_model"] == "environment_reciprocal"
    assert fused.channel_analysis_summary["best_margin_point"]["return_path_loss_db"] == pytest.approx(env_loss + 4.0)


def test_target_height_illumination_field_overrides_isac_incident_power_without_changing_coverage():
    from types import SimpleNamespace

    rf = RFParams(
        technology="dvt",
        waveform="atsc1",
        dvt=_dvt(),
        channel_analysis=_channel_config(),
        freq_mhz=587.0,
        tx_power_dbm=0.0,
        terrain_enabled=False,
        path_loss_model="fspl",
    )
    world = _one_cell_world(rf)
    base = compute_attenuation_grid(world, apply_channel=False)
    communication_incident = float(base.channel_array("incident_power_isotropic_dbm")[0])
    target_incident = communication_incident + 20.0
    illumination = SimpleNamespace(
        incident_power_dbm=np.asarray([target_incident], dtype=np.float32),
        path_loss_db=np.asarray([111.0], dtype=np.float32),
        environment_loss_db=np.asarray([7.0], dtype=np.float32),
        terrain_loss_db=np.asarray([2.0], dtype=np.float32),
        los=np.asarray([False], dtype=np.bool_),
        sample_error_m=np.asarray([3.0], dtype=np.float32),
        metadata={"model": "target_height_tx_environment", "target_height_m_agl": 1000.0},
    )

    fused = apply_channel_analysis(world, base, illumination_field=illumination)

    # Communications coverage remains at the communications receiver height.
    assert float(fused.channel_array("incident_power_isotropic_dbm")[0]) == pytest.approx(communication_incident)
    # ISAC fusion uses the dedicated target-height illumination field instead.
    assert float(fused.channel_array("isac_incident_power_isotropic_dbm")[0]) == pytest.approx(target_incident)
    best = fused.channel_analysis_summary["best_margin_point"]
    assert best["incident_power_isotropic_dbm"] == pytest.approx(target_incident)
    assert best["tx_target_path_loss_db"] == pytest.approx(111.0)
    assert best["tx_target_environment_loss_db"] == pytest.approx(7.0)
    assert best["tx_target_terrain_loss_db"] == pytest.approx(2.0)
    assert best["tx_target_los"] is False
    fidelity = fused.channel_analysis_summary["model_fidelity"]
    assert fidelity["target_height_tx_environment_field"] is True
    assert fidelity["direct_path_exact_environment_ray"] is False


def test_channel_product_probe_returns_nearest_target(tmp_path, monkeypatch):
    monkeypatch.setenv("RF_CHANNEL_PRODUCT_DIR", str(tmp_path))
    info = write_channel_product(
        arrays={
            "latitude_deg": [38.0, 38.1],
            "longitude_deg": [-121.0, -121.1],
            "detection_margin_db": [-5.0, 12.0],
            "return_path_loss_db": [120.0, 140.0],
            "detectable": [False, True],
        },
        metadata={
            "transmitter": {"latitude": 37.9, "longitude": -121.0},
            "summary": {"receiver": {"latitude": 38.2, "longitude": -121.2}},
        },
    )
    response = TestClient(app).get(
        f"/api/channel-analysis/products/{info['product_id']}/probe?lat=38.099&lon=-121.099"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["index"] == 1
    assert payload["values"]["detection_margin_db"] == pytest.approx(12.0)
    assert payload["values"]["return_path_loss_db"] == pytest.approx(140.0)
    assert payload["values"]["detectable"] is True
    assert payload["transmitter"]["latitude"] == pytest.approx(37.9)


def _reusable_scene_product(tmp_path, monkeypatch, *, direct_received_dbm=-120.0, static_background=False):
    monkeypatch.setenv("RF_CHANNEL_PRODUCT_DIR", str(tmp_path))
    arrays = {
        "latitude_deg": np.array([38.2700, 38.2710, 38.2720, 38.2730], dtype=np.float64),
        "longitude_deg": np.array([-121.5060, -121.5050, -121.5040, -121.5030], dtype=np.float64),
        "z_ground_m": np.array([5.0, 6.0, 7.0, 8.0], dtype=np.float32),
        "echo_geometry_base_dbm": np.array([-120.0, -121.0, -122.0, -123.0], dtype=np.float32),
        "doppler_east_hz_per_mps": np.array([1.0, 1.5, 2.0, 2.5], dtype=np.float32),
        "doppler_north_hz_per_mps": np.array([2.0, 2.0, 2.0, 2.0], dtype=np.float32),
        "doppler_up_hz_per_mps": np.array([0.25, 0.25, 0.25, 0.25], dtype=np.float32),
        "doppler_sensitivity_hz_per_mps": np.array([2.25, 2.55, 2.84, 3.21], dtype=np.float32),
        "return_environment_valid": np.array([True, True, True, True]),
        "return_path_loss_db": np.array([110.0, 111.0, 112.0, 113.0], dtype=np.float32),
        "return_environment_loss_db": np.array([3.0, 4.0, 5.0, 6.0], dtype=np.float32),
        "return_terrain_loss_db": np.array([1.0, 1.5, 2.0, 2.5], dtype=np.float32),
        "return_los": np.array([True, True, False, False]),
    }
    if static_background:
        rx_lat = 38.41717489626978
        rx_lon = -121.39508190497462
        rx_abs = 15.0
        tx_lat = 38.271667
        tx_lon = -121.506111
        tx_abs = 325.0
        delays = []
        zero_motion = TargetMotion(speedMps=0.0, headingDegTrue=0.0, climbRateMps=0.0)
        for lat_i, lon_i, ground_i in zip(arrays["latitude_deg"], arrays["longitude_deg"], arrays["z_ground_m"]):
            g = bistatic_geometry(
                tx_latitude_deg=tx_lat, tx_longitude_deg=tx_lon, tx_altitude_m=tx_abs,
                target_latitude_deg=float(lat_i), target_longitude_deg=float(lon_i),
                target_altitude_m=float(ground_i) + 1000.0,
                receiver_latitude_deg=rx_lat, receiver_longitude_deg=rx_lon, receiver_altitude_m=rx_abs,
                target_motion=zero_motion, frequency_hz=587e6,
            )
            delays.append(g.excess_delay_s)
        arrays["excess_delay_s"] = np.asarray(delays, dtype=np.float64)
    summary = {
        "frequency_hz": 587e6,
        "waveform_bandwidth_hz": 6e6,
        "receiver": {
            "latitude": 38.41717489626978,
            "longitude": -121.39508190497462,
            "altitudeMamsl": 5.0,
            "antennaHeightMagl": 10.0,
            "directAntennaGainDbi": 8.0,
            "echoAntennaGainDbi": 12.0,
            "feederLossDb": 1.0,
            "noiseFigureDb": 5.0,
            "directPathExcessLossDb": 0.0,
            "returnPathExcessLossDb": 0.0,
        },
        "target": {"heightMagl": 1000.0, "bistaticRcsM2": 10.0},
        "motion": {"speedMps": 50.0, "headingDegTrue": 0.0, "climbRateMps": 0.0},
        "processing": {
            "coherentIntegrationS": 1.0,
            "processingLossDb": 3.0,
            "systemLossDb": 3.0,
            "requiredSnrDb": 10.0,
            "directPathCancellationDb": 60.0,
            "requiredEchoToResidualDirectDb": 0.0,
            "requireDirectPathConstraint": True,
            "requireDynamicRangeConstraint": False,
            "clutterNotchHz": 0.0,
            "minimumDetectableDopplerHz": 0.0,
        },
        "direct_path": {"received_power_dbm": direct_received_dbm},
    }
    if static_background:
        summary["static_background_channel"] = {
            "enabled": True,
            "model": "osm_2d_single_bounce_specular_background",
            "scope": "static_mapped_building_facades_only",
            "power_basis": "existing_material_labelled_specular_rt_model_not_site_calibrated",
            "calibrated_absolute_clutter_power": False,
            "paths": [
                {"building_id": 101, "material": "brick", "excess_delay_s": _horizontal_excess_delay_scalar(tx_lat, tx_lon, float(arrays["latitude_deg"][1]), float(arrays["longitude_deg"][1]), rx_lat, rx_lon), "total_path_m": 20000.0},
                {"building_id": 102, "material": "concrete", "excess_delay_s": _horizontal_excess_delay_scalar(tx_lat, tx_lon, float(arrays["latitude_deg"][1]), float(arrays["longitude_deg"][1]), rx_lat, rx_lon), "total_path_m": 20000.0},
                {"building_id": 999, "material": "glass", "excess_delay_s": _horizontal_excess_delay_scalar(tx_lat, tx_lon, float(arrays["latitude_deg"][1]), float(arrays["longitude_deg"][1]), rx_lat, rx_lon) + 8.0e-6, "total_path_m": 20008.0},
            ],
        }
    return write_channel_product(
        arrays=arrays,
        metadata={
            "schema": "agentic_rf_planner.channel_analysis_grid",
            "schema_version": "2.0",
            "summary": summary,
            "transmitter": {
                "latitude": 38.271667,
                "longitude": -121.506111,
                "absolute_height_m": 325.0,
            },
            "rf_config": {"max_range_m": 20000.0, "step_m": 20.0, "dtheta_deg": 0.25, "freq_mhz": 587.0},
        },
    )


def test_reusable_isac_scene_reanalysis_changes_motion_and_rcs_without_world_rebuild(tmp_path, monkeypatch):
    info = _reusable_scene_product(tmp_path, monkeypatch)
    client = TestClient(app)
    base = client.post(
        f"/api/channel-analysis/products/{info['product_id']}/evaluate",
        json={
            "layer": "bistatic_doppler",
            "selectedLatitude": 38.271,
            "selectedLongitude": -121.505,
            "processing": {"effectiveProcessingGainDb": 20.0, "interferencePlusClutterPowerDbm": -120.0},
        },
    )
    assert base.status_code == 200, base.text
    a = base.json()
    assert a["scene_reused"] is True
    assert a["complexity"]["time"] == "O(N)"
    assert a["processing_assessment"]["qualified"] is True
    assert a["selected_target"]["latitude"] == pytest.approx(38.271, abs=1e-12)
    assert a["selected_target"]["longitude"] == pytest.approx(-121.505, abs=1e-12)
    assert a["selected_target"]["scene_interpolation"]["method"] == "inverse_distance_squared_4_nearest"
    assert a["target_measurement_overlay"]["png_b64"].startswith("data:image/png;base64,")
    assert a["selected_target"]["measurement_cell"]["joint_cell_point_count"] >= 0
    assert a["selected_target"]["bistatic_rcs_m2"] == pytest.approx(10.0)
    assert a["layer"]["png_b64"].startswith("data:image/png;base64,")

    changed = client.post(
        f"/api/channel-analysis/products/{info['product_id']}/evaluate",
        json={
            "layer": "bistatic_echo",
            "selectedLatitude": 38.271,
            "selectedLongitude": -121.505,
            "target": {"bistaticRcsM2": 1.0},
            "motion": {"speedMps": 50.0, "headingDegTrue": 90.0, "climbRateMps": 0.0},
            "processing": {"effectiveProcessingGainDb": 20.0, "interferencePlusClutterPowerDbm": -120.0},
        },
    )
    assert changed.status_code == 200, changed.text
    b = changed.json()
    assert b["selected_target"]["echo_power_dbm"] == pytest.approx(a["selected_target"]["echo_power_dbm"] - 10.0, abs=1e-3)
    assert b["selected_target"]["doppler_hz"] != pytest.approx(a["selected_target"]["doppler_hz"], abs=1e-6)
    assert b["selected_target"]["bistatic_rcs_m2"] == pytest.approx(1.0)


def test_qualified_detectability_requires_explicit_effective_processing_gain(tmp_path, monkeypatch):
    info = _reusable_scene_product(tmp_path, monkeypatch)
    client = TestClient(app)
    response = client.post(
        f"/api/channel-analysis/products/{info['product_id']}/evaluate",
        json={"layer": "qualified_detectable"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["processing_assessment"]["gain_source"] == "ideal_time_bandwidth_screening"
    assert payload["processing_assessment"]["qualified"] is False
    assert payload["counts"]["qualified_detectable"] == 0
    assert payload["counts"]["screening_detectable"] > 0


def test_direct_residual_constraint_gates_screening_detectability(tmp_path, monkeypatch):
    info = _reusable_scene_product(tmp_path, monkeypatch, direct_received_dbm=-20.0)
    response = TestClient(app).post(
        f"/api/channel-analysis/products/{info['product_id']}/evaluate",
        json={
            "layer": "screening_detectable",
            "selectedLatitude": 38.271,
            "selectedLongitude": -121.505,
            "processing": {"effectiveProcessingGainDb": 20.0, "interferencePlusClutterPowerDbm": -120.0, "directPathCancellationDb": 60.0},
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    selected = payload["selected_target"]
    assert selected["snr_noise_interference_ok"] is True
    assert selected["direct_residual_ok"] is False
    assert selected["detectable_screening"] is False
    assert "direct_residual" in selected["failed_constraints"]
    assert payload["counts"]["direct_residual_ok"] == 0


def test_target_height_change_requires_new_scene(tmp_path, monkeypatch):
    info = _reusable_scene_product(tmp_path, monkeypatch)
    response = TestClient(app).post(
        f"/api/channel-analysis/products/{info['product_id']}/evaluate",
        json={"target": {"heightMagl": 900.0}},
    )
    assert response.status_code == 400
    assert "requires a new RF plan" in response.json()["detail"]


def test_doppler_sensitivity_basis_matches_bistatic_angle_identity():
    rf = RFParams(
        technology="dvt", waveform="atsc1", dvt=_dvt(),
        channel_analysis=_channel_config().model_copy(update={
            "processing": _channel_config().processing.model_copy(update={"effective_processing_gain_db": 20.0})
        }),
        freq_mhz=587.0, tx_power_dbm=0.0, terrain_enabled=False, path_loss_model="fspl",
    )
    grid = compute_attenuation_grid(_one_cell_world(rf))
    beta = math.radians(float(grid.bistatic_angle_deg[0]))
    expected = 2.0 * rf.dvt.fc / 299_792_458.0 * math.cos(beta / 2.0)
    assert grid.bistatic_doppler_sensitivity_hz_per_mps[0] == pytest.approx(expected, rel=2e-3)


def test_receiver_chain_changes_reanalyze_without_rebuilding_scene(tmp_path, monkeypatch):
    info = _reusable_scene_product(tmp_path, monkeypatch)
    client = TestClient(app)
    common = {
        "layer": "bistatic_echo",
        "selectedLatitude": 38.271,
        "selectedLongitude": -121.505,
        "processing": {"effectiveProcessingGainDb": 20.0, "interferencePlusClutterPowerDbm": -120.0},
    }
    base = client.post(f"/api/channel-analysis/products/{info['product_id']}/evaluate", json=common)
    assert base.status_code == 200, base.text
    base_echo = base.json()["selected_target"]["echo_power_dbm"]

    changed = client.post(
        f"/api/channel-analysis/products/{info['product_id']}/evaluate",
        json={**common, "receiver": {"echoAntennaGainDbi": 22.0}},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["selected_target"]["echo_power_dbm"] == pytest.approx(base_echo + 10.0, abs=1e-3)

    geometry_change = client.post(
        f"/api/channel-analysis/products/{info['product_id']}/evaluate",
        json={**common, "receiver": {"latitude": 38.5}},
    )
    assert geometry_change.status_code == 400
    assert "receiver position/height" in geometry_change.json()["detail"]


def test_effective_gain_alone_is_not_qualified_without_interference_basis(tmp_path, monkeypatch):
    info = _reusable_scene_product(tmp_path, monkeypatch)
    response = TestClient(app).post(
        f"/api/channel-analysis/products/{info['product_id']}/evaluate",
        json={"processing": {"effectiveProcessingGainDb": 20.0}, "layer": "qualified_detectable"},
    )
    assert response.status_code == 200, response.text
    assessment = response.json()["processing_assessment"]
    assert assessment["effective_gain_qualified"] is True
    assert assessment["interference_input_qualified"] is False
    assert assessment["qualified"] is False
    assert response.json()["counts"]["qualified_detectable"] == 0


def test_isac_bundle_renders_complete_current_hypothesis_in_one_scene_read(tmp_path, monkeypatch):
    info = _reusable_scene_product(tmp_path, monkeypatch)
    response = TestClient(app).post(
        f"/api/channel-analysis/products/{info['product_id']}/evaluate-bundle",
        json={
            "layers": [
                "bistatic_echo", "bistatic_margin", "rcs_margin",
                "minimum_detectable_rcs", "bistatic_doppler", "doppler_sensitivity",
                "minimum_detectable_speed", "required_cancellation",
                "direct_residual_margin", "screening_detectable", "qualified_detectable",
            ],
            "processing": {
                "effectiveProcessingGainDb": 20.0,
                "interferencePlusClutterPowerDbm": -120.0,
            },
            "imageSize": 256,
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["scene_reused"] is True
    assert payload["complexity"]["scene_reads"] == 1
    assert payload["processing_assessment"]["qualified"] is True
    assert set(payload["layers"]) == {
        "bistatic_echo", "bistatic_margin", "rcs_margin",
        "minimum_detectable_rcs", "bistatic_doppler", "doppler_sensitivity",
        "minimum_detectable_speed", "required_cancellation", "direct_residual_margin",
        "screening_detectable", "qualified_detectable",
    }
    for layer in payload["layers"].values():
        assert layer["png_b64"].startswith("data:image/png;base64,")
    assert payload["layers"]["minimum_detectable_rcs"]["units"] == "dBsm"
    assert payload["layers"]["doppler_sensitivity"]["units"] == "Hz/(m/s)"
    assert payload["layers"]["minimum_detectable_speed"]["units"] == "m/s"


def test_reanalysis_exposes_static_facade_delay_doppler_background_without_using_it_as_calibrated_detection(tmp_path, monkeypatch):
    info = _reusable_scene_product(tmp_path, monkeypatch, static_background=True)
    response = TestClient(app).post(
        f"/api/channel-analysis/products/{info['product_id']}/evaluate",
        json={
            "layer": "static_clutter_path_count",
            "selectedLatitude": 38.271,
            "selectedLongitude": -121.505,
            "motion": {"speedMps": 0.0, "headingDegTrue": 0.0, "climbRateMps": 0.0},
            "processing": {
                "effectiveProcessingGainDb": 20.0,
                "interferencePlusClutterPowerDbm": -120.0,
                "minimumDetectableDopplerHz": 0.0,
                "clutterNotchHz": 0.0,
            },
            "imageSize": 256,
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["layer"]["units"] == "count"
    assert payload["layer"]["png_b64"].startswith("data:image/png;base64,")
    selected = payload["selected_target"]["static_background"]
    assert selected["available"] is True
    assert selected["absolute_scatter_power_available"] is False
    assert selected["target_in_static_doppler_cell"] is True
    assert selected["same_cell_contributor_count"] == 2
    assert [row["building_id"] for row in selected["same_cell_contributors"]] == [101, 102]
    assert "same_cell_modeled_clutter_power_dbm" not in selected
    # The mapped-facade model is a diagnostic background product only; it must
    # not silently become a calibrated detector constraint.
    assert payload["processing_assessment"]["mapped_static_background_used_for_qualified_detection"] is False


def test_reanalysis_static_facade_overlap_depends_on_target_doppler(tmp_path, monkeypatch):
    info = _reusable_scene_product(tmp_path, monkeypatch, static_background=True)
    client = TestClient(app)
    zero = client.post(
        f"/api/channel-analysis/products/{info['product_id']}/evaluate",
        json={
            "layer": "static_clutter_overlap",
            "selectedLatitude": 38.271,
            "selectedLongitude": -121.505,
            "motion": {"speedMps": 0.0, "headingDegTrue": 0.0, "climbRateMps": 0.0},
        },
    )
    assert zero.status_code == 200, zero.text
    assert zero.json()["selected_target"]["static_background"]["target_in_static_doppler_cell"] is True

    moving = client.post(
        f"/api/channel-analysis/products/{info['product_id']}/evaluate",
        json={
            "layer": "static_clutter_overlap",
            "selectedLatitude": 38.271,
            "selectedLongitude": -121.505,
            "motion": {"speedMps": 50.0, "headingDegTrue": 0.0, "climbRateMps": 0.0},
        },
    )
    assert moving.status_code == 200, moving.text
    assert moving.json()["selected_target"]["static_background"]["target_in_static_doppler_cell"] is False


def test_isac_bundle_includes_static_facade_background_layers_from_one_scene_read(tmp_path, monkeypatch):
    info = _reusable_scene_product(tmp_path, monkeypatch, static_background=True)
    response = TestClient(app).post(
        f"/api/channel-analysis/products/{info['product_id']}/evaluate-bundle",
        json={
            "layers": [
                "static_clutter_delay_separation", "static_clutter_overlap", "static_clutter_path_count",
            ],
            "motion": {"speedMps": 0.0, "headingDegTrue": 0.0, "climbRateMps": 0.0},
            "imageSize": 256,
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["complexity"]["scene_reads"] == 1
    assert set(payload["layers"]) == {
        "static_clutter_delay_separation", "static_clutter_overlap", "static_clutter_path_count",
    }
    assert payload["counts"]["static_clutter_overlap"] >= 1
    assert payload["layers"]["static_clutter_path_count"]["units"] == "count"
    assert payload["processing_assessment"]["mapped_static_background_used_for_qualified_detection"] is False
    for rendered in payload["layers"].values():
        assert rendered["png_b64"].startswith("data:image/png;base64,")
