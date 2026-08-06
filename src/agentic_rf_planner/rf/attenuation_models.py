"""RF attenuation calculations."""

from __future__ import annotations

import logging
import math

import numpy as np
from collections import defaultdict
from typing import Any, Dict

from ..pipeline.schemas import WorldModel, AttenuationGrid, MaterialType, RFParams
from .material_models import DEFAULT_MATERIAL_DB
from .modulation_schemes import get_modulation_by_name, select_modulation
from .ofdm_params import MIMOConfig, OFDMParams
from .dvt import received_power_to_field_strength_dbuv_m
from .channel_analysis import (
    SPEED_OF_LIGHT_M_S,
    bistatic_echo_power_dbm,
    bistatic_geometry,
    bistatic_geometry_arrays,
    coherent_processing_gain_db,
    free_space_path_loss_db as channel_free_space_path_loss_db,
    thermal_noise_power_dbm,
)

logger = logging.getLogger(__name__)


def compute_attenuation_grid(
    world: WorldModel,
    channel_context: Dict[str, Any] | None = None,
    *,
    include_channel_analysis: bool = True,
) -> AttenuationGrid:
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

    if _is_dvt(world.rf_params):
        base_grid = _compute_single_dvt_grid(world)
        return (
            _apply_channel_analysis(world, base_grid, channel_context)
            if include_channel_analysis
            else base_grid
        )

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
        if (
            bool(getattr(world.rf_params, "terrain_enabled", True))
            and getattr(cell, "z_rx_abs_m", None) is not None
            and getattr(world, "z_tx_abs_m", None) is not None
        ):
            dz_abs = float(cell.z_rx_abs_m) - float(world.z_tx_abs_m)
            d3 = math.sqrt(d2 * d2 + dz_abs * dz_abs)
        sector_params = _resolve_sector_params(cell, world.rf_params, sector_lookup)
        sector_freq_mhz = float(sector_params["freq_mhz"])
        sector_bandwidth_mhz = float(sector_params["channel_bandwidth_mhz"])
        rs_eirp_dbm = _transmit_source_eirp_dbm(
            world.rf_params,
            tx_power_dbm_override=float(sector_params["tx_power_dbm"]),
            tx_antenna_gain_dbi=float(sector_params["tx_antenna_gain_dbi"]),
        )
        carrier_eirp_dbm = _total_carrier_eirp_dbm(
            world.rf_params,
            tx_power_dbm_override=float(sector_params["tx_power_dbm"]),
            tx_antenna_gain_dbi=float(sector_params["tx_antenna_gain_dbi"]),
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
        terrain_loss_db = float(getattr(cell, "terrain_loss_db", 0.0) or 0.0)

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
            penetration_loss_db
            + shadow_loss_db
            + diffraction_loss_db
            + terrain_loss_db
            - canyon_recovery_db,
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
        # Recover total-carrier isotropic power at the candidate point from the
        # reference-signal result.  This is the waveform-independent first-leg
        # input used by bistatic channel analysis.
        carrier_incident_isotropic_dbm = (
            rsrp_uncapped
            - rx_combining_gain_db
            + (carrier_eirp_dbm - rs_eirp_dbm)
        )

        key = (round(cell.lat, 8), round(cell.lon, 8))
        sample_groups[key].append(
            {
                "lat": cell.lat,
                "lon": cell.lon,
                "rsrp_dbm": rsrp,
                "sector_id": str(sector_params["sector_id"]),
                "freq_mhz": sector_freq_mhz,
                "channel_bandwidth_mhz": sector_bandwidth_mhz,
                "incident_power_isotropic_dbm": carrier_incident_isotropic_dbm,
                "source_eirp_at_target_dbm": (
                    carrier_eirp_dbm - horizontal_pattern_loss_db - vertical_pattern_loss_db
                ),
                "extra_loss_db": extra_loss_db,
                "penetration_loss_db": penetration_loss_db,
                "shadow_loss_db": shadow_loss_db,
                "diffraction_loss_db": diffraction_loss_db,
                "canyon_recovery_db": canyon_recovery_db,
                "horizontal_pattern_loss_db": horizontal_pattern_loss_db,
                "vertical_pattern_loss_db": vertical_pattern_loss_db,
                "obstacles_count": int(getattr(cell, "obstacles_count", 0) or 0),
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
    sector_id_order: list[str] = [
        str(s.get("sector_id", "")).strip()
        for s in (getattr(world.rf_params, "sectors", None) or [])
        if str(s.get("sector_id", "")).strip()
    ]
    rsrp_by_sector_lists: dict[str, list[float]] | None
    if sector_id_order:
        rsrp_by_sector_lists = {sid: [] for sid in sector_id_order}
    else:
        rsrp_by_sector_lists = None

    terrain_by_key: dict[tuple[float, float], WorldCell] = {}
    for cell in world.cells:
        key = (round(cell.lat, 8), round(cell.lon, 8))
        if key not in terrain_by_key:
            terrain_by_key[key] = cell

    terrain_loss_out: list[float] = []
    los_terrain_out: list[bool] = []
    terrain_state_out: list[str] = []
    z_ground_out: list[float] = []
    incident_power_isotropic_dbm: list[float] = []
    source_eirp_at_target_dbm: list[float] = []
    tx_target_path_loss_db: list[float] = []
    tx_target_environment_excess_db: list[float] = []
    tx_target_penetration_loss_db: list[float] = []
    tx_target_shadow_loss_db: list[float] = []
    tx_target_diffraction_loss_db: list[float] = []
    tx_target_canyon_recovery_db: list[float] = []
    tx_target_horizontal_pattern_loss_db: list[float] = []
    tx_target_vertical_pattern_loss_db: list[float] = []
    tx_target_obstacles_count: list[int] = []
    tx_target_propagation_mode: list[str] = []

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
        mod_name_out = mod_scheme.name.value

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
        modulation.append(mod_name_out)
        throughput_mbps.append(throughput)
        serving_sector_id.append(str(serving["sector_id"]))
        interferer_count.append(len(same_carrier_interferers))
        top_interferer_rsrp_dbm.append(top_interferer_dbm)
        pilot_pollution_metric_db.append(pollution_metric_db)
        incident_value = float(serving["incident_power_isotropic_dbm"])
        source_value = float(serving["source_eirp_at_target_dbm"])
        incident_power_isotropic_dbm.append(incident_value)
        source_eirp_at_target_dbm.append(source_value)
        tx_target_path_loss_db.append(source_value - incident_value)
        tx_target_environment_excess_db.append(float(serving["extra_loss_db"]))
        tx_target_penetration_loss_db.append(float(serving["penetration_loss_db"]))
        tx_target_shadow_loss_db.append(float(serving["shadow_loss_db"]))
        tx_target_diffraction_loss_db.append(float(serving["diffraction_loss_db"]))
        tx_target_canyon_recovery_db.append(float(serving["canyon_recovery_db"]))
        tx_target_horizontal_pattern_loss_db.append(float(serving["horizontal_pattern_loss_db"]))
        tx_target_vertical_pattern_loss_db.append(float(serving["vertical_pattern_loss_db"]))
        tx_target_obstacles_count.append(int(serving["obstacles_count"]))
        tx_target_propagation_mode.append(str(serving["propagation_mode"]))
        modulation_counts[mod_name_out] = modulation_counts.get(mod_name_out, 0) + 1

        if rsrp_by_sector_lists is not None:
            by_sid = {str(s["sector_id"]): float(s["rsrp_dbm"]) for s in samples}
            for sid in sector_id_order:
                v = by_sid.get(sid)
                rsrp_by_sector_lists[sid].append(float("nan") if v is None else v)

        tkey = (round(float(serving["lat"]), 8), round(float(serving["lon"]), 8))
        tcell = terrain_by_key.get(tkey)
        terrain_loss_out.append(float(getattr(tcell, "terrain_loss_db", 0.0) or 0.0))
        los_terrain_out.append(bool(getattr(tcell, "los_terrain", True)))
        terrain_state_out.append(str(getattr(tcell, "terrain_state", "los") or "los"))
        z_ground_out.append(float(getattr(tcell, "z_ground_m", 0.0) or 0.0))

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

    grid = AttenuationGrid(
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
        rsrp_by_sector=rsrp_by_sector_lists,
        technology="5g_nr",
        received_power_dbm=None,
        field_strength_dbuv_m=None,
        incident_power_isotropic_dbm=incident_power_isotropic_dbm,
        source_eirp_at_target_dbm=source_eirp_at_target_dbm,
        tx_target_path_loss_db=tx_target_path_loss_db,
        tx_target_environment_excess_db=tx_target_environment_excess_db,
        tx_target_penetration_loss_db=tx_target_penetration_loss_db,
        tx_target_shadow_loss_db=tx_target_shadow_loss_db,
        tx_target_diffraction_loss_db=tx_target_diffraction_loss_db,
        tx_target_canyon_recovery_db=tx_target_canyon_recovery_db,
        tx_target_horizontal_pattern_loss_db=tx_target_horizontal_pattern_loss_db,
        tx_target_vertical_pattern_loss_db=tx_target_vertical_pattern_loss_db,
        tx_target_obstacles_count=tx_target_obstacles_count,
        tx_target_propagation_mode=tx_target_propagation_mode,
        waveform="5g_nr",
        terrain_loss_db=terrain_loss_out if terrain_loss_out else None,
        los_terrain=los_terrain_out if los_terrain_out else None,
        terrain_state=terrain_state_out if terrain_state_out else None,
        z_ground_m=z_ground_out if z_ground_out else None,
    )
    return (
        _apply_channel_analysis(world, grid, channel_context)
        if include_channel_analysis
        else grid
    )


def apply_channel_analysis(
    world: WorldModel,
    grid: AttenuationGrid,
    channel_context: Dict[str, Any] | None = None,
) -> AttenuationGrid:
    """Apply the optional waveform-agnostic sensing stage to a completed coverage grid.

    Keeping this as a separate public stage allows the orchestrator to release
    the large forward ``WorldCell`` collection before it builds the reciprocal
    receiver environment.
    """

    return _apply_channel_analysis(world, grid, channel_context)


def _compute_single_dvt_grid(world: WorldModel) -> AttenuationGrid:
    """Compute one-transmitter broadcast coverage into preallocated arrays."""

    rf_params = world.rf_params
    dvt = rf_params.dvt
    if dvt is None:
        raise ValueError("DVT RF parameters require a dvt transmitter object")

    tx_h = float(getattr(rf_params, "tx_height_m", 0.0) or 0.0)
    rx_h = float(getattr(rf_params, "rx_height_m", 1.5) or 1.5)
    freq_mhz = float(dvt.frequency_mhz)
    bandwidth_mhz = float(dvt.bandwidth_mhz)
    waveform = str(dvt.waveform)
    count = len(world.cells)

    cell_lat = np.empty(count, dtype=np.float64)
    cell_lon = np.empty(count, dtype=np.float64)
    received_power_dbm = np.empty(count, dtype=np.float64)
    incident_power_isotropic_dbm = np.empty(count, dtype=np.float64)
    source_eirp_at_target_dbm = np.empty(count, dtype=np.float64)
    tx_target_path_loss_db = np.empty(count, dtype=np.float64)
    tx_target_environment_excess_db = np.empty(count, dtype=np.float32)
    tx_target_penetration_loss_db = np.empty(count, dtype=np.float32)
    tx_target_shadow_loss_db = np.empty(count, dtype=np.float32)
    tx_target_diffraction_loss_db = np.empty(count, dtype=np.float32)
    tx_target_canyon_recovery_db = np.empty(count, dtype=np.float32)
    tx_target_horizontal_pattern_loss_db = np.empty(count, dtype=np.float32)
    tx_target_vertical_pattern_loss_db = np.empty(count, dtype=np.float32)
    tx_target_obstacles_count = np.empty(count, dtype=np.int32)
    field_strength_dbuv_m = np.empty(count, dtype=np.float32)
    carrier_to_noise_db = np.empty(count, dtype=np.float32)
    terrain_loss_out = np.empty(count, dtype=np.float32)
    los_terrain_out = np.empty(count, dtype=np.bool_)
    z_ground_out = np.empty(count, dtype=np.float32)
    tx_target_propagation_mode: list[str] = ["los"] * count
    terrain_state_out: list[str] = ["los"] * count

    noise_floor_dbm = _noise_floor_dbm_for_bandwidth(rf_params, bandwidth_mhz)
    rx_gain_dbi = float(getattr(rf_params, "ue_antenna_gain_dbi", 0.0) or 0.0)

    for index, cell in enumerate(world.cells):
        d2 = max(float(cell.distance_m), 1.0)
        d3 = _three_dimensional_distance_m(d2, tx_h, rx_h)
        if (
            bool(getattr(rf_params, "terrain_enabled", True))
            and getattr(cell, "z_rx_abs_m", None) is not None
            and getattr(world, "z_tx_abs_m", None) is not None
        ):
            dz_abs = float(cell.z_rx_abs_m) - float(world.z_tx_abs_m)
            d3 = math.sqrt(d2 * d2 + dz_abs * dz_abs)

        path_loss_db = _scenario_path_loss_db(
            distance_2d_m=d2,
            distance_3d_m=d3,
            freq_mhz=freq_mhz,
            tx_height_m=tx_h,
            rx_height_m=rx_h,
            is_los=bool(getattr(cell, "is_los", True)),
            rf_params=rf_params,
        )
        penetration_loss_db = float(getattr(cell, "penetration_loss_db", 0.0) or 0.0)
        shadow_loss_db = float(getattr(cell, "shadow_loss_db", 0.0) or 0.0)
        diffraction_loss_db = float(getattr(cell, "diffraction_loss_db", 0.0) or 0.0)
        canyon_recovery_db = float(getattr(cell, "canyon_recovery_db", 0.0) or 0.0)
        terrain_loss_db = float(getattr(cell, "terrain_loss_db", 0.0) or 0.0)
        extra_loss_db = max(
            0.0,
            penetration_loss_db
            + shadow_loss_db
            + diffraction_loss_db
            + terrain_loss_db
            - canyon_recovery_db,
        )

        source_eirp_dbm = dvt.effective_source_eirp_dbm(float(cell.bearing_deg))
        horizontal_pattern_loss_db = max(
            0.0, float(dvt.power.source_eirp_dbm) - source_eirp_dbm
        )
        vertical_pattern_loss_db = _dvt_vertical_pattern_attenuation_db(
            distance_m=d2,
            tx_height_m=tx_h,
            rx_height_m=rx_h,
            rf_params=rf_params,
        )
        precomputed = getattr(cell, "precomputed_rsrp_dbm", None)
        power_dbm = (
            float(precomputed)
            if precomputed is not None
            else (
                source_eirp_dbm
                - path_loss_db
                - extra_loss_db
                - vertical_pattern_loss_db
                + rx_gain_dbi
            )
        )
        incident_isotropic_dbm = power_dbm - rx_gain_dbi
        source_after_pattern_dbm = source_eirp_dbm - vertical_pattern_loss_db

        cell.extra_loss_db = extra_loss_db
        cell_lat[index] = float(cell.lat)
        cell_lon[index] = float(cell.lon)
        received_power_dbm[index] = power_dbm
        incident_power_isotropic_dbm[index] = incident_isotropic_dbm
        source_eirp_at_target_dbm[index] = source_after_pattern_dbm
        tx_target_path_loss_db[index] = source_after_pattern_dbm - incident_isotropic_dbm
        tx_target_environment_excess_db[index] = extra_loss_db
        tx_target_penetration_loss_db[index] = penetration_loss_db
        tx_target_shadow_loss_db[index] = shadow_loss_db
        tx_target_diffraction_loss_db[index] = diffraction_loss_db
        tx_target_canyon_recovery_db[index] = canyon_recovery_db
        tx_target_horizontal_pattern_loss_db[index] = horizontal_pattern_loss_db
        tx_target_vertical_pattern_loss_db[index] = vertical_pattern_loss_db
        tx_target_obstacles_count[index] = int(getattr(cell, "obstacles_count", 0) or 0)
        tx_target_propagation_mode[index] = str(
            getattr(cell, "propagation_mode", "los") or "los"
        )
        field_strength_dbuv_m[index] = received_power_to_field_strength_dbuv_m(
            power_dbm,
            freq_mhz,
            rx_gain_dbi=rx_gain_dbi,
        )
        carrier_to_noise_db[index] = power_dbm - noise_floor_dbm
        terrain_loss_out[index] = terrain_loss_db
        los_terrain_out[index] = bool(getattr(cell, "los_terrain", True))
        terrain_state_out[index] = str(getattr(cell, "terrain_state", "los") or "los")
        z_ground_out[index] = float(getattr(cell, "z_ground_m", 0.0) or 0.0)

    return AttenuationGrid(
        tx=world.tx,
        rf_params=rf_params,
        cell_lat=cell_lat,
        cell_lon=cell_lon,
        rsrp_dbm=received_power_dbm,
        sinr_db=carrier_to_noise_db,
        modulation=[waveform] * count,
        throughput_mbps=[0.0] * count,
        serving_sector_id=[],
        interferer_count=[],
        top_interferer_rsrp_dbm=[],
        pilot_pollution_metric_db=[],
        rsrp_by_sector=None,
        technology="dvt",
        received_power_dbm=received_power_dbm,
        field_strength_dbuv_m=field_strength_dbuv_m,
        carrier_to_noise_db=carrier_to_noise_db,
        incident_power_isotropic_dbm=incident_power_isotropic_dbm,
        source_eirp_at_target_dbm=source_eirp_at_target_dbm,
        tx_target_path_loss_db=tx_target_path_loss_db,
        tx_target_environment_excess_db=tx_target_environment_excess_db,
        tx_target_penetration_loss_db=tx_target_penetration_loss_db,
        tx_target_shadow_loss_db=tx_target_shadow_loss_db,
        tx_target_diffraction_loss_db=tx_target_diffraction_loss_db,
        tx_target_canyon_recovery_db=tx_target_canyon_recovery_db,
        tx_target_horizontal_pattern_loss_db=tx_target_horizontal_pattern_loss_db,
        tx_target_vertical_pattern_loss_db=tx_target_vertical_pattern_loss_db,
        tx_target_obstacles_count=tx_target_obstacles_count,
        tx_target_propagation_mode=tx_target_propagation_mode,
        waveform=waveform,
        terrain_loss_db=terrain_loss_out if count else None,
        los_terrain=los_terrain_out if count else None,
        terrain_state=terrain_state_out if count else None,
        z_ground_m=z_ground_out if count else None,
    )



def _active_frequency_and_bandwidth_hz(rf_params: RFParams) -> tuple[float, float]:
    if _is_dvt(rf_params):
        dvt = rf_params.dvt
        if dvt is None:
            raise ValueError("DVT RF parameters require a dvt transmitter object")
        return float(dvt.fc), float(dvt.bandwidth)
    return (
        float(rf_params.freq_mhz) * 1.0e6,
        float(rf_params.channel_bandwidth_mhz) * 1.0e6,
    )


def _initial_bearing_deg(
    lat1_deg: float,
    lon1_deg: float,
    lat2_deg: float,
    lon2_deg: float,
) -> float:
    lat1 = math.radians(float(lat1_deg))
    lat2 = math.radians(float(lat2_deg))
    dlon = math.radians(float(lon2_deg) - float(lon1_deg))
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _nr_sector_params_for_direct_path(
    rf_params: RFParams,
) -> list[dict[str, Any]]:
    sectors = list(getattr(rf_params, "sectors", None) or [])
    if not sectors:
        sectors = [{"sector_id": "omnidirectional"}]
    resolved: list[dict[str, Any]] = []
    for raw in sectors:
        resolved.append(
            {
                "sector_id": str(raw.get("sector_id", "omnidirectional") or "omnidirectional"),
                "tx_power_dbm": float(raw.get("tx_power_dbm", rf_params.tx_power_dbm)),
                "tx_antenna_gain_dbi": float(
                    raw.get("tx_antenna_gain_dbi", rf_params.tx_antenna_gain_dbi)
                ),
                "azimuth_deg": float(raw.get("azimuth_deg", rf_params.azimuth_deg)),
                "beamwidth_h_deg": float(
                    raw.get("beamwidth_h_deg", rf_params.horizontal_beamwidth_deg)
                ),
                "beamwidth_v_deg": float(
                    raw.get("beamwidth_v_deg", rf_params.vertical_beamwidth_deg)
                ),
                "electrical_tilt_deg": float(
                    raw.get("electrical_tilt_deg", rf_params.electrical_tilt_deg)
                ),
                "mechanical_tilt_deg": float(
                    raw.get("mechanical_tilt_deg", rf_params.mechanical_tilt_deg)
                ),
                "max_horizontal_attenuation_db": float(
                    raw.get(
                        "max_horizontal_attenuation_db",
                        rf_params.max_horizontal_attenuation_db,
                    )
                ),
                "front_to_back_attenuation_db": float(
                    raw.get(
                        "front_to_back_attenuation_db",
                        rf_params.front_to_back_attenuation_db,
                    )
                ),
                "max_vertical_attenuation_db": float(
                    raw.get(
                        "max_vertical_attenuation_db",
                        rf_params.max_vertical_attenuation_db,
                    )
                ),
            }
        )
    return resolved


def _carrier_source_toward_receiver(
    *,
    world: WorldModel,
    receiver_latitude: float,
    receiver_longitude: float,
    receiver_absolute_height_m: float,
    direct_range_m: float,
) -> tuple[float, str, float, float]:
    """Return strongest total-carrier EIRP toward a receiver.

    Returns ``(eirp_after_pattern_dbm, source_id, horizontal_loss_db,
    vertical_loss_db)``.  This operates on physical carrier power, not RSRP.
    """

    rf_params = world.rf_params
    bearing_deg = _initial_bearing_deg(
        world.tx.lat,
        world.tx.lon,
        receiver_latitude,
        receiver_longitude,
    )
    tx_abs_m = float(
        world.z_tx_abs_m
        if world.z_tx_abs_m is not None
        else (
            (getattr(rf_params, "site_altitude_m", 0.0) or 0.0)
            + float(rf_params.tx_height_m)
        )
    )

    if _is_dvt(rf_params):
        dvt = rf_params.dvt
        if dvt is None:
            raise ValueError("DVT RF parameters require a dvt transmitter object")
        vertical_loss_db = _dvt_vertical_pattern_attenuation_absolute_db(
            distance_m=direct_range_m,
            tx_absolute_height_m=tx_abs_m,
            rx_absolute_height_m=receiver_absolute_height_m,
            rf_params=rf_params,
        )
        return (
            float(dvt.effective_source_eirp_dbm(bearing_deg)) - vertical_loss_db,
            "broadcast_antenna",
            0.0,
            vertical_loss_db,
        )

    best: tuple[float, str, float, float] | None = None
    for sector_params in _nr_sector_params_for_direct_path(rf_params):
        horizontal_loss_db = _horizontal_pattern_attenuation_db(
            bearing_deg=bearing_deg,
            sector_params=sector_params,
            rf_params=rf_params,
        )
        vertical_loss_db = _vertical_pattern_attenuation_db(
            distance_m=direct_range_m,
            tx_height_m=tx_abs_m,
            rx_height_m=receiver_absolute_height_m,
            rf_params=rf_params,
            sector_params=sector_params,
        )
        eirp_dbm = (
            _total_carrier_eirp_dbm(
                rf_params,
                tx_power_dbm_override=float(sector_params["tx_power_dbm"]),
                tx_antenna_gain_dbi=float(sector_params["tx_antenna_gain_dbi"]),
            )
            - horizontal_loss_db
            - vertical_loss_db
        )
        candidate = (
            eirp_dbm,
            str(sector_params["sector_id"]),
            horizontal_loss_db,
            vertical_loss_db,
        )
        if best is None or candidate[0] > best[0]:
            best = candidate
    if best is None:
        raise ValueError("No transmitter source is available for channel analysis")
    return best


def _apply_channel_analysis(
    world: WorldModel,
    grid: AttenuationGrid,
    channel_context: Dict[str, Any] | None = None,
) -> AttenuationGrid:
    """Attach per-target link-budget, bistatic geometry, Doppler and quality outputs.

    Geometry, reciprocal-environment sampling, and link-budget arithmetic are
    vectorized.  Large numeric layers stay as compact NumPy arrays until JSON is
    explicitly requested by the REST layer.
    """

    config = getattr(world.rf_params, "channel_analysis", None)
    if config is None:
        return grid

    point_count = len(grid.cell_lat)
    incident_raw = grid.incident_power_isotropic_dbm
    if incident_raw is None or len(incident_raw) != point_count:
        raise ValueError(
            "channel analysis requires one incident_power_isotropic_dbm value per grid point"
        )
    incident = np.asarray(incident_raw, dtype=np.float64)

    path_loss_raw = grid.tx_target_path_loss_db
    if path_loss_raw is not None and len(path_loss_raw) == point_count:
        tx_target_path_loss = np.asarray(path_loss_raw, dtype=np.float64)
    else:
        source_raw = grid.source_eirp_at_target_dbm
        if source_raw is None or len(source_raw) != point_count:
            tx_target_path_loss = np.full(point_count, np.nan, dtype=np.float64)
        else:
            tx_target_path_loss = np.asarray(source_raw, dtype=np.float64) - incident

    frequency_hz, waveform_bandwidth_hz = _active_frequency_and_bandwidth_hz(world.rf_params)
    processing = config.processing
    receiver = config.receiver
    target = config.target
    processing_bandwidth_hz = float(
        processing.processing_bandwidth_hz or waveform_bandwidth_hz
    )
    noise_power_dbm = thermal_noise_power_dbm(
        processing_bandwidth_hz,
        receiver.noise_figure_db,
    )
    processing_gain_db = coherent_processing_gain_db(
        processing_bandwidth_hz,
        processing.coherent_integration_s,
    )
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

    tx_abs_m = float(
        world.z_tx_abs_m
        if world.z_tx_abs_m is not None
        else (
            (getattr(world.rf_params, "site_altitude_m", 0.0) or 0.0)
            + float(world.rf_params.tx_height_m)
        )
    )
    receiver_abs_m = float(receiver.absolute_height_m)
    direct_geometry = bistatic_geometry(
        tx_latitude_deg=world.tx.lat,
        tx_longitude_deg=world.tx.lon,
        tx_altitude_m=tx_abs_m,
        target_latitude_deg=receiver.latitude,
        target_longitude_deg=receiver.longitude,
        target_altitude_m=receiver_abs_m,
        receiver_latitude_deg=receiver.latitude,
        receiver_longitude_deg=receiver.longitude,
        receiver_altitude_m=receiver_abs_m,
        target_motion=config.motion.model_copy(
            update={"speed_mps": 0.0, "climb_rate_mps": 0.0}
        ),
        frequency_hz=frequency_hz,
    )
    direct_source_eirp_dbm, direct_source_id, direct_h_loss_db, direct_v_loss_db = (
        _carrier_source_toward_receiver(
            world=world,
            receiver_latitude=receiver.latitude,
            receiver_longitude=receiver.longitude,
            receiver_absolute_height_m=receiver_abs_m,
            direct_range_m=direct_geometry.direct_tx_receiver_range_m,
        )
    )

    return_lookup = (channel_context or {}).get("return_path_lookup")
    requested_return_model = str(config.return_path_model)
    use_environment_lookup = (
        requested_return_model == "environmental_reciprocal_grid"
        and return_lookup is not None
    )
    direct_environment_excess_db = 0.0
    direct_environment_valid = False
    if use_environment_lookup:
        direct_environment_excess_db, _, _, direct_environment_valid = return_lookup.sample(
            world.tx.lat,
            world.tx.lon,
        )
    direct_fspl_db = channel_free_space_path_loss_db(
        direct_geometry.direct_tx_receiver_range_m,
        frequency_hz,
    )
    direct_total_path_loss_db = (
        direct_fspl_db
        + float(direct_environment_excess_db)
        + float(receiver.direct_path_excess_loss_db)
    )
    direct_received_power_dbm = (
        direct_source_eirp_dbm
        - direct_total_path_loss_db
        + float(receiver.direct_antenna_gain_dbi)
        - float(receiver.feeder_loss_db)
    )
    residual_direct_power_dbm = (
        direct_received_power_dbm - float(processing.direct_path_cancellation_db)
    )

    lat = np.asarray(grid.cell_lat, dtype=np.float64)
    lon = np.asarray(grid.cell_lon, dtype=np.float64)
    z_ground_raw = grid.z_ground_m
    if z_ground_raw is None or len(z_ground_raw) != point_count:
        z_ground = np.zeros(point_count, dtype=np.float64)
    else:
        z_ground = np.asarray(z_ground_raw, dtype=np.float64)
    target_altitude = z_ground + float(target.height_m_agl)

    geometry = bistatic_geometry_arrays(
        tx_latitude_deg=world.tx.lat,
        tx_longitude_deg=world.tx.lon,
        tx_altitude_m=tx_abs_m,
        target_latitude_deg=lat,
        target_longitude_deg=lon,
        target_altitude_m=target_altitude,
        receiver_latitude_deg=receiver.latitude,
        receiver_longitude_deg=receiver.longitude,
        receiver_altitude_m=receiver_abs_m,
        target_motion=config.motion,
        frequency_hz=frequency_hz,
    )

    if use_environment_lookup and hasattr(return_lookup, "sample_many"):
        return_environment_excess_db, _, _, environment_valid = return_lookup.sample_many(
            lat, lon
        )
        return_environment_excess_db = np.asarray(
            return_environment_excess_db, dtype=np.float64
        )
        environment_valid = np.asarray(environment_valid, dtype=np.bool_)
    elif use_environment_lookup:
        sampled = [return_lookup.sample(float(a), float(b)) for a, b in zip(lat, lon, strict=True)]
        return_environment_excess_db = np.fromiter(
            (float(item[0]) for item in sampled), dtype=np.float64, count=point_count
        )
        environment_valid = np.fromiter(
            (bool(item[3]) for item in sampled), dtype=np.bool_, count=point_count
        )
    else:
        return_environment_excess_db = np.zeros(point_count, dtype=np.float64)
        environment_valid = np.zeros(point_count, dtype=np.bool_)

    target_receiver_range = np.asarray(
        geometry["target_receiver_range_m"], dtype=np.float64
    )
    wavelength_m = SPEED_OF_LIGHT_M_S / max(float(frequency_hz), 1.0)
    return_fspl_db = 20.0 * np.log10(
        4.0 * math.pi * np.maximum(target_receiver_range, 1.0) / wavelength_m
    )
    return_loss_db = (
        return_fspl_db
        + return_environment_excess_db
        + float(receiver.return_path_excess_loss_db)
    )
    scattering_term_db = (
        10.0 * math.log10(max(float(target.bistatic_rcs_m2), 1.0e-18))
        + 10.0 * math.log10(4.0 * math.pi)
        - 20.0 * math.log10(wavelength_m)
    )
    echo_power = (
        incident
        - return_loss_db
        + float(receiver.echo_antenna_gain_dbi)
        - float(receiver.feeder_loss_db)
        + scattering_term_db
        - float(processing.system_loss_db)
    )
    pre_snr = echo_power - noise_power_dbm
    post_snr = pre_snr + processing_gain_db - float(processing.processing_loss_db)
    detection_margin = post_snr - float(processing.required_snr_db)
    doppler = np.asarray(geometry["doppler_hz"], dtype=np.float64)
    resolved = np.abs(doppler) >= doppler_detection_threshold_hz
    if max_unambiguous_doppler_hz is None:
        ambiguous = np.zeros(point_count, dtype=np.bool_)
    else:
        ambiguous = np.abs(doppler) > max_unambiguous_doppler_hz
    detectable = (detection_margin >= 0.0) & resolved & ~ambiguous

    quality = np.full(point_count, 5, dtype=np.uint8)
    quality[(detection_margin >= 0.0) & resolved & ~ambiguous & (detection_margin < 15.0)] = 4
    quality[(detection_margin >= 0.0) & resolved & ~ambiguous & (detection_margin < 6.0)] = 3
    quality[(detection_margin >= 0.0) & resolved & ambiguous] = 2
    quality[(detection_margin >= 0.0) & ~resolved] = 1
    quality[detection_margin < 0.0] = 0

    total_path_loss = tx_target_path_loss + return_loss_db
    echo_to_residual = echo_power - residual_direct_power_dbm
    required_dynamic_range = direct_received_power_dbm - echo_power

    best_margin_index = int(np.nanargmax(detection_margin)) if point_count else None
    detectable_indices = np.flatnonzero(detectable)
    best_detectable_index = (
        int(detectable_indices[np.argmax(detection_margin[detectable_indices])])
        if detectable_indices.size
        else None
    )

    quality_labels = {
        0: "below_required_snr",
        1: "doppler_unresolved",
        2: "doppler_ambiguous",
        3: "detectable_low_margin",
        4: "detectable_moderate_margin",
        5: "detectable_high_margin",
    }

    def point_payload(index: int | None) -> dict[str, Any] | None:
        if index is None:
            return None
        return {
            "index": index,
            "latitude": float(lat[index]),
            "longitude": float(lon[index]),
            "incident_power_isotropic_dbm": float(incident[index]),
            "tx_target_path_loss_db": float(tx_target_path_loss[index]),
            "return_path_loss_db": float(return_loss_db[index]),
            "return_environment_excess_db": float(return_environment_excess_db[index]),
            "total_bistatic_path_loss_db": float(total_path_loss[index]),
            "echo_power_dbm": float(echo_power[index]),
            "preprocessing_snr_db": float(pre_snr[index]),
            "postprocessing_snr_db": float(post_snr[index]),
            "detection_margin_db": float(detection_margin[index]),
            "echo_to_residual_direct_db": float(echo_to_residual[index]),
            "required_dynamic_range_db": float(required_dynamic_range[index]),
            "tx_target_range_m": float(geometry["tx_target_range_m"][index]),
            "target_receiver_range_m": float(target_receiver_range[index]),
            "bistatic_path_range_m": float(geometry["bistatic_path_range_m"][index]),
            "excess_path_range_m": float(geometry["excess_path_range_m"][index]),
            "excess_delay_s": float(geometry["excess_delay_s"][index]),
            "bistatic_angle_deg": float(geometry["bistatic_angle_deg"][index]),
            "path_range_rate_mps": float(geometry["path_range_rate_mps"][index]),
            "closing_speed_mps": float(geometry["closing_speed_mps"][index]),
            "doppler_hz": float(doppler[index]),
            "doppler_resolved": bool(resolved[index]),
            "doppler_ambiguous": bool(ambiguous[index]),
            "detectable": bool(detectable[index]),
            "isac_quality_code": int(quality[index]),
            "isac_quality_label": quality_labels[int(quality[index])],
        }

    def f32(values: Any) -> np.ndarray:
        return np.asarray(values, dtype=np.float32)

    grid.bistatic_echo_power_dbm = f32(echo_power)
    grid.bistatic_preprocessing_snr_db = f32(pre_snr)
    grid.bistatic_postprocessing_snr_db = f32(post_snr)
    grid.bistatic_detection_margin_db = f32(detection_margin)
    grid.bistatic_echo_to_residual_direct_db = f32(echo_to_residual)
    grid.bistatic_required_dynamic_range_db = f32(required_dynamic_range)
    grid.bistatic_tx_target_range_m = f32(geometry["tx_target_range_m"])
    grid.bistatic_target_receiver_range_m = f32(target_receiver_range)
    grid.bistatic_return_path_loss_db = f32(return_loss_db)
    grid.bistatic_return_environment_excess_db = f32(return_environment_excess_db)
    grid.bistatic_total_path_loss_db = f32(total_path_loss)
    grid.bistatic_path_range_m = f32(geometry["bistatic_path_range_m"])
    grid.bistatic_excess_path_range_m = f32(geometry["excess_path_range_m"])
    grid.bistatic_excess_delay_s = f32(geometry["excess_delay_s"])
    grid.bistatic_angle_deg = f32(geometry["bistatic_angle_deg"])
    grid.bistatic_path_range_rate_mps = f32(geometry["path_range_rate_mps"])
    grid.bistatic_closing_speed_mps = f32(geometry["closing_speed_mps"])
    grid.bistatic_doppler_hz = f32(doppler)
    grid.bistatic_doppler_resolved = resolved
    grid.bistatic_doppler_ambiguous = ambiguous
    grid.bistatic_detectable = detectable
    grid.bistatic_isac_quality_code = quality

    environment_samples_used = int(np.count_nonzero(environment_valid))
    effective_return_model = (
        "environmental_reciprocal_grid"
        if use_environment_lookup and environment_samples_used > 0
        else "free_space_plus_excess"
    )
    return_lookup_metadata = (
        return_lookup.metadata()
        if use_environment_lookup and hasattr(return_lookup, "metadata")
        else None
    )
    quality_counts_raw = np.bincount(quality, minlength=6)
    grid.channel_analysis_summary = {
        "schema_version": "2.1",
        "model": "waveform_agnostic_bistatic_channel",
        "technology": str(grid.technology),
        "waveform": str(grid.waveform or world.rf_params.waveform or "unknown"),
        "frequency_hz": frequency_hz,
        "waveform_bandwidth_hz": waveform_bandwidth_hz,
        "processing_bandwidth_hz": processing_bandwidth_hz,
        "source_power_basis": "total_carrier_eirp",
        "transmitter": {
            "latitude": world.tx.lat,
            "longitude": world.tx.lon,
            "absolute_height_m": tx_abs_m,
        },
        "receiver": receiver.model_dump(by_alias=True),
        "target": target.model_dump(by_alias=True),
        "motion": config.motion.model_dump(by_alias=True),
        "processing": processing.model_dump(by_alias=True),
        "direct_path": {
            "source_id": direct_source_id,
            "distance_m": direct_geometry.direct_tx_receiver_range_m,
            "source_eirp_after_pattern_dbm": direct_source_eirp_dbm,
            "horizontal_pattern_loss_db": direct_h_loss_db,
            "vertical_pattern_loss_db": direct_v_loss_db,
            "free_space_path_loss_db": direct_fspl_db,
            "environment_excess_loss_db": float(direct_environment_excess_db),
            "environment_sample_valid": bool(direct_environment_valid),
            "configured_excess_loss_db": float(receiver.direct_path_excess_loss_db),
            "total_path_loss_db": direct_total_path_loss_db,
            "receiver_antenna_gain_dbi": float(receiver.direct_antenna_gain_dbi),
            "receiver_feeder_loss_db": float(receiver.feeder_loss_db),
            "received_power_dbm": direct_received_power_dbm,
            "noise_power_dbm": noise_power_dbm,
            "carrier_to_noise_db": direct_received_power_dbm - noise_power_dbm,
            "residual_after_cancellation_dbm": residual_direct_power_dbm,
        },
        "resolution": {
            "delay_resolution_s": delay_resolution_s,
            "bistatic_path_resolution_m": bistatic_path_resolution_m,
            "doppler_resolution_hz": doppler_resolution_hz,
            "doppler_detection_threshold_hz": doppler_detection_threshold_hz,
            "max_unambiguous_doppler_hz": max_unambiguous_doppler_hz,
            "max_unambiguous_path_rate_mps": (
                SPEED_OF_LIGHT_M_S * max_unambiguous_doppler_hz / frequency_hz
                if max_unambiguous_doppler_hz is not None
                else None
            ),
            "return_path_environment": return_lookup_metadata,
        },
        "counts": {
            "grid_points": point_count,
            "return_environment_samples_used": environment_samples_used,
            "doppler_resolved_points": int(np.count_nonzero(resolved)),
            "doppler_ambiguous_points": int(np.count_nonzero(ambiguous)),
            "detectable_points": int(np.count_nonzero(detectable)),
            "quality_counts": {
                quality_labels[code]: int(quality_counts_raw[code])
                for code in sorted(quality_labels)
            },
        },
        "isac_quality": {
            "type": "planner_defined_categorical_quality",
            "code_legend": {str(code): label for code, label in quality_labels.items()},
            "margin_bands_db": {
                "low": [0.0, 6.0],
                "moderate": [6.0, 15.0],
                "high": [15.0, None],
            },
            "note": "This is a deterministic status class, not probability of detection.",
        },
        "best_margin_point": point_payload(best_margin_index),
        "best_detectable_point": point_payload(best_detectable_index),
        "requested_return_path_model": requested_return_model,
        "return_path_model": effective_return_model,
        "assumptions": [
            "transmitter-to-target power uses the active waveform's total-carrier environment-aware one-way propagation result",
            (
                "target-to-receiver and direct paths use a receiver-centered OSM/terrain reciprocal polar lookup plus configured excess loss"
                if effective_return_model == "environmental_reciprocal_grid"
                else "target-to-receiver and direct paths use free-space loss plus configured excess loss because no environmental lookup was available"
            ),
            "positive Doppler means the total transmitter-target-receiver path is shortening",
            "detectable requires non-negative SNR margin, resolved Doppler, and no configured PRF ambiguity",
        ],
    }
    return grid


def _dvt_vertical_pattern_attenuation_db(
    *,
    distance_m: float,
    tx_height_m: float,
    rx_height_m: float,
    rf_params: RFParams,
) -> float:
    dvt = rf_params.dvt
    if dvt is None:
        raise ValueError("DVT vertical pattern requires a dvt transmitter object")
    antenna = dvt.antenna
    theta_deg = math.degrees(
        math.atan2(max(0.0, tx_height_m - rx_height_m), max(float(distance_m), 1.0))
    )
    delta_deg = theta_deg - float(antenna.beam_tilt_deg)
    return min(
        12.0
        * (delta_deg / max(float(antenna.vertical_beamwidth_deg), 0.1)) ** 2,
        float(antenna.max_vertical_attenuation_db),
    )

def _dvt_vertical_pattern_attenuation_absolute_db(
    *,
    distance_m: float,
    tx_absolute_height_m: float,
    rx_absolute_height_m: float,
    rf_params: RFParams,
) -> float:
    """Broadcast vertical-pattern attenuation using absolute endpoint heights."""

    dvt = rf_params.dvt
    if dvt is None:
        raise ValueError("DVT vertical pattern requires a dvt transmitter object")
    antenna = dvt.antenna
    theta_deg = math.degrees(
        math.atan2(
            float(tx_absolute_height_m) - float(rx_absolute_height_m),
            max(float(distance_m), 1.0),
        )
    )
    delta_deg = theta_deg - float(antenna.beam_tilt_deg)
    return min(
        12.0 * (delta_deg / max(float(antenna.vertical_beamwidth_deg), 0.1)) ** 2,
        float(antenna.max_vertical_attenuation_db),
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
            else sector_cfg.get("azimuth_deg", getattr(rf_params, "azimuth_deg", 0.0))
        ),
        "beamwidth_h_deg": float(
            getattr(cell, "sector_beamwidth_h_deg", None)
            if getattr(cell, "sector_beamwidth_h_deg", None) is not None
            else sector_cfg.get("beamwidth_h_deg", getattr(rf_params, "horizontal_beamwidth_deg", 360.0))
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
        "tx_antenna_gain_dbi": _resolve_sector_tx_gain_db(cell, sector_cfg, rf_params),
    }


def _resolve_sector_tx_gain_db(cell: Any, sector_cfg: dict[str, Any], rf_params: RFParams) -> float:
    g_cell = getattr(cell, "sector_tx_antenna_gain_dbi", None)
    if g_cell is not None:
        return float(g_cell)
    g_cfg = sector_cfg.get("tx_antenna_gain_dbi") if sector_cfg else None
    if g_cfg is not None:
        return float(g_cfg)
    return float(getattr(rf_params, "tx_antenna_gain_dbi", 0.0) or 0.0)


def _is_dvt(rf_params: RFParams) -> bool:
    return str(getattr(rf_params, "technology", "5g_nr") or "5g_nr").strip().lower() == "dvt"


def _total_carrier_eirp_dbm(
    rf_params: RFParams,
    tx_power_dbm_override: float | None = None,
    tx_antenna_gain_dbi: float | None = None,
) -> float:
    """Physical total-carrier EIRP before directional pattern attenuation."""

    if _is_dvt(rf_params):
        dvt = getattr(rf_params, "dvt", None)
        if dvt is None:
            raise ValueError("DVT RF parameters require a dvt transmitter object")
        return float(dvt.power.source_eirp_dbm)
    total_tx_dbm = float(
        tx_power_dbm_override
        if tx_power_dbm_override is not None
        else (getattr(rf_params, "tx_power_dbm", 0.0) or 0.0)
    )
    tx_gain_dbi = float(
        tx_antenna_gain_dbi
        if tx_antenna_gain_dbi is not None
        else (getattr(rf_params, "tx_antenna_gain_dbi", 0.0) or 0.0)
    )
    return (
        total_tx_dbm
        + float(getattr(rf_params, "tx_chain_gain_db", 0.0) or 0.0)
        + tx_gain_dbi
        - float(getattr(rf_params, "tx_feeder_loss_db", 0.0) or 0.0)
    )


def _transmit_source_eirp_dbm(
    rf_params: RFParams,
    tx_power_dbm_override: float | None = None,
    tx_antenna_gain_dbi: float | None = None,
) -> float:
    """Return the isotropic source term for the active technology.

    DVT uses the physical source selected by ``DVTPower``: direct ERP, or
    conducted power with TX-chain gain, feeder loss, and antenna gain. The
    directional antenna-pattern attenuation is applied separately per cell.
    NR keeps its occupied-RE/reference-signal allocation while sharing the same
    physical TX-chain fields.
    """
    if _is_dvt(rf_params):
        dvt = getattr(rf_params, "dvt", None)
        if dvt is None:
            raise ValueError("DVT RF parameters require a dvt transmitter object")
        return float(dvt.power.source_eirp_dbm)
    return _reference_signal_eirp_dbm(
        rf_params,
        tx_power_dbm_override=tx_power_dbm_override,
        tx_antenna_gain_dbi=tx_antenna_gain_dbi,
    )


def _reference_signal_eirp_dbm(
    rf_params: RFParams,
    tx_power_dbm_override: float | None = None,
    tx_antenna_gain_dbi: float | None = None,
) -> float:
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

    if tx_antenna_gain_dbi is not None:
        tx_gain_db = float(tx_antenna_gain_dbi)
    else:
        tx_gain_db = float(getattr(rf_params, "tx_antenna_gain_dbi", 0.0) or 0.0)
    tx_chain_gain_db = float(getattr(rf_params, "tx_chain_gain_db", 0.0) or 0.0)
    feeder_loss_db = float(getattr(rf_params, "tx_feeder_loss_db", 0.0) or 0.0)
    ref_offset_db = float(getattr(rf_params, "reference_signal_offset_db", 0.0) or 0.0)
    return epre_dbm + tx_chain_gain_db + tx_gain_db - feeder_loss_db + ref_offset_db


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
    if _is_dvt(rf_params):
        return _free_space_path_loss_db(distance_3d_m, freq_mhz)
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


