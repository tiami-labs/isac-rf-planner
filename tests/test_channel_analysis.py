import json
import math
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from agentic_rf_planner.api.rest import PlanRequest, app
from agentic_rf_planner.pipeline.schemas import LatLon, MaterialType, RFParams, WorldCell, WorldModel
from agentic_rf_planner.rf.attenuation_models import compute_attenuation_grid
from agentic_rf_planner.rf.channel_analysis import (
    ChannelAnalysisConfig,
    TargetMotion,
    bistatic_echo_power_dbm,
    bistatic_geometry,
    free_space_path_loss_db,
)
from agentic_rf_planner.rf.channel_products import write_channel_product
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
        assert metadata["array_manifest"]["echo_power_dbm"]["dtype"] == "float64"
        assert metadata["array_manifest"]["echo_power_dbm"]["point_aligned"] is True

    response = TestClient(app).get(info["download_url"])
    assert response.status_code == 200
    assert response.content == path.read_bytes()
    assert response.headers["content-disposition"].endswith('.npz"')


def test_environmental_return_lookup_changes_every_target_return_and_total_path_loss():
    class Lookup:
        def sample(self, latitude, longitude):
            return (12.0, 1000.0, 0.0, True)

        def metadata(self):
            return {"model": "test_environment_lookup", "valid_samples": 1}

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
    world = _one_cell_world(rf)
    free_space = compute_attenuation_grid(world)
    environmental = compute_attenuation_grid(
        world,
        channel_context={"return_path_lookup": Lookup()},
    )

    assert environmental.bistatic_return_environment_excess_db == pytest.approx([12.0])
    assert environmental.bistatic_return_path_loss_db[0] == pytest.approx(
        free_space.bistatic_return_path_loss_db[0] + 12.0
    )
    assert environmental.bistatic_total_path_loss_db[0] == pytest.approx(
        free_space.bistatic_total_path_loss_db[0] + 12.0
    )
    assert environmental.bistatic_echo_power_dbm[0] == pytest.approx(
        free_space.bistatic_echo_power_dbm[0] - 12.0
    )
    assert environmental.channel_analysis_summary["return_path_model"] == "environmental_reciprocal_grid"
    assert environmental.channel_analysis_summary["counts"]["return_environment_samples_used"] == 1


def test_complete_channel_product_arrays_include_base_rf_environment_and_isac_outputs():
    from agentic_rf_planner.agents.rf_planning_agent import _channel_product_arrays

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
    grid = compute_attenuation_grid(_one_cell_world(rf))
    arrays = _channel_product_arrays(grid)
    required = {
        "latitude_deg",
        "longitude_deg",
        "ground_elevation_m_amsl",
        "terrain_loss_db",
        "received_power_dbm",
        "field_strength_dbuv_m",
        "carrier_to_noise_db",
        "source_eirp_at_target_dbm",
        "incident_isotropic_power_dbm",
        "tx_target_path_loss_db",
        "tx_target_environment_excess_db",
        "tx_target_penetration_loss_db",
        "tx_target_shadow_loss_db",
        "tx_target_diffraction_loss_db",
        "tx_target_canyon_recovery_db",
        "tx_target_horizontal_pattern_loss_db",
        "tx_target_vertical_pattern_loss_db",
        "tx_target_obstacles_count",
        "tx_target_propagation_mode",
        "return_path_loss_db",
        "return_environment_excess_db",
        "total_bistatic_path_loss_db",
        "echo_power_dbm",
        "postprocessing_snr_db",
        "detection_margin_db",
        "doppler_hz",
        "detectable",
        "isac_quality_code",
    }
    assert required.issubset(arrays)
    assert all(len(arrays[name]) == len(grid.cell_lat) for name in required)


def test_nearest_target_api_returns_full_path_echo_doppler_and_quality(tmp_path, monkeypatch):
    monkeypatch.setenv("RF_CHANNEL_PRODUCT_DIR", str(tmp_path))
    info = write_channel_product(
        arrays={
            "latitude_deg": [38.0, 38.1],
            "longitude_deg": [-121.0, -121.1],
            "tx_target_path_loss_db": [100.0, 110.0],
            "return_path_loss_db": [120.0, 130.0],
            "total_bistatic_path_loss_db": [220.0, 240.0],
            "echo_power_dbm": [-130.0, -145.0],
            "postprocessing_snr_db": [20.0, 5.0],
            "detection_margin_db": [10.0, -5.0],
            "doppler_hz": [100.0, 25.0],
            "detectable": [True, False],
            "isac_quality_code": [5, 0],
        },
        metadata={
            "schema": "test",
            "schema_version": "2.0",
            "summary": {
                "transmitter": {"latitude": 37.9, "longitude": -121.0},
                "receiver": {"latitude": 38.2, "longitude": -121.2},
                "return_path_model": "environmental_reciprocal_grid",
                "isac_quality": {"code_legend": {"0": "below_required_snr", "5": "detectable_high_margin"}},
            },
            "array_units": {"echo_power_dbm": "dBm", "doppler_hz": "Hz"},
        },
    )
    response = TestClient(app).get(
        f"/api/channel-analysis/products/{info['product_id']}/nearest?lat=38.001&lon=-121.001"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["index"] == 0
    assert payload["inspection_source"] == "memory"
    assert payload["target"]["isac_quality_label"] == "detectable_high_margin"
    assert payload["metrics"]["total_bistatic_path_loss_db"] == pytest.approx(220.0)
    assert payload["metrics"]["echo_power_dbm"] == pytest.approx(-130.0)
    assert payload["metrics"]["doppler_hz"] == pytest.approx(100.0)
    assert payload["return_path_model"] == "environmental_reciprocal_grid"
