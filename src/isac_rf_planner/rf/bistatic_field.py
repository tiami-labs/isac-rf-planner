"""Chunked numerical kernel for reusable bistatic/ISAC capability fields.

The expensive physical scene (TX illumination + reciprocal RX propagation) is
computed elsewhere.  This module fuses those scene fields with a target/process
hypothesis using bounded NumPy chunks.  It also emits target-independent Doppler
sensitivity bases so RCS/motion/processing changes can be reevaluated without
rebuilding OSM/terrain propagation.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Sequence

import numpy as np

from .channel_analysis import (
    SPEED_OF_LIGHT_M_S,
    WGS84_A_M,
    WGS84_E2,
    ChannelAnalysisConfig,
    coherent_processing_gain_db,
    thermal_noise_power_dbm,
)
from .channel_arrays import encode_terrain_states, terrain_state_encoding_metadata


_CHUNK_POINTS = 131_072

# Compact failure bits exported for machine-readable diagnosis.
FAIL_THERMAL_SNR = np.uint8(1)
FAIL_DOPPLER_RESOLUTION = np.uint8(2)
FAIL_DOPPLER_AMBIGUITY = np.uint8(4)
FAIL_DIRECT_RESIDUAL = np.uint8(8)
FAIL_DYNAMIC_RANGE = np.uint8(16)
FAIL_PROCESSING_QUALIFICATION = np.uint8(32)
FAIL_ENVIRONMENT_RETURN = np.uint8(64)


@dataclass(slots=True)
class BistaticFieldArrays:
    # Reusable scene/hypothesis basis.
    echo_geometry_base_dbm: np.ndarray
    doppler_east_hz_per_mps: np.ndarray
    doppler_north_hz_per_mps: np.ndarray
    doppler_up_hz_per_mps: np.ndarray
    doppler_sensitivity_hz_per_mps: np.ndarray
    motion_doppler_sensitivity_hz_per_mps: np.ndarray
    minimum_detectable_speed_mps: np.ndarray

    # Current target/process hypothesis.
    echo_power_dbm: np.ndarray
    preprocessing_snr_db: np.ndarray
    postprocessing_snr_db: np.ndarray
    detection_margin_db: np.ndarray
    echo_to_residual_direct_db: np.ndarray
    direct_residual_margin_db: np.ndarray
    required_cancellation_db: np.ndarray
    required_dynamic_range_db: np.ndarray
    dynamic_range_margin_db: np.ndarray
    minimum_detectable_rcs_m2: np.ndarray
    rcs_margin_db: np.ndarray

    # Geometry and kinematics.
    tx_target_range_m: np.ndarray
    target_receiver_range_m: np.ndarray
    bistatic_path_range_m: np.ndarray
    excess_path_range_m: np.ndarray
    excess_delay_s: np.ndarray
    bistatic_angle_deg: np.ndarray
    path_range_rate_mps: np.ndarray
    closing_speed_mps: np.ndarray
    doppler_hz: np.ndarray

    # Constraint masks.
    thermal_snr_ok: np.ndarray
    doppler_resolved: np.ndarray
    doppler_ambiguous: np.ndarray
    direct_residual_ok: np.ndarray
    dynamic_range_ok: np.ndarray
    return_environment_valid: np.ndarray
    detectable_screening: np.ndarray
    detectable_qualified: np.ndarray
    detectable: np.ndarray
    constraint_failure_code: np.ndarray

    # Reciprocal environmental field.
    return_path_loss_db: np.ndarray
    return_environment_loss_db: np.ndarray
    return_terrain_loss_db: np.ndarray
    return_los: np.ndarray
    return_terrain_state_code: np.ndarray
    return_sample_error_m: np.ndarray

    def mapping(self) -> dict[str, np.ndarray]:
        """Names match AttenuationGrid/internal channel fields."""

        return {
            "isac_echo_geometry_base_dbm": self.echo_geometry_base_dbm,
            "bistatic_doppler_east_hz_per_mps": self.doppler_east_hz_per_mps,
            "bistatic_doppler_north_hz_per_mps": self.doppler_north_hz_per_mps,
            "bistatic_doppler_up_hz_per_mps": self.doppler_up_hz_per_mps,
            "bistatic_doppler_sensitivity_hz_per_mps": self.doppler_sensitivity_hz_per_mps,
            "bistatic_motion_doppler_sensitivity_hz_per_mps": self.motion_doppler_sensitivity_hz_per_mps,
            "bistatic_minimum_detectable_speed_mps": self.minimum_detectable_speed_mps,
            "bistatic_echo_power_dbm": self.echo_power_dbm,
            "bistatic_preprocessing_snr_db": self.preprocessing_snr_db,
            "bistatic_postprocessing_snr_db": self.postprocessing_snr_db,
            "bistatic_detection_margin_db": self.detection_margin_db,
            "bistatic_echo_to_residual_direct_db": self.echo_to_residual_direct_db,
            "bistatic_direct_residual_margin_db": self.direct_residual_margin_db,
            "bistatic_required_cancellation_db": self.required_cancellation_db,
            "bistatic_required_dynamic_range_db": self.required_dynamic_range_db,
            "bistatic_dynamic_range_margin_db": self.dynamic_range_margin_db,
            "bistatic_minimum_detectable_rcs_m2": self.minimum_detectable_rcs_m2,
            "bistatic_rcs_margin_db": self.rcs_margin_db,
            "bistatic_tx_target_range_m": self.tx_target_range_m,
            "bistatic_target_receiver_range_m": self.target_receiver_range_m,
            "bistatic_path_range_m": self.bistatic_path_range_m,
            "bistatic_excess_path_range_m": self.excess_path_range_m,
            "bistatic_excess_delay_s": self.excess_delay_s,
            "bistatic_angle_deg": self.bistatic_angle_deg,
            "bistatic_path_range_rate_mps": self.path_range_rate_mps,
            "bistatic_closing_speed_mps": self.closing_speed_mps,
            "bistatic_doppler_hz": self.doppler_hz,
            "bistatic_snr_noise_interference_ok": self.thermal_snr_ok,
            "bistatic_doppler_resolved": self.doppler_resolved,
            "bistatic_doppler_ambiguous": self.doppler_ambiguous,
            "bistatic_direct_residual_ok": self.direct_residual_ok,
            "bistatic_dynamic_range_ok": self.dynamic_range_ok,
            "return_environment_valid": self.return_environment_valid,
            "bistatic_detectable_screening": self.detectable_screening,
            "bistatic_detectable_qualified": self.detectable_qualified,
            "bistatic_detectable": self.detectable,
            "bistatic_constraint_failure_code": self.constraint_failure_code,
            "return_path_loss_db": self.return_path_loss_db,
            "return_environment_loss_db": self.return_environment_loss_db,
            "return_terrain_loss_db": self.return_terrain_loss_db,
            "return_los": self.return_los,
            "return_terrain_state_code": self.return_terrain_state_code,
            "return_sample_error_m": self.return_sample_error_m,
        }


@dataclass(slots=True)
class BistaticFieldResult:
    arrays: BistaticFieldArrays
    noise_power_dbm: float
    thermal_noise_power_dbm: float
    interference_plus_clutter_power_dbm: float | None
    ideal_processing_gain_db: float
    processing_gain_db: float
    processing_qualified: bool
    processing_gain_source: str
    processing_bandwidth_hz: float
    doppler_resolution_hz: float
    doppler_detection_threshold_hz: float
    max_unambiguous_doppler_hz: float | None
    delay_resolution_s: float
    bistatic_path_resolution_m: float
    best_margin_index: int | None
    best_screening_index: int | None
    best_detectable_index: int | None
    doppler_resolved_count: int
    doppler_ambiguous_count: int
    screening_count: int
    detectable_count: int
    direct_residual_ok_count: int
    dynamic_range_ok_count: int
    return_environment_valid_count: int
    return_path_model: str
    terrain_state_encoding: dict[str, str]


def _fixed_ecef(latitude_deg: float, longitude_deg: float, altitude_m: float) -> tuple[float, float, float]:
    lat = math.radians(float(latitude_deg))
    lon = math.radians(float(longitude_deg))
    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    n = WGS84_A_M / math.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    return (
        (n + float(altitude_m)) * cos_lat * math.cos(lon),
        (n + float(altitude_m)) * cos_lat * math.sin(lon),
        (n * (1.0 - WGS84_E2) + float(altitude_m)) * sin_lat,
    )


def _target_ecef(
    latitude_deg: np.ndarray,
    longitude_deg: np.ndarray,
    altitude_m: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    lat = np.deg2rad(latitude_deg)
    lon = np.deg2rad(longitude_deg)
    sin_lat = np.sin(lat)
    cos_lat = np.cos(lat)
    sin_lon = np.sin(lon)
    cos_lon = np.cos(lon)
    n = WGS84_A_M / np.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    x = (n + altitude_m) * cos_lat * cos_lon
    y = (n + altitude_m) * cos_lat * sin_lon
    z = (n * (1.0 - WGS84_E2) + altitude_m) * sin_lat
    return x, y, z, sin_lat, cos_lat, sin_lon, cos_lon


def _source_slice(values: Sequence[Any] | np.ndarray, start: int, stop: int, dtype: Any) -> np.ndarray:
    if isinstance(values, np.ndarray):
        return np.asarray(values[start:stop], dtype=dtype)
    return np.asarray(values[start:stop], dtype=dtype)


def _reciprocal_state_codes(reciprocal_field: Any, count: int) -> np.ndarray:
    encoded = getattr(reciprocal_field, "terrain_state_code", None)
    if encoded is not None:
        arr = np.asarray(encoded, dtype=np.uint8)
        if arr.size == count:
            return arr
    labels = getattr(reciprocal_field, "terrain_state", None)
    if labels is None:
        return np.full(count, 5, dtype=np.uint8)  # unknown
    return encode_terrain_states(labels, count=count)


def _new_arrays(count: int, *, processing_qualified: bool = False) -> BistaticFieldArrays:
    bool_names = {
        "thermal_snr_ok", "doppler_resolved", "doppler_ambiguous",
        "direct_residual_ok", "dynamic_range_ok", "return_environment_valid",
        "detectable_screening", "detectable_qualified", "detectable", "return_los",
    }
    uint8_names = {"constraint_failure_code", "return_terrain_state_code"}
    values: dict[str, np.ndarray] = {}
    for name in BistaticFieldArrays.__dataclass_fields__:
        # ``detectable`` is a compatibility alias of detectable_qualified.  Share
        # one mask in memory; both NPZ members are still emitted with identical
        # values so the external product schema/output is unchanged.
        if name == "detectable_qualified" and processing_qualified and "detectable_screening" in values:
            values[name] = values["detectable_screening"]
            continue
        if name == "detectable" and "detectable_qualified" in values:
            values[name] = values["detectable_qualified"]
            continue
        dtype = np.bool_ if name in bool_names else np.uint8 if name in uint8_names else np.float32
        values[name] = np.empty(count, dtype=dtype)
    return BistaticFieldArrays(**values)


def compute_bistatic_field_arrays(
    *,
    target_latitude_deg: Sequence[float] | np.ndarray,
    target_longitude_deg: Sequence[float] | np.ndarray,
    target_ground_m: Sequence[float] | np.ndarray | None,
    incident_isotropic_power_dbm: Sequence[float] | np.ndarray,
    tx_latitude_deg: float,
    tx_longitude_deg: float,
    tx_altitude_m: float,
    config: ChannelAnalysisConfig,
    frequency_hz: float,
    waveform_bandwidth_hz: float,
    direct_received_power_dbm: float,
    residual_direct_power_dbm: float,
    reciprocal_field: Any | None,
) -> BistaticFieldResult:
    """Compute per-target ISAC capability layers with O(N) time/O(N) outputs.

    Scratch memory is O(chunk).  All outputs are compact arrays and the geometry
    basis can be reused for later target/process hypothesis reevaluation.
    """

    count = len(target_latitude_deg)
    if len(target_longitude_deg) != count or len(incident_isotropic_power_dbm) != count:
        raise ValueError("aligned target latitude/longitude/incident arrays are required")
    if target_ground_m is not None and len(target_ground_m) != count:
        target_ground_m = None

    processing = config.processing
    receiver = config.receiver
    target = config.target
    processing_bandwidth_hz = float(processing.processing_bandwidth_hz or waveform_bandwidth_hz)
    thermal_noise_dbm = thermal_noise_power_dbm(processing_bandwidth_hz, receiver.noise_figure_db)
    interference_dbm = processing.interference_plus_clutter_power_dbm
    if interference_dbm is None:
        noise_power_dbm = thermal_noise_dbm
    else:
        noise_power_dbm = 10.0 * math.log10(
            10.0 ** (thermal_noise_dbm / 10.0) + 10.0 ** (float(interference_dbm) / 10.0)
        )
    ideal_processing_gain_db = coherent_processing_gain_db(
        processing_bandwidth_hz, processing.coherent_integration_s
    )
    gain_qualified = processing.effective_processing_gain_db is not None
    interference_qualified = (
        processing.interference_plus_clutter_power_dbm is not None
        or not processing.require_interference_input_for_qualification
    )
    processing_qualified = bool(gain_qualified and interference_qualified)
    processing_gain_db = float(
        processing.effective_processing_gain_db
        if gain_qualified
        else ideal_processing_gain_db
    )
    processing_gain_source = "explicit_effective_gain" if gain_qualified else "ideal_time_bandwidth_screening"

    doppler_resolution_hz = 1.0 / float(processing.coherent_integration_s)
    doppler_detection_threshold_hz = max(
        doppler_resolution_hz,
        float(processing.clutter_notch_hz),
        float(processing.minimum_detectable_doppler_hz),
    )
    prf_hz = processing.pulse_repetition_frequency_hz
    max_unambiguous_doppler_hz = float(prf_hz) / 2.0 if prf_hz is not None else None
    delay_resolution_s = 1.0 / processing_bandwidth_hz
    bistatic_path_resolution_m = SPEED_OF_LIGHT_M_S * delay_resolution_s

    arrays = _new_arrays(count, processing_qualified=processing_qualified)

    use_environment_return = (
        str(config.return_path_model) == "environment_reciprocal"
        and reciprocal_field is not None
        and len(getattr(reciprocal_field, "path_loss_db", [])) == count
    )
    environment_requested = str(config.return_path_model) == "environment_reciprocal"
    return_state_codes = (
        _reciprocal_state_codes(reciprocal_field, count)
        if use_environment_return
        else np.full(count, 4, dtype=np.uint8)  # free_space
    )
    arrays.return_terrain_state_code[:] = return_state_codes

    tx = _fixed_ecef(tx_latitude_deg, tx_longitude_deg, tx_altitude_m)
    receiver_abs_m = float(receiver.absolute_height_m)
    rx = _fixed_ecef(receiver.latitude, receiver.longitude, receiver_abs_m)
    direct_dx = rx[0] - tx[0]
    direct_dy = rx[1] - tx[1]
    direct_dz = rx[2] - tx[2]
    direct_range_m = math.sqrt(direct_dx * direct_dx + direct_dy * direct_dy + direct_dz * direct_dz)

    wavelength_m = SPEED_OF_LIGHT_M_S / max(float(frequency_hz), 1.0)
    geometry_scattering_db = 10.0 * math.log10(4.0 * math.pi) - 20.0 * math.log10(wavelength_m)
    receiver_chain_db = (
        float(receiver.echo_antenna_gain_dbi)
        - float(receiver.feeder_loss_db)
        - float(processing.system_loss_db)
    )
    rcs_db = 10.0 * math.log10(max(float(target.bistatic_rcs_m2), 1.0e-18))
    return_excess_db = float(receiver.return_path_excess_loss_db)
    post_add_db = processing_gain_db - float(processing.processing_loss_db)
    required_snr_db = float(processing.required_snr_db)
    required_direct_margin_db = float(processing.required_echo_to_residual_direct_db)
    require_direct = bool(processing.require_direct_path_constraint)
    require_dynamic = bool(processing.require_dynamic_range_constraint)
    max_dynamic_db = processing.max_receiver_dynamic_range_db
    frequency_over_c = float(frequency_hz) / SPEED_OF_LIGHT_M_S

    heading = math.radians(float(config.motion.heading_deg_true))
    east_mps = float(config.motion.speed_mps) * math.sin(heading)
    north_mps = float(config.motion.speed_mps) * math.cos(heading)
    up_mps = float(config.motion.climb_rate_mps)
    velocity_norm_mps = math.sqrt(east_mps * east_mps + north_mps * north_mps + up_mps * up_mps)

    for start in range(0, count, _CHUNK_POINTS):
        stop = min(start + _CHUNK_POINTS, count)
        n = stop - start
        lat_deg = _source_slice(target_latitude_deg, start, stop, np.float64)
        lon_deg = _source_slice(target_longitude_deg, start, stop, np.float64)
        if target_ground_m is None:
            altitude = np.full(n, float(target.height_m_agl), dtype=np.float64)
        else:
            altitude = _source_slice(target_ground_m, start, stop, np.float64)
            altitude += float(target.height_m_agl)

        x, y, z, sin_lat, cos_lat, sin_lon, cos_lon = _target_ecef(lat_deg, lon_deg, altitude)

        tx_dx = x - tx[0]
        tx_dy = y - tx[1]
        tx_dz = z - tx[2]
        rx_dx = x - rx[0]
        rx_dy = y - rx[1]
        rx_dz = z - rx[2]
        del x, y, z

        r_tx = np.sqrt(tx_dx * tx_dx + tx_dy * tx_dy + tx_dz * tx_dz)
        r_rx = np.sqrt(rx_dx * rx_dx + rx_dy * rx_dy + rx_dz * rx_dz)
        safe_tx = np.maximum(r_tx, 1.0e-12)
        safe_rx = np.maximum(r_rx, 1.0e-12)

        path = r_tx + r_rx
        excess = path - direct_range_m
        cos_beta = (tx_dx * rx_dx + tx_dy * rx_dy + tx_dz * rx_dz) / (safe_tx * safe_rx)
        np.clip(cos_beta, -1.0, 1.0, out=cos_beta)
        np.arccos(cos_beta, out=cos_beta)
        np.degrees(cos_beta, out=cos_beta)
        angle = cos_beta

        # The raw TX/RX deltas are dead after range/angle. Reuse those six
        # buffers for unit vectors, then reuse the TX buffers for the summed
        # bistatic direction. This removes nine chunk-sized float64 temporaries.
        tx_dx /= safe_tx
        tx_dy /= safe_tx
        tx_dz /= safe_tx
        rx_dx /= safe_rx
        rx_dy /= safe_rx
        rx_dz /= safe_rx
        del safe_tx, safe_rx
        tx_dx += rx_dx
        tx_dy += rx_dy
        tx_dz += rx_dz
        del rx_dx, rx_dy, rx_dz
        sx, sy, sz = tx_dx, tx_dy, tx_dz

        # Local ENU Doppler basis: f_D = cE*vE + cN*vN + cU*vU.
        c_e = -frequency_over_c * (-sin_lon * sx + cos_lon * sy)
        c_n = -frequency_over_c * (-sin_lat * cos_lon * sx - sin_lat * sin_lon * sy + cos_lat * sz)
        c_u = -frequency_over_c * (cos_lat * cos_lon * sx + cos_lat * sin_lon * sy + sin_lat * sz)
        del sx, sy, sz
        doppler = c_e * east_mps + c_n * north_mps + c_u * up_mps
        sensitivity = np.sqrt(c_e * c_e + c_n * c_n + c_u * c_u)
        if velocity_norm_mps > 0.0:
            motion_sensitivity = doppler / velocity_norm_mps
        else:
            motion_sensitivity = np.zeros(n, dtype=np.float64)
        with np.errstate(divide="ignore", invalid="ignore"):
            min_speed = np.where(
                sensitivity > 1.0e-9,
                doppler_detection_threshold_hz / sensitivity,
                np.inf,
            )

        # Range-rate sign retained for compatibility: positive = path lengthening.
        range_rate = -doppler / frequency_over_c

        if use_environment_return:
            return_loss = _source_slice(reciprocal_field.path_loss_db, start, stop, np.float32)
            return_loss = return_loss + np.float32(return_excess_db)
            return_env = _source_slice(reciprocal_field.environment_loss_db, start, stop, np.float32)
            return_terrain = _source_slice(reciprocal_field.terrain_loss_db, start, stop, np.float32)
            return_los = _source_slice(reciprocal_field.los, start, stop, np.bool_)
            return_error = _source_slice(reciprocal_field.sample_error_m, start, stop, np.float32)
            environment_valid = np.isfinite(return_loss) & np.isfinite(return_error)
        else:
            return_loss = 20.0 * np.log10(
                4.0 * math.pi * np.maximum(r_rx, 1.0) / wavelength_m
            ) + return_excess_db
            return_env = np.zeros(n, dtype=np.float32)
            return_terrain = np.zeros(n, dtype=np.float32)
            return_los = np.ones(n, dtype=np.bool_)
            return_error = np.zeros(n, dtype=np.float32)
            environment_valid = np.ones(n, dtype=np.bool_) if not environment_requested else np.zeros(n, dtype=np.bool_)

        incident = _source_slice(incident_isotropic_power_dbm, start, stop, np.float32)
        echo_base = incident - return_loss + geometry_scattering_db
        echo = echo_base + receiver_chain_db + rcs_db
        pre = echo - noise_power_dbm
        post = pre + post_add_db
        snr_margin = post - required_snr_db
        thermal_ok = snr_margin >= 0.0

        resolved = np.abs(doppler) >= doppler_detection_threshold_hz
        if max_unambiguous_doppler_hz is None:
            ambiguous = np.zeros(n, dtype=np.bool_)
        else:
            ambiguous = np.abs(doppler) > max_unambiguous_doppler_hz

        echo_to_residual = echo - residual_direct_power_dbm
        direct_margin = echo_to_residual - required_direct_margin_db
        direct_ok = direct_margin >= 0.0 if require_direct else np.ones(n, dtype=np.bool_)
        required_cancellation = np.maximum(
            0.0,
            direct_received_power_dbm - echo + required_direct_margin_db,
        )

        dynamic_required = direct_received_power_dbm - echo
        if max_dynamic_db is None:
            dynamic_margin = np.full(n, np.nan, dtype=np.float32)
            dynamic_ok = np.zeros(n, dtype=np.bool_) if require_dynamic else np.ones(n, dtype=np.bool_)
        else:
            dynamic_margin = float(max_dynamic_db) - dynamic_required
            dynamic_ok = dynamic_margin >= 0.0 if require_dynamic else np.ones(n, dtype=np.bool_)

        # Minimum RCS to satisfy all enabled *power* constraints. Doppler is
        # reported separately because no amount of RCS fixes a geometric blind zone.
        power_margin = np.asarray(snr_margin, dtype=np.float64)
        if require_direct:
            power_margin = np.minimum(power_margin, direct_margin)
        if require_dynamic:
            if max_dynamic_db is None:
                power_margin = np.full(n, -np.inf, dtype=np.float64)
            else:
                power_margin = np.minimum(power_margin, dynamic_margin)
        with np.errstate(over="ignore", invalid="ignore"):
            min_rcs = float(target.bistatic_rcs_m2) * np.power(10.0, -power_margin / 10.0)

        screening = thermal_ok & resolved & ~ambiguous & direct_ok & dynamic_ok & environment_valid
        qualified = screening & bool(processing_qualified)

        failure = np.zeros(n, dtype=np.uint8)
        failure[~thermal_ok] |= FAIL_THERMAL_SNR
        failure[~resolved] |= FAIL_DOPPLER_RESOLUTION
        failure[ambiguous] |= FAIL_DOPPLER_AMBIGUITY
        if require_direct:
            failure[~direct_ok] |= FAIL_DIRECT_RESIDUAL
        if require_dynamic:
            failure[~dynamic_ok] |= FAIL_DYNAMIC_RANGE
        if not processing_qualified:
            failure |= FAIL_PROCESSING_QUALIFICATION
        failure[~environment_valid] |= FAIL_ENVIRONMENT_RETURN

        arrays.echo_geometry_base_dbm[start:stop] = echo_base
        arrays.doppler_east_hz_per_mps[start:stop] = c_e
        arrays.doppler_north_hz_per_mps[start:stop] = c_n
        arrays.doppler_up_hz_per_mps[start:stop] = c_u
        arrays.doppler_sensitivity_hz_per_mps[start:stop] = sensitivity
        arrays.motion_doppler_sensitivity_hz_per_mps[start:stop] = motion_sensitivity
        arrays.minimum_detectable_speed_mps[start:stop] = min_speed
        arrays.echo_power_dbm[start:stop] = echo
        arrays.preprocessing_snr_db[start:stop] = pre
        arrays.postprocessing_snr_db[start:stop] = post
        arrays.detection_margin_db[start:stop] = snr_margin
        arrays.echo_to_residual_direct_db[start:stop] = echo_to_residual
        arrays.direct_residual_margin_db[start:stop] = direct_margin
        arrays.required_cancellation_db[start:stop] = required_cancellation
        arrays.required_dynamic_range_db[start:stop] = dynamic_required
        arrays.dynamic_range_margin_db[start:stop] = dynamic_margin
        arrays.minimum_detectable_rcs_m2[start:stop] = min_rcs
        arrays.rcs_margin_db[start:stop] = power_margin
        arrays.tx_target_range_m[start:stop] = r_tx
        arrays.target_receiver_range_m[start:stop] = r_rx
        arrays.bistatic_path_range_m[start:stop] = path
        arrays.excess_path_range_m[start:stop] = excess
        arrays.excess_delay_s[start:stop] = excess / SPEED_OF_LIGHT_M_S
        arrays.bistatic_angle_deg[start:stop] = angle
        arrays.path_range_rate_mps[start:stop] = range_rate
        arrays.closing_speed_mps[start:stop] = -range_rate
        arrays.doppler_hz[start:stop] = doppler
        arrays.thermal_snr_ok[start:stop] = thermal_ok
        arrays.doppler_resolved[start:stop] = resolved
        arrays.doppler_ambiguous[start:stop] = ambiguous
        arrays.direct_residual_ok[start:stop] = direct_ok
        arrays.dynamic_range_ok[start:stop] = dynamic_ok
        arrays.return_environment_valid[start:stop] = environment_valid
        arrays.detectable_screening[start:stop] = screening
        arrays.detectable_qualified[start:stop] = qualified
        arrays.detectable[start:stop] = qualified
        arrays.constraint_failure_code[start:stop] = failure
        arrays.return_path_loss_db[start:stop] = return_loss
        arrays.return_environment_loss_db[start:stop] = return_env
        arrays.return_terrain_loss_db[start:stop] = return_terrain
        arrays.return_los[start:stop] = return_los
        arrays.return_sample_error_m[start:stop] = return_error

    if count:
        best_margin_index = int(np.nanargmax(arrays.rcs_margin_db))
    else:
        best_margin_index = None
    if count and np.any(arrays.detectable_screening):
        best_screening_index = int(
            np.argmax(np.where(arrays.detectable_screening, arrays.rcs_margin_db, -np.inf))
        )
    else:
        best_screening_index = None
    if count and np.any(arrays.detectable_qualified):
        best_detectable_index = int(
            np.argmax(np.where(arrays.detectable_qualified, arrays.rcs_margin_db, -np.inf))
        )
    else:
        best_detectable_index = None

    return BistaticFieldResult(
        arrays=arrays,
        noise_power_dbm=float(noise_power_dbm),
        thermal_noise_power_dbm=float(thermal_noise_dbm),
        interference_plus_clutter_power_dbm=(float(interference_dbm) if interference_dbm is not None else None),
        ideal_processing_gain_db=float(ideal_processing_gain_db),
        processing_gain_db=float(processing_gain_db),
        processing_qualified=bool(processing_qualified),
        processing_gain_source=processing_gain_source,
        processing_bandwidth_hz=processing_bandwidth_hz,
        doppler_resolution_hz=doppler_resolution_hz,
        doppler_detection_threshold_hz=doppler_detection_threshold_hz,
        max_unambiguous_doppler_hz=max_unambiguous_doppler_hz,
        delay_resolution_s=delay_resolution_s,
        bistatic_path_resolution_m=bistatic_path_resolution_m,
        best_margin_index=best_margin_index,
        best_screening_index=best_screening_index,
        best_detectable_index=best_detectable_index,
        doppler_resolved_count=int(np.count_nonzero(arrays.doppler_resolved)),
        doppler_ambiguous_count=int(np.count_nonzero(arrays.doppler_ambiguous)),
        screening_count=int(np.count_nonzero(arrays.detectable_screening)),
        detectable_count=int(np.count_nonzero(arrays.detectable_qualified)),
        direct_residual_ok_count=int(np.count_nonzero(arrays.direct_residual_ok)),
        dynamic_range_ok_count=int(np.count_nonzero(arrays.dynamic_range_ok)),
        return_environment_valid_count=int(np.count_nonzero(arrays.return_environment_valid)),
        return_path_model=("environment_reciprocal" if use_environment_return else "free_space_plus_excess"),
        terrain_state_encoding=terrain_state_encoding_metadata(),
    )
