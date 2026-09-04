"""Terrain horizon, Fresnel clearance, and knife-edge diffraction along TX→RX profiles."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

EARTH_RADIUS_M = 6371000.0


@dataclass
class TerrainSampleResult:
    terrain_loss_db: float
    los_terrain: bool
    fresnel_clearance: float
    terrain_state: str
    z_ground_m: float
    z_rx_abs_m: float


def earth_bulge_m(u_m: float, d_m: float, k_factor: float) -> float:
    """Earth-curvature bulge at distance u along path of length d."""
    if d_m <= 0.0:
        return 0.0
    k = max(1.0, float(k_factor))
    re = k * EARTH_RADIUS_M
    return (u_m * max(0.0, d_m - u_m)) / (2.0 * re)


def fresnel_radius_m(u_m: float, d_m: float, wavelength_m: float) -> float:
    if d_m <= 0.0 or u_m <= 0.0 or u_m >= d_m:
        return 0.0
    return math.sqrt(max(0.0, wavelength_m * u_m * (d_m - u_m) / d_m))


def knife_edge_loss_db(nu: float) -> float:
    """Single knife-edge diffraction (positive nu = obstruction)."""
    if nu <= -0.78:
        return 0.0
    return 6.9 + 20.0 * math.log10(math.sqrt((nu - 0.1) ** 2 + 1.0) + nu - 0.1)


def _wavelength_m(freq_mhz: float) -> float:
    return 299792458.0 / max(1.0, float(freq_mhz) * 1e6)


def compute_terrain_at_sample(
    *,
    profile_u_m: Sequence[float],
    profile_z_dem_m: Sequence[float],
    sample_distance_m: float,
    z_tx_abs_m: float,
    z_rx_abs_m: float,
    freq_mhz: float,
    clutter_height_m: float,
    k_factor: float,
    fresnel_min_clearance: float,
    loss_cap_db: float,
) -> TerrainSampleResult:
    """Classify terrain LOS/Fresnel/shadow and return diffraction loss at sample range."""
    d = max(1.0, float(sample_distance_m))
    z_line_end = z_tx_abs_m + (d / d) * (z_rx_abs_m - z_tx_abs_m)
    z_line_at_d = z_tx_abs_m + (z_rx_abs_m - z_tx_abs_m)

    wl = _wavelength_m(freq_mhz)
    eta = max(0.1, float(fresnel_min_clearance))

    worst_h = -1e9
    worst_u = d * 0.5
    min_q = 1e9
    alpha_max = -1e9

    for u, z_dem in zip(profile_u_m, profile_z_dem_m):
        if u <= 0.0 or u > d + 1e-6:
            continue
        bulge = earth_bulge_m(u, d, k_factor)
        z_line = z_tx_abs_m + (u / d) * (z_rx_abs_m - z_tx_abs_m)
        z_obst = float(z_dem) + bulge + float(clutter_height_m)
        h = z_obst - z_line
        if u > 1.0:
            alpha = (z_obst - z_tx_abs_m) / u
            alpha_max = max(alpha_max, alpha)

        f1 = fresnel_radius_m(u, d, wl)
        if f1 > 1e-6:
            q = h / f1
            min_q = min(min_q, q)
        if h > worst_h:
            worst_h = h
            worst_u = u

    alpha_rx = (z_rx_abs_m - z_tx_abs_m) / d if d > 0 else 0.0
    alpha_fresnel = eta * wl / max(d, 1.0)

    if min_q >= eta:
        state = "los"
        los = True
        loss = 0.0
    elif min_q >= 0.0:
        state = "fresnel_partial"
        los = True
        loss = max(0.0, (eta - min_q) * 6.0)
    elif alpha_rx <= alpha_max + alpha_fresnel:
        state = "terrain_shadow"
        los = False
        d1 = worst_u
        d2 = max(1.0, d - d1)
        nu = worst_h * math.sqrt(2.0 * d / (wl * d1 * d2))
        loss = min(float(loss_cap_db), knife_edge_loss_db(nu))
    else:
        state = "terrain_diffraction"
        los = False
        d1 = worst_u
        d2 = max(1.0, d - d1)
        nu = max(0.0, worst_h) * math.sqrt(2.0 * d / (wl * d1 * d2))
        loss = min(float(loss_cap_db), knife_edge_loss_db(nu))

    z_ground = float(profile_z_dem_m[-1]) if profile_z_dem_m else 0.0
    for u, z_dem in zip(profile_u_m, profile_z_dem_m):
        if abs(u - d) < 1e-3:
            z_ground = float(z_dem)
            break

    return TerrainSampleResult(
        terrain_loss_db=float(loss),
        los_terrain=bool(los),
        fresnel_clearance=float(min_q if min_q < 1e8 else 1.0),
        terrain_state=state,
        z_ground_m=z_ground,
        z_rx_abs_m=z_rx_abs_m,
    )
