# Google Maps 3D Tiles Troubleshooting

## Issue: 3D Map Not Rendering (Only Blue Ring Visible)

### Problem
The 3D heatmap shows only the heatmap points (blue rings) but no geography/terrain/buildings from Google Maps 3D Tiles.

### Root Cause
The tileset JSON contains **relative URIs** like `/v1/3dtiles/datasets/...?session=...` that need to be:
1. Resolved to full URLs: `https://tile.googleapis.com` + relative path
2. Include the API key in all tile requests
3. Preserve session tokens from the tileset

### Solution Implemented

**Custom Request Handler**: Intercepts all CesiumJS resource requests to:
- Resolve relative URIs to full URLs
- Add API key to all Google Maps tile requests
- Preserve existing query parameters (session tokens)

**Code Location**: `scripts/test_google_maps_3d_heatmap.py` lines 330-370

### Verification Steps

1. **Open Browser Console** (F12)
   - Check for "Loading tile:" messages
   - Look for any 403/404 errors
   - Check for CesiumJS errors

2. **Expected Console Output**:
   ```
   Loading tile: https://tile.googleapis.com/v1/3dtiles/datasets/...?session=...&key=...
   Google Maps 3D Tiles loaded successfully
   Tileset bounding sphere: ...
   Tiles loading: pending=X, processing=Y
   ```

3. **Check Network Tab**:
   - Look for requests to `tile.googleapis.com`
   - Verify API key is in query string
   - Check response status (should be 200)
   - Verify tile files are being downloaded (.glb, .json)

4. **Visual Verification**:
   - Wait 5-10 seconds for initial tiles to load
   - Pan/zoom to trigger more tile loading
   - Buildings and terrain should appear progressively

### Common Issues

#### Issue 1: 403 Forbidden on Tile Requests
**Symptom**: Console shows 403 errors for tile requests

**Solution**: 
- Verify API key is correct
- Check that Map Tiles API is enabled in Google Cloud Console
- Ensure API key restrictions allow the domain/IP

#### Issue 2: Relative URIs Not Resolved
**Symptom**: 404 errors or "Failed to load resource"

**Solution**:
- Verify custom request handler is in HTML
- Check that `baseUrl` is set correctly
- Ensure handler is intercepting requests (check console logs)

#### Issue 3: Tiles Load But No Geography Visible
**Symptom**: Network shows successful tile loads but nothing renders

**Possible Causes**:
- Camera position not over tileset bounding volume
- Tileset bounding volume is incorrect
- CesiumJS version compatibility issue
- Browser WebGL support

**Solution**:
- Check tileset bounding sphere in console
- Manually set camera position
- Verify WebGL is enabled in browser
- Try different CesiumJS version

#### Issue 4: Session Token Expired
**Symptom**: Initial tiles load but subsequent tiles fail

**Solution**:
- Session tokens are valid for 3+ hours
- Clear cache and regenerate HTML
- Use `--no-cache` flag to fetch fresh tileset

### Debugging Commands

```bash
# Regenerate HTML with fresh tileset (no cache)
python3 scripts/test_google_maps_3d_heatmap.py \
    --api-key YOUR_API_KEY \
    --output ./test_3d_heatmap.html \
    --points 20 \
    --no-cache

# Check tileset structure
python3 << 'EOF'
import json
import glob
files = glob.glob('cache/google_maps_3d/tileset_*.json')
if files:
    with open(files[0]) as f:
        data = json.load(f)
        print("Tileset version:", data['tileset']['asset']['version'])
        print("Root bounding volume:", data['tileset']['root']['boundingVolume'])
EOF
```

### Browser Console Checks

1. **Check if handler is active**:
   ```javascript
   // Should show custom function
   console.log(Cesium.Resource._Implementations.loadWithXhr.toString().substring(0, 100));
   ```

2. **Check tileset status**:
   ```javascript
   // In browser console after page loads
   const tileset = viewer.scene.primitives.get(0);
   console.log('Tileset ready:', tileset.ready);
   console.log('Bounding sphere:', tileset.boundingSphere);
   console.log('Tiles loaded:', tileset.tilesLoaded);
   ```

3. **Manually trigger tile load**:
   ```javascript
   // Force load tiles
   viewer.scene.requestRender();
   ```

### Next Steps if Still Not Working

1. **Verify API Key Permissions**:
   - Check Google Cloud Console
   - Ensure Map Tiles API is enabled
   - Verify API key restrictions

2. **Test with Simple Example**:
   - Use Google's official example code
   - Compare with our implementation
   - Identify differences

3. **Check CesiumJS Version**:
   - Current: 1.111
   - Try latest version
   - Check compatibility with Google Maps 3D Tiles

4. **Network Inspection**:
   - Use browser DevTools Network tab
   - Verify tile requests include API key
   - Check response headers and status codes

