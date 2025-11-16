# Physics-Based Ray Behavior & Sectored Antenna Plan

## Objective 1: Physics-Based Ray Behavior with Material Interactions

### Current Problem

**Current Behavior**:
- Rays extend to predetermined grid boundaries (fixed distance rings)
- Material attenuation is applied but doesn't affect ray length
- Rays continue even when signal is too weak to be usable
- No frequency-dependent material penetration
- No OSM material tag extraction

**Desired Behavior**:
- Rays stop when signal drops below usable threshold (RSRP < noise_floor + margin)
- Rays are affected by material interactions along the path
- Material properties from OSM tags (wood, concrete, metal, brick)
- Frequency-dependent penetration (5G penetrates most materials except metal)
- Ray length changes based on cumulative attenuation

---

## Phase 1: OSM Material Tag Extraction

### 1.1 Extract Material from OSM Building Tags

**Location**: `osm_map_provider.py`

**OSM Tags to Check**:
- `building:material` - Direct material tag (wood, concrete, brick, metal, glass, etc.)
- `building` - Building type can hint at material:
  - `residential`, `house` → typically wood (US)
  - `apartments`, `commercial` → concrete/brick
  - `industrial`, `warehouse` → metal/concrete
- `building:construction` - Construction type
- `roof:material` - Roof material (can affect RF)

**Implementation**:
```python
def _extract_building_material(building: dict) -> str:
    """
    Extract material type from OSM building tags.
    
    Returns: 'wood', 'concrete', 'brick', 'metal', 'glass', 'unknown'
    """
    tags = building.get("tags", {})
    
    # Direct material tag
    material = tags.get("building:material", "").lower()
    if material in ("wood", "concrete", "brick", "metal", "steel", "glass"):
        return material
    
    # Infer from building type (US context)
    building_type = tags.get("building", "").lower()
    if building_type in ("residential", "house", "detached", "semi"):
        return "wood"  # Typical US residential
    elif building_type in ("apartments", "commercial", "retail", "office"):
        return "concrete"  # Typical commercial
    elif building_type in ("industrial", "warehouse", "factory"):
        return "metal"  # Typical industrial
    
    # Infer from construction
    construction = tags.get("building:construction", "").lower()
    if "wood" in construction or "timber" in construction:
        return "wood"
    elif "concrete" in construction or "cinder" in construction:
        return "concrete"
    elif "brick" in construction:
        return "brick"
    elif "metal" in construction or "steel" in construction:
        return "metal"
    
    # Default based on clutter type
    # Dense urban → concrete/metal, Suburban → wood
    return "unknown"
```

**Store in Building Dict**:
- Add `material` field to each building during prefetch
- Cache material type for fast lookup during ray queries

---

## Phase 2: Frequency-Dependent Material Penetration

### 2.1 Material Penetration Loss by Frequency

**Location**: `material_models.py`

**Key Insight**: 
- **5G (sub-6 GHz) penetrates most materials** except metal
- **Lower frequencies (n71 @ 0.628 GHz) penetrate better** than higher frequencies (n41 @ 2.5 GHz)
- **Metal structures block RF** regardless of frequency
- **Concrete/brick attenuate more at higher frequencies**

**Material Penetration Loss (dB per wall/obstacle)**:

| Material | n71 (0.628 GHz) | n25 (1.9 GHz) | n41 (2.5 GHz) | n78 (3.5 GHz) |
|----------|----------------|---------------|---------------|---------------|
| Wood | 2-5 dB | 3-7 dB | 4-8 dB | 5-10 dB |
| Concrete | 10-15 dB | 15-20 dB | 18-25 dB | 20-30 dB |
| Brick | 8-12 dB | 12-18 dB | 15-22 dB | 18-25 dB |
| Metal | **BLOCKS** (100+ dB) | **BLOCKS** (100+ dB) | **BLOCKS** (100+ dB) | **BLOCKS** (100+ dB) |
| Glass | 3-6 dB | 4-8 dB | 5-10 dB | 6-12 dB |

**Implementation**:
```python
@dataclass
class MaterialRFProps:
    base_loss_db: float = 0.0
    per_obstacle_loss_db: float = 0.0
    per_meter_loss_db: float = 0.0
    penetration_loss_db_per_wall: Dict[float, float] = None  # freq_mhz -> loss_db
    
    def get_penetration_loss(self, freq_mhz: float) -> float:
        """Get penetration loss for this material at given frequency."""
        if self.penetration_loss_db_per_wall is None:
            return self.per_obstacle_loss_db  # Fallback
        
        # Interpolate between known frequencies
        freqs = sorted(self.penetration_loss_db_per_wall.keys())
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

# Frequency-dependent material properties
MATERIAL_PENETRATION_DB = {
    "wood": {
        628: 3.0,   # n71
        1900: 5.0,  # n25
        2500: 6.0,  # n41
        3500: 7.0,  # n78
    },
    "concrete": {
        628: 12.0,
        1900: 17.0,
        2500: 21.0,
        3500: 25.0,
    },
    "brick": {
        628: 10.0,
        1900: 15.0,
        2500: 18.0,
        3500: 21.0,
    },
    "metal": {
        628: 100.0,  # Effectively blocks
        1900: 100.0,
        2500: 100.0,
        3500: 100.0,
    },
    "glass": {
        628: 4.0,
        1900: 6.0,
        2500: 7.0,
        3500: 9.0,
    },
}
```

---

## Phase 3: Adaptive Ray Termination

### 3.1 Signal Strength-Based Ray Stopping

**Location**: `coverage_grid.py` → New function `build_adaptive_coverage_grid()`

**Concept**: 
- Start with fixed grid generation
- For each ray, compute signal strength incrementally along the path
- Stop ray when RSRP drops below threshold (e.g., noise_floor + 10 dB margin)
- Stop ray if metal structure completely blocks path

**Implementation**:
```python
def build_adaptive_coverage_grid(
    tx: LatLon,
    rf_params: RFParams,
    map_provider: Optional[MapProvider] = None
) -> List[WorldCell]:
    """
    Build coverage grid with adaptive ray termination.
    
    Rays stop when:
    1. Signal strength < noise_floor + margin
    2. Metal structure blocks path (100+ dB loss)
    3. Maximum range reached
    """
    cells: List[WorldCell] = []
    max_r = rf_params.max_range_m
    dr = rf_params.step_m
    dtheta = 5.0
    
    min_rsrp_threshold = rf_params.noise_floor_dbm + 10.0  # 10 dB margin
    
    # For each bearing
    for theta in range(0, 360, int(dtheta)):
        theta_deg = float(theta)
        r = dr
        last_valid_cell = None
        
        while r <= max_r:
            # Project point
            lat, lon = _project_from_tx(tx.lat, tx.lon, r, theta_deg)
            
            # Create temporary cell
            temp_cell = WorldCell(...)
            
            # Compute signal strength incrementally
            if map_provider:
                # Get buildings along ray up to this point
                buildings_along_path = _get_buildings_along_ray_segment(
                    tx, temp_cell, map_provider
                )
                
                # Compute cumulative attenuation
                cumulative_loss_db = 0.0
                metal_blocked = False
                
                for building in buildings_along_path:
                    material = building.get("material", "unknown")
                    penetration_loss = _get_penetration_loss(material, rf_params.freq_mhz)
                    
                    if material == "metal" and penetration_loss >= 100.0:
                        metal_blocked = True
                        break
                    
                    cumulative_loss_db += penetration_loss
                
                if metal_blocked:
                    break  # Ray blocked by metal
                
                # Compute path loss
                fspl = _free_space_path_loss_db(r, rf_params.freq_mhz)
                total_loss = fspl + cumulative_loss_db
                estimated_rsrp = rf_params.tx_power_dbm - total_loss
                
                if estimated_rsrp < min_rsrp_threshold:
                    break  # Signal too weak
                
                # Cell is valid, add it
                temp_cell.actual_path_length_m = r
                temp_cell.cumulative_material_loss_db = cumulative_loss_db
                cells.append(temp_cell)
                last_valid_cell = temp_cell
            
            r += dr
    
    return cells
```

---

## Phase 4: Material-Aware Path Loss Along Ray

### 4.1 Compute Material Loss Per Building

**Location**: `world_builder.py` → New function `_compute_material_loss_along_ray()`

**Concept**:
- For each ray, get list of buildings it passes through
- For each building, extract material from OSM tags
- Apply frequency-dependent penetration loss
- Sum cumulative loss along path

**Implementation**:
```python
def _compute_material_loss_along_ray(
    tx: LatLon,
    cell: WorldCell,
    map_provider: MapProvider,
    freq_mhz: float
) -> Tuple[float, bool, List[dict]]:
    """
    Compute cumulative material loss along ray path.
    
    Returns:
        (cumulative_loss_db, metal_blocked, buildings_along_path)
    """
    if map_provider is None:
        return 0.0, False, []
    
    # Get buildings along ray using quadtree
    buildings_along_path = _get_buildings_along_ray_ordered(tx, cell, map_provider)
    
    cumulative_loss_db = 0.0
    metal_blocked = False
    
    for building in buildings_along_path:
        material = building.get("material", "unknown")
        penetration_loss = _get_penetration_loss_for_material(material, freq_mhz)
        
        if material == "metal" and penetration_loss >= 100.0:
            metal_blocked = True
            break
        
        cumulative_loss_db += penetration_loss
    
    return cumulative_loss_db, metal_blocked, buildings_along_path

def _get_penetration_loss_for_material(material: str, freq_mhz: float) -> float:
    """Get penetration loss for material at frequency."""
    if material not in MATERIAL_PENETRATION_DB:
        return 15.0  # Default concrete-like loss
    
    freq_dict = MATERIAL_PENETRATION_DB[material]
    
    # Find closest frequency or interpolate
    freqs = sorted(freq_dict.keys())
    if freq_mhz <= freqs[0]:
        return freq_dict[freqs[0]]
    if freq_mhz >= freqs[-1]:
        return freq_dict[freqs[-1]]
    
    # Linear interpolation
    for i in range(len(freqs) - 1):
        if freqs[i] <= freq_mhz <= freqs[i + 1]:
            f1, f2 = freqs[i], freqs[i + 1]
            l1, l2 = freq_dict[f1], freq_dict[f2]
            return l1 + (l2 - l1) * (freq_mhz - f1) / (f2 - f1)
    
    return 15.0  # Fallback
```

---

## Phase 5: Update Attenuation Model

### 5.1 Use Material-Specific Loss

**Location**: `attenuation_models.py`

**Changes**:
- Replace generic `_compute_extra_loss()` with material-specific calculation
- Use frequency-dependent penetration loss
- Account for metal blocking (set RSRP to very low value)

**Implementation**:
```python
def _compute_material_loss(
    cell: WorldCell,
    freq_mhz: float,
    buildings_along_path: List[dict]
) -> float:
    """
    Compute material loss based on actual buildings along path.
    
    Uses OSM material tags and frequency-dependent penetration.
    """
    cumulative_loss = 0.0
    
    for building in buildings_along_path:
        material = building.get("material", "unknown")
        loss = _get_penetration_loss_for_material(material, freq_mhz)
        cumulative_loss += loss
    
    return cumulative_loss
```

---

## Objective 2: Sectored Antenna Model (PLAN)

### Overview

Real gNB/eNB deployments use **sectored antennas** with:
- Multiple sectors (typically 3, sometimes 6)
- Each sector has different beam pattern (horizontal/vertical)
- Each sector uses different frequency band
- Each sector has unique PCI (Physical Cell ID)
- Different frequencies → different propagation characteristics

### T-Mobile Configuration Example

**Typical 3-Sector Setup**:
- **Sector 1**: n71 @ 0.628 GHz (600 MHz band) - Long range, good penetration
- **Sector 2**: n25 @ 1.9 GHz (1900 MHz band) - Medium range
- **Sector 3**: n41 @ 2.5 GHz (2500 MHz band) - Short range, high capacity

**Beam Patterns**:
- Each sector covers ~120° horizontal (3 sectors = 360°)
- Vertical beamwidth: ~10-15° (down-tilted)
- Different antenna gains per sector (directional)

---

## Phase 1: Sector Configuration Schema

### 1.1 Add Sector Model to RFParams

**Location**: `schemas.py`

**New Schema**:
```python
class SectorConfig(BaseModel):
    """Configuration for one antenna sector."""
    sector_id: int  # 0, 1, 2 (for 3-sector)
    pci: int  # Physical Cell ID (0-503 for LTE, 0-1007 for 5G)
    freq_mhz: float  # Center frequency
    bandwidth_mhz: float  # Channel bandwidth
    azimuth_deg: float  # Sector pointing direction (0-360)
    horizontal_beamwidth_deg: float  # Horizontal beamwidth (typically 65-120°)
    vertical_beamwidth_deg: float  # Vertical beamwidth (typically 10-15°)
    antenna_gain_dbi: float  # Antenna gain in dBi
    downtilt_deg: float  # Antenna downtilt angle
    tx_power_dbm: float  # Transmit power for this sector

class RFParams(BaseModel):
    # ... existing fields ...
    
    # Sectored antenna configuration
    use_sectored_antenna: bool = False  # Enable multi-sector model
    sectors: List[SectorConfig] = []  # List of sectors (typically 3)
```

### 1.2 Default T-Mobile Configuration

**Location**: `rf/sector_configs.py` (NEW)

```python
def get_tmobile_3sector_config(tx_power_dbm: float = 43.0) -> List[SectorConfig]:
    """
    Get T-Mobile 3-sector configuration.
    
    Sectors:
    - Sector 0: n71 @ 0.628 GHz (600 MHz) - North
    - Sector 1: n25 @ 1.9 GHz (1900 MHz) - Southeast (120°)
    - Sector 2: n41 @ 2.5 GHz (2500 MHz) - Southwest (240°)
    """
    return [
        SectorConfig(
            sector_id=0,
            pci=100,
            freq_mhz=628.0,  # n71
            bandwidth_mhz=20.0,
            azimuth_deg=0.0,  # North
            horizontal_beamwidth_deg=120.0,
            vertical_beamwidth_deg=12.0,
            antenna_gain_dbi=18.0,
            downtilt_deg=3.0,
            tx_power_dbm=tx_power_dbm,
        ),
        SectorConfig(
            sector_id=1,
            pci=101,
            freq_mhz=1900.0,  # n25
            bandwidth_mhz=20.0,
            azimuth_deg=120.0,  # Southeast
            horizontal_beamwidth_deg=120.0,
            vertical_beamwidth_deg=12.0,
            antenna_gain_dbi=18.0,
            downtilt_deg=3.0,
            tx_power_dbm=tx_power_dbm,
        ),
        SectorConfig(
            sector_id=2,
            pci=102,
            freq_mhz=2500.0,  # n41
            bandwidth_mhz=20.0,
            azimuth_deg=240.0,  # Southwest
            horizontal_beamwidth_deg=120.0,
            vertical_beamwidth_deg=12.0,
            antenna_gain_dbi=18.0,
            downtilt_deg=3.0,
            tx_power_dbm=tx_power_dbm,
        ),
    ]
```

---

## Phase 2: Sector Beam Pattern

### 2.1 Antenna Gain Pattern

**Location**: `rf/antenna_patterns.py` (NEW)

**Concept**:
- Antenna gain varies with angle from boresight
- Horizontal pattern: Higher gain in main lobe, lower in side lobes
- Vertical pattern: Affected by downtilt

**Implementation**:
```python
def compute_sector_gain(
    bearing_deg: float,
    sector: SectorConfig,
    distance_m: float
) -> float:
    """
    Compute antenna gain for a ray at given bearing.
    
    Args:
        bearing_deg: Bearing from TX to cell (0-360)
        sector: Sector configuration
        distance_m: Distance (for vertical pattern)
    
    Returns:
        Antenna gain in dBi (can be negative in side lobes)
    """
    # Compute angle from sector boresight
    angle_diff = abs(bearing_deg - sector.azimuth_deg)
    if angle_diff > 180.0:
        angle_diff = 360.0 - angle_diff
    
    # Horizontal pattern (simplified cosine model)
    if angle_diff <= sector.horizontal_beamwidth_deg / 2.0:
        # Within main lobe
        # Gain drops as cosine of angle from boresight
        gain_h = sector.antenna_gain_dbi * math.cos(
            math.radians(angle_diff * 180.0 / sector.horizontal_beamwidth_deg)
        )
    else:
        # Side lobe - much lower gain
        gain_h = sector.antenna_gain_dbi - 20.0  # -20 dB in side lobes
    
    # Vertical pattern (affected by downtilt and distance)
    # Simplified: gain drops with distance due to vertical beamwidth
    # More complex: would use actual vertical pattern
    
    return gain_h
```

---

## Phase 3: Multi-Sector Coverage Grid

### 3.1 Generate Cells Per Sector

**Location**: `coverage_grid.py`

**Concept**:
- Generate separate coverage grid for each sector
- Each sector only covers cells within its beam pattern
- Cells can be covered by multiple sectors (handover regions)

**Implementation**:
```python
def build_sectored_coverage_grid(
    tx: LatLon,
    sectors: List[SectorConfig],
    rf_params: RFParams,
    map_provider: Optional[MapProvider] = None
) -> Dict[int, List[WorldCell]]:
    """
    Build coverage grid for each sector.
    
    Returns:
        Dict mapping sector_id -> List[WorldCell]
    """
    sector_cells: Dict[int, List[WorldCell]] = {}
    
    for sector in sectors:
        cells = []
        max_r = rf_params.max_range_m
        dr = rf_params.step_m
        dtheta = 5.0
        
        # Only generate cells within sector beam pattern
        sector_start_azimuth = (sector.azimuth_deg - sector.horizontal_beamwidth_deg / 2.0) % 360.0
        sector_end_azimuth = (sector.azimuth_deg + sector.horizontal_beamwidth_deg / 2.0) % 360.0
        
        for theta in range(0, 360, int(dtheta)):
            theta_deg = float(theta)
            
            # Check if bearing is within sector beam
            if not _bearing_in_sector(theta_deg, sector_start_azimuth, sector_end_azimuth):
                continue
            
            # Generate cells along this bearing with adaptive termination
            r = dr
            while r <= max_r:
                lat, lon = _project_from_tx(tx.lat, tx.lon, r, theta_deg)
                
                # Compute sector-specific gain
                sector_gain = compute_sector_gain(theta_deg, sector, r)
                
                # Create cell with sector info
                cell = WorldCell(
                    lat=lat, lon=lon,
                    distance_m=r, bearing_deg=theta_deg,
                    sector_id=sector.sector_id,
                    pci=sector.pci,
                    freq_mhz=sector.freq_mhz,
                    sector_gain_dbi=sector_gain,
                    # ... other fields
                )
                
                # Check signal strength with sector-specific parameters
                # Stop if too weak
                
                cells.append(cell)
                r += dr
        
        sector_cells[sector.sector_id] = cells
    
    return sector_cells
```

---

## Phase 4: Sector-Specific Attenuation

### 4.1 Frequency-Dependent Path Loss Per Sector

**Location**: `attenuation_models.py`

**Concept**:
- Each sector uses different frequency → different path loss
- Material penetration loss is frequency-dependent
- Lower frequencies (n71) penetrate better than higher frequencies (n41)

**Implementation**:
```python
def compute_sectored_attenuation_grid(
    world: WorldModel,
    sectors: List[SectorConfig]
) -> Dict[int, AttenuationGrid]:
    """
    Compute attenuation grid for each sector.
    
    Returns:
        Dict mapping sector_id -> AttenuationGrid
    """
    sector_grids: Dict[int, AttenuationGrid] = {}
    
    for sector in sectors:
        # Filter cells for this sector
        sector_cells = [c for c in world.cells if getattr(c, 'sector_id', None) == sector.sector_id]
        
        # Create sector-specific world model
        sector_world = WorldModel(
            tx=world.tx,
            rf_params=world.rf_params,  # May need to override freq_mhz
            cells=sector_cells
        )
        
        # Compute attenuation with sector frequency
        grid = compute_attenuation_grid_for_sector(sector_world, sector)
        sector_grids[sector.sector_id] = grid
    
    return sector_grids
```

---

## Phase 5: UI Visualization

### 5.1 Display Multiple Sectors

**Location**: `ui/static/app.js`

**Concept**:
- Show coverage from each sector with different colors
- Overlay sectors (cells can belong to multiple sectors)
- Show handover regions
- Display sector info (PCI, frequency, beam direction)

**Implementation**:
- Add sector selector in UI
- Color-code heatmap by sector
- Show sector boundaries (beam edges)
- Display best serving sector per cell

---

## Implementation Priority

### Immediate (This Week)
1. **OSM Material Extraction** (Phase 1.1)
2. **Frequency-Dependent Material Loss** (Phase 2.1)
3. **Adaptive Ray Termination** (Phase 3.1)

### Short Term (Next 2 Weeks)
4. **Material-Aware Path Loss** (Phase 4.1)
5. **Sector Configuration Schema** (Objective 2, Phase 1)
6. **Sector Beam Pattern** (Objective 2, Phase 2)

### Medium Term (Next Month)
7. **Multi-Sector Coverage Grid** (Objective 2, Phase 3)
8. **Sector-Specific Attenuation** (Objective 2, Phase 4)
9. **UI Visualization** (Objective 2, Phase 5)

---

## Expected Outcomes

### After Objective 1 (Physics-Based Rays)
- Rays stop when signal too weak (no wasted computation)
- Material-specific attenuation (wood vs concrete vs metal)
- Frequency-dependent penetration (n71 penetrates better than n41)
- Metal structures block rays completely
- More realistic coverage maps

### After Objective 2 (Sectored Antennas)
- Multiple sectors with different frequencies
- Sector-specific beam patterns
- Realistic gNB/eNB deployment model
- Handover regions visible
- Frequency-dependent coverage (n71 extends further than n41)

---

## Files to Create/Modify

### New Files
1. `src/agentic_rf_planner/rf/material_penetration.py` - Frequency-dependent material loss
2. `src/agentic_rf_planner/rf/sector_configs.py` - Sector configurations
3. `src/agentic_rf_planner/rf/antenna_patterns.py` - Antenna gain patterns

### Modified Files
1. `src/agentic_rf_planner/geo/osm_map_provider.py` - Extract material from OSM tags
2. `src/agentic_rf_planner/geo/coverage_grid.py` - Adaptive ray termination
3. `src/agentic_rf_planner/pipeline/world_builder.py` - Material-aware path loss
4. `src/agentic_rf_planner/rf/attenuation_models.py` - Frequency-dependent loss
5. `src/agentic_rf_planner/pipeline/schemas.py` - Add SectorConfig
6. `src/agentic_rf_planner/ui/static/app.js` - Multi-sector visualization

---

**Status**: Plan complete, ready for implementation.


