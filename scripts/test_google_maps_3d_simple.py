#!/usr/bin/env python3
"""3D Map Test + 3D Heatmap

Generates an HTML file that loads Google Maps Photorealistic 3D Tiles in CesiumJS.

What this adds compared to the old billboard/sprite heatmap:
- A *real 3D* heatmap overlay (cylinders) that is depth-tested against the Google mesh.
- Optional subsurface depth (the cylinders extend below the sampled surface).
- Optional global clipping plane (cutaway) so you can expose the subsurface.

Reality check:
- Google photorealistic tiles are a textured mesh with no per-building/forest metadata exposed.
- You cannot "paint on the mesh" (texture draping/blending) in a supported way in CesiumJS 1.111.
  The approach here is an overlay (your own geometry) that visually interacts via depth testing.

Run:
  python3 scripts/test_google_maps_3d_simple.py --api-key YOUR_KEY --lat 37.7749 --lon -122.4194 --points 50 --output ./test_3d_simple.html
Serve:
  python3 -m http.server 8000
Open:
  http://localhost:8000/test_3d_simple.html
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class GoogleMaps3DTilesCache:
    """Cache manager for Google Maps 3D Tiles to prevent API overuse."""

    def __init__(self, cache_dir: str = "./cache/google_maps_3d"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # Session tokens are at least ~3 hours; keep cache aligned.
        self.cache_ttl_hours = 3

    def _get_cache_key(self, api_key: str) -> str:
        return hashlib.md5(api_key.encode()).hexdigest()

    def _get_cache_path(self, cache_key: str) -> Path:
        return self.cache_dir / f"tileset_{cache_key}.json"

    def get_cached_tileset(self, api_key: str) -> Optional[Dict]:
        cache_key = self._get_cache_key(api_key)
        cache_path = self._get_cache_path(cache_key)

        if not cache_path.exists():
            logger.info("No cached tileset found")
            return None

        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache_data = json.load(f)

            cached_time = datetime.fromisoformat(cache_data["timestamp"])
            if datetime.now() - cached_time > timedelta(hours=self.cache_ttl_hours):
                logger.info("Cached tileset expired")
                return None

            logger.info(f"Using cached tileset (age: {datetime.now() - cached_time})")
            return cache_data["tileset"]
        except Exception as e:
            logger.warning(f"Error reading cache: {e}")
            return None

    def cache_tileset(self, api_key: str, tileset_data: Dict) -> None:
        cache_key = self._get_cache_key(api_key)
        cache_path = self._get_cache_path(cache_key)
        try:
            cache_data = {"timestamp": datetime.now().isoformat(), "tileset": tileset_data}
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, indent=2)
            logger.info(f"Cached tileset to {cache_path}")
        except Exception as e:
            logger.error(f"Error caching tileset: {e}")


def fetch_tileset_with_cache(api_key: str, cache_dir: str, use_cache: bool) -> Dict:
    """Fetch 3D tileset root.json from Google Maps API with caching."""
    cache = GoogleMaps3DTilesCache(cache_dir)
    if use_cache:
        cached = cache.get_cached_tileset(api_key)
        if cached:
            return cached

    url = f"https://tile.googleapis.com/v1/3dtiles/root.json?key={api_key}"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }
    logger.info("Fetching 3D tileset from Google Maps API...")
    response = requests.get(url, headers=headers, timeout=10)
    response.raise_for_status()
    tileset_data = response.json()

    if use_cache:
        cache.cache_tileset(api_key, tileset_data)

    return tileset_data


def create_sample_heatmap_data(center_lat: float, center_lon: float, num_points: int = 50) -> List[Tuple[float, float, float]]:
    """Create sample heat sources around a center: (lat, lon, intensity[0..1])."""
    heatmap_data: List[Tuple[float, float, float]] = []
    for _ in range(num_points):
        # Random points around center (roughly within ~1km radius)
        lat = center_lat + random.uniform(-0.01, 0.01)
        lon = center_lon + random.uniform(-0.01, 0.01)
        intensity = random.uniform(0.0, 1.0)
        heatmap_data.append((lat, lon, intensity))
    return heatmap_data


def create_simple_3d_map_html(
    api_key: str,
    center_lat: float,
    center_lon: float,
    heatmap_data: List[Tuple[float, float, float]],
    output_path: str,
    heatmap_mode: str = "columns",
    max_height_m: float = 80.0,
    radius_m: float = 12.0,
    subsurface_depth_m: float = 30.0,
    cutaway_enabled: bool = True,
) -> str:
    """Create an HTML file that loads Google Maps 3D Tiles and overlays a 3D heatmap."""

    tileset_url = f"https://tile.googleapis.com/v1/3dtiles/root.json?key={api_key}"

    heatmap_json = json.dumps(
        [{"lat": lat, "lon": lon, "intensity": intensity} for lat, lon, intensity in heatmap_data]
    )

    # Note: the API key must be present client-side for Map Tiles API. Treat it as public and lock it down
    # using HTTP referrer restrictions and API restrictions in Google Cloud.
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Google Photorealistic 3D Tiles + 3D Heatmap</title>
  <script src="https://cesium.com/downloads/cesiumjs/releases/1.111/Build/Cesium/Cesium.js"></script>
  <link href="https://cesium.com/downloads/cesiumjs/releases/1.111/Build/Cesium/Widgets/widgets.css" rel="stylesheet">
  <style>
    html, body, #cesiumContainer {{
      width: 100%; height: 100%; margin: 0; padding: 0; overflow: hidden;
    }}
    .panel {{
      position: absolute;
      background: rgba(42, 42, 42, 0.90);
      color: white;
      padding: 10px 12px;
      border-radius: 6px;
      font-family: Arial, sans-serif;
      font-size: 12px;
      z-index: 1000;
      max-width: 360px;
    }}
    #infoPanel {{ top: 10px; left: 10px; }}
    #controlsPanel {{ top: 10px; right: 10px; }}
    #legendPanel {{ bottom: 10px; left: 10px; }}
    .row {{ display:flex; align-items:center; justify-content:space-between; gap:10px; margin: 6px 0; }}
    .row label {{ white-space: nowrap; }}
    input[type="range"] {{ width: 190px; }}
    select {{ width: 200px; }}
    .legend-item {{ display:flex; align-items:center; margin: 4px 0; }}
    .legend-color {{ width: 16px; height: 16px; border-radius: 50%; margin-right: 8px; border: 1px solid rgba(255,255,255,0.35); }}
    code {{ color: #b6e0ff; }}
  </style>
</head>
<body>
  <div id="cesiumContainer"></div>

  <div id="infoPanel" class="panel">
    <div style="font-weight:700; font-size:13px; margin-bottom:6px;">3D Tiles + 3D Heatmap</div>
    <div>Center: ({center_lat:.6f}, {center_lon:.6f})</div>
    <div>Heat points: {len(heatmap_data)}</div>
    <div id="status" style="margin-top:6px;">Loading…</div>
    <div id="pick" style="margin-top:6px; opacity:0.9;"></div>
  </div>

  <div id="controlsPanel" class="panel">
    <div style="font-weight:700; font-size:13px; margin-bottom:6px;">Controls</div>

    <div class="row">
      <label for="mode">Heatmap mode</label>
      <select id="mode">
        <option value="columns">3D columns (volumes)</option>
        <option value="billboards">Billboards (old)</option>
      </select>
    </div>

    <div class="row">
      <label for="maxHeight">Max height</label>
      <input id="maxHeight" type="range" min="10" max="200" value="{int(max_height_m)}" step="1" />
    </div>

    <div class="row">
      <label for="radius">Radius</label>
      <input id="radius" type="range" min="2" max="60" value="{int(radius_m)}" step="1" />
    </div>

    <div class="row">
      <label for="subDepth">Subsurface depth</label>
      <input id="subDepth" type="range" min="0" max="150" value="{int(subsurface_depth_m)}" step="1" />
    </div>

    <div class="row" style="margin-top:10px;">
      <label><input id="cutawayEnabled" type="checkbox" {"checked" if cutaway_enabled else ""}/> Cutaway plane</label>
      <span id="cutawayValue"></span>
    </div>
    <div class="row">
      <label for="cutawayHeight">Cut height (m)</label>
      <input id="cutawayHeight" type="range" min="-80" max="200" value="50" step="1" />
    </div>

    <div class="row" style="margin-top:10px;">
      <button id="rerender" style="width:100%; padding:8px; border-radius:6px; border:0; cursor:pointer;">Rebuild heatmap</button>
    </div>

    <div style="opacity:0.85; margin-top:6px; line-height:1.3;">
      Tip: enable the cutaway and drag it down (lower values) to expose subsurface volumes.
    </div>
  </div>

  <div id="legendPanel" class="panel">
    <div style="font-weight:700; font-size:13px; margin-bottom:6px;">Intensity → Color</div>
    <div class="legend-item"><div class="legend-color" style="background:#2196F3;"></div>Low</div>
    <div class="legend-item"><div class="legend-color" style="background:#4CAF50;"></div>Medium</div>
    <div class="legend-item"><div class="legend-color" style="background:#FF9800;"></div>High</div>
    <div class="legend-item"><div class="legend-color" style="background:#F44336;"></div>Very high</div>
  </div>

<script>
(() => {{
  const statusEl = document.getElementById('status');
  const pickEl = document.getElementById('pick');

  const tilesetUrl = {json.dumps(tileset_url)};
  const heatSources = {heatmap_json};

  // UI
  const modeEl = document.getElementById('mode');
  const maxHeightEl = document.getElementById('maxHeight');
  const radiusEl = document.getElementById('radius');
  const subDepthEl = document.getElementById('subDepth');
  const cutawayEnabledEl = document.getElementById('cutawayEnabled');
  const cutawayHeightEl = document.getElementById('cutawayHeight');
  const cutawayValueEl = document.getElementById('cutawayValue');
  const rerenderBtn = document.getElementById('rerender');

  modeEl.value = {json.dumps(heatmap_mode)};

  // Viewer
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
    globe: false
  }});

  Cesium.RequestScheduler.requestsByServer['tile.googleapis.com:443'] = 18;

  let heatPrimitive = null;
  let cutawayPlanes = null;
  let tileset = null;

  function clamp01(x) {{
    return Math.max(0.0, Math.min(1.0, x));
  }}

  // Blue -> Green -> Orange -> Red (roughly matching your legend)
  function getHeatmapColor(intensity) {{
    const i = clamp01(intensity);
    if (i < 0.4) {{
      const t = i / 0.4;
      return Cesium.Color.fromBytes(
        Math.floor(33 + (76 - 33) * t),
        Math.floor(150 + (175 - 150) * t),
        Math.floor(243 + (80 - 243) * t)
      );
    }} else if (i < 0.6) {{
      const t = (i - 0.4) / 0.2;
      return Cesium.Color.fromBytes(
        Math.floor(76 + (255 - 76) * t),
        Math.floor(175 + (152 - 175) * t),
        Math.floor(80 + (0 - 80) * t)
      );
    }} else if (i < 0.8) {{
      const t = (i - 0.6) / 0.2;
      return Cesium.Color.fromBytes(
        255,
        Math.floor(152 - 67 * t),
        Math.floor(0 + 38 * t)
      );
    }}
    return Cesium.Color.RED;
  }}

  function destroyHeatmap() {{
    if (heatPrimitive) {{
      viewer.scene.primitives.remove(heatPrimitive);
      heatPrimitive = null;
    }}
    // Billboard mode uses entities; clear them too.
    viewer.entities.removeAll();
  }}

  function createHeatmapDot(color) {{
    const canvas = document.createElement('canvas');
    canvas.width = 32;
    canvas.height = 32;
    const ctx = canvas.getContext('2d');
    ctx.beginPath();
    ctx.arc(16, 16, 14, 0, 2 * Math.PI, false);
    ctx.fillStyle = color.toCssColorString();
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = Cesium.Color.WHITE.toCssColorString();
    ctx.stroke();
    return canvas.toDataURL();
  }}

  async function clampPointsToMesh(cartesians) {{
    // clampToHeightMostDetailed is the best option when available.
    // If not available, fall back to unclamped.
    try {{
      if (viewer.scene.clampToHeightSupported && viewer.scene.clampToHeightMostDetailed) {{
        const updated = await viewer.scene.clampToHeightMostDetailed(cartesians);
        return updated;
      }}
    }} catch (e) {{
      console.warn('clampToHeightMostDetailed failed; continuing without clamping.', e);
    }}
    return cartesians;
  }}

  async function buildColumnsHeatmap() {{
    destroyHeatmap();

    const maxHeight = parseFloat(maxHeightEl.value);
    const radiusM = parseFloat(radiusEl.value);
    const subDepth = parseFloat(subDepthEl.value);

    statusEl.textContent = 'Building 3D heatmap volumes…';

    const cartesians = heatSources.map(p => Cesium.Cartesian3.fromDegrees(p.lon, p.lat, 0));
    const clamped = await clampPointsToMesh(cartesians);

    const instances = [];
    const alpha = 0.55;  // visual clarity

    for (let i = 0; i < heatSources.length; i++) {{
      const src = heatSources[i];
      const pos = clamped[i];
      if (!Cesium.defined(pos)) continue;

      const intensity = clamp01(src.intensity);
      const above = Math.max(2.0, intensity * maxHeight);
      const depth = Math.max(0.0, subDepth);
      const length = above + depth;

      const geom = new Cesium.CylinderGeometry({{
        length: length,
        topRadius: radiusM,
        bottomRadius: radiusM,
        vertexFormat: Cesium.PerInstanceColorAppearance.VERTEX_FORMAT
      }});

      // Center the cylinder so it extends depth below the surface and above above-surface
      // Local ENU origin at the clamped surface point:
      const enu = Cesium.Transforms.eastNorthUpToFixedFrame(pos);
      const offsetUp = (above - depth) / 2.0;
      const modelMatrix = Cesium.Matrix4.multiplyByTranslation(
        enu,
        new Cesium.Cartesian3(0, 0, offsetUp),
        new Cesium.Matrix4()
      );

      const color = getHeatmapColor(intensity).withAlpha(alpha);

      instances.push(new Cesium.GeometryInstance({{
        geometry: geom,
        modelMatrix: modelMatrix,
        attributes: {{
          color: Cesium.ColorGeometryInstanceAttribute.fromColor(color)
        }}
      }}));
    }}

    heatPrimitive = viewer.scene.primitives.add(new Cesium.Primitive({{
      geometryInstances: instances,
      appearance: new Cesium.PerInstanceColorAppearance({{
        translucent: true,
        closed: true
      }}),
      asynchronous: true
    }}));

    statusEl.textContent = `3D heatmap ready (columns: ${{instances.length}}).`;
  }}

  async function buildBillboardsHeatmap() {{
    destroyHeatmap();

    statusEl.textContent = 'Building billboards…';

    const maxHeight = parseFloat(maxHeightEl.value);
    const cartesians = heatSources.map(p => Cesium.Cartesian3.fromDegrees(p.lon, p.lat, 0));
    const clamped = await clampPointsToMesh(cartesians);

    for (let i = 0; i < heatSources.length; i++) {{
      const src = heatSources[i];
      const pos = clamped[i];
      if (!Cesium.defined(pos)) continue;

      const intensity = clamp01(src.intensity);
      const color = getHeatmapColor(intensity);
      const extraHeight = 2 + intensity * maxHeight;

      // Billboard wants cartographic degrees + height:
      const carto = Cesium.Cartographic.fromCartesian(pos);
      viewer.entities.add({{
        position: Cesium.Cartesian3.fromRadians(carto.longitude, carto.latitude, carto.height + extraHeight),
        billboard: {{
          image: createHeatmapDot(color),
          scale: 0.6 + intensity * 1.2,
          verticalOrigin: Cesium.VerticalOrigin.BOTTOM
        }},
        description: `
          <table>
            <tr><td><b>Intensity:</b></td><td>${{intensity.toFixed(3)}}</td></tr>
            <tr><td><b>Lat:</b></td><td>${{Cesium.Math.toDegrees(carto.latitude).toFixed(6)}}</td></tr>
            <tr><td><b>Lon:</b></td><td>${{Cesium.Math.toDegrees(carto.longitude).toFixed(6)}}</td></tr>
          </table>
        `
      }});
    }}

    statusEl.textContent = `Billboards ready (${{heatSources.length}}).`;
  }}

  async function rebuildHeatmap() {{
    const mode = modeEl.value;
    if (mode === 'billboards') {{
      await buildBillboardsHeatmap();
    }} else {{
      await buildColumnsHeatmap();
    }}
    viewer.scene.requestRender();
  }}

  function setCutaway(enabled, heightM) {{
    if (!tileset) return;

    if (!cutawayPlanes) {{
      // Create once, then toggle.
      // We'll place the plane in an ENU frame near the requested center.
      // We clamp the center to the mesh when possible, otherwise the ellipsoid surface.
      const centerCartesian = Cesium.Cartesian3.fromDegrees({center_lon}, {center_lat}, 0);
      cutawayPlanes = new Cesium.ClippingPlaneCollection({{
        planes: [ new Cesium.ClippingPlane(new Cesium.Cartesian3(0.0, 0.0, -1.0), 50.0) ],
        enabled: true,
        edgeWidth: 1.0,
        edgeColor: Cesium.Color.WHITE,
        modelMatrix: Cesium.Transforms.eastNorthUpToFixedFrame(centerCartesian)
      }});
      tileset.clippingPlanes = cutawayPlanes;
    }}

    cutawayPlanes.enabled = enabled;
    // normal (0,0,-1) + distance = heightM clips everything with z > heightM in the local ENU frame
    cutawayPlanes.get(0).distance = heightM;
  }}

  function wireUI() {{
    function refreshCutawayLabel() {{
      const h = parseFloat(cutawayHeightEl.value);
      cutawayValueEl.textContent = cutawayEnabledEl.checked ? `${{h.toFixed(0)}} m` : 'off';
    }}

    rerenderBtn.addEventListener('click', () => rebuildHeatmap());
    modeEl.addEventListener('change', () => rebuildHeatmap());
    maxHeightEl.addEventListener('change', () => rebuildHeatmap());
    radiusEl.addEventListener('change', () => rebuildHeatmap());
    subDepthEl.addEventListener('change', () => rebuildHeatmap());

    cutawayEnabledEl.addEventListener('change', () => {{
      refreshCutawayLabel();
      setCutaway(cutawayEnabledEl.checked, parseFloat(cutawayHeightEl.value));
      viewer.scene.requestRender();
    }});
    cutawayHeightEl.addEventListener('input', () => {{
      refreshCutawayLabel();
      setCutaway(cutawayEnabledEl.checked, parseFloat(cutawayHeightEl.value));
      viewer.scene.requestRender();
    }});

    refreshCutawayLabel();
  }}

  function wirePicking() {{
    const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
    handler.setInputAction((movement) => {{
      pickEl.textContent = '';
      try {{
        if (!viewer.scene.pickPositionSupported) return;
        const cart = viewer.scene.pickPosition(movement.position);
        if (!Cesium.defined(cart)) return;
        const c = Cesium.Cartographic.fromCartesian(cart);
        pickEl.innerHTML = `Pick: <code>${{Cesium.Math.toDegrees(c.latitude).toFixed(6)}}, ${{Cesium.Math.toDegrees(c.longitude).toFixed(6)}}, h=${{c.height.toFixed(1)}}m</code>`;
      }} catch (e) {{
        // ignore
      }}
    }}, Cesium.ScreenSpaceEventType.LEFT_CLICK);
  }}

  async function main() {{
    try {{
      wireUI();
      wirePicking();

      statusEl.textContent = 'Requesting tileset…';
      tileset = await Cesium.Cesium3DTileset.fromUrl(tilesetUrl, {{ showCreditsOnScreen: true }});
      viewer.scene.primitives.add(tileset);

      viewer.camera.setView({{
        destination: Cesium.Cartesian3.fromDegrees({center_lon}, {center_lat}, 650),
        orientation: {{
          heading: Cesium.Math.toRadians(0),
          pitch: Cesium.Math.toRadians(-45),
          roll: 0.0
        }}
      }});

      // Cesium 1.111: use `loadProgress` (NOT tileLoadProgressEvent)
      tileset.loadProgress.addEventListener(async (pending, processing) => {{
        if (pending === 0 && processing === 0) {{
          statusEl.textContent = 'Tiles loaded. Building heatmap…';
          await rebuildHeatmap();
          setCutaway(cutawayEnabledEl.checked, parseFloat(cutawayHeightEl.value));
        }}
      }});

      statusEl.textContent = 'Loading tiles…';
    }} catch (error) {{
      console.error('Error loading 3D Tiles:', error);
      statusEl.textContent = 'Error: ' + (error.message || error);
      alert('Error loading 3D Tiles: ' + (error.message || error));
    }}
  }}

  main();
}})();
</script>
</body>
</html>"""

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    logger.info(f"3D map HTML saved to {output_path}")
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Google Maps 3D Tiles test with 3D heatmap overlay")
    parser.add_argument("--api-key", required=True, help="Google Maps Platform API key (Map Tiles API enabled)")
    parser.add_argument("--lat", type=float, default=37.7749, help="Center latitude (default: San Francisco)")
    parser.add_argument("--lon", type=float, default=-122.4194, help="Center longitude (default: San Francisco)")
    parser.add_argument("--output", default="./test_3d_simple.html", help="Output HTML file")
    parser.add_argument("--cache-dir", default="./cache/google_maps_3d", help="Cache directory")
    parser.add_argument("--no-cache", action="store_true", help="Disable cache")
    parser.add_argument("--points", type=int, default=50, help="Number of sample heat points")

    parser.add_argument("--mode", choices=["columns", "billboards"], default="columns", help="Heatmap render mode")
    parser.add_argument("--max-height-m", type=float, default=80.0, help="Max above-surface height for columns (m)")
    parser.add_argument("--radius-m", type=float, default=12.0, help="Radius for columns (m)")
    parser.add_argument("--subsurface-depth-m", type=float, default=30.0, help="Depth below surface for columns (m)")
    parser.add_argument("--no-cutaway", action="store_true", help="Disable cutaway clipping plane UI (default enabled)")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("Google Maps 3D Tiles + 3D Heatmap")
    logger.info("=" * 60)
    logger.info(f"Center: ({args.lat}, {args.lon})")
    logger.info(f"Mode: {args.mode}")
    logger.info(f"Points: {args.points}")

    try:
        # Verify API key works (and warm cache)
        _ = fetch_tileset_with_cache(args.api_key, args.cache_dir, use_cache=not args.no_cache)
        logger.info("✓ Root tileset fetch OK")

        heatmap_data = create_sample_heatmap_data(args.lat, args.lon, args.points)
        logger.info(f"✓ Created {len(heatmap_data)} sample heat points")

        create_simple_3d_map_html(
            api_key=args.api_key,
            center_lat=args.lat,
            center_lon=args.lon,
            heatmap_data=heatmap_data,
            output_path=args.output,
            heatmap_mode=args.mode,
            max_height_m=args.max_height_m,
            radius_m=args.radius_m,
            subsurface_depth_m=args.subsurface_depth_m,
            cutaway_enabled=(not args.no_cutaway),
        )

        logger.info("=" * 60)
        logger.info("HTML generated successfully.")
        logger.info("Run via HTTP (NOT file://), e.g.:")
        logger.info("  python3 -m http.server 8000")
        logger.info(f"  Then open: http://localhost:8000/{Path(args.output).name}")
        logger.info("=" * 60)
        return 0
    except Exception as e:
        logger.error(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
