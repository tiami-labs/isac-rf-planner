"""RF attenuation calculations."""

import logging
import math
from typing import Dict

from ..pipeline.schemas import WorldModel, AttenuationGrid, MaterialType
from .material_models import DEFAULT_MATERIAL_DB
from .modulation_schemes import select_modulation, get_modulation_by_name, ModulationOrder
from .ofdm_params import OFDMParams, MIMOConfig

logger = logging.getLogger(__name__)


def compute_attenuation_grid(world: WorldModel) -> AttenuationGrid:
    """
    Given a WorldModel (cells with obstacles), compute RSRP, SINR, and link adaptation per cell.

    Model (v2 - Post-Engineering Review):
      RSRP = tx_power_dbm - PL_base(d) - L_material + G_RX
      where:
        - PL_base(d) = FSPL(d) if LOS, else FSPL(d) + L_NLOS(d) if NLOS
        - L_NLOS(d) = 20.0 + 0.1 × max(0, d - 50.0) dB
        - L_material = cumulative material penetration loss along ray
        - G_RX = 0 dB (MIMO doesn't increase RSRP)
      
      SINR = RSRP - noise_floor_dbm - interference_db
      Modulation = selected based on SINR (link adaptation)
      Throughput = spectral_efficiency * bandwidth * MIMO_streams
      
      Noise floor: P_noise = -174 + 10·log₁₀(B_Hz) + NF_dB (if not explicitly set)
    """

    tx_power_dbm = world.rf_params.tx_power_dbm
    freq_mhz = world.rf_params.freq_mhz
    
    # Calculate noise floor from bandwidth + noise figure if not explicitly set
    if world.rf_params.noise_floor_dbm is not None:
        noise_floor_dbm = world.rf_params.noise_floor_dbm
    else:
        # N = -174 dBm/Hz + 10*log10(B_Hz) + NF_dB
        bandwidth_hz = world.rf_params.channel_bandwidth_mhz * 1e6
        noise_floor_dbm = -174.0 + 10.0 * math.log10(bandwidth_hz) + world.rf_params.noise_figure_db
        logger.info(f"Calculated noise floor: {noise_floor_dbm:.2f} dBm (BW={world.rf_params.channel_bandwidth_mhz} MHz, NF={world.rf_params.noise_figure_db} dB)")
    
    # OFDM parameters
    ofdm = OFDMParams(
        subcarrier_spacing_khz=world.rf_params.subcarrier_spacing_khz,
        num_rb=world.rf_params.num_resource_blocks,
        channel_bandwidth_mhz=world.rf_params.channel_bandwidth_mhz,
    )
    
    # MIMO configuration
    mimo = MIMOConfig(
        num_tx_antennas=world.rf_params.num_tx_antennas,
        num_rx_antennas=world.rf_params.num_rx_antennas,
        mimo_mode=world.rf_params.mimo_mode,
    )
    
    mimo_gain_db = mimo.diversity_gain_db
    mimo_streams = mimo.spatial_multiplexing_gain

    cell_lat = []
    cell_lon = []
    rsrp_dbm = []
    sinr_db = []
    modulation = []
    throughput_mbps = []
    
    # Track material distribution for debugging
    material_counts = {}
    total_extra_loss = 0.0
    cells_with_loss = 0
    modulation_counts = {}

    for cell in world.cells:
        # Fix 1: Use straight-line distance only (d_actual was fake accuracy)
        # All NLOS effects are handled via material loss, not geometric distance
        d = max(cell.distance_m, 1.0)  # Straight-line distance only
        
        # Fix 2: LOS/NLOS base path loss models
        # LOS: pure FSPL
        # NLOS: FSPL + base NLOS excess loss (typically +20 dB after some distance)
        is_los = getattr(cell, 'is_los', True)
        fspl_db = _free_space_path_loss_db(d, freq_mhz)
        
        # Add NLOS base excess loss if path is blocked
        # 3GPP-style: NLOS has additional base loss beyond FSPL
        nlos_excess_loss_db = 0.0
        if not is_los:
            # Base NLOS excess loss: typically 15-25 dB depending on environment
            # For now, use a simple model: +20 dB base, with slight distance dependency
            nlos_excess_loss_db = 20.0  # Base NLOS excess loss
            # Optional: add distance-dependent component (e.g., +0.1 dB/m after 50m)
            if d > 50.0:
                nlos_excess_loss_db += 0.1 * (d - 50.0)  # Additional loss for longer NLOS paths
        
        base_path_loss_db = fspl_db + nlos_excess_loss_db
        
        # Objective 1: Use material-aware loss if available (ALWAYS use if present, even if 0)
        # This ensures loss persists after exiting obstacles
        if hasattr(cell, 'cumulative_material_loss_db'):
            # Use pre-computed cumulative material loss (persists along entire ray)
            material_loss_db = cell.cumulative_material_loss_db
        else:
            # Fallback: compute generic material loss (shouldn't happen if coverage_grid was called correctly)
            material_loss_db = _compute_extra_loss(cell.dominant_material, cell.obstacles_count)
        
        # Check if path is blocked by metal
        if hasattr(cell, 'metal_blocked') and cell.metal_blocked:
            # Metal structure blocks signal - set RSRP to very low value
            rsrp = -150.0  # Effectively no signal
        else:
            # Fix 3: MIMO gain should be 0 for RSRP (RSRP is per-antenna power)
            # MIMO affects throughput/reliability, not received power
            # For now, keep it as 0 (or treat as RX combining gain if needed)
            rx_combining_gain_db = 0.0  # Set to 0; MIMO doesn't increase RSRP
            
            # RSRP calculation with LOS/NLOS base path loss
            rsrp = tx_power_dbm - base_path_loss_db - material_loss_db + rx_combining_gain_db
        
        # SINR calculation (simplified: RSRP - noise, no interference model yet)
        # In real systems, interference would come from other cells/sectors
        interference_db = 0.0  # TODO: Add interference model
        sinr = rsrp - noise_floor_dbm - interference_db
        
        # Link adaptation: select modulation based on SINR
        if world.rf_params.enable_link_adaptation and world.rf_params.fixed_modulation is None:
            mod_scheme = select_modulation(sinr)
            if mod_scheme is None:
                mod_scheme = get_modulation_by_name("QPSK")  # Fallback to lowest
        else:
            # Use fixed modulation if specified
            if world.rf_params.fixed_modulation:
                mod_scheme = get_modulation_by_name(world.rf_params.fixed_modulation)
                if mod_scheme is None:
                    logger.warning(f"Unknown fixed modulation {world.rf_params.fixed_modulation}, using QPSK")
                    mod_scheme = get_modulation_by_name("QPSK")
            else:
                mod_scheme = get_modulation_by_name("QPSK")
        
        if mod_scheme is None:
            mod_scheme = get_modulation_by_name("QPSK")  # Final fallback
        
        # Throughput calculation: spectral_efficiency * bandwidth * MIMO_streams
        # Simplified: assumes all RBs are used, no overhead
        throughput = (mod_scheme.spectral_efficiency * 
                     ofdm.effective_bandwidth_mhz * 
                     mimo_streams)

        cell_lat.append(cell.lat)
        cell_lon.append(cell.lon)
        rsrp_dbm.append(rsrp)
        sinr_db.append(sinr)
        modulation.append(mod_scheme.name.value)
        throughput_mbps.append(throughput)

        # store it back if you want
        cell.extra_loss_db = material_loss_db
        
        # Track statistics
        material_counts[cell.dominant_material] = material_counts.get(cell.dominant_material, 0) + 1
        modulation_counts[mod_scheme.name.value] = modulation_counts.get(mod_scheme.name.value, 0) + 1
        total_extra_loss += material_loss_db
        if material_loss_db > 0:
            cells_with_loss += 1

    # Log statistics
    logger.info(f"Material distribution: {material_counts}")
    logger.info(f"Modulation distribution: {modulation_counts}")
    logger.info(f"Cells with material loss: {cells_with_loss}/{len(world.cells)} ({100*cells_with_loss/len(world.cells):.1f}%)")
    logger.info(f"Average extra loss: {total_extra_loss/len(world.cells):.2f} dB")
    logger.info(f"MIMO gain: {mimo_gain_db:.2f} dB, streams: {mimo_streams}")
    
    # Log LOS/NLOS distribution
    los_count = sum(1 for cell in world.cells if getattr(cell, 'is_los', True))
    nlos_count = len(world.cells) - los_count
    logger.info(f"LOS cells: {los_count} ({100*los_count/len(world.cells):.1f}%), NLOS cells: {nlos_count} ({100*nlos_count/len(world.cells):.1f}%)")

    return AttenuationGrid(
        tx=world.tx,
        rf_params=world.rf_params,
        cell_lat=cell_lat,
        cell_lon=cell_lon,
        rsrp_dbm=rsrp_dbm,
        sinr_db=sinr_db,
        modulation=modulation,
        throughput_mbps=throughput_mbps,
    )


def _free_space_path_loss_db(distance_m: float, freq_mhz: float) -> float:
    """
    Standard FSPL formula (d in km, f in MHz):

      FSPL(dB) = 32.45 + 20*log10(d_km) + 20*log10(f_MHz)
    """
    d_km = distance_m / 1000.0
    return 32.45 + 20.0 * math.log10(max(d_km, 1e-3)) + 20.0 * math.log10(freq_mhz)


def _compute_extra_loss(material: MaterialType, obstacles_count: int) -> float:
    """
    Compute extra attenuation based on material and obstacle count.
    
    Formula: base_loss + (obstacles_count * per_obstacle_loss)
    - Base loss: applies even with 0 obstacles (material type itself causes loss)
    - Per-obstacle loss: additional loss for each building/obstacle the ray passes through
    """
    props = DEFAULT_MATERIAL_DB.get(material)
    if props is None:
        return 0.0
    
    base_loss = props.base_loss_db
    obstacle_loss = obstacles_count * props.per_obstacle_loss_db
    
    return base_loss + obstacle_loss


