#!/usr/bin/env python3
"""Standalone 3D demo: Google Photorealistic 3D Tiles ONLY (no OSM)

Features
- Loads Google Photorealistic 3D Tiles in CesiumJS.
- Left-click to place a TX point.
- Draw a *circular* horizontal plane centered at TX.
- The plane radius expands until it "collides" with the Google mesh, then stops.

Collision definition (runtime test, not rendering):
- We cast horizontal rays in many directions from the TX plane height.
- For each ray we find the first intersection with the 3D tiles mesh using Cesium
  pickFromRayMostDetailed(). The smallest hit distance across all rays becomes the
  circle radius. If nothing hits within max-range, radius=max-range.

Height control
- Enable "Height mode" to make clicks raise TX height instead of moving TX.
- TX height is an offset (meters) above the clamped surface at the TX location.
  Raising height can allow the plane to pass over low obstacles (larger radius).

Run:
  python3 scripts/test_google_maps_3d_tiles_only.py --api-key YOUR_KEY --lat 37.7749 --lon -122.4194 --output ./tiles_only.html

Serve:
  python3 -m http.server 8000
  open http://localhost:8000/tiles_only.html
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def create_tiles_only_html(
    api_key: str,
    center_lat: float,
    center_lon: float,
    output_path: str,
    max_range_m: float = 500.0,
    num_rays: int = 96,
    show_google_tiles: bool = True,
) -> str:
    tileset_url = f"https://tile.googleapis.com/v1/3dtiles/root.json?key={api_key}"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Google 3D Tiles Only — Plane Stops on Mesh</title>
  <script src="https://cesium.com/downloads/cesiumjs/releases/1.111/Build/Cesium/Cesium.js"></script>
  <link href="https://cesium.com/downloads/cesiumjs/releases/1.111/Build/Cesium/Widgets/widgets.css" rel="stylesheet" />
  <style>
    html, body, #cesiumContainer {{ width: 100%; height: 100%; margin: 0; padding: 0; overflow: hidden; }}
    .panel {{
      position: absolute; background: rgba(30,30,30,0.90); color: #fff;
      padding: 10px 12px; border-radius: 8px; font-family: Arial, sans-serif;
      font-size: 12px; z-index: 10; max-width: 420px;
    }}
    #info {{ top: 10px; left: 10px; }}
    #controls {{ top: 10px; right: 10px; }}
    .row {{ display:flex; align-items:center; justify-content:space-between; gap:10px; margin: 6px 0; }}
    input[type=range] {{ width: 200px; }}
    .small {{ opacity: 0.85; font-size: 11px; }}
    .badge {{ display:inline-block; padding: 2px 6px; border-radius: 999px; background: rgba(255,255,255,0.12); }}
  </style>
</head>
<body>
  <div id="cesiumContainer"></div>

  <div id="info" class="panel">
    <div style="font-weight:700; font-size:13px;">Google 3D Tiles Only</div>
    <div class="small">Center: ({center_lat:.6f}, {center_lon:.6f})</div>
    <div id="status" class="small" style="margin-top:6px;">Loading…</div>
    <div class="small" style="margin-top:6px;">
      TX: <span class="badge" id="txInfo">unset</span>
      Height: <span class="badge" id="hInfo">0</span> m
      Radius: <span class="badge" id="rInfo">0</span> m
    </div>
  </div>

  <div id="controls" class="panel">
    <div style="font-weight:700; font-size:13px;">Controls</div>
    <div class="row"><label><input id="toggleGoogle" type="checkbox" {('checked' if show_google_tiles else '')}/> Google 3D Tiles</label></div>
    <div class="row"><label><input id="togglePlane" type="checkbox" checked /> Plane</label></div>
    <div class="row"><label><input id="toggleClick" type="checkbox" checked /> Click enabled</label></div>
    <div class="row"><label><input id="toggleHeightMode" type="checkbox" /> Height mode (click raises)</label></div>

    <div class="row">
      <label for="txHeight">TX height offset (m)</label>
      <input id="txHeight" type="range" min="0" max="200" step="1" value="10" />
    </div>
    <div class="row">
      <label for="heightStep">Click raise step (m)</label>
      <input id="heightStep" type="range" min="1" max="50" step="1" value="5" />
    </div>

    <div class="small">
      Collision rule: the circle radius stops at the *first* hit against the Google mesh (closest hit among rays).
      If tiles are still streaming, results may change as more detail loads—click again to recompute.
    </div>
  </div>

  <script>
    const tilesetUrl = {json.dumps(tileset_url)};
    const CENTER_LON = {center_lon};
    const CENTER_LAT = {center_lat};

    const MAX_RANGE_M = {float(max_range_m)};
    const NUM_RAYS = {int(num_rays)};

    const statusEl = document.getElementById('status');
    const txInfoEl = document.getElementById('txInfo');
    const hInfoEl = document.getElementById('hInfo');
    const rInfoEl = document.getElementById('rInfo');

    function clamp(v, lo, hi) {{ return Math.max(lo, Math.min(hi, v)); }}

    // Cesium viewer (no default globe)
    const viewer = new Cesium.Viewer('cesiumContainer', {{
      imageryProvider: false,
      baseLayerPicker: false,
      geocoder: false,
      timeline: false,
      animation: false,
      sceneModePicker: true,
      navigationHelpButton: true,
      homeButton: true,
      globe: false,
    }});

    // Recommended for tile.googleapis.com concurrency
    Cesium.RequestScheduler.requestsByServer['tile.googleapis.com:443'] = 18;

    let googleTileset = null;

    // TX state
    let _txLon = CENTER_LON;
    let _txLat = CENTER_LAT;
    let _txBaseH = 0.0;     // clamped mesh height at TX
    let _txHeightM = 10.0;  // user offset above _txBaseH

    // Entities
    let _txMarker = null;
    let _plane = null;
    let _planeOutline = null;

    // Debounce heavy recompute work
    let _busy = false;
    let _pending = null;

    function flyToCenter() {{
      viewer.camera.setView({{
        destination: Cesium.Cartesian3.fromDegrees(CENTER_LON, CENTER_LAT, 1300),
        orientation: {{ heading: 0.0, pitch: Cesium.Math.toRadians(-40), roll: 0.0 }}
      }});
    }}

    async function loadGoogleTiles() {{
      statusEl.textContent = 'Loading Google photorealistic tiles…';
      googleTileset = await Cesium.Cesium3DTileset.fromUrl(tilesetUrl, {{ showCreditsOnScreen: true }});
      viewer.scene.primitives.add(googleTileset);
      googleTileset.loadProgress.addEventListener(function(pending, processing) {{
        if (pending === 0 && processing === 0) {{
          statusEl.textContent = 'Tiles loaded. Click to place TX.';
        }}
      }});
      googleTileset.show = document.getElementById('toggleGoogle').checked;
      return googleTileset;
    }}

    async function clampHeightAt(lon, lat, aboveHeightM) {{
      // Clamp against 3D Tiles mesh (photorealistic surface).
      const probe = Cesium.Cartesian3.fromDegrees(lon, lat, (aboveHeightM || 300.0));
      try {{
        const clamped = await viewer.scene.clampToHeightMostDetailed([probe]);
        if (clamped && clamped[0]) {{
          const carto = Cesium.Cartographic.fromCartesian(clamped[0]);
          return carto.height;
        }}
      }} catch (e) {{
        // ignore
      }}
      return null;
    }}

    function enuDirectionAt(txCart, angleRad) {{
      // Local ENU direction (horizontal)
      const localDir = new Cesium.Cartesian3(Math.cos(angleRad), Math.sin(angleRad), 0.0);
      const enu = Cesium.Transforms.eastNorthUpToFixedFrame(txCart);
      const worldDir = Cesium.Matrix4.multiplyByPointAsVector(enu, localDir, new Cesium.Cartesian3());
      return Cesium.Cartesian3.normalize(worldDir, worldDir);
    }}

    async function pickDistanceToMesh(originCart, worldDir, maxDist) {{
      // Hide overlays to avoid self-hits
      if (_plane) _plane.show = false;
      if (_planeOutline) _planeOutline.show = false;

      const ray = new Cesium.Ray(originCart, worldDir);
      try {{
        const hit = await viewer.scene.pickFromRayMostDetailed(ray);
        if (Cesium.defined(hit) && Cesium.defined(hit.position)) {{
          const d = Cesium.Cartesian3.distance(originCart, hit.position);
          if (isFinite(d) && d > 0.5) {{
            return Math.min(d, maxDist);
          }}
        }}
      }} catch (e) {{
        // ignore
      }} finally {{
        // Restore visibility (caller may still toggle off later)
        if (_plane) _plane.show = document.getElementById('togglePlane').checked;
        if (_planeOutline) _planeOutline.show = document.getElementById('togglePlane').checked;
      }}
      return maxDist;
    }}

    async function computeStopRadius(txLon, txLat, planeHeightM) {{
      // Cast NUM_RAYS horizontal rays; take the minimum intersection distance.
      const origin = Cesium.Cartesian3.fromDegrees(txLon, txLat, planeHeightM);

      // Avoid doing expensive ray picks before the tileset exists.
      if (!googleTileset) return MAX_RANGE_M;

      statusEl.textContent = 'Computing stop radius (ray hits)…';

      // Limit concurrent ray-picks to reduce spikes.
      const BATCH = 10;
      let best = MAX_RANGE_M;

      for (let i = 0; i < NUM_RAYS; i += BATCH) {{
        const promises = [];
        const end = Math.min(NUM_RAYS, i + BATCH);
        for (let k = i; k < end; k++) {{
          const ang = (k / NUM_RAYS) * Math.PI * 2.0;
          const dir = enuDirectionAt(origin, ang);
          promises.push(pickDistanceToMesh(origin, dir, MAX_RANGE_M));
        }}
        const ds = await Promise.all(promises);
        for (const d of ds) {{
          if (d < best) best = d;
        }}
        // Early exit if we already hit something very close.
        if (best < 5.0) break;
      }}

      // Keep a small safety margin so the circle doesn't slightly penetrate the mesh due to precision.
      best = Math.max(0.0, best - 1.0);
      return best;
    }}

    function ensurePlaneEntities() {{
      if (!_plane) {{
        _plane = viewer.entities.add({{
          name: 'plane',
          polygon: {{
            hierarchy: Cesium.Cartesian3.fromDegreesArray([CENTER_LON, CENTER_LAT, CENTER_LON, CENTER_LAT, CENTER_LON, CENTER_LAT]),
            height: 0.0,
            material: new Cesium.Color(0.95, 0.85, 0.10, 0.28),
            outline: false,
          }}
        }});
      }}
      if (!_planeOutline) {{
        _planeOutline = viewer.entities.add({{
          name: 'plane_outline',
          polyline: {{
            positions: Cesium.Cartesian3.fromDegreesArray([CENTER_LON, CENTER_LAT, CENTER_LON, CENTER_LAT]),
            width: 2,
            material: Cesium.Color.CYAN,
            clampToGround: false,
            disableDepthTestDistance: Number.POSITIVE_INFINITY
          }}
        }});
      }}
    }}

    function ensureTxMarker(pos) {{
      if (!_txMarker) {{
        _txMarker = viewer.entities.add({{
          name: 'tx',
          point: {{
            pixelSize: 9,
            color: Cesium.Color.YELLOW,
            outlineColor: Cesium.Color.BLACK,
            outlineWidth: 2,
            disableDepthTestDistance: Number.POSITIVE_INFINITY
          }},
          position: pos
        }});
      }} else {{
        _txMarker.position = pos;
      }}
    }}

    function circleDegrees(txLon, txLat, radiusM) {{
      const pts = [];
      const metersPerDegLat = 111320.0;
      const cosLat = Math.cos(Cesium.Math.toRadians(txLat));
      const metersPerDegLon = metersPerDegLat * Math.max(0.1, cosLat);

      for (let i = 0; i < NUM_RAYS; i++) {{
        const ang = (i / NUM_RAYS) * Math.PI * 2.0;
        const east = Math.cos(ang) * radiusM;
        const north = Math.sin(ang) * radiusM;
        const lon = txLon + (east / metersPerDegLon);
        const lat = txLat + (north / metersPerDegLat);
        pts.push(lon, lat);
      }}
      return pts;
    }}

    async function recomputePlane() {{
      ensurePlaneEntities();

      // Read UI
      const txHeightEl = document.getElementById('txHeight');
      _txHeightM = parseFloat(txHeightEl.value || '0');
      hInfoEl.textContent = String(Math.round(_txHeightM));

      txInfoEl.textContent = _txLon.toFixed(6) + ',' + _txLat.toFixed(6);

      // Base mesh height at TX (may be roof if TX is inside a building footprint)
      const baseH = await clampHeightAt(_txLon, _txLat, 500.0);
      _txBaseH = (baseH != null) ? baseH : 0.0;

      const planeH = _txBaseH + _txHeightM;

      // TX marker
      const markerPos = Cesium.Cartesian3.fromDegrees(_txLon, _txLat, planeH + 2.0);
      ensureTxMarker(markerPos);

      // Collision radius
      const radius = await computeStopRadius(_txLon, _txLat, planeH);
      rInfoEl.textContent = String(Math.round(radius));

      // Geometry update
      const degs = circleDegrees(_txLon, _txLat, radius);

      _plane.polygon.height = planeH;
      _plane.polygon.hierarchy = Cesium.Cartesian3.fromDegreesArray(degs);

      const degsClosed = degs.slice();
      degsClosed.push(degs[0], degs[1]);
      _planeOutline.polyline.positions = Cesium.Cartesian3.fromDegreesArray(degsClosed);

      const showPlane = document.getElementById('togglePlane').checked;
      _plane.show = showPlane;
      _planeOutline.show = showPlane;

      statusEl.textContent = 'Ready.';
    }}

    async function requestRecompute() {{
      _pending = true;
      if (_busy) return;
      _busy = true;
      while (_pending) {{
        _pending = null;
        await recomputePlane();
      }}
      _busy = false;
    }}

    function setupUI() {{
      document.getElementById('toggleGoogle').addEventListener('change', function() {{
        if (!googleTileset) return;
        googleTileset.show = this.checked;
      }});

      document.getElementById('togglePlane').addEventListener('change', function() {{
        const on = this.checked;
        if (_plane) _plane.show = on;
        if (_planeOutline) _planeOutline.show = on;
      }});

      document.getElementById('txHeight').addEventListener('input', function() {{
        // Debounced recompute (height changes trigger re-raycast)
        requestRecompute();
      }});

      document.getElementById('toggleHeightMode').addEventListener('change', function() {{
        if (this.checked) {{
          document.getElementById('toggleClick').checked = true;
        }}
      }});
    }}

    function setupClickHandler() {{
      const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
      handler.setInputAction(function(click) {{
        if (!document.getElementById('toggleClick').checked) return;

        let cartesian = null;
        if (viewer.scene.pickPositionSupported) {{
          cartesian = viewer.scene.pickPosition(click.position);
        }}
        if (!Cesium.defined(cartesian)) {{
          cartesian = viewer.camera.pickEllipsoid(click.position, Cesium.Ellipsoid.WGS84);
        }}
        if (!Cesium.defined(cartesian)) return;

        const carto = Cesium.Cartographic.fromCartesian(cartesian);
        const lon = Cesium.Math.toDegrees(carto.longitude);
        const lat = Cesium.Math.toDegrees(carto.latitude);

        const heightMode = document.getElementById('toggleHeightMode').checked;
        if (heightMode) {{
          const step = parseFloat(document.getElementById('heightStep').value || '1');
          const hEl = document.getElementById('txHeight');
          const cur = parseFloat(hEl.value || '0');
          const next = clamp(cur + step, parseFloat(hEl.min || '0'), parseFloat(hEl.max || '200'));
          hEl.value = String(next);
          _txHeightM = next;
          requestRecompute();
          return;
        }}

        _txLon = lon;
        _txLat = lat;
        requestRecompute();
      }}, Cesium.ScreenSpaceEventType.LEFT_CLICK);
    }}

    (async function main() {{
      flyToCenter();
      setupUI();
      setupClickHandler();

      await loadGoogleTiles();

      // Build once after tiles start streaming.
      setTimeout(function() {{
        requestRecompute();
      }}, 900);
    }})();
  </script>
</body>
</html>
"""
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return str(out_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Google 3D Tiles only: circular plane stops on mesh collision")
    parser.add_argument("--api-key", required=True, help="Google Maps Platform API key (Map Tiles API enabled)")
    parser.add_argument("--lat", type=float, default=37.7749, help="Center latitude")
    parser.add_argument("--lon", type=float, default=-122.4194, help="Center longitude")
    parser.add_argument("--max-range-m", type=float, default=500.0, help="Max plane radius in meters")
    parser.add_argument("--num-rays", type=int, default=96, help="Number of rays for collision search")
    parser.add_argument("--output", default="./tiles_only.html", help="Output HTML")
    parser.add_argument("--no-google", action="store_true", help="Start with Google tiles hidden")
    args = parser.parse_args()

    logger.info("Generating tiles-only HTML (no OSM)…")
    out = create_tiles_only_html(
        api_key=args.api_key,
        center_lat=args.lat,
        center_lon=args.lon,
        output_path=args.output,
        max_range_m=float(args.max_range_m),
        num_rays=int(args.num_rays),
        show_google_tiles=not args.no_google,
    )
    logger.info("✓ HTML generated: %s", out)
    logger.info("Serve with: python3 -m http.server 8000")
    logger.info("Open: http://localhost:8000/%s", Path(out).name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
