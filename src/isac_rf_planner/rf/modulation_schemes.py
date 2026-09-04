"""OFDM modulation schemes and link adaptation parameters."""

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional


class ModulationOrder(str, Enum):
    """Modulation schemes ordered by spectral efficiency."""
    QPSK = "QPSK"
    QAM16 = "16-QAM"
    QAM64 = "64-QAM"
    QAM256 = "256-QAM"
    QAM1024 = "1024-QAM"


@dataclass
class ModulationScheme:
    """Modulation scheme parameters."""
    name: ModulationOrder
    bits_per_symbol: int  # 2 for QPSK, 4 for 16-QAM, etc.
    min_sinr_db: float  # Minimum SINR required (dB)
    coding_rate: float  # Effective coding rate (0.0-1.0)
    spectral_efficiency: float  # bits/s/Hz (bits_per_symbol * coding_rate)


# Standard 5G/LTE modulation schemes with SINR thresholds
# Based on typical link adaptation tables
MODULATION_SCHEMES: Dict[ModulationOrder, ModulationScheme] = {
    ModulationOrder.QPSK: ModulationScheme(
        name=ModulationOrder.QPSK,
        bits_per_symbol=2,
        min_sinr_db=-2.0,  # Very low SINR threshold
        coding_rate=0.5,  # Conservative coding
        spectral_efficiency=1.0,
    ),
    ModulationOrder.QAM16: ModulationScheme(
        name=ModulationOrder.QAM16,
        bits_per_symbol=4,
        min_sinr_db=5.0,
        coding_rate=0.6,
        spectral_efficiency=2.4,
    ),
    ModulationOrder.QAM64: ModulationScheme(
        name=ModulationOrder.QAM64,
        bits_per_symbol=6,
        min_sinr_db=12.0,
        coding_rate=0.7,
        spectral_efficiency=4.2,
    ),
    ModulationOrder.QAM256: ModulationScheme(
        name=ModulationOrder.QAM256,
        bits_per_symbol=8,
        min_sinr_db=20.0,
        coding_rate=0.75,
        spectral_efficiency=6.0,
    ),
    ModulationOrder.QAM1024: ModulationScheme(
        name=ModulationOrder.QAM1024,
        bits_per_symbol=10,
        min_sinr_db=28.0,
        coding_rate=0.8,
        spectral_efficiency=8.0,
    ),
}


def select_modulation(sinr_db: float) -> Optional[ModulationScheme]:
    """
    Select best modulation scheme based on SINR (link adaptation).
    
    Returns the highest-order modulation that can be supported at given SINR.
    """
    best_scheme = None
    best_efficiency = 0.0
    
    for scheme in MODULATION_SCHEMES.values():
        if sinr_db >= scheme.min_sinr_db:
            if scheme.spectral_efficiency > best_efficiency:
                best_efficiency = scheme.spectral_efficiency
                best_scheme = scheme
    
    return best_scheme


def get_modulation_by_name(name: str) -> Optional[ModulationScheme]:
    """Get modulation scheme by name."""
    try:
        mod_order = ModulationOrder(name)
        return MODULATION_SCHEMES.get(mod_order)
    except ValueError:
        return None

