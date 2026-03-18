"""FastAPI REST API for RF planning."""

import logging
import sys
import os
from pathlib import Path
from typing import Dict, Any, Optional, List

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ..config import load_rf_config
from ..pipeline.schemas import RFParams, LatLon
from ..agents.rf_planning_agent import run_rf_planning_for_point

from ..geo.google_mesh import RayProfileSet, MeshProfileStore, PROFILE_VERSION
from ..geo.google_mesh.provider import MissingMeshProfiles
from ..geo.road_labels import fetch_road_labels


# FastAPI application must be created before route decorators are evaluated.
app = FastAPI(title="Agentic RF Planner API")


class RaytraceDebugRequest(BaseModel):
    """Request model for debug ray/path rendering in the UI."""

    lat: float
    lon: float
    ray_mode: str = "3d_rt"  # only used to gate behavior client-side
    tx_height_m: float = 10.0
    rx_height_m: float = 1.5
    freq_mhz: float = 3500.0
    tx_power_dbm: float = 43.0
    max_range_m: float = 800.0
    step_m: float = 10.0
    bearing_stride_deg: float = 30.0
    termination_rsrp_dbm: float = -140.0
    max_reflections_per_sample: int = 1
    max_wall_candidates: int = 40
    reflection_loss_db: float = 8.0
    sample_stride: int = 25


@app.post("/api/raytrace_debug")
async def api_raytrace_debug(req: RaytraceDebugRequest) -> Dict[str, Any]:
    """Compute a small set of multipath rays for UI debugging/visualization."""
    from ..geo.osm_map_provider import OSMMapProvider
    from ..pipeline.schemas import LatLon
    from ..rf.ray_tracing import compute_single_bounce_paths, extract_wall_segments
    from ..geo.coverage_grid import _project_from_tx, _enu_from_tx  # type: ignore
    from ..rf.attenuation_models import (
        _horizontal_pattern_attenuation_db,
        _reference_signal_eirp_dbm,
        _resolve_sector_params,
        _scenario_path_loss_db,
        _vertical_pattern_attenuation_db,
    )
    import math

    tx = LatLon(lat=req.lat, lon=req.lon)
    # Build minimal RFParams for path-loss computations.
    rf_params = RFParams(
        freq_mhz=req.freq_mhz,
        tx_power_dbm=req.tx_power_dbm,
        max_range_m=req.max_range_m,
        step_m=req.step_m,
        dtheta_deg=req.bearing_stride_deg,
        ray_mode="3d_rt",
        tx_height_m=req.tx_height_m,
        rx_height_m=req.rx_height_m,
        termination_rsrp_dbm=req.termination_rsrp_dbm,
        rt_max_reflections_per_sample=req.max_reflections_per_sample,
        rt_max_wall_candidates=req.max_wall_candidates,
        rt_reflection_loss_db=req.reflection_loss_db,
        rt_debug_sample_stride=req.sample_stride,
    )

    osm = OSMMapProvider(cache_radius_m=1000.0)
    osm.prefetch_all_data(tx, req.max_range_m + 50.0)

    wall_segments_all = extract_wall_segments(getattr(osm, "_cached_buildings", []) or [], tx)

    # Omnidirectional sector parameters.
    sector_params = _resolve_sector_params(
        type(
            "RaytraceDebugSector",
            (),
            {
                "sector_id": "omnidirectional",
                "sector_freq_mhz": req.freq_mhz,
                "sector_tx_power_dbm": req.tx_power_dbm,
                "sector_channel_bandwidth_mhz": 20.0,
                "sector_azimuth_deg": None,
                "sector_beamwidth_h_deg": None,
                "sector_beamwidth_v_deg": None,
                "sector_electrical_tilt_deg": None,
                "sector_mechanical_tilt_deg": None,
                "sector_max_horizontal_attenuation_db": None,
                "sector_front_to_back_attenuation_db": None,
                "sector_max_vertical_attenuation_db": None,
            },
        )(),
        rf_params,
        {},
    )
    rs_eirp_dbm = _reference_signal_eirp_dbm(rf_params, tx_power_dbm_override=req.tx_power_dbm)
    rx_gain_db = float(getattr(rf_params, "ue_antenna_gain_dbi", 0.0) or 0.0)
    dz = float(req.tx_height_m) - float(req.rx_height_m)
    dz2 = dz * dz

    def is_path_clear(p0: LatLon, p1: LatLon, exclude_id: int | None) -> bool:
        hits = osm.get_buildings_along_ray(p0, p1)
        for b in hits:
            bid = b.get("id")
            try:
                bid_int = int(bid) if bid is not None else None
            except Exception:
                bid_int = None
            if exclude_id is not None and bid_int == exclude_id:
                continue
            return False
        return True

    out_paths: list[dict[str, Any]] = []

    # Generate bearings.
    bstride = float(req.bearing_stride_deg)
    if bstride <= 0.0:
        bstride = 30.0
    bearings = []
    th = 0.0
    while th < 360.0 - 1e-6:
        bearings.append(th)
        th += bstride

    # For each bearing, sample points along range.
    dr = float(req.step_m)
    stride = max(1, int(req.sample_stride))
    for theta in bearings:
        r = dr
        while r <= float(req.max_range_m):
            if int(round(r / dr)) % stride != 0:
                r += dr
                continue
            lat, lon = _project_from_tx(tx.lat, tx.lon, r, theta)
            rx = LatLon(lat=lat, lon=lon)

            # Candidate walls near RX.
            wall_segments = wall_segments_all

            # Direct-path RSRP (free-space/scenario LOS as a baseline for visualization).
            d2 = max(1.0, r)
            d3 = math.sqrt(d2 * d2 + dz2)
            pl = _scenario_path_loss_db(
                distance_2d_m=d2,
                distance_3d_m=d3,
                freq_mhz=req.freq_mhz,
                tx_height_m=req.tx_height_m,
                rx_height_m=req.rx_height_m,
                is_los=True,
                rf_params=rf_params,
            )
            vpat = _vertical_pattern_attenuation_db(
                distance_m=d2,
                tx_height_m=req.tx_height_m,
                rx_height_m=req.rx_height_m,
                rf_params=rf_params,
                sector_params=sector_params,
            )
            hpat = _horizontal_pattern_attenuation_db(
                bearing_deg=theta,
                sector_params=sector_params,
                rf_params=rf_params,
            )
            direct_rsrp = rs_eirp_dbm - (pl + hpat + vpat) + rx_gain_db
            out_paths.append(
                {
                    "kind": "direct",
                    "bearing_deg": theta,
                    "rsrp_dbm": direct_rsrp,
                    "points": [
                        {"lat": tx.lat, "lon": tx.lon, "h": req.tx_height_m},
                        {"lat": rx.lat, "lon": rx.lon, "h": req.rx_height_m},
                    ],
                }
            )

            def rsrp_for_reflection(tx_ll: LatLon, bounce_ll: LatLon, rx_ll: LatLon, total_d: float, refl_loss: float) -> float:
                be, bn = _enu_from_tx(tx_ll, bounce_ll)
                brg = (math.degrees(math.atan2(be, bn)) + 360.0) % 360.0
                d2r = max(1.0, float(total_d))
                d3r = math.sqrt(d2r * d2r + dz2)
                plr = _scenario_path_loss_db(
                    distance_2d_m=d2r,
                    distance_3d_m=d3r,
                    freq_mhz=req.freq_mhz,
                    tx_height_m=req.tx_height_m,
                    rx_height_m=req.rx_height_m,
                    is_los=True,
                    rf_params=rf_params,
                )
                vpatr = _vertical_pattern_attenuation_db(
                    distance_m=d2r,
                    tx_height_m=req.tx_height_m,
                    rx_height_m=req.rx_height_m,
                    rf_params=rf_params,
                    sector_params=sector_params,
                )
                hpatr = _horizontal_pattern_attenuation_db(
                    bearing_deg=brg,
                    sector_params=sector_params,
                    rf_params=rf_params,
                )
                return rs_eirp_dbm - (plr + refl_loss + hpatr + vpatr) + rx_gain_db

            refl_paths = compute_single_bounce_paths(
                tx,
                rx,
                wall_segments,
                max_candidates=req.max_wall_candidates,
                max_return=req.max_reflections_per_sample,
                rf_params=rf_params,
                is_path_clear_fn=is_path_clear,
                rsrp_for_path_fn=rsrp_for_reflection,
            )
            for p in refl_paths:
                out_paths.append(
                    {
                        "kind": "reflect",
                        "bearing_deg": theta,
                        "rsrp_dbm": p.rsrp_dbm,
                        "points": [
                            {"lat": p.points[0].lat, "lon": p.points[0].lon, "h": req.tx_height_m},
                            {"lat": p.points[1].lat, "lon": p.points[1].lon, "h": req.rx_height_m},
                            {"lat": p.points[2].lat, "lon": p.points[2].lon, "h": req.rx_height_m},
                        ],
                    }
                )
            r += dr

    return {"tx": {"lat": tx.lat, "lon": tx.lon}, "paths": out_paths}

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

    # Ray propagation mode selection
    # If omitted, defaults to "2d". UI controls this via the ray-mode selector.
    ray_mode: Optional[str] = None  # "2d" or "3d"
    tx_height_m: float = 0.0
    rx_height_m: float = 1.5
    tx_antenna_gain_dbi: Optional[float] = None
    tx_feeder_loss_db: Optional[float] = None
    reference_signal_offset_db: Optional[float] = None
    ue_antenna_gain_dbi: Optional[float] = None
    max_rsrp_dbm: Optional[float] = None
    electrical_tilt_deg: Optional[float] = None
    mechanical_tilt_deg: Optional[float] = None
    vertical_beamwidth_deg: Optional[float] = None
    max_vertical_attenuation_db: Optional[float] = None
    max_horizontal_attenuation_db: Optional[float] = None
    front_to_back_attenuation_db: Optional[float] = None
    path_loss_model: Optional[str] = None
    propagation_scenario: Optional[str] = None
    shadow_loss_db: Optional[float] = None
    shadow_decay_db_per_100m: Optional[float] = None
    shadow_loss_cap_db: Optional[float] = None
    diffraction_base_loss_db: Optional[float] = None
    diffraction_slope_db_per_100m: Optional[float] = None
    diffraction_loss_cap_db: Optional[float] = None
    canyon_recovery_max_db: Optional[float] = None
    canyon_recovery_slope_db_per_100m: Optional[float] = None
    termination_rsrp_dbm: Optional[float] = None
    building_attenuation: Optional[Dict[str, Any]] = None  # Override config; { materials: {...}, overall: {...} }

    # 3D multipath ray tracing (ray_mode=3d_rt)
    rt_max_bounces: Optional[int] = None
    rt_max_reflections_per_sample: Optional[int] = None
    rt_max_wall_candidates: Optional[int] = None
    rt_reflection_loss_db: Optional[float] = None
    rt_debug_sample_stride: Optional[int] = None

    # Coverage / grid resolution (optional overrides; defaults match RFParams)
    # In 3D mode these must match the mesh-profile cache key that the UI
    # generates (max_range_m, step_m, dtheta_deg).
    max_range_m: Optional[float] = None
    step_m: Optional[float] = None
    dtheta_deg: Optional[float] = None


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
        # Use UI-provided ray_mode if present, otherwise default to 2d (ignore env var)
        # The UI toggle should control this, not an environment variable
        effective_ray_mode = (req.ray_mode or "2d").strip().lower()
        # Load RF config for building attenuation etc. (config-driven, no code changes needed)
        try:
            rf_cfg = load_rf_config("configs/rf.params.yaml")
            if not rf_cfg:
                logger.warning("RF config not found (configs/rf.params.yaml); building_attenuation will be None")
            elif "building_attenuation" not in rf_cfg:
                logger.warning("RF config has no building_attenuation; penetration loss will use hardcoded defaults")
        except Exception as e:
            rf_cfg = {}
            logger.warning("Failed to load RF config: %s", e)
        rf_params = RFParams(
            freq_mhz=req.freq_mhz,
            tx_power_dbm=req.tx_power_dbm,
            noise_floor_dbm=req.noise_floor_dbm,
            noise_figure_db=req.noise_figure_db,
            max_range_m=(req.max_range_m if req.max_range_m is not None else rf_cfg.get("max_range_m", RFParams.model_fields["max_range_m"].default)),
            step_m=(req.step_m if req.step_m is not None else RFParams.model_fields["step_m"].default),
            dtheta_deg=(req.dtheta_deg if req.dtheta_deg is not None else RFParams.model_fields.get("dtheta_deg").default if "dtheta_deg" in RFParams.model_fields else 5.0),
            subcarrier_spacing_khz=req.subcarrier_spacing_khz,
            num_resource_blocks=req.num_resource_blocks,
            channel_bandwidth_mhz=req.channel_bandwidth_mhz,
            num_tx_antennas=req.num_tx_antennas,
            num_rx_antennas=req.num_rx_antennas,
            mimo_mode=req.mimo_mode,
            enable_link_adaptation=req.enable_link_adaptation,
            fixed_modulation=req.fixed_modulation,
            sectors=req.sectors,  # Pass sector configurations
            ray_mode=effective_ray_mode,
            tx_height_m=req.tx_height_m,
            rx_height_m=req.rx_height_m,
            tx_antenna_gain_dbi=(req.tx_antenna_gain_dbi if req.tx_antenna_gain_dbi is not None else rf_cfg.get("tx_antenna_gain_dbi", RFParams.model_fields["tx_antenna_gain_dbi"].default)),
            tx_feeder_loss_db=(req.tx_feeder_loss_db if req.tx_feeder_loss_db is not None else rf_cfg.get("tx_feeder_loss_db", RFParams.model_fields["tx_feeder_loss_db"].default)),
            reference_signal_offset_db=(req.reference_signal_offset_db if req.reference_signal_offset_db is not None else rf_cfg.get("reference_signal_offset_db", RFParams.model_fields["reference_signal_offset_db"].default)),
            ue_antenna_gain_dbi=(req.ue_antenna_gain_dbi if req.ue_antenna_gain_dbi is not None else rf_cfg.get("ue_antenna_gain_dbi", RFParams.model_fields["ue_antenna_gain_dbi"].default)),
            max_rsrp_dbm=(req.max_rsrp_dbm if req.max_rsrp_dbm is not None else rf_cfg.get("max_rsrp_dbm", RFParams.model_fields["max_rsrp_dbm"].default)),
            electrical_tilt_deg=(req.electrical_tilt_deg if req.electrical_tilt_deg is not None else rf_cfg.get("electrical_tilt_deg", RFParams.model_fields["electrical_tilt_deg"].default)),
            mechanical_tilt_deg=(req.mechanical_tilt_deg if req.mechanical_tilt_deg is not None else rf_cfg.get("mechanical_tilt_deg", RFParams.model_fields["mechanical_tilt_deg"].default)),
            vertical_beamwidth_deg=(req.vertical_beamwidth_deg if req.vertical_beamwidth_deg is not None else rf_cfg.get("vertical_beamwidth_deg", RFParams.model_fields["vertical_beamwidth_deg"].default)),
            max_vertical_attenuation_db=(req.max_vertical_attenuation_db if req.max_vertical_attenuation_db is not None else rf_cfg.get("max_vertical_attenuation_db", RFParams.model_fields["max_vertical_attenuation_db"].default)),
            max_horizontal_attenuation_db=(req.max_horizontal_attenuation_db if req.max_horizontal_attenuation_db is not None else rf_cfg.get("max_horizontal_attenuation_db", RFParams.model_fields["max_horizontal_attenuation_db"].default)),
            front_to_back_attenuation_db=(req.front_to_back_attenuation_db if req.front_to_back_attenuation_db is not None else rf_cfg.get("front_to_back_attenuation_db", RFParams.model_fields["front_to_back_attenuation_db"].default)),
            path_loss_model=(req.path_loss_model if req.path_loss_model is not None else rf_cfg.get("path_loss_model", RFParams.model_fields["path_loss_model"].default)),
            propagation_scenario=(req.propagation_scenario if req.propagation_scenario is not None else rf_cfg.get("propagation_scenario", RFParams.model_fields["propagation_scenario"].default)),
            shadow_loss_db=(req.shadow_loss_db if req.shadow_loss_db is not None else rf_cfg.get("shadow_loss_db", RFParams.model_fields["shadow_loss_db"].default)),
            shadow_decay_db_per_100m=(req.shadow_decay_db_per_100m if req.shadow_decay_db_per_100m is not None else rf_cfg.get("shadow_decay_db_per_100m", RFParams.model_fields["shadow_decay_db_per_100m"].default)),
            shadow_loss_cap_db=(req.shadow_loss_cap_db if req.shadow_loss_cap_db is not None else rf_cfg.get("shadow_loss_cap_db", RFParams.model_fields["shadow_loss_cap_db"].default)),
            diffraction_base_loss_db=(req.diffraction_base_loss_db if req.diffraction_base_loss_db is not None else rf_cfg.get("diffraction_base_loss_db", RFParams.model_fields["diffraction_base_loss_db"].default)),
            diffraction_slope_db_per_100m=(req.diffraction_slope_db_per_100m if req.diffraction_slope_db_per_100m is not None else rf_cfg.get("diffraction_slope_db_per_100m", RFParams.model_fields["diffraction_slope_db_per_100m"].default)),
            diffraction_loss_cap_db=(req.diffraction_loss_cap_db if req.diffraction_loss_cap_db is not None else rf_cfg.get("diffraction_loss_cap_db", RFParams.model_fields["diffraction_loss_cap_db"].default)),
            canyon_recovery_max_db=(req.canyon_recovery_max_db if req.canyon_recovery_max_db is not None else rf_cfg.get("canyon_recovery_max_db", RFParams.model_fields["canyon_recovery_max_db"].default)),
            canyon_recovery_slope_db_per_100m=(req.canyon_recovery_slope_db_per_100m if req.canyon_recovery_slope_db_per_100m is not None else rf_cfg.get("canyon_recovery_slope_db_per_100m", RFParams.model_fields["canyon_recovery_slope_db_per_100m"].default)),
            termination_rsrp_dbm=(req.termination_rsrp_dbm if req.termination_rsrp_dbm is not None else rf_cfg.get("termination_rsrp_dbm", RFParams.model_fields["termination_rsrp_dbm"].default)),
            building_attenuation=(req.building_attenuation if req.building_attenuation is not None else rf_cfg.get("building_attenuation")),

            # Multipath ray tracing knobs (optional overrides)
            rt_max_bounces=(req.rt_max_bounces if req.rt_max_bounces is not None else RFParams.model_fields["rt_max_bounces"].default),
            rt_max_reflections_per_sample=(req.rt_max_reflections_per_sample if req.rt_max_reflections_per_sample is not None else RFParams.model_fields["rt_max_reflections_per_sample"].default),
            rt_max_wall_candidates=(req.rt_max_wall_candidates if req.rt_max_wall_candidates is not None else RFParams.model_fields["rt_max_wall_candidates"].default),
            rt_reflection_loss_db=(req.rt_reflection_loss_db if req.rt_reflection_loss_db is not None else RFParams.model_fields["rt_reflection_loss_db"].default),
            rt_debug_sample_stride=(req.rt_debug_sample_stride if req.rt_debug_sample_stride is not None else RFParams.model_fields["rt_debug_sample_stride"].default),
        )
        logger.info(f"  RFParams created: freq={rf_params.freq_mhz}MHz, power={rf_params.tx_power_dbm}dBm")
        if req.sectors:
            logger.info(f"  Sectors: {len(req.sectors)} sector(s) configured")
        else:
            logger.info(f"  Sectors: Omnidirectional (360°)")
        logger.info(f"  OFDM: SCS={rf_params.subcarrier_spacing_khz}kHz, RB={rf_params.num_resource_blocks}, BW={rf_params.channel_bandwidth_mhz}MHz")
        logger.info(f"  MIMO: {rf_params.mimo_mode} ({rf_params.num_tx_antennas}x{rf_params.num_rx_antennas})")
        logger.info(f"  Link adaptation: {'enabled' if rf_params.enable_link_adaptation else 'disabled'}")
        logger.info(f"  Ray mode: {rf_params.ray_mode} (tx_h={rf_params.tx_height_m}m, rx_h={rf_params.rx_height_m}m)")
        logger.info(f"  Ray mode: {effective_ray_mode} (tx_height_m={rf_params.tx_height_m}, rx_height_m={rf_params.rx_height_m})")
        
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
    except MissingMeshProfiles as e:
        # 3D mode requires persisted mesh ray profiles; UI will auto-generate.
        logger.error(f"Missing 3D mesh profiles: {e}")
        raise HTTPException(
            status_code=409,
            detail={
                "status": "missing_mesh_profiles",
                "key": e.key,
                "message": str(e),
            },
        )
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


@app.get("/api/config")
def api_config() -> Dict[str, Any]:
    """Expose minimal runtime config needed by local frontend utilities.

    Note: Google Photorealistic 3D Tiles requires the API key client-side.
    This endpoint is intended for local development only.
    """
    key = os.environ.get("GOOGLE_MAPS_API_KEY") or os.environ.get("GOOGLE_MAPS_APIKEY")
    default_ray_mode = "2d"
    rf_cfg = load_rf_config("configs/rf.params.yaml")
    return {
        "google_maps_api_key": key or "",
        "google_maps_api_key_present": bool(key),
        "mesh_profile_version": PROFILE_VERSION,
        "default_ray_mode": default_ray_mode,
        "rf_params": rf_cfg,
    }


@app.get("/api/rf-params")
def api_rf_params() -> Dict[str, Any]:
    """Return RF params from rf.params.yaml for GUI defaults (attenuation knobs, etc.)."""
    return load_rf_config("configs/rf.params.yaml")


@app.get("/api/roads/labels")
def api_road_labels(
    lat: float,
    lon: float,
    radius_m: float = 2500.0,
    major_limit: int = 24,
    minor_limit: int = 40,
) -> Dict[str, Any]:
    """Return named OSM road labels for the 3D map."""
    labels = fetch_road_labels(
        lat,
        lon,
        radius_m=radius_m,
        major_limit=major_limit,
        minor_limit=minor_limit,
    )
    return {
        "status": "ok",
        "labels": labels,
        "query": {
            "lat": lat,
            "lon": lon,
            "radius_m": max(200.0, min(5000.0, float(radius_m))),
            "major_limit": max(0, int(major_limit)),
            "minor_limit": max(0, int(minor_limit)),
        },
    }


@app.post("/api/mesh-profiles/put")
async def mesh_profiles_put(profile_set: RayProfileSet, enrich_osm: bool = True) -> Dict[str, Any]:
    """Persist a full set of mesh ray profiles for a TX/config.

    The frontend is expected to compute mesh intersections (Google 3D mesh) and
    enrich them with OSM semantics (tree/building/house) and material bucket.

    Returns a deterministic cache key that can be referenced later.
    """
    if enrich_osm:
        try:
            from ..geo.google_mesh.osm_enrichment import enrich_profile_set_with_osm

            profile_set = enrich_profile_set_with_osm(profile_set)
        except Exception as e:
            # Non-fatal; store whatever we received.
            logging.getLogger(__name__).warning(f"OSM enrichment failed (storing raw profiles): {e}")

    store = MeshProfileStore()
    key = store.put(profile_set)
    return {"status": "ok", "key": key, "stats": store.stats()}


@app.get("/api/mesh-profiles/has")
def mesh_profiles_has(
    tx_lat: float,
    tx_lon: float,
    tx_height_m: float = 0.0,
    rx_height_m: float = 1.5,
    max_range_m: float = 2000.0,
    dr_m: float = 5.0,
    dtheta_deg: float = 5.0,
    version: str = PROFILE_VERSION,
) -> Dict[str, Any]:
    """Check whether profiles exist on disk for the given TX/config."""
    store = MeshProfileStore()
    exists, key = store.has(
        tx=LatLon(lat=tx_lat, lon=tx_lon),
        tx_height_m=tx_height_m,
        rx_height_m=rx_height_m,
        max_range_m=max_range_m,
        dr_m=dr_m,
        dtheta_deg=dtheta_deg,
        version=version,
    )
    return {"status": "ok", "exists": exists, "key": key}


@app.get("/api/mesh-profiles/get")
def mesh_profiles_get(
    tx_lat: float,
    tx_lon: float,
    tx_height_m: float = 0.0,
    rx_height_m: float = 1.5,
    max_range_m: float = 2000.0,
    dr_m: float = 5.0,
    dtheta_deg: float = 5.0,
    version: str = PROFILE_VERSION,
) -> Dict[str, Any]:
    """Load persisted profiles for a TX/config (useful for debugging)."""
    from fastapi import HTTPException

    store = MeshProfileStore()
    tx = LatLon(lat=tx_lat, lon=tx_lon)
    prof = store.get(
        tx=tx,
        tx_height_m=tx_height_m,
        rx_height_m=rx_height_m,
        max_range_m=max_range_m,
        dr_m=dr_m,
        dtheta_deg=dtheta_deg,
        version=version,
    )
    if prof is None:
        _, key_str = store.compute_key(tx, tx_height_m, rx_height_m, max_range_m, dr_m, dtheta_deg, version)
        raise HTTPException(status_code=404, detail={"message": "not found", "key_str": key_str})

    key, _ = store.compute_key(tx, tx_height_m, rx_height_m, max_range_m, dr_m, dtheta_deg, version)
    return {"status": "ok", "key": key, "profile_set": prof.model_dump()}


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
    clear_mesh = False
    try:
        body = await req.json()
        if isinstance(body, dict):
            clear_osm = body.get("clear_osm", False)
            clear_mesh = body.get("clear_mesh", False)
    except:
        # No body provided, use default (preserve OSM cache)
        pass
    
    result = {
        "status": "ok",
        "message": "Cache cleared",
        "osm_cache_cleared": False,
        "mesh_cache_cleared": False,
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

    if clear_mesh:
        store = MeshProfileStore()
        deleted_profiles = store.clear()
        logger.info(f"Mesh profile cache clear requested - deleted {deleted_profiles} profile set(s)")
        result.update({
            "mesh_cache_cleared": True,
            "mesh_profiles_deleted": deleted_profiles,
            "mesh_cache_stats": store.stats(),
        })
    
    return result


# Serve frontend static files - explicit routes for known files only
static_dir = Path(__file__).parent.parent / "ui" / "static"
if static_dir.exists():
    from fastapi.responses import FileResponse

    # Serve Cesium assets (repo root /Cesium) for the mesh-profiler utility.
    # This keeps Google mesh sampling out of the core planner and avoids adding
    # a server-side tiles dependency.
    repo_root = Path(__file__).resolve().parents[3]
    cesium_dir = repo_root / "Cesium"
    if cesium_dir.exists():
        app.mount("/Cesium", StaticFiles(directory=str(cesium_dir)), name="Cesium")
    
    @app.get("/")
    async def serve_index():
        """Serve the default UI.

        Defaults to 2D Leaflet UI. 3D Cesium UI is available at /3d.
        """
        # Always default to 2D - users can navigate to /3d if they want 3D mode
        default_ray_mode = "2d"
        if default_ray_mode == "3d":
            idx3 = static_dir / "index_3d.html"
            if idx3.exists():
                return FileResponse(str(idx3))

        index_path = static_dir / "index.html"
        if index_path.exists():
            return FileResponse(str(index_path))
        from fastapi import HTTPException
        raise HTTPException(status_code=404)

    @app.get("/2d")
    async def serve_index_2d():
        """Serve the 2D Leaflet UI explicitly."""
        index_path = static_dir / "index.html"
        if index_path.exists():
            return FileResponse(str(index_path))
        from fastapi import HTTPException
        raise HTTPException(status_code=404)

    @app.get("/3d")
    async def serve_index_3d():
        """Serve the 3D Cesium UI explicitly."""
        idx3 = static_dir / "index_3d.html"
        if idx3.exists():
            return FileResponse(str(idx3), headers={"Cache-Control": "no-store"})
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
            return FileResponse(str(file_path), headers={"Cache-Control": "no-store"})
        from fastapi import HTTPException
        raise HTTPException(status_code=404)

    @app.get("/planner_3d.js")
    async def serve_planner_3d_js():
        file_path = static_dir / "planner_3d.js"
        if file_path.exists():
            return FileResponse(str(file_path), headers={"Cache-Control": "no-store"})
        from fastapi import HTTPException
        raise HTTPException(status_code=404)
    
    @app.get("/style.css")
    async def serve_style_css():
        file_path = static_dir / "style.css"
        if file_path.exists():
            return FileResponse(str(file_path))
        from fastapi import HTTPException
        raise HTTPException(status_code=404)

    @app.get("/export_utils.js")
    async def serve_export_utils():
        file_path = static_dir / "export_utils.js"
        if file_path.exists():
            return FileResponse(str(file_path), headers={"Cache-Control": "no-store"})
        from fastapi import HTTPException
        raise HTTPException(status_code=404)

    # Optional mesh profiler UI (Cesium + Google Photorealistic mesh sampling)
    @app.get("/mesh-profiler")
    async def serve_mesh_profiler():
        file_path = static_dir / "mesh_profiler.html"
        if file_path.exists():
            return FileResponse(str(file_path))
        from fastapi import HTTPException
        raise HTTPException(status_code=404)

    @app.get("/mesh_profiler.js")
    async def serve_mesh_profiler_js():
        file_path = static_dir / "mesh_profiler.js"
        if file_path.exists():
            return FileResponse(str(file_path))
        from fastapi import HTTPException
        raise HTTPException(status_code=404)


    @app.get("/mesh_profiler_core.js")
    async def serve_mesh_profiler_core_js():
        file_path = static_dir / "mesh_profiler_core.js"
        if file_path.exists():
            return FileResponse(str(file_path))
        from fastapi import HTTPException
        raise HTTPException(status_code=404)
