# Ray-Tracing Mode Implementation Status

This repo drop adds a new 3D ray-tracing planning mode and wires it into the existing RF planner.

## Implemented

1. **3D ray-tracing engine path**
   - Added multi-bounce path search in `src/agentic_rf_planner/rf/ray_tracing.py`.
   - Supports up to **20 bounces**.
   - Expansion stops when candidate path **RSRP < -140 dBm** (or the request threshold).
   - Reuses the existing RF attenuation / antenna / path-loss parameters from the planner.

2. **Ray-tracing render**
   - Added 3D RT overlay rendering in `src/agentic_rf_planner/ui/static/planner_3d.js`.
   - Returned paths are drawn in the 3D Cesium/Google-mesh view.

3. **GUI integration**
   - In 3D RT mode, the user selects:
     - TX normally
     - RX with **SHIFT+click**
   - Pressing **Plan RF / RT Queue** now launches the RT path instead of the coverage planner.

4. **Same attenuation models / map providers**
   - `/api/raytrace_paths` in `src/agentic_rf_planner/api/rest.py` uses the same RF parameters already exposed by the planner.
   - OSM is still used for building extraction/material semantics.
   - Google mesh profiles are loaded when available in 3D RT mode.

5. **Google mesh + OSM together**
   - OSM is used for candidate reflecting walls and semantics.
   - Fine-resolution Google mesh validation is applied in the 3D UI against the actual loaded mesh before drawing paths.

## Important caveat

The backend candidate generator is **not** a full arbitrary-leg mesh collider like Omniverse/OptiX.

Current behavior is:
- backend candidate generation = OSM wall search + hybrid clearance using persisted TX-anchored mesh profiles where applicable
- final rendered path validation = actual Google mesh checks in the 3D UI (`clampToHeightMostDetailed`) on every returned segment

So this drop **does** use Google mesh + OSM together and prevents rendering paths that still cut through the actual Google mesh, but the backend solver itself is not doing full arbitrary-leg Google-mesh intersection for every bounce leg.

## Files changed

- `src/agentic_rf_planner/rf/ray_tracing.py`
- `src/agentic_rf_planner/api/rest.py`
- `src/agentic_rf_planner/pipeline/schemas.py`
- `src/agentic_rf_planner/ui/static/planner_3d.js`
- `src/agentic_rf_planner/ui/static/index_3d.html`
- `tests/test_ray_tracing_rt_mode.py`

## Validation run

Executed successfully:

```bash
python -m py_compile src/agentic_rf_planner/rf/ray_tracing.py src/agentic_rf_planner/api/rest.py src/agentic_rf_planner/pipeline/schemas.py
PYTHONPATH=src pytest -q tests/test_ray_tracing_rt_mode.py tests/test_rf_vendor_grade.py
```

Result:

- `5 passed`
