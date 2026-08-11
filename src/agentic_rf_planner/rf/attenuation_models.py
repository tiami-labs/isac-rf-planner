"""RF attenuation calculations."""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from typing import Any, Dict

import numpy as np

from ..pipeline.schemas import WorldModel, AttenuationGrid, MaterialType, RFParams
from .material_models import DEFAULT_MATERIAL_DB
from .modulation_schemes import get_modulation_by_name, select_modulation
from .ofdm_params import MIMOConfig, OFDMParams
from .dvt import received_power_to_field_strength_dbuv_m
from .bistatic_field import compute_bistatic_field_arrays
from .channel_arrays import LARGE_ARRAY_THRESHOLD_POINTS, terrain_state_label
from .channel_analysis import (
    SPEED_OF_LIGHT_M_S,
    bistatic_echo_power_dbm,
    bistatic_geometry,
    coherent_processing_gain_db,
    free_space_path_loss_db as channel_free_space_path_loss_db,
    thermal_noise_power_dbm,
)

logger = logging.getLogger(__name__)


def compute_attenuation_grid(
    world: WorldModel,
    *,
    apply_channel: bool = True,
    reciprocal_field: Any | None = None,
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
        grid = _compute_single_dvt_grid(world)
        return _apply_channel_analysis(world, grid, reciprocal_field=reciprocal_field) if apply_channel else grid

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
        incident_power_isotropic_dbm.append(float(serving["incident_power_isotropic_dbm"]))
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

    # Solver outputs are already type-normalized. model_construct avoids Pydantic
    # cloning every large list before the grid is immediately consumed internally.
    grid = AttenuationGrid.model_construct(
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
        waveform="5g_nr",
        terrain_loss_db=terrain_loss_out if terrain_loss_out else None,
        los_terrain=los_terrain_out if los_terrain_out else None,
        terrain_state=terrain_state_out if terrain_state_out else None,
        z_ground_m=z_ground_out if z_ground_out else None,
    )
    return _apply_channel_analysis(world, grid, reciprocal_field=reciprocal_field) if apply_channel else grid


def _compute_single_dvt_grid(world: WorldModel, *, capture_path_components: bool = False) -> AttenuationGrid | tuple[AttenuationGrid, np.ndarray, np.ndarray, np.ndarray]:
    """Compute one-transmitter broadcast coverage.

    Large compact plans write directly into NumPy arrays so the one-way field does
    not first exist as millions of boxed Python floats only to be converted later
    by ISAC and export stages. Small/debug plans retain the historical list surface.
    """

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
    array_backed = (
        count > LARGE_ARRAY_THRESHOLD_POINTS
        and getattr(rf_params, "compact_output", None) is not False
    )

    if array_backed:
        cell_lat: Any = np.empty(count, dtype=np.float64)
        cell_lon: Any = np.empty(count, dtype=np.float64)
        received_power_dbm: Any = np.empty(count, dtype=np.float32)
        incident_power_isotropic_dbm: Any = np.empty(count, dtype=np.float32)
        field_strength_dbuv_m: Any = np.empty(count, dtype=np.float32)
        carrier_to_noise_db: Any = np.empty(count, dtype=np.float32)
        terrain_loss_out: Any = np.empty(count, dtype=np.float32)
        los_terrain_out: Any = np.empty(count, dtype=np.bool_)
        z_ground_out: Any = np.empty(count, dtype=np.float32)
        modulation: list[str] = []
        throughput_mbps: list[float] = []
        terrain_state_out: Any = None
    else:
        cell_lat = []
        cell_lon = []
        received_power_dbm = []
        incident_power_isotropic_dbm = []
        field_strength_dbuv_m = []
        carrier_to_noise_db = []
        modulation = []
        throughput_mbps = []
        terrain_loss_out = []
        los_terrain_out = []
        terrain_state_out = []
        z_ground_out = []

    # Optional target-height ISAC capture: emit path/environment/LOS terms from
    # this exact DVT pass so callers do not need to walk every WorldCell twice.
    captured_path = np.empty(count, dtype=np.float32) if capture_path_components else None
    captured_environment = np.empty(count, dtype=np.float32) if capture_path_components else None
    captured_los = np.empty(count, dtype=np.bool_) if capture_path_components else None

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
        vertical_pattern_loss_db = _dvt_vertical_pattern_attenuation_db(
            distance_m=d2,
            tx_height_m=tx_h,
            rx_height_m=rx_h,
            rf_params=rf_params,
        )
        precomputed = getattr(cell, "precomputed_rsrp_dbm", None)
        if precomputed is not None:
            power_dbm = float(precomputed)
        else:
            power_dbm = (
                source_eirp_dbm
                - path_loss_db
                - extra_loss_db
                - vertical_pattern_loss_db
                + rx_gain_dbi
            )

        incident_isotropic_dbm = power_dbm - rx_gain_dbi
        field_dbuv_m = received_power_to_field_strength_dbuv_m(
            power_dbm,
            freq_mhz,
            rx_gain_dbi=rx_gain_dbi,
        )

        cell.extra_loss_db = extra_loss_db
        if capture_path_components:
            captured_path[index] = path_loss_db + extra_loss_db
            captured_environment[index] = extra_loss_db
            captured_los[index] = bool(getattr(cell, "is_los", True)) and bool(getattr(cell, "los_terrain", True))
        if array_backed:
            cell_lat[index] = float(cell.lat)
            cell_lon[index] = float(cell.lon)
            received_power_dbm[index] = power_dbm
            incident_power_isotropic_dbm[index] = incident_isotropic_dbm
            field_strength_dbuv_m[index] = field_dbuv_m
            carrier_to_noise_db[index] = power_dbm - noise_floor_dbm
            terrain_loss_out[index] = terrain_loss_db
            los_terrain_out[index] = bool(getattr(cell, "los_terrain", True))
            z_ground_out[index] = float(getattr(cell, "z_ground_m", 0.0) or 0.0)
        else:
            cell_lat.append(float(cell.lat))
            cell_lon.append(float(cell.lon))
            received_power_dbm.append(power_dbm)
            incident_power_isotropic_dbm.append(incident_isotropic_dbm)
            field_strength_dbuv_m.append(field_dbuv_m)
            carrier_to_noise_db.append(power_dbm - noise_floor_dbm)
            modulation.append(waveform)
            throughput_mbps.append(0.0)
            terrain_loss_out.append(terrain_loss_db)
            los_terrain_out.append(bool(getattr(cell, "los_terrain", True)))
            terrain_state_out.append(str(getattr(cell, "terrain_state", "los") or "los"))
            z_ground_out.append(float(getattr(cell, "z_ground_m", 0.0) or 0.0))

    # Internal trusted construction keeps DVT legacy aliases on the same storage:
    # rsrp_dbm == received_power_dbm and sinr_db == carrier_to_noise_db.
    grid = AttenuationGrid.model_construct(
        tx=world.tx,
        rf_params=rf_params,
        cell_lat=cell_lat,
        cell_lon=cell_lon,
        rsrp_dbm=received_power_dbm,
        sinr_db=carrier_to_noise_db,
        modulation=modulation,
        throughput_mbps=throughput_mbps,
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
        waveform=waveform,
        terrain_loss_db=terrain_loss_out,
        los_terrain=los_terrain_out,
        terrain_state=terrain_state_out,
        z_ground_m=z_ground_out,
    )
    if capture_path_components:
        assert captured_path is not None and captured_environment is not None and captured_los is not None
        return grid, captured_path, captured_environment, captured_los
    return grid



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



def apply_channel_analysis(
    world: WorldModel,
    grid: AttenuationGrid,
    *,
    reciprocal_field: Any | None = None,
    illumination_field: Any | None = None,
) -> AttenuationGrid:
    """Public fusion step for adding waveform-agnostic bistatic outputs to a one-way field."""

    return _apply_channel_analysis(
        world, grid, reciprocal_field=reciprocal_field, illumination_field=illumination_field
    )

def _apply_channel_analysis(
    world: WorldModel,
    grid: AttenuationGrid,
    *,
    reciprocal_field: Any | None = None,
    illumination_field: Any | None = None,
) -> AttenuationGrid:
    """Attach waveform-agnostic one-way and bistatic channel outputs.

    Large plans keep derived layers in compact NumPy arrays attached to the grid's
    private channel store.  Only small/debug responses materialize those arrays as
    Python lists.  This prevents ISAC analysis from multiplying memory use by the
    number of output layers.
    """

    config = getattr(world.rf_params, "channel_analysis", None)
    if config is None:
        return grid
    communication_incident = grid.channel_array("incident_power_isotropic_dbm")
    incident = (
        getattr(illumination_field, "incident_power_dbm", None)
        if illumination_field is not None else communication_incident
    )
    if incident is None or len(incident) != len(grid.cell_lat):
        raise ValueError(
            "channel analysis requires one incident_power_isotropic_dbm value per grid point"
        )

    frequency_hz, waveform_bandwidth_hz = _active_frequency_and_bandwidth_hz(world.rf_params)
    receiver = config.receiver

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
        target_motion=config.motion.model_copy(update={"speed_mps": 0.0, "climb_rate_mps": 0.0}),
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
    direct_fspl_db = channel_free_space_path_loss_db(
        direct_geometry.direct_tx_receiver_range_m,
        frequency_hz,
    )
    # When an RX-centered environmental field exists, reuse its sampled direct
    # RX<->TX environmental penalties by reciprocity, but recompute the baseline
    # free-space/scenario term with the actual TX and analysis-RX endpoint heights.
    # This avoids a third large world solve while making direct-path cancellation
    # depend on the same mapped environment as the return field.
    use_environment_direct = (
        reciprocal_field is not None
        and str(config.return_path_model) == "environment_reciprocal"
        and math.isfinite(float(getattr(reciprocal_field, "direct_environment_loss_db", math.nan)))
    )
    if use_environment_direct:
        dz_m = tx_abs_m - receiver_abs_m
        d3_m = float(direct_geometry.direct_tx_receiver_range_m)
        d2_m = math.sqrt(max(1.0, d3_m * d3_m - dz_m * dz_m))
        direct_environment_loss_db = float(reciprocal_field.direct_environment_loss_db)
        direct_terrain_loss_db = float(reciprocal_field.direct_terrain_loss_db)
        direct_los = bool(reciprocal_field.direct_los)
        direct_sample_error_m = float(reciprocal_field.direct_sample_error_m)
        direct_scenario_db = _scenario_path_loss_db(
            distance_2d_m=d2_m,
            distance_3d_m=d3_m,
            freq_mhz=frequency_hz / 1.0e6,
            tx_height_m=float(world.rf_params.tx_height_m),
            rx_height_m=float(receiver.antenna_height_m_agl),
            is_los=direct_los,
            rf_params=world.rf_params,
        )
        direct_propagation_loss_db = direct_scenario_db + direct_environment_loss_db
        direct_model = "environment_reciprocal_direct_sample"
    else:
        direct_environment_loss_db = 0.0
        direct_terrain_loss_db = 0.0
        direct_los = True
        direct_sample_error_m = 0.0
        direct_scenario_db = direct_fspl_db
        direct_propagation_loss_db = direct_fspl_db
        direct_model = "free_space_plus_excess"
    direct_received_power_dbm = (
        direct_source_eirp_dbm
        - direct_propagation_loss_db
        - float(receiver.direct_path_excess_loss_db)
        + float(receiver.direct_antenna_gain_dbi)
        - float(receiver.feeder_loss_db)
    )
    residual_direct_power_dbm = (
        direct_received_power_dbm - float(config.processing.direct_path_cancellation_db)
    )

    point_count = len(grid.cell_lat)
    # Default large-plan behavior is compact.  Explicit compact_output=False keeps
    # legacy JSON/list materialization for callers that knowingly accept the memory cost.
    materialize_public_lists = (
        point_count <= LARGE_ARRAY_THRESHOLD_POINTS
        or getattr(world.rf_params, "compact_output", None) is False
    )

    target_latitudes: Any = grid.cell_lat
    target_longitudes: Any = grid.cell_lon
    target_ground: Any = grid.z_ground_m
    incident_values: Any = incident
    if not materialize_public_lists:
        # Cache authoritative aligned numerical sources once.  Downstream heatmaps
        # and NPZ export consume these arrays directly instead of reconverting lists.
        target_latitudes = np.asarray(grid.cell_lat, dtype=np.float64)
        target_longitudes = np.asarray(grid.cell_lon, dtype=np.float64)
        incident_values = np.asarray(incident, dtype=np.float32)
        target_ground = (
            np.asarray(grid.z_ground_m, dtype=np.float32)
            if grid.z_ground_m is not None and len(grid.z_ground_m) == point_count
            else None
        )
        grid.set_channel_array("cell_lat", target_latitudes)
        grid.set_channel_array("cell_lon", target_longitudes)
        grid.set_channel_array("incident_power_isotropic_dbm", incident_values)
        if target_ground is not None:
            grid.set_channel_array("z_ground_m", target_ground)

    # Keep communications coverage and target-height illumination distinct. ISAC
    # fusion consumes the target-height field; communications layers retain their
    # original receiver-height values.
    grid.set_channel_array("isac_incident_power_isotropic_dbm", np.asarray(incident_values, dtype=np.float32))
    if illumination_field is not None:
        grid.set_channel_array("isac_tx_target_path_loss_db", np.asarray(illumination_field.path_loss_db, dtype=np.float32))
        grid.set_channel_array("isac_tx_target_environment_loss_db", np.asarray(illumination_field.environment_loss_db, dtype=np.float32))
        grid.set_channel_array("isac_tx_target_terrain_loss_db", np.asarray(illumination_field.terrain_loss_db, dtype=np.float32))
        grid.set_channel_array("isac_tx_target_los", np.asarray(illumination_field.los, dtype=np.bool_))
        grid.set_channel_array("isac_tx_target_sample_error_m", np.asarray(illumination_field.sample_error_m, dtype=np.float32))

    field = compute_bistatic_field_arrays(
        target_latitude_deg=target_latitudes,
        target_longitude_deg=target_longitudes,
        target_ground_m=target_ground,
        incident_isotropic_power_dbm=incident_values,
        tx_latitude_deg=world.tx.lat,
        tx_longitude_deg=world.tx.lon,
        tx_altitude_m=tx_abs_m,
        config=config,
        frequency_hz=frequency_hz,
        waveform_bandwidth_hz=waveform_bandwidth_hz,
        direct_received_power_dbm=direct_received_power_dbm,
        residual_direct_power_dbm=residual_direct_power_dbm,
        reciprocal_field=reciprocal_field,
    )
    arrays = field.arrays
    for name, values in arrays.mapping().items():
        grid.set_channel_array(name, values)

    if materialize_public_lists:
        for name, values in arrays.mapping().items():
            if name == "return_terrain_state_code":
                continue
            setattr(grid, name, values.tolist())
        grid.return_terrain_state = [
            terrain_state_label(v) for v in arrays.return_terrain_state_code.tolist()
        ]
    else:
        # Do not keep stale/duplicate large public lists if this grid is reused.
        for name in arrays.mapping():
            if name != "return_terrain_state_code" and hasattr(grid, name):
                setattr(grid, name, None)
        grid.return_terrain_state = None

    def point_payload(index: int | None) -> dict[str, Any] | None:
        if index is None:
            return None
        return {
            "index": index,
            "latitude": float(target_latitudes[index]),
            "longitude": float(target_longitudes[index]),
            "incident_power_isotropic_dbm": float(incident_values[index]),
            "tx_target_path_loss_db": (float(illumination_field.path_loss_db[index]) if illumination_field is not None else None),
            "tx_target_environment_loss_db": (float(illumination_field.environment_loss_db[index]) if illumination_field is not None else None),
            "tx_target_terrain_loss_db": (float(illumination_field.terrain_loss_db[index]) if illumination_field is not None else None),
            "tx_target_los": (bool(illumination_field.los[index]) if illumination_field is not None else None),
            "tx_target_sample_error_m": (float(illumination_field.sample_error_m[index]) if illumination_field is not None else None),
            "echo_power_dbm": float(arrays.echo_power_dbm[index]),
            "preprocessing_snr_db": float(arrays.preprocessing_snr_db[index]),
            "postprocessing_snr_db": float(arrays.postprocessing_snr_db[index]),
            "detection_margin_db": float(arrays.detection_margin_db[index]),
            "tx_target_range_m": float(arrays.tx_target_range_m[index]),
            "target_receiver_range_m": float(arrays.target_receiver_range_m[index]),
            "bistatic_path_range_m": float(arrays.bistatic_path_range_m[index]),
            "excess_path_range_m": float(arrays.excess_path_range_m[index]),
            "excess_delay_s": float(arrays.excess_delay_s[index]),
            "bistatic_angle_deg": float(arrays.bistatic_angle_deg[index]),
            "path_range_rate_mps": float(arrays.path_range_rate_mps[index]),
            "closing_speed_mps": float(arrays.closing_speed_mps[index]),
            "doppler_hz": float(arrays.doppler_hz[index]),
            "doppler_sensitivity_hz_per_mps": float(arrays.doppler_sensitivity_hz_per_mps[index]),
            "motion_doppler_sensitivity_hz_per_mps": float(arrays.motion_doppler_sensitivity_hz_per_mps[index]),
            "minimum_detectable_speed_mps": float(arrays.minimum_detectable_speed_mps[index]),
            "doppler_resolved": bool(arrays.doppler_resolved[index]),
            "doppler_ambiguous": bool(arrays.doppler_ambiguous[index]),
            "snr_noise_interference_ok": bool(arrays.thermal_snr_ok[index]),
            "direct_residual_margin_db": float(arrays.direct_residual_margin_db[index]),
            "direct_residual_ok": bool(arrays.direct_residual_ok[index]),
            "required_cancellation_db": float(arrays.required_cancellation_db[index]),
            "required_dynamic_range_db": float(arrays.required_dynamic_range_db[index]),
            "dynamic_range_margin_db": float(arrays.dynamic_range_margin_db[index]),
            "dynamic_range_ok": bool(arrays.dynamic_range_ok[index]),
            "minimum_detectable_rcs_m2": float(arrays.minimum_detectable_rcs_m2[index]),
            "rcs_margin_db": float(arrays.rcs_margin_db[index]),
            "return_environment_valid": bool(arrays.return_environment_valid[index]),
            "detectable_screening": bool(arrays.detectable_screening[index]),
            "detectable_qualified": bool(arrays.detectable_qualified[index]),
            "detectable": bool(arrays.detectable[index]),
            "constraint_failure_code": int(arrays.constraint_failure_code[index]),
            "return_path_loss_db": float(arrays.return_path_loss_db[index]),
            "return_environment_loss_db": float(arrays.return_environment_loss_db[index]),
            "return_terrain_loss_db": float(arrays.return_terrain_loss_db[index]),
            "return_los": bool(arrays.return_los[index]),
            "return_terrain_state": terrain_state_label(arrays.return_terrain_state_code[index]),
            "return_sample_error_m": float(arrays.return_sample_error_m[index]),
        }

    use_environment_return = field.return_path_model == "environment_reciprocal"
    grid.channel_analysis_summary = {
        "schema_version": "2.0",
        "model": "waveform_agnostic_bistatic_channel",
        "technology": str(grid.technology),
        "waveform": str(grid.waveform or world.rf_params.waveform or "unknown"),
        "frequency_hz": frequency_hz,
        "waveform_bandwidth_hz": waveform_bandwidth_hz,
        "processing_bandwidth_hz": field.processing_bandwidth_hz,
        "thermal_noise_power_dbm": field.thermal_noise_power_dbm,
        "interference_plus_clutter_power_dbm": field.interference_plus_clutter_power_dbm,
        "effective_noise_plus_interference_dbm": field.noise_power_dbm,
        "ideal_processing_gain_db": field.ideal_processing_gain_db,
        "effective_processing_gain_db": field.processing_gain_db,
        "processing_gain_source": field.processing_gain_source,
        "processing_qualified": field.processing_qualified,
        "source_power_basis": "total_carrier_eirp",
        "pipeline": {
            "execution": "sequential_memory_bounded",
            "stages": [
                "physical_world", "communications_tx_field", "release_communications_world_cells",
                "target_height_tx_field", "release_target_height_world_cells",
                "rx_reciprocal_field", "direct_path_sample", "isac_scene_fusion",
                "release_reciprocal_field", "render_and_export",
            ],
            "interactive_reanalysis": "reusable_scene_basis_O(N)_without_world_rebuild",
        },
        "receiver": receiver.model_dump(by_alias=True),
        "target": config.target.model_dump(by_alias=True),
        "motion": config.motion.model_dump(by_alias=True),
        "processing": config.processing.model_dump(by_alias=True),
        "storage": {
            "array_backed": not materialize_public_lists,
            "float_dtype": "float32",
            "coordinate_dtype": "float64",
            "terrain_state_encoding": field.terrain_state_encoding,
        },
        "direct_path": {
            "source_id": direct_source_id,
            "distance_m": direct_geometry.direct_tx_receiver_range_m,
            "source_eirp_after_pattern_dbm": direct_source_eirp_dbm,
            "horizontal_pattern_loss_db": direct_h_loss_db,
            "vertical_pattern_loss_db": direct_v_loss_db,
            "free_space_path_loss_db": direct_fspl_db,
            "scenario_path_loss_db": direct_scenario_db,
            "propagation_path_loss_db": direct_propagation_loss_db,
            "model": direct_model,
            "environment_loss_db": direct_environment_loss_db,
            "terrain_loss_db": direct_terrain_loss_db,
            "los": direct_los,
            "sample_error_m": direct_sample_error_m,
            "environment_sample_endpoint_height_note": (
                "environment/terrain obstruction terms are sampled from the reciprocal lattice at candidate-target height; "
                "the baseline scenario term is recomputed with actual TX/RX endpoint heights"
                if use_environment_direct else None
            ),
            "configured_excess_loss_db": float(receiver.direct_path_excess_loss_db),
            "receiver_antenna_gain_dbi": float(receiver.direct_antenna_gain_dbi),
            "receiver_feeder_loss_db": float(receiver.feeder_loss_db),
            "received_power_dbm": direct_received_power_dbm,
            "thermal_noise_power_dbm": field.thermal_noise_power_dbm,
            "interference_plus_clutter_power_dbm": field.interference_plus_clutter_power_dbm,
            "effective_noise_plus_interference_dbm": field.noise_power_dbm,
            "carrier_to_noise_db": direct_received_power_dbm - field.thermal_noise_power_dbm,
            "carrier_to_noise_plus_interference_db": direct_received_power_dbm - field.noise_power_dbm,
            "residual_after_cancellation_dbm": residual_direct_power_dbm,
        },
        "resolution": {
            "delay_resolution_s": field.delay_resolution_s,
            "bistatic_path_resolution_m": field.bistatic_path_resolution_m,
            "doppler_resolution_hz": field.doppler_resolution_hz,
            "doppler_detection_threshold_hz": field.doppler_detection_threshold_hz,
            "max_unambiguous_doppler_hz": field.max_unambiguous_doppler_hz,
            "max_unambiguous_path_rate_mps": (
                SPEED_OF_LIGHT_M_S * field.max_unambiguous_doppler_hz / frequency_hz
                if field.max_unambiguous_doppler_hz is not None
                else None
            ),
        },
        "counts": {
            "grid_points": point_count,
            "doppler_resolved_points": field.doppler_resolved_count,
            "doppler_ambiguous_points": field.doppler_ambiguous_count,
            "direct_residual_ok_points": field.direct_residual_ok_count,
            "dynamic_range_ok_points": field.dynamic_range_ok_count,
            "environment_return_valid_points": field.return_environment_valid_count,
            "screening_detectable_points": field.screening_count,
            "qualified_detectable_points": field.detectable_count,
            "detectable_points": field.detectable_count,
        },
        "best_margin_point": point_payload(field.best_margin_index),
        "best_screening_point": point_payload(field.best_screening_index),
        "best_detectable_point": point_payload(field.best_detectable_index),
        "tx_target_illumination": (dict(illumination_field.metadata) if illumination_field is not None else {
            "model": "communications_receiver_height_field_fallback",
            "target_height_m_agl": float(config.target.height_m_agl),
        }),
        "model_fidelity": {
            "target_height_tx_environment_field": illumination_field is not None,
            "target_to_rx_environment_field": use_environment_return,
            "direct_path_exact_environment_ray": False,
            "direct_path_environment_method": (
                "reciprocal_lattice_sample_with_actual_endpoint_scenario_term"
                if use_environment_direct else "free_space_plus_configured_excess"
            ),
            "building_clearance_model": "existing_2d_footprint_ray_with_provider_slice_height_when_available",
            "note": (
                "Terrain and endpoint heights are represented in the dedicated target-height and reciprocal solves; "
                "the current building obstruction model is not a general 3D building-clearance ray tracer."
            ),
        },
        "return_path_model": field.return_path_model,
        "return_path": (
            dict(reciprocal_field.metadata) if use_environment_return else {
                "model": "free_space_plus_excess",
                "fallback_reason": (
                    "reciprocal field was not supplied"
                    if str(config.return_path_model) == "environment_reciprocal"
                    else None
                ),
            }
        ),
        "assumptions": [
            (
                "transmitter-to-target power uses a dedicated target-height environmental propagation field"
                if illumination_field is not None
                else "transmitter-to-target power falls back to the communications receiver-height one-way field"
            ),
            (
                "target-to-receiver path uses an RX-centered reciprocal environmental propagation field plus configured excess loss"
                if use_environment_return
                else "target-to-receiver path uses free-space loss plus configured excess loss"
            ),
            (
                "the direct transmitter-to-analysis-receiver baseline uses the RX-centered environmental sample plus the actual direct endpoint-height scenario term"
                if use_environment_direct
                else "the direct transmitter-to-analysis-receiver baseline uses free-space loss plus configured direct excess loss"
            ),
            "positive Doppler means the total transmitter-target-receiver path is shortening",
            "Doppler sensitivity is exported as local ENU Hz/(m/s), so motion hypotheses can be changed without rebuilding propagation",
            "screening detectability requires SNR, Doppler, direct-residual, optional dynamic-range, and requested environmental-return constraints",
            "processing-qualified detectability requires explicit effectiveProcessingGainDb and, by default, an explicit interferencePlusClutterPowerDbm; otherwise results are screening-only",
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


