"""OFDM parameters for 5G/LTE systems."""

import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SubcarrierSpacing(str, Enum):
    """OFDM subcarrier spacing options (kHz)."""
    SCS_15 = "15"  # LTE, 5G FR1
    SCS_30 = "30"  # 5G FR1
    SCS_60 = "60"  # 5G FR1
    SCS_120 = "120"  # 5G FR2


@dataclass
class OFDMParams:
    """OFDM physical layer parameters."""
    subcarrier_spacing_khz: float = 15.0  # Subcarrier spacing in kHz
    fft_size: int = 2048  # FFT size (number of subcarriers)
    cyclic_prefix_ratio: float = 0.07  # CP length / symbol length (typical 7% for normal CP)
    num_rb: int = 100  # Number of resource blocks (12 subcarriers per RB)
    channel_bandwidth_mhz: float = 20.0  # Total channel bandwidth in MHz
    
    @property
    def num_subcarriers(self) -> int:
        """Number of active subcarriers (excluding guard bands)."""
        return self.num_rb * 12  # 12 subcarriers per resource block
    
    @property
    def symbol_duration_us(self) -> float:
        """OFDM symbol duration in microseconds."""
        return 1000.0 / self.subcarrier_spacing_khz  # 1 / (SCS in kHz)
    
    @property
    def cp_duration_us(self) -> float:
        """Cyclic prefix duration in microseconds."""
        return self.symbol_duration_us * self.cyclic_prefix_ratio
    
    @property
    def total_symbol_duration_us(self) -> float:
        """Total OFDM symbol duration including CP."""
        return self.symbol_duration_us + self.cp_duration_us
    
    @property
    def effective_bandwidth_mhz(self) -> float:
        """Effective bandwidth (active subcarriers * subcarrier spacing)."""
        return (self.num_subcarriers * self.subcarrier_spacing_khz) / 1000.0


# Predefined OFDM configurations for common standards
OFDM_CONFIGS = {
    "LTE_20MHz": OFDMParams(
        subcarrier_spacing_khz=15.0,
        fft_size=2048,
        cyclic_prefix_ratio=0.07,
        num_rb=100,
        channel_bandwidth_mhz=20.0,
    ),
    "5G_FR1_20MHz": OFDMParams(
        subcarrier_spacing_khz=30.0,
        fft_size=2048,
        cyclic_prefix_ratio=0.07,
        num_rb=51,  # 51 RBs for 20 MHz at 30 kHz SCS
        channel_bandwidth_mhz=20.0,
    ),
    "5G_FR1_100MHz": OFDMParams(
        subcarrier_spacing_khz=30.0,
        fft_size=4096,
        cyclic_prefix_ratio=0.07,
        num_rb=273,  # 273 RBs for 100 MHz at 30 kHz SCS
        channel_bandwidth_mhz=100.0,
    ),
    "5G_FR2_100MHz": OFDMParams(
        subcarrier_spacing_khz=120.0,
        fft_size=4096,
        cyclic_prefix_ratio=0.07,
        num_rb=66,  # 66 RBs for 100 MHz at 120 kHz SCS
        channel_bandwidth_mhz=100.0,
    ),
}


def get_ofdm_config(name: str) -> Optional[OFDMParams]:
    """Get predefined OFDM configuration by name."""
    return OFDM_CONFIGS.get(name)


@dataclass
class MIMOConfig:
    """MIMO antenna configuration."""
    num_tx_antennas: int = 1  # Number of transmit antennas
    num_rx_antennas: int = 1  # Number of receive antennas
    mimo_mode: str = "SISO"  # SISO, SIMO, MISO, MIMO
    
    @property
    def diversity_gain_db(self) -> float:
        """Diversity gain from MIMO (simplified model)."""
        if self.mimo_mode == "SISO":
            return 0.0
        elif self.mimo_mode == "SIMO":
            # Receive diversity: ~3 dB for 2 antennas
            return 3.0 * math.log2(self.num_rx_antennas)
        elif self.mimo_mode == "MISO":
            # Transmit diversity: ~3 dB for 2 antennas
            return 3.0 * math.log2(self.num_tx_antennas)
        elif self.mimo_mode == "MIMO":
            # MIMO gain: min(tx, rx) * 3 dB (simplified)
            return 3.0 * math.log2(min(self.num_tx_antennas, self.num_rx_antennas))
        return 0.0
    
    @property
    def spatial_multiplexing_gain(self) -> float:
        """Spatial multiplexing gain (number of parallel streams)."""
        if self.mimo_mode == "MIMO":
            return min(self.num_tx_antennas, self.num_rx_antennas)
        return 1.0

