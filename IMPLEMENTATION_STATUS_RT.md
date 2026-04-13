# Google-Mesh RT Rework Status

This drop keeps `rfp_repo_old` as the rendering / UX baseline and changes the RT solver path so it no longer depends on OSM walls alone.

## What changed

- `src/agentic_rf_planner/rf/ray_tracing.py`
  - added `compute_multi_bounce_paths(...)`
  - added `MeshProfileIndex`
  - added `extract_mesh_profile_wall_segments(...)`
  - OSM walls are still supported, but Google-mesh radial profiles now generate additional reflector segments
  - returned wall segments are tagged with `source="mesh"` or `source="osm"`

- `src/agentic_rf_planner/api/rest.py`
  - `/api/raytrace_paths` now:
    - loads the persisted Google-mesh profile set for the TX/config
    - converts those profiles into reflector segments
    - combines `mesh_walls + osm_walls`
    - validates every leg against:
      - OSM footprint hits
      - Google-mesh profile occupancy sampling
    - runs RT search up to **20 bounces**
    - terminates on **RSRP < -140 dBm**
  - response now includes `message` and `diagnostics`

- `src/agentic_rf_planner/ui/static/planner_3d.js`
  - `/api/raytrace_paths` requests now use:
    - `max_bounces: 20`
    - `max_wall_candidates: 120`
    - `max_paths: 24`
  - viewer console prints RT diagnostics
  - sidebar status line shows the API message

- `src/agentic_rf_planner/ui/static/index_3d.html`
  - RT help text updated to describe Google-mesh + OSM RT mode

## Terminal / API log meanings

The backend now logs one summary line for each `/api/raytrace_paths` call:

```text
raytrace_paths tx=(...) rx=(...) 3D RT: mesh_loaded=... osm_walls=... mesh_walls=... combined=... raw_paths=... returned=... los=...
```

Meaning:

- `mesh_loaded`
  - whether the persisted Google-mesh radial profile set was found and loaded for this TX/config
- `osm_walls`
  - number of reflector wall segments derived from OSM building polygons
- `mesh_walls`
  - number of reflector wall segments derived from the Google-mesh profile contours
- `combined`
  - total reflector segments passed into the RT solver
- `raw_paths`
  - number of reflected paths produced by the RT solver before the final return trimming
- `returned`
  - final number of paths returned in the API payload (includes direct path)
- `los`
  - whether the direct TX→RX chord is clear under the hybrid OSM + mesh-profile occupancy check

Important:

- `los=true` does **not** suppress reflected search
- `los=false` does **not** imply zero reflected paths
- reflected search always runs in `3d_rt`

## Validation run

```bash
python -m py_compile src/agentic_rf_planner/rf/ray_tracing.py src/agentic_rf_planner/api/rest.py
PYTHONPATH=src pytest -q tests/test_ray_tracing_rt_mode.py tests/test_rf_vendor_grade.py
```

Result: `6 passed`
