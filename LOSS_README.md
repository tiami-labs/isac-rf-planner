## Actual loss equation now
Current RF solve is:

```107:117:src/agentic_rf_planner/rf/attenuation_models.py
extra_loss_db = max(
    0.0,
    penetration_loss_db + shadow_loss_db + diffraction_loss_db - canyon_recovery_db,
)
rsrp_uncapped = (
    rs_eirp_dbm
    - scenario_path_loss_db
    - extra_loss_db
    - horizontal_pattern_loss_db
    - vertical_pattern_loss_db
    + rx_combining_gain_db
)
```

So the attenuation terms are:

1. `scenario_path_loss_db`
2. `penetration_loss_db`
3. `shadow_loss_db`
4. `diffraction_loss_db`
5. `horizontal_pattern_loss_db`
6. `vertical_pattern_loss_db`

And one **credit** reduces loss:
- `canyon_recovery_db`

## Important caveat
If you say **“lower all attenuations except FSPL by 2 dB”**, then:

- `scenario_path_loss_db` is **not pure FSPL**
- in your current run it is `3gpp_38901 / umi_street_canyon`
- so it already includes NLOS scenario behavior inside the formula, not as a separate knob

That means the exact “except FSPL” split is:

### Outside-FSPL explicit attenuation knobs
These are the clean ones to change.

## A. Through-material attenuation
Applied while inside active building/forest intervals:

```420:459:src/agentic_rf_planner/geo/coverage_grid.py
penetration_loss_db = (
    sum(float(interval["penetration_loss_db"]) for interval in active_buildings)
    + len(active_forest) * wood_loss_db
)
# ...
extra_loss_db = max(
    0.0,
    penetration_loss_db + shadow_loss_db + diffraction_loss_db - canyon_recovery_db,
)
```

### Current config values actually used
These override the raw material table at `3.5 GHz`:

```55:70:configs/rf.params.yaml
building_attenuation:
  materials:
    concrete: 9.0
    brick: 7.5
    wood: 5.25
    glass: 6.75
    metal: 18.75
    unknown: 9.0
  overall:
    scale: 0.75
    reduction_db: 2.0
```

So the **direct penetration attenuation knobs** are:
- `building_attenuation.materials.concrete`
- `building_attenuation.materials.brick`
- `building_attenuation.materials.wood`
- `building_attenuation.materials.glass`
- `building_attenuation.materials.metal`
- `building_attenuation.materials.unknown`

Fallback-only knobs:
- `building_attenuation.overall.scale`
- `building_attenuation.overall.reduction_db`

## B. Shadowing
Applied only when LOS is lost and ray is not currently inside penetration:

```429:435:src/agentic_rf_planner/geo/coverage_grid.py
shadow_loss_db = min(
    float(getattr(rf_params, "shadow_loss_cap_db", 22.0) or 22.0),
    float(getattr(rf_params, "shadow_loss_db", 6.0) or 6.0)
    + float(getattr(rf_params, "shadow_decay_db_per_100m", 4.0) or 4.0)
    * (behind_first_blocker_m / 100.0),
)
```

So the shadow knobs are:
- `shadow_loss_db`
- `shadow_decay_db_per_100m`
- `shadow_loss_cap_db`

Current values:

```29:39:configs/rf.params.yaml
path_loss_model: "3gpp_38901"
propagation_scenario: "umi_street_canyon"
shadow_loss_db: 6.0
shadow_decay_db_per_100m: 4.0
shadow_loss_cap_db: 22.0
diffraction_base_loss_db: 6.0
diffraction_slope_db_per_100m: 3.0
diffraction_loss_cap_db: 18.0
canyon_recovery_max_db: 8.0
canyon_recovery_slope_db_per_100m: 6.0
termination_rsrp_dbm: -140.0
```

## C. Diffraction loss
Also only after LOS loss / outside active penetration:

```442:449:src/agentic_rf_planner/geo/coverage_grid.py
diffraction_loss_db = min(
    float(getattr(rf_params, "diffraction_loss_cap_db", 18.0) or 18.0),
    float(getattr(rf_params, "diffraction_base_loss_db", 6.0) or 6.0)
    + float(getattr(rf_params, "diffraction_slope_db_per_100m", 3.0) or 3.0)
    * (behind_first_blocker_m / 100.0),
)
```

Diffraction knobs:
- `diffraction_base_loss_db`
- `diffraction_slope_db_per_100m`
- `diffraction_loss_cap_db`

## D. Post-blocker continuation credit
This is **not attenuation**, it reduces attenuation:

```451:454:src/agentic_rf_planner/geo/coverage_grid.py
canyon_recovery_db = min(
    float(getattr(rf_params, "canyon_recovery_max_db", 8.0) or 8.0),
    float(getattr(rf_params, "canyon_recovery_slope_db_per_100m", 6.0) or 6.0)
    * (open_gap_after_exit_m / 100.0),
)
```

Credit knobs:
- `canyon_recovery_max_db`
- `canyon_recovery_slope_db_per_100m`

If your instruction is “lower all attenuations by 2 dB”, this one is the opposite sign:
- to make propagation **less harsh**, you would usually **increase** this credit, not lower it.

## E. Antenna-pattern attenuation
These are also explicit non-FSPL losses.

### Horizontal
Controlled by:
- `max_horizontal_attenuation_db`
- `front_to_back_attenuation_db`

Current config:
```20:23:configs/rf.params.yaml
max_vertical_attenuation_db: 30.0
max_horizontal_attenuation_db: 30.0
front_to_back_attenuation_db: 25.0
```

### Vertical
Controlled by:
- `max_vertical_attenuation_db`

Also shape-controlled by:
- `vertical_beamwidth_deg`
- `electrical_tilt_deg`
- `mechanical_tilt_deg`

Important:
- `vertical_beamwidth_deg`, `electrical_tilt_deg`, `mechanical_tilt_deg` are **not direct attenuation offsets**
- they change *where* attenuation lands, not “subtract 2 dB everywhere”

## Not attenuation knobs
These should **not** be in your “lower all attenuation by 2 dB” bucket:

- `tx_power_dbm`
- `tx_antenna_gain_dbi`
- `tx_feeder_loss_db`
- `reference_signal_offset_db`
- `ue_antenna_gain_dbi`
- `max_rsrp_dbm`
- `termination_rsrp_dbm`

And also:
- `path_loss_model`
- `propagation_scenario`

because those select the scenario formula, they are not simple `-2 dB` knobs.

## Raw material table
This exists too:

```10:55:src/agentic_rf_planner/rf/material_penetration.py
MATERIAL_PENETRATION_DB = {
    "wood": { ... 3500.0: 7.0 },
    "concrete": { ... 3500.0: 12.0 },
    "brick": { ... 3500.0: 10.0 },
    "metal": { ... 3500.0: 25.0 },
    "glass": { ... 3500.0: 9.0 },
    "unknown": { ... 3500.0: 12.0 },
}
```

But for your current planner run, the YAML `building_attenuation.materials.*` values are the primary ones actually used.

## Short version: exact list to touch if you mean “all explicit non-FSPL losses”
Direct loss knobs:
- `building_attenuation.materials.concrete`
- `building_attenuation.materials.brick`
- `building_attenuation.materials.wood`
- `building_attenuation.materials.glass`
- `building_attenuation.materials.metal`
- `building_attenuation.materials.unknown`
- `shadow_loss_db`
- `shadow_loss_cap_db`
- `diffraction_base_loss_db`
- `diffraction_loss_cap_db`
- `max_vertical_attenuation_db`
- `max_horizontal_attenuation_db`
- `front_to_back_attenuation_db`

Slope / shape knobs, not plain `-2 dB`:
- `shadow_decay_db_per_100m`
- `diffraction_slope_db_per_100m`
- `vertical_beamwidth_deg`
- `electrical_tilt_deg`
- `mechanical_tilt_deg`

Opposite-sign continuation credit:
- `canyon_recovery_max_db`
- `canyon_recovery_slope_db_per_100m`
