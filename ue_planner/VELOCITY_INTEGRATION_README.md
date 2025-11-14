# Velocity Integration for RF Planner

This document describes the implementation of GPS integration and velocity estimation for the RF Planner system, following a phased approach to ensure robust and accurate motion analysis.

## Overview

The velocity integration system replaces placeholder GPS coordinates (0.0, 0.0) with actual GPS positioning and implements velocity estimation from telemetry data using CFO (Carrier Frequency Offset) and timing drift measurements.

## Implementation Phases

### Phase 1: GPS Position Integration
**Goal**: Replace placeholder coordinates with actual GPS data

**Components**:
- Enhanced `GPSTelemetryMatcher` class
- GPS data loading and parsing
- Timestamp-based matching between GPS and telemetry
- Quality assessment for position accuracy

**Key Features**:
- Loads GPS trajectory data from CSV files
- Matches GPS timestamps with telemetry timestamps
- Calculates position quality scores
- Handles multiple GPS formats and coordinate systems

### Phase 2: Velocity Estimation from Telemetry
**Goal**: Implement velocity estimation using CFO and timing measurements

**Components**:
- CFO-based velocity estimation
- Timing drift-based velocity estimation
- Combined velocity estimation with quality weighting
- Velocity estimation quality assessment

**Formulas Implemented**:

1. **CFO-based velocity**:
   ```
   v_radial = (freq_offset_hz * c) / carrier_freq
   ```
   Where:
   - `freq_offset_hz`: Frequency offset in Hz
   - `c`: Speed of light (299,792,458 m/s)
   - `carrier_freq`: Downlink carrier frequency (typically 3.5 GHz)

2. **Timing drift-based velocity**:
   ```
   v_radial = (timing_drift_us * 1e-6 * c) / dt_s
   ```
   Where:
   - `timing_drift_us`: Timing drift in microseconds
   - `dt_s`: Time interval in seconds
   - `c`: Speed of light

3. **Combined velocity estimation**:
   ```
   v_combined = w1 * v_cfo + w2 * v_timing
   ```
   Where weights depend on signal quality:
   - Good signal (> -80 dBm): 70% CFO, 30% timing
   - Poor signal (≤ -80 dBm): 30% CFO, 70% timing

### Phase 3: GPS-Telemetry Velocity Comparison
**Goal**: Compare estimated velocities with GPS ground truth

**Components**:
- `VelocityAnalyzer` class for comprehensive analysis
- Statistical analysis of velocity accuracy
- Visualization of velocity comparisons
- Error analysis by different conditions

**Analysis Features**:
- Velocity error statistics (mean, RMSE, correlation)
- Error analysis by velocity ranges (low, medium, high)
- Error analysis by signal quality
- Time series analysis of velocity tracking

## Usage

### Basic Usage

```python
from rfplanner.core import RFPlanner

# Initialize RF Planner
planner = RFPlanner()

# Phase 1: Match GPS with telemetry
matched_measurements = planner.match_gps_telemetry(
    gps_file="gps_trajectory.csv",
    telemetry_files=["telemetry_data.json"]
)

# Phase 2: Analyze velocity accuracy
velocity_analysis = planner.analyze_velocity_accuracy()

# Phase 3: Generate visualizations
plot_files = planner.generate_velocity_visualizations()

# Export results
csv_file = planner.export_velocity_analysis()
```

### Complete Example

Run the complete example to see all phases in action:

```bash
cd rfplanner
python velocity_integration_example.py
```

This will:
1. Create sample GPS and telemetry data
2. Demonstrate all three phases
3. Generate comprehensive visualizations
4. Export detailed analysis results

## Expected Performance

### Velocity Resolution

Based on theoretical analysis and implementation:

**Single SSB burst (~20ms)**:
- **SNR = 10 dB**: ~3 m/s (1-σ)
- **SNR = 20 dB**: ~1 m/s (1-σ)

**Multi-burst averaging**:
- **200ms averaging**: ~1 m/s (at 10 dB SNR)
- **1s averaging**: ~0.4 m/s (at 10 dB SNR)

**Long-term accuracy**:
- **Realistic resolution**: 0.5-1 m/s (≥200ms averaging)
- **Best case**: 0.2-0.5 m/s (high SNR, long averaging)

### Accuracy Factors

1. **Signal Quality**: Higher SNR improves CFO estimation
2. **Motion Stability**: Lower velocity improves timing accuracy
3. **Measurement Duration**: Longer averaging reduces noise
4. **Carrier Frequency**: Higher frequencies provide better resolution

## File Structure

```
rfplanner/
├── core.py                          # Main RF Planner orchestrator
├── gps_telemetry_matcher.py         # GPS-telemetry matching (Phase 1)
├── velocity_analyzer.py             # Velocity analysis (Phase 3)
├── velocity_integration_example.py  # Complete example
├── VELOCITY_INTEGRATION_README.md   # This file
└── requirements.txt                 # Updated dependencies
```

## Output Files

The system generates several output files:

### Analysis Results
- `velocity_analysis.csv`: Detailed velocity comparison data
- `matched_measurements.csv`: GPS-telemetry matched data

### Visualizations
- `velocity_scatter.png`: GPS vs estimated velocity scatter plot
- `error_distribution.png`: Velocity error distribution
- `error_vs_quality.png`: Error vs signal quality analysis
- `velocity_timeseries.png`: Time series velocity comparison
- `error_by_conditions.png`: Error analysis by different conditions

## Configuration

### GPS Data Format

Expected CSV format:
```csv
time,location.lat,location.lon,altitude,velocity.x,velocity.y,velocity.z,horizontal_accuracy,vertical_accuracy
2024-01-01T12:00:00Z,40.7128,-74.0060,10.0,5.0,2.0,0.0,2.0,3.0
```

### Telemetry Data Format

Expected JSON format (from PBCH telemetry):
```json
{
  "timestamp_ms": 1704110400000,
  "freq_offset_hz": 1250.5,
  "current_timing_offset_us": 15,
  "initial_timing_offset_us": 10,
  "timing_measurement_timestamp_ms": 1704110400000,
  "dl_carrier_freq": 3500000000,
  "channel_level_db": [-65.0, -68.0],
  "pci": 123
}
```

## Troubleshooting

### Common Issues

1. **No matched measurements**:
   - Check timestamp formats in GPS and telemetry data
   - Verify time tolerance settings (default: 2 seconds)
   - Ensure GPS and telemetry data overlap in time

2. **Poor velocity accuracy**:
   - Check signal quality (should be > -80 dBm for good results)
   - Verify carrier frequency is correctly set
   - Ensure timing measurements are stable

3. **Missing visualizations**:
   - Check matplotlib installation
   - Verify output directory permissions
   - Ensure velocity analysis data is available

### Debug Mode

Enable debug logging:
```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

## Future Enhancements

1. **Advanced Filtering**: Implement Kalman filtering for velocity estimation
2. **Multi-Cell Fusion**: Combine velocity estimates from multiple cells
3. **Real-time Processing**: Stream processing for live velocity tracking
4. **Machine Learning**: ML-based velocity estimation improvement
5. **3D Velocity**: Full 3D velocity vector estimation

## References

1. **CFO-based velocity estimation**: Doppler effect in wireless communications
2. **Timing drift velocity**: Propagation delay changes due to motion
3. **PBCH DM-RS phase tracking**: 3GPP TS 38.211 for reference signal structure
4. **Velocity resolution limits**: Shannon-Hartley theorem for estimation bounds

## Support

For issues or questions regarding the velocity integration:

1. Check the troubleshooting section above
2. Review the example code in `velocity_integration_example.py`
3. Examine the generated analysis files for detailed diagnostics
4. Verify GPS and telemetry data formats match expected schemas 