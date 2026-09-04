import math

import numpy as np
import pytest

from isac_rf_planner.geo.coverage_grid import build_coverage_grid
from isac_rf_planner.geo.terrain_propagation import compute_terrain_at_sample
from isac_rf_planner.pipeline.schemas import LatLon, MaterialType, RFParams, WorldCell, WorldModel
from isac_rf_planner.rf.attenuation_models import (
    _dvt_vertical_pattern_attenuation_absolute_db,
    _vertical_pattern_attenuation_db,
    compute_attenuation_grid,
)
from isac_rf_planner.rf.channel_analysis import ChannelAnalysisConfig
from isac_rf_planner.rf.dvt import DVTTransmitter


def dvt() -> DVTTransmitter:
    return DVTTransmitter.model_validate({
        "fc": 587e6,
        "fs": 10e6,
        "bandwidth": 6e6,
        "waveform": "atsc1",
        "tx": {"latitude": 38.271667, "longitude": -121.506111, "altitude": 0, "antennaHeight": 320, "name": "TX"},
        "power": {"erpHKw": 1000, "polarization": "H"},
        "antenna": {"patternType": "omnidirectional", "verticalBeamwidthDeg": 8, "maxVerticalAttenuationDb": 30},
    })


def channel(*, cancellation=60.0, external=None, doppler_rate=2000.0) -> ChannelAnalysisConfig:
    processing = {
        "coherentIntegrationS": 1.0,
        "processingLossDb": 3.0,
        "rangeWindowLossDb": 1.5,
        "dopplerWindowLossDb": 1.5,
        "systemLossDb": 3.0,
        "requiredSnrDb": 10.0,
        "directPathCancellationDb": cancellation,
        "dopplerSamplingRateHz": doppler_rate,
        "probabilityFalseAlarm": 1e-6,
        "requiredProbabilityDetection": 0.9,
        "ambiguitySidelobeFloorDb": 35.0,
        "clutterToNoiseDb": 0.0,
        "clutterDopplerSpreadHz": 5.0,
    }
    if external is not None:
        processing["externalInterferencePowerDbm"] = external
    return ChannelAnalysisConfig.model_validate({
        "receiver": {
            "latitude": 38.41717489626978,
            "longitude": -121.39508190497462,
            "altitudeMamsl": 0,
            "antennaHeightMagl": 10,
            "directAntennaGainDbi": 8,
            "echoAntennaGainDbi": 12,
            "feederLossDb": 1,
            "noiseFigureDb": 5,
        },
        "target": {
            "heightMagl": 1000,
            "bistaticRcsM2": 10,
            "rcsModel": "bistatic_angle_table",
            "bistaticAngleRcs": [
                {"angleDeg": 0, "rcsM2": 10},
                {"angleDeg": 90, "rcsM2": 2},
                {"angleDeg": 180, "rcsM2": 0.1},
            ],
        },
        "motion": {"speedMps": 50, "headingDegTrue": 0, "climbRateMps": 0},
        "processing": processing,
        "returnPathModel": "free_space_plus_excess",
        "environmentRequirement": "best_effort",
    })


def world(config: ChannelAnalysisConfig) -> WorldModel:
    rf = RFParams(
        technology="dvt", waveform="atsc1", dvt=dvt(), channel_analysis=config,
        freq_mhz=1, tx_power_dbm=0,
        terrain_enabled=(config.environment_requirement == "required"),
        path_loss_model="fspl",
        max_range_m=20_000, step_m=20, dtheta_deg=0.25,
    )
    cells = [
        WorldCell(lat=38.28, lon=-121.50, distance_m=1200, bearing_deg=25,
                  dominant_material=MaterialType.UNKNOWN, obstacles_count=0, extra_loss_db=0,
                  z_ground_m=0, terrain_state="los", los_terrain=True),
        WorldCell(lat=38.39, lon=-121.42, distance_m=17_000, bearing_deg=35,
                  dominant_material=MaterialType.UNKNOWN, obstacles_count=0, extra_loss_db=0,
                  z_ground_m=0, terrain_state="los", los_terrain=True),
    ]
    return WorldModel(tx=LatLon(lat=38.271667, lon=-121.506111), rf_params=rf, cells=cells,
                      z_tx_ground_m=0, z_tx_abs_m=320)


def test_vertical_pattern_penalizes_targets_above_transmitter_for_broadcast_and_nr():
    rf_dvt = RFParams(technology="dvt", waveform="atsc1", dvt=dvt(), freq_mhz=1, tx_power_dbm=0)
    loss = _dvt_vertical_pattern_attenuation_absolute_db(
        distance_m=20.0, tx_absolute_height_m=320.0, rx_absolute_height_m=1000.0, rf_params=rf_dvt
    )
    assert loss == pytest.approx(30.0)

    rf_nr = RFParams(freq_mhz=3500, tx_power_dbm=43, vertical_beamwidth_deg=8,
                     max_vertical_attenuation_db=30)
    nr_loss = _vertical_pattern_attenuation_db(20.0, 320.0, 1000.0, rf_nr)
    assert nr_loss == pytest.approx(30.0)


def test_terrain_clearance_sign_and_knife_edge_loss_are_physical():
    clear = compute_terrain_at_sample(
        profile_u_m=[0, 500, 1000], profile_z_dem_m=[0, 0, 0], sample_distance_m=1000,
        z_tx_abs_m=100, z_rx_abs_m=100, freq_mhz=587, clutter_height_m=0,
        k_factor=4/3, fresnel_min_clearance=0.6, loss_cap_db=40,
    )
    blocked = compute_terrain_at_sample(
        profile_u_m=[0, 500, 1000], profile_z_dem_m=[0, 120, 0], sample_distance_m=1000,
        z_tx_abs_m=100, z_rx_abs_m=100, freq_mhz=587, clutter_height_m=0,
        k_factor=4/3, fresnel_min_clearance=0.6, loss_cap_db=40,
    )
    assert clear.los_terrain is True and clear.terrain_loss_db == pytest.approx(0)
    assert clear.fresnel_clearance > 0
    assert blocked.los_terrain is False and blocked.fresnel_clearance < 0
    assert blocked.terrain_loss_db > 0


def test_full_bistatic_outputs_change_with_both_legs_angle_and_doppler_geometry():
    grid = compute_attenuation_grid(world(channel()))
    assert len(grid.bistatic_tx_target_range_m) == 2
    assert not np.isclose(grid.bistatic_target_receiver_range_m[0], grid.bistatic_target_receiver_range_m[1])
    assert not np.isclose(grid.bistatic_angle_deg[0], grid.bistatic_angle_deg[1])
    assert not np.isclose(grid.bistatic_doppler_hz[0], grid.bistatic_doppler_hz[1])
    assert not np.isclose(grid.bistatic_spatial_range_resolution_m[0], grid.bistatic_spatial_range_resolution_m[1])
    assert not np.isclose(grid.bistatic_rcs_m2[0], grid.bistatic_rcs_m2[1])
    assert not np.isclose(grid.bistatic_echo_power_dbm[0], grid.bistatic_echo_power_dbm[1])
    assert grid.channel_analysis_summary["architecture"] == "full_two_leg_environmental_bistatic_channel"


def test_residual_direct_and_external_interference_reduce_sinr_and_probability_detection():
    clean = compute_attenuation_grid(world(channel(cancellation=120.0)))
    impaired = compute_attenuation_grid(world(channel(cancellation=20.0, external=-70.0)))
    assert np.all(np.asarray(impaired.bistatic_postprocessing_sinr_db) < np.asarray(clean.bistatic_postprocessing_sinr_db))
    assert np.all(np.asarray(impaired.bistatic_probability_detection) <= np.asarray(clean.bistatic_probability_detection))


def test_missing_slow_time_sampling_never_claims_nonaliased_operational_detection():
    grid = compute_attenuation_grid(world(channel(doppler_rate=None)))
    assert not np.any(grid.bistatic_doppler_ambiguity_assessed)
    assert not np.any(grid.bistatic_detectable)
    assert grid.channel_analysis_summary["resolution"]["ambiguity_assessed"] is False


def test_required_environment_allows_explicitly_disabled_terrain():
    cfg = channel().model_copy(update={
        "environment_requirement": "required",
        "return_path_model": "environmental_reciprocal_grid",
    })
    rf = RFParams(
        technology="dvt", waveform="atsc1", dvt=dvt(), channel_analysis=cfg,
        freq_mhz=1, tx_power_dbm=0, terrain_enabled=False,
    )
    assert rf.terrain_enabled is False
    assert rf.channel_analysis.environment_requirement == "required"


def test_reciprocal_grid_clip_avoids_full_receiver_circle():
    rf = RFParams(freq_mhz=1000, tx_power_dbm=0, max_range_m=1000, step_m=100,
                  dtheta_deg=90, path_loss_model="fspl", terrain_enabled=False,
                  coverage_clip_center_lat=0.0, coverage_clip_center_lon=0.0045,
                  coverage_clip_radius_m=220)
    cells = build_coverage_grid(LatLon(lat=0, lon=0), rf, map_provider=None, terrain_provider=None)
    # Only the east-facing angular chord intersects the offset target AOI;
    # the full 360-degree/1000 m polar circle would contain about 2090 cells.
    bearings = {float(c.bearing_deg) for c in cells}
    assert min(bearings) > 60 and max(bearings) < 120
    assert len(cells) < 150


class _ZeroLossReturnLookup:
    def sample_components_many(self, latitudes, longitudes):
        shape = np.asarray(latitudes).shape
        zeros = np.zeros(shape, dtype=np.float32)
        return {
            "excess_loss_db": zeros,
            "penetration_loss_db": zeros,
            "shadow_loss_db": zeros,
            "diffraction_loss_db": zeros,
            "terrain_loss_db": zeros,
            "canyon_recovery_db": zeros,
            "propagation_mode_code": np.ones(shape, dtype=np.uint8),
            "valid": np.ones(shape, dtype=np.bool_),
        }

    def sample(self, latitude, longitude):
        return 0.0, 1.0, 0.0, True

    def metadata(self):
        return {
            "model": "test_environmental_reciprocal_grid",
            "valid_samples": 2,
            "total_samples": 2,
            "radial_resolution_m": 20.0,
            "bearing_resolution_deg": 0.25,
        }


def _operational_channel() -> ChannelAnalysisConfig:
    return ChannelAnalysisConfig.model_validate({
        "receiver": {
            "latitude": 38.41717489626978,
            "longitude": -121.39508190497462,
            "antennaHeightMagl": 10,
            "directAntennaGainDbi": 8,
            "echoAntennaGainDbi": 12,
            "feederLossDb": 1,
            "noiseFigureDb": 5,
        },
        "target": {
            "heightMagl": 1000,
            "rcsModel": "bistatic_angle_table",
            "bistaticRcsM2": 10,
            "bistaticAngleRcs": [
                {"angleDeg": 0, "rcsM2": 10},
                {"angleDeg": 90, "rcsM2": 2},
                {"angleDeg": 180, "rcsM2": 0.1},
            ],
        },
        "motion": {"speedMps": 50, "headingDegTrue": 0},
        "processing": {
            "coherentIntegrationS": 1,
            "processingGainMode": "configured",
            "configuredProcessingGainDb": 45,
            "processingLossDb": 3,
            "systemLossDb": 3,
            "requiredSinrDb": 10,
            "directPathCancellationDb": 100,
            "directPathCancellationBasis": "measured",
            "dopplerSamplingRateHz": 2000,
            "ambiguityModel": "configured_floor",
            "ambiguitySidelobeFloorDb": 70,
            "interferenceModelMode": "configured",
            "clutterToNoiseDb": -20,
            "clutterDopplerSpreadHz": 5,
            "externalInterferencePowerDbm": -150,
            "probabilityFalseAlarm": 1e-6,
            "requiredProbabilityDetection": 0.9,
        },
        "returnPathModel": "environmental_reciprocal_grid",
        "environmentRequirement": "required",
    })


def test_required_two_leg_model_is_valid_without_terrain_when_terrain_is_disabled_by_request():
    cfg = _operational_channel().model_copy(update={
        "environment_requirement": "required",
        "return_path_model": "environmental_reciprocal_grid",
    })
    rf = RFParams(
        technology="dvt", waveform="atsc1", dvt=dvt(), channel_analysis=cfg,
        freq_mhz=1, tx_power_dbm=0, terrain_enabled=False, path_loss_model="fspl",
        max_range_m=20_000, step_m=20, dtheta_deg=0.25,
    )
    w = world(cfg)
    w.rf_params = rf
    grid = compute_attenuation_grid(
        w, channel_context={"return_path_lookup": _ZeroLossReturnLookup()}
    )
    assert np.all(grid.bistatic_forward_environment_valid)
    assert np.all(grid.bistatic_return_environment_valid)
    assert np.all(grid.bistatic_environment_valid)
    assert grid.channel_analysis_summary["environment_basis"]["terrain_requested"] is False
    assert grid.channel_analysis_summary["environment_basis"]["terrain_is_required_for_channel_analysis"] is False


def test_operational_detectability_is_gated_by_calibrated_two_leg_models():
    cfg = _operational_channel()
    rf = RFParams(
        technology="dvt", waveform="atsc1", dvt=dvt(), channel_analysis=cfg,
        freq_mhz=1, tx_power_dbm=0, terrain_enabled=True, path_loss_model="fspl",
        max_range_m=20_000, step_m=20, dtheta_deg=0.25,
    )
    w = world(cfg)
    w.rf_params = rf
    grid = compute_attenuation_grid(
        w, channel_context={"return_path_lookup": _ZeroLossReturnLookup()}
    )
    assert np.all(grid.bistatic_environment_valid)
    assert np.all(grid.bistatic_rcs_model_valid)
    assert np.all(grid.bistatic_processing_model_valid)
    assert np.all(grid.bistatic_ambiguity_model_valid)
    assert np.all(grid.bistatic_interference_model_valid)
    assert np.all(grid.bistatic_cancellation_model_valid)
    assert grid.channel_analysis_summary["processing_budget"]["required_postprocessing_sinr_db"] == 10


def test_default_upper_bound_models_never_claim_operational_detectability():
    grid = compute_attenuation_grid(world(channel()))
    assert not np.any(grid.bistatic_detectable)
    assert not np.any(grid.bistatic_processing_model_valid)
    assert not np.any(grid.bistatic_ambiguity_model_valid)
    assert not np.any(grid.bistatic_interference_model_valid)
    assert not np.any(grid.bistatic_cancellation_model_valid)


def test_polar_sampling_exports_true_location_dependent_cross_range_spacing():
    grid = compute_attenuation_grid(world(channel()))
    radial = np.asarray(grid.bistatic_sample_radial_spacing_m)
    cross = np.asarray(grid.bistatic_sample_cross_range_spacing_m)
    assert np.allclose(radial, 20.0)
    assert cross[1] > cross[0]
    from isac_rf_planner.geo.google_mesh.utils import haversine_m
    target_surface_range_m = haversine_m(
        LatLon(lat=38.271667, lon=-121.506111),
        LatLon(lat=float(grid.cell_lat[1]), lon=float(grid.cell_lon[1])),
    )
    assert cross[1] == pytest.approx(
        max(20.0, target_surface_range_m * math.radians(0.25)), rel=0.02
    )


def test_thermal_noise_uses_exact_ktb_reference_at_290k():
    from isac_rf_planner.rf.channel_analysis import thermal_noise_power_dbm

    one_hz = thermal_noise_power_dbm(1.0, 0.0, 290.0)
    assert one_hz == pytest.approx(-173.975, abs=0.01)
    six_mhz_nf5 = thermal_noise_power_dbm(6.0e6, 5.0, 290.0)
    assert six_mhz_nf5 == pytest.approx(one_hz + 10.0 * math.log10(6.0e6) + 5.0, abs=0.01)


def test_detector_cell_clutter_is_not_given_signal_processing_gain_twice():
    cfg = _operational_channel()
    processing = cfg.processing.model_copy(update={
        "configured_processing_gain_db": 60.0,
        "clutter_to_noise_db": 10.0,
        "clutter_doppler_spread_hz": 1.0e9,
        "external_interference_power_dbm": None,
    })
    cfg = cfg.model_copy(update={
        "processing": processing,
        "motion": cfg.motion.model_copy(update={"speed_mps": 0.0}),
    })
    rf = RFParams(
        technology="dvt", waveform="atsc1", dvt=dvt(), channel_analysis=cfg,
        freq_mhz=1, tx_power_dbm=0, terrain_enabled=True, path_loss_model="fspl",
        max_range_m=20_000, step_m=20, dtheta_deg=0.25,
    )
    w = world(cfg)
    w.rf_params = rf
    grid = compute_attenuation_grid(
        w, channel_context={"return_path_lookup": _ZeroLossReturnLookup()}
    )
    summary = grid.channel_analysis_summary
    noise = summary["direct_path"]["noise_power_dbm"]
    clutter = np.asarray(grid.bistatic_clutter_at_detector_dbm, dtype=float)
    # At zero Doppler and a very broad clutter spectrum, detector-cell clutter
    # is exactly the configured C/N above kTB, independent of signal gain.
    assert np.allclose(clutter, noise + 10.0, atol=1.0e-5)


def test_broadcast_preliminary_source_power_uses_signed_target_elevation_pattern():
    rf = RFParams(
        technology="dvt", waveform="atsc1", dvt=dvt(),
        freq_mhz=1, tx_power_dbm=0, path_loss_model="fspl",
        max_range_m=20, step_m=20, dtheta_deg=360,
        rx_height_m=1000, site_altitude_m=0, terrain_enabled=False,
        height_aware_obstructions=True,
    )
    cells = build_coverage_grid(
        LatLon(lat=38.271667, lon=-121.506111),
        rf,
        map_provider=None,
        terrain_provider=None,
    )
    assert cells
    # With a target almost directly above the 320 m broadcast antenna, the
    # 8-degree vertical HPBW model must apply the configured 30 dB cap.
    w = WorldModel(
        tx=LatLon(lat=38.271667, lon=-121.506111),
        rf_params=rf,
        cells=[cells[0]],
        z_tx_ground_m=0.0,
        z_tx_abs_m=320.0,
    )
    result = compute_attenuation_grid(w)
    assert result.tx_target_vertical_pattern_loss_db[0] == pytest.approx(30.0)


def test_tabulated_ambiguity_surface_varies_by_delay_and_doppler_and_does_not_extrapolate():
    from isac_rf_planner.rf.channel_analysis import (
        AmbiguitySurfacePoint,
        ambiguity_response_power_db,
    )

    surface = [
        AmbiguitySurfacePoint.model_validate({"delayS": 0.0, "dopplerHz": 0.0, "responseDb": 0.0}),
        AmbiguitySurfacePoint.model_validate({"delayS": 10e-6, "dopplerHz": 0.0, "responseDb": -40.0}),
        AmbiguitySurfacePoint.model_validate({"delayS": 0.0, "dopplerHz": 100.0, "responseDb": -30.0}),
        AmbiguitySurfacePoint.model_validate({"delayS": 10e-6, "dopplerHz": 100.0, "responseDb": -60.0}),
    ]
    response, valid = ambiguity_response_power_db(
        excess_delay_s=np.asarray([0.0, 5e-6, 10e-6, 20e-6]),
        doppler_hz=np.asarray([0.0, 50.0, 100.0, 50.0]),
        bandwidth_hz=6e6,
        integration_s=1.0,
        model="tabulated_surface",
        sidelobe_floor_db=0.0,
        surface_points=surface,
    )
    assert valid.tolist() == [True, True, True, False]
    assert response[0] == pytest.approx(0.0)
    assert response[2] == pytest.approx(-60.0)
    assert -60.0 < response[1] < 0.0
    assert np.isnan(response[3])


def test_tabulated_ambiguity_surface_rejects_collinear_or_incomplete_data():
    from isac_rf_planner.rf.channel_analysis import ChannelProcessing

    with pytest.raises(ValueError, match="at least three"):
        ChannelProcessing.model_validate({
            "ambiguityModel": "tabulated_surface",
            "ambiguitySurface": [
                {"delayS": 0.0, "dopplerHz": 0.0, "responseDb": 0.0},
                {"delayS": 1e-6, "dopplerHz": 10.0, "responseDb": -20.0},
            ],
        })
    with pytest.raises(ValueError, match="not be collinear"):
        ChannelProcessing.model_validate({
            "ambiguityModel": "tabulated_surface",
            "ambiguitySurface": [
                {"delayS": 0.0, "dopplerHz": 0.0, "responseDb": 0.0},
                {"delayS": 1e-6, "dopplerHz": 10.0, "responseDb": -20.0},
                {"delayS": 2e-6, "dopplerHz": 20.0, "responseDb": -40.0},
            ],
        })
