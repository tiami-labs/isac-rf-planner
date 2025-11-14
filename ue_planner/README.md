# RF Planner - ISAC-based UE Placement and RF Planning System

A comprehensive Python system for real-time UE placement and RF planning using telemetry data from NR PBCH decoding. This system leverages ISAC (Integrated Sensing and Communication) techniques to provide intelligent RF planning recommendations.

## Features

### 🎯 Core Capabilities
- **Telemetry Data Processing**: Parse and analyze JSON telemetry data from NR PBCH decoding
- **ISAC Analysis**: Motion detection, timing analysis, and positioning using ISAC techniques
- **RF Planning**: Coverage analysis, interference detection, and network optimization
- **UE Placement**: Intelligent placement recommendations based on signal quality and motion patterns
- **Real-time Monitoring**: Live monitoring of telemetry data with automatic updates

### 📊 Visualization
- **Interactive Dashboards**: Plotly-based interactive visualizations
- **Coverage Maps**: Folium-based interactive maps with signal strength visualization
- **Motion Analysis**: Comprehensive motion detection and trajectory analysis plots
- **Signal Quality Trends**: Time-series analysis of signal quality metrics
- **Network Optimization**: Visualization of coverage gaps, interference areas, and recommendations

### 🔧 Technical Features
- **Multi-antenna Support**: Analysis of MRC weights and antenna quality metrics
- **Motion Detection**: Microsecond-level timing analysis for motion detection
- **Position Estimation**: Hybrid timing and signal strength-based positioning
- **Network Optimization**: Automated detection of coverage gaps and interference areas
- **Real-time Processing**: Live data processing with configurable update intervals

## Installation

### Prerequisites
- Python 3.7 or higher
- Access to telemetry data from NR PBCH decoding

### Install Dependencies
```bash
pip install -r rfplanner/requirements.txt
```

### Quick Start
```bash
# Clone or download the rfplanner folder
cd rfplanner

# Install dependencies
pip install -r requirements.txt

# Run analysis on your telemetry data
python -m main analyze --output ./results
```

## Usage

### Command Line Interface

The RF Planner provides a comprehensive command-line interface with multiple commands:

#### 1. Complete Analysis
```bash
# Run complete RF planning analysis
python -m main analyze --output ./results

# With custom configuration
python -m main analyze --config my_config.json --output ./results --save-config
```

#### 2. Real-time Monitoring
```bash
# Start real-time monitoring (updates every 60 seconds)
python -m main monitor

# Custom update interval (30 seconds)
python -m main monitor --interval 30
```

#### 3. Visualization Only
```bash
# Generate visualizations from existing data
python -m main visualize --output ./plots
```

#### 4. Data Export
```bash
# Export analysis results to CSV/JSON files
python -m main export --output ./data
```

#### 5. System Status
```bash
# Check system status and data availability
python -m main status
```

### Python API

You can also use the RF Planner as a Python library:

```python
from rfplanner import RFPlanner
from rfplanner.config import RFPlannerConfig

# Initialize with custom configuration
config = RFPlannerConfig(
    telemetry_data_path="../tiami_data",
    output_path="./results",
    isac_enabled=True
)

# Create RF Planner instance
rf_planner = RFPlanner(config)

# Load and analyze data
if rf_planner.load_data():
    # Perform ISAC analysis
    isac_results = rf_planner.analyze_isac_data()
    
    # Perform RF planning
    planning_results = rf_planner.perform_rf_planning()
    
    # Generate visualizations
    viz_files = rf_planner.generate_visualizations()
    
    # Get recommendations
    recommendations = rf_planner.get_recommendations()
    
    print(f"Analysis complete: {len(isac_results['motion_events'])} motion events detected")
```

## Configuration

The system uses a comprehensive configuration system. Key configuration options:

### Data Paths
- `telemetry_data_path`: Path to telemetry JSON files
- `output_path`: Output directory for results and visualizations

### ISAC Settings
- `isac_enabled`: Enable/disable ISAC analysis
- `motion_detection_threshold_us`: Motion detection threshold in microseconds
- `timing_accuracy_threshold_us`: Timing accuracy threshold

### RF Planning Parameters
- `min_snr_db`: Minimum SNR threshold
- `max_path_loss_db`: Maximum path loss threshold
- `coverage_radius_m`: Default coverage radius in meters

### Example Configuration
```json
{
    "telemetry_data_path": "../tiami_data",
    "output_path": "./rfplanner_output",
    "isac_enabled": true,
    "motion_detection_threshold_us": 10.0,
    "min_snr_db": 10.0,
    "coverage_radius_m": 500.0
}
```

## Data Format

The system expects telemetry data in JSON format with the following structure:

```json
[
    {
        "timestamp_ms": 1640995200000,
        "latitude": 40.7128,
        "longitude": -74.0060,
        "altitude": 0.0,
        "pci": 123,
        "ssb_index": 0,
        "channel_level_db": [-75.2, -78.1],
        "mrc_weights": [0.6, 0.4],
        "llr_energy": 45.2,
        "freq_offset_hz": 1250.5,
        "current_timing_offset_us": 15,
        "initial_timing_offset_us": 10,
        "motion_magnitude": 5.0,
        "iso_timestamp": "2022-01-01T12:00:00.000Z"
    }
]
```

## Output Files

The system generates various output files:

### Visualizations
- `telemetry_overview.png`: Overview plots of telemetry data
- `coverage_map.html`: Interactive coverage map
- `motion_analysis.png`: Motion detection analysis
- `signal_quality_trends.png`: Signal quality trends
- `network_optimization.png`: Network optimization results
- `interactive_dashboard.html`: Interactive Plotly dashboard

### Data Exports
- `telemetry_data.csv`: Processed telemetry data
- `motion_data.csv`: Motion analysis results
- `signal_quality_data.csv`: Signal quality metrics
- `analysis_statistics.json`: Analysis statistics

## ISAC Features

### Motion Detection
- **Microsecond Precision**: Timing analysis with microsecond-level precision
- **Motion Classification**: Stationary, slow motion, and fast motion detection
- **Confidence Scoring**: Motion detection confidence based on signal quality

### Position Estimation
- **Hybrid Positioning**: Combines timing and signal strength information
- **Multi-method Fusion**: Timing-based, signal strength-based, and hybrid approaches
- **Confidence Metrics**: Position estimation confidence scoring

### Trajectory Analysis
- **Distance Calculation**: Haversine-based distance calculations
- **Speed Estimation**: Velocity estimation from timing changes
- **Pattern Recognition**: Motion pattern analysis and classification

## RF Planning Features

### Coverage Analysis
- **Signal Strength Mapping**: Comprehensive signal strength analysis
- **SNR Calculation**: Signal-to-noise ratio estimation
- **Coverage Quality**: Coverage quality classification (excellent/good/fair/poor)

### Network Optimization
- **Coverage Gap Detection**: Automated detection of coverage gaps
- **Interference Analysis**: Identification of interference areas
- **Capacity Planning**: Traffic load analysis and capacity recommendations

### Placement Recommendations
- **Optimal Placement**: Signal quality-based placement recommendations
- **Cluster Analysis**: K-means clustering for optimal positioning
- **Motion-aware Placement**: Dynamic placement based on motion patterns

## Real-time Monitoring

The system supports real-time monitoring with:

- **Live Data Processing**: Continuous processing of new telemetry data
- **Automatic Updates**: Configurable update intervals
- **Real-time Dashboards**: Live visualization updates
- **Alert System**: Automatic detection of issues and recommendations

## Troubleshooting

### Common Issues

1. **No telemetry data found**
   - Check the `telemetry_data_path` configuration
   - Ensure JSON files are in the correct format
   - Verify file permissions

2. **Visualization errors**
   - Install all required dependencies: `pip install -r requirements.txt`
   - Check for sufficient disk space
   - Verify matplotlib backend configuration

3. **Memory issues with large datasets**
   - Reduce `max_history_points` in configuration
   - Use data filtering options
   - Consider processing data in chunks

### Performance Optimization

- **Data Filtering**: Use time-based filtering for large datasets
- **Caching**: Enable result caching for repeated analyses
- **Parallel Processing**: Use multi-threading for large datasets

## Contributing

To contribute to the RF Planner system:

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests for new functionality
5. Submit a pull request

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Support

For support and questions:
- Check the documentation
- Review the example configurations
- Open an issue for bugs or feature requests

## Acknowledgments

- OpenAirInterface community for 5G NR specifications
- Scientific Python community for data analysis tools 