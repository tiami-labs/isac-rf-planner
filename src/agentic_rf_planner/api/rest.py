"""FastAPI REST API for RF planning."""

import asyncio
import json
import logging
import re
import threading
import sys
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, List

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
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

# --- In-memory planner diagnostics (3D UI POSTs phases; GET exposes them for curl/watch scripts) ---
PLANNER_DIAG_PROCESS_BOOT_UTC = datetime.now(timezone.utc).isoformat()
_planner_diag_lock = threading.Lock()
_planner_diag: Dict[str, Any] = {
    "seq": 0,
    "events": [],
    "last": None,
}
_PLANNER_EVENTS_CAP = 40
PLANNER_DIAG_FILE = Path.home() / ".rf_planning_cache" / "planner_phase_diag.json"

# --- Remote "Plan RF" for an already-open /3d tab ---
REMOTE_PLAN_QUEUE_BOOT_UTC = datetime.now(timezone.utc).isoformat()
_ui_remote_plan_lock = threading.Lock()
_ui_remote_plan: Dict[str, Any] = {
    "seq": 0,
    "status": "idle",
    "plan": None,
    "error": None,
    "requested_utc": None,
    "completed_utc": None,
}


def _evt_public(evt: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not evt:
        return None
    return {k: v for k, v in evt.items() if k != "monotonic_s"}


def _persist_planner_diag_to_disk() -> None:
    log = logging.getLogger(__name__)
    try:
        PLANNER_DIAG_FILE.parent.mkdir(parents=True, exist_ok=True)
        last = _planner_diag.get("last")
        payload: Dict[str, Any] = {
            "version": 1,
            "seq": int(_planner_diag.get("seq") or 0),
            "last": _evt_public(last),
            "events": [_evt_public(e) for e in (_planner_diag.get("events") or [])],
            "persisted_utc": datetime.now(timezone.utc).isoformat(),
            "process_boot_utc": PLANNER_DIAG_PROCESS_BOOT_UTC,
        }
        tmp = PLANNER_DIAG_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(PLANNER_DIAG_FILE)
    except Exception as e:
        log.warning("planner_phase_diag: persist failed: %s", e)


def _load_planner_diag_from_disk() -> Optional[Dict[str, Any]]:
    if not PLANNER_DIAG_FILE.is_file():
        return None
    try:
        return json.loads(PLANNER_DIAG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _seconds_since_event(evt: Dict[str, Any], now_mono: float) -> float:
    if "monotonic_s" in evt:
        return max(0.0, now_mono - float(evt["monotonic_s"]))
    try:
        raw = str(evt.get("utc") or "")
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return 0.0


def _parse_profiler_bearings(detail: str) -> Optional[Dict[str, int]]:
    m = re.search(r"(\d+)\s*/\s*(\d+)\s*bearings", detail, flags=re.IGNORECASE)
    if not m:
        return None
    return {"current": int(m.group(1)), "total": int(m.group(2))}


def _record_planner_phase(phase: str, detail: str, client_t_ms: Any) -> Dict[str, Any]:
    utc = datetime.now(timezone.utc).isoformat()
    mono = time.monotonic()
    bearings = _parse_profiler_bearings(detail)
    cli = None
    if client_t_ms is not None:
        try:
            cli = int(client_t_ms)
        except (TypeError, ValueError):
            cli = None
    evt: Dict[str, Any] = {
        "utc": utc,
        "monotonic_s": mono,
        "phase": phase,
        "detail": detail[:500],
        "client_ms": cli,
        "bearings": bearings,
    }
    with _planner_diag_lock:
        _planner_diag["seq"] = int(_planner_diag["seq"]) + 1
        evt["seq"] = int(_planner_diag["seq"])
        _planner_diag["last"] = evt
        evs = list(_planner_diag.get("events") or [])
        evs.append(evt)
        _planner_diag["events"] = evs[-_PLANNER_EVENTS_CAP:]
        _persist_planner_diag_to_disk()
    return evt


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
                footprint_buildings=getattr(osm, "_cached_buildings", []) or [],
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



class RaytracePathsRequest(BaseModel):
    """Compute multipath rays between a TX and a specific RX (for Omniverse-style debugging)."""

    tx_lat: float
    tx_lon: float
    rx_lat: float
    rx_lon: float
    ray_mode: str = "3d_rt"  # expects 3d_rt
    tx_height_m: float = 10.0
    rx_height_m: float = 1.5
    freq_mhz: float = 3500.0
    tx_power_dbm: float = 43.0
    noise_figure_db: float = 7.0
    channel_bandwidth_mhz: float = 40.0
    num_resource_blocks: int = 100
    num_tx_antennas: int = 1
    num_rx_antennas: int = 1
    mimo_mode: str = "MIMO"
    tx_antenna_gain_dbi: float = 17.0
    tx_feeder_loss_db: float = 2.0
    reference_signal_offset_db: float = -18.0
    ue_antenna_gain_dbi: float = 0.0
    electrical_tilt_deg: float = 0.0
    mechanical_tilt_deg: float = 0.0
    vertical_beamwidth_deg: float = 8.0
    max_vertical_attenuation_db: float = 30.0
    max_horizontal_attenuation_db: float = 30.0
    front_to_back_attenuation_db: float = 25.0
    path_loss_model: str = "3gpp_38901"
    propagation_scenario: str = "umi_street_canyon"
    termination_rsrp_dbm: float = -140.0
    sectors: Optional[List[Dict[str, Any]]] = None

    max_bounces: int = 2
    max_wall_candidates: int = 80
    max_paths: int = 12
    reflection_loss_db: float = 8.0
    profile_max_range_m: Optional[float] = None
    profile_dr_m: Optional[float] = None
    profile_dtheta_deg: Optional[float] = None


def _compute_raytrace_paths_response(req: RaytracePathsRequest, progress=None) -> Dict[str, Any]:
    """Return direct + OSM reflection candidate paths between TX and RX.

    OSM-only by design here: this keeps baseline behavior stable while the solver is
    improved independently from any mesh integration.
    """
    import math

    from ..geo.osm_map_provider import OSMMapProvider, _estimate_osm_height_m
    from ..rf.ray_tracing import (
        RayPath,
        WallSegment,
        _norm,
        _segment_intersection_point,
        compute_single_bounce_paths,
        compute_two_bounce_paths,
        enu_from_latlon,
        extract_wall_segments,
    )
    from ..rf.attenuation_models import (
        _horizontal_pattern_attenuation_db,
        _reference_signal_eirp_dbm,
        _resolve_sector_params,
        _scenario_path_loss_db,
        _vertical_pattern_attenuation_db,
    )

    log = logging.getLogger(__name__)
    started = time.monotonic()

    def emit(stage: str, detail: str = "", **extra: Any) -> None:
        payload: Dict[str, Any] = {
            "type": "heartbeat",
            "stage": stage,
            "detail": detail,
            "elapsed_s": round(time.monotonic() - started, 3),
        }
        if extra:
            payload.update(extra)
        if progress is not None:
            progress(payload)

    tx = LatLon(lat=req.tx_lat, lon=req.tx_lon)
    rx = LatLon(lat=req.rx_lat, lon=req.rx_lon)
    emit("request_enter", f"tx=({tx.lat:.6f},{tx.lon:.6f}) rx=({rx.lat:.6f},{rx.lon:.6f})")

    rf_params = RFParams(
        freq_mhz=req.freq_mhz,
        tx_power_dbm=req.tx_power_dbm,
        ray_mode="3d_rt",
        tx_height_m=req.tx_height_m,
        rx_height_m=req.rx_height_m,
        termination_rsrp_dbm=req.termination_rsrp_dbm,
        rt_max_bounces=req.max_bounces,
        rt_max_reflections_per_sample=req.max_paths,
        rt_max_wall_candidates=req.max_wall_candidates,
        rt_reflection_loss_db=req.reflection_loss_db,
    )

    mid = LatLon(lat=(tx.lat + rx.lat) * 0.5, lon=(tx.lon + rx.lon) * 0.5)

    def _hav(a: LatLon, b: LatLon) -> float:
        R = 6371000.0
        la1 = math.radians(a.lat)
        la2 = math.radians(b.lat)
        dlat = math.radians(b.lat - a.lat)
        dlon = math.radians(b.lon - a.lon)
        x = math.sin(dlat / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlon / 2) ** 2
        return 2 * R * math.atan2(math.sqrt(x), math.sqrt(max(1e-15, 1 - x)))

    dist_m = _hav(tx, rx)
    radius_m = max(600.0, min(2500.0, dist_m + 350.0))
    emit("osm_prefetch_start", f"radius_m={radius_m:.1f}")
    osm = OSMMapProvider(cache_radius_m=max(1000.0, radius_m))
    osm.prefetch_all_data(mid, radius_m)
    buildings = list(getattr(osm, "_cached_buildings", []) or [])
    emit("osm_prefetch_done", f"cached_buildings={len(buildings)}", cached_buildings=len(buildings))

    def is_path_clear(p0: LatLon, p1: LatLon, exclude_id: int | None) -> bool:
        hits = osm.get_buildings_along_ray(p0, p1)
        for b in hits:
            bid = b.get("id") or b.get("osm_id")
            try:
                bid_int = int(bid) if bid is not None else None
            except Exception:
                bid_int = None
            if exclude_id is not None and bid_int == exclude_id:
                continue
            return False
        return True

    emit("wall_extract_start")
    wall_segments = extract_wall_segments(buildings, tx)
    emit("wall_extract_done", f"osm_walls={len(wall_segments)}", osm_walls=len(wall_segments))

    building_height_by_id: Dict[int, float] = {}
    building_by_id: Dict[int, Dict[str, Any]] = {}
    for b in buildings:
        bid = b.get("id") or b.get("osm_id")
        try:
            bid_int = int(bid)
        except Exception:
            continue
        building_by_id[bid_int] = b
        height = b.get("height_m") or b.get("height") or b.get("building_height_m")
        try:
            if height is None:
                height = _estimate_osm_height_m(b.get("tags", {}))
            if height is None:
                height = 15.0
            height_f = float(height)
            b["height_m"] = height_f
            building_height_by_id[bid_int] = height_f
        except Exception:
            continue

    _building_edges_cache: Dict[int, List[tuple[tuple[float, float], tuple[float, float]]]] = {}

    def _get_building_id(building: Dict[str, Any]) -> Optional[int]:
        bid = building.get("id") or building.get("osm_id")
        try:
            return int(bid) if bid is not None else None
        except Exception:
            return None

    def _building_edges_enu(building: Dict[str, Any]) -> List[tuple[tuple[float, float], tuple[float, float]]]:
        bid = _get_building_id(building)
        if bid is not None and bid in _building_edges_cache:
            return _building_edges_cache[bid]
        geom = building.get("geometry") or []
        pts: List[tuple[float, float]] = []
        for node in geom:
            if not isinstance(node, dict) or "lat" not in node or "lon" not in node:
                continue
            pts.append(enu_from_latlon(tx, LatLon(lat=float(node["lat"]), lon=float(node["lon"]))))
        edges: List[tuple[tuple[float, float], tuple[float, float]]] = []
        if len(pts) >= 2:
            if _norm(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) > 1.0:
                pts.append(pts[0])
            for i in range(len(pts) - 1):
                a, b = pts[i], pts[i + 1]
                if _norm(b[0] - a[0], b[1] - a[1]) >= 0.25:
                    edges.append((a, b))
        if bid is not None:
            _building_edges_cache[bid] = edges
        return edges

    def _dist_point_to_seg_m(pt: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        px, py = pt
        ax, ay = a
        bx, by = b
        vx, vy = bx - ax, by - ay
        wx, wy = px - ax, py - ay
        vv = vx * vx + vy * vy
        if vv < 1e-9:
            return _norm(px - ax, py - ay)
        t = max(0.0, min(1.0, (wx * vx + wy * vy) / vv))
        cx, cy = ax + t * vx, ay + t * vy
        return _norm(px - cx, py - cy)

    def _wall_segment_from_meta(meta: Dict[str, Any] | None) -> Optional[WallSegment]:
        if not meta:
            return None
        try:
            return WallSegment(
                a_e=float(meta["a_e"]),
                a_n=float(meta["a_n"]),
                b_e=float(meta["b_e"]),
                b_n=float(meta["b_n"]),
                building_id=(int(meta["building_id"]) if meta.get("building_id") is not None else None),
                material=str(meta.get("material", "unknown") or "unknown"),
            )
        except Exception:
            return None

    def _segment_hits(p0: LatLon, p1: LatLon) -> List[Dict[str, Any]]:
        hits = osm.get_buildings_along_ray(p0, p1)
        s = enu_from_latlon(tx, p0)
        t = enu_from_latlon(tx, p1)
        result: List[Dict[str, Any]] = []
        seen: set[tuple[Optional[int], int, int]] = set()
        for building in hits:
            bid = _get_building_id(building)
            for edge_a, edge_b in _building_edges_enu(building):
                inter = _segment_intersection_point(s, t, edge_a, edge_b)
                if inter is None:
                    continue
                dist = _norm(inter[0] - s[0], inter[1] - s[1])
                key = (bid, int(round(dist * 100.0)), int(round(_dist_point_to_seg_m(inter, edge_a, edge_b) * 100.0)))
                if key in seen:
                    continue
                seen.add(key)
                result.append({
                    "bid": bid,
                    "point": inter,
                    "dist": dist,
                    "edge": (edge_a, edge_b),
                })
        result.sort(key=lambda item: item["dist"])
        return result

    def _hit_matches_wall(hit: Dict[str, Any], wall: Optional[WallSegment], expected_pt: tuple[float, float], tol_m: float = 2.0) -> bool:
        if wall is None:
            return False
        if wall.building_id is not None and hit.get("bid") != wall.building_id:
            return False
        if _dist_point_to_seg_m(hit["point"], (wall.a_e, wall.a_n), (wall.b_e, wall.b_n)) > tol_m:
            return False
        if _norm(hit["point"][0] - expected_pt[0], hit["point"][1] - expected_pt[1]) > tol_m:
            return False
        return True

    def _validate_leg(
        p0: LatLon,
        p1: LatLon,
        *,
        allow_start_wall: Optional[WallSegment] = None,
        allow_end_wall: Optional[WallSegment] = None,
    ) -> tuple[bool, str]:
        hits = _segment_hits(p0, p1)
        s = enu_from_latlon(tx, p0)
        t = enu_from_latlon(tx, p1)
        leg_len = _norm(t[0] - s[0], t[1] - s[1])
        start_tol = 1.5
        end_tol = 1.5
        have_end = allow_end_wall is None
        for hit in hits:
            d = float(hit["dist"])
            near_start = d <= start_tol
            near_end = abs(leg_len - d) <= end_tol
            if near_start and allow_start_wall is not None and _hit_matches_wall(hit, allow_start_wall, s):
                continue
            if near_end and allow_end_wall is not None and _hit_matches_wall(hit, allow_end_wall, t):
                have_end = True
                continue
            if near_start and allow_start_wall is None:
                return False, "blocked_at_start"
            return False, "blocked_mid_leg"
        if not have_end:
            return False, "missing_reflector_hit"
        return True, "ok"

    def _interpolated_heights(path: RayPath) -> Optional[List[float]]:
        distances = list(path.meta.get("distances_m") or [])
        total = float(sum(float(x) for x in distances)) if distances else 0.0
        if total <= 0.0:
            return None
        hs: List[float] = [float(req.tx_height_m)]
        cumulative = 0.0
        for idx, leg_d in enumerate(distances[:-1], start=1):
            cumulative += float(leg_d)
            z = float(req.tx_height_m) + (float(req.rx_height_m) - float(req.tx_height_m)) * (cumulative / total)
            wall_meta = path.meta.get(f"wall{idx}")
            wall = _wall_segment_from_meta(wall_meta)
            bh = building_height_by_id.get(int(wall.building_id)) if wall and wall.building_id is not None else None
            if bh is not None and z > bh + 0.25:
                return None
            if bh is not None:
                z = min(z, max(1.0, bh - 0.25))
            hs.append(max(0.0, z))
        hs.append(float(req.rx_height_m))
        return hs

    def _validate_reflection_path(path: RayPath) -> tuple[bool, Optional[List[float]], str]:
        points = path.points
        if path.kind == "reflect" and len(points) == 3:
            wall1 = _wall_segment_from_meta(path.meta.get("wall1"))
            ok1, why1 = _validate_leg(points[0], points[1], allow_end_wall=wall1)
            if not ok1:
                return False, None, f"leg1_{why1}"
            ok2, why2 = _validate_leg(points[1], points[2], allow_start_wall=wall1)
            if not ok2:
                return False, None, f"leg2_{why2}"
            heights = _interpolated_heights(path)
            if heights is None:
                return False, None, "height_exceeds_building"
            return True, heights, "ok"
        if path.kind == "reflect2" and len(points) == 4:
            wall1 = _wall_segment_from_meta(path.meta.get("wall1"))
            wall2 = _wall_segment_from_meta(path.meta.get("wall2"))
            ok1, why1 = _validate_leg(points[0], points[1], allow_end_wall=wall1)
            if not ok1:
                return False, None, f"leg1_{why1}"
            ok2, why2 = _validate_leg(points[1], points[2], allow_start_wall=wall1, allow_end_wall=wall2)
            if not ok2:
                return False, None, f"leg2_{why2}"
            ok3, why3 = _validate_leg(points[2], points[3], allow_start_wall=wall2)
            if not ok3:
                return False, None, f"leg3_{why3}"
            heights = _interpolated_heights(path)
            if heights is None:
                return False, None, "height_exceeds_building"
            return True, heights, "ok"
        return False, None, "unsupported_path_kind"

    sector_params = _resolve_sector_params(
        type(
            "RaytracePathsSector",
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

    def rsrp_for_reflection(tx_ll: LatLon, bounce_ll: LatLon, rx_ll: LatLon, total_d: float, refl_loss: float) -> float:
        be = (bounce_ll.lon - tx_ll.lon) * math.cos(math.radians(tx_ll.lat))
        bn = (bounce_ll.lat - tx_ll.lat)
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

    def _bounce_height_for_path(path: RayPath) -> Optional[List[float]]:
        if path.kind == "direct":
            return [float(req.tx_height_m), float(req.rx_height_m)]
        return _interpolated_heights(path)

    emit("direct_path_start")
    is_los = is_path_clear(tx, rx, None)
    d2 = max(1.0, dist_m)
    d3 = math.sqrt(d2 * d2 + dz2)
    pl = _scenario_path_loss_db(
        distance_2d_m=d2,
        distance_3d_m=d3,
        freq_mhz=req.freq_mhz,
        tx_height_m=req.tx_height_m,
        rx_height_m=req.rx_height_m,
        is_los=is_los,
        rf_params=rf_params,
    )
    vpat = _vertical_pattern_attenuation_db(
        distance_m=d2,
        tx_height_m=req.tx_height_m,
        rx_height_m=req.rx_height_m,
        rf_params=rf_params,
        sector_params=sector_params,
    )
    be = (rx.lon - tx.lon) * math.cos(math.radians(tx.lat))
    bn = (rx.lat - tx.lat)
    brg = (math.degrees(math.atan2(be, bn)) + 360.0) % 360.0
    hpat = _horizontal_pattern_attenuation_db(
        bearing_deg=brg,
        sector_params=sector_params,
        rf_params=rf_params,
    )
    direct_rsrp = rs_eirp_dbm - (pl + hpat + vpat) + rx_gain_db
    emit("direct_path_done", f"los={is_los}", los=bool(is_los), direct_rsrp_dbm=round(direct_rsrp, 3))

    out_paths: List[Dict[str, Any]] = []
    if is_los:
        out_paths.append(
            {
                "kind": "direct",
                "rsrp_dbm": direct_rsrp,
                "points": [
                    {"lat": tx.lat, "lon": tx.lon, "h": float(req.tx_height_m)},
                    {"lat": rx.lat, "lon": rx.lon, "h": float(req.rx_height_m)},
                ],
            }
        )

    term = float(req.termination_rsrp_dbm)
    raw_paths = 0
    accepted_reflections = 0
    rejected_invalid_footprint = 0

    if int(req.max_bounces) >= 1:
        emit("single_bounce_start", f"max_candidates={int(req.max_wall_candidates)}")
        p1 = compute_single_bounce_paths(
            tx,
            rx,
            wall_segments,
            max_candidates=int(req.max_wall_candidates),
            max_return=max(1, int(req.max_paths)),
            rf_params=rf_params,
            is_path_clear_fn=is_path_clear,
            rsrp_for_path_fn=rsrp_for_reflection,
            footprint_buildings=buildings,
        )
        emit("single_bounce_done", f"count={len(p1)}", count=len(p1))
        for p in p1:
            raw_paths += 1
            if p.rsrp_dbm < term:
                continue
            ok, heights, reason = _validate_reflection_path(p)
            if not ok or heights is None:
                rejected_invalid_footprint += 1
                continue
            accepted_reflections += 1
            out_paths.append({
                "kind": p.kind,
                "rsrp_dbm": p.rsrp_dbm,
                "points": [
                    {"lat": pt.lat, "lon": pt.lon, "h": heights[i] if i < len(heights) else float(req.rx_height_m)}
                    for i, pt in enumerate(p.points)
                ],
            })

    if int(req.max_bounces) >= 2:
        emit("two_bounce_start", f"max_candidates={max(2, int(req.max_wall_candidates // 2))}")
        p2 = compute_two_bounce_paths(
            tx,
            rx,
            wall_segments,
            max_candidates=max(2, int(req.max_wall_candidates // 2)),
            max_return=max(1, int(req.max_paths)),
            rf_params=rf_params,
            is_path_clear_fn=is_path_clear,
            rsrp_for_path_fn=rsrp_for_reflection,
            footprint_buildings=buildings,
        )
        emit("two_bounce_done", f"count={len(p2)}", count=len(p2))
        for p in p2:
            raw_paths += 1
            if p.rsrp_dbm < term:
                continue
            ok, heights, reason = _validate_reflection_path(p)
            if not ok or heights is None:
                rejected_invalid_footprint += 1
                continue
            accepted_reflections += 1
            out_paths.append({
                "kind": p.kind,
                "rsrp_dbm": p.rsrp_dbm,
                "points": [
                    {"lat": pt.lat, "lon": pt.lon, "h": heights[i] if i < len(heights) else float(req.rx_height_m)}
                    for i, pt in enumerate(p.points)
                ],
            })

    out_paths.sort(key=lambda p: float(p.get("rsrp_dbm", -9999.0)), reverse=True)
    out_paths = out_paths[: max(1, int(req.max_paths) + (1 if is_los else 0))]

    diagnostics: Dict[str, Any] = {
        "osm_walls": len(wall_segments),
        "raw_paths": raw_paths,
        "accepted_reflections": accepted_reflections,
        "rejected_invalid_footprint": rejected_invalid_footprint,
        "returned": len(out_paths),
        "los": bool(is_los),
    }
    message = (
        f"3D RT (OSM strict): osm_walls={diagnostics['osm_walls']} raw_paths={diagnostics['raw_paths']} "
        f"accepted={diagnostics['accepted_reflections']} rejected_invalid_footprint={diagnostics['rejected_invalid_footprint']} "
        f"returned={diagnostics['returned']} los={diagnostics['los']}"
    )
    log.info(
        "raytrace_paths tx=(%.6f, %.6f) rx=(%.6f, %.6f) %s",
        tx.lat, tx.lon, rx.lat, rx.lon, message,
    )
    emit("finalize", message, **diagnostics)

    return {
        "tx": {"lat": tx.lat, "lon": tx.lon},
        "rx": {"lat": rx.lat, "lon": rx.lon},
        "paths": out_paths,
        "los": bool(is_los),
        "diagnostics": diagnostics,
        "message": message,
    }


@app.post("/api/raytrace_paths")
async def api_raytrace_paths(req: RaytracePathsRequest) -> Dict[str, Any]:
    return await asyncio.to_thread(_compute_raytrace_paths_response, req, None)


@app.post("/api/raytrace_paths/stream")
async def api_raytrace_paths_stream(req: RaytracePathsRequest):
    import queue

    log = logging.getLogger(__name__)
    started = time.monotonic()
    log.info(
        "raytrace_paths_stream start tx=(%.6f, %.6f) rx=(%.6f, %.6f)",
        req.tx_lat, req.tx_lon, req.rx_lat, req.rx_lon,
    )
    q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
    done = threading.Event()

    def push(event: Dict[str, Any]) -> None:
        q.put(event)

    def run_compute() -> None:
        try:
            result = _compute_raytrace_paths_response(req, push)
            q.put({
                "type": "result",
                "stage": "complete",
                "elapsed_s": round(time.monotonic() - started, 3),
                "payload": result,
            })
        except Exception as e:
            log.exception("raytrace_paths_stream failed")
            q.put({
                "type": "error",
                "stage": "exception",
                "elapsed_s": round(time.monotonic() - started, 3),
                "error": f"{type(e).__name__}: {e}",
            })
        finally:
            done.set()

    threading.Thread(target=run_compute, daemon=True).start()

    def encode(obj: Dict[str, Any]) -> bytes:
        return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")

    def gen():
        while True:
            try:
                event = q.get(timeout=2.0)
            except queue.Empty:
                if done.is_set():
                    break
                yield encode({
                    "type": "heartbeat",
                    "stage": "processing",
                    "detail": "still_processing",
                    "elapsed_s": round(time.monotonic() - started, 3),
                })
                continue
            yield encode(event)
            if event.get("type") in {"result", "error"}:
                break

    return StreamingResponse(gen(), media_type="application/x-ndjson")
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


class PlanAndTrace3DRequest(PlanRequest):
    """lat/lon = TX, rx_lat/rx_lon = RX; convenience wrapper for coverage + trace."""

    rx_lat: float
    rx_lon: float


class RemotePlanRFRequest(PlanRequest):
    """Same as PlanRequest; for ray_mode=3d_rt also provide rx_lat/rx_lon."""

    rx_lat: Optional[float] = None
    rx_lon: Optional[float] = None


def _resolved_grid_for_mesh_profile_key(req: PlanRequest) -> tuple[float, float, float]:
    try:
        rf_cfg = load_rf_config("configs/rf.params.yaml") or {}
    except Exception:
        rf_cfg = {}
    max_r = float(
        req.max_range_m
        if req.max_range_m is not None
        else rf_cfg.get("max_range_m", RFParams.model_fields["max_range_m"].default)
    )
    step_m = float(
        req.step_m if req.step_m is not None else RFParams.model_fields["step_m"].default
    )
    dth = float(
        req.dtheta_deg
        if req.dtheta_deg is not None
        else (
            RFParams.model_fields["dtheta_deg"].default
            if "dtheta_deg" in RFParams.model_fields
            else 5.0
        )
    )
    return max_r, step_m, dth


def _raytrace_request_from_plan_and_rx(plan_req: PlanRequest, rx_lat: float, rx_lon: float) -> RaytracePathsRequest:
    max_r, step_m, dth = _resolved_grid_for_mesh_profile_key(plan_req)
    return RaytracePathsRequest(
        tx_lat=plan_req.lat,
        tx_lon=plan_req.lon,
        rx_lat=rx_lat,
        rx_lon=rx_lon,
        ray_mode="3d_rt",
        tx_height_m=plan_req.tx_height_m,
        rx_height_m=plan_req.rx_height_m,
        freq_mhz=plan_req.freq_mhz,
        tx_power_dbm=plan_req.tx_power_dbm,
        noise_figure_db=plan_req.noise_figure_db,
        channel_bandwidth_mhz=plan_req.channel_bandwidth_mhz,
        num_resource_blocks=plan_req.num_resource_blocks,
        num_tx_antennas=plan_req.num_tx_antennas,
        num_rx_antennas=plan_req.num_rx_antennas,
        mimo_mode=plan_req.mimo_mode,
        tx_antenna_gain_dbi=(plan_req.tx_antenna_gain_dbi if plan_req.tx_antenna_gain_dbi is not None else 17.0),
        tx_feeder_loss_db=(plan_req.tx_feeder_loss_db if plan_req.tx_feeder_loss_db is not None else 2.0),
        reference_signal_offset_db=(plan_req.reference_signal_offset_db if plan_req.reference_signal_offset_db is not None else -18.0),
        ue_antenna_gain_dbi=(plan_req.ue_antenna_gain_dbi if plan_req.ue_antenna_gain_dbi is not None else 0.0),
        electrical_tilt_deg=(plan_req.electrical_tilt_deg if plan_req.electrical_tilt_deg is not None else 0.0),
        mechanical_tilt_deg=(plan_req.mechanical_tilt_deg if plan_req.mechanical_tilt_deg is not None else 0.0),
        vertical_beamwidth_deg=(plan_req.vertical_beamwidth_deg if plan_req.vertical_beamwidth_deg is not None else 8.0),
        max_vertical_attenuation_db=(plan_req.max_vertical_attenuation_db if plan_req.max_vertical_attenuation_db is not None else 30.0),
        max_horizontal_attenuation_db=(plan_req.max_horizontal_attenuation_db if plan_req.max_horizontal_attenuation_db is not None else 30.0),
        front_to_back_attenuation_db=(plan_req.front_to_back_attenuation_db if plan_req.front_to_back_attenuation_db is not None else 25.0),
        path_loss_model=(plan_req.path_loss_model or "3gpp_38901"),
        propagation_scenario=(plan_req.propagation_scenario or "umi_street_canyon"),
        termination_rsrp_dbm=(plan_req.termination_rsrp_dbm if plan_req.termination_rsrp_dbm is not None else -140.0),
        sectors=plan_req.sectors,
        max_bounces=(plan_req.rt_max_bounces if plan_req.rt_max_bounces is not None else 2),
        max_wall_candidates=(plan_req.rt_max_wall_candidates if plan_req.rt_max_wall_candidates is not None else 80),
        max_paths=(plan_req.rt_max_reflections_per_sample if plan_req.rt_max_reflections_per_sample is not None else 12),
        reflection_loss_db=(plan_req.rt_reflection_loss_db if plan_req.rt_reflection_loss_db is not None else 8.0),
        profile_max_range_m=max_r,
        profile_dr_m=step_m,
        profile_dtheta_deg=dth,
    )


def _require_mesh_profiles_or_409(plan_req: PlanRequest) -> str:
    max_r, step_m, dth = _resolved_grid_for_mesh_profile_key(plan_req)
    tx_ll = LatLon(lat=plan_req.lat, lon=plan_req.lon)
    store = MeshProfileStore()
    exists, pkey = store.has(
        tx_ll,
        plan_req.tx_height_m,
        plan_req.rx_height_m,
        max_r,
        step_m,
        dth,
        PROFILE_VERSION,
    )
    if not exists:
        raise HTTPException(
            status_code=409,
            detail={
                "status": "missing_mesh_profiles",
                "cache_key_hash": pkey,
                "message": (
                    "Mesh profiles missing for this TX and grid resolution. Generate them from the /3d or / "
                    "planner UI with Ray Mode 3D or 3d_rt (in-browser sampling), or POST /api/mesh-profiles/put."
                ),
            },
        )
    return str(pkey)


def _browser_draw_recipe(*, plan_result: Dict[str, Any], ray_result: Dict[str, Any], profile_cache_key: str) -> Dict[str, Any]:
    heatmap = plan_result.get("heatmap") or {}
    snapped = plan_result.get("snapped_tx") or {}
    return {
        "heatmap": {
            "where_in_response": "plan.heatmap",
            "format": heatmap.get("format") or "png_base64",
            "width": heatmap.get("width"),
            "height": heatmap.get("height"),
        },
        "snapped_tx_marker": {
            "where_in_response": "plan.snapped_tx",
            "lat": snapped.get("lat"),
            "lon": snapped.get("lon"),
        },
        "rx_marker": {
            "where_in_response": "request body rx_lat/rx_lon",
        },
        "ray_multipath_polylines": {
            "where_in_response": "raytrace.paths[].points",
            "crs": "WGS84",
        },
        "provenance": {
            "mesh_profiles": f"Photorealistic mesh profiles loaded from cache key {profile_cache_key} (version {PROFILE_VERSION}).",
            "osm": "Building footprints / reflections use cached OpenStreetMap geometry on the server.",
        },
    }


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


@app.post("/api/ui/remote-plan-rf")
async def api_ui_remote_plan_rf(req: RemotePlanRFRequest) -> Dict[str, Any]:
    logger = logging.getLogger(__name__)
    utc_req = datetime.now(timezone.utc).isoformat()
    with _ui_remote_plan_lock:
        _ui_remote_plan["status"] = "computing"
        _ui_remote_plan["requested_utc"] = utc_req

    plan_only = PlanRequest(**req.model_dump(exclude={"rx_lat", "rx_lon"}))
    ray_mode_eff = (plan_only.ray_mode or "2d").strip().lower()

    try:
        if ray_mode_eff in ("3d_rt", "3d-rt", "rt3d"):
            if req.rx_lat is None or req.rx_lon is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "ray_mode=3d_rt requires rx_lat and rx_lon in the JSON body "
                        "(receiver location; same as SHIFT+click RX in the 3D UI)."
                    ),
                )
            _require_mesh_profiles_or_409(plan_only)
            rt_req = _raytrace_request_from_plan_and_rx(plan_only, float(req.rx_lat), float(req.rx_lon))
            ray_result = await api_raytrace_paths(rt_req)
            result: Dict[str, Any] = {
                "mode": "3d_rt",
                "ray_mode": "3d_rt",
                "original_point": {"lat": plan_only.lat, "lon": plan_only.lon},
                "snapped_tx": {"lat": plan_only.lat, "lon": plan_only.lon},
                "rx_point": {"lat": float(req.rx_lat), "lon": float(req.rx_lon)},
                "raytrace": ray_result,
            }
        else:
            result = await api_plan(plan_only)
    except HTTPException as e:
        utc_done = datetime.now(timezone.utc).isoformat()
        detail = e.detail
        if not isinstance(detail, (dict, list, str, int, float, bool, type(None))):
            detail = str(detail)
        with _ui_remote_plan_lock:
            _ui_remote_plan["seq"] = int(_ui_remote_plan.get("seq") or 0) + 1
            _ui_remote_plan["status"] = "error"
            _ui_remote_plan["plan"] = None
            _ui_remote_plan["error"] = {"status_code": int(e.status_code), "detail": detail}
            _ui_remote_plan["completed_utc"] = utc_done
        raise
    except Exception as e:
        utc_done = datetime.now(timezone.utc).isoformat()
        logger.exception("api_ui_remote_plan_rf: %s", e)
        with _ui_remote_plan_lock:
            _ui_remote_plan["seq"] = int(_ui_remote_plan.get("seq") or 0) + 1
            _ui_remote_plan["status"] = "error"
            _ui_remote_plan["plan"] = None
            _ui_remote_plan["error"] = {"status_code": 500, "detail": str(e)}
            _ui_remote_plan["completed_utc"] = utc_done
        raise HTTPException(status_code=500, detail=str(e)) from e

    utc_done = datetime.now(timezone.utc).isoformat()
    with _ui_remote_plan_lock:
        _ui_remote_plan["seq"] = int(_ui_remote_plan.get("seq") or 0) + 1
        new_seq = int(_ui_remote_plan["seq"])
        _ui_remote_plan["status"] = "ready"
        _ui_remote_plan["plan"] = result
        _ui_remote_plan["error"] = None
        _ui_remote_plan["completed_utc"] = utc_done
    logger.info("Remote plan RF published for UI poll: seq=%s", new_seq)
    return {
        "ok": True,
        "seq": new_seq,
        "message": "Plan published. Open /3d tabs will pick this up via poll and draw on Cesium.",
    }


@app.get("/api/ui/remote-plan-rf/poll")
async def api_ui_remote_plan_rf_poll(since_seq: int = 0) -> Dict[str, Any]:
    with _ui_remote_plan_lock:
        seq = int(_ui_remote_plan.get("seq") or 0)
        st = str(_ui_remote_plan.get("status") or "idle")
        req_utc = _ui_remote_plan.get("requested_utc")
        done_utc = _ui_remote_plan.get("completed_utc")
        plan = _ui_remote_plan.get("plan")
        err = _ui_remote_plan.get("error")
    is_new = seq > int(since_seq)
    out: Dict[str, Any] = {
        "seq": seq,
        "new": bool(is_new),
        "status": st,
        "requested_utc": req_utc,
        "completed_utc": done_utc,
        "server_boot_utc": REMOTE_PLAN_QUEUE_BOOT_UTC,
    }
    if is_new:
        if st == "ready" and plan is not None:
            out["plan"] = plan
        elif st == "error" and err is not None:
            out["error"] = err
    return out


@app.post("/api/3d/plan-and-trace")
async def api_3d_plan_and_trace(req: PlanAndTrace3DRequest) -> Dict[str, Any]:
    stages: List[Dict[str, Any]] = []
    plan_payload = req.model_dump(exclude={"rx_lat", "rx_lon"})
    plan_payload["ray_mode"] = "3d_rt"
    plan_req = PlanRequest(**plan_payload)

    max_r, step_m, dth = _resolved_grid_for_mesh_profile_key(plan_req)
    tx_ll = LatLon(lat=plan_req.lat, lon=plan_req.lon)
    store = MeshProfileStore()
    exists, pkey = store.has(
        tx_ll,
        plan_req.tx_height_m,
        plan_req.rx_height_m,
        max_r,
        step_m,
        dth,
        PROFILE_VERSION,
    )
    stages.append({
        "id": "mesh_profiles",
        "status": "complete" if exists else "missing",
        "cache_key_hash": pkey,
        "profile_version": PROFILE_VERSION,
        "resolution": {"max_range_m": max_r, "step_m": step_m, "dtheta_deg": dth},
    })
    if not exists:
        raise HTTPException(
            status_code=409,
            detail={
                "stages": stages,
                "message": (
                    "Mesh profiles missing for this TX and grid resolution. Generate them from the /3d or / "
                    "planner UI with Ray Mode 3D or 3d_rt (in-browser sampling), or POST /api/mesh-profiles/put."
                ),
            },
        )

    stages.append({"id": "coverage_plan", "status": "started"})
    try:
        plan_result = await api_plan(plan_req)
    except HTTPException as e:
        err_detail: Any = e.detail
        if isinstance(err_detail, dict):
            err_detail = {**err_detail, "stages": stages + [{"id": "coverage_plan", "status": "error"}]}
        else:
            err_detail = {"message": str(err_detail), "stages": stages}
        raise HTTPException(status_code=e.status_code, detail=err_detail) from e

    stages[-1] = {
        "id": "coverage_plan",
        "status": "complete",
        "mesh_profile_key": plan_result.get("mesh_profile_key"),
        "effective_ray_mode": plan_result.get("ray_mode"),
    }

    stages.append({"id": "tx_rx_raytrace", "status": "started"})
    rt_req = _raytrace_request_from_plan_and_rx(plan_req, req.rx_lat, req.rx_lon)
    ray_result = await api_raytrace_paths(rt_req)
    stages[-1] = {
        "id": "tx_rx_raytrace",
        "status": "complete",
        "paths": len(ray_result.get("paths") or []),
        "mesh_profile_loaded": ray_result.get("mesh_profile_loaded"),
        "los": ray_result.get("los"),
    }

    browser_draw = _browser_draw_recipe(
        plan_result=plan_result,
        ray_result=ray_result,
        profile_cache_key=str(pkey),
    )

    return {
        "status": "ok",
        "stages": stages,
        "plan": plan_result,
        "raytrace": ray_result,
        "browser_draw": browser_draw,
        "tx": {"lat": plan_req.lat, "lon": plan_req.lon},
        "rx": {"lat": req.rx_lat, "lon": req.rx_lon},
    }


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


@app.get("/api/ping")
def api_ping() -> Dict[str, Any]:
    return {"status": "ok", "utc": datetime.now(timezone.utc).isoformat()}


@app.get("/api/debug/planner-state")
def debug_planner_state() -> Dict[str, Any]:
    now_mono = time.monotonic()
    disk = _load_planner_diag_from_disk()
    disk_seq = int((disk or {}).get("seq") or 0)

    with _planner_diag_lock:
        mem_last = _planner_diag.get("last")
        mem_events: List[Dict[str, Any]] = list(_planner_diag.get("events") or [])
        mem_seq = int(_planner_diag.get("seq") or 0)

    if mem_seq > disk_seq and mem_last is not None:
        last = mem_last
        events = mem_events
        seq_top = mem_seq
        source = "memory"
    elif disk_seq > mem_seq and disk and disk.get("last"):
        last = disk["last"]
        events = list(disk.get("events") or [])
        seq_top = disk_seq
        source = "disk"
    elif mem_last is not None:
        last = mem_last
        events = mem_events
        seq_top = mem_seq
        source = "memory"
    elif disk and disk.get("last"):
        last = disk["last"]
        events = list(disk.get("events") or [])
        seq_top = disk_seq
        source = "disk"
    else:
        last = None
        events = []
        seq_top = max(mem_seq, disk_seq)
        source = "none"

    out: Dict[str, Any] = {
        "status": "ok",
        "seq": seq_top,
        "source": source,
        "process_boot_utc": PLANNER_DIAG_PROCESS_BOOT_UTC,
        "persist_file": str(PLANNER_DIAG_FILE),
        "persisted_utc_from_file": (disk or {}).get("persisted_utc"),
        "utc": datetime.now(timezone.utc).isoformat(),
        "last": None,
        "seconds_since_last_event": None,
        "recent": [],
        "hints": [],
    }

    if last is None:
        out["hints"].append("No planner events recorded yet.")
        return out

    last_age = round(_seconds_since_event(last, now_mono), 3)
    out["last"] = {
        "seq": last.get("seq"),
        "utc": last.get("utc"),
        "phase": last.get("phase"),
        "detail": last.get("detail"),
        "bearings": last.get("bearings"),
        "client_ms": last.get("client_ms"),
        "age_s": last_age,
    }
    out["seconds_since_last_event"] = last_age

    for e in events[-15:]:
        age_e = round(_seconds_since_event(e, now_mono), 3)
        out["recent"].append({
            "seq": e.get("seq"),
            "age_s": age_e,
            "phase": e.get("phase"),
            "detail": str(e.get("detail") or "")[:160],
            "bearings": e.get("bearings"),
        })
    return out


@app.post("/api/debug/planner-phase")
async def debug_planner_phase(payload: Dict[str, Any]) -> Dict[str, Any]:
    phase = str(payload.get("phase") or "").strip() or "unknown"
    detail = str(payload.get("detail") or "").replace("\n", " ")[:500]
    logging.getLogger(__name__).info("planner_phase_client: phase=%s detail=%s", phase, detail[:240])
    evt = _record_planner_phase(phase, detail, payload.get("t"))
    resp: Dict[str, Any] = {"status": "ok", "seq": evt.get("seq")}
    if evt.get("bearings"):
        resp["bearings"] = evt["bearings"]
    return resp


@app.get("/api/osm/building-at-point")
def osm_building_at_point(lat: float, lon: float, radius_m: float = 50.0) -> Dict[str, Any]:
    """Return the nearest/containing OSM building near a selected point."""
    from ..geo.osm_map_provider import OSMMapProvider

    center = LatLon(lat=float(lat), lon=float(lon))
    provider = OSMMapProvider(cache_radius_m=max(100.0, float(radius_m) + 25.0))
    building = provider.find_building_at_point(center, radius_m=float(radius_m))
    return {
        "status": "ok",
        "center": center.model_dump(),
        "radius_m": float(radius_m),
        "found": building is not None,
        "building": building,
    }


@app.get("/api/osm/buildings-near-point")
def osm_buildings_near_point(lat: float, lon: float, radius_m: float = 50.0, max_count: Optional[int] = None) -> Dict[str, Any]:
    """Return OSM buildings within radius of a selected point."""
    from ..geo.osm_map_provider import OSMMapProvider

    center = LatLon(lat=float(lat), lon=float(lon))
    provider = OSMMapProvider(cache_radius_m=max(100.0, float(radius_m) + 25.0))
    buildings = provider.find_buildings_within_radius(center, radius_m=float(radius_m), max_count=max_count)
    return {
        "status": "ok",
        "center": center.model_dump(),
        "radius_m": float(radius_m),
        "found": bool(buildings),
        "count": len(buildings),
        "buildings": buildings,
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

    # Serve Cesium assets (repo root /Cesium) for /3d and for client-side Google mesh profile sampling.
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

    @app.get("/2d/planner")
    async def redirect_2d_planner():
        """Legacy bookmark: 2D planner is the default UI at /."""
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/", status_code=307)

    @app.get("/3d")
    async def serve_index_3d():
        """Serve the 3D Cesium UI explicitly."""
        idx3 = static_dir / "index_3d.html"
        if idx3.exists():
            return FileResponse(str(idx3), headers={"Cache-Control": "no-store"})
        from fastapi import HTTPException
        raise HTTPException(status_code=404)

    @app.get("/tests/google_mesh_radial_raycast_demo.html")
    async def serve_google_mesh_radial_raycast_demo():
        """Minimal Cesium page: fixed TX/RX, radial rays vs Google Photorealistic3D Tiles only."""
        demo_path = repo_root / "tests" / "google_mesh_radial_raycast_demo.html"
        if demo_path.exists():
            return FileResponse(str(demo_path), headers={"Cache-Control": "no-store"})
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

    @app.get("/integrated_2d_raytrace.js")
    async def serve_integrated_2d_raytrace_js():
        file_path = static_dir / "integrated_2d_raytrace.js"
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

    @app.get("/mesh_profiler_core.js")
    async def serve_mesh_profiler_core_js():
        file_path = static_dir / "mesh_profiler_core.js"
        if file_path.exists():
            return FileResponse(str(file_path))
        from fastapi import HTTPException
        raise HTTPException(status_code=404)
