# Adaptive Ray Propagation Implementation Plan

## Problem Statement

**Current System**: Ray endpoints are fixed grid points that don't adapt to:
- Obstacles in the path
- Signal strength (rays continue even when signal is too weak)
- Material properties (distance doesn't account for path through obstacles)
- LOS/NLOS conditions

**Goal**: Make ray propagation intelligent and adaptive, stopping early when appropriate and adjusting path length for obstacles.

---

## Phase 1: LOS/NLOS Detection and Path Length Adjustment

### 1.1 Add LOS Detection to WorldCell

**File**: `src/agentic_rf_planner/pipeline/schemas.py`

**Changes**:
```python
class WorldCell(BaseModel):
    # ... existing fields ...
    is_los: bool = True  # Line-of-sight flag
    actual_path_length_m: float = 0.0  # Actual path length (may differ from straight-line)
    num_buildings: int = 0  # Explicit building count
    num_trees: int = 0  # Explicit tree/foliage count
    diffraction_flag: bool = False  # True if path involves diffraction
```

### 1.2 Implement LOS Test in World Builder

**File**: `src/agentic_rf_planner/pipeline/world_builder.py`

**New Function**:
```python
def _check_los(tx: LatLon, cell: WorldCell, map_provider: MapProvider) -> bool:
    """
    Check if there's line-of-sight from TX to cell.
    
    Returns False if ray intersects any building polygon (blocked).
    """
    # Cast ray from TX to cell
    # Check if ray intersects any building polygon
    # Return False if blocked, True if clear
```

**Integration**: Call `_check_los()` for each cell during world model building.

### 1.3 Calculate Actual Path Length Through Obstacles

**File**: `src/agentic_rf_planner/geo/physical_spanning.py` or new `ray_propagation.py`

**New Function**:
```python
def compute_actual_path_length(
    tx: LatLon,
    cell: WorldCell,
    map_provider: MapProvider
) -> float:
    """
    Calculate actual path length accounting for obstacles.
    
    If ray passes through buildings:
    - Add path length through building (estimate from polygon intersection)
    - Account for material propagation speed (slower in dense materials)
    
    Returns: actual_path_length_m (>= straight_line_distance)
    """
    straight_distance = cell.distance_m
    
    # Get buildings along ray
    buildings = map_provider.get_buildings_along_ray(tx, cell)
    
    # For each building, compute path length through it
    path_through_buildings = 0.0
    for building in buildings:
        # Compute intersection length (ray segment inside building polygon)
        intersection_length = compute_ray_polygon_intersection_length(
            tx, cell, building["geometry"]
        )
        path_through_buildings += intersection_length
    
    # Effective path length: straight + extra through obstacles
    # Material slows propagation, so effective distance increases
    effective_distance = straight_distance + (path_through_buildings * 1.2)  # 20% penalty
    
    return effective_distance
```

**Rationale**: 
- RF signals travel slower through dense materials (concrete, etc.)
- Path through building is longer than straight-line
- Effective distance = straight + (path_through_building × material_factor)

---

## Phase 2: Adaptive Ray Termination

### 2.1 Signal Strength-Based Termination

**File**: `src/agentic_rf_planner/geo/coverage_grid.py`

**New Function**:
```python
def build_adaptive_coverage_grid(
    tx: LatLon,
    rf_params: RFParams,
    map_provider: Optional[MapProvider] = None
) -> List[WorldCell]:
    """
    Build coverage grid with adaptive ray termination.
    
    Rays stop early if:
    1. Signal strength drops below minimum threshold (e.g., noise_floor + margin)
    2. Path is completely blocked (no viable path exists)
    3. Maximum penetration depth exceeded (too many obstacles)
    """
    cells: List[WorldCell] = []
    max_r = rf_params.max_range_m
    dr = rf_params.step_m
    dtheta = 5.0  # Configurable later
    
    min_rsrp_threshold = rf_params.noise_floor_dbm + 10.0  # 10 dB margin above noise
    
    # For each bearing
    for theta in range(0, 360, int(dtheta)):
        theta_deg = float(theta)
        r = dr
        last_valid_cell = None
        
        while r <= max_r:
            # Project point
            lat, lon = _project_from_tx(tx.lat, tx.lon, r, theta_deg)
            
            # Create temporary cell for evaluation
            temp_cell = WorldCell(
                lat=lat, lon=lon, distance_m=r, bearing_deg=theta_deg,
                dominant_material=MaterialType.UNKNOWN,
                obstacles_count=0, extra_loss_db=0.0
            )
            
            # Check LOS and obstacles
            if map_provider:
                is_los = _check_los(tx, temp_cell, map_provider)
                obstacles_count = estimate_obstacles_along_ray(...)
                material = _estimate_material_from_geometry(...)
            else:
                is_los = True
                obstacles_count = 0
                material = MaterialType.UNKNOWN
            
            # Estimate path loss (quick check)
            fspl = _free_space_path_loss_db(r, rf_params.freq_mhz)
            material_loss = _estimate_material_loss(material, obstacles_count)
            estimated_rsrp = rf_params.tx_power_dbm - fspl - material_loss
            
            # Termination conditions
            if estimated_rsrp < min_rsrp_threshold:
                # Signal too weak, stop this ray
                break
            
            if not is_los and obstacles_count > 5:
                # Too many obstacles, path likely not viable
                break
            
            # Cell is valid, add it
            temp_cell.is_los = is_los
            temp_cell.obstacles_count = obstacles_count
            temp_cell.dominant_material = material
            cells.append(temp_cell)
            last_valid_cell = temp_cell
            
            r += dr
        
        # Optional: Add one "fade-out" cell beyond last valid point
        if last_valid_cell and r <= max_r:
            # Add cell at next distance to show coverage edge
            pass
    
    return cells
```

**Key Features**:
- Rays stop when signal drops below threshold
- Rays stop when too many obstacles block path
- More efficient (fewer cells in blocked directions)

---

## Phase 3: 3GPP-Style Path Loss Models

### 3.1 Implement LOS/NLOS Path Loss Formulas

**File**: `src/agentic_rf_planner/rf/path_loss_models.py` (NEW)

**Content**:
```python
"""3GPP TR 38.901 style path loss models."""

import math
from enum import Enum

class Scenario(str, Enum):
    """3GPP scenario types."""
    UMA = "Urban Macro"  # Urban Macrocell
    UMI = "Urban Micro"  # Urban Microcell (street canyon)
    RMA = "Rural Macro"  # Rural Macrocell
    INDOOR = "Indoor"


def path_loss_los(distance_m: float, freq_mhz: float, scenario: Scenario = Scenario.UMI) -> float:
    """
    3GPP LOS path loss.
    
    For UMi (Urban Micro):
    PL_LOS = 32.4 + 21*log10(d) + 20*log10(f)  [d in meters, f in GHz]
    
    For UMa (Urban Macro):
    PL_LOS = 28.0 + 22*log10(d) + 20*log10(f)
    """
    d_km = distance_m / 1000.0
    f_ghz = freq_mhz / 1000.0
    
    if scenario == Scenario.UMI:
        return 32.4 + 21.0 * math.log10(distance_m) + 20.0 * math.log10(f_ghz)
    elif scenario == Scenario.UMA:
        return 28.0 + 22.0 * math.log10(distance_m) + 20.0 * math.log10(f_ghz)
    elif scenario == Scenario.RMA:
        return 32.4 + 20.0 * math.log10(distance_m) + 20.0 * math.log10(f_ghz)
    else:
        # Fallback to FSPL
        return 32.45 + 20.0 * math.log10(d_km) + 20.0 * math.log10(freq_mhz)


def path_loss_nlos(
    distance_m: float,
    freq_mhz: float,
    scenario: Scenario = Scenario.UMI,
    los_distance_m: Optional[float] = None
) -> float:
    """
    3GPP NLOS path loss.
    
    For UMi:
    PL_NLOS = max(PL_LOS, PL_NLOS_UMi)
    where PL_NLOS_UMi = 35.3*log10(d) + 22.4 + 21.3*log10(f) - 0.3*(h_UT - 1.5)
    
    Simplified (no height): PL_NLOS = 35.3*log10(d) + 22.4 + 21.3*log10(f)
    """
    f_ghz = freq_mhz / 1000.0
    
    if scenario == Scenario.UMI:
        pl_nlos = 35.3 * math.log10(distance_m) + 22.4 + 21.3 * math.log10(f_ghz)
        # NLOS is always >= LOS
        if los_distance_m:
            pl_los = path_loss_los(los_distance_m, freq_mhz, scenario)
            return max(pl_nlos, pl_los)
        return pl_nlos
    elif scenario == Scenario.UMA:
        pl_nlos = 32.4 + 20.0 * math.log10(distance_m) + 20.0 * math.log10(f_ghz) + 17.3
        if los_distance_m:
            pl_los = path_loss_los(los_distance_m, freq_mhz, scenario)
            return max(pl_nlos, pl_los)
        return pl_nlos
    else:
        # Fallback: LOS + fixed NLOS penalty
        return path_loss_los(distance_m, freq_mhz, scenario) + 20.0


def compute_path_loss(
    distance_m: float,
    freq_mhz: float,
    is_los: bool,
    scenario: Scenario = Scenario.UMI,
    actual_path_length_m: Optional[float] = None
) -> float:
    """
    Compute path loss using LOS or NLOS model.
    
    Args:
        distance_m: Straight-line distance
        freq_mhz: Frequency
        is_los: Line-of-sight flag
        scenario: 3GPP scenario
        actual_path_length_m: If provided, use this instead of straight-line distance
    
    Returns:
        Path loss in dB
    """
    d = actual_path_length_m if actual_path_length_m else distance_m
    
    if is_los:
        return path_loss_los(d, freq_mhz, scenario)
    else:
        return path_loss_nlos(d, freq_mhz, scenario, los_distance_m=distance_m)
```

### 3.2 Update Attenuation Calculation

**File**: `src/agentic_rf_planner/rf/attenuation_models.py`

**Changes**:
- Replace `_free_space_path_loss_db()` with `compute_path_loss()` from path_loss_models
- Use `cell.is_los` to select LOS vs NLOS model
- Use `cell.actual_path_length_m` if available, else `cell.distance_m`

---

## Phase 4: Integration with Existing System

### 4.1 Update World Builder

**File**: `src/agentic_rf_planner/pipeline/world_builder.py`

**Changes**:
1. For each cell:
   - Call `_check_los()` → set `cell.is_los`
   - Call `compute_actual_path_length()` → set `cell.actual_path_length_m`
   - Count buildings and trees separately → `cell.num_buildings`, `cell.num_trees`

2. Optionally use adaptive grid generation:
   - Replace `build_coverage_grid()` with `build_adaptive_coverage_grid()`
   - Or keep fixed grid but mark cells as "invalid" if signal too weak

### 4.2 Update Attenuation Models

**File**: `src/agentic_rf_planner/rf/attenuation_models.py`

**Changes**:
```python
# Replace FSPL with 3GPP path loss
from .path_loss_models import compute_path_loss, Scenario

# In compute_attenuation_grid():
for cell in world.cells:
    d = cell.actual_path_length_m if cell.actual_path_length_m > 0 else cell.distance_m
    
    # Use 3GPP path loss model
    pl_base = compute_path_loss(
        distance_m=cell.distance_m,
        freq_mhz=freq_mhz,
        is_los=cell.is_los,
        scenario=Scenario.UMI,  # Or make configurable
        actual_path_length_m=cell.actual_path_length_m
    )
    
    # Material losses (already computed)
    extra_db = _compute_extra_loss(cell.dominant_material, cell.obstacles_count)
    
    # Total path loss
    total_pl = pl_base + extra_db
    
    # RSRP
    rsrp = tx_power_dbm - total_pl + mimo_gain_db
```

---

## Phase 5: Optional Enhancements

### 5.1 Two-Ray Ground Reflection (Near Field)

**File**: `src/agentic_rf_planner/rf/path_loss_models.py`

**Function**:
```python
def two_ray_path_loss(
    distance_m: float,
    freq_mhz: float,
    tx_height_m: float = 30.0,  # Typical base station height
    rx_height_m: float = 1.5,   # Typical UE height
    ground_permittivity: float = 15.0
) -> float:
    """
    Two-ray ground reflection model for near-field (< 200m).
    
    Accounts for direct path + ground-reflected path interference.
    """
    # Compute direct and reflected path lengths
    # Compute reflection coefficient
    # Compute interference pattern
    # Return effective path loss
```

**Usage**: Apply only to LOS cells within 200m of TX.

### 5.2 Diffraction Loss

**File**: `src/agentic_rf_planner/rf/path_loss_models.py`

**Function**:
```python
def compute_diffraction_loss(
    obstacle_height_m: float,
    tx_height_m: float,
    rx_height_m: float,
    distance_m: float,
    freq_mhz: float
) -> float:
    """
    Compute diffraction loss using knife-edge or rounded-edge model.
    
    Used when ray grazes building edge/rooftop (diffraction_flag = True).
    """
    # Knife-edge diffraction model
    # Returns additional loss in dB
```

---

## Implementation Priority

### Immediate (This Week)
1. **Add LOS detection** (Phase 1.1, 1.2)
2. **Add actual_path_length calculation** (Phase 1.3)
3. **Update attenuation to use actual_path_length** (Phase 3.2)

### Short Term (Next 2 Weeks)
4. **Implement 3GPP path loss models** (Phase 3.1)
5. **Update attenuation to use LOS/NLOS models** (Phase 4.2)
6. **Add adaptive ray termination** (Phase 2.1)

### Medium Term (Next Month)
7. **Two-ray ground reflection** (Phase 5.1)
8. **Diffraction loss** (Phase 5.2)
9. **Make scenario configurable** (Urban Macro/Micro/Rural)

---

## Expected Improvements

### Before (Current)
- Fixed grid: 7,200 cells regardless of obstacles
- Straight-line distance only
- No LOS/NLOS distinction
- Rays continue even when signal is too weak

### After (Adaptive)
- Adaptive grid: Fewer cells in blocked directions
- Actual path length accounts for obstacles
- LOS/NLOS path loss models (3GPP-aligned)
- Rays stop when signal too weak
- More realistic coverage maps

### Performance Impact
- **Fewer cells**: Adaptive termination reduces cell count by 20-40% in urban areas
- **More accurate**: Path length adjustment improves accuracy
- **Slightly slower**: LOS detection adds computation, but offset by fewer cells

---

## Files to Create/Modify

### New Files
1. `src/agentic_rf_planner/rf/path_loss_models.py` - 3GPP path loss models
2. `src/agentic_rf_planner/geo/ray_propagation.py` - Ray length calculation (optional)

### Modified Files
1. `src/agentic_rf_planner/pipeline/schemas.py` - Add fields to WorldCell
2. `src/agentic_rf_planner/pipeline/world_builder.py` - Add LOS detection, path length
3. `src/agentic_rf_planner/rf/attenuation_models.py` - Use new path loss models
4. `src/agentic_rf_planner/geo/coverage_grid.py` - Optional adaptive grid generation
5. `src/agentic_rf_planner/geo/osm_map_provider.py` - Add method to get buildings along ray

---

## Testing Strategy

1. **Unit Tests**:
   - LOS detection: Test with known building polygons
   - Path length: Verify it's >= straight-line distance
   - Path loss: Compare 3GPP formulas to reference values

2. **Integration Tests**:
   - Compare coverage maps before/after
   - Verify adaptive termination works
   - Check that blocked rays stop early

3. **Validation**:
   - Compare to real-world measurements (if available)
   - Verify 3GPP formulas match published values

---

## Configuration

Add to `RFParams`:
```python
class RFParams(BaseModel):
    # ... existing fields ...
    
    # Path loss model
    path_loss_scenario: str = "UMI"  # UMA, UMI, RMA, INDOOR
    use_adaptive_rays: bool = True  # Enable adaptive termination
    min_rsrp_threshold_db: float = -110.0  # Stop rays below this RSRP
    max_obstacle_count: int = 5  # Stop rays with more obstacles
    enable_two_ray: bool = False  # Enable two-ray model for near field
    tx_height_m: float = 30.0  # Transmitter height
    rx_height_m: float = 1.5  # Receiver height
```

---

**Status**: Plan complete, ready for implementation.

