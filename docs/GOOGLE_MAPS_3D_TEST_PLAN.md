# Google Maps 3D Tiles Test Plan

## Overview

Before integrating Google Maps 3D Tiles into the main codebase, we need to prove the concept works with a standalone test script.

## Test Requirements

### 1. Fetch 3D Tiles from Google Maps API

**Goal**: Successfully fetch the root tileset from Google Maps 3D Tiles API

**Implementation**:
- Use correct URL: `https://tile.googleapis.com/v1/3dtiles/root.json?key=YOUR_API_KEY`
- Handle API responses and errors
- Validate tileset JSON structure

**Success Criteria**:
- ✅ Tileset JSON fetched successfully
- ✅ Valid JSON structure returned
- ✅ No authentication errors

### 2. Cache Management

**Goal**: Implement caching to prevent API overuse

**Implementation**:
- Cache tileset JSON response
- Cache TTL: 3 hours (matches API session validity)
- Cache key: Hash of API key (for security)
- Cache location: `./cache/google_maps_3d/`

**Success Criteria**:
- ✅ Cache file created on first fetch
- ✅ Subsequent requests use cached data
- ✅ Cache expires after 3 hours
- ✅ Cache can be bypassed with flag

### 3. 3D Heatmap Visualization

**Goal**: Create 3D heatmap equivalent to existing 2D heatmap

**2D Heatmap Reference** (from `create_position_heatmap()`):
- Data format: `[lat, lon, intensity]` where `intensity = quality_score * 10`
- Gradient: `{0.4: 'blue', 0.6: 'lime', 0.8: 'orange', 1.0: 'red'}`
- Radius: 15, Blur: 10
- Visualization: Folium HeatMap plugin

**3D Heatmap Implementation**:
- Same data format: `[lat, lon, intensity]`
- Same color gradient: blue → lime → orange → red
- Visualization: CesiumJS billboards with gradient circles
- Height: Based on intensity (10-60m above ground)
- Scale: Based on intensity (0.5-2.0x)

**Success Criteria**:
- ✅ 3D heatmap renders in CesiumJS
- ✅ Colors match 2D gradient
- ✅ Points positioned correctly in 3D space
- ✅ Interactive (click to see details)
- ✅ Google Maps 3D Tiles visible as base layer

## Test Script

**Location**: `scripts/test_google_maps_3d_heatmap.py`

**Usage**:
```bash
python scripts/test_google_maps_3d_heatmap.py \
    --api-key YOUR_API_KEY \
    --output ./test_3d_heatmap.html \
    --points 50
```

**Options**:
- `--api-key`: Google Maps Platform API key (required)
- `--output`: Output HTML file path (default: `./test_3d_heatmap.html`)
- `--cache-dir`: Cache directory (default: `./cache/google_maps_3d`)
- `--no-cache`: Disable caching (force API fetch)
- `--points`: Number of heatmap points (default: 50)

## Test Execution

### Step 1: Initial Test
```bash
# First run - will fetch from API and cache
python scripts/test_google_maps_3d_heatmap.py --api-key YOUR_API_KEY
```

**Expected**:
- API request made
- Cache file created
- HTML file generated
- Open HTML in browser to verify 3D visualization

### Step 2: Cache Test
```bash
# Second run - should use cache
python scripts/test_google_maps_3d_heatmap.py --api-key YOUR_API_KEY
```

**Expected**:
- No API request (uses cache)
- Faster execution
- Same HTML output

### Step 3: Cache Bypass Test
```bash
# Force API fetch
python scripts/test_google_maps_3d_heatmap.py --api-key YOUR_API_KEY --no-cache
```

**Expected**:
- API request made despite cache
- Cache updated
- HTML file generated

## Verification Checklist

- [x] Tileset fetched successfully from API ✅
- [x] Cache file created in `./cache/google_maps_3d/` ✅
- [x] Cache used on subsequent runs ✅
- [x] Cache expires after 3 hours (TTL configured)
- [ ] 3D heatmap renders in browser (needs manual verification)
- [ ] Google Maps 3D Tiles visible (buildings/terrain) (needs manual verification)
- [ ] Heatmap points visible with correct colors (needs manual verification)
- [ ] Color gradient matches 2D version (blue→lime→orange→red) (needs manual verification)
- [ ] Points positioned at correct 3D locations (needs manual verification)
- [ ] Interactive features work (click, zoom, pan) (needs manual verification)
- [ ] No console errors in browser (needs manual verification)

## Test Results

**Status**: ✅ **PASSED** - All automated tests successful

**Test Execution**:
```bash
$ python3 scripts/test_google_maps_3d_heatmap.py \
    --api-key YOUR_API_KEY \
    --output ./test_3d_heatmap.html \
    --points 20

✓ Tileset fetched successfully
✓ Cache created: cache/google_maps_3d/tileset_*.json
✓ 3D heatmap generated: ./test_3d_heatmap.html
```

**Key Findings**:
1. ✅ API URL format is correct: `https://tile.googleapis.com/v1/3dtiles/root.json?key=API_KEY`
2. ✅ No session tokens needed for root tileset - direct API key authentication works
3. ✅ Headers required: User-Agent and Accept headers prevent 403 errors
4. ✅ Caching works: Tileset cached for 3 hours, reused on subsequent runs
5. ✅ HTML generation successful: CesiumJS integration complete
6. ⚠️ **Relative URI Resolution**: Tileset contains relative URIs like `/v1/3dtiles/datasets/...?session=...`
   - Custom request handler intercepts CesiumJS requests
   - Resolves relative URIs to full URLs: `https://tile.googleapis.com` + relative path
   - Preserves session tokens from tileset
   - Adds API key to all tile requests
7. ⚠️ **Tile Loading**: CesiumJS automatically loads tiles based on camera position
   - Tiles load progressively as user navigates
   - May take a few seconds for initial tiles to appear
   - Check browser console for tile loading progress

**Next Steps**:
- Manual browser verification of 3D visualization
- Integration into main codebase
- Add Elevation API for altitude data

## Next Steps

Once all tests pass:

1. **Integrate into main codebase**
   - Move classes to `src/agentic_ue_planner/ui/`
   - Update `RFVisualizer` to use new classes
   - Add configuration options

2. **Add Elevation API**
   - Fetch altitude for telemetry points
   - Support 3D coordinate system

3. **Map Provider Abstraction**
   - Create abstract `MapProvider` interface
   - Support both OSM (2D) and Google 3D
   - Unified API for visualization methods

4. **Update Documentation**
   - Integration guide
   - API reference
   - Usage examples

