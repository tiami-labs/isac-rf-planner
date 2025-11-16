# RF Attenuation Model: Mathematical Description

## Overview

This document describes the complete mathematical model for RF signal attenuation from the transmitter (TX) to the end of each ray in the coverage grid.

**Current Version:** v2 (Post-Engineering Review)  
**Last Updated:** After implementing 5 key fixes based on RF engineering review

**Model Status:** 
- ✅ Physically consistent and mathematically sound
- ✅ Suitable for first-pass large-scale coverage visualization
- ✅ Aligned with 3GPP-style RF planning approximations
- ⚠️ Not calibrated for < 5-10 dB accuracy vs measurements (requires site-specific tuning)

---

## 1. Ray Generation

Rays are generated in a polar grid around the TX:
- **Bearing (θ)**: 0° to 360° in steps of `dθ = 5°`
- **Distance (r)**: From `dr` to `max_range_m` in steps of `dr = step_m` (default 5m)

For each ray at bearing θ, cells are created incrementally at distances:
```
r ∈ {dr, 2*dr, 3*dr, ..., max_range_m}
```

---

## 2. Free Space Path Loss (FSPL)

**Formula:**
```
FSPL(dB) = 32.45 + 20·log₁₀(d_km) + 20·log₁₀(f_MHz)
```

Where:
- `d_km = distance_m / 1000` (distance in kilometers)
- `f_MHz` = frequency in MHz (e.g., 3500 for n78 band)

**Physical Meaning:**
- First term (32.45): Constant accounting for unit conversion
- Second term: Distance-dependent loss (20 dB per decade of distance)
- Third term: Frequency-dependent loss (20 dB per decade of frequency)

**Example:**
- Distance: 100m = 0.1 km
- Frequency: 3500 MHz
- FSPL = 32.45 + 20·log₁₀(0.1) + 20·log₁₀(3500)
- FSPL = 32.45 - 20 + 70.9 = **83.35 dB**

---

## 3. Material Penetration Loss

### 3.1 Cumulative Loss Along Ray

As the ray propagates, it accumulates material loss for each building/obstacle it passes through:

```
cumulative_material_loss_db = Σ L_penetration(building_i, f)
```

Where:
- `L_penetration(material, f)` = frequency-dependent penetration loss (dB per wall/obstacle)
- Sum is over all buildings along the ray from TX to current point

### 3.2 Frequency-Dependent Penetration Loss

Penetration loss is interpolated from lookup table:

```
L_penetration(material, f) = {
  L(f₁) + (L(f₂) - L(f₁)) · (f - f₁) / (f₂ - f₁)  if f₁ ≤ f ≤ f₂
  L(f_min)                                           if f < f_min
  L(f_max)                                           if f > f_max
}
```

**Material Loss Values (dB per wall/obstacle):**

| Material | 628 MHz (n71) | 1900 MHz (n25) | 2500 MHz (n41) | 3500 MHz (n78) |
|----------|---------------|----------------|----------------|----------------|
| Wood     | 3.0           | 5.0            | 6.0            | 7.0            |
| Concrete | 12.0          | 17.0           | 21.0           | 25.0           |
| Brick    | 10.0          | 15.0           | 18.0           | 21.0           |
| Metal    | 100.0         | 100.0          | 100.0          | 100.0          |
| Glass    | 4.0           | 6.0            | 7.0            | 9.0            |
| Unknown  | 12.0          | 17.0           | 21.0           | 25.0           |

**Key Properties:**
- **Metal blocks completely**: L ≥ 100 dB → ray terminates immediately
- **Lower frequencies penetrate better**: n71 (628 MHz) < n25 (1900 MHz) < n41 (2500 MHz) < n78 (3500 MHz)
- **Wood has lowest loss**, concrete/brick have moderate loss

### 3.3 Tree/Foliage Loss

If ray passes through forest/vegetation:
```
cumulative_material_loss_db += L_penetration("wood", f)
```

---

## 4. Path Length Calculation

**Fix 1: Use straight-line distance only**

All path loss calculations use straight-line distance:
```
d = r  (polar distance from TX)
```

**Rationale:** The extra geometric distance inside buildings (tens of meters) is small compared to total path length (hundreds of meters). The dominant effect is **attenuation**, not the tiny extra distance. All NLOS effects are handled via material loss, not geometric distance.

---

## 5. RSRP Calculation

### 5.1 Main Formula

```
RSRP(dBm) = P_tx - PL_base(d) - L_material + G_RX
```

Where:
- `P_tx` = transmitter power (dBm), default: 43 dBm
- `PL_base(d)` = base path loss (dB), using straight-line distance `d`
- `L_material` = cumulative material loss (dB)
- `G_RX` = RX combining gain (dB), typically 0 (MIMO doesn't increase RSRP)

### 5.2 Base Path Loss (LOS/NLOS)

**Fix 2: LOS/NLOS base path loss models**

```
PL_base(d) = {
  FSPL(d)                    if LOS (is_los = True)
  FSPL(d) + L_NLOS(d)       if NLOS (is_los = False)
}
```

Where:
- `FSPL(d)` = free space path loss (dB)
- `L_NLOS(d)` = NLOS excess loss (dB):
  - Base: +20.0 dB (typical for urban environments)
  - Distance-dependent: +0.1 dB/m after 50m
  - Formula: `L_NLOS = 20.0 + 0.1 × max(0, d - 50.0)`

**Implementation:**
```python
if not is_los:
    nlos_excess_loss_db = 20.0  # Base NLOS excess loss
    if d > 50.0:
        nlos_excess_loss_db += 0.1 * (d - 50.0)  # Additional loss for longer NLOS paths
    base_path_loss_db = fspl_db + nlos_excess_loss_db
```

**Rationale:** 3GPP models use different path loss exponents for LOS vs NLOS. This adds a base NLOS excess loss when any building blocks the direct path. The distance-dependent component accounts for increasing loss in longer NLOS paths.

### 5.3 RX Combining Gain

**Fix 3: MIMO gain removed from RSRP**

```
G_RX = 0 dB
```

**Rationale:** RSRP is defined as per-antenna received power. MIMO diversity/combining affects throughput and reliability, not the received power itself. For accurate RSRP calculation, MIMO gain should be 0.

### 5.4 Metal Blocking

If ray encounters metal structure:
```
RSRP = -150 dBm  (effectively no signal)
```

**Note:** This only blocks the direct path. Real systems may still receive signal via diffraction/reflection, but these are not modeled yet.

---

## 6. Ray Termination Conditions

A ray stops propagating when any of these conditions are met:

### 6.1 Signal Too Weak
```
RSRP < P_noise + 10 dB
```

Where:
- `P_noise` = noise floor (dBm), calculated from bandwidth + noise figure
- Margin: 10 dB above noise floor

**Fix 5: Noise floor calculation**

```
P_noise = -174 + 10·log₁₀(B_Hz) + NF_dB
```

Where:
- `-174 dBm/Hz` = thermal noise floor
- `B_Hz` = channel bandwidth in Hz
- `NF_dB` = receiver noise figure (typical: 5-10 dB, default: 7 dB)

**Examples:**
- 20 MHz, NF=7 dB → `P_noise = -174 + 10·log₁₀(20×10⁶) + 7 = -174 + 73.01 + 7 = -93.99 dBm ≈ -94 dBm`
- 100 MHz, NF=7 dB → `P_noise = -174 + 10·log₁₀(100×10⁶) + 7 = -174 + 80.0 + 7 = -87.0 dBm`

**Implementation:**
```python
if noise_floor_dbm is not None:
    noise_floor_dbm = noise_floor_dbm  # Use explicit value
else:
    bandwidth_hz = channel_bandwidth_mhz * 1e6
    noise_floor_dbm = -174.0 + 10.0 * log10(bandwidth_hz) + noise_figure_db
```

**Rationale:** Noise floor depends on bandwidth and receiver characteristics, not a fixed value. This makes the model physically accurate and allows reasoning about different bandwidth configurations.

### 6.2 Metal Blocking
```
L_penetration(material, f) ≥ 100 dB
```
→ Ray terminates immediately

### 6.3 Excessive Material Loss

**Fix 4: Removed aggressive 50 dB hard stop**

```
cumulative_material_loss_db > 80.0 dB  (safety check only)
```
→ Ray terminates only if material loss is extremely high (safety check)

**Implementation:**
```python
# In coverage_grid.py
if cumulative_material_loss_db > 80.0:  # Very high material loss (safety check only)
    logger.debug(f"Ray terminated due to extremely high material loss ({cumulative_material_loss_db:.1f}dB)")
    break
```

**Rationale:** Material loss can exceed 50 dB in dense urban areas and still be decodable. The previous 50 dB cutoff was too aggressive and underestimated coverage. Now we rely primarily on the RSRP threshold for natural termination. The 80 dB limit is only a safety check to prevent rays from propagating through unrealistically dense obstacles.

### 6.4 Maximum Range
```
r > max_range_m  (default: 500 m)
```

---

## 7. Complete Attenuation Model (Summary)

For a ray at distance `r` from TX:

```
1. Compute distance:
   d = r  (straight-line distance only)

2. Determine LOS/NLOS:
   is_los = (num_buildings == 0)

3. Determine LOS/NLOS and compute base path loss:
   FSPL = 32.45 + 20·log₁₀(d/1000) + 20·log₁₀(f)
   if is_los:
     PL_base = FSPL
   else:
     L_NLOS = 20.0 + 0.1·max(0, d - 50.0)  (NLOS excess loss)
     PL_base = FSPL + L_NLOS

4. Compute cumulative material loss:
   L_material = Σ L_penetration(building_i, f) + L_trees

5. Check for metal blocking:
   if any L_penetration ≥ 100 dB:
     RSRP = -150 dBm
     terminate ray

6. Compute RSRP:
   RSRP = P_tx - PL_base - L_material + G_RX
   where G_RX = 0 dB (MIMO doesn't increase RSRP)

7. Calculate noise floor:
   P_noise = -174 + 10·log₁₀(B_Hz) + NF_dB

8. Check termination:
   if RSRP < P_noise + 10 dB:
     terminate ray
   if L_material > 80 dB:  (safety check only)
     terminate ray
   if r > max_range_m:
     terminate ray
```

---

## 8. Example Calculation

**Scenario:**
- TX power: 43 dBm
- Frequency: 3500 MHz (n78)
- Distance: 200 m
- Ray passes through 2 concrete buildings
- MIMO: SISO (no gain)

**Step 1: Base Path Loss (NLOS)**
```
FSPL = 32.45 + 20·log₁₀(0.2) + 20·log₁₀(3500)
     = 32.45 - 13.98 + 70.88
     = 89.35 dB

L_NLOS = 20.0 + 0.1 × max(0, 200 - 50)
       = 20.0 + 0.1 × 150
       = 20.0 + 15.0
       = 35.0 dB  (NLOS excess loss with distance component)

PL_base = 89.35 + 35.0 = 124.35 dB
```

**Step 2: Material Loss**
```
L_material = 2 × 25.0 dB = 50.0 dB  (2 concrete walls at 3500 MHz)
```

**Step 3: RSRP**
```
RSRP = 43 - 124.35 - 50.0 + 0
     = -131.35 dBm
```

**Step 4: Check Termination**
```
P_noise = -174 + 10·log₁₀(20×10⁶) + 7 = -94 dBm
RSRP (-131.35) < P_noise + 10 (-84)?
Yes → Ray terminates at 200m (well below threshold)
```

**Note:** The NLOS excess loss makes this path significantly worse than the old model, which is more realistic for NLOS conditions.

---

## 9. Key Properties

1. **Loss Persistence**: Material loss accumulates and persists even after exiting obstacles
2. **Frequency Dependency**: Higher frequencies (n78) have higher penetration loss than lower frequencies (n71)
3. **Metal Blocking**: Metal structures completely block signal (100+ dB loss) on direct path
4. **Adaptive Termination**: Rays stop when signal becomes too weak (RSRP threshold)
5. **Straight-Line Distance**: All path loss uses straight-line distance; NLOS effects via material loss only
6. **LOS/NLOS Models**: Different base path loss for LOS (FSPL) vs NLOS (FSPL + excess loss)
7. **Physical Noise Floor**: Calculated from bandwidth and noise figure, not fixed value

---

## 10. Recent Improvements (v2)

**Applied fixes based on RF engineering review:**

1. ✅ **Removed d_actual**: Use straight-line distance only; all NLOS via material loss
2. ✅ **Added LOS/NLOS base models**: Different path loss for LOS vs NLOS paths
3. ✅ **Fixed MIMO gain**: Set to 0 for RSRP (MIMO doesn't increase received power)
4. ✅ **Relaxed material cutoff**: Changed from 50 dB to 80 dB (safety check only)
5. ✅ **Physical noise floor**: Calculate from bandwidth + noise figure, not fixed value

**Result:** Model is now closer to "respectable RF planning approximation" instead of "FSPL plus random dB hacks."

## 11. Future Enhancements

- **3GPP Path Loss Models**: Replace FSPL with ITU-R/3GPP models (e.g., Urban Macro, Urban Micro)
- **Diffraction Loss**: Add loss for rays that diffract around building edges
- **Reflection Loss**: Account for ground/obstacle reflections
- **Interference Model**: Add interference from other cells/sectors
- **Antenna Patterns**: Add directional antenna gain patterns (sector-specific)

