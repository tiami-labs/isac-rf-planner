"""
Google Maps 3D Tiles Integration
Provides 3D visualization using Google Maps Photorealistic 3D Tiles
"""

import os
import json
import logging
from typing import Dict, List, Optional, Tuple, Any
import pandas as pd
import requests
from datetime import datetime, timedelta

from ..config import DEFAULT_CONFIG


class GoogleMaps3DTiles:
    """Google Maps 3D Tiles provider for 3D visualization"""
    
    # Google Maps Tiles API endpoints
    TILES_API_BASE_URL = "https://tile.googleapis.com/v1/3dtiles:root"
    SESSION_TOKEN_URL = "https://tile.googleapis.com/v1/sessiontokens"
    
    def __init__(self, api_key: str, config=DEFAULT_CONFIG):
        """
        Initialize Google Maps 3D Tiles provider
        
        Args:
            api_key: Google Maps Platform API key with Map Tiles API enabled
            config: RFPlannerConfig instance
        """
        if not api_key:
            raise ValueError("Google Maps API key is required for 3D Tiles")
        
        self.api_key = api_key
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.session_token = None
        self.session_token_expiry = None
        
    def get_session_token(self) -> str:
        """
        Get or refresh session token for 3D Tiles access
        
        Session tokens are valid for at least 3 hours
        
        Returns:
            Session token string
        """
        # Check if we have a valid session token
        if self.session_token and self.session_token_expiry:
            if datetime.now() < self.session_token_expiry:
                return self.session_token
        
        # Request new session token
        try:
            url = f"{self.SESSION_TOKEN_URL}?key={self.api_key}"
            response = requests.post(url, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            self.session_token = data.get('session')
            
            # Set expiry to 3 hours from now (conservative)
            self.session_token_expiry = datetime.now() + timedelta(hours=3)
            
            self.logger.info("Obtained new Google Maps 3D Tiles session token")
            return self.session_token
            
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Failed to obtain session token: {e}")
            raise
    
    def get_root_tileset_url(self, session_token: str = None) -> str:
        """
        Get the root tileset URL for 3D Tiles
        
        Args:
            session_token: Optional session token (will be fetched if not provided)
            
        Returns:
            Root tileset URL
        """
        if session_token is None:
            session_token = self.get_session_token()
        
        # Construct the root tileset URL
        url = f"{self.TILES_API_BASE_URL}?key={self.api_key}&session_token={session_token}"
        return url
    
    def create_cesium_html(self, 
                          telemetry_data: pd.DataFrame,
                          coverage_areas: List[Any] = None,
                          gnb_positions: List[Tuple[float, float, float]] = None,
                          output_path: str = "./google_maps_3d.html",
                          center_lat: float = None,
                          center_lon: float = None,
                          zoom: float = 15.0) -> str:
        """
        Create an HTML file with CesiumJS rendering Google Maps 3D Tiles
        
        Args:
            telemetry_data: DataFrame with telemetry data (latitude, longitude, etc.)
            coverage_areas: List of coverage area objects
            gnb_positions: List of (lat, lon, altitude) tuples for gNB positions
            output_path: Path to save the HTML file
            center_lat: Center latitude (defaults to data mean)
            center_lon: Center longitude (defaults to data mean)
            zoom: Initial zoom level
            
        Returns:
            Path to the generated HTML file
        """
        try:
            # Get session token
            session_token = self.get_session_token()
            root_tileset_url = self.get_root_tileset_url(session_token)
            
            # Calculate center if not provided
            if center_lat is None or center_lon is None:
                if not telemetry_data.empty:
                    center_lat = telemetry_data['latitude'].mean()
                    center_lon = telemetry_data['longitude'].mean()
                else:
                    center_lat = self.config.map_center_lat
                    center_lon = self.config.map_center_lon
            
            # Prepare telemetry points for visualization
            telemetry_points = []
            if not telemetry_data.empty:
                for _, row in telemetry_data.iterrows():
                    # Calculate signal strength
                    if isinstance(row.get('channel_level_db'), list):
                        signal_strength = sum([v for v in row['channel_level_db'] if v > -999.0]) / len([v for v in row['channel_level_db'] if v > -999.0])
                    else:
                        signal_strength = row.get('channel_level_db', -999.0)
                    
                    telemetry_points.append({
                        'lat': row['latitude'],
                        'lon': row['longitude'],
                        'alt': row.get('altitude', 0.0),
                        'pci': row.get('pci', 0),
                        'signal': signal_strength,
                        'llr': row.get('llr_energy', 0.0),
                        'timestamp': str(row.get('timestamp', ''))
                    })
            
            # Prepare coverage areas
            coverage_data = []
            if coverage_areas:
                for area in coverage_areas:
                    coverage_data.append({
                        'lat': area.latitude,
                        'lon': area.longitude,
                        'radius': area.radius_m,
                        'quality': area.coverage_quality,
                        'snr': area.snr_db
                    })
            
            # Prepare gNB positions
            gnb_data = []
            if gnb_positions:
                for lat, lon, alt in gnb_positions:
                    gnb_data.append({
                        'lat': lat,
                        'lon': lon,
                        'alt': alt
                    })
            
            # Create HTML with CesiumJS
            html_content = self._generate_cesium_html(
                root_tileset_url=root_tileset_url,
                center_lat=center_lat,
                center_lon=center_lon,
                zoom=zoom,
                telemetry_points=telemetry_points,
                coverage_areas=coverage_data,
                gnb_positions=gnb_data
            )
            
            # Save HTML file
            os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else '.', exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(html_content)
            
            self.logger.info(f"Google Maps 3D visualization saved to {output_path}")
            return output_path
            
        except Exception as e:
            self.logger.error(f"Error creating Google Maps 3D visualization: {e}")
            raise
    
    def _generate_cesium_html(self,
                            root_tileset_url: str,
                            center_lat: float,
                            center_lon: float,
                            zoom: float,
                            telemetry_points: List[Dict],
                            coverage_areas: List[Dict],
                            gnb_positions: List[Dict]) -> str:
        """Generate HTML content with CesiumJS and Google Maps 3D Tiles"""
        
        # Convert data to JSON for JavaScript
        telemetry_json = json.dumps(telemetry_points)
        coverage_json = json.dumps(coverage_areas)
        gnb_json = json.dumps(gnb_positions)
        
        html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>UE Planner - Google Maps 3D Visualization</title>
    <script src="https://cesium.com/downloads/cesiumjs/releases/1.111/Build/Cesium/Cesium.js"></script>
    <link href="https://cesium.com/downloads/cesiumjs/releases/1.111/Build/Cesium/Widgets/widgets.css" rel="stylesheet">
    <style>
        html, body, #cesiumContainer {{
            width: 100%; height: 100%; margin: 0; padding: 0; overflow: hidden;
        }}
        .info-panel {{
            position: absolute;
            top: 10px;
            left: 10px;
            background: rgba(42, 42, 42, 0.9);
            color: white;
            padding: 15px;
            border-radius: 5px;
            font-family: Arial, sans-serif;
            font-size: 12px;
            z-index: 1000;
            max-width: 300px;
        }}
        .info-panel h3 {{
            margin: 0 0 10px 0;
            color: #4CAF50;
        }}
        .legend {{
            margin-top: 10px;
        }}
        .legend-item {{
            display: flex;
            align-items: center;
            margin: 5px 0;
        }}
        .legend-color {{
            width: 20px;
            height: 20px;
            border-radius: 50%;
            margin-right: 8px;
        }}
    </style>
</head>
<body>
    <div id="cesiumContainer"></div>
    <div class="info-panel">
        <h3>UE Planner 3D Map</h3>
        <div>Telemetry Points: {len(telemetry_points)}</div>
        <div>Coverage Areas: {len(coverage_areas)}</div>
        <div>gNB Positions: {len(gnb_positions)}</div>
        <div class="legend">
            <div class="legend-item">
                <div class="legend-color" style="background-color: #4CAF50;"></div>
                <span>Good Signal (>-70 dB)</span>
            </div>
            <div class="legend-item">
                <div class="legend-color" style="background-color: #FFC107;"></div>
                <span>Fair Signal (-70 to -80 dB)</span>
            </div>
            <div class="legend-item">
                <div class="legend-color" style="background-color: #FF9800;"></div>
                <span>Poor Signal (-80 to -90 dB)</span>
            </div>
            <div class="legend-item">
                <div class="legend-color" style="background-color: #F44336;"></div>
                <span>Very Poor (<-90 dB)</span>
            </div>
            <div class="legend-item">
                <div class="legend-color" style="background-color: #2196F3;"></div>
                <span>Coverage Area</span>
            </div>
            <div class="legend-item">
                <div class="legend-color" style="background-color: #9C27B0;"></div>
                <span>gNB Position</span>
            </div>
        </div>
    </div>
    
    <script>
        // Cesium Ion access token (you may need to set this)
        // Cesium.Ion.defaultAccessToken = 'YOUR_ION_ACCESS_TOKEN';
        
        // Initialize Cesium viewer
        const viewer = new Cesium.Viewer('cesiumContainer', {{
            terrainProvider: Cesium.createWorldTerrain(),
            baseLayerPicker: false,
            vrButton: false,
            geocoder: false,
            homeButton: true,
            infoBox: true,
            sceneModePicker: true,
            selectionIndicator: true,
            timeline: true,
            navigationHelpButton: true,
            animation: true,
            shouldAnimate: true
        }});
        
        // Load Google Maps 3D Tiles
        const tileset = viewer.scene.primitives.add(
            new Cesium.Cesium3DTileset({{
                url: '{root_tileset_url}'
            }})
        );
        
        tileset.readyPromise.then(function(tileset) {{
            viewer.zoomTo(tileset);
            console.log('Google Maps 3D Tiles loaded successfully');
        }}).otherwise(function(error) {{
            console.error('Error loading 3D Tiles:', error);
        }});
        
        // Telemetry points data
        const telemetryPoints = {telemetry_json};
        
        // Coverage areas data
        const coverageAreas = {coverage_json};
        
        // gNB positions data
        const gnbPositions = {gnb_json};
        
        // Add telemetry points
        telemetryPoints.forEach(function(point) {{
            // Determine color based on signal strength
            let color = Cesium.Color.RED;
            if (point.signal > -70) {{
                color = Cesium.Color.GREEN;
            }} else if (point.signal > -80) {{
                color = Cesium.Color.YELLOW;
            }} else if (point.signal > -90) {{
                color = Cesium.Color.ORANGE;
            }}
            
            // Create point entity
            viewer.entities.add({{
                position: Cesium.Cartesian3.fromDegrees(point.lon, point.lat, point.alt + 10),
                point: {{
                    pixelSize: 10,
                    color: color,
                    outlineColor: Cesium.Color.WHITE,
                    outlineWidth: 2,
                    heightReference: Cesium.HeightReference.RELATIVE_TO_GROUND
                }},
                label: {{
                    text: 'PCI: ' + point.pci,
                    font: '12pt sans-serif',
                    fillColor: Cesium.Color.WHITE,
                    outlineColor: Cesium.Color.BLACK,
                    outlineWidth: 2,
                    style: Cesium.LabelStyle.FILL_AND_OUTLINE,
                    verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
                    pixelOffset: new Cesium.Cartesian2(0, -30)
                }},
                description: `
                    <table>
                        <tr><td><b>PCI:</b></td><td>${{point.pci}}</td></tr>
                        <tr><td><b>Signal:</b></td><td>${{point.signal.toFixed(1)}} dB</td></tr>
                        <tr><td><b>LLR Energy:</b></td><td>${{point.llr.toFixed(2)}}</td></tr>
                        <tr><td><b>Time:</b></td><td>${{point.timestamp}}</td></tr>
                    </table>
                `
            }});
        }});
        
        // Add coverage areas
        coverageAreas.forEach(function(area) {{
            viewer.entities.add({{
                position: Cesium.Cartesian3.fromDegrees(area.lon, area.lat, 0),
                ellipse: {{
                    semiMajorAxis: area.radius,
                    semiMinorAxis: area.radius,
                    material: Cesium.Color.BLUE.withAlpha(0.3),
                    heightReference: Cesium.HeightReference.RELATIVE_TO_GROUND
                }},
                label: {{
                    text: area.quality,
                    font: '10pt sans-serif',
                    fillColor: Cesium.Color.WHITE,
                    outlineColor: Cesium.Color.BLACK,
                    outlineWidth: 2,
                    style: Cesium.LabelStyle.FILL_AND_OUTLINE,
                    verticalOrigin: Cesium.VerticalOrigin.BOTTOM
                }},
                description: `
                    <table>
                        <tr><td><b>Quality:</b></td><td>${{area.quality}}</td></tr>
                        <tr><td><b>SNR:</b></td><td>${{area.snr.toFixed(1)}} dB</td></tr>
                        <tr><td><b>Radius:</b></td><td>${{area.radius.toFixed(0)}} m</td></tr>
                    </table>
                `
            }});
        }});
        
        // Add gNB positions
        gnbPositions.forEach(function(gnb, index) {{
            viewer.entities.add({{
                position: Cesium.Cartesian3.fromDegrees(gnb.lon, gnb.lat, gnb.alt),
                point: {{
                    pixelSize: 15,
                    color: Cesium.Color.PURPLE,
                    outlineColor: Cesium.Color.WHITE,
                    outlineWidth: 3,
                    heightReference: Cesium.HeightReference.RELATIVE_TO_GROUND
                }},
                model: {{
                    uri: 'https://raw.githubusercontent.com/CesiumGS/cesium/main/Apps/SampleData/models/CesiumGround/Cesium_Ground.glb',
                    minimumPixelSize: 64,
                    maximumScale: 20000,
                    heightReference: Cesium.HeightReference.RELATIVE_TO_GROUND
                }},
                label: {{
                    text: 'gNB ' + (index + 1),
                    font: '14pt sans-serif',
                    fillColor: Cesium.Color.WHITE,
                    outlineColor: Cesium.Color.BLACK,
                    outlineWidth: 2,
                    style: Cesium.LabelStyle.FILL_AND_OUTLINE,
                    verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
                    pixelOffset: new Cesium.Cartesian2(0, -40)
                }},
                description: `
                    <table>
                        <tr><td><b>gNB Position</b></td></tr>
                        <tr><td><b>Lat:</b></td><td>${{gnb.lat.toFixed(6)}}</td></tr>
                        <tr><td><b>Lon:</b></td><td>${{gnb.lon.toFixed(6)}}</td></tr>
                        <tr><td><b>Alt:</b></td><td>${{gnb.alt.toFixed(1)}} m</td></tr>
                    </table>
                `
            }});
        }});
        
        // Set camera to center position
        viewer.camera.setView({{
            destination: Cesium.Cartesian3.fromDegrees({center_lon}, {center_lat}, 1000),
            orientation: {{
                heading: Cesium.Math.toRadians(0),
                pitch: Cesium.Math.toRadians(-45),
                roll: 0.0
            }}
        }});
        
        // Add attribution
        viewer.cesiumWidget.creditContainer.style.display = "block";
    </script>
</body>
</html>"""
        
        return html_template

