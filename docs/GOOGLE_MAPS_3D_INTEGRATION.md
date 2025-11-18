# Google Maps 3D Tiles Integration

## Overview

This integration adds support for Google Maps Photorealistic 3D Tiles to the UE Planner visualization system. It enables immersive 3D visualization of telemetry data, coverage areas, and gNB positions using high-resolution 3D maps.

## Prerequisites

1. **Google Cloud Project**
   - Create a project in [Google Cloud Console](https://console.cloud.google.com/)
   - Enable billing for the project

2. **Enable Map Tiles API**
   - Navigate to APIs & Services > Library
   - Search for "Map Tiles API"
   - Click "Enable"

3. **API Key**
   - Go to APIs & Services > Credentials
   - Create API Key
   - Restrict the key to:
     - Application restrictions: HTTP referrers (for web) or IP addresses (for server)
     - API restrictions: Restrict to "Map Tiles API"

4. **Optional: Cesium Ion Token**
   - Sign up at [Cesium Ion](https://cesium.com/ion/)
   - Get your access token (free tier available)
   - This is optional but recommended for better terrain and 3D models

## Configuration

Add the following to your `RFPlannerConfig` or config JSON file:

```python
config = RFPlannerConfig(
    google_maps_api_key="YOUR_API_KEY_HERE",
    google_maps_3d_enabled=True,
    cesium_ion_token="YOUR_CESIUM_TOKEN"  # Optional
)
```

Or in JSON:

```json
{
    "google_maps_api_key": "YOUR_API_KEY_HERE",
    "google_maps_3d_enabled": true,
    "cesium_ion_token": "YOUR_CESIUM_TOKEN"
}
```

## Usage

### Basic Usage

```python
from agentic_ue_planner import RFPlanner, RFPlannerConfig

# Configure with Google Maps API key
config = RFPlannerConfig(
    google_maps_api_key="YOUR_API_KEY",
    google_maps_3d_enabled=True
)

# Initialize planner
planner = RFPlanner(config)

# Load data
planner.load_data()

# Generate 3D visualization
html_path = planner.visualizer.plot_coverage_map_3d(
    telemetry_data=planner.telemetry_data,
    coverage_areas=planner.planning_results.get('coverage_areas', []),
    gnb_positions=[(40.7128, -74.0060, 50.0)],  # (lat, lon, alt)
    save_path="./output/coverage_3d.html"
)

print(f"3D map saved to: {html_path}")
```

### With Replay Analysis

```python
# Run replay analysis
result = planner.run_replay_analysis(
    gps_file="path/to/gps.csv",
    output_dir="./replay_results"
)

# Extract gNB positions
gnb_positions = []
if result.estimated_gnb_position:
    lat, lon, confidence = result.estimated_gnb_position
    gnb_positions.append((lat, lon, 50.0))  # 50m altitude

# Create 3D visualization with gNB position
html_path = planner.visualizer.plot_coverage_map_3d(
    telemetry_data=planner.telemetry_data,
    gnb_positions=gnb_positions,
    save_path="./replay_results/coverage_3d.html"
)
```

### Direct API Usage

```python
from agentic_ue_planner.ui.google_maps_3d import GoogleMaps3DTiles
from agentic_ue_planner.config import RFPlannerConfig

# Initialize 3D Tiles provider
config = RFPlannerConfig(google_maps_api_key="YOUR_API_KEY")
tiles_provider = GoogleMaps3DTiles(api_key="YOUR_API_KEY", config=config)

# Get session token
session_token = tiles_provider.get_session_token()

# Get root tileset URL
tileset_url = tiles_provider.get_root_tileset_url(session_token)

# Create 3D visualization
html_path = tiles_provider.create_cesium_html(
    telemetry_data=your_telemetry_df,
    output_path="./output/3d_map.html"
)
```

## Features

### 3D Visualization
- **Photorealistic 3D Buildings**: High-resolution 3D models of buildings and structures
- **Terrain**: Real-world terrain elevation
- **Coverage Areas**: 3D ellipses showing coverage zones
- **Telemetry Points**: Color-coded points based on signal strength
- **gNB Positions**: 3D markers for base station locations

### Color Coding
- **Green**: Good signal (>-70 dB)
- **Yellow**: Fair signal (-70 to -80 dB)
- **Orange**: Poor signal (-80 to -90 dB)
- **Red**: Very poor signal (<-90 dB)
- **Blue**: Coverage areas
- **Purple**: gNB positions

### Interactive Features
- **Camera Controls**: Pan, zoom, rotate, tilt
- **Entity Selection**: Click on points to see details
- **Info Box**: Detailed information popup
- **Timeline**: Time-based animation (if time data available)
- **Scene Modes**: 3D, 2D, Columbus View, CV

## API Reference

### GoogleMaps3DTiles Class

#### Methods

- `get_session_token() -> str`: Get or refresh session token (valid for 3+ hours)
- `get_root_tileset_url(session_token: str = None) -> str`: Get root tileset URL
- `create_cesium_html(...) -> str`: Create HTML file with CesiumJS visualization

### RFVisualizer Class

#### New Methods

- `plot_coverage_map_3d(...) -> Optional[str]`: Create 3D coverage map

## Limitations

1. **Coverage**: 3D Tiles are available in 49+ countries. Check [Google Maps Platform coverage](https://mapsplatform.google.com/maps-products/map-tiles/) for your area.

2. **Session Tokens**: Session tokens are valid for at least 3 hours. The system automatically refreshes them when needed.

3. **API Quotas**: Google Maps Platform has usage quotas. Monitor your usage in the Cloud Console.

4. **Cesium Ion**: Some features (terrain, 3D models) may require Cesium Ion token, though basic 3D Tiles work without it.

## Best Practices

1. **API Key Security**: Never commit API keys to version control. Use environment variables or secure config files.

2. **Error Handling**: Always check if `google_maps_3d` is initialized before use:
   ```python
   if planner.visualizer.google_maps_3d:
       html_path = planner.visualizer.plot_coverage_map_3d(...)
   ```

3. **Performance**: For large datasets, consider filtering telemetry points before visualization.

4. **Attribution**: The generated HTML includes proper attribution as required by Google's terms.

## Troubleshooting

### "Failed to obtain session token"
- Check that your API key is valid
- Verify Map Tiles API is enabled
- Check API key restrictions

### "Error loading 3D Tiles"
- Verify session token is valid
- Check network connectivity
- Ensure location is within 3D Tiles coverage area

### "Cesium Ion token required"
- This is optional for basic 3D Tiles
- Sign up at cesium.com/ion for free token
- Add token to config if you want enhanced terrain/models

## Resources

- [Google Maps 3D Tiles Documentation](https://developers.google.com/maps/documentation/tile/3d-tiles)
- [Map Tiles API Overview](https://developers.google.com/maps/documentation/tile/overview)
- [CesiumJS Documentation](https://cesium.com/learn/cesiumjs/)
- [Google Maps Platform Policies](https://developers.google.com/maps/documentation/tile/policies)

