# OFDM and Modulation Scheme Features

## Overview

The RF planning system now includes comprehensive OFDM and modulation scheme support, enabling realistic 5G/LTE link budget calculations with link adaptation.

## New Parameters

### OFDM Parameters

- **Subcarrier Spacing (kHz)**: 15, 30, 60, or 120 kHz
  - 15 kHz: LTE and 5G FR1 (low band)
  - 30 kHz: 5G FR1 (mid band)
  - 60 kHz: 5G FR1 (high band)
  - 120 kHz: 5G FR2 (mmWave)

- **Number of Resource Blocks**: Typically 100 for 20 MHz LTE, 51-273 for 5G depending on bandwidth
- **Channel Bandwidth (MHz)**: Total channel bandwidth (e.g., 20, 100 MHz)

### MIMO Parameters

- **Number of TX Antennas**: 1-8 (typical: 1, 2, 4, 8)
- **Number of RX Antennas**: 1-8 (typical: 1, 2, 4, 8)
- **MIMO Mode**: 
  - `SISO`: Single Input Single Output (1x1)
  - `SIMO`: Single Input Multiple Output (1xN) - receive diversity
  - `MISO`: Multiple Input Single Output (Nx1) - transmit diversity
  - `MIMO`: Multiple Input Multiple Output (NxM) - spatial multiplexing

**MIMO Gains**:
- Diversity gain: ~3 dB per doubling of antennas (reduces fading)
- Spatial multiplexing: Up to min(TX, RX) parallel streams (increases throughput)

### Link Adaptation

- **Enable Link Adaptation**: Automatically selects best modulation based on SINR
- **Fixed Modulation**: Optional override to force a specific modulation scheme
  - Options: `QPSK`, `16-QAM`, `64-QAM`, `256-QAM`, `1024-QAM`

## Modulation Schemes

| Modulation | Bits/Symbol | Min SINR (dB) | Spectral Efficiency (bps/Hz) | Use Case |
|------------|-------------|---------------|------------------------------|----------|
| QPSK | 2 | -2.0 | 1.0 | Poor signal (cell edge) |
| 16-QAM | 4 | 5.0 | 2.4 | Moderate signal |
| 64-QAM | 6 | 12.0 | 4.2 | Good signal |
| 256-QAM | 8 | 20.0 | 6.0 | Excellent signal |
| 1024-QAM | 10 | 28.0 | 8.0 | Very strong signal (5G) |

## Enhanced Output

The `AttenuationGrid` now includes:

1. **RSRP (dBm)**: Received Signal Received Power
2. **SINR (dB)**: Signal-to-Interference-plus-Noise Ratio
3. **Modulation**: Selected modulation scheme per cell
4. **Throughput (Mbps)**: Estimated throughput per cell

### Throughput Calculation

```
Throughput = Spectral_Efficiency × Bandwidth × MIMO_Streams
```

Where:
- Spectral Efficiency: From modulation scheme (bits/s/Hz)
- Bandwidth: Effective OFDM bandwidth (active subcarriers × SCS)
- MIMO Streams: Number of parallel spatial streams

## Example Configurations

### LTE 20 MHz (Default)
```yaml
subcarrier_spacing_khz: 15.0
num_resource_blocks: 100
channel_bandwidth_mhz: 20.0
num_tx_antennas: 2
num_rx_antennas: 2
mimo_mode: "MIMO"
```

### 5G FR1 100 MHz
```yaml
subcarrier_spacing_khz: 30.0
num_resource_blocks: 273
channel_bandwidth_mhz: 100.0
num_tx_antennas: 4
num_rx_antennas: 4
mimo_mode: "MIMO"
```

### 5G FR2 mmWave
```yaml
subcarrier_spacing_khz: 120.0
num_resource_blocks: 66
channel_bandwidth_mhz: 100.0
num_tx_antennas: 8
num_rx_antennas: 8
mimo_mode: "MIMO"
```

## API Usage

### Request Parameters

```json
{
  "lat": 37.7749,
  "lon": -122.4194,
  "freq_mhz": 3500.0,
  "tx_power_dbm": 43.0,
  "subcarrier_spacing_khz": 15.0,
  "num_resource_blocks": 100,
  "channel_bandwidth_mhz": 20.0,
  "num_tx_antennas": 2,
  "num_rx_antennas": 2,
  "mimo_mode": "MIMO",
  "enable_link_adaptation": true,
  "fixed_modulation": null
}
```

### Response Fields

```json
{
  "grid": {
    "cell_lat": [...],
    "cell_lon": [...],
    "rsrp_dbm": [...],
    "sinr_db": [...],
    "modulation": ["64-QAM", "16-QAM", ...],
    "throughput_mbps": [84.0, 48.0, ...]
  }
}
```

## Implementation Details

### Files Added

1. `src/agentic_rf_planner/rf/modulation_schemes.py` - Modulation scheme definitions and link adaptation
2. `src/agentic_rf_planner/rf/ofdm_params.py` - OFDM parameters and MIMO configuration

### Files Modified

1. `src/agentic_rf_planner/pipeline/schemas.py` - Extended `RFParams` and `AttenuationGrid`
2. `src/agentic_rf_planner/rf/attenuation_models.py` - Enhanced to compute SINR, modulation, throughput
3. `src/agentic_rf_planner/api/rest.py` - Added new parameters to API
4. `src/agentic_rf_planner/ui/static/app.js` - Updated to send new parameters
5. `configs/rf.params.yaml` - Added default OFDM/MIMO parameters

## Future Enhancements

- [ ] Interference model (co-channel interference from other cells)
- [ ] Advanced MIMO precoding (beamforming gains)
- [ ] HARQ retransmission overhead
- [ ] Control channel overhead
- [ ] Multi-user MIMO (MU-MIMO)
- [ ] Carrier aggregation
- [ ] UI controls for OFDM/MIMO parameters

