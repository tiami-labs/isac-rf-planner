# DVT planning support

The planner accepts DVT transmitter data through the existing `POST /api/plan` endpoint.
DVT is a separate RF technology path; existing 5G NR requests retain their current defaults and link-budget behavior.

## Example request

```json
{
  "technology": "dvt",
  "ray_mode": "2d_osm",
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
      "name": "Example DVT TX",
      "azimuthDeg": 90.0,
      "elevationDeg": -1.0,
      "beamwidthHDeg": 65.0,
      "beamwidthVDeg": 8.0,
      "electricalTiltDeg": 0.0,
      "mechanicalTiltDeg": 0.0,
      "maxHorizontalAttenuationDb": 30.0,
      "frontToBackAttenuationDb": 30.0,
      "maxVerticalAttenuationDb": 30.0
    },
    "power": {
      "erpKw": 50.0,
      "polarization": "DA (E)"
    },
    "station": {
      "callSign": "KEXAMPLE",
      "virtualChannel": "7.1",
      "rfChannel": 33,
      "physicalChannel": 33,
      "facilityId": "12345",
      "city": "San Francisco",
      "network": "Example Network",
      "standard": "ATSC 3.0",
      "band": "UHF",
      "source": "internal",
      "sourceUrl": "https://example.invalid/station/12345"
    }
  },
  "max_range_m": 20000,
  "step_m": 20,
  "coverage_display_layer": "field_strength",
  "compact_output": true
}
```

`lat` and `lon` may be omitted for DVT requests because they are taken from `dvt.tx`. When both forms are supplied, they must match.

## Link-budget semantics

- Supply exactly one source-power form:
  - `erpKw`: effective radiated power; antenna-system gain and feeder effects are already included, and ERP is converted to EIRP by adding 2.15 dB once.
  - `conductedPowerKw`: transmitter output before the antenna system; the source EIRP is `conducted power + txGainDb + antennaGainDbi - feederLossDb`.
- TX-chain gain, feeder loss, antenna gain, receiver gain, frequency/wavelength, bandwidth/noise, and directional antenna patterns are physical terms shared across waveform families.
- DVT omits only NR-specific occupied-resource-element spreading and NR reference-signal offsets.
- Transmitter absolute height is `tx.altitude + tx.antennaHeight`.
- Directional attenuation uses the supplied azimuth, horizontal/vertical beamwidth, tilt, and attenuation limits.
- `polarization` and all `station` fields are metadata only.
- The returned grid includes `received_power_dbm` and `field_strength_dbuv_m`.
- `rsrp_dbm` remains populated with received carrier power for compatibility with existing rendering and result consumers.

## Large-area OSM behavior

Requests above 7.5 km are split into exact cached OSM tiles rather than one large Overpass bounding box. The implementation deduplicates features across tile boundaries and parses outer rings from OSM multipolygon relations. Cached regions are reused only when they fully contain the new request.

No coarse solver, spatial interpolation, or distant-building simplification is introduced by this change.

## Large-result serialization

For DVT plans, `compact_output` controls whether raw per-cell arrays are returned:

- `true`: return the full-resolution heatmap PNG, configuration, summaries, and `num_points`, but omit the large JSON cell arrays.
- `false`: return every raw cell array.
- omitted: automatically compact DVT results above 100,000 points.

The DVT heatmap texture supports up to 2048 pixels. A 40 km diameter plan at 20 m spacing therefore renders at approximately the requested 20 m display resolution.

The solver uses an exact interval-event sweep and reuses the coverage grid's precomputed OSM intersection state; it does not query buildings and forests again for every output cell. The single-transmitter DVT attenuation path also bypasses the 5G serving-sector/interference grouping allocation.

## Current propagation boundary

This change implements the supplied DVT transmitter model with either direct ERP or conducted-power RF-chain input, using frequency-dependent path loss plus the planner's existing terrain, building, vegetation, diffraction, shadow, receiver-gain, bandwidth/noise, and antenna-pattern terms. The waveform preset is modeled and returned, but it does not yet select waveform-specific receiver thresholds or regulatory service-contour criteria. Polarization and station identity remain metadata, as specified.


## Conducted-power example

See `examples/dvt_conducted_power_plan_request.json` for a request where feeder loss and antenna gain are calculated explicitly. The original `examples/dvt_plan_request.json` remains the direct-ERP form.


## UI waveform selection

Both planner surfaces expose a **Waveform and transmitter** section:

- `5G NR`
- `DVT — ATSC 1.0`
- `DVT — ATSC 3.0`
- `DVT — DVB-T`
- `DVT — baseline`

For DVT selections, the UI sends both an explicit top-level waveform and the typed nested transmitter waveform:

```json
{
  "technology": "dvt",
  "waveform": "atsc3",
  "dvt": {
    "waveform": "atsc3"
  }
}
```

The API rejects a mismatch between `waveform` and `dvt.waveform`. The DVT panel exposes `fc` through the shared frequency control, `bandwidth` through the shared bandwidth control, `fs`, site altitude, antenna height, azimuth/elevation, horizontal and vertical beamwidth, ERP or conducted-power RF-chain inputs, polarization, range/dR, and station identity metadata. The 3D UI keeps DVT on `3d_osm`; Google-mesh RT modes are disabled for large-area DVT plans.

## Waveform-isolated UI

Selecting ATSC 1.0, ATSC 3.0, DVB-T, or the baseline DVT profile switches both planner surfaces to a DVT-only transmitter form. NR-only controls (SCS, resource blocks, RSRP termination, MIMO, link adaptation, 3GPP scenario selection, and sector configuration) are hidden and removed from the submitted request. DVT frequency, channel bandwidth, receiver noise figure/gain, termination power, antenna pattern, ERP/conducted-power chain, and station identity use dedicated controls.

The coverage selector disables RSRP for DVT and defaults to field strength. The legend title and units follow the active layer; DVT field strength is displayed in dBµV/m.
