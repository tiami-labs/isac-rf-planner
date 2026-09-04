"""Frequency-dependent material penetration loss for RF propagation."""

from typing import Any, Dict, Optional, Tuple

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
        # NOTE: The planner applies this loss per encountered obstacle/segment.
        # In 3D Google-mesh mode, a single building can contribute multiple segments
        # (especially when osm_id is missing). These values are tuned to avoid
        # unrealistically "dead" propagation after a few obstacles while still
        # reflecting meaningful O2I / building interaction loss.
        628.0: 6.0,
        1900.0: 9.0,
        2500.0: 10.0,
        3500.0: 12.0,
    },
    "brick": {
        628.0: 5.0,
        1900.0: 7.0,
        2500.0: 8.0,
        3500.0: 10.0,
    },
    "metal": {
        # Keep metal highly attenuating, but not "infinite" (no hard stop).
        # This prevents the engine from collapsing any ray that touches a
        # mesh/OSM segment tagged as metal.
        628.0: 25.0,
        1900.0: 25.0,
        2500.0: 25.0,
        3500.0: 25.0,
    },
    "glass": {
        628.0: 4.0,    # n71 - Low loss
        1900.0: 6.0,   # n25
        2500.0: 7.0,   # n41
        3500.0: 9.0,   # n78
    },
    "unknown": {
        # Unknown is common in Google-mesh mode; keep it moderate.
        628.0: 6.0,
        1900.0: 9.0,
        2500.0: 10.0,
        3500.0: 12.0,
    },
}


def _resolve_attenuation_params(
    material: str, attenuation_config: Optional[Dict[str, Any]] = None
) -> Tuple[float, float]:
    """Resolve scale and reduction_db for a material from config (overall + per-material)."""
    scale = 0.75
    reduction_db = 2.0
    if attenuation_config:
        overall = attenuation_config.get("overall") or {}
        mats = attenuation_config.get("materials") or {}
        mat_cfg = mats.get(material) or mats.get("unknown") or {}
        scale = float(mat_cfg.get("scale", overall.get("scale", 0.75)))
        reduction_db = float(mat_cfg.get("reduction_db", overall.get("reduction_db", 2.0)))
    return scale, reduction_db


def _get_material_loss_from_config(
    material: str, attenuation_config: Dict[str, Any], raw: float
) -> Optional[float]:
    """
    Resolve loss from config. Returns absolute dB if material has direct value, else None.
    Supports: materials.concrete = 9.0 (number) or materials.concrete = {loss_db: 9.0}
    """
    mats = attenuation_config.get("materials") or {}
    mat_val = mats.get(material) or mats.get("unknown")
    if mat_val is None:
        return None
    if isinstance(mat_val, (int, float)):
        return max(0.0, float(mat_val))
    if isinstance(mat_val, dict) and "loss_db" in mat_val:
        return max(0.0, float(mat_val["loss_db"]))
    return None


def get_penetration_loss_for_material(
    material: str,
    freq_mhz: float,
    *,
    attenuation_config: Optional[Dict[str, Any]] = None,
    scale: Optional[float] = None,
    reduction_db: float = 0.0,
) -> float:
    """
    Get penetration loss for a material at a given frequency.

    Config-driven: per-material absolute values (dB) are primary. Optional scale/subtract as fallback.

    Precedence:
      1. materials.concrete = 9.0 (number) or materials.concrete = {loss_db: 9.0} -> use directly
      2. materials.concrete = {scale, reduction_db} -> raw * scale - reduction_db
      3. overall.scale, overall.reduction_db -> raw * scale - reduction_db
    """
    if material not in MATERIAL_PENETRATION_DB:
        material = "unknown"

    freq_dict = MATERIAL_PENETRATION_DB[material]
    freqs = sorted(freq_dict.keys())
    if not freqs:
        raw = 15.0
    elif freq_mhz <= freqs[0]:
        raw = freq_dict[freqs[0]]
    elif freq_mhz >= freqs[-1]:
        raw = freq_dict[freqs[-1]]
    else:
        raw = 15.0
        for i in range(len(freqs) - 1):
            if freqs[i] <= freq_mhz <= freqs[i + 1]:
                f1, f2 = freqs[i], freqs[i + 1]
                l1, l2 = freq_dict[f1], freq_dict[f2]
                raw = l1 + (l2 - l1) * (freq_mhz - f1) / (f2 - f1)
                break

    # Config: per-material absolute values primary; optional scale/subtract fallback
    if attenuation_config:
        direct = _get_material_loss_from_config(material, attenuation_config, raw)
        if direct is not None:
            return direct
        scale, reduction_db = _resolve_attenuation_params(material, attenuation_config)
    else:
        scale = scale if scale is not None else 0.75

    return max(0.0, raw * scale - reduction_db)


def is_material_blocking(material: str, freq_mhz: float) -> bool:
    """
    Check if material effectively blocks RF signal.
    
    Returns True if penetration loss >= 100 dB (effectively blocks).
    """
    loss = get_penetration_loss_for_material(material, freq_mhz)
    return loss >= 100.0


