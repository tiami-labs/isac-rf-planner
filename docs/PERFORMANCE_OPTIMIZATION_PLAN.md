# Performance Optimization Plan: Geometric Computation Acceleration

## Current Bottleneck

**Problem**: Processing 7,200 cells takes ~10-12 minutes due to expensive geometric computations.

**Root Cause**: For each cell, we perform:
- 2-3 ray-polygon intersection checks
- Each check tests against ALL polygons in cache (~200-400 buildings + landuse)
- Each polygon test involves ray-edge intersection checks
- **Total**: ~50-100 million geometric operations

**Current Flow**:
```
For each of 7,200 cells:
  1. _estimate_material_from_geometry():
     - is_forest_between() → tests all landuse polygons
     - count_buildings_between() → tests all building polygons
  2. estimate_obstacles_along_ray():
     - count_buildings_between() → tests all building polygons again
```

---

## Optimization Strategy: Multi-Layer Approach

### Phase 1: Quick Wins (2-5x speedup, ~1-2 hours implementation)

#### 1.1 Bounding Box Pre-Filtering
**Location**: `osm_map_provider.py::count_buildings_between()` and `is_forest_between()`

**Idea**: Before expensive ray-polygon intersection, quickly reject polygons whose bounding box doesn't intersect the ray's bounding box.

**Implementation**:
- Pre-compute bounding boxes for all polygons during prefetch
- Store as `(min_lat, max_lat, min_lon, max_lon)` per polygon
- Before `_ray_intersects_polygon()`, check if ray's bbox overlaps polygon's bbox
- Skip ~80-90% of polygons without expensive intersection tests

**Expected Speedup**: 3-5x (reduces polygon tests by 80-90%)

**Code Changes**:
```python
# In prefetch_all_data():
for building in buildings:
    # Compute and cache bounding box
    lats = [p["lat"] for p in building["geometry"]]
    lons = [p["lon"] for p in building["geometry"]]
    building["bbox"] = (min(lats), max(lats), min(lons), max(lons))

# In count_buildings_between():
for building in buildings:
    if not _ray_bbox_intersects_polygon_bbox(start, end, building["bbox"]):
        continue  # Skip expensive intersection test
    if _ray_intersects_polygon(start, end, building["geometry"]):
        count += 1
```

---

#### 1.2 Early Termination in Material Detection
**Location**: `world_builder.py::_estimate_material_from_geometry()`

**Idea**: Once we find forest, we don't need to check buildings. Once we find enough buildings, we can stop.

**Implementation**:
- `is_forest_between()`: Return `True` immediately on first forest intersection
- `count_buildings_between()`: Add optional `max_count` parameter, stop when reached
- For material classification: Stop after finding 3 buildings (enough to classify as LARGE_STRUCTURE)

**Expected Speedup**: 1.5-2x (reduces unnecessary checks)

**Code Changes**:
```python
# In _estimate_material_from_geometry():
if map_provider.is_forest_between(tx, cell):  # Stops at first forest
    return MaterialType.TREES

# Only count up to 3 buildings (enough for classification)
building_count = map_provider.count_buildings_between(tx, cell, max_count=3)
```

---

#### 1.3 Cache Ray-Polygon Results
**Location**: `osm_map_provider.py::count_buildings_between()`

**Idea**: Many cells share similar rays (especially at same distance, different bearings). Cache intersection results.

**Implementation**:
- Create `_ray_cache: Dict[Tuple[LatLon, LatLon], Set[int]]` mapping ray → set of intersecting polygon IDs
- Before testing, check if we've seen this exact ray before
- Cache polygon IDs that intersect, not just counts

**Expected Speedup**: 1.3-1.5x (reduces duplicate computations)

**Limitation**: Memory usage increases, but manageable (~few MB)

---

### Phase 2: Spatial Indexing (10-20x speedup, ~4-6 hours implementation)

#### 2.1 Quadtree Spatial Index
**Location**: New file `geo/spatial_index.py`, integrate into `OSMMapProvider`

**Idea**: Organize polygons in a quadtree. For each ray, only test polygons in quadtree cells that the ray passes through.

**Implementation**:
- Build quadtree during `prefetch_all_data()`
- Each quadtree node contains list of polygon IDs whose bounding boxes overlap that region
- For ray query: traverse quadtree to find cells ray intersects, only test polygons in those cells
- Reduces polygon tests from O(n) to O(log n + k) where k = polygons near ray

**Expected Speedup**: 10-20x for typical urban areas

**Dependencies**: Implement simple quadtree (or use lightweight library like `pyqtree`)

**Code Structure**:
```python
class QuadtreeIndex:
    def __init__(self, bbox, max_depth=8, max_items=10):
        self.bbox = bbox
        self.max_depth = max_depth
        self.max_items = max_items
        self.children = None
        self.items = []  # List of (polygon_id, polygon_bbox)
    
    def insert(self, polygon_id, bbox):
        # Recursively insert into appropriate child
        
    def query_ray(self, start, end):
        # Return list of polygon IDs whose cells intersect ray
```

---

#### 2.2 R-Tree Alternative (More Complex, Better for Complex Geometries)
**Location**: New file `geo/spatial_index.py`

**Idea**: Use R-tree (rectangular bounding box tree) instead of quadtree. Better for irregular polygon distributions.

**Implementation**:
- Use library like `rtree` (Python bindings for libspatialindex)
- Build R-tree index during prefetch
- Query with ray's bounding box
- More accurate than quadtree for complex geometries

**Expected Speedup**: 15-25x

**Dependencies**: `pip install rtree` (requires libspatialindex C library)

**Trade-off**: More complex setup, but better performance for irregular data

---

### Phase 3: Parallelization (2-4x speedup, ~2-3 hours implementation)

#### 3.1 Multi-Processing Cell Processing
**Location**: `world_builder.py::build_world_model()`

**Idea**: Process multiple cells in parallel using multiprocessing.

**Implementation**:
- Split cells into chunks (e.g., 1000 cells per chunk)
- Use `multiprocessing.Pool` to process chunks in parallel
- Each worker gets copy of map_provider with cached data
- Combine results at end

**Expected Speedup**: 2-4x (limited by CPU cores, typically 4-8 cores)

**Code Structure**:
```python
from multiprocessing import Pool, cpu_count

def _process_cell_chunk(args):
    cells_chunk, tx, views, map_provider = args
    # Process chunk of cells
    return processed_cells

# In build_world_model():
num_workers = min(cpu_count(), 8)
chunk_size = len(cells) // num_workers
chunks = [cells[i:i+chunk_size] for i in range(0, len(cells), chunk_size)]

with Pool(num_workers) as pool:
    results = pool.map(_process_cell_chunk, 
                       [(chunk, tx, views, map_provider) for chunk in chunks])
```

**Challenges**:
- MapProvider needs to be pickleable (OSMMapProvider with cached data should work)
- Memory usage increases (each worker has copy of cache)
- Debugging is harder

---

#### 3.2 Vectorized Ray-Polygon Tests (Advanced)
**Location**: `osm_map_provider.py::_ray_intersects_polygon()`

**Idea**: Use NumPy vectorization to test multiple polygons against one ray simultaneously.

**Implementation**:
- Convert polygon edges to NumPy arrays
- Vectorize segment intersection tests
- Process batches of polygons at once

**Expected Speedup**: 1.5-2x (limited by algorithm, not implementation)

**Complexity**: High - requires rewriting intersection logic in NumPy

---

### Phase 4: Algorithmic Improvements (5-10x speedup, ~6-8 hours implementation)

#### 4.1 Sweep-Line Algorithm for Ray-Polygon Intersection
**Location**: `osm_map_provider.py::_ray_intersects_polygon()`

**Idea**: Instead of testing ray against all polygon edges, use sweep-line algorithm to find intersections more efficiently.

**Current**: O(n) where n = number of edges
**Sweep-line**: O(n log n) preprocessing, O(log n + k) query where k = intersections

**Expected Speedup**: 2-3x for complex polygons

**Complexity**: High - requires significant algorithm rewrite

---

#### 4.2 Approximate Material Classification
**Location**: `world_builder.py::_estimate_material_from_geometry()`

**Idea**: For distant cells, use coarser material classification (skip detailed building counting).

**Implementation**:
- For cells > 200m: Use clutter type only (already computed)
- For cells 100-200m: Count buildings with lower precision
- For cells < 100m: Full precision (current behavior)

**Expected Speedup**: 2-3x (reduces computation for ~60% of cells)

**Trade-off**: Slightly less accurate for distant cells, but RF signal is already weak there

---

## Implementation Priority

### Immediate (This Week)
1. **Bounding Box Pre-Filtering** (Phase 1.1) - Biggest bang for buck
2. **Early Termination** (Phase 1.2) - Easy win

### Short Term (Next 2 Weeks)
3. **Quadtree Spatial Index** (Phase 2.1) - Major performance boost
4. **Parallelization** (Phase 3.1) - Leverage multi-core

### Medium Term (Next Month)
5. **R-Tree Alternative** (Phase 2.2) - If quadtree insufficient
6. **Approximate Classification** (Phase 4.2) - Further optimization

### Long Term (Future)
7. **Sweep-Line Algorithm** (Phase 4.1) - If still needed
8. **Vectorized Tests** (Phase 3.2) - Advanced optimization

---

## Expected Combined Performance

**Current**: ~10-12 minutes for 7,200 cells

**After Phase 1** (Bounding Box + Early Termination):
- **Target**: 2-4 minutes (3-5x speedup)

**After Phase 2** (Spatial Indexing):
- **Target**: 30-60 seconds (10-20x speedup)

**After Phase 3** (Parallelization):
- **Target**: 10-30 seconds (20-40x speedup)

**After Phase 4** (Algorithmic):
- **Target**: 5-15 seconds (40-80x speedup)

---

## Testing Strategy

1. **Benchmark Current Performance**: Measure exact time for 7,200 cells
2. **After Each Phase**: Re-measure and verify speedup
3. **Correctness Tests**: Ensure optimization doesn't change results
4. **Memory Profiling**: Monitor memory usage (spatial indexes use more RAM)
5. **Edge Cases**: Test with sparse data, dense urban, mixed environments

---

## Notes

- **Maintainability**: Keep optimizations modular - can disable if issues arise
- **Configuration**: Add flags to enable/disable optimizations (e.g., `USE_SPATIAL_INDEX=True`)
- **Backward Compatibility**: Ensure optimizations work with existing code
- **Documentation**: Document each optimization's trade-offs

---

## Files to Modify

1. `src/agentic_rf_planner/geo/osm_map_provider.py` - Add bounding boxes, spatial index
2. `src/agentic_rf_planner/pipeline/world_builder.py` - Add parallelization, early termination
3. `src/agentic_rf_planner/geo/spatial_index.py` - NEW: Quadtree/R-tree implementation
4. `src/agentic_rf_planner/geo/physical_spanning.py` - Update MapProvider interface if needed

---

## Dependencies to Add (if using R-tree)

```bash
pip install rtree  # For R-tree spatial index (optional)
```

Or implement simple quadtree from scratch (no dependencies).

---

**Status**: Plan complete, ready for implementation when needed.

