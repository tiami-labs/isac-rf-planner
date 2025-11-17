"""FastAPI REST API for RF planning."""

import logging
import sys
from pathlib import Path
from typing import Dict, Any, Optional, List

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ..pipeline.schemas import RFParams
from ..agents.rf_planning_agent import run_rf_planning_for_point

# Configure logging to show INFO and above, with detailed format
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
    force=True,  # Override any existing config
)

# Set specific loggers to DEBUG for detailed tracing
logging.getLogger("agentic_rf_planner").setLevel(logging.DEBUG)
logging.getLogger("agentic_rf_planner.agents").setLevel(logging.DEBUG)
logging.getLogger("agentic_rf_planner.geo").setLevel(logging.DEBUG)
logging.getLogger("agentic_rf_planner.api").setLevel(logging.DEBUG)

app = FastAPI(title="Agentic RF Planner API")

# CORS – keep it permissive for local dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class PlanRequest(BaseModel):
    """Request model for RF planning."""

    lat: float
    lon: float
    freq_mhz: float = 3500.0
    tx_power_dbm: float = 43.0  # eNodeB-ish default
    noise_floor_dbm: Optional[float] = None  # If None, calculated from bandwidth + NF
    noise_figure_db: float = 7.0  # Receiver noise figure (typical: 5-10 dB)
    
    # Sector configuration (optional - if None, uses omnidirectional)
    sectors: Optional[List[Dict[str, Any]]] = None  # List of sector configs
    
    # OFDM parameters
    subcarrier_spacing_khz: float = 15.0
    num_resource_blocks: int = 100
    channel_bandwidth_mhz: float = 20.0
    
    # MIMO parameters
    num_tx_antennas: int = 1
    num_rx_antennas: int = 1
    mimo_mode: str = "SISO"  # SISO, SIMO, MISO, MIMO
    
    # Link adaptation
    enable_link_adaptation: bool = True
    fixed_modulation: Optional[str] = None


@app.post("/api/plan")
async def api_plan(req: PlanRequest) -> Dict[str, Any]:
    """
    Run RF planning for a given point.

    Args:
        req: Planning request with lat/lon and RF parameters

    Returns:
        RF planning results including grid and heatmap data
    """
    from fastapi import HTTPException
    import logging
    
    logger = logging.getLogger(__name__)
    
    logger.info("="*60)
    logger.info(f"API REQUEST: /api/plan")
    logger.info(f"  lat: {req.lat}")
    logger.info(f"  lon: {req.lon}")
    logger.info(f"  freq_mhz: {req.freq_mhz}")
    logger.info(f"  tx_power_dbm: {req.tx_power_dbm}")
    logger.info("="*60)
    
    try:
        logger.info("Step 1: Creating RFParams...")
        rf_params = RFParams(
            freq_mhz=req.freq_mhz,
            tx_power_dbm=req.tx_power_dbm,
            noise_floor_dbm=req.noise_floor_dbm,
            noise_figure_db=req.noise_figure_db,
            subcarrier_spacing_khz=req.subcarrier_spacing_khz,
            num_resource_blocks=req.num_resource_blocks,
            channel_bandwidth_mhz=req.channel_bandwidth_mhz,
            num_tx_antennas=req.num_tx_antennas,
            num_rx_antennas=req.num_rx_antennas,
            mimo_mode=req.mimo_mode,
            enable_link_adaptation=req.enable_link_adaptation,
            fixed_modulation=req.fixed_modulation,
            sectors=req.sectors,  # Pass sector configurations
        )
        logger.info(f"  RFParams created: freq={rf_params.freq_mhz}MHz, power={rf_params.tx_power_dbm}dBm")
        if req.sectors:
            logger.info(f"  Sectors: {len(req.sectors)} sector(s) configured")
        else:
            logger.info(f"  Sectors: Omnidirectional (360°)")
        logger.info(f"  OFDM: SCS={rf_params.subcarrier_spacing_khz}kHz, RB={rf_params.num_resource_blocks}, BW={rf_params.channel_bandwidth_mhz}MHz")
        logger.info(f"  MIMO: {rf_params.mimo_mode} ({rf_params.num_tx_antennas}x{rf_params.num_rx_antennas})")
        logger.info(f"  Link adaptation: {'enabled' if rf_params.enable_link_adaptation else 'disabled'}")
        
        logger.info("Step 2: Calling run_rf_planning_for_point...")
        # Run in executor to avoid blocking the event loop during long processing
        import asyncio
        from concurrent.futures import ThreadPoolExecutor
        
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as executor:
            result = await loop.run_in_executor(
                executor,
                lambda: run_rf_planning_for_point(
                    lat=req.lat,
                    lon=req.lon,
                    rf_params=rf_params,
                )
            )
        logger.info("Step 3: RF planning completed successfully")
        logger.info(f"  Result keys: {list(result.keys())}")
        return result
    except ValueError as e:
        logger.error(f"ValueError in RF planning: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"Unexpected error in RF planning: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@app.get("/api/health")
def health() -> Dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


@app.post("/api/clear-cache")
async def clear_cache(req: Request) -> Dict[str, Any]:
    """
    Clear backend caches.
    
    Request body (optional JSON):
        {
            "clear_osm": true/false  # If True, clears persistent OSM cache. Default: False
        }
    
    Note: OSM cache is preserved by default to avoid repeated API calls for the same region.
          Set clear_osm=true only if you need to force fresh data.
    """
    logger = logging.getLogger(__name__)
    
    # Parse request body (if provided)
    clear_osm = False
    try:
        body = await req.json()
        if isinstance(body, dict):
            clear_osm = body.get("clear_osm", False)
    except:
        # No body provided, use default (preserve OSM cache)
        pass
    
    result = {
        "status": "ok",
        "message": "Cache cleared",
        "osm_cache_cleared": False,
    }
    
    # Only clear OSM cache if explicitly requested
    if clear_osm:
        from ..geo.osm_cache import clear_cache, get_cache_stats
        
        stats_before = get_cache_stats()
        deleted = clear_cache()  # Clear all cache
        
        logger.info(f"OSM cache clear requested - deleted {deleted} file(s)")
        
        result.update({
            "message": f"OSM cache cleared - deleted {deleted} file(s)",
            "osm_cache_cleared": True,
            "cache_stats_before": stats_before,
            "files_deleted": deleted,
        })
    else:
        logger.info("Cache clear requested - OSM cache preserved (use clear_osm=true to clear)")
        result["message"] = "In-memory cache cleared. OSM cache preserved (to avoid repeated API calls)."
    
    return result


# Serve frontend static files - explicit routes for known files only
static_dir = Path(__file__).parent.parent / "ui" / "static"
if static_dir.exists():
    from fastapi.responses import FileResponse
    
    @app.get("/")
    async def serve_index():
        """Serve index.html."""
        index_path = static_dir / "index.html"
        if index_path.exists():
            return FileResponse(str(index_path))
        from fastapi import HTTPException
        raise HTTPException(status_code=404)
    
    # Explicit routes for known static files
    @app.get("/index.html")
    async def serve_index_html():
        index_path = static_dir / "index.html"
        if index_path.exists():
            return FileResponse(str(index_path))
        from fastapi import HTTPException
        raise HTTPException(status_code=404)
    
    @app.get("/app.js")
    async def serve_app_js():
        file_path = static_dir / "app.js"
        if file_path.exists():
            return FileResponse(str(file_path))
        from fastapi import HTTPException
        raise HTTPException(status_code=404)
    
    @app.get("/style.css")
    async def serve_style_css():
        file_path = static_dir / "style.css"
        if file_path.exists():
            return FileResponse(str(file_path))
        from fastapi import HTTPException
        raise HTTPException(status_code=404)

