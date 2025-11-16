# Dependencies and Environment Setup

This document tracks all dependencies required to replicate the RF Planning environment.

## System Requirements

### Operating System
- **OS**: Linux (tested on Ubuntu/Debian)
- **Architecture**: x86_64 (AMD64)

### Python
- **Version**: Python 3.8+ (tested with Python 3.10)
- **Location**: System Python or virtual environment
- **Package Manager**: pip (comes with Python)

### System Packages (if needed)
- `python3-venv` (for creating virtual environments)
- `build-essential` (for compiling some Python packages)
- `git` (for cloning the repository)

## Python Dependencies

### Dependency Management
- **Primary File**: `pyproject.toml` (PEP 517/518 standard)
- **Alternative**: `requirements.txt` (for pip install -r)
- **Frozen Versions**: `requirements-freeze.txt` (exact versions from current env)
- **Build Backend**: setuptools
- **Installation**: `pip install -e .` (recommended) or `pip install -r requirements.txt`

### Core Dependencies (from pyproject.toml)

```toml
dependencies = [
    "numpy>=1.21.0",              # Numerical computing
    "pillow>=9.0.0",              # Image processing
    "pydantic>=2.0.0",            # Data validation
    "pyyaml>=6.0",                # YAML config parsing
    "transformers>=4.30.0",       # HuggingFace transformers (VLM models)
    "torch>=2.0.0",               # PyTorch (deep learning)
    "accelerate>=0.20.0",         # HuggingFace accelerate (model optimization)
    "sentencepiece>=0.1.99",     # Tokenization
    "protobuf>=3.20.0",           # Protocol buffers
    "huggingface-hub>=0.16.0",   # HuggingFace model hub
    "fastapi>=0.100.0",           # Web API framework
    "uvicorn[standard]>=0.23.0", # ASGI server
    "requests>=2.28.0",           # HTTP requests
]
```

### Optional Development Dependencies

```toml
[project.optional-dependencies]
dev = [
    "pytest>=7.0.0",              # Testing framework
    "pytest-cov>=4.0.0",         # Coverage reporting
    "black>=23.0.0",             # Code formatter
    "ruff>=0.1.0",               # Linter
    "mypy>=1.0.0",               # Type checker
]
```

### GPU Support (Optional)
- **CUDA**: Required for GPU acceleration (if available)
- **PyTorch CUDA**: Automatically installed with `torch>=2.0.0` if CUDA is detected
- **CPU Fallback**: Works without GPU (slower but functional)

## Frontend Dependencies

### JavaScript Libraries (CDN)
- **Leaflet.js**: `1.9.4` (loaded from `https://unpkg.com/leaflet@1.9.4/`)
  - No Node.js/npm required
  - Loaded directly in browser via CDN
  - Used for interactive maps

### Static Files
- **Location**: `src/agentic_rf_planner/ui/static/`
- **Files**: `index.html`, `app.js`, `style.css`
- **No Build Step**: Files served directly by FastAPI

## External Services and APIs

### Required (Free, No API Key)

1. **OSM Overpass API**
   - **URL**: `https://overpass-api.de/api/interpreter`
   - **Purpose**: Building footprints, street data, landcover
   - **Rate Limits**: Public instance, reasonable use expected
   - **Caching**: Implemented in `src/agentic_rf_planner/geo/osm_cache.py`
   - **Cache Location**: `~/.rf_planning_cache/osm_data/`

2. **Bing Maps Tiles**
   - **URL**: `https://ecn.t*.tiles.virtualearth.net/`
   - **Purpose**: Satellite/aerial map tiles
   - **Rate Limits**: Free tier, reasonable use
   - **No API Key**: Required

### Optional (Requires API Key)

1. **Mapillary API** (Street View Panoramas)
   - **URL**: `https://graph.mapillary.com/`
   - **Purpose**: 360° Street View panoramas
   - **API Key**: Required (free tier available)
   - **Environment Variable**: `MAPILLARY_API_KEY`
   - **Setup**:
     ```bash
     export MAPILLARY_API_KEY="your_key_here"
     ```
   - **Get Key**: https://www.mapillary.com/dashboard/developers
   - **Fallback**: System works without it (uses geometry-only mode)

2. **Google Street View API** (Alternative)
   - **URL**: `https://maps.googleapis.com/maps/api/streetview`
   - **Purpose**: Alternative Street View provider
   - **API Key**: Required ($200/month free credit)
   - **Not Currently Implemented**: Mapillary is default

## Environment Variables

### Required
None (all services work without API keys, with reduced functionality)

### Optional
- `MAPILLARY_API_KEY`: Mapillary API key for Street View panoramas
- `TINY_VLM_360_DEVICE`: Override device selection (`cpu`, `cuda`, or `auto`)

## Installation Steps

### 1. Clone Repository
```bash
git clone <repository-url>
cd rf-planning
```

### 2. Create Virtual Environment
```bash
# Create venv (recommended location: ~/RFP)
python3 -m venv ~/RFP

# Activate venv
source ~/RFP/bin/activate
```

### 3. Install Python Dependencies
```bash
# Standard installation (GPU if available, CPU fallback)
pip install -e .

# Or CPU-only (smaller PyTorch, saves ~2GB)
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
pip install -e .
```

### 4. Set Environment Variables (Optional)
```bash
# For Street View panoramas
export MAPILLARY_API_KEY="your_key_here"

# Add to ~/.bashrc or ~/.zshrc for persistence
echo 'export MAPILLARY_API_KEY="your_key_here"' >> ~/.bashrc
```

### 5. Verify Installation
```bash
# Check Python packages
pip list | grep -E "torch|transformers|fastapi"

# Test import
python -c "import agentic_rf_planner; print('OK')"
```

### 6. Start Server
```bash
# Activate venv if not already active
source ~/RFP/bin/activate

# Start development server
uvicorn agentic_rf_planner.api.rest:app --reload

# Or production server
uvicorn agentic_rf_planner.api.rest:app --host 0.0.0.0 --port 8000
```

## Dependency Locations

### Virtual Environment
- **Location**: `~/RFP/` (or custom path)
- **Python**: `~/RFP/bin/python`
- **Packages**: `~/RFP/lib/python3.10/site-packages/`
- **Size**: ~6.9 GB (with GPU support)

### Project Package
- **Location**: Project root (`/path/to/rf-planning/`)
- **Installation Mode**: Editable (`pip install -e .`)
- **Source**: `src/agentic_rf_planner/`

### Cache Directories
- **OSM Cache**: `~/.rf_planning_cache/osm_data/`
- **HuggingFace Models**: `~/.cache/huggingface/` (auto-downloaded)

## Version Compatibility

### Tested Versions
- **Python**: 3.8, 3.9, 3.10, 3.11
- **PyTorch**: 2.0.0+ (with CUDA 12.x support)
- **Transformers**: 4.30.0+
- **FastAPI**: 0.100.0+
- **Uvicorn**: 0.23.0+

### Known Issues
- Python 3.7: Not supported (requires 3.8+)
- CUDA 11.x: May require specific PyTorch version
- Older transformers: May have API changes

## Troubleshooting

### Missing Dependencies
```bash
# Reinstall all dependencies
pip install --upgrade -e .
```

### CUDA Issues
```bash
# Check CUDA availability
python -c "import torch; print(torch.cuda.is_available())"

# Install CPU-only if GPU not needed
pip install torch --index-url https://download.pytorch.org/whl/cpu
```

### API Key Issues
```bash
# Verify environment variable
echo $MAPILLARY_API_KEY

# Test in Python
python -c "import os; print(os.environ.get('MAPILLARY_API_KEY', 'Not set'))"
```

### Port Already in Use
```bash
# Use different port
uvicorn agentic_rf_planner.api.rest:app --port 8001
```

## Dependency Size Estimates

- **Python packages (total)**: ~6.9 GB
- **PyTorch (with CUDA)**: ~3-4 GB
- **Transformers models**: Downloaded on-demand (~1-5 GB per model)
- **OSM cache**: Grows with usage (~10-100 MB per region)

## Minimal Installation (CPU Only)

For systems without GPU or limited disk space:

```bash
# 1. Create venv
python3 -m venv ~/RFP
source ~/RFP/bin/activate

# 2. Install CPU-only PyTorch (saves ~2GB)
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu

# 3. Install rest of dependencies
pip install -e .

# 4. Set device to CPU in config
# Edit configs/default.yaml: device: cpu
```

## Production Deployment

### Additional Considerations
- **Reverse Proxy**: Nginx or Apache (recommended)
- **Process Manager**: systemd, supervisor, or PM2
- **SSL/TLS**: Let's Encrypt certificates
- **Database**: None required (stateless API)
- **File Storage**: Local filesystem for cache

### Docker (Future)
Dockerfile not yet provided, but can be created using this dependency list.

## Summary

**Minimum Requirements:**
- Python 3.8+
- pip
- Internet connection (for CDN and APIs)
- ~7 GB disk space

**Recommended:**
- Python 3.10+
- CUDA-capable GPU (for faster VLM inference)
- 16+ GB RAM (for large models)
- Mapillary API key (for Street View)

**No Additional Requirements:**
- Node.js/npm (frontend uses CDN)
- Database (stateless application)
- System packages (beyond Python)
- Build tools (editable install)

