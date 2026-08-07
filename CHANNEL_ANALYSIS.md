# Waveform-agnostic link budget and bistatic channel analysis

This feature is available for **5G NR, ATSC 1.0, ATSC 3.0, DVB-T, and baseline DVT**. It is not part of the broadcast-only transmitter model.

## Start and use the planner

Start the application exactly as before:

```bash
uvicorn agentic_rf_planner.api.rest:app --reload
```

In either the 2D or 3D planner:

1. Select any waveform and configure its transmitter normally.
2. Select the TX site and coverage radius.
3. Open **Link budget and bistatic channel**.
4. Enable **waveform-agnostic receiver, target-return and Doppler analysis**.
5. Enter the fixed analysis receiver, target assumptions, target velocity, and processing assumptions.
6. Select a channel display layer, such as **Bistatic echo power**, **Signed bistatic Doppler**, or **Detection margin**.
7. Run the plan.
8. Use **Download machine-readable channel grid (.npz)** for every per-target output array.

No separate command or preprocessing step is required.

## What the planner calculates

The existing propagation solver supplies the transmitter-to-candidate-target channel. This preserves the active waveform's:

- center frequency and wavelength;
- occupied/channel bandwidth;
- physical total-carrier transmitter power;
- TX chain gain, feeder loss, and antenna gain;
- NR sector radiation pattern or DVT broadcast radiation pattern;
- terrain, OSM obstruction, diffraction, vegetation, and configured propagation losses.

For each candidate target position, the channel-analysis layer adds:

- TX-to-target range;
- target-to-receiver range;
- total bistatic path range;
- excess bistatic path range relative to the direct TX-to-receiver path;
- total and excess delay;
- bistatic angle;
- path-range rate and closing speed;
- signed bistatic Doppler;
- incident total-carrier power at the target;
- target echo power at the receiver input;
- pre- and post-processing SNR;
- detection margin;
- residual-direct-path comparison and required dynamic range;
- resolved, ambiguous, and detectable Boolean flags.

The signed Doppler convention is:

```text
positive Doppler = the TX → target → RX path is shortening
```

The implemented relation is:

```text
f_d = -(f_c / c) · d(R_TX,target + R_target,RX)/dt
```

## Waveform-independent source power

### 5G NR

The channel analysis uses total-carrier EIRP rather than RSRP/reference-signal power:

```text
total carrier EIRP = conducted TX power + TX-chain gain + antenna gain - feeder loss
```

For a directional NR deployment, the strongest configured sector toward the analysis path is used. The normal NR coverage output remains RSRP/SINR.

### Broadcast waveforms

The channel analysis uses the broadcast transmitter's physical ERP/EIRP chain and its broadcast antenna radiation pattern. It does not create cellular sectors.

## Current direct and return path model

The TX-to-target leg uses the planner's full one-way propagation result.

The current fixed receiver legs are explicit and machine-labeled:

```text
direct TX → RX: free-space loss + directPathExcessLossDb
target → RX:    free-space loss + returnPathExcessLossDb
```

These excess-loss fields allow measured or externally modeled losses to be inserted without hiding the assumption.

## Doppler resolution and detectability

```text
Doppler resolution = 1 / coherent integration time
Doppler threshold  = max(resolution, clutter notch, configured minimum Doppler)
maximum unambiguous Doppler = PRF / 2, when PRF is supplied
```

A candidate target is marked `detectable=true` only when all three checks pass:

```text
post-processing SNR margin >= 0 dB
absolute Doppler >= Doppler threshold
absolute Doppler <= PRF/2, when PRF is configured
```

This is a deterministic link-budget/channel flag, not a probability-of-detection model.

## Machine-readable API output

The JSON response includes `channel_analysis`, containing configuration, direct-path link budget, resolution limits, counts, and best points.

It also includes `channel_analysis_product`:

```json
{
  "product_id": "<32 hex characters>",
  "format": "npz",
  "download_url": "/api/channel-analysis/products/<product_id>",
  "point_count": 123456,
  "arrays": ["latitude_deg", "longitude_deg", "echo_power_dbm", "doppler_hz"]
}
```

The NPZ contains these arrays in common point order:

```text
latitude_deg
longitude_deg
incident_isotropic_power_dbm
echo_power_dbm
preprocessing_snr_db
postprocessing_snr_db
detection_margin_db
echo_to_residual_direct_db
required_dynamic_range_db
tx_target_range_m
target_receiver_range_m
bistatic_path_range_m
excess_path_range_m
excess_delay_s
bistatic_angle_deg
path_range_rate_mps
closing_speed_mps
doppler_hz
doppler_resolved
doppler_ambiguous
detectable
metadata_json
```

Example reader:

```python
import json
import numpy as np

with np.load("channel_analysis_<id>.npz", allow_pickle=False) as product:
    metadata = json.loads(str(product["metadata_json"]))
    latitude = product["latitude_deg"]
    longitude = product["longitude_deg"]
    echo_dbm = product["echo_power_dbm"]
    doppler_hz = product["doppler_hz"]
    detectable = product["detectable"]
```

Products are written automatically under `data/channel_analysis/` relative to the directory from which Uvicorn was started. Set `RF_CHANNEL_PRODUCT_DIR` only when a different storage directory is required.
