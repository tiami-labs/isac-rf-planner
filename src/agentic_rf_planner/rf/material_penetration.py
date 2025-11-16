"""Frequency-dependent material penetration loss for RF propagation."""

from typing import Dict

# Frequency-dependent penetration loss (dB per wall/obstacle)
# Based on 3GPP TR 38.901 and empirical measurements
# Key insight: 5G (sub-6 GHz) penetrates most materials except metal
# Lower frequencies (n71 @ 0.628 GHz) penetrate better than higher frequencies (n41 @ 2.5 GHz)

MATERIAL_PENETRATION_DB: Dict[str, Dict[float, float]] = {
    "wood": {
        628.0: 3.0,   # n71 (600 MHz) - Low loss, good penetration
        1900.0: 5.0,  # n25 (1900 MHz)
        2500.0: 6.0,  # n41 (2500 MHz)
        3500.0: 7.0,  # n78 (3500 MHz) - Higher loss at higher frequency
    },
    "concrete": {
        628.0: 12.0,   # n71 - Moderate penetration
        1900.0: 17.0,  # n25 - Higher loss
        2500.0: 21.0,  # n41 - Even higher loss
        3500.0: 25.0,  # n78 - Highest loss
    },
    "brick": {
        628.0: 10.0,   # n71 - Similar to concrete
        1900.0: 15.0,  # n25
        2500.0: 18.0,  # n41
        3500.0: 21.0,  # n78
    },
    "metal": {
        628.0: 100.0,  # n71 - Effectively blocks (5G doesn't penetrate metal)
        1900.0: 100.0, # n25 - Blocks
        2500.0: 100.0, # n41 - Blocks
        3500.0: 100.0, # n78 - Blocks
    },
    "glass": {
        628.0: 4.0,    # n71 - Low loss
        1900.0: 6.0,   # n25
        2500.0: 7.0,   # n41
        3500.0: 9.0,   # n78
    },
    "unknown": {
        628.0: 12.0,   # Default to concrete-like loss
        1900.0: 17.0,
        2500.0: 21.0,
        3500.0: 25.0,
    },
}


def get_penetration_loss_for_material(material: str, freq_mhz: float) -> float:
    """
    Get penetration loss for a material at a given frequency.
    
    Args:
        material: Material type ('wood', 'concrete', 'brick', 'metal', 'glass', 'unknown')
        freq_mhz: Frequency in MHz
    
    Returns:
        Penetration loss in dB per wall/obstacle
    
    Note:
        - Metal structures effectively block RF (100+ dB loss)
        - Lower frequencies penetrate better (n71 > n25 > n41 > n78)
        - Wood has lowest loss, concrete/brick have moderate loss
    """
    if material not in MATERIAL_PENETRATION_DB:
        material = "unknown"
    
    freq_dict = MATERIAL_PENETRATION_DB[material]
    freqs = sorted(freq_dict.keys())
    
    if not freqs:
        return 15.0  # Fallback
    
    # Clamp to frequency range
    if freq_mhz <= freqs[0]:
        return freq_dict[freqs[0]]
    if freq_mhz >= freqs[-1]:
        return freq_dict[freqs[-1]]
    
    # Linear interpolation between known frequencies
    for i in range(len(freqs) - 1):
        if freqs[i] <= freq_mhz <= freqs[i + 1]:
            f1, f2 = freqs[i], freqs[i + 1]
            l1, l2 = freq_dict[f1], freq_dict[f2]
            return l1 + (l2 - l1) * (freq_mhz - f1) / (f2 - f1)
    
    return 15.0  # Fallback


def is_material_blocking(material: str, freq_mhz: float) -> bool:
    """
    Check if material effectively blocks RF signal.
    
    Returns True if penetration loss >= 100 dB (effectively blocks).
    """
    loss = get_penetration_loss_for_material(material, freq_mhz)
    return loss >= 100.0


