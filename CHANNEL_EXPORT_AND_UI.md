# Channel-analysis export and UI contract

## Export scope

Each completed channel-analysis run writes one compressed schema-3 `.npz` bundle. The bundle represents one validated solver run; it is not a parameter sweep. Changing a setting and running again creates a separate bundle.

The bundle contains:

- `settings_json`: technology-active validated settings used by the run.
- `solver_state_json`: complete normalized `RFParams`, including defaults and derived compatibility state.
- `execution_context_json`: effective TX, ray mode, geometry source, output mode and point count.
- `summary_json`: direct path, processing/noise terms, resolution, counts and selected targets.
- `array_manifest_json`: dtype, shape, unit and description for every exported array.
- `schema_json`: schema/version and coordinate-system information.
- `metadata_json`: combined manifest and export provenance.
- every active per-target waveform, terrain/environment, communication, bistatic sensing and joint ISAC output in index-aligned arrays.

The bundle does not duplicate PNG previews or raw OSM polygons. The numeric arrays are sufficient to regenerate heatmaps; geometry source/provenance is exported in the execution context.

## UI contract

The primary output is a per-target heatmap, not a bistatic ellipse.

After an analysis run, the 2D planner defaults to **Joint ISAC quality** unless another analysis layer is already selected. Available layers include environment loss, incident carrier, communication margin, expected echo, sensing margin, Doppler, sensing quality and joint ISAC quality/usability.

The analysis overlay contains only useful geometric context:

- analysis receiver;
- direct TX-to-RX baseline;
- selected TX-to-target and target-to-RX legs;
- selected target motion vector.

No constant-delay ellipse is drawn.

Use **Inspect target on map** and click any heatmap location. The UI calls the product point endpoint and displays all exported values for the nearest computed target cell, including the environmental illumination loss components, communication margin, expected return, Doppler, receiver feasibility and joint ISAC result.
