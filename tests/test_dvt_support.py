"""DVT transmitter, broadcast-pattern, and link-budget regression tests."""

import asyncio
import logging
import math

import pytest

from agentic_rf_planner.api import rest as rest_api
from agentic_rf_planner.api.rest import PlanRequest
from agentic_rf_planner.geo.osm_map_provider import (
    _parse_overpass_elements,
    _tile_centers_for_square,
)
from agentic_rf_planner.pipeline.schemas import LatLon, RFParams, WorldCell, WorldModel
from agentic_rf_planner.rf.attenuation_models import compute_attenuation_grid
from agentic_rf_planner.rf.dvt import DVTTransmitter, erp_kw_to_eirp_dbm


def _dvt_payload(*, power_overrides=None, antenna_overrides=None, **tx_overrides):
    tx = {
        "latitude": 37.0,
        "longitude": -122.0,
        "altitude": 125.0,
        "antennaHeight": 180.0,
        "name": "KTEST",
    }
    tx.update(tx_overrides)
    antenna = {
        "patternType": "parametric",
        "mainAzimuthDeg": 90.0,
        "horizontalBeamwidthDeg": 60.0,
        "verticalBeamwidthDeg": 12.0,
        "beamTiltDeg": 0.0,
        "maxHorizontalAttenuationDb": 30.0,
        "frontToBackAttenuationDb": 25.0,
        "maxVerticalAttenuationDb": 30.0,
    }
    if antenna_overrides:
        antenna.update(antenna_overrides)
    power = {"erpKw": 100.0, "polarization": "DA (E)"}
    if power_overrides:
        if any(k in power_overrides for k in ("conductedPowerKw", "erpHKw", "erpVKw")) and "erpKw" not in power_overrides:
            power.pop("erpKw", None)
        power.update(power_overrides)
    return {
        "fc": 587_000_000.0,
        "fs": 10_000_000.0,
        "bandwidth": 6_000_000.0,
        "waveform": "atsc3",
        "tx": tx,
        "antenna": antenna,
        "power": power,
        "station": {
            "callSign": "KTEST",
            "virtualChannel": "5.1",
            "rfChannel": "33",
            "physicalChannel": "33",
            "facilityId": "12345",
            "city": "Test City",
            "network": "Test Network",
            "standard": "ATSC 3.0",
            "band": "UHF",
            "source": "fixture",
            "sourceUrl": "https://example.invalid/station",
        },
    }


def _world_for(dvt: DVTTransmitter, bearing_deg: float = 90.0) -> WorldModel:
    rf = RFParams(
        technology="dvt",
        dvt=dvt,
        freq_mhz=1.0,
        tx_power_dbm=0.0,
        path_loss_model="3gpp_38901",  # DVT must not enter the NR model.
        tx_antenna_gain_dbi=99.0,
        tx_feeder_loss_db=77.0,
        reference_signal_offset_db=-55.0,
        max_rsrp_dbm=-62.0,
    )
    return WorldModel(
        tx=LatLon(lat=dvt.tx.latitude, lon=dvt.tx.longitude),
        rf_params=rf,
        cells=[
            WorldCell(
                lat=37.001,
                lon=-121.999,
                distance_m=1000.0,
                bearing_deg=bearing_deg,
                dominant_material="unknown",
                obstacles_count=0,
                extra_loss_db=0.0,
            )
        ],
        z_tx_ground_m=dvt.tx.altitude,
        z_tx_abs_m=dvt.tx.altitude + dvt.tx.antenna_height,
    )


def test_plan_request_accepts_nested_dvt_coordinates_and_defaults():
    req = PlanRequest.model_validate({"dvt": _dvt_payload()})
    assert req.technology == "dvt"
    assert req.lat == 37.0
    assert req.lon == -122.0


def test_rf_params_keep_broadcast_pattern_separate_from_nr_sector_fields():
    dvt = DVTTransmitter.model_validate(_dvt_payload())
    rf = RFParams(technology="dvt", dvt=dvt, freq_mhz=1.0, tx_power_dbm=0.0)

    assert rf.freq_mhz == pytest.approx(587.0)
    assert rf.channel_bandwidth_mhz == pytest.approx(6.0)
    assert rf.tx_height_m == pytest.approx(180.0)
    assert rf.site_altitude_m == pytest.approx(125.0)
    assert rf.tx_power_dbm == pytest.approx(80.0)  # 100 kW ERP
    assert rf.sectors is None
    assert rf.azimuth_deg == pytest.approx(0.0)  # NR default remains untouched.
    assert rf.horizontal_beamwidth_deg == pytest.approx(360.0)
    assert dvt.antenna.main_azimuth_deg == pytest.approx(90.0)
    assert dvt.antenna.horizontal_beamwidth_deg == pytest.approx(60.0)
    assert rf.coverage_display_layer == "field_strength"


def test_legacy_tx_pattern_is_migrated_to_broadcast_antenna_object():
    payload = _dvt_payload()
    payload.pop("antenna")
    payload["tx"].update(
        {
            "azimuthDeg": 90.0,
            "beamwidthHDeg": 60.0,
            "beamwidthVDeg": 12.0,
            "electricalTiltDeg": 1.5,
        }
    )
    dvt = DVTTransmitter.model_validate(payload)

    assert dvt.tx.model_dump(by_alias=True) == {
        "latitude": 37.0,
        "longitude": -122.0,
        "altitude": 125.0,
        "antennaHeight": 180.0,
        "name": "KTEST",
    }
    assert dvt.antenna.pattern_type == "parametric"
    assert dvt.antenna.main_azimuth_deg == pytest.approx(90.0)
    assert dvt.antenna.beam_tilt_deg == pytest.approx(1.5)


def test_dvt_erp_uses_direct_radiated_power_without_nr_re_spreading():
    dvt = DVTTransmitter.model_validate(
        _dvt_payload(antenna_overrides={"patternType": "omnidirectional", "verticalBeamwidthDeg": 180.0})
    )
    world = _world_for(dvt)
    grid = compute_attenuation_grid(world)

    d3_m = math.sqrt(1000.0**2 + (180.0 - 1.5) ** 2)
    fspl_db = 32.45 + 20.0 * math.log10(d3_m / 1000.0) + 20.0 * math.log10(587.0)
    expected_dbm = erp_kw_to_eirp_dbm(100.0) - fspl_db

    assert grid.technology == "dvt"
    assert grid.waveform == "atsc3"
    assert grid.received_power_dbm[0] == pytest.approx(expected_dbm, abs=0.05)
    assert grid.rsrp_dbm[0] == pytest.approx(expected_dbm, abs=0.05)
    assert grid.modulation == ["atsc3"]
    assert grid.throughput_mbps == [0.0]
    assert grid.serving_sector_id == []
    assert grid.rsrp_by_sector is None
    assert grid.received_power_dbm[0] > -62.0


def test_dvt_conducted_power_applies_tx_gain_feeder_loss_and_antenna_gain():
    dvt = DVTTransmitter.model_validate(
        _dvt_payload(
            power_overrides={
                "conductedPowerKw": 10.0,
                "txGainDb": 1.5,
                "feederLossDb": 2.0,
                "antennaGainDbi": 12.0,
            },
            antenna_overrides={"patternType": "omnidirectional", "verticalBeamwidthDeg": 180.0},
        )
    )
    world = _world_for(dvt)
    grid = compute_attenuation_grid(world)

    d3_m = math.sqrt(1000.0**2 + (180.0 - 1.5) ** 2)
    fspl_db = 32.45 + 20.0 * math.log10(d3_m / 1000.0) + 20.0 * math.log10(587.0)
    conducted_dbm = 10.0 * math.log10(10.0 * 1.0e6)
    expected_dbm = conducted_dbm + 1.5 - 2.0 + 12.0 - fspl_db

    assert world.rf_params.tx_chain_gain_db == pytest.approx(1.5)
    assert world.rf_params.tx_feeder_loss_db == pytest.approx(2.0)
    assert world.rf_params.tx_antenna_gain_dbi == pytest.approx(12.0)
    assert grid.received_power_dbm[0] == pytest.approx(expected_dbm, abs=0.05)


def test_dvt_physical_chain_terms_have_exact_db_effects():
    base_power = {
        "conductedPowerKw": 10.0,
        "txGainDb": 0.0,
        "feederLossDb": 0.0,
        "antennaGainDbi": 0.0,
    }

    def power_with(**overrides):
        cfg = dict(base_power)
        cfg.update(overrides)
        dvt = DVTTransmitter.model_validate(
            _dvt_payload(
                power_overrides=cfg,
                antenna_overrides={"patternType": "omnidirectional", "verticalBeamwidthDeg": 180.0},
            )
        )
        return compute_attenuation_grid(_world_for(dvt)).received_power_dbm[0]

    baseline = power_with()
    assert power_with(txGainDb=3.0) - baseline == pytest.approx(3.0, abs=1.0e-6)
    assert power_with(antennaGainDbi=7.0) - baseline == pytest.approx(7.0, abs=1.0e-6)
    assert power_with(feederLossDb=2.5) - baseline == pytest.approx(-2.5, abs=1.0e-6)


def test_dvt_frequency_and_bandwidth_remain_in_physics():
    base_payload = _dvt_payload(
        antenna_overrides={"patternType": "omnidirectional", "verticalBeamwidthDeg": 180.0}
    )
    base = compute_attenuation_grid(_world_for(DVTTransmitter.model_validate(base_payload)))

    high_frequency_payload = dict(base_payload)
    high_frequency_payload["fc"] = 800_000_000.0
    high_frequency = compute_attenuation_grid(
        _world_for(DVTTransmitter.model_validate(high_frequency_payload))
    )
    assert high_frequency.received_power_dbm[0] - base.received_power_dbm[0] == pytest.approx(
        -20.0 * math.log10(800.0 / 587.0), abs=0.02
    )

    wide_payload = dict(base_payload)
    wide_payload["bandwidth"] = 9_000_000.0
    wide_payload["fs"] = 12_000_000.0
    wide = compute_attenuation_grid(_world_for(DVTTransmitter.model_validate(wide_payload)))
    assert wide.sinr_db[0] - base.sinr_db[0] == pytest.approx(
        -10.0 * math.log10(9.0 / 6.0), abs=0.02
    )


def test_dvt_rejects_double_counted_erp_and_component_chain():
    with pytest.raises(ValueError, match="use conductedPowerKw"):
        DVTTransmitter.model_validate(_dvt_payload(power_overrides={"antennaGainDbi": 10.0}))


def test_parametric_broadcast_pattern_penalizes_back_direction_without_sectors():
    dvt = DVTTransmitter.model_validate(
        _dvt_payload(antenna_overrides={"verticalBeamwidthDeg": 180.0})
    )
    on_axis = compute_attenuation_grid(_world_for(dvt, bearing_deg=90.0)).received_power_dbm[0]
    back = compute_attenuation_grid(_world_for(dvt, bearing_deg=270.0)).received_power_dbm[0]

    assert on_axis > back
    assert on_axis - back >= 25.0


def test_tabulated_broadcast_pattern_rotation_and_circular_interpolation():
    dvt = DVTTransmitter.model_validate(
        _dvt_payload(
            antenna_overrides={
                "patternType": "tabulated",
                "rotationDeg": 30.0,
                "verticalBeamwidthDeg": 180.0,
                "azimuthPattern": [
                    {"azimuthDeg": 0.0, "relativeField": 1.0},
                    {"azimuthDeg": 90.0, "relativeField": 0.5},
                    {"azimuthDeg": 180.0, "relativeField": 0.1},
                    {"azimuthDeg": 270.0, "relativeField": 0.5},
                ],
            }
        )
    )
    peak = compute_attenuation_grid(_world_for(dvt, bearing_deg=30.0)).received_power_dbm[0]
    null = compute_attenuation_grid(_world_for(dvt, bearing_deg=210.0)).received_power_dbm[0]
    near_wrap = compute_attenuation_grid(_world_for(dvt, bearing_deg=20.0)).received_power_dbm[0]

    assert peak - null == pytest.approx(20.0, abs=0.05)
    assert peak > near_wrap > null


def test_elliptical_h_v_erp_components_are_combined_after_component_patterns():
    dvt = DVTTransmitter.model_validate(
        _dvt_payload(
            power_overrides={"erpHKw": 1000.0, "erpVKw": 250.0},
            antenna_overrides={
                "patternType": "tabulated",
                "verticalBeamwidthDeg": 180.0,
                "azimuthPattern": [
                    {"azimuthDeg": 0.0, "relativeFieldH": 1.0, "relativeFieldV": 0.5},
                    {"azimuthDeg": 180.0, "relativeFieldH": 0.1, "relativeFieldV": 1.0},
                ],
            },
        )
    )
    at_zero = dvt.effective_source_eirp_dbm(0.0)
    expected_kw = 1000.0 * 1.0**2 + 250.0 * 0.5**2
    assert at_zero == pytest.approx(erp_kw_to_eirp_dbm(expected_kw), abs=1.0e-6)


def test_dvt_rejects_cellular_sector_configuration():
    dvt = DVTTransmitter.model_validate(_dvt_payload())
    with pytest.raises(ValueError, match="broadcast antenna radiation pattern, not cellular sectors"):
        RFParams(
            technology="dvt",
            dvt=dvt,
            freq_mhz=1.0,
            tx_power_dbm=0.0,
            sectors=[{"sector_id": "not-valid-for-dvt"}],
        )


def test_large_osm_region_is_partitioned_into_4_by_4_tiles():
    tiles = _tile_centers_for_square(LatLon(lat=37.0, lon=-122.0), 20_000.0)
    assert len(tiles) == 16
    assert all(half_size == pytest.approx(5000.0) for _, half_size in tiles)


def test_osm_relation_outer_members_are_stitched_into_polygon():
    data = {
        "elements": [
            {
                "type": "relation",
                "id": 42,
                "tags": {"building": "yes"},
                "members": [
                    {
                        "role": "outer",
                        "geometry": [
                            {"lat": 0.0, "lon": 0.0},
                            {"lat": 0.0, "lon": 1.0},
                            {"lat": 1.0, "lon": 1.0},
                        ],
                    },
                    {
                        "role": "outer",
                        "geometry": [
                            {"lat": 1.0, "lon": 1.0},
                            {"lat": 1.0, "lon": 0.0},
                            {"lat": 0.0, "lon": 0.0},
                        ],
                    },
                ],
            }
        ]
    }
    buildings, landuse = _parse_overpass_elements(data)
    assert len(buildings) == 1
    assert landuse == []
    assert buildings[0]["geometry"][0] == buildings[0]["geometry"][-1]


def test_plan_request_exposes_matching_top_level_waveform():
    req = PlanRequest.model_validate({"waveform": "atsc3", "dvt": _dvt_payload()})
    assert req.technology == "dvt"
    assert req.waveform == "atsc3"


def test_plan_request_rejects_waveform_mismatch():
    with pytest.raises(ValueError, match="waveform must match dvt.waveform"):
        PlanRequest.model_validate({"waveform": "dvbt", "dvt": _dvt_payload()})


def test_dvt_rejects_non_television_center_frequency():
    payload = _dvt_payload()
    payload["fc"] = 3_500_000_000.0
    with pytest.raises(ValueError, match="between 40 MHz and 1000 MHz"):
        DVTTransmitter.model_validate(payload)


def test_dvt_request_rejects_explicit_nr_fields():
    with pytest.raises(ValueError, match="NR-only top-level fields: freq_mhz, tx_power_dbm"):
        PlanRequest.model_validate(
            {
                "waveform": "atsc3",
                "dvt": _dvt_payload(),
                "freq_mhz": 3500.0,
                "tx_power_dbm": 43.0,
            }
        )


def test_dvt_public_config_contains_no_nr_contract_fields():
    dvt = DVTTransmitter.model_validate(_dvt_payload())
    rf = RFParams(technology="dvt", dvt=dvt, freq_mhz=1.0, tx_power_dbm=0.0)
    public = rf.public_config()

    for key in (
        "freq_mhz",
        "tx_power_dbm",
        "subcarrier_spacing_khz",
        "num_resource_blocks",
        "mimo_mode",
        "enable_link_adaptation",
        "reference_signal_offset_db",
        "max_rsrp_dbm",
        "termination_rsrp_dbm",
        "path_loss_model",
        "propagation_scenario",
        "sectors",
    ):
        assert key not in public
    assert public["center_frequency_mhz"] == pytest.approx(587.0)
    assert public["termination_power_dbm"] == pytest.approx(-140.0)
    assert public["dvt"]["antenna"]["patternType"] == "parametric"
    assert "azimuthDeg" not in public["dvt"]["tx"]


def test_dvt_api_logging_contains_broadcast_antenna_and_no_nr_runtime_sections(monkeypatch, caplog):
    captured = {}

    def fake_run_rf_planning_for_point(*, lat, lon, rf_params, progress_cb=None):
        captured["rf_params"] = rf_params
        return {
            "ray_mode": rf_params.ray_mode,
            "rf_config_used": rf_params.public_config(),
            "grid": {"technology": "dvt", "waveform": str(rf_params.dvt.waveform)},
        }

    monkeypatch.setattr(rest_api, "run_rf_planning_for_point", fake_run_rf_planning_for_point)
    req = PlanRequest.model_validate(
        {
            "waveform": "atsc3",
            "dvt": _dvt_payload(),
            "termination_power_dbm": -125.0,
            "publish_ui": False,
        }
    )

    caplog.set_level(logging.INFO, logger="agentic_rf_planner.api.rest")
    asyncio.run(rest_api.api_plan(req))
    messages = "\n".join(record.getMessage() for record in caplog.records)

    assert "DVT RFParams: waveform=atsc3" in messages
    assert "fc=587.000000MHz" in messages
    assert "DVT broadcast antenna: pattern=parametric" in messages
    assert "OFDM:" not in messages
    assert "MIMO:" not in messages
    assert "Link adaptation:" not in messages
    assert "Sectors:" not in messages
    assert "freq_mhz: 3500" not in messages
    assert "tx_power_dbm: 43" not in messages
    assert captured["rf_params"].termination_rsrp_dbm == pytest.approx(-125.0)


def test_5g_request_resolves_explicit_waveform():
    req = PlanRequest.model_validate({"lat": 37.0, "lon": -122.0, "waveform": "5g_nr"})
    assert req.technology == "5g_nr"
    assert req.waveform == "5g_nr"
