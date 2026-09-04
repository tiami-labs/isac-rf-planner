"""Material RF properties for attenuation calculations."""

import math
from dataclasses import dataclass, field
from typing import Dict, Optional

from ..pipeline.schemas import MaterialType


@dataclass
class MaterialRFProps:
    """RF properties for a material type."""

    base_loss_db: float = 0.0  # Base loss for being in this material type (even with 0 obstacles)
    per_obstacle_loss_db: float = 0.0  # Additional dB per building/structure obstacle
    per_meter_loss_db: float = 0.0  # for trees, you might use per-meter
    penetration_loss_db_per_wall: Optional[Dict[float, float]] = None  # freq_mhz -> loss_db (frequency-dependent)
    
    def get_penetration_loss(self, freq_mhz: float) -> float:
        """
        Get penetration loss for this material at given frequency.
        
        If frequency-dependent data available, interpolates.
        Otherwise returns per_obstacle_loss_db.
        """
        if self.penetration_loss_db_per_wall is None:
            return self.per_obstacle_loss_db  # Fallback to generic loss
        
        freqs = sorted(self.penetration_loss_db_per_wall.keys())
        if not freqs:
            return self.per_obstacle_loss_db
        
        # Clamp to frequency range
        if freq_mhz <= freqs[0]:
            return self.penetration_loss_db_per_wall[freqs[0]]
        if freq_mhz >= freqs[-1]:
            return self.penetration_loss_db_per_wall[freqs[-1]]
        
        # Linear interpolation
        for i in range(len(freqs) - 1):
            if freqs[i] <= freq_mhz <= freqs[i + 1]:
                f1, f2 = freqs[i], freqs[i + 1]
                l1, l2 = self.penetration_loss_db_per_wall[f1], self.penetration_loss_db_per_wall[f2]
                return l1 + (l2 - l1) * (freq_mhz - f1) / (f2 - f1)
        
        return self.per_obstacle_loss_db


DEFAULT_MATERIAL_DB: Dict[MaterialType, MaterialRFProps] = {
    MaterialType.BUILDING: MaterialRFProps(base_loss_db=15.0, per_obstacle_loss_db=12.0),
    MaterialType.HOUSE: MaterialRFProps(base_loss_db=8.0, per_obstacle_loss_db=8.0),
    MaterialType.LARGE_STRUCTURE: MaterialRFProps(base_loss_db=20.0, per_obstacle_loss_db=15.0),
    MaterialType.TREES: MaterialRFProps(base_loss_db=3.0, per_obstacle_loss_db=2.0),
    MaterialType.UNKNOWN: MaterialRFProps(base_loss_db=0.0, per_obstacle_loss_db=0.0),
}


