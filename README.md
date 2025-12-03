# Agentic RF Planner

RF planning system that uses vision-language models (VLMs) to detect materials in 360° panoramas and compute RF signal coverage heatmaps.

![RF Planning Interface](planner.png)

## Overview

This project processes equirectangular 360° panoramas by:
1. Snapping user-selected (lat, lon) to nearest street point
2. Fetching 360° panorama at that location
3. Tiling panorama into overlapping views
4. Running VLM (MiniCPM-V 2.0 or Qwen2-VL-2B) to detect static materials (buildings, trees, structures)
5. Building a world model with material obstacles
6. Computing RF attenuation using free-space path loss + material-dependent losses
7. Generating a heatmap overlay showing signal strength

## Architecture

- **Geo**: Street snapping, panorama fetching, coverage grid generation
- **Vision**: Panorama tiling, VLM-based material extraction
- **RF**: Material RF properties, attenuation calculations
- **Pipeline**: World model building, end-to-end orchestration
- **Agents**: Main RF planning orchestrator
- **API**: FastAPI REST API for web interface
- **UI**: Web-based map interface with Leaflet

## Installation

> **📋 For complete dependency tracking and replication instructions, see [DEPENDENCIES.md](DEPENDENCIES.md)**

### What You Need Beyond `requirements.txt`

Besides installing Python dependencies, you need:

1. **Configuration Files** (included in repo):
   - `configs/default.yaml` - Global settings
   - `configs/models.*.yaml` - VLM model configurations
   - `configs/rf.params.yaml` - RF simulation parameters
   - These are already in the repository, no action needed

2. **Virtual Environment** (recommended):
   ```bash
   python3 -m venv ~/RFP
   source ~/RFP/bin/activate
   ```

3. **Editable Package Installation**:
   ```bash
   pip install -e .  # Instead of just pip install -r requirements.txt
   ```
   This installs the package in editable mode so the code is importable.
   - **Note**: `uvicorn` (ASGI server) is included in dependencies and will be installed automatically
   - You'll use `uvicorn` to run the server: `uvicorn agentic_rf_planner.api.rest:app --reload`

   **If uvicorn is missing**, install it explicitly:
   ```bash
   pip install "uvicorn[standard]>=0.23.0"
   ```
   Or install all dependencies: `pip install -r requirements.txt` or `pip install -e .`

4. **Optional: Mapillary API Key** (for Street View panoramas):
   ```bash
   export MAPILLARY_API_KEY="your_key_here"
   ```
   - Get free key: https://www.mapillary.com/dashboard/developers
   - **Not required**: System works without it (uses geometry-only mode)

5. **Internet Connection** (for first run):
   - Downloads VLM models from HuggingFace (auto-downloaded on first use)
   - Accesses OSM Overpass API for building data (cached locally)
   - Loads map tiles from Bing Maps (CDN)

6. **Disk Space**: ~7 GB for Python packages + model downloads

**That's it!** No database, no Node.js, no build tools needed.

### Quick Replication

**Fastest way to replicate the environment:**

```bash
# 1. Clone repository
git clone <repository-url>
cd rf-planning

# 2. Create virtual environment
python3 -m venv ~/RFP
source ~/RFP/bin/activate

# 3. Install dependencies (editable install - recommended)
pip install -e .

# 4. Optional: Set Mapillary API key for Street View panoramas
export MAPILLARY_API_KEY="your_key_here"
# Get key from: https://www.mapillary.com/dashboard/developers

# 5. Start server
uvicorn agentic_rf_planner.api.rest:app --reload   
```

**Alternative installation methods:**
```bash
# Using requirements.txt
pip install -r requirements.txt

# For exact version replication (may not work on all systems)
pip install -r requirements-freeze.txt
```

### Installation Options

**Standard Installation (GPU if available, CPU fallback):**
```bash
# Create virtual environment (recommended)
python3 -m venv ~/RFP
source ~/RFP/bin/activate

# Install dependencies
pip install -e .
```

**CPU-Only Installation (smaller PyTorch, no CUDA):**
```bash
# Create virtual environment
python3 -m venv ~/RFP
source ~/RFP/bin/activate

# Install PyTorch CPU-only version (optional, saves ~2GB disk space)
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu

# Install the rest
pip install -e .
```

The default config uses `device: auto` which:
- **Automatically detects and uses GPU** if CUDA is available
- **Falls back to CPU** if no GPU is available
- Works seamlessly on both GPU and CPU-only systems

## Quick Start

### Web GUI

```bash
# Start the web server
uvicorn agentic_rf_planner.api.rest:app --reload

# Or use the wrapper
python -m agentic_rf_planner.ui.web_app
```

Then open http://localhost:8000 in your browser and click on the map to run RF planning.

## Sector Configuration

The RF planner supports three types of sectors for defining coverage areas:

### Sector Types

1. **Omnidirectional (360°)** - Full circular coverage around the TX point
2. **Angle-based** - Sector defined by start and end angles (e.g., 0° to 120°)
3. **Abstract Polygon** - Custom-shaped coverage area defined by user-drawn polygon

### Drawing Sectors

#### Basic Workflow

1. **Set TX Location**: Click on the map to set the transmitter location (or enter coordinates manually)
2. **Add Sectors**: Click "+ Add Sector" button in the sidebar
3. **Configure Sector**: Select sector type and configure parameters:
   - **Frequency (MHz)**: Operating frequency (e.g., 622, 3500)
   - **TX Power (dBm)**: Transmitter power (e.g., 43 dBm)
   - **Sector Type**: Choose from 360°, angle-based, or polygon

#### Drawing Polygon Sectors

For custom polygon sectors:

1. **Select Polygon Type**: In the sector configuration, select "Abstract Polygon"
2. **Start Drawing**: Click "Draw Polygon" button
3. **Set TX Point**: 
   - If TX location is already set, the first click adds the first polygon vertex
   - If no TX location is set, the first click sets the TX point (origin of polygon)
4. **Add Vertices**: Continue clicking on the map to add polygon vertices
   - **Minimum**: 2 clicked points (TX origin is the third point, total of 3)
   - The system automatically resolves self-intersecting or disconnected polygons into a single contiguous shape using convex hull
5. **Finish Drawing**: 
   - Click "Finish Drawing" button, OR
   - Right-click or double-click on the map
   - The "Finish Drawing" button highlights when you have at least 3 points total
6. **Run Planning**: Click "Plan RF" to generate coverage for the polygon sector

#### Sector Visualization

- **Sector Overlays**: Visual indicators show sector boundaries on the map:
  - Circles for omnidirectional (360°) sectors
  - Cones/arcs for angle-based sectors
  - Polygons for abstract polygon sectors
- **Toggle Visibility**: Use the "Show Sector Overlays" checkbox to hide/show sector boundaries
- **Multiple Sectors**: You can add multiple sectors with different frequencies, powers, and shapes
- **Multiple Heatmaps**: Each RF plan creates a new heatmap layer, allowing comparison of coverage from different TX locations

### Assessing Coverage

After running RF planning:

- **Heatmap Colors**: Signal strength is color-coded:
  - **Red/Orange**: Strong signal (close to TX, good coverage)
  - **Yellow/Green**: Moderate signal
  - **Blue/Cyan**: Weak signal (far from TX, poor coverage)
- **RSRP Legend**: Shows the actual signal strength range for the current heatmap
- **Multiple Plans**: Click new points and run "Plan RF" to compare coverage from different locations (previous heatmaps are preserved)
- **Clear Map**: Use "Clear Map" button to remove all heatmaps and start fresh

### Tips

- **Polygon Drawing**: The TX point is always the origin of polygon sectors. Draw vertices relative to where you want coverage
- **Frequency Impact**: Lower frequencies (e.g., 622 MHz) propagate farther than higher frequencies (e.g., 3500 MHz)
- **Material Attenuation**: The system models building penetration loss, so coverage inside buildings will be weaker
- **Ray Termination**: Rays stop when signal strength drops below -110 dBm (realistic detection threshold)

### CLI

```bash
# Place a panorama image in data/panos/
python -m agentic_rf_planner.cli 37.7749 -122.4194 \
    --config configs/default.yaml \
    --model-config configs/models.minicpm.yaml \
    --rf-config configs/rf.params.yaml
```

## Configuration

- `configs/default.yaml` - Global settings (tiling, logging, device: cpu/auto/cuda)
- `configs/models.*.yaml` - VLM model configurations
- `configs/rf.params.yaml` - RF simulation parameters (frequency, power, range)

### CPU-Only Configuration

Set `device: cpu` in `configs/default.yaml` or use environment variable:
```bash
export TINY_VLM_360_DEVICE=cpu
```

## Development

The system is designed for incremental development:
1. Stub map provider → make pipeline work
2. Stub VLM with hardcoded segments → make RF & heatmap work
3. Plug in real VLM when ready
4. Add real map integration later

## Key Design Principles

- **Feasibility first**: Build in days, not a research project
- **Clear separation**: Perceptual (VLM) vs Physical (RF)
- **Incremental**: Stub → Make it work → Refine
- **Debuggable**: Clear data flow, simple structures

## Device Configuration

- **Default (`device: auto`)**: Automatically uses GPU if available, falls back to CPU
- **CPU-only systems**: Work out of the box - `auto` will detect no GPU and use CPU
- **Force CPU**: Set `device: cpu` in config if you want to force CPU even with GPU available
- **Force GPU**: Set `device: cuda` in config to require GPU (will error if unavailable)

## Street View Configuration

The system uses **Mapillary** for real Street View panoramas (free API, requires API key).

**To get a Mapillary API key:**
1. Go to https://www.mapillary.com/dashboard/developers
2. Sign up (free) and create an API key
3. Set it as environment variable: `export MAPILLARY_API_KEY=your_key_here`
4. Or pass it when creating provider: `create_streetview_provider("mapillary", api_key="your_key")`

**Alternative providers:**
- `"file"`: Load from local files in `data/panos/` (for testing)
- `"mapillary"`: Real Street View API (default, requires API key)
- `"google"`: Google Street View API (requires API key, $200/month free credit)

### CPU Compatibility Notes

- All dependencies are CPU-compatible
- Models use `float32` on CPU (vs `float16` on GPU) for compatibility
- Performance will be slower on CPU, but fully functional
- For CPU-only PyTorch installation, use the CPU-only index (see Installation)

