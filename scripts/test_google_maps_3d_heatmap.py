#!/usr/bin/env python3
"""
Standalone test for Google Maps 3D Tiles integration
Tests:
1. Fetching 3D tiles from Google Maps API
2. Caching tiles to prevent API overuse
3. Creating 3D heatmap visualization (equivalent to 2D heatmap)

This is a proof-of-concept before integrating into main codebase.
"""

import os
import json
import hashlib
import time
import logging
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from datetime import datetime, timedelta
import requests
import pandas as pd

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
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


class GoogleMaps3DTilesFetcher:
    """Fetches 3D tiles from Google Maps API with caching"""
    
    TILES_API_URL = "https://tile.googleapis.com/v1/3dtiles/root.json"
    
    def __init__(self, api_key: str, cache_dir: str = "./cache/google_maps_3d"):
        if not api_key:
            raise ValueError("Google Maps API key is required")
        
        self.api_key = api_key
        self.cache = GoogleMaps3DTilesCache(cache_dir)
        logger.info("Google Maps 3D Tiles Fetcher initialized")
    
    def fetch_tileset(self, use_cache: bool = True) -> Dict:
        """
        Fetch root tileset from Google Maps API
        
        Args:
            use_cache: Whether to use cached data if available
            
        Returns:
            Tileset JSON data
        """
        # Try cache first
        if use_cache:
            cached_tileset = self.cache.get_cached_tileset(self.api_key)
            if cached_tileset:
                return cached_tileset
        
        # Fetch from API
        logger.info("Fetching 3D tileset from Google Maps API...")
        url = f"{self.TILES_API_URL}?key={self.api_key}"
        
        # Headers similar to what curl/browser would send
        headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
            'Accept': 'application/json, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://developers.google.com/'
        }
        
        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            
            tileset_data = response.json()
            
            # Cache the response
            if use_cache:
                self.cache.cache_tileset(self.api_key, tileset_data)
            
            logger.info("Successfully fetched 3D tileset")
            return tileset_data
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch tileset: {e}")
            raise


class GoogleMaps3DHeatmapGenerator:
    """Generates 3D heatmap visualization using CesiumJS"""
    
    def __init__(self, tileset_url: str):
        self.tileset_url = tileset_url
        logger.info("3D Heatmap Generator initialized")
    
    def create_3d_heatmap_html(self,
                               heatmap_data: List[Tuple[float, float, float]],
                               output_path: str = "./test_3d_heatmap.html",
                               center_lat: float = None,
                               center_lon: float = None,
                               zoom: float = 15.0) -> str:
        """
        Create 3D heatmap HTML file
        
        Args:
            heatmap_data: List of (lat, lon, intensity) tuples
            output_path: Path to save HTML file
            center_lat: Center latitude (defaults to data mean)
            center_lon: Center longitude (defaults to data mean)
            zoom: Initial zoom level
            
        Returns:
            Path to generated HTML file
        """
        if not heatmap_data:
            raise ValueError("Heatmap data is required")
        
        # Calculate center if not provided
        if center_lat is None or center_lon is None:
            center_lat = sum(p[0] for p in heatmap_data) / len(heatmap_data)
            center_lon = sum(p[1] for p in heatmap_data) / len(heatmap_data)
        
        # Normalize intensity values for better visualization
        intensities = [p[2] for p in heatmap_data]
        max_intensity = max(intensities) if intensities else 1.0
        min_intensity = min(intensities) if intensities else 0.0
        intensity_range = max_intensity - min_intensity if max_intensity != min_intensity else 1.0
        
        # Convert to JavaScript format
        heatmap_points_js = json.dumps([
            {
                'lat': lat,
                'lon': lon,
                'intensity': (intensity - min_intensity) / intensity_range  # Normalize 0-1
            }
            for lat, lon, intensity in heatmap_data
        ])
        
        # Extract API key from tileset URL
        import urllib.parse
        parsed_url = urllib.parse.urlparse(self.tileset_url)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        api_key = query_params.get('key', [''])[0]
        
        # Generate HTML
        html_content = self._generate_cesium_heatmap_html(
            tileset_url=self.tileset_url,
            api_key=api_key,
            heatmap_points=heatmap_points_js,
            center_lat=center_lat,
            center_lon=center_lon,
            zoom=zoom
        )
        
        # Save HTML file
        os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else '.', exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        logger.info(f"3D heatmap saved to {output_path}")
        return output_path
    
    def _generate_cesium_heatmap_html(self,
                                     tileset_url: str,
                                     api_key: str,
                                     heatmap_points: str,
                                     center_lat: float,
                                     center_lon: float,
                                     zoom: float) -> str:
        """Generate HTML content with CesiumJS and 3D heatmap"""
        
        html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>3D Heatmap Test - Google Maps 3D Tiles</title>
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
        <h3>3D Heatmap Test</h3>
        <div>Points: {len(json.loads(heatmap_points))}</div>
        <div>Center: ({center_lat:.6f}, {center_lon:.6f})</div>
        <div class="legend">
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
    </div>
    
    <script>
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
            requestRenderMode: true,
        }});
        
        // Hide globe (we're using 3D tiles)
        viewer.scene.globe.show = false;
        
        // Configure Cesium to include API key in all tile requests
        const apiKey = '{api_key}';
        const baseUrl = 'https://tile.googleapis.com';
        
        // Custom request handler to add API key to all Google Maps tile requests
        // This is needed because relative URIs in the tileset need the API key
        // The tileset contains relative URIs like /v1/3dtiles/datasets/...?session=...
        const originalLoad = Cesium.Resource._Implementations.loadWithXhr;
        Cesium.Resource._Implementations.loadWithXhr = function(url, responseType, headers, deferred, overrideMimeType) {{
            // Handle relative URLs - resolve them to full URLs with base
            let fullUrl = url;
            if (url.startsWith('/')) {{
                // Relative URL - prepend base URL
                fullUrl = baseUrl + url;
            }} else if (!url.startsWith('http')) {{
                // Relative URL without leading slash
                fullUrl = baseUrl + '/' + url;
            }}
            
            // Add API key to query string for all Google Maps tile requests
            // Preserve existing query parameters (like session tokens)
            if (fullUrl.includes('tile.googleapis.com')) {{
                try {{
                    const urlObj = new URL(fullUrl);
                    // Only add key if not already present (preserve session tokens)
                    if (!urlObj.searchParams.has('key')) {{
                        urlObj.searchParams.set('key', apiKey);
                        fullUrl = urlObj.toString();
                    }}
                }} catch (e) {{
                    // If URL parsing fails, try simple string manipulation
                    // Only add key if not already present
                    if (!fullUrl.includes('key=')) {{
                        const separator = fullUrl.includes('?') ? '&' : '?';
                        fullUrl = fullUrl + separator + 'key=' + encodeURIComponent(apiKey);
                    }}
                }}
            }}
            
            // Debug logging (can be removed in production)
            if (fullUrl.includes('tile.googleapis.com')) {{
                console.log('Loading tile:', fullUrl.substring(0, 120) + '...');
            }}
            return originalLoad(fullUrl, responseType, headers, deferred, overrideMimeType);
        }};
        
        // Load Google Maps 3D Tiles
        const tileset = viewer.scene.primitives.add(
            new Cesium.Cesium3DTileset({{
                url: '{tileset_url}',
                showCreditsOnScreen: true,
                maximumScreenSpaceError: 16  // Adjust for better performance
            }})
        );
        
        tileset.readyPromise.then(function(loadedTileset) {{
            console.log('Google Maps 3D Tiles loaded successfully');
            console.log('Tileset bounding sphere:', loadedTileset.boundingSphere);
            console.log('Tileset ready for rendering');
            
            // Zoom to tileset after a short delay to ensure tiles are loading
            setTimeout(function() {{
                viewer.zoomTo(loadedTileset);
            }}, 1000);
        }}).otherwise(function(error) {{
            console.error('Error loading 3D Tiles:', error);
            console.error('Error details:', error.message, error.stack);
            alert('Error loading 3D Tiles: ' + error.message + '\\nCheck browser console for details.');
        }});
        
        // Monitor tile loading progress
        tileset.loadProgress.addEventListener(function(numberOfPendingRequests, numberOfTilesProcessing) {{
            console.log('Tiles loading: pending=' + numberOfPendingRequests + ', processing=' + numberOfTilesProcessing);
        }});
        
        // Heatmap points data
        const heatmapPoints = {heatmap_points};
        
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
        
        // Add heatmap points as 3D billboards
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
        
        // Create heatmap dot image
        function createHeatmapDot(color, intensity) {{
            const canvas = document.createElement('canvas');
            canvas.width = 64;
            canvas.height = 64;
            const ctx = canvas.getContext('2d');
            
            // Draw gradient circle
            const gradient = ctx.createRadialGradient(32, 32, 0, 32, 32, 32);
            gradient.addColorStop(0, Cesium.Color.toCssColorString(color.withAlpha(0.9)));
            gradient.addColorStop(0.5, Cesium.Color.toCssColorString(color.withAlpha(0.6)));
            gradient.addColorStop(1, Cesium.Color.toCssColorString(color.withAlpha(0.0)));
            
            ctx.fillStyle = gradient;
            ctx.beginPath();
            ctx.arc(32, 32, 32, 0, Math.PI * 2);
            ctx.fill();
            
            return canvas.toDataURL();
        }}
        
        // Set camera to center position
        viewer.camera.setView({{
            destination: Cesium.Cartesian3.fromDegrees({center_lon}, {center_lat}, 500),
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


def create_sample_heatmap_data(num_points: int = 50) -> List[Tuple[float, float, float]]:
    """
    Create sample heatmap data for testing
    Returns list of (lat, lon, intensity) tuples
    """
    import random
    
    # Use a known location (e.g., San Francisco)
    center_lat = 37.7749
    center_lon = -122.4194
    
    heatmap_data = []
    for _ in range(num_points):
        # Random points around center
        lat = center_lat + random.uniform(-0.01, 0.01)
        lon = center_lon + random.uniform(-0.01, 0.01)
        # Random intensity (0-1)
        intensity = random.uniform(0.0, 1.0)
        
        heatmap_data.append((lat, lon, intensity))
    
    return heatmap_data


def main():
    """Main test function"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Test Google Maps 3D Tiles with heatmap")
    parser.add_argument('--api-key', required=True, help='Google Maps Platform API key')
    parser.add_argument('--output', default='./test_3d_heatmap.html', help='Output HTML file path')
    parser.add_argument('--cache-dir', default='./cache/google_maps_3d', help='Cache directory')
    parser.add_argument('--no-cache', action='store_true', help='Disable cache')
    parser.add_argument('--points', type=int, default=50, help='Number of heatmap points')
    
    args = parser.parse_args()
    
    logger.info("=" * 60)
    logger.info("Google Maps 3D Tiles Heatmap Test")
    logger.info("=" * 60)
    
    try:
        # Step 1: Fetch 3D tileset
        logger.info("\n[Step 1] Fetching 3D tileset...")
        fetcher = GoogleMaps3DTilesFetcher(
            api_key=args.api_key,
            cache_dir=args.cache_dir
        )
        
        tileset_data = fetcher.fetch_tileset(use_cache=not args.no_cache)
        logger.info(f"✓ Tileset fetched successfully")
        logger.info(f"  Root: {tileset_data.get('root', {}).get('content', {}).get('uri', 'N/A')}")
        
        # Construct tileset URL
        tileset_url = f"https://tile.googleapis.com/v1/3dtiles/root.json?key={args.api_key}"
        
        # Step 2: Create sample heatmap data
        logger.info("\n[Step 2] Creating sample heatmap data...")
        heatmap_data = create_sample_heatmap_data(num_points=args.points)
        logger.info(f"✓ Created {len(heatmap_data)} heatmap points")
        
        # Step 3: Generate 3D heatmap
        logger.info("\n[Step 3] Generating 3D heatmap visualization...")
        generator = GoogleMaps3DHeatmapGenerator(tileset_url=tileset_url)
        output_path = generator.create_3d_heatmap_html(
            heatmap_data=heatmap_data,
            output_path=args.output
        )
        logger.info(f"✓ 3D heatmap generated: {output_path}")
        
        logger.info("\n" + "=" * 60)
        logger.info("Test completed successfully!")
        logger.info(f"Open {output_path} in a web browser to view the 3D heatmap")
        logger.info("=" * 60)
        
    except Exception as e:
        logger.error(f"\nTest failed: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0


if __name__ == "__main__":
    exit(main())

