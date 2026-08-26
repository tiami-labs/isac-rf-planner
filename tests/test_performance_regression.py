"""Regression checks for allocation-only planner optimizations.

These tests intentionally compare optimized paths against the prior reference
semantics. They are small, deterministic guards that optimization must not alter
RF values, raster fill behavior, or the machine-readable product schema.
"""

from __future__ import annotations

import numpy as np

from agentic_rf_planner.agents.rf_planning_agent import (
    _channel_product_arrays,
    _iter_channel_product_arrays,
)
from agentic_rf_planner.geo.heatmap import _fill_nans_nearest
from agentic_rf_planner.pipeline.schemas import AttenuationGrid, LatLon, RFParams, WorldCell, WorldModel
from agentic_rf_planner.rf.attenuation_models import _compute_single_dvt_grid
from agentic_rf_planner.rf.dvt import DVTTransmitter
from agentic_rf_planner.rf.reciprocal_propagation import _cell_path_components


def _reference_fill_nans_nearest(arr: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Reference implementation used before destination-buffer reuse."""

    out = arr.copy()
    out[~mask] = np.nan
    for _ in range(256):
        need = np.isnan(out) & mask
        if not need.any():
            break
        filled_any = False
        tmp = out.copy()

        src = out[:-1, :]
        can = (np.isnan(out[1:, :]) & mask[1:, :]) & (np.isfinite(src) & mask[:-1, :])
        if np.any(can):
            tmp[1:, :][can] = src[can]
            filled_any = True

        src = out[1:, :]
        can = (np.isnan(out[:-1, :]) & mask[:-1, :]) & (np.isfinite(src) & mask[1:, :])
        if np.any(can):
            tmp[:-1, :][can] = src[can]
            filled_any = True

        src = out[:, :-1]
        can = (np.isnan(out[:, 1:]) & mask[:, 1:]) & (np.isfinite(src) & mask[:, :-1])
        if np.any(can):
            tmp[:, 1:][can] = src[can]
            filled_any = True

        src = out[:, 1:]
        can = (np.isnan(out[:, :-1]) & mask[:, :-1]) & (np.isfinite(src) & mask[:, 1:])
        if np.any(can):
            tmp[:, :-1][can] = src[can]
            filled_any = True

        out = tmp
        if not filled_any:
            break

    remain = np.isnan(out) & mask
    if remain.any():
        valid = out[np.isfinite(out) & mask]
        if valid.size:
            out[remain] = float(np.mean(valid))
    return out


def test_reused_wavefront_buffer_is_reference_exact():
    arr = np.full((13, 13), np.nan, dtype=np.float32)
    arr[2, 2] = -101.25
    arr[10, 9] = -74.5
    arr[6, 6] = -83.75
    mask = np.ones_like(arr, dtype=bool)
    mask[:2, 9:] = False
    mask[11:, :3] = False

    expected = _reference_fill_nans_nearest(arr, mask)
    actual = _fill_nans_nearest(arr, fill_mask=mask)
    assert np.array_equal(actual, expected, equal_nan=True)


def _small_grid() -> AttenuationGrid:
    grid = AttenuationGrid.model_construct(
        tx=None,
        rf_params=None,
        cell_lat=[],
        cell_lon=[],
        rsrp_dbm=[],
        sinr_db=[],
        modulation=[],
        throughput_mbps=[],
        serving_sector_id=[],
        interferer_count=[],
        top_interferer_rsrp_dbm=[],
        pilot_pollution_metric_db=[],
        technology="dvt",
    )
    names = (
        "cell_lat", "cell_lon", "received_power_dbm", "field_strength_dbuv_m",
        "carrier_to_noise_db", "terrain_loss_db", "los_terrain", "z_ground_m",
        "isac_incident_power_isotropic_dbm", "isac_tx_target_path_loss_db",
        "isac_tx_target_environment_loss_db", "isac_tx_target_terrain_loss_db",
        "isac_tx_target_los", "isac_tx_target_sample_error_m", "isac_echo_geometry_base_dbm",
        "bistatic_doppler_east_hz_per_mps", "bistatic_doppler_north_hz_per_mps",
        "bistatic_doppler_up_hz_per_mps", "bistatic_doppler_sensitivity_hz_per_mps",
        "bistatic_motion_doppler_sensitivity_hz_per_mps", "bistatic_minimum_detectable_speed_mps",
        "bistatic_echo_power_dbm", "bistatic_preprocessing_snr_db", "bistatic_postprocessing_snr_db",
        "bistatic_detection_margin_db", "bistatic_echo_to_residual_direct_db",
        "bistatic_direct_residual_margin_db", "bistatic_required_cancellation_db",
        "bistatic_required_dynamic_range_db", "bistatic_dynamic_range_margin_db",
        "bistatic_minimum_detectable_rcs_m2", "bistatic_rcs_margin_db",
        "bistatic_tx_target_range_m", "bistatic_target_receiver_range_m", "bistatic_path_range_m",
        "bistatic_excess_path_range_m", "bistatic_excess_delay_s", "bistatic_angle_deg",
        "bistatic_path_range_rate_mps", "bistatic_closing_speed_mps", "bistatic_doppler_hz",
        "bistatic_snr_noise_interference_ok", "bistatic_doppler_resolved", "bistatic_doppler_ambiguous",
        "bistatic_direct_residual_ok", "bistatic_dynamic_range_ok", "return_environment_valid",
        "bistatic_detectable_screening", "bistatic_detectable_qualified", "bistatic_detectable",
        "bistatic_constraint_failure_code", "return_path_loss_db", "return_environment_loss_db",
        "return_terrain_loss_db", "return_los", "return_terrain_state_code", "return_sample_error_m",
    )
    boolean = {
        "los_terrain", "isac_tx_target_los", "bistatic_snr_noise_interference_ok",
        "bistatic_doppler_resolved", "bistatic_doppler_ambiguous", "bistatic_direct_residual_ok",
        "bistatic_dynamic_range_ok", "return_environment_valid", "bistatic_detectable_screening",
        "bistatic_detectable_qualified", "bistatic_detectable", "return_los",
    }
    uint8 = {"bistatic_constraint_failure_code", "return_terrain_state_code"}
    for index, name in enumerate(names):
        if name in boolean:
            values = np.array([True, False, True], dtype=np.bool_)
        elif name in uint8:
            values = np.array([1, 2, 3], dtype=np.uint8)
        elif name in {"cell_lat", "cell_lon"}:
            values = np.array([38.1, 38.2, 38.3], dtype=np.float64) + index * 0.001
        else:
            values = np.array([index + 0.125, index + 1.25, index + 2.5], dtype=np.float32)
        grid.set_channel_array(name, values)
    return grid


def test_consuming_product_iterator_matches_legacy_mapping_exactly():
    reference_grid = _small_grid()
    reference = _channel_product_arrays(reference_grid)

    consuming_grid = _small_grid()
    generated_items = list(_iter_channel_product_arrays(consuming_grid, release_after_write=True))
    generated = dict(generated_items)

    assert list(reference) == [name for name, _ in generated_items]
    for name, expected in reference.items():
        actual = generated[name]
        if expected is None:
            assert actual is None
        else:
            assert np.array_equal(np.asarray(actual), np.asarray(expected), equal_nan=True)
    assert consuming_grid._channel_arrays == {}


def _dvt_world() -> WorldModel:
    dvt = DVTTransmitter.model_validate(
        {
            "fc": 587e6,
            "fs": 10e6,
            "bandwidth": 6e6,
            "waveform": "atsc1",
            "tx": {
                "latitude": 38.271667,
                "longitude": -121.506111,
                "altitude": 8.0,
                "antennaHeight": 300.0,
                "name": "KTEST",
            },
            "power": {"erpKw": 100.0, "polarization": "H"},
            "antenna": {"patternType": "omnidirectional", "verticalBeamwidthDeg": 180.0},
        }
    )
    rf = RFParams(
        technology="dvt",
        waveform="atsc1",
        dvt=dvt,
        freq_mhz=587.0,
        tx_power_dbm=80.0,
        rx_height_m=1000.0,
        terrain_enabled=True,
        path_loss_model="fspl",
    )
    cells = [
        WorldCell(
            lat=38.30,
            lon=-121.48,
            distance_m=4100.0,
            bearing_deg=32.0,
            dominant_material="unknown",
            obstacles_count=1,
            extra_loss_db=0.0,
            penetration_loss_db=4.25,
            shadow_loss_db=2.5,
            diffraction_loss_db=3.75,
            terrain_loss_db=1.5,
            canyon_recovery_db=0.75,
            is_los=False,
            los_terrain=False,
            z_ground_m=15.0,
            z_rx_abs_m=1015.0,
        ),
        WorldCell(
            lat=38.34,
            lon=-121.44,
            distance_m=9200.0,
            bearing_deg=54.0,
            dominant_material="unknown",
            obstacles_count=0,
            extra_loss_db=0.0,
            penetration_loss_db=0.5,
            shadow_loss_db=1.0,
            diffraction_loss_db=0.0,
            terrain_loss_db=2.0,
            canyon_recovery_db=0.25,
            is_los=True,
            los_terrain=True,
            z_ground_m=22.0,
            z_rx_abs_m=1022.0,
        ),
    ]
    return WorldModel(
        tx=LatLon(lat=dvt.tx.latitude, lon=dvt.tx.longitude),
        rf_params=rf,
        cells=cells,
        z_tx_ground_m=dvt.tx.altitude,
        z_tx_abs_m=dvt.tx.altitude + dvt.tx.antenna_height,
    )


def test_dvt_one_pass_path_capture_matches_reference_extraction_exactly():
    world = _dvt_world()
    expected_path, expected_environment, expected_terrain, expected_los, _ = _cell_path_components(world)

    grid, path, environment, los = _compute_single_dvt_grid(world, capture_path_components=True)

    assert np.array_equal(path, expected_path)
    assert np.array_equal(environment, expected_environment)
    assert np.array_equal(np.asarray(grid.terrain_loss_db, dtype=np.float32), expected_terrain)
    assert np.array_equal(los, expected_los)
