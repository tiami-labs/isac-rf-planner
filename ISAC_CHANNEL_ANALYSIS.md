# Waveform-agnostic ISAC / bistatic channel analysis

## Run

Start the planner exactly as before:

```bash
uvicorn isac_rf_planner.api.rest:app --reload
```

No separate preprocessing command is required.

## UI workflow

1. Select any supported waveform: 5G NR, ATSC 1.0, ATSC 3.0, DVB-T, or baseline.
2. Configure the active transmitter and coverage radius.
3. Enable **Link budget and bistatic channel**.
4. Enter the analysis receiver as one `lat,lon` value, for example:

   ```text
   38.41717489626978, -121.39508190497462
   ```

5. Leave **Target→RX propagation** at **OSM + terrain reciprocal grid** to evaluate the return leg with the same local geometry and terrain data used by the forward planner.
6. Configure target height, bistatic RCS, target velocity, coherent integration, receiver gains/losses, noise figure, required SNR, Doppler thresholds, and cancellation.
7. Run the plan.

When channel analysis is enabled, the default display is **Expected ISAC quality at each target**. Every heatmap pixel is a candidate target location, not a receiver coverage sample only.

## Per-target path model

For every candidate target point `x`, the planner evaluates:

```text
active transmitter → x → analysis receiver
```

The output includes:

- active-waveform source EIRP toward `x`
- transmitter-to-target total path loss and environmental loss components
- incident power at `x`
- target-to-receiver free-space and OSM/terrain excess loss
- total bistatic path loss
- expected bistatic echo at the surveillance receiver input
- pre- and post-processing SNR
- detection margin
- direct-path residual and required dynamic range
- total/excess path length and excess delay
- bistatic angle
- path-range rate, closing speed, and signed Doppler
- Doppler resolved/ambiguous flags
- detectability and ISAC quality class

Clicking a heatmap location queries the nearest machine-grid point, draws both path legs, draws the iso-bistatic-range ellipse through that target, and displays the full link budget for that point.

## UI ZIP export

A successful **Export ZIP** includes all available outputs, not only the currently selected layer:

```text
full_view.png
full_view_without_rf.png                  # 3D export
metadata.json
export_manifest.json
plans/TX1/complete_settings.json
plans/TX1/complete_plan_response.json
heatmaps/TX1/*.png                        # every generated RF/ISAC layer
heatmaps/TX1/sectors/*.png                # NR only, when returned
channel/TX1_complete_rf_channel_grid.npz  # all per-target arrays
```

If channel analysis exists but its NPZ cannot be fetched, export stops with an error instead of creating an incomplete archive.

`complete_settings.json` contains the exact `rf_config_used`, channel-analysis configuration, and geometry-source metadata. The NPZ `metadata_json` member repeats the exact RF configuration, analysis summary, array units, dtype/shape manifest, and geometry source.

## Machine-readable target inspection

The UI uses this endpoint when a target point is clicked:

```text
GET /api/channel-analysis/products/{product_id}/nearest?lat={lat}&lon={lon}
```

It returns the nearest candidate target point, every one-dimensional per-target array value, units, transmitter and receiver locations, return-path model, and the quality label.
