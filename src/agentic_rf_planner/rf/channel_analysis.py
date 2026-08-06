"""Waveform-agnostic one-way and bistatic channel analysis.

The propagation planner supplies the transmitter-to-grid-point channel.  This
module adds a fixed analysis receiver, bistatic target geometry, echo link
budget, target-motion Doppler, coherent-processing metrics, and explicit
machine-readable detectability flags.  It does not depend on ATSC, DVB-T, NR,
or any other particular waveform; frequency and occupied bandwidth are taken
from the active RF configuration unless the processing bandwidth is overridden.
"""

from __future__ import annotations

import math

import numpy as np
from dataclasses import dataclass
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


SPEED_OF_LIGHT_M_S = 299_792_458.0
WGS84_A_M = 6_378_137.0
WGS84_F = 1.0 / 298.257_223_563
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)


class ChannelReceiver(BaseModel):
    """Fixed receiver and RF-chain parameters used by the channel analysis."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    altitude_m_amsl: float = Field(0.0, validation_alias=AliasChoices("altitudeMamsl", "altitude"), serialization_alias="altitudeMamsl")
    antenna_height_m_agl: float = Field(10.0, validation_alias=AliasChoices("antennaHeightMagl", "antennaHeightM"), serialization_alias="antennaHeightMagl", ge=0.0)
    direct_antenna_gain_dbi: float = Field(0.0, validation_alias=AliasChoices("directAntennaGainDbi", "referenceAntennaGainDbi"), serialization_alias="directAntennaGainDbi")
    echo_antenna_gain_dbi: float = Field(0.0, validation_alias=AliasChoices("echoAntennaGainDbi", "surveillanceAntennaGainDbi"), serialization_alias="echoAntennaGainDbi")
    feeder_loss_db: float = Field(0.0, alias="feederLossDb", ge=0.0)
    noise_figure_db: float = Field(5.0, alias="noiseFigureDb", ge=0.0)
    direct_path_excess_loss_db: float = Field(0.0, alias="directPathExcessLossDb", ge=0.0)
    return_path_excess_loss_db: float = Field(0.0, alias="returnPathExcessLossDb", ge=0.0)

    @property
    def absolute_height_m(self) -> float:
        return float(self.altitude_m_amsl) + float(self.antenna_height_m_agl)

    # Read-only compatibility names for clients that imported the earlier
    # DVT-specific passive-radar models. New serialization uses the generic names.
    @property
    def altitude(self) -> float:
        return self.altitude_m_amsl

    @property
    def antenna_height_m(self) -> float:
        return self.antenna_height_m_agl

    @property
    def reference_antenna_gain_dbi(self) -> float:
        return self.direct_antenna_gain_dbi

    @property
    def surveillance_antenna_gain_dbi(self) -> float:
        return self.echo_antenna_gain_dbi


class ChannelTarget(BaseModel):
    """Target scattering and height assumptions for every candidate grid point."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    height_m_agl: float = Field(1000.0, validation_alias=AliasChoices("heightMagl", "heightAglM"), serialization_alias="heightMagl", ge=0.0)
    bistatic_rcs_m2: float = Field(10.0, alias="bistaticRcsM2", gt=0.0)

    @property
    def height_agl_m(self) -> float:
        return self.height_m_agl


class TargetMotion(BaseModel):
    """Target velocity in local navigation coordinates.

    Heading is true clockwise from north.  Positive climb rate is upward.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    speed_mps: float = Field(0.0, alias="speedMps", ge=0.0)
    heading_deg_true: float = Field(0.0, alias="headingDegTrue", ge=0.0, lt=360.0)
    climb_rate_mps: float = Field(0.0, alias="climbRateMps")


class ChannelProcessing(BaseModel):
    """Signal-processing and detection assumptions."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    processing_bandwidth_hz: float | None = Field(
        None,
        alias="processingBandwidthHz",
        gt=0.0,
        description="Defaults to the active waveform occupied/channel bandwidth.",
    )
    coherent_integration_s: float = Field(1.0, alias="coherentIntegrationS", gt=0.0)
    processing_loss_db: float = Field(3.0, alias="processingLossDb", ge=0.0)
    system_loss_db: float = Field(3.0, alias="systemLossDb", ge=0.0)
    required_snr_db: float = Field(10.0, alias="requiredSnrDb")
    direct_path_cancellation_db: float = Field(60.0, alias="directPathCancellationDb", ge=0.0)
    pulse_repetition_frequency_hz: float | None = Field(
        None,
        alias="pulseRepetitionFrequencyHz",
        gt=0.0,
        description="Optional Doppler sampling rate. Omit for no PRF ambiguity test.",
    )
    clutter_notch_hz: float = Field(0.0, alias="clutterNotchHz", ge=0.0)
    minimum_detectable_doppler_hz: float = Field(
        0.0, alias="minimumDetectableDopplerHz", ge=0.0
    )


class ChannelAnalysisConfig(BaseModel):
    """Waveform-independent link-budget and bistatic-channel request."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    receiver: ChannelReceiver
    target: ChannelTarget = Field(default_factory=ChannelTarget)
    motion: TargetMotion = Field(default_factory=TargetMotion)
    processing: ChannelProcessing = Field(default_factory=ChannelProcessing)
    return_path_model: Literal[
        "environmental_reciprocal_grid",
        "free_space_plus_excess",
    ] = Field(
        "environmental_reciprocal_grid", alias="returnPathModel"
    )


@dataclass(frozen=True)
class BistaticGeometry:
    tx_target_range_m: float
    target_receiver_range_m: float
    direct_tx_receiver_range_m: float
    bistatic_path_range_m: float
    excess_path_range_m: float
    total_delay_s: float
    excess_delay_s: float
    bistatic_angle_deg: float
    path_range_rate_mps: float
    closing_speed_mps: float
    doppler_hz: float


def geodetic_to_ecef_m(
    latitude_deg: float,
    longitude_deg: float,
    altitude_m: float,
) -> tuple[float, float, float]:
    """Convert WGS-84 geodetic coordinates to ECEF metres."""

    lat = math.radians(float(latitude_deg))
    lon = math.radians(float(longitude_deg))
    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    n = WGS84_A_M / math.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    x = (n + float(altitude_m)) * cos_lat * math.cos(lon)
    y = (n + float(altitude_m)) * cos_lat * math.sin(lon)
    z = (n * (1.0 - WGS84_E2) + float(altitude_m)) * sin_lat
    return x, y, z


def enu_velocity_to_ecef_mps(
    *,
    latitude_deg: float,
    longitude_deg: float,
    speed_mps: float,
    heading_deg_true: float,
    climb_rate_mps: float,
) -> tuple[float, float, float]:
    """Convert horizontal speed/heading and climb rate to an ECEF velocity."""

    heading = math.radians(float(heading_deg_true))
    east = float(speed_mps) * math.sin(heading)
    north = float(speed_mps) * math.cos(heading)
    up = float(climb_rate_mps)
    lat = math.radians(float(latitude_deg))
    lon = math.radians(float(longitude_deg))

    vx = -math.sin(lon) * east - math.sin(lat) * math.cos(lon) * north + math.cos(lat) * math.cos(lon) * up
    vy = math.cos(lon) * east - math.sin(lat) * math.sin(lon) * north + math.cos(lat) * math.sin(lon) * up
    vz = math.cos(lat) * north + math.sin(lat) * up
    return vx, vy, vz


def _sub(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return a[0] - b[0], a[1] - b[1], a[2] - b[2]


def _dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(v: tuple[float, float, float]) -> float:
    return math.sqrt(max(_dot(v, v), 0.0))


def _unit(v: tuple[float, float, float]) -> tuple[float, float, float]:
    length = max(_norm(v), 1.0e-12)
    return v[0] / length, v[1] / length, v[2] / length


def free_space_path_loss_db(distance_m: float, frequency_hz: float) -> float:
    """Free-space path loss for a 3-D path length."""

    wavelength_m = SPEED_OF_LIGHT_M_S / max(float(frequency_hz), 1.0)
    return 20.0 * math.log10(4.0 * math.pi * max(float(distance_m), 1.0) / wavelength_m)


def thermal_noise_power_dbm(bandwidth_hz: float, noise_figure_db: float) -> float:
    """Thermal-noise power at 290 K using -174 dBm/Hz."""

    return -174.0 + 10.0 * math.log10(max(float(bandwidth_hz), 1.0)) + float(noise_figure_db)


def coherent_processing_gain_db(bandwidth_hz: float, integration_s: float) -> float:
    """Ideal coherent time-bandwidth gain before configured processing loss."""

    return 10.0 * math.log10(max(float(bandwidth_hz) * float(integration_s), 1.0))


def bistatic_echo_power_dbm(
    *,
    incident_isotropic_power_dbm: float,
    return_path_loss_db: float,
    receiver_gain_dbi: float,
    receiver_feeder_loss_db: float,
    bistatic_rcs_m2: float,
    frequency_hz: float,
    system_loss_db: float = 0.0,
) -> float:
    """Echo power at the receiver input with the first path already evaluated."""

    wavelength_m = SPEED_OF_LIGHT_M_S / max(float(frequency_hz), 1.0)
    scattering_term_db = (
        10.0 * math.log10(max(float(bistatic_rcs_m2), 1.0e-18))
        + 10.0 * math.log10(4.0 * math.pi)
        - 20.0 * math.log10(wavelength_m)
    )
    return (
        float(incident_isotropic_power_dbm)
        - float(return_path_loss_db)
        + float(receiver_gain_dbi)
        - float(receiver_feeder_loss_db)
        + scattering_term_db
        - float(system_loss_db)
    )


def bistatic_geometry(
    *,
    tx_latitude_deg: float,
    tx_longitude_deg: float,
    tx_altitude_m: float,
    target_latitude_deg: float,
    target_longitude_deg: float,
    target_altitude_m: float,
    receiver_latitude_deg: float,
    receiver_longitude_deg: float,
    receiver_altitude_m: float,
    target_motion: TargetMotion,
    frequency_hz: float,
) -> BistaticGeometry:
    """Compute bistatic ranges, delay, angle, range rate, and Doppler.

    Sign convention: positive Doppler means the total TX-target-RX path is
    shortening. ``path_range_rate_mps`` is d(R_tx_target + R_target_rx)/dt, so
    closing motion has a negative range rate and a positive Doppler.
    """

    tx = geodetic_to_ecef_m(tx_latitude_deg, tx_longitude_deg, tx_altitude_m)
    target = geodetic_to_ecef_m(target_latitude_deg, target_longitude_deg, target_altitude_m)
    receiver = geodetic_to_ecef_m(
        receiver_latitude_deg, receiver_longitude_deg, receiver_altitude_m
    )

    tx_to_target = _sub(target, tx)
    receiver_to_target = _sub(target, receiver)
    target_to_tx = _sub(tx, target)
    target_to_receiver = _sub(receiver, target)
    tx_to_receiver = _sub(receiver, tx)

    r_tx_target = _norm(tx_to_target)
    r_target_receiver = _norm(target_to_receiver)
    r_direct = _norm(tx_to_receiver)
    bistatic_path = r_tx_target + r_target_receiver
    excess_path = bistatic_path - r_direct

    cos_beta = max(-1.0, min(1.0, _dot(_unit(target_to_tx), _unit(target_to_receiver))))
    bistatic_angle_deg = math.degrees(math.acos(cos_beta))

    velocity_ecef = enu_velocity_to_ecef_mps(
        latitude_deg=target_latitude_deg,
        longitude_deg=target_longitude_deg,
        speed_mps=target_motion.speed_mps,
        heading_deg_true=target_motion.heading_deg_true,
        climb_rate_mps=target_motion.climb_rate_mps,
    )
    # Derivative of |target-tx| + |target-rx|.
    path_range_rate_mps = _dot(
        velocity_ecef,
        (
            _unit(tx_to_target)[0] + _unit(receiver_to_target)[0],
            _unit(tx_to_target)[1] + _unit(receiver_to_target)[1],
            _unit(tx_to_target)[2] + _unit(receiver_to_target)[2],
        ),
    )
    closing_speed_mps = -path_range_rate_mps
    doppler_hz = -(float(frequency_hz) / SPEED_OF_LIGHT_M_S) * path_range_rate_mps

    return BistaticGeometry(
        tx_target_range_m=r_tx_target,
        target_receiver_range_m=r_target_receiver,
        direct_tx_receiver_range_m=r_direct,
        bistatic_path_range_m=bistatic_path,
        excess_path_range_m=excess_path,
        total_delay_s=bistatic_path / SPEED_OF_LIGHT_M_S,
        excess_delay_s=excess_path / SPEED_OF_LIGHT_M_S,
        bistatic_angle_deg=bistatic_angle_deg,
        path_range_rate_mps=path_range_rate_mps,
        closing_speed_mps=closing_speed_mps,
        doppler_hz=doppler_hz,
    )


def bistatic_geometry_arrays(
    *,
    tx_latitude_deg: float,
    tx_longitude_deg: float,
    tx_altitude_m: float,
    target_latitude_deg: np.ndarray,
    target_longitude_deg: np.ndarray,
    target_altitude_m: np.ndarray,
    receiver_latitude_deg: float,
    receiver_longitude_deg: float,
    receiver_altitude_m: float,
    target_motion: TargetMotion,
    frequency_hz: float,
) -> dict[str, np.ndarray | float]:
    """Vectorized equivalent of :func:`bistatic_geometry`.

    All per-target outputs use float64 during geometry evaluation so the result
    matches the scalar WGS-84 implementation while avoiding millions of Python
    function calls.
    """

    lat = np.asarray(target_latitude_deg, dtype=np.float64)
    lon = np.asarray(target_longitude_deg, dtype=np.float64)
    alt = np.asarray(target_altitude_m, dtype=np.float64)
    if lat.shape != lon.shape or lat.shape != alt.shape:
        raise ValueError("target latitude, longitude, and altitude arrays must have matching shapes")

    def ecef_many(lat_deg: np.ndarray, lon_deg: np.ndarray, altitude_m: np.ndarray) -> np.ndarray:
        lat_rad = np.deg2rad(lat_deg)
        lon_rad = np.deg2rad(lon_deg)
        sin_lat = np.sin(lat_rad)
        cos_lat = np.cos(lat_rad)
        n = WGS84_A_M / np.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
        x = (n + altitude_m) * cos_lat * np.cos(lon_rad)
        y = (n + altitude_m) * cos_lat * np.sin(lon_rad)
        z = (n * (1.0 - WGS84_E2) + altitude_m) * sin_lat
        return np.column_stack((x, y, z))

    tx = np.asarray(geodetic_to_ecef_m(tx_latitude_deg, tx_longitude_deg, tx_altitude_m), dtype=np.float64)
    receiver = np.asarray(
        geodetic_to_ecef_m(receiver_latitude_deg, receiver_longitude_deg, receiver_altitude_m),
        dtype=np.float64,
    )
    target = ecef_many(lat, lon, alt)

    tx_to_target = target - tx
    receiver_to_target = target - receiver
    target_to_tx = -tx_to_target
    target_to_receiver = -receiver_to_target

    r_tx_target = np.linalg.norm(tx_to_target, axis=1)
    r_target_receiver = np.linalg.norm(target_to_receiver, axis=1)
    direct_range = float(np.linalg.norm(receiver - tx))
    safe_tx = np.maximum(r_tx_target, 1.0e-12)
    safe_rx = np.maximum(r_target_receiver, 1.0e-12)
    u_tx_to_target = tx_to_target / safe_tx[:, None]
    u_receiver_to_target = receiver_to_target / safe_rx[:, None]
    u_target_to_tx = target_to_tx / safe_tx[:, None]
    u_target_to_receiver = target_to_receiver / safe_rx[:, None]

    cos_beta = np.clip(np.einsum("ij,ij->i", u_target_to_tx, u_target_to_receiver), -1.0, 1.0)
    beta_deg = np.rad2deg(np.arccos(cos_beta))

    heading = math.radians(float(target_motion.heading_deg_true))
    east = float(target_motion.speed_mps) * math.sin(heading)
    north = float(target_motion.speed_mps) * math.cos(heading)
    up = float(target_motion.climb_rate_mps)
    lat_rad = np.deg2rad(lat)
    lon_rad = np.deg2rad(lon)
    sin_lat = np.sin(lat_rad)
    cos_lat = np.cos(lat_rad)
    sin_lon = np.sin(lon_rad)
    cos_lon = np.cos(lon_rad)
    velocity = np.column_stack(
        (
            -sin_lon * east - sin_lat * cos_lon * north + cos_lat * cos_lon * up,
            cos_lon * east - sin_lat * sin_lon * north + cos_lat * sin_lon * up,
            cos_lat * north + sin_lat * up,
        )
    )
    path_rate = np.einsum(
        "ij,ij->i", velocity, u_tx_to_target + u_receiver_to_target
    )
    bistatic_path = r_tx_target + r_target_receiver
    excess_path = bistatic_path - direct_range
    doppler = -(float(frequency_hz) / SPEED_OF_LIGHT_M_S) * path_rate

    return {
        "tx_target_range_m": r_tx_target,
        "target_receiver_range_m": r_target_receiver,
        "direct_tx_receiver_range_m": direct_range,
        "bistatic_path_range_m": bistatic_path,
        "excess_path_range_m": excess_path,
        "total_delay_s": bistatic_path / SPEED_OF_LIGHT_M_S,
        "excess_delay_s": excess_path / SPEED_OF_LIGHT_M_S,
        "bistatic_angle_deg": beta_deg,
        "path_range_rate_mps": path_rate,
        "closing_speed_mps": -path_rate,
        "doppler_hz": doppler,
    }
