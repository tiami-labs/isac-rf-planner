"""RF attenuation calculations."""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from typing import Any, Dict

from ..pipeline.schemas import WorldModel, AttenuationGrid, MaterialType, RFParams
from .material_models import DEFAULT_MATERIAL_DB
from .modulation_schemes import get_modulation_by_name, select_modulation
from .ofdm_params import MIMOConfig, OFDMParams

logger = logging.getLogger(__name__)


def compute_attenuation_grid(world: WorldModel) -> AttenuationGrid:
    """
    Given a WorldModel (cells with obstacles), compute RSRP, SINR, and link adaptation per cell.

    Model (v5 - vendor-grade roadmap, deterministic first pass):
      RSRP = min(max_rsrp_dbm, P_RS_EIRP - PL_scenario - L_pen - L_shadow
                 - L_diffraction + G_canyon - A_h - A_v + G_UE)
      where:
        - P_RS_EIRP is a reference-signal-equivalent source term derived from
          total carrier TX power, occupied RE spreading, antenna/feed assumptions,
          and a conservative reference-signal offset
        - PL_scenario is the selected deterministic scenario path loss
          (3GPP 38.901 UMa/UMi first pass, fallback to legacy FSPL+NLOS)
        - L_pen applies only while the ray segment is inside a blocker
        - L_shadow applies after LOS is lost, instead of permanently stacking every wall
        - L_diffraction and G_canyon are continuation terms for blocked-but-surviving rays
        - A_h / A_v are horizontal and vertical antenna-pattern attenuations
        - G_UE = UE antenna gain

      SINR is computed after aggregating co-channel same-site sector candidates
      at each unique sample location:
        serving = strongest RSRP candidate
        interference = sum(other same-carrier sector powers, linear domain)
        SINR = 10*log10(P_serving / (P_noise + Sum(P_interferers)))
    """

    max_rsrp_dbm = float(getattr(world.rf_params, "max_rsrp_dbm", -62.0) or -62.0)
    tx_h = float(getattr(world.rf_params, "tx_height_m", 0.0) or 0.0)
    rx_h = float(getattr(world.rf_params, "rx_height_m", 1.5) or 1.5)

    mimo = MIMOConfig(
        num_tx_antennas=world.rf_params.num_tx_antennas,
        num_rx_antennas=world.rf_params.num_rx_antennas,
        mimo_mode=world.rf_params.mimo_mode,
    )
    mimo_gain_db = mimo.diversity_gain_db
    mimo_streams = mimo.spatial_multiplexing_gain

    material_counts: Dict[Any, int] = {}
    propagation_mode_counts: Dict[str, int] = {}
    sample_groups: dict[tuple[float, float], list[dict[str, Any]]] = defaultdict(list)
    total_extra_loss = 0.0
    cells_with_loss = 0
    sector_lookup = _build_sector_lookup(world.rf_params)

    for cell in world.cells:
        d2 = max(cell.distance_m, 1.0)
        d3 = _three_dimensional_distance_m(d2, tx_h, rx_h)
        sector_params = _resolve_sector_params(cell, world.rf_params, sector_lookup)
        sector_freq_mhz = float(sector_params["freq_mhz"])
        sector_bandwidth_mhz = float(sector_params["channel_bandwidth_mhz"])
        rs_eirp_dbm = _reference_signal_eirp_dbm(
            world.rf_params,
            tx_power_dbm_override=float(sector_params["tx_power_dbm"]),
        )
        scenario_path_loss_db = _scenario_path_loss_db(
            distance_2d_m=d2,
            distance_3d_m=d3,
            freq_mhz=sector_freq_mhz,
            tx_height_m=tx_h,
            rx_height_m=rx_h,
            is_los=bool(getattr(cell, "is_los", True)),
            rf_params=world.rf_params,
        )

        penetration_loss_db = float(getattr(cell, "penetration_loss_db", 0.0) or 0.0)
        shadow_loss_db = float(getattr(cell, "shadow_loss_db", 0.0) or 0.0)
        diffraction_loss_db = float(getattr(cell, "diffraction_loss_db", 0.0) or 0.0)
        canyon_recovery_db = float(getattr(cell, "canyon_recovery_db", 0.0) or 0.0)

        # Fallback path for legacy cells that still only expose obstacle/material counts.
        if penetration_loss_db == shadow_loss_db == diffraction_loss_db == canyon_recovery_db == 0.0:
            penetration_loss_db = max(
                0.0,
                float(getattr(cell, "cumulative_material_loss_db", 0.0) or 0.0),
            )

        horizontal_pattern_loss_db = _horizontal_pattern_attenuation_db(
            bearing_deg=cell.bearing_deg,
            sector_params=sector_params,
            rf_params=world.rf_params,
        )
        vertical_pattern_loss_db = _vertical_pattern_attenuation_db(
            distance_m=d2,
            tx_height_m=tx_h,
            rx_height_m=rx_h,
            rf_params=world.rf_params,
            sector_params=sector_params,
        )
        rx_combining_gain_db = float(getattr(world.rf_params, "ue_antenna_gain_dbi", 0.0) or 0.0)

        extra_loss_db = max(
            0.0,
            penetration_loss_db + shadow_loss_db + diffraction_loss_db - canyon_recovery_db,
        )
        precomputed = getattr(cell, "precomputed_rsrp_dbm", None)
        if precomputed is not None:
            # Multipath ray-tracing mode may precompute per-candidate RSRP directly.
            rsrp_uncapped = float(precomputed)
        else:
            rsrp_uncapped = (
                rs_eirp_dbm
                - scenario_path_loss_db
                - extra_loss_db
                - horizontal_pattern_loss_db
                - vertical_pattern_loss_db
                + rx_combining_gain_db
            )
        rsrp = min(rsrp_uncapped, max_rsrp_dbm)

        key = (round(cell.lat, 8), round(cell.lon, 8))
        sample_groups[key].append(
            {
                "lat": cell.lat,
                "lon": cell.lon,
                "rsrp_dbm": rsrp,
                "sector_id": str(sector_params["sector_id"]),
                "freq_mhz": sector_freq_mhz,
                "channel_bandwidth_mhz": sector_bandwidth_mhz,
                "extra_loss_db": extra_loss_db,
                "propagation_mode": str(getattr(cell, "propagation_mode", "los") or "los"),
            }
        )

        cell.extra_loss_db = extra_loss_db
        material_counts[cell.dominant_material] = material_counts.get(cell.dominant_material, 0) + 1
        propagation_mode = str(getattr(cell, "propagation_mode", "los") or "los")
        propagation_mode_counts[propagation_mode] = propagation_mode_counts.get(propagation_mode, 0) + 1
        total_extra_loss += extra_loss_db
        if extra_loss_db > 0.0:
            cells_with_loss += 1

    cell_lat: list[float] = []
    cell_lon: list[float] = []
    rsrp_dbm: list[float] = []
    sinr_db: list[float] = []
    modulation: list[str] = []
    throughput_mbps: list[float] = []
    serving_sector_id: list[str] = []
    interferer_count: list[int] = []
    top_interferer_rsrp_dbm: list[float] = []
    pilot_pollution_metric_db: list[float] = []
    modulation_counts: Dict[str, int] = {}

    for samples in sample_groups.values():
        samples_sorted = sorted(samples, key=lambda sample: sample["rsrp_dbm"], reverse=True)
        serving = samples_sorted[0]
        same_carrier_interferers = [
            sample
            for sample in samples_sorted[1:]
            if abs(sample["freq_mhz"] - serving["freq_mhz"]) < 1e-6
        ]

        noise_floor_dbm = _noise_floor_dbm_for_bandwidth(
            world.rf_params,
            float(serving["channel_bandwidth_mhz"]),
        )
        serving_mw = _dbm_to_mw(float(serving["rsrp_dbm"]))
        interference_mw = sum(_dbm_to_mw(float(sample["rsrp_dbm"])) for sample in same_carrier_interferers)
        sinr = 10.0 * math.log10(serving_mw / max(_dbm_to_mw(noise_floor_dbm) + interference_mw, 1e-15))

        if world.rf_params.enable_link_adaptation and world.rf_params.fixed_modulation is None:
            mod_scheme = select_modulation(sinr) or get_modulation_by_name("QPSK")
        else:
            mod_name = world.rf_params.fixed_modulation or "QPSK"
            mod_scheme = get_modulation_by_name(mod_name)
            if mod_scheme is None:
                logger.warning("Unknown fixed modulation %s, using QPSK", mod_name)
                mod_scheme = get_modulation_by_name("QPSK")
        if mod_scheme is None:
            mod_scheme = get_modulation_by_name("QPSK")

        ofdm = OFDMParams(
            subcarrier_spacing_khz=world.rf_params.subcarrier_spacing_khz,
            num_rb=world.rf_params.num_resource_blocks,
            channel_bandwidth_mhz=float(serving["channel_bandwidth_mhz"]),
        )
        throughput = mod_scheme.spectral_efficiency * ofdm.effective_bandwidth_mhz * mimo_streams

        top_interferer_dbm = float(same_carrier_interferers[0]["rsrp_dbm"]) if same_carrier_interferers else -200.0
        pollution_metric_db = (
            float(serving["rsrp_dbm"]) - top_interferer_dbm
            if same_carrier_interferers
            else 99.0
        )

        cell_lat.append(float(serving["lat"]))
        cell_lon.append(float(serving["lon"]))
        rsrp_dbm.append(float(serving["rsrp_dbm"]))
        sinr_db.append(sinr)
        modulation.append(mod_scheme.name.value)
        throughput_mbps.append(throughput)
        serving_sector_id.append(str(serving["sector_id"]))
        interferer_count.append(len(same_carrier_interferers))
        top_interferer_rsrp_dbm.append(top_interferer_dbm)
        pilot_pollution_metric_db.append(pollution_metric_db)
        modulation_counts[mod_scheme.name.value] = modulation_counts.get(mod_scheme.name.value, 0) + 1

    logger.info(
        "Reference-signal source: total_tx=%.2f dBm, model=%s/%s "
        "(tx_gain=%.1f dBi, feeder_loss=%.1f dB, rs_offset=%.1f dB, "
        "elec_tilt=%.1f°, mech_tilt=%.1f°, v_bw=%.1f°, v_cap=%.1f dB, h_cap=%.1f dB, fbr=%.1f dB, max_rsrp=%.1f dBm)",
        world.rf_params.tx_power_dbm,
        str(getattr(world.rf_params, "path_loss_model", "legacy")),
        str(getattr(world.rf_params, "propagation_scenario", "legacy")),
        float(getattr(world.rf_params, "tx_antenna_gain_dbi", 0.0) or 0.0),
        float(getattr(world.rf_params, "tx_feeder_loss_db", 0.0) or 0.0),
        float(getattr(world.rf_params, "reference_signal_offset_db", 0.0) or 0.0),
        float(getattr(world.rf_params, "electrical_tilt_deg", 0.0) or 0.0),
        float(getattr(world.rf_params, "mechanical_tilt_deg", 0.0) or 0.0),
        float(getattr(world.rf_params, "vertical_beamwidth_deg", 0.0) or 0.0),
        float(getattr(world.rf_params, "max_vertical_attenuation_db", 0.0) or 0.0),
        float(getattr(world.rf_params, "max_horizontal_attenuation_db", 0.0) or 0.0),
        float(getattr(world.rf_params, "front_to_back_attenuation_db", 0.0) or 0.0),
        max_rsrp_dbm,
    )
    logger.info("Material distribution: %s", material_counts)
    logger.info("Propagation modes: %s", propagation_mode_counts)
    logger.info("Modulation distribution: %s", modulation_counts)
    if world.cells:
        logger.info(
            "Cells with extra loss: %d/%d (%.1f%%)",
            cells_with_loss,
            len(world.cells),
            100.0 * cells_with_loss / len(world.cells),
        )
        logger.info("Average extra loss: %.2f dB", total_extra_loss / len(world.cells))
    logger.info("MIMO gain: %.2f dB, streams: %s", mimo_gain_db, mimo_streams)

    los_count = sum(1 for cell in world.cells if getattr(cell, "is_los", True))
    nlos_count = len(world.cells) - los_count
    if world.cells:
        logger.info(
            "LOS cells: %d (%.1f%%), NLOS cells: %d (%.1f%%)",
            los_count,
            100.0 * los_count / len(world.cells),
            nlos_count,
            100.0 * nlos_count / len(world.cells),
        )

    return AttenuationGrid(
        tx=world.tx,
        rf_params=world.rf_params,
        cell_lat=cell_lat,
        cell_lon=cell_lon,
        rsrp_dbm=rsrp_dbm,
        sinr_db=sinr_db,
        modulation=modulation,
        throughput_mbps=throughput_mbps,
        serving_sector_id=serving_sector_id,
        interferer_count=interferer_count,
        top_interferer_rsrp_dbm=top_interferer_rsrp_dbm,
        pilot_pollution_metric_db=pilot_pollution_metric_db,
    )


def _free_space_path_loss_db(distance_m: float, freq_mhz: float) -> float:
    """
    Standard FSPL formula (d in km, f in MHz):

      FSPL(dB) = 32.45 + 20*log10(d_km) + 20*log10(f_MHz)
    """
    d_km = distance_m / 1000.0
    return 32.45 + 20.0 * math.log10(max(d_km, 1e-3)) + 20.0 * math.log10(freq_mhz)


def _three_dimensional_distance_m(distance_2d_m: float, tx_height_m: float, rx_height_m: float) -> float:
    dz = tx_height_m - rx_height_m
    return math.sqrt(distance_2d_m * distance_2d_m + dz * dz)


def _dbm_to_mw(power_dbm: float) -> float:
    return 10.0 ** (power_dbm / 10.0)


def _noise_floor_dbm_for_bandwidth(rf_params: RFParams, bandwidth_mhz: float) -> float:
    if rf_params.noise_floor_dbm is not None:
        return float(rf_params.noise_floor_dbm)
    bandwidth_hz = max(1.0, float(bandwidth_mhz) * 1e6)
    return -174.0 + 10.0 * math.log10(bandwidth_hz) + float(rf_params.noise_figure_db)


def _build_sector_lookup(rf_params: RFParams) -> dict[str, dict[str, Any]]:
    sectors = getattr(rf_params, "sectors", None) or []
    lookup: dict[str, dict[str, Any]] = {}
    for sector in sectors:
        sector_id = str(sector.get("sector_id", "")).strip()
        if sector_id:
            lookup[sector_id] = sector
    return lookup


def _resolve_sector_params(cell: Any, rf_params: RFParams, sector_lookup: dict[str, dict[str, Any]]) -> dict[str, Any]:
    sector_id = str(getattr(cell, "sector_id", "omnidirectional") or "omnidirectional")
    sector_cfg = sector_lookup.get(sector_id, {})
    return {
        "sector_id": sector_id,
        "freq_mhz": float(
            getattr(cell, "sector_freq_mhz", None)
            or sector_cfg.get("freq_mhz")
            or rf_params.freq_mhz
        ),
        "tx_power_dbm": float(
            getattr(cell, "sector_tx_power_dbm", None)
            or sector_cfg.get("tx_power_dbm")
            or rf_params.tx_power_dbm
        ),
        "channel_bandwidth_mhz": float(
            getattr(cell, "sector_channel_bandwidth_mhz", None)
            or sector_cfg.get("channel_bandwidth_mhz")
            or rf_params.channel_bandwidth_mhz
        ),
        "azimuth_deg": float(
            getattr(cell, "sector_azimuth_deg", None)
            if getattr(cell, "sector_azimuth_deg", None) is not None
            else sector_cfg.get("azimuth_deg", 0.0)
        ),
        "beamwidth_h_deg": float(
            getattr(cell, "sector_beamwidth_h_deg", None)
            if getattr(cell, "sector_beamwidth_h_deg", None) is not None
            else sector_cfg.get("beamwidth_h_deg", 360.0)
        ),
        "beamwidth_v_deg": float(
            getattr(cell, "sector_beamwidth_v_deg", None)
            if getattr(cell, "sector_beamwidth_v_deg", None) is not None
            else sector_cfg.get("beamwidth_v_deg", getattr(rf_params, "vertical_beamwidth_deg", 8.0))
        ),
        "electrical_tilt_deg": float(
            getattr(cell, "sector_electrical_tilt_deg", None)
            if getattr(cell, "sector_electrical_tilt_deg", None) is not None
            else sector_cfg.get("electrical_tilt_deg", getattr(rf_params, "electrical_tilt_deg", 0.0))
        ),
        "mechanical_tilt_deg": float(
            getattr(cell, "sector_mechanical_tilt_deg", None)
            if getattr(cell, "sector_mechanical_tilt_deg", None) is not None
            else sector_cfg.get("mechanical_tilt_deg", getattr(rf_params, "mechanical_tilt_deg", 0.0))
        ),
        "max_horizontal_attenuation_db": float(
            getattr(cell, "sector_max_horizontal_attenuation_db", None)
            if getattr(cell, "sector_max_horizontal_attenuation_db", None) is not None
            else sector_cfg.get("max_horizontal_attenuation_db", getattr(rf_params, "max_horizontal_attenuation_db", 30.0))
        ),
        "front_to_back_attenuation_db": float(
            getattr(cell, "sector_front_to_back_attenuation_db", None)
            if getattr(cell, "sector_front_to_back_attenuation_db", None) is not None
            else sector_cfg.get("front_to_back_attenuation_db", getattr(rf_params, "front_to_back_attenuation_db", 25.0))
        ),
        "max_vertical_attenuation_db": float(
            getattr(cell, "sector_max_vertical_attenuation_db", None)
            if getattr(cell, "sector_max_vertical_attenuation_db", None) is not None
            else sector_cfg.get("max_vertical_attenuation_db", getattr(rf_params, "max_vertical_attenuation_db", 30.0))
        ),
    }


def _reference_signal_eirp_dbm(rf_params: RFParams, tx_power_dbm_override: float | None = None) -> float:
    """Convert total carrier TX power into a conservative reference-signal EIRP.

    Vendor radios are usually specified in total conducted/output power, while RSRP is
    a reference-signal measurement. Approximate the per-reference-signal source term by
    spreading total power across occupied REs, then apply antenna/feed terms and a
    conservative offset that represents broadcast/reference power being lower than the
    total carrier budget in typical macro deployments.
    """
    total_tx_dbm = float(
        tx_power_dbm_override
        if tx_power_dbm_override is not None
        else (getattr(rf_params, "tx_power_dbm", 0.0) or 0.0)
    )
    num_rb = max(1, int(getattr(rf_params, "num_resource_blocks", 1) or 1))
    occupied_re = max(1, num_rb * 12)
    epre_dbm = total_tx_dbm - 10.0 * math.log10(occupied_re)

    tx_gain_db = float(getattr(rf_params, "tx_antenna_gain_dbi", 0.0) or 0.0)
    feeder_loss_db = float(getattr(rf_params, "tx_feeder_loss_db", 0.0) or 0.0)
    ref_offset_db = float(getattr(rf_params, "reference_signal_offset_db", 0.0) or 0.0)
    return epre_dbm + tx_gain_db - feeder_loss_db + ref_offset_db


def _effective_total_tilt_deg(rf_params: RFParams, sector_params: dict[str, Any] | None = None) -> float:
    """First-order total downtilt model.

    In this planner's 2.5D abstraction we treat electrical and mechanical downtilt as
    additive boresight shifts in the vertical plane. This captures the main coverage
    effect even though it does not model full pattern blooming/asymmetry.

    Math:
      tilt_total_deg = electrical_tilt_deg + mechanical_tilt_deg

    Cause/effect:
    - increasing either tilt term moves the vertical boresight downward
    - that reduces attenuation for UEs whose elevation angle is near the new boresight
    - and increases attenuation for UEs above/below that boresight
    """
    electrical_tilt_deg = float(
        sector_params["electrical_tilt_deg"]
        if sector_params is not None
        else (getattr(rf_params, "electrical_tilt_deg", 0.0) or 0.0)
    )
    mechanical_tilt_deg = float(
        sector_params["mechanical_tilt_deg"]
        if sector_params is not None
        else (getattr(rf_params, "mechanical_tilt_deg", 0.0) or 0.0)
    )
    return electrical_tilt_deg + mechanical_tilt_deg


def _vertical_pattern_attenuation_db(
    distance_m: float,
    tx_height_m: float,
    rx_height_m: float,
    rf_params: RFParams,
    sector_params: dict[str, Any] | None = None,
) -> float:
    """3GPP-style vertical antenna attenuation from downtilt and beamwidth.

    We use a standard quadratic vertical attenuation model:
      A_v = min(12 * ((theta - tilt) / theta_3dB)^2, A_max)

    Definitions:
      theta = atan2(tx_height_m - rx_height_m, distance_m) expressed in degrees
      tilt  = electrical_tilt_deg + mechanical_tilt_deg
      theta_3dB = vertical_beamwidth_deg
      A_max = max_vertical_attenuation_db

    Sign convention:
    - theta > 0 means the UE is below the antenna horizon
    - tilt > 0 means downward tilt

    Exact cause/effect:
    - If theta == tilt, then A_v = 0 dB and the UE is on vertical boresight.
    - If |theta - tilt| grows, attenuation grows quadratically.
    - Narrower beamwidth (smaller theta_3dB) makes the same angular error more costly.
    - Larger total tilt shifts the low-loss region farther away from the mast on the ground.

    Consequence in this model:
    - with 0° tilt, strongest signal tends to occur near the horizon direction
    - with positive downtilt, very near-site users can be penalized while mid-cell users
      close to the main-lobe landing region receive less vertical-pattern loss
    """
    beamwidth_v_deg = float(
        sector_params["beamwidth_v_deg"]
        if sector_params is not None
        else (getattr(rf_params, "vertical_beamwidth_deg", 8.0) or 8.0)
    )
    max_vertical_attenuation_db = float(
        sector_params["max_vertical_attenuation_db"]
        if sector_params is not None
        else (getattr(rf_params, "max_vertical_attenuation_db", 30.0) or 30.0)
    )
    total_tilt_deg = _effective_total_tilt_deg(rf_params, sector_params=sector_params)

    # Elevation angle from TX toward the UE in the vertical plane.
    # theta_deg = arctan(vertical_drop / horizontal_distance)
    # A UE close to the mast has a larger theta_deg; a far UE approaches 0°.
    theta_deg = math.degrees(math.atan2(max(0.0, tx_height_m - rx_height_m), max(distance_m, 1.0)))

    # Angular miss between the antenna boresight and the UE direction.
    # delta_deg = 0 means the UE lies on the main vertical lobe.
    delta_deg = theta_deg - total_tilt_deg
    return min(12.0 * (delta_deg / max(beamwidth_v_deg, 0.1)) ** 2, max_vertical_attenuation_db)


def _horizontal_pattern_attenuation_db(
    bearing_deg: float,
    sector_params: dict[str, Any],
    rf_params: RFParams,
) -> float:
    """Quadratic 3GPP-style horizontal attenuation around the sector boresight.

    Math:
      A_h = min(12 * (delta_phi / phi_3dB)^2, A_h,max)

    Cause/effect:
    - `delta_phi = 0` on boresight, so attenuation is 0 dB.
    - larger azimuth mismatch reduces sector gain quadratically.
    - back-lobe directions are additionally limited by the configured
      front-to-back attenuation floor.
    """
    beamwidth_h_deg = float(sector_params.get("beamwidth_h_deg", getattr(rf_params, "beamwidth_h_deg", 360.0)) or 360.0)
    if beamwidth_h_deg >= 359.9:
        return 0.0
    max_horizontal_attenuation_db = float(
        sector_params.get(
            "max_horizontal_attenuation_db",
            getattr(rf_params, "max_horizontal_attenuation_db", 30.0),
        )
        or 30.0
    )
    front_to_back_attenuation_db = float(
        sector_params.get(
            "front_to_back_attenuation_db",
            getattr(rf_params, "front_to_back_attenuation_db", 25.0),
        )
        or 25.0
    )
    delta_phi_deg = _wrapped_angle_delta_deg(
        float(bearing_deg),
        float(sector_params.get("azimuth_deg", 0.0) or 0.0),
    )
    attenuation_db = min(
        12.0 * (delta_phi_deg / max(beamwidth_h_deg, 0.1)) ** 2,
        max_horizontal_attenuation_db,
    )
    if delta_phi_deg > 90.0:
        attenuation_db = max(attenuation_db, front_to_back_attenuation_db)
    return attenuation_db


def _wrapped_angle_delta_deg(angle_a_deg: float, angle_b_deg: float) -> float:
    delta = (angle_a_deg - angle_b_deg + 180.0) % 360.0 - 180.0
    return abs(delta)


def _scenario_path_loss_db(
    *,
    distance_2d_m: float,
    distance_3d_m: float,
    freq_mhz: float,
    tx_height_m: float,
    rx_height_m: float,
    is_los: bool,
    rf_params: RFParams,
) -> float:
    model = str(getattr(rf_params, "path_loss_model", "legacy") or "legacy").strip().lower()
    scenario = str(getattr(rf_params, "propagation_scenario", "umi_street_canyon") or "umi_street_canyon").strip().lower()
    if model != "3gpp_38901":
        fspl_db = _free_space_path_loss_db(distance_3d_m, freq_mhz)
        if is_los:
            return fspl_db
        legacy_nlos_db = 12.0 + 0.02 * max(0.0, distance_3d_m - 50.0)
        return fspl_db + legacy_nlos_db
    if scenario == "uma":
        return _uma_path_loss_db(
            distance_2d_m=distance_2d_m,
            distance_3d_m=distance_3d_m,
            freq_mhz=freq_mhz,
            tx_height_m=tx_height_m,
            rx_height_m=rx_height_m,
            is_los=is_los,
        )
    return _umi_street_canyon_path_loss_db(
        distance_2d_m=distance_2d_m,
        distance_3d_m=distance_3d_m,
        freq_mhz=freq_mhz,
        tx_height_m=tx_height_m,
        rx_height_m=rx_height_m,
        is_los=is_los,
    )


def _umi_street_canyon_path_loss_db(
    *,
    distance_2d_m: float,
    distance_3d_m: float,
    freq_mhz: float,
    tx_height_m: float,
    rx_height_m: float,
    is_los: bool,
) -> float:
    fc_ghz = max(0.01, freq_mhz / 1000.0)
    d2 = max(10.0, distance_2d_m)
    d3 = max(distance_3d_m, 10.0)
    d_bp = _breakpoint_distance_m(tx_height_m, rx_height_m, fc_ghz)
    pl_los_1 = 32.4 + 21.0 * math.log10(d3) + 20.0 * math.log10(fc_ghz)
    pl_los_2 = (
        32.4
        + 40.0 * math.log10(d3)
        + 20.0 * math.log10(fc_ghz)
        - 9.5 * math.log10(d_bp * d_bp + (tx_height_m - rx_height_m) ** 2)
    )
    pl_los = pl_los_1 if d2 <= d_bp else pl_los_2
    if is_los:
        return pl_los
    pl_nlos = 22.4 + 35.3 * math.log10(d3) + 21.3 * math.log10(fc_ghz) - 0.3 * (rx_height_m - 1.5)
    return max(pl_los, pl_nlos)


def _uma_path_loss_db(
    *,
    distance_2d_m: float,
    distance_3d_m: float,
    freq_mhz: float,
    tx_height_m: float,
    rx_height_m: float,
    is_los: bool,
) -> float:
    fc_ghz = max(0.01, freq_mhz / 1000.0)
    d2 = max(10.0, distance_2d_m)
    d3 = max(distance_3d_m, 10.0)
    d_bp = _breakpoint_distance_m(tx_height_m, rx_height_m, fc_ghz)
    pl_los_1 = 28.0 + 22.0 * math.log10(d3) + 20.0 * math.log10(fc_ghz)
    pl_los_2 = (
        28.0
        + 40.0 * math.log10(d3)
        + 20.0 * math.log10(fc_ghz)
        - 9.0 * math.log10(d_bp * d_bp + (tx_height_m - rx_height_m) ** 2)
    )
    pl_los = pl_los_1 if d2 <= d_bp else pl_los_2
    if is_los:
        return pl_los
    pl_nlos = 13.54 + 39.08 * math.log10(d3) + 20.0 * math.log10(fc_ghz) - 0.6 * (rx_height_m - 1.5)
    return max(pl_los, pl_nlos)


def _breakpoint_distance_m(tx_height_m: float, rx_height_m: float, fc_ghz: float) -> float:
    # 3GPP-style effective environment height of 1 m for first-order median path loss.
    h_bs_prime = max(1.0, tx_height_m - 1.0)
    h_ut_prime = max(1.0, rx_height_m - 1.0)
    c_m_per_s = 3.0e8
    fc_hz = fc_ghz * 1.0e9
    return max(10.0, 4.0 * h_bs_prime * h_ut_prime * fc_hz / c_m_per_s)


# Map MaterialType (fallback path) to config material keys
_MATERIAL_TYPE_TO_CONFIG_KEY = {
    MaterialType.BUILDING: "concrete",
    MaterialType.HOUSE: "wood",
    MaterialType.LARGE_STRUCTURE: "concrete",
    MaterialType.TREES: "wood",
    MaterialType.UNKNOWN: "unknown",
}


def _compute_extra_loss(
    material: MaterialType, obstacles_count: int, rf_params: RFParams
) -> float:
    """
    Compute extra attenuation based on material and obstacle count.

    Config-driven via rf_params.building_attenuation (overall + per-material).
    Formula: max(0, (base_loss + obstacles_count * per_obstacle_loss) * scale - reduction_db)
    """
    atten_cfg = getattr(rf_params, "building_attenuation", None)
    mat_key = _MATERIAL_TYPE_TO_CONFIG_KEY.get(material, "unknown")
    if atten_cfg:
        from .material_penetration import _resolve_attenuation_params

        scale, reduction_db = _resolve_attenuation_params(mat_key, atten_cfg)
    else:
        scale = 0.75
        reduction_db = 2.0

    props = DEFAULT_MATERIAL_DB.get(material)
    if props is None:
        return 0.0

    base_loss = props.base_loss_db
    obstacle_loss = obstacles_count * props.per_obstacle_loss_db
    raw = base_loss + obstacle_loss

    return max(0.0, raw * scale - reduction_db)


