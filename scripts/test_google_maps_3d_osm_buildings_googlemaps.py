#!/usr/bin/env python3
"""Standalone 3D demo: Google Mesh RF interaction

This is intentionally NOT integrated into the main app/UI.

Pipeline:
  - Load Google Photorealistic 3D Tiles (background)
  - Generate an HTML file that:
      * loads Google Photorealistic 3D Tiles (background)
      * creates a circular RF plane from TX point
      * plane interacts with Google 3D mesh (stops at buildings)

Run:
  python3 scripts/test_google_maps_3d_osm_buildings_googlemaps.py \
    --api-key YOUR_KEY \
    --lat 37.7749 --lon -122.4194 \
    --max-range-m 500 --step-m 5 \
    --freq-mhz 3500 --tx-power-dbm 43 \
    --output ./google_mesh_rf_3d.html

Serve:
  python3 -m http.server 8000
  open http://localhost:8000/google_mesh_rf_3d.html
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

# Allow running as a standalone script from repo root.
import sys

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# OSM imports removed - using Google mesh only


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# -------------------------
# Google tileset caching
# -------------------------


class GoogleMaps3DTilesCache:
    """Cache manager for Google Maps 3D Tiles root.json to reduce API churn."""

    def __init__(self, cache_dir: str = "./cache/google_maps_3d"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # Session tokens are multi-hour; keep cache aligned.
        self.cache_ttl_hours = 3

    def _get_cache_key(self, api_key: str) -> str:
        return hashlib.md5(api_key.encode()).hexdigest()

    def _get_cache_path(self, cache_key: str) -> Path:
        return self.cache_dir / f"tileset_{cache_key}.json"

    def get_cached_tileset(self, api_key: str) -> Optional[Dict[str, Any]]:
        cache_key = self._get_cache_key(api_key)
        cache_path = self._get_cache_path(cache_key)
        if not cache_path.exists():
            return None
        try:
            cache_data = json.loads(cache_path.read_text(encoding="utf-8"))
            cached_time = datetime.fromisoformat(cache_data["timestamp"])
            if datetime.now() - cached_time > timedelta(hours=self.cache_ttl_hours):
                return None
            logger.info(f"Using cached tileset (age: {datetime.now() - cached_time})")
            return cache_data["tileset"]
        except Exception as e:
            logger.warning(f"Error reading Google tileset cache: {e}")
            return None

    def cache_tileset(self, api_key: str, tileset_data: Dict[str, Any]) -> None:
        cache_key = self._get_cache_key(api_key)
        cache_path = self._get_cache_path(cache_key)
        try:
            cache_data = {"timestamp": datetime.now().isoformat(), "tileset": tileset_data}
            cache_path.write_text(json.dumps(cache_data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Error writing Google tileset cache: {e}")


def fetch_tileset_with_cache(api_key: str, cache_dir: str, use_cache: bool) -> Dict[str, Any]:
    cache = GoogleMaps3DTilesCache(cache_dir)
    if use_cache:
        cached = cache.get_cached_tileset(api_key)
        if cached:
            return cached

    url = f"https://tile.googleapis.com/v1/3dtiles/root.json?key={api_key}"
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    logger.info("Fetching Google 3D tileset root.json…")
    r = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    tileset_data = r.json()
    if use_cache:
        cache.cache_tileset(api_key, tileset_data)
    return tileset_data


# OSM geometry helpers removed - using Google mesh only


# -------------------------
# HTML generation
# -------------------------


# OSM BuildingFeature and ForestFeature dataclasses removed - using Google mesh only


def _color_ramp_js() -> str:
    """Return JS helpers for RSRP -> color."""
    # Avoid template literals to prevent f-string brace issues.
    return r"""
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

// Map RSRP dBm [-120..-60] to a blue->red ramp.
function colorForRsrp(rsrpDbm, alpha) {
  // normalize: -120 -> 0, -60 -> 1
  const t = clamp((rsrpDbm + 120.0) / 60.0, 0.0, 1.0);
  // simple HSV-ish ramp: blue(240deg) to red(0deg)
  const hue = (1.0 - t) * 240.0;
  const c = Cesium.Color.fromHsl(hue / 360.0, 1.0, 0.5, alpha);
  return c;
}
"""

def _interactive_click_js() -> str:
    """JS block: click-to-set TX and reproject heat (uses OSM polygons for interaction)."""
    return r"""
// === Interactive TX click + 3D heat projection (Milestone A - interactive) ===

function metersPerDegLat() { return 111320.0; }
function metersPerDegLon(latDeg) { return 111320.0 * Math.cos(latDeg * Math.PI / 180.0); }

function lonLatToOffsetMeters(originLon, originLat, lon, lat) {
  const dx = (lon - originLon) * metersPerDegLon(originLat);
  const dy = (lat - originLat) * metersPerDegLat();
  return [dx, dy];
}

function offsetMetersToLonLat(originLon, originLat, dx, dy) {
  const lon = originLon + dx / metersPerDegLon(originLat);
  const lat = originLat + dy / metersPerDegLat();
  return [lon, lat];
}

function haversineMeters(lat1, lon1, lat2, lon2) {
  const R = 6371000.0;
  const dLat = (lat2 - lat1) * Math.PI / 180.0;
  const dLon = (lon2 - lon1) * Math.PI / 180.0;
  const a = Math.sin(dLat/2.0)*Math.sin(dLat/2.0) +
            Math.cos(lat1*Math.PI/180.0)*Math.cos(lat2*Math.PI/180.0)*
            Math.sin(dLon/2.0)*Math.sin(dLon/2.0);
  const c = 2.0 * Math.atan2(Math.sqrt(a), Math.sqrt(1.0 - a));
  return R * c;
}

function fsplDb(distM, freqMHz) {
  const dKm = Math.max(0.001, distM / 1000.0); // >= 1m
  return 32.44 + 20.0 * Math.log10(freqMHz) + 20.0 * Math.log10(dKm);
}

function _orient(ax, ay, bx, by, cx, cy) {
  // cross((b-a),(c-a))
  return (bx-ax)*(cy-ay) - (by-ay)*(cx-ax);
}

function _onSeg(ax, ay, bx, by, cx, cy) {
  return Math.min(ax, bx) <= cx + 1e-12 && cx <= Math.max(ax, bx) + 1e-12 &&
         Math.min(ay, by) <= cy + 1e-12 && cy <= Math.max(ay, by) + 1e-12;
}

function _segIntersects(ax, ay, bx, by, cx, cy, dx, dy) {
  const o1 = _orient(ax, ay, bx, by, cx, cy);
  const o2 = _orient(ax, ay, bx, by, dx, dy);
  const o3 = _orient(cx, cy, dx, dy, ax, ay);
  const o4 = _orient(cx, cy, dx, dy, bx, by);

  if ((o1 > 0 && o2 < 0 || o1 < 0 && o2 > 0) && (o3 > 0 && o4 < 0 || o3 < 0 && o4 > 0)) return true;

  if (Math.abs(o1) < 1e-12 && _onSeg(ax, ay, bx, by, cx, cy)) return true;
  if (Math.abs(o2) < 1e-12 && _onSeg(ax, ay, bx, by, dx, dy)) return true;
  if (Math.abs(o3) < 1e-12 && _onSeg(cx, cy, dx, dy, ax, ay)) return true;
  if (Math.abs(o4) < 1e-12 && _onSeg(cx, cy, dx, dy, bx, by)) return true;
  return false;
}

function segmentIntersectsPolygon(ax, ay, bx, by, verts) {
  const v = _dropDuplicateClosingVertex(verts);
  if (!v || v.length < 3) return false;

  // If endpoint is inside polygon, count as intersection.
  if (_pointInPoly(ax, ay, v) || _pointInPoly(bx, by, v)) return true;

  for (let i = 0; i < v.length; i++) {
    const j = (i + 1) % v.length;
    const cx = v[i][0], cy = v[i][1];
    const dx = v[j][0], dy = v[j][1];
    if (_segIntersects(ax, ay, bx, by, cx, cy, dx, dy)) return true;
  }
  return false;
}

function bboxOfVerts(verts) {
  const v = _dropDuplicateClosingVertex(verts);
  let minLon = Infinity, minLat = Infinity, maxLon = -Infinity, maxLat = -Infinity;
  for (let i = 0; i < v.length; i++) {
    const lon = v[i][0], lat = v[i][1];
    if (lon < minLon) minLon = lon;
    if (lat < minLat) minLat = lat;
    if (lon > maxLon) maxLon = lon;
    if (lat > maxLat) maxLat = lat;
  }
  return [minLon, minLat, maxLon, maxLat];
}

function bboxOverlap(a, b) {
  // [minLon,minLat,maxLon,maxLat]
  return !(a[2] < b[0] || a[0] > b[2] || a[3] < b[1] || a[1] > b[3]);
}

function makeSpatialIndex(features, cellLonDeg, cellLatDeg) {
  // Simple spatial hash on feature bbox coverage. Good enough for ~1k polys.
  // Returns {cellLonDeg, cellLatDeg, buckets: Map<string, int[]>, bboxes: Array<[..]>}
  const buckets = new Map();
  const bboxes = [];
  let globalMinLon = Infinity, globalMinLat = Infinity;

  for (let i = 0; i < features.length; i++) {
    const bb = bboxOfVerts(features[i].verts);
    bboxes.push(bb);
    if (bb[0] < globalMinLon) globalMinLon = bb[0];
    if (bb[1] < globalMinLat) globalMinLat = bb[1];
  }

  function key(ix, iy) { return ix + "," + iy; }

  for (let i = 0; i < features.length; i++) {
    const bb = bboxes[i];
    const ix0 = Math.floor((bb[0] - globalMinLon) / cellLonDeg);
    const ix1 = Math.floor((bb[2] - globalMinLon) / cellLonDeg);
    const iy0 = Math.floor((bb[1] - globalMinLat) / cellLatDeg);
    const iy1 = Math.floor((bb[3] - globalMinLat) / cellLatDeg);

    for (let ix = ix0; ix <= ix1; ix++) {
      for (let iy = iy0; iy <= iy1; iy++) {
        const k = key(ix, iy);
        const arr = buckets.get(k);
        if (arr) arr.push(i);
        else buckets.set(k, [i]);
      }
    }
  }

  return { cellLonDeg: cellLonDeg, cellLatDeg: cellLatDeg, buckets: buckets, bboxes: bboxes, minLon: globalMinLon, minLat: globalMinLat };
}

function querySpatial(index, rayBbox) {
  const out = new Set();
  const ix0 = Math.floor((rayBbox[0] - index.minLon) / index.cellLonDeg);
  const ix1 = Math.floor((rayBbox[2] - index.minLon) / index.cellLonDeg);
  const iy0 = Math.floor((rayBbox[1] - index.minLat) / index.cellLatDeg);
  const iy1 = Math.floor((rayBbox[3] - index.minLat) / index.cellLatDeg);
  for (let ix = ix0; ix <= ix1; ix++) {
    for (let iy = iy0; iy <= iy1; iy++) {
      const k = ix + "," + iy;
      const arr = index.buckets.get(k);
      if (!arr) continue;
      for (let t = 0; t < arr.length; t++) out.add(arr[t]);
    }
  }
  return out;
}

// OSM building/forest indices removed - using Google mesh instead

// Precompute RX sample offsets relative to initial center, so we can reuse the same pattern around any TX.
const _gridOffsets = (function() {
  const out = [];
  for (let i = 0; i < grid.length; i++) {
    const o = lonLatToOffsetMeters(CENTER_LON, CENTER_LAT, grid[i].lon, grid[i].lat);
    out.push(o);
  }
  return out;
})();

// OSM obstruction counting removed - using Google mesh for plane interaction only
// No need for computeRsrpDbm with OSM buildings since we're not rendering them

let _heatPoints = null;
let _heatBuilt = false;
let _heatPrims = [];
let _heatLonLat = [];
let _txMarker = null;
let _txSphere = null;
let _txPlaneOutline = null;
let _txLon = CENTER_LON;
let _txLat = CENTER_LAT;
let _recomputeBusy = false;
let _pendingTx = null;

// Allow clicks while overlays are still building.
// If build is in progress, we queue the most recent TX and apply it once overlays finish.
window.__buildBusy = false;
window.__queuedTxLonLat = null;

async function buildPlaneWithGoogleMeshInteraction(txLon, txLat, planeHeight) {
  // Create a plane that expands from TX point, stops when it hits Google mesh
  const numPoints = 128; // Points around the circle
  const metersPerDegLat = 111320.0;
  const cosLat = Math.cos(Cesium.Math.toRadians(txLat));
  const metersPerDegLon = metersPerDegLat * Math.max(0.1, cosLat);
  
  const wavePoints = [];
  
  console.log(`[TX Plane] Checking Google 3D mesh for plane expansion`);
  
  // Create boundary points at full range
  const testPoints = [];
  const directions = [];
  for (let i = 0; i < numPoints; i++) {
    const angle = (i / numPoints) * Math.PI * 2.0;
    const dirX = Math.cos(angle);
    const dirY = Math.sin(angle);
    const testLon = txLon + (dirX * MAX_RANGE_M / metersPerDegLon);
    const testLat = txLat + (dirY * MAX_RANGE_M / metersPerDegLat);
    const testPos = Cesium.Cartesian3.fromDegrees(testLon, testLat, planeHeight + 100.0);
    testPoints.push(testPos);
    directions.push({ dirX, dirY });
  }
  
  // Batch clamp all boundary points
  let clampedResults = [];
  try {
    const batchSize = 50;
    for (let i = 0; i < testPoints.length; i += batchSize) {
      const batch = testPoints.slice(i, i + batchSize);
      const clamped = await viewer.scene.clampToHeightMostDetailed(batch);
      if (clamped) {
        clampedResults.push(...clamped);
      } else {
        clampedResults.push(...new Array(batch.length).fill(null));
      }
    }
  } catch (e) {
    console.warn('[TX Plane] clampToHeightMostDetailed failed, using full circle');
    clampedResults = new Array(testPoints.length).fill(null);
  }
  
  // For each boundary point, check if mesh blocks expansion
  for (let i = 0; i < numPoints; i++) {
    const { dirX, dirY } = directions[i];
    let finalDist = MAX_RANGE_M;
    
    const clamped = clampedResults[i];
    if (clamped) {
      try {
        const carto = Cesium.Cartographic.fromCartesian(clamped);
        const meshHeight = carto.height;
        
        // If mesh height is above plane height, plane stops expanding at this point
        if (meshHeight > planeHeight + 2.0) {
          // Find stopping distance by checking points closer to TX
          const checkDistances = [MAX_RANGE_M * 0.9, MAX_RANGE_M * 0.8, MAX_RANGE_M * 0.7, MAX_RANGE_M * 0.6, MAX_RANGE_M * 0.5, MAX_RANGE_M * 0.4, MAX_RANGE_M * 0.3, MAX_RANGE_M * 0.2, MAX_RANGE_M * 0.1];
          
          for (const checkDist of checkDistances) {
            const checkLon = txLon + (dirX * checkDist / metersPerDegLon);
            const checkLat = txLat + (dirY * checkDist / metersPerDegLat);
            const checkPos = Cesium.Cartesian3.fromDegrees(checkLon, checkLat, planeHeight + 100.0);
            
            try {
              const checkClamped = await viewer.scene.clampToHeightMostDetailed([checkPos]);
              if (checkClamped && checkClamped[0]) {
                const checkCarto = Cesium.Cartographic.fromCartesian(checkClamped[0]);
                if (checkCarto.height <= planeHeight + 2.0) {
                  finalDist = checkDist;
                  break;
                }
              }
            } catch (e) {
              // Continue
            }
          }
        }
      } catch (e) {
        // Use full range if check fails
      }
    }
    
    // Add point at final distance
    const finalLon = txLon + (dirX * finalDist / metersPerDegLon);
    const finalLat = txLat + (dirY * finalDist / metersPerDegLat);
    wavePoints.push(finalLon, finalLat);
  }
  
  console.log(`[TX Plane] Plane expansion complete, ${wavePoints.length / 2} points generated`);
  
  return {
    hierarchy: new Cesium.PolygonHierarchy(Cesium.Cartesian3.fromDegreesArray(wavePoints)),
    wavePoints: wavePoints
  };
}

async function setTxMarker(lon, lat) {
  let h = 0.0;
  try {
    h = await clampHeightAt(lon, lat);
  } catch (e) {
    console.warn('[TX Marker] clampHeightAt failed, using 0.0');
    h = 0.0;
  }

  // Place TX slightly above the clamped surface so it's visible.
  const TX_HEIGHT_M = 10.0;
  const pos = Cesium.Cartesian3.fromDegrees(lon, lat, h + TX_HEIGHT_M);

  // Small center marker (for precise TX location)
  if (!_txMarker) {
    _txMarker = viewer.entities.add({
      name: 'tx',
      point: {
        pixelSize: 8,
        color: Cesium.Color.YELLOW,
        outlineColor: Cesium.Color.BLACK,
        outlineWidth: 2,
        disableDepthTestDistance: Number.POSITIVE_INFINITY
      },
      position: pos
    });
  } else {
    _txMarker.position = pos;
  }

  // TX emission plane (flat circle at ground level, deformed/cut by buildings)
  // The plane grows from TX point and gets fully stopped/deformed when contacting buildings
  // This represents maximum attenuation - buildings completely block the RF signal
  const planeHeightOffset = parseFloat(document.getElementById('planeHeight').value || '0.5');
  const planeHeight = Math.max(0.0, h) + planeHeightOffset; // Use slider value
  
  console.log(`[TX Plane] Creating plane at height ${planeHeight}m (offset: ${planeHeightOffset}m)`);
  
  // Build the plane with Google mesh interaction (deformations)
  let result;
  try {
    result = await buildPlaneWithGoogleMeshInteraction(lon, lat, planeHeight);
  } catch (e) {
    console.error('[TX Plane] buildPlaneWithGoogleMeshInteraction failed:', e);
    // Fallback to full circle if mesh interaction fails
    const numRays = 64;
    const metersPerDegLat = 111320.0;
    const cosLat = Math.cos(Cesium.Math.toRadians(lat));
    const metersPerDegLon = metersPerDegLat * Math.max(0.1, cosLat);
    const wavePoints = [];
    for (let i = 0; i < numRays; i++) {
      const angle = (i / numRays) * Math.PI * 2.0;
      const dirX = Math.cos(angle);
      const dirY = Math.sin(angle);
      const finalLon = lon + (dirX * MAX_RANGE_M / metersPerDegLon);
      const finalLat = lat + (dirY * MAX_RANGE_M / metersPerDegLat);
      wavePoints.push(finalLon, finalLat);
    }
    result = {
      hierarchy: new Cesium.PolygonHierarchy(Cesium.Cartesian3.fromDegreesArray(wavePoints)),
      wavePoints: wavePoints
    };
  }
  
  const hierarchy = result.hierarchy;
  const wavePoints = result.wavePoints;
  
  if (!_txSphere) {
    _txSphere = viewer.entities.add({
      name: 'tx_plane',
      polygon: {
        hierarchy: hierarchy,
        height: planeHeight,
        material: new Cesium.Color(0.8, 0.6, 0.0, 0.75), // Darker orange-yellow, more opaque (75% opacity)
        outline: true,
        outlineColor: new Cesium.Color(0.9, 0.7, 0.0, 0.90), // Darker outline, very opaque (90% opacity)
        // Disable depth testing - we handle building blocking in ray intersection logic, not via Cesium's depth buffer
        // Setting to infinity prevents Google tiles from clipping the plane
        disableDepthTestDistance: Number.POSITIVE_INFINITY
      }
    });
    
    console.log(
      "[TX Plane] Created plane, disableDepthTestDistance =",
      _txSphere.polygon.disableDepthTestDistance
    );
  } else {
    // Update the plane when TX moves (recompute holes for new position)
    _txSphere.polygon.hierarchy = hierarchy;
    _txSphere.polygon.height = planeHeight;
    
    console.log(
      "[TX Plane] Updated plane, disableDepthTestDistance =",
      _txSphere.polygon.disableDepthTestDistance
    );
  }
  
  // Check C: Draw the same boundary as a polyline (no fill, no triangulation)
  if (_txPlaneOutline) {
    viewer.entities.remove(_txPlaneOutline);
    _txPlaneOutline = null;
  }
  
  _txPlaneOutline = viewer.entities.add({
    name: "tx_plane_outline",
    polyline: {
      positions: Cesium.Cartesian3.fromDegreesArray(wavePoints),
      width: 3,
      material: Cesium.Color.CYAN,
      clampToGround: false,
      disableDepthTestDistance: Number.POSITIVE_INFINITY
    }
  });
  
  console.log(`[TX Plane] Plane created with ${wavePoints.length / 2} points`);
}


// Build the heat overlay *once* at fixed world locations (the precomputed grid around CENTER).
// When TX moves we only update colors. This avoids the "overlay disappears/rebuilds" behavior.
async function buildHeatOverlayOnce() {
  if (!_heatPoints || _heatBuilt) return;

  // Precompute probe positions at the grid lon/lat (not relative to TX).
  const probes = [];
  _heatLonLat = [];
  for (let i = 0; i < grid.length; i++) {
    const lon = grid[i].lon;
    const lat = grid[i].lat;
    _heatLonLat.push([lon, lat]);
    probes.push(Cesium.Cartesian3.fromDegrees(lon, lat, 200.0));
  }

  // Clamp in batches (Cesium can get unhappy with huge arrays)
  const BATCH = 400;
  _heatPrims = new Array(probes.length);
  for (let i = 0; i < probes.length; i += BATCH) {
    const slice = probes.slice(i, i + BATCH);
    let clamped = null;
    try {
      clamped = await viewer.scene.clampToHeightMostDetailed(slice);
    } catch (e) {
      clamped = null;
    }
    for (let j = 0; j < slice.length; j++) {
      const k = i + j;
      const pos = (clamped && clamped[j]) ? clamped[j] : slice[j];
      const p = _heatPoints.add({
        position: pos,
        color: Cesium.Color.TRANSPARENT,
        pixelSize: 6,
        disableDepthTestDistance: 0.0,
        show: false
      });
      _heatPrims[k] = p;
    }
  }

  _heatBuilt = true;
  // Apply the current TX immediately after building.
  updateHeatOverlayColors();
}

function updateHeatOverlayColors() {
  if (!_heatPoints || !_heatBuilt) return;
  const on = document.getElementById('toggleHeat').checked;
  const alpha = parseFloat(document.getElementById('alpha').value || '0.55');

  // Simple FSPL-based heat overlay (no OSM building attenuation)
  for (let i = 0; i < _heatPrims.length; i++) {
    const p = _heatPrims[i];
    if (!p) continue;
    if (!on) {
      p.show = false;
      continue;
    }
    p.show = true;
    const ll = _heatLonLat[i];
    const d = haversineMeters(_txLat, _txLon, ll[1], ll[0]);
    const pl = fsplDb(d, RF_FREQ_MHZ);
    const rsrp = TX_POWER_DBM - pl;
    p.color = colorForRsrp(rsrp, alpha);
  }
}

async function recomputeAll(lon, lat) {
  _txLon = lon;
  _txLat = lat;
  statusEl.textContent = 'Recomputing RF (click TX) …';
  // Don't block RF updates on the (async) height clamp.
  setTxMarker(lon, lat).catch(function(e){ console.warn('setTxMarker failed', e); });
  updateHeatOverlayColors();
  statusEl.textContent = 'Ready.';
}

async function requestRecompute(lon, lat) {
  _pendingTx = [lon, lat];
  if (_recomputeBusy) return;
  _recomputeBusy = true;
  while (_pendingTx) {
    const p = _pendingTx;
    _pendingTx = null;
    await recomputeAll(p[0], p[1]);
  }
  _recomputeBusy = false;
}

function setupInteractiveClickHandler() {
  // Replace any existing handler by just creating a new one (Cesium allows multiple, but we only use one in this demo).
  const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
  handler.setInputAction(function(click) {
    // Pick info removed (no OSM entities to pick)
    pickedEl.textContent = '';

    if (!document.getElementById('toggleClick').checked) return;

    let cartesian = null;
    if (viewer.scene.pickPositionSupported) {
      cartesian = viewer.scene.pickPosition(click.position);
    }
    if (!Cesium.defined(cartesian)) {
      cartesian = viewer.camera.pickEllipsoid(click.position, Cesium.Ellipsoid.WGS84);
    }
    if (!Cesium.defined(cartesian)) return;

    const carto = Cesium.Cartographic.fromCartesian(cartesian);
    const lon = Cesium.Math.toDegrees(carto.longitude);
    const lat = Cesium.Math.toDegrees(carto.latitude);

    // If overlays are still building, queue the TX but still show the marker immediately.
    if (window.__buildBusy) {
      window.__queuedTxLonLat = [lon, lat];
      statusEl.textContent = 'TX set (queued) — overlays still loading…';
      // Fire-and-forget marker update
      setTxMarker(lon, lat);
      return;
    }

    requestRecompute(lon, lat);
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);
}

async function initInteractiveMilestoneA() {
  // Guard: this function can be called multiple times (e.g., after rebuilding entities).
  // We want to re-render, but not stack event listeners / handlers endlessly.
  if (!_heatPoints) {
    _heatPoints = viewer.scene.primitives.add(new Cesium.PointPrimitiveCollection());
  }

  // Heat overlay geometry is built once and then recolored on TX changes.
  // If the user clicks before this finishes, colors will apply after build completes.
  if (!_heatBuilt) {
    buildHeatOverlayOnce();
  }

  // Entities get wiped by viewer.entities.removeAll() during rebuilds.
  // Force marker to be recreated on the next setTxMarker().
  _txMarker = null;
  _txSphere = null;

  // Bind UI listeners only once.
  if (!window.__rfInteractiveBound) {
    window.__rfInteractiveBound = true;

    setupInteractiveClickHandler();

    document.getElementById('toggleHeat').addEventListener('change', function() {
      // No rebuild; just show/hide + recolor.
      updateHeatOverlayColors();
    });

    // Alpha slider is wired outside this block to avoid double listeners.
  }

  // Apply queued TX (if the user clicked while things were loading), otherwise recompute at current TX.
  const q = window.__queuedTxLonLat;
  if (q && q.length === 2) {
    window.__queuedTxLonLat = null;
    await requestRecompute(q[0], q[1]);
  } else {
    await requestRecompute(_txLon, _txLat);
  }
}
// === End interactive block ===
"""


def create_google_mesh_rf_3d_html(
    api_key: str,
    center_lat: float,
    center_lon: float,
    grid_points: List[Tuple[float, float, float]],
    output_path: str,
    freq_mhz: float,
    tx_power_dbm: float,
    max_range_m: float,
    step_m: float,
    show_google_tiles: bool = True,
) -> str:
    tileset_url = f"https://tile.googleapis.com/v1/3dtiles/root.json?key={api_key}"

    grid_json = json.dumps(
        [{"lon": lon, "lat": lat, "rf_dbm": rsrp} for lon, lat, rsrp in grid_points]
    )

    # Important: avoid JS template literals with ${} inside a Python f-string.
    html = f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
  <title>Google Mesh RF in 3D</title>
  <script src=\"https://cesium.com/downloads/cesiumjs/releases/1.111/Build/Cesium/Cesium.js\"></script>
  <link href=\"https://cesium.com/downloads/cesiumjs/releases/1.111/Build/Cesium/Widgets/widgets.css\" rel=\"stylesheet\" />
  <style>
    html, body, #cesiumContainer {{ width: 100%; height: 100%; margin: 0; padding: 0; overflow: hidden; }}
    .panel {{
      position: absolute; background: rgba(30,30,30,0.90); color: #fff;
      padding: 10px 12px; border-radius: 8px; font-family: Arial, sans-serif;
      font-size: 12px; z-index: 10; max-width: 360px;
    }}
    #info {{ top: 10px; left: 10px; }}
    #controls {{ top: 10px; right: 10px; }}
    .row {{ display:flex; align-items:center; justify-content:space-between; gap:10px; margin: 6px 0; }}
    input[type=range] {{ width: 180px; }}
    .small {{ opacity: 0.85; font-size: 11px; }}
    .badge {{ display:inline-block; padding: 2px 6px; border-radius: 999px; background: rgba(255,255,255,0.12); }}
    button {{ cursor:pointer; }}
  </style>
</head>
<body>
  <div id=\"cesiumContainer\"></div>

  <div id=\"info\" class=\"panel\">
    <div style=\"font-weight:700; font-size:13px;\">Google Mesh RF in 3D</div>
    <div class=\"small\">Center: ({center_lat:.6f}, {center_lon:.6f})</div>
    <div id=\"status\" class=\"small\" style=\"margin-top:6px;\">Loading…</div>
    <div id=\"picked\" class=\"small\" style=\"margin-top:6px;\"></div>
  </div>

  <div id=\"controls\" class=\"panel\">
    <div style=\"font-weight:700; font-size:13px;\">Controls</div>
    <div class=\"row\"><label><input id=\"toggleGoogle\" type=\"checkbox\" {('checked' if show_google_tiles else '')}/> Google 3D Tiles</label></div>
    <div class=\"row\"><label><input id=\"toggleHeat\" type=\"checkbox\" checked /> Ground heat overlay</label></div>
    <div class=\"row\"><label><input id=\"toggleClick\" type=\"checkbox\" checked /> Click sets TX + recompute</label></div>
    <div class=\"row\">
      <label for=\"alpha\">Overlay alpha</label>
      <input id=\"alpha\" type=\"range\" min=\"0.05\" max=\"0.95\" step=\"0.05\" value=\"0.55\" />
    </div>
    <div class=\"row\">
      <label for=\"planeHeight\">Plane height (m)</label>
      <input id=\"planeHeight\" type=\"range\" min=\"0.0\" max=\"50.0\" step=\"0.5\" value=\"0.5\" />
      <span id=\"planeHeightValue\" class=\"small\">0.5</span>
    </div>
    <div class=\"small\">Tip: click anywhere to move the transmitter. Plane interacts with Google 3D mesh.</div>
  </div>

  <script>
    const tilesetUrl = {json.dumps(tileset_url)};
    const grid = {grid_json};

const CENTER_LON = {center_lon};
const CENTER_LAT = {center_lat};
const RF_FREQ_MHZ = {freq_mhz};
const TX_POWER_DBM = {tx_power_dbm};
const MAX_RANGE_M = {max_range_m};
const GRID_STEP_M = {step_m};


    const statusEl = document.getElementById('status');
    const pickedEl = document.getElementById('picked');

    { _color_ramp_js() }

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

    async function loadGoogleTiles() {{
      statusEl.textContent = 'Loading Google photorealistic tiles…';
      googleTileset = await Cesium.Cesium3DTileset.fromUrl(tilesetUrl, {{ showCreditsOnScreen: true }});
      viewer.scene.primitives.add(googleTileset);
      // Progress event: use loadProgress (tileLoadProgressEvent is not in Cesium 1.111)
      googleTileset.loadProgress.addEventListener(function(pending, processing) {{
        if (pending === 0 && processing === 0) {{
          statusEl.textContent = 'Tiles loaded. Building overlays…';
        }}
      }});
      return googleTileset;
    }}

    function flyToCenter() {{
      viewer.camera.setView({{
        destination: Cesium.Cartesian3.fromDegrees({center_lon}, {center_lat}, 1300),
        orientation: {{ heading: 0.0, pitch: Cesium.Math.toRadians(-40), roll: 0.0 }}
      }});
    }}

    async function clampHeightAt(lon, lat) {{
      // Clamp against the Google photorealistic tileset (or any loaded 3D Tiles).
      // NOTE: This often returns *roof* height when the (lon,lat) is inside a building footprint.
      const probe = Cesium.Cartesian3.fromDegrees(lon, lat, 200.0);
      try {{
        const clamped = await viewer.scene.clampToHeightMostDetailed([probe]);
        if (clamped && clamped[0]) {{
          const carto = Cesium.Cartographic.fromCartesian(clamped[0]);
          return carto.height;
        }}
      }} catch (e) {{
        // ignore
      }}
      return 0.0;
    }}

    function _dropDuplicateClosingVertex(verts) {{
      if (!verts || verts.length < 3) return verts || [];
      const a = verts[0];
      const b = verts[verts.length - 1];
      if (a && b && a.length === 2 && b.length === 2 && a[0] === b[0] && a[1] === b[1]) {{
        return verts.slice(0, verts.length - 1);
      }}
      return verts;
    }}

    function _outwardProbeLonLats(vertsIn, centroidLonLat, offsetM) {{
      // Build probe points just outside the footprint so clamp-to-height is more likely to hit ground/street
      // instead of the roof. Uses ENU frame at centroid.
      const verts = _dropDuplicateClosingVertex(vertsIn);
      if (!verts || verts.length < 3) return [];

      const cCarto = Cesium.Cartographic.fromDegrees(centroidLonLat[0], centroidLonLat[1], 0.0);
      const cPos = Cesium.Ellipsoid.WGS84.cartographicToCartesian(cCarto);
      const enu = Cesium.Transforms.eastNorthUpToFixedFrame(cPos);
      const inv = Cesium.Matrix4.inverse(enu, new Cesium.Matrix4());

      const probes = [];
      const n = verts.length;
      const maxSamples = 6;
      const step = Math.max(1, Math.floor(n / maxSamples));

      for (let i = 0; i < n; i += step) {{
        const v = verts[i];
        const vCarto = Cesium.Cartographic.fromDegrees(v[0], v[1], 0.0);
        const vPos = Cesium.Ellipsoid.WGS84.cartographicToCartesian(vCarto);
        const vLocal = Cesium.Matrix4.multiplyByPoint(inv, vPos, new Cesium.Cartesian3());

        // Direction from centroid (0,0) to vertex in local EN plane
        const dx = vLocal.x;
        const dy = vLocal.y;
        const len = Math.hypot(dx, dy);
        if (!isFinite(len) || len < 0.01) continue;

        const ux = dx / len;
        const uy = dy / len;

        // Push outward beyond the vertex by offsetM meters
        const outLocal = new Cesium.Cartesian3(dx + ux * offsetM, dy + uy * offsetM, 0.0);
        const outWorld = Cesium.Matrix4.multiplyByPoint(enu, outLocal, new Cesium.Cartesian3());
        const outCarto = Cesium.Cartographic.fromCartesian(outWorld);

        probes.push([
          Cesium.Math.toDegrees(outCarto.longitude),
          Cesium.Math.toDegrees(outCarto.latitude),
        ]);
      }}

      return probes;
    }}

    function _pointInPoly(lon, lat, vertsIn) {{
      const verts = _dropDuplicateClosingVertex(vertsIn);
      if (!verts || verts.length < 3) return false;
      let inside = false;
      for (let i = 0, j = verts.length - 1; i < verts.length; j = i++) {{
        const xi = verts[i][0], yi = verts[i][1];
        const xj = verts[j][0], yj = verts[j][1];
        // Ray-cast intersection test
        const denom = (yj - yi);
        const xInt = (xj - xi) * (lat - yi) / (Math.abs(denom) < 1e-12 ? 1e-12 : denom) + xi;
        const intersect = ((yi > lat) !== (yj > lat)) && (lon < xInt);
        if (intersect) inside = !inside;
      }}
      return inside;
    }}

    // OSM building check removed - using Google mesh only

    function _radialProbeLonLats(centroidLonLat, radiusM, num) {{
      const probes = [];
      const lon0 = centroidLonLat[0];
      const lat0 = centroidLonLat[1];
      const metersPerDegLat = 111320.0;
      const cosLat = Math.cos(Cesium.Math.toRadians(lat0));
      const metersPerDegLon = metersPerDegLat * Math.max(0.1, cosLat);
      for (let k = 0; k < num; k++) {{
        const ang = (k / num) * Math.PI * 2.0;
        const east = Math.cos(ang) * radiusM;
        const north = Math.sin(ang) * radiusM;
        const lon = lon0 + (east / metersPerDegLon);
        const lat = lat0 + (north / metersPerDegLat);
        probes.push([lon, lat]);
      }}
      return probes;
    }}

    // OSM building/forest rendering removed - using Google mesh only
    // Picking + interactive TX click
    {_interactive_click_js()}

    // UI wiring
    document.getElementById('toggleGoogle').addEventListener('change', function() {{
      if (!googleTileset) return;
      googleTileset.show = this.checked;
    }});
    // OSM building/forest toggles removed
    document.getElementById('alpha').addEventListener('input', function() {{
      // Recolor using current TX + alpha without rebuilding geometry.
      requestRecompute(_txLon, _txLat);
    }});
    
    // Plane height slider
    const planeHeightSlider = document.getElementById('planeHeight');
    const planeHeightValue = document.getElementById('planeHeightValue');
    planeHeightSlider.addEventListener('input', function() {{
      const value = parseFloat(this.value);
      planeHeightValue.textContent = value.toFixed(1);
      // Recompute plane with new height
      if (_txLon !== undefined && _txLat !== undefined) {{
        requestRecompute(_txLon, _txLat);
      }}
    }});
    // Initialize display value
    planeHeightValue.textContent = parseFloat(planeHeightSlider.value).toFixed(1);

    (async function main() {{
      flyToCenter();
      // Bind click handler + create heat overlay immediately so clicks work even while overlays load.
      await initInteractiveMilestoneA();
      const tiles = await loadGoogleTiles();
      // Ensure toggle matches initial state
      tiles.show = document.getElementById('toggleGoogle').checked;
      // OSM building/forest rendering removed
    }})();
  </script>
</body>
</html>
"""

    out_dir = os.path.dirname(output_path) or "."
    os.makedirs(out_dir, exist_ok=True)
    Path(output_path).write_text(html, encoding="utf-8")
    return output_path


# -------------------------
# Main
# -------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Google Mesh RF 3D demo")
    parser.add_argument("--api-key", required=True, help="Google Maps Platform API key (Map Tiles API enabled)")
    parser.add_argument("--lat", type=float, default=37.7749, help="Center latitude")
    parser.add_argument("--lon", type=float, default=-122.4194, help="Center longitude")

    parser.add_argument("--freq-mhz", type=float, default=3500.0, help="TX frequency (MHz)")
    parser.add_argument("--tx-power-dbm", type=float, default=43.0, help="TX power (dBm)")
    parser.add_argument("--max-range-m", type=float, default=500.0, help="Coverage max range (m)")
    parser.add_argument("--step-m", type=float, default=5.0, help="Coverage radial step (m)")

    # OSM-related arguments removed

    parser.add_argument("--cache-dir", default="./cache/google_maps_3d", help="Cache directory for Google tileset root.json")
    parser.add_argument("--no-cache", action="store_true", help="Disable Google tileset caching")
    parser.add_argument("--output", default="./osm_rf_3d.html", help="Output HTML")
    parser.add_argument("--no-google", action="store_true", help="Start with Google tiles hidden (OSM overlay only)")

    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("Google Mesh RF in 3D")
    logger.info("=" * 60)
    logger.info(f"Center: ({args.lat}, {args.lon})")
    logger.info(f"RF: freq={args.freq_mhz} MHz, tx={args.tx_power_dbm} dBm")
    logger.info(f"Grid: max_range={args.max_range_m}m, step={args.step_m}m")

    # 1) Validate Google tileset fetch (helps catch API key issues early)
    use_cache = not args.no_cache
    try:
        _ = fetch_tileset_with_cache(args.api_key, args.cache_dir, use_cache)
        logger.info("✓ Google tileset root fetch OK")
    except Exception as e:
        logger.error(f"Google tileset root fetch failed: {e}")
        return 1

    # OSM data fetching removed - using Google mesh only
    # No RF pipeline needed - plane interaction is handled in JavaScript via Google mesh
    
    grid_points: List[Tuple[float, float, float]] = []

    out = create_google_mesh_rf_3d_html(
        api_key=args.api_key,
        center_lat=args.lat,
        center_lon=args.lon,
        grid_points=grid_points,
        output_path=args.output,
        freq_mhz=args.freq_mhz,
        tx_power_dbm=args.tx_power_dbm,
        max_range_m=args.max_range_m,
        step_m=args.step_m,
        show_google_tiles=not args.no_google,
    )

    logger.info("=" * 60)
    logger.info("✓ HTML generated")
    logger.info(out)
    logger.info("Serve with: python3 -m http.server 8000")
    logger.info(f"Open: http://localhost:8000/{Path(out).name}")
    logger.info("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

