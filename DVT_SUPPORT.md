# DVT planning support

The planner accepts terrestrial digital television transmitters through `POST /api/plan` using `technology: "dvt"` and one of these waveform presets:

- `atsc1`
- `atsc3`
- `dvbt`
- `baseline`

## Broadcast transmitter versus cellular sectors

A DVT plan contains **one station transmitter and one broadcast antenna radiation system**. It does not create cellular sectors, serving-sector selection, sector overlays, per-sector heatmaps, or same-site sector interference.

The DVT transmitter object is split into independent physical components:

```json
{
  "dvt": {
    "fc": 587000000,
    "fs": 10000000,
    "bandwidth": 6000000,
    "waveform": "atsc3",
    "tx": {
      "latitude": 37.7749,
      "longitude": -122.4194,
      "altitude": 18.0,
      "antennaHeight": 320.0,
      "name": "Example TV TX"
    },
    "antenna": {
      "patternType": "tabulated",
      "manufacturer": "Example manufacturer",
      "model": "Example antenna",
      "rotationDeg": 345.0,
      "beamTiltDeg": 1.5,
      "verticalBeamwidthDeg": 8.0,
      "maxVerticalAttenuationDb": 30.0,
      "azimuthPattern": [
        {"azimuthDeg": 0.0, "relativeFieldH": 1.0, "relativeFieldV": 0.5},
        {"azimuthDeg": 90.0, "relativeFieldH": 0.8, "relativeFieldV": 0.4},
        {"azimuthDeg": 180.0, "relativeFieldH": 0.1, "relativeFieldV": 0.1},
        {"azimuthDeg": 270.0, "relativeFieldH": 0.2, "relativeFieldV": 0.2}
      ]
    },
    "power": {
      "erpHKw": 1000.0,
      "erpVKw": 250.0,
      "polarization": "DA (E)"
    }
  }
}
```

`tx` contains site geometry only. Directionality belongs in `antenna`.

## Broadcast antenna pattern types

### `omnidirectional`

No horizontal pattern attenuation is applied. Vertical beam tilt and vertical HPBW remain active.

### `parametric`

A broadcast-pattern approximation using:

- `mainAzimuthDeg`
- `horizontalBeamwidthDeg`
- `maxHorizontalAttenuationDb`
- `frontToBackAttenuationDb`
- `rotationDeg`

These are properties of one broadcast radiation pattern, not cellular sector definitions.

### `tabulated`

A circular filed/manufacturer pattern made of azimuth samples. Each sample may contain:

- `relativeField` or `attenuationDb` for one common pattern
- `relativeFieldH` / `attenuationDbH`
- `relativeFieldV` / `attenuationDbV`

Relative-field values are linear electric-field ratios. The solver converts them to ERP attenuation with `20 log10(field)`, interpolates circularly between azimuth samples, and applies `rotationDeg` clockwise.

## Power references

Three mutually exclusive input forms are supported:

1. `erpKw`: one maximum ERP value.
2. `erpHKw` and/or `erpVKw`: maximum horizontal and vertical ERP components for elliptical polarization.
3. `conductedPowerKw`: transmitter output before the antenna system, combined with `txGainDb`, `feederLossDb`, and `antennaGainDbi`.

ERP already includes antenna-system gain and feeder effects, so ERP cannot be combined with explicit feeder or antenna gain. Conducted power applies those terms once.

For separate H/V ERP components, the solver applies the H and V azimuth patterns independently and then sums their received powers in the linear domain.

## Physical terms

The DVT calculation uses:

- center frequency and wavelength-dependent path loss
- channel bandwidth and receiver noise figure
- ERP or conducted transmitter power
- transmitter-chain gain
- feeder loss
- antenna gain
- broadcast horizontal radiation pattern
- pattern rotation
- vertical beam tilt and vertical HPBW
- receiver antenna gain
- terrain, buildings, vegetation, diffraction, and shadow losses

It does not use NR resource blocks, subcarrier spacing, reference-signal spreading, MIMO, link adaptation, RSRP caps, 3GPP UMi/UMa scenarios, or sectors.

## UI behavior

Selecting ATSC 1.0, ATSC 3.0, DVB-T, or baseline DVT:

- hides the complete NR advanced panel
- hides NR sector configuration and sector overlays
- shows the broadcast antenna radiation-pattern editor
- supports omnidirectional, parametric, and tabulated patterns
- supports separate maximum H/V ERP values
- sends no `sectors` field
- defaults the display layer to received carrier power in dBm

## Large-area execution

DVT defaults to 20 km range and 20 m radial spacing when those fields are omitted. Large OSM requests are tiled and cached. The solver reuses precomputed obstacle intervals, performs interval-event sweeps, and returns compact raster output for large plans without coarsening the configured propagation sampling.

Examples:

- `examples/dvt_plan_request.json`
- `examples/dvt_conducted_power_plan_request.json`
- `examples/dvt_tabulated_broadcast_pattern_request.json`

## Automatic OSM AOI cache for large-area DVT planning

The execution surface is unchanged:

```bash
uvicorn agentic_rf_planner.api.rest:app --reload
```

Then select a DVT waveform, select the transmitter, set the radius, and run.
No PBF download or preparation command is required.

For each DVT request the planner:

1. Computes the requested AOI around the configured broadcast transmitter.
2. Opens or creates `data/osm/rf_geometry.sqlite`.
3. Checks `aoi_cache` for a completed region that contains the requested AOI.
4. On a cache hit, loads buildings and land use using the SQLite R*Tree only.
5. On a cache miss, first attempts one complete Overpass AOI request.
6. If the complete request is rejected or too large, falls back to bounded tiles.
7. Commits every successful tile to SQLite immediately and marks the AOI complete
   only when all required tiles succeed.
8. Continues the same RF planning request using the newly cached geometry.

A later request inside the cached AOI makes zero OSM network requests. Successful
fallback tiles are resumable after a failed or interrupted first run.

The cache path can be overridden with `DVT_OSM_DATABASE` or `RF_OSM_DATABASE`;
otherwise it is created automatically at `data/osm/rf_geometry.sqlite` relative
to the directory where Uvicorn is started.

`agentic-rf-prepare-region` remains an optional offline pre-seeding tool. It is
not part of the normal click-and-run workflow.

Broadcast-site coordinates are used directly. DVT planning skips street
snapping, Street View discovery, panorama retrieval, and VLM refinement.

## Shared link budget and bistatic channel analysis

Link budget, receiver geometry, target-return power, delay, Doppler, and
detectability are implemented by the shared waveform-agnostic channel layer.
They are not owned by the DVT transmitter model. The same UI and API contract
operate with 5G NR, ATSC 1.0, ATSC 3.0, DVB-T, and baseline DVT.

See `CHANNEL_ANALYSIS.md` and the concrete requests in:

- `examples/channel_analysis_5g_nr.json`
- `examples/channel_analysis_atsc1.json`
