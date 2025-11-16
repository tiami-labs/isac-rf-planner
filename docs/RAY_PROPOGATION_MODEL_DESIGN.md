# Ray Propagation Model Design (Physics- and 3GPP-Informed)

This note summarizes how to go from a **TX point with known obstacles** to a **realistic RF attenuation / coverage model**, in a way that is:

- Physically grounded (FSPL, ground reflection, obstacles, diffraction).
- Aligned in spirit with **3GPP TR 38.901**-style large-scale modeling.
- Implementable on your existing `WorldModel` / `WorldCell` grid structure.

---

## 1. Baseline: No Obstacles → FSPL + Ground Reflection

If there were truly no obstacles, the received power at distance \(d\) is governed by **free‑space path loss (FSPL)**:

\[
\text{FSPL}(\text{dB}) = 32.45 + 20\log_{10}(f_{\text{MHz}}) + 20\log_{10}(d_{\text{km}})
\]

Then

\[
P_{\text{rx, dBm}} = P_{\text{tx, dBm}} + G_{\text{tx}} + G_{\text{rx}} - \text{FSPL}
\]

This is the cleanest baseline and matches what you already implement.

### Two-Ray Ground Reflection Model

Even with no buildings, **ground reflection** matters (especially sub‑6 GHz). You effectively have two paths:

- Direct path: length \(d_1\)
- Ground-reflected path: length \(d_2\)
- Reflection coefficient \(\Gamma(\theta_i)\) depending on incidence angle, polarization, and ground permittivity.

Complex baseband field at RX:

\[
E_{\text{rx}} \propto \frac{1}{d_1} e^{-jk d_1}
     + \Gamma(\theta_i) \frac{1}{d_2} e^{-jk d_2}
\]

where \(k = 2\pi / \lambda\). The **received power** is \(|E_{\text{rx}}|^2\).

Effects:

- Constructive / destructive interference vs distance (power **ripples**).
- At larger distances, effective decay can approach \(1/d^4\) instead of \(1/d^2\).

For **planning heatmaps**, people rarely show per‑meter ripple. Instead they use:

- FSPL (LOS) in the near field.
- Transition to an **effective steeper slope** (two‑ray asymptotic) beyond a breakpoint distance.

You *can* compute two‑ray per cell (cheap), but it’s often enough to use a smoothed or piecewise model rather than exposing raw ripples.

---

## 2. Add Obstacles: Practical Ray / Geometric Model

Once you have a good obstacle map, you basically want a **geometric optics (GO)** model with a few key paths:

- LOS (if it exists).
- Ground reflection.
- Single-bounce reflections (building facades).
- Single-edge diffractions (around corners / over rooftops).
- Penetration through walls / foliage.

In full EM / ray‑tracing, you might track many paths and solve GO + UTD (uniform theory of diffraction). That is **overkill** for a fast planner.

### 2.1 Rays That Actually Matter for a Planner

For each RX cell, relevant contributions are:

1. **LOS path** (if unblocked).
2. **Ground-reflected path**.
3. One **dominant reflection/diffraction** if needed.
4. **Penetrated path** (indoor or behind building).

Higher-order bounces exist but, for coverage maps, they’re usually absorbed into:

- NLOS path‑loss models, and
- Log‑normal shadowing.

So for a **coverage heatmap**, you don’t need to keep and sum 10–20 explicit paths per pixel; you need a **good effective path loss**.

### 2.2 Anatomy of One Path

For each hypothetical path \(p\):

\[
P_{\text{rx}, p} = P_{\text{tx}}
                    + G_{\text{tx}}(\theta_p)
                    + G_{\text{rx}}(\theta'_p)
                    - L_{\text{fs}}(d_p)
                    - L_{\text{mat}, p}
                    - L_{\text{diff}, p}
                    - L_{\text{scatt}, p}
\]

If you are simulating **waveforms / channels**, you’d treat each path as a complex tap:

\[
h(t) = \sum_p a_p e^{j\phi_p} \delta(t - \tau_p)
\]

with \(a_p\) set by the dB path loss and \(\phi_p\) by path length + reflections. That’s essentially what **3GPP 38.901** channel models approximate, but **statistically**, not via full geometry.

For **planning**, you usually collapse all that into:

- A **large-scale mean path loss** (dB),
- Optional **shadowing margin** (log‑normal),
- Optional safety margin for fast fading.

---

## 3. 3GPP Perspective (38.901 / 5G NR Style)

3GPP does **not** require you to full‑ray‑trace a city. TR 38.901 defines:

- Scenario-specific **LOS/NLOS path loss formulas** (UMa, UMi, RMa, indoor, etc.).
- Tables of **penetration losses** (external wall, internal wall, glass, etc.).
- **Clustered delay line** (CDL) models for small‑scale fading.

Typical workflow in 3GPP land:

1. Choose scenario: Urban Macro, Urban Micro (street canyon), Rural Macro, Indoor, etc.
2. Determine LOS vs NLOS (either probabilistically or via geometry if you have it).
3. Use the scenario’s LOS or NLOS path-loss formula, e.g.

   \[
   PL_{\text{LOS}}(dB) = A + B\log_{10}(d) + C\log_{10}(f_c) + X_\sigma
   \]

4. Add penetration and additional losses if RX is indoors or behind obstacles.
5. For channel simulation, overlay a CDL model for small-scale fading.

**Key point for you:**  
Your “very good assessment of obstacles” lets you replace the **probabilistic LOS/NLOS decision** with a **deterministic, geometry-based** one and then adjust the 3GPP formulas with obstacle-specific losses.

---

## 4. Concrete Per-Cell Model for Your System

You already have a grid with `WorldCell`s (distance, bearing, material, obstacles). Use that to drive a 3GPP‑flavored model.

Assume each `WorldCell` knows:

- `distance_m`
- `is_los` (bool)
- `num_buildings`
- `num_trees`
- Maybe `dominant_material` (building/trees/etc.)

### Step 1 – LOS Test

Geometric LOS test from TX to cell center:

- Cast a ray from TX to the cell.
- If it intersects any building polygon up to the relevant height → `is_los = False`.
- Else `is_los = True`.

You can do this inside `world_builder` or a dedicated geometry module.

### Step 2 – Base Path-Loss Law

Per scenario (urban macro, micro, etc.), define:

- \(PL_{\text{LOS}}(d)\): either FSPL or the 3GPP LOS formula.
- \(PL_{\text{NLOS}}(d)\): 3GPP NLOS formula, or FSPL plus an extra NLOS term.

Then for each cell:

\[
PL_{\text{base}}(d) =
\begin{cases}
PL_{\text{LOS}}(d), & \text{if LOS}\\
PL_{\text{NLOS}}(d), & \text{if NLOS}
\end{cases}
\]

You can start simple:

- `PL_LOS = FSPL`.
- `PL_NLOS = FSPL + NLOS_extra(d)` (a function or just a constant offset you tune).

### Step 3 – Material / Obstacle Losses

Now incorporate your obstacle info. For each `WorldCell`:

- Let \(N_{\text{bldg}}\) = number of building “faces” or walls intersected.
- Let \(N_{\text{trees}}\) = tree/foliage segments intersected.

Define per-material losses, e.g.

- Building wall: 15–30 dB per effective wall (depends on concrete vs glass).
- Tree belt: maybe 0.2–0.5 dB/m through foliage (or a fixed 5–10 dB if you don’t have exact depth).

Then:

\[
L_{\text{mat}} = N_{\text{bldg}} \cdot L_{\text{wall}}
                + N_{\text{trees}} \cdot L_{\text{foliage}}
\]

If you detect a path is purely diffracted (e.g. no LOS, but only a single edge/rooftop in the geometry), you can add a **diffraction term** \(L_{\text{diff}}\). If you don’t explicitly compute it, a pragmatic approach is a fixed 10–20 dB penalty for NLOS “around the corner” cells.

So:

\[
PL_{\text{cell}} = PL_{\text{base}}(d) + L_{\text{mat}} + L_{\text{diff}} + L_{\text{clutter}}
\]

where \(L_{\text{clutter}}\) can capture environment class (dense urban vs suburban, etc.).

Then the received power:

\[
P_{\text{rx, cell}} = P_{\text{tx}} + G_{\text{tx}} + G_{\text{rx}} - PL_{\text{cell}}
\]

### Step 4 – Optional Two-Ray Adjustment (LOS + Close Range)

If you want extra realism for near‑TX behaviour:

- For **LOS cells within some radius** (say \(< 200 \text{ m}\)), apply a **two‑ray correction**:

  1. TX height \(h_t\), RX height \(h_r\).
  2. Compute direct \(d_1\) and ground-reflected \(d_2\).
  3. Use an approximate reflection coefficient \(\Gamma\) (e.g. \(-0.5\) for typical ground and vertical polarization).
  4. Compute effective **field** from direct + reflected and convert to an effective additional loss \(\Delta L_{2\text{ray}}\).

- To avoid ugly ripples on the map, smooth \(\Delta L_{2\text{ray}}\) over distance (e.g. moving average over a few meters) or just use a pre-computed two‑ray curve.

If this feels like too much complexity, you can skip it initially and stick to LOS path loss only.

### Step 5 – Small-Scale Fading (Waveform / 3GPP Alignment)

You can think of the computed \(P_{\text{rx, cell}}\) as **large-scale mean power**. For waveform/channel simulation:

- If `is_los`:
  - Use a **Rician** fading model with a K-factor consistent with LOS macros.
- If NLOS:
  - Use **Rayleigh** fading.

For **coverage heatmaps**, you normally **don’t plot the random fluctuations**; you either:

- Ignore small-scale fading, or
- Subtract a fixed **fading margin** (e.g. 3–6 dB) to be conservative.

This keeps the map stable while still being 3GPP‑aware.

---

## 5. How to Plug This Into Your Current Code

Your existing concepts (`WorldModel`, `WorldCell`) can be extended slightly.

### Extend `WorldCell`

Add fields:

- `is_los: bool`
- `num_buildings: int`
- `num_trees: int`
- Optionally, `diffraction_flag: bool`

You may also track `dominant_material`, but counts are more actionable.

### `world_builder` Responsibilities

For each cell:

1. Run LOS test using building polygons → set `is_los`.
2. Count intersections with building and tree/foliage polygons along the ray:
   - `num_buildings`.
   - `num_trees`.
3. Optionally mark `diffraction_flag` if the ray just grazes a building edge / rooftop instead of going through.

### `attenuation_models` Responsibilities

Implement something like:

```python
def path_loss_los(d, freq_mhz):
    # FSPL or 3GPP LOS formula
    ...

def path_loss_nlos(d, freq_mhz):
    # 3GPP NLOS formula or FSPL + extra
    ...

def compute_cell_power(cell, rf_params, mat_db):
    d = max(cell.distance_m, 1.0)
    if cell.is_los:
        PL_base = path_loss_los(d, rf_params.freq_mhz)
    else:
        PL_base = path_loss_nlos(d, rf_params.freq_mhz)

    L_mat = mat_db["building"] * cell.num_buildings + mat_db["trees"] * cell.num_trees

    L_diff = 0.0
    if getattr(cell, "diffraction_flag", False):
        L_diff = 10.0  # for example

    PL = PL_base + L_mat + L_diff

    Prx = rf_params.tx_power_dbm - PL
    return Prx
```

You can later add an optional two‑ray correction for LOS cells near the TX.

---

## 6. Summary

- **No obstacles:** FSPL + optional two‑ray for ground reflection.
- **With obstacles:** Use geometry to decide LOS/NLOS and to count penetrations and foliage.
- **3GPP alignment:** Use LOS/NLOS path-loss laws from 38.901 as your base, instead of inventing everything from scratch.
- **Planning vs channel simulation:**
  - Planning: work with **large-scale path loss** and simple margins.
  - Channel sim: turn your geometric insights into parameters for a 3GPP‑style fading model (CDL, Rician/Rayleigh).

This gives you a **realistic, waveform- and 3GPP-informed propagation model** that is tight enough for serious use, but still feasible to implement on top of your existing grid/world representation.
