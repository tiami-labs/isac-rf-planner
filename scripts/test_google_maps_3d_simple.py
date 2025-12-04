#!/usr/bin/env python3
"""
3D Map Test with Heatmap: Load Google Maps 3D Tiles and display 3D heatmap
Tests:
1. Fetching 3D tiles with caching
2. Creating sample heatmap data
3. Visualizing 3D heatmap on 3D map
"""

import os
import json
import argparse
import logging
import requests
import random
import hashlib
from pathlib import Path
from typing import List, Tuple, Optional, Dict
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class GoogleMaps3DTilesCache:
    """Cache manager for Google Maps 3D Tiles to prevent API overuse"""
    
    def __init__(self, cache_dir: str = "./cache/google_maps_3d"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_ttl_hours = 3  # Cache valid for 3 hours (matches API session)
        
    def _get_cache_key(self, api_key: str) -> str:
        """Generate cache key from API key (hash for security)"""
        return hashlib.md5(api_key.encode()).hexdigest()
    
    def _get_cache_path(self, cache_key: str) -> Path:
        """Get cache file path"""
        return self.cache_dir / f"tileset_{cache_key}.json"
    
    def get_cached_tileset(self, api_key: str) -> Optional[Dict]:
        """Get cached tileset if available and not expired"""
        cache_key = self._get_cache_key(api_key)
        cache_path = self._get_cache_path(cache_key)
        
        if not cache_path.exists():
            logger.info("No cached tileset found")
            return None
        
        try:
            with open(cache_path, 'r') as f:
                cache_data = json.load(f)
            
            # Check if cache is expired
            cached_time = datetime.fromisoformat(cache_data['timestamp'])
            if datetime.now() - cached_time > timedelta(hours=self.cache_ttl_hours):
                logger.info("Cached tileset expired")
                return None
            
            logger.info(f"Using cached tileset (age: {datetime.now() - cached_time})")
            return cache_data['tileset']
            
        except Exception as e:
            logger.warning(f"Error reading cache: {e}")
            return None
    
    def cache_tileset(self, api_key: str, tileset_data: Dict) -> None:
        """Cache tileset data with timestamp"""
        cache_key = self._get_cache_key(api_key)
        cache_path = self._get_cache_path(cache_key)
        
        try:
            cache_data = {
                'timestamp': datetime.now().isoformat(),
                'tileset': tileset_data
            }
            
            with open(cache_path, 'w') as f:
                json.dump(cache_data, f, indent=2)
            
            logger.info(f"Cached tileset to {cache_path}")
            
        except Exception as e:
            logger.error(f"Error caching tileset: {e}")


def fetch_tileset_with_cache(api_key: str, cache_dir: str = "./cache/google_maps_3d", use_cache: bool = True) -> Dict:
    """Fetch 3D tileset from Google Maps API with caching"""
    cache = GoogleMaps3DTilesCache(cache_dir)
    
    # Try cache first
    if use_cache:
        cached = cache.get_cached_tileset(api_key)
        if cached:
            return cached
    
    # Fetch from API
    url = f"https://tile.googleapis.com/v1/3dtiles/root.json?key={api_key}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    }
    
    logger.info("Fetching 3D tileset from Google Maps API...")
    response = requests.get(url, headers=headers, timeout=10)
    response.raise_for_status()
    
    tileset_data = response.json()
    
    # Cache it
    if use_cache:
        cache.cache_tileset(api_key, tileset_data)
    
    return tileset_data


def create_sample_heatmap_data(center_lat: float, center_lon: float, num_points: int = 50) -> List[Tuple[float, float, float]]:
    """
    Create sample heatmap data for testing
    Returns list of (lat, lon, intensity) tuples
    Intensity is normalized 0.0-1.0 (similar to RSRP normalized)
    """
    heatmap_data = []
    for _ in range(num_points):
        # Random points around center (within ~1km radius)
        lat = center_lat + random.uniform(-0.01, 0.01)
        lon = center_lon + random.uniform(-0.01, 0.01)
        # Random intensity (0-1) - simulating signal strength
        intensity = random.uniform(0.0, 1.0)
        
        heatmap_data.append((lat, lon, intensity))
    
    return heatmap_data


def create_simple_3d_map_html(
    api_key: str, 
    center_lat: float, 
    center_lon: float, 
    heatmap_data: List[Tuple[float, float, float]],
    output_path: str
):
    """Create HTML file that loads Google Maps 3D Tiles and displays 3D heatmap"""
    
    tileset_url = f"https://tile.googleapis.com/v1/3dtiles/root.json?key={api_key}"
    
    # Convert heatmap data to JSON for JavaScript
    heatmap_json = json.dumps([
        {"lat": lat, "lon": lon, "intensity": intensity}
        for lat, lon, intensity in heatmap_data
    ])
    
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Simple Google Maps 3D Tiles Test</title>
    <script src="https://cesium.com/downloads/cesiumjs/releases/1.111/Build/Cesium/Cesium.js"></script>
    <link href="https://cesium.com/downloads/cesiumjs/releases/1.111/Build/Cesium/Widgets/widgets.css" rel="stylesheet">
    <style>
        html, body, #cesiumContainer {{
            width: 100%; height: 100%; margin: 0; padding: 0; overflow: hidden;
        }}
        .info {{
            position: absolute;
            top: 10px;
            left: 10px;
            background: rgba(42, 42, 42, 0.9);
            color: white;
            padding: 10px;
            border-radius: 5px;
            font-family: Arial, sans-serif;
            font-size: 12px;
            z-index: 1000;
        }}
        .legend {{
            position: absolute;
            bottom: 10px;
            left: 10px;
            background: rgba(42, 42, 42, 0.8);
            padding: 10px;
            border-radius: 5px;
            color: white;
            font-family: Arial, sans-serif;
            font-size: 12px;
            z-index: 1000;
        }}
        .legend-item {{
            display: flex;
            align-items: center;
            margin-bottom: 5px;
        }}
        .legend-color {{
            width: 20px;
            height: 20px;
            margin-right: 10px;
            border-radius: 50%;
        }}
    </style>
</head>
<body>
    <div id="cesiumContainer"></div>
    <div class="info">
        <h3>3D Map with Heatmap</h3>
        <div>Center: ({center_lat:.6f}, {center_lon:.6f})</div>
        <div id="status">Loading...</div>
    </div>
    <div class="legend">
        <h4>3D Heatmap Intensity</h4>
        <div class="legend-item">
            <div class="legend-color" style="background-color: #2196F3;"></div>
            <span>Low Intensity (Blue)</span>
        </div>
        <div class="legend-item">
            <div class="legend-color" style="background-color: #4CAF50;"></div>
            <span>Medium (Lime)</span>
        </div>
        <div class="legend-item">
            <div class="legend-color" style="background-color: #FF9800;"></div>
            <span>High (Orange)</span>
        </div>
        <div class="legend-item">
            <div class="legend-color" style="background-color: #F44336;"></div>
            <span>Very High (Red)</span>
        </div>
    </div>
    
    <script>
        const statusEl = document.getElementById('status');
        const apiKey = '{api_key}';
        const tilesetUrl = '{tileset_url}';
        
        // Initialize Cesium viewer
        const viewer = new Cesium.Viewer('cesiumContainer', {{
            imageryProvider: false,
            baseLayerPicker: false,
            vrButton: false,
            geocoder: false,
            homeButton: true,
            infoBox: true,
            sceneModePicker: true,
            selectionIndicator: true,
            timeline: false,
            navigationHelpButton: true,
            animation: false,
            globe: false  // No default globe, we just want 3D Tiles
        }});
        
        // Optional but recommended by Google examples:
        Cesium.RequestScheduler.requestsByServer['tile.googleapis.com:443'] = 18;
        
        // Load Google Maps 3D Tiles using the correct Cesium 1.111 API
        (async function() {{
            try {{
                statusEl.textContent = 'Requesting tileset...';
                
                const tileset = await Cesium.Cesium3DTileset.fromUrl(
                    tilesetUrl,
                    {{
                        showCreditsOnScreen: true
                    }}
                );
                
                console.log('3D Tiles loaded', tileset);
                viewer.scene.primitives.add(tileset);
                statusEl.textContent = 'Tiles loaded!';
                
                // Fly to your chosen center
                viewer.camera.setView({{
                    destination: Cesium.Cartesian3.fromDegrees({center_lon}, {center_lat}, 500),
                    orientation: {{
                        heading: Cesium.Math.toRadians(0),
                        pitch: Cesium.Math.toRadians(-45),
                        roll: 0.0
                    }}
                }});
                
                // Optional: zoom to tileset (commented out to stay at custom camera position)
                // setTimeout(function() {{
                //     viewer.zoomTo(tileset);
                // }}, 1000);
                
                // Monitor tile loading – Cesium 1.111 uses `loadProgress`
                tileset.loadProgress.addEventListener(function(pending, processing) {{
                    console.log('Tiles: pending=' + pending + ', processing=' + processing);
                    if (pending === 0 && processing === 0) {{
                        statusEl.textContent = 'All tiles loaded';
                        // Add heatmap after tiles are loaded
                        addHeatmap();
                    }}
                }});
                
                // Heatmap data
                const heatmapPoints = {heatmap_json};
                
                // Color gradient function (matching 2D heatmap: blue -> lime -> orange -> red)
                function getHeatmapColor(intensity) {{
                    if (intensity < 0.4) {{
                        // Blue to Lime
                        const t = intensity / 0.4;
                        return Cesium.Color.fromBytes(
                            Math.floor(33 + (76 - 33) * t),   // R: 33->76
                            Math.floor(150 + (175 - 150) * t), // G: 150->175
                            Math.floor(243 + (80 - 243) * t)   // B: 243->80
                        );
                    }} else if (intensity < 0.6) {{
                        // Lime to Orange
                        const t = (intensity - 0.4) / 0.2;
                        return Cesium.Color.fromBytes(
                            Math.floor(76 + (255 - 76) * t),   // R: 76->255
                            Math.floor(175 + (152 - 175) * t), // G: 175->152
                            Math.floor(80 + (0 - 80) * t)      // B: 80->0
                        );
                    }} else if (intensity < 0.8) {{
                        // Orange to Red
                        const t = (intensity - 0.6) / 0.2;
                        return Cesium.Color.fromBytes(
                            255,  // R: 255
                            Math.floor(152 - 67 * t),  // G: 152->85
                            Math.floor(0 + 38 * t)     // B: 0->38
                        );
                    }} else {{
                        // Red
                        return Cesium.Color.RED;
                    }}
                }}
                
                // Function to create a circular dot image for billboards
                function createHeatmapDot(color, intensity) {{
                    const canvas = document.createElement('canvas');
                    canvas.width = 32;
                    canvas.height = 32;
                    const ctx = canvas.getContext('2d');
                    const radius = 15;
                    
                    ctx.beginPath();
                    ctx.arc(16, 16, radius, 0, 2 * Math.PI, false);
                    ctx.fillStyle = color.toCssColorString();
                    ctx.fill();
                    ctx.lineWidth = 2;
                    ctx.strokeStyle = Cesium.Color.WHITE.toCssColorString();
                    ctx.stroke();
                    
                    return canvas.toDataURL();
                }}
                
                // Add 3D heatmap points
                function addHeatmap() {{
                    statusEl.textContent = 'Adding heatmap...';
                    console.log('Adding ' + heatmapPoints.length + ' heatmap points');
                    
                    heatmapPoints.forEach(function(point) {{
                        const color = getHeatmapColor(point.intensity);
                        const height = 10 + point.intensity * 50; // Height based on intensity (10-60m)
                        
                        viewer.entities.add({{
                            position: Cesium.Cartesian3.fromDegrees(point.lon, point.lat, height),
                            billboard: {{
                                image: createHeatmapDot(color, point.intensity),
                                scale: 0.5 + point.intensity * 1.5, // Scale 0.5-2.0
                                heightReference: Cesium.HeightReference.RELATIVE_TO_GROUND,
                                verticalOrigin: Cesium.VerticalOrigin.BOTTOM
                            }},
                            label: {{
                                text: point.intensity.toFixed(2),
                                font: '10pt sans-serif',
                                fillColor: Cesium.Color.WHITE,
                                outlineColor: Cesium.Color.BLACK,
                                outlineWidth: 2,
                                style: Cesium.LabelStyle.FILL_AND_OUTLINE,
                                verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
                                pixelOffset: new Cesium.Cartesian2(0, -25),
                                show: false  // Hide by default, show on click
                            }},
                            description: `
                                <table>
                                    <tr><td><b>Intensity:</b></td><td>${{point.intensity.toFixed(3)}}</td></tr>
                                    <tr><td><b>Lat:</b></td><td>${{point.lat.toFixed(6)}}</td></tr>
                                    <tr><td><b>Lon:</b></td><td>${{point.lon.toFixed(6)}}</td></tr>
                                </table>
                            `
                        }});
                    }});
                    
                    statusEl.textContent = 'Heatmap added! (' + heatmapPoints.length + ' points)';
                    console.log('Heatmap visualization complete');
                }}
            }} catch (error) {{
                console.error('Error loading 3D Tiles:', error);
                statusEl.textContent = 'Error: ' + (error.message || error);
                alert('Error loading 3D Tiles: ' + (error.message || error));
            }}
        }})();
    </script>
</body>
</html>"""
    
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else '.', exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    logger.info(f"Simple 3D map HTML saved to {output_path}")
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Google Maps 3D Tiles test with heatmap")
    parser.add_argument('--api-key', required=True, help='Google Maps Platform API key')
    parser.add_argument('--lat', type=float, default=37.7749, help='Center latitude (default: San Francisco)')
    parser.add_argument('--lon', type=float, default=-122.4194, help='Center longitude (default: San Francisco)')
    parser.add_argument('--output', default='./test_3d_simple.html', help='Output HTML file')
    parser.add_argument('--cache-dir', default='./cache/google_maps_3d', help='Cache directory')
    parser.add_argument('--no-cache', action='store_true', help='Disable cache')
    parser.add_argument('--points', type=int, default=50, help='Number of heatmap points')
    
    args = parser.parse_args()
    
    logger.info("=" * 60)
    logger.info("Google Maps 3D Tiles Test with Heatmap")
    logger.info("=" * 60)
    logger.info(f"Center: ({args.lat}, {args.lon})")
    
    try:
        # Step 1: Fetch tileset with caching
        logger.info("\n[Step 1] Fetching 3D tileset (with caching)...")
        tileset_data = fetch_tileset_with_cache(
            api_key=args.api_key,
            cache_dir=args.cache_dir,
            use_cache=not args.no_cache
        )
        logger.info("✓ Tileset fetched/cached successfully")
        
        # Step 2: Create sample heatmap data
        logger.info(f"\n[Step 2] Creating sample heatmap data ({args.points} points)...")
        heatmap_data = create_sample_heatmap_data(
            center_lat=args.lat,
            center_lon=args.lon,
            num_points=args.points
        )
        logger.info(f"✓ Created {len(heatmap_data)} heatmap points")
        
        # Step 3: Generate HTML with 3D heatmap
        logger.info("\n[Step 3] Generating 3D map HTML with heatmap...")
        output_path = create_simple_3d_map_html(
            api_key=args.api_key,
            center_lat=args.lat,
            center_lon=args.lon,
            heatmap_data=heatmap_data,
            output_path=args.output
        )
        logger.info(f"✓ HTML generated: {output_path}")
        
        logger.info("\n" + "=" * 60)
        logger.info("Test completed successfully!")
        logger.info("")
        logger.info("IMPORTANT: Run from a local HTTP server, not file://")
        logger.info("  cd /home/tiami-manu/2025.v4/rf-planning")
        logger.info("  python3 -m http.server 8000")
        logger.info(f"  Then open: http://localhost:8000/{os.path.basename(output_path)}")
        logger.info("")
        logger.info(f"Or open directly: {output_path}")
        logger.info("=" * 60)
        
    except Exception as e:
        logger.error(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0


if __name__ == "__main__":
    exit(main())

