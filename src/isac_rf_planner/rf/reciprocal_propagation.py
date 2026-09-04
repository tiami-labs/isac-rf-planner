"""Environment-aware reciprocal propagation field for bistatic/ISAC analysis.

The normal planner evaluates TX -> candidate target paths. For a fixed sensing
receiver, this module evaluates the reciprocal RX -> candidate-target field over
the same mapped environment, then resamples that field onto the TX target grid.
Propagation loss is reciprocal; receiver antenna gain and target scattering are
applied later by the bistatic link-budget layer.

Memory design:
- numerical return layers stay in compact NumPy arrays (float32/bool/uint8),
  never million-element Python-float/string lists;
- nearest-neighbour resampling queries the target grid in bounded chunks rather
  than allocating an N x 2 target coordinate matrix;
- the temporary RX-centered WorldCell collection is released immediately after
  extracting the numerical propagation field.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Optional

import numpy as np
from scipy.spatial import cKDTree

from ..pipeline.schemas import AttenuationGrid, LatLon, RFParams, WorldModel
from ..pipeline.world_builder import build_world_model
from .attenuation_models import _scenario_path_loss_db
from .channel_arrays import terrain_state_code, terrain_state_label, terrain_state_encoding_metadata


EARTH_RADIUS_M = 6_371_000.0
_RESAMPLE_CHUNK_POINTS = 131_072

@dataclass(slots=True)
class ReciprocalPropagationField:
    """Return-path values aligned one-for-one with the primary target grid."""

    path_loss_db: np.ndarray
    environment_loss_db: np.ndarray
    terrain_loss_db: np.ndarray
    los: np.ndarray
    terrain_state_code: np.ndarray
    sample_error_m: np.ndarray
    direct_path_loss_db: float
    direct_environment_loss_db: float
    direct_terrain_loss_db: float
    direct_los: bool
    direct_terrain_state_code: int
    direct_sample_error_m: float
    metadata: dict[str, Any]

    def terrain_state_at(self, index: int) -> str:
        return terrain_state_label(self.terrain_state_code[index])


def _surface_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1 = math.radians(float(lat1))
    p2 = math.radians(float(lat2))
    dp = p2 - p1
    dl = math.radians(float(lon2) - float(lon1))
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _xy_m(latitudes: np.ndarray, longitudes: np.ndarray, origin: LatLon) -> np.ndarray:
    """Return local XY coordinates with one allocation.

    The operation order matches the former subtract -> radians -> scale path, but
    writes directly into the two output columns instead of materializing x, y,
    and then a third column_stack result.
    """

    xy = np.empty((latitudes.size, 2), dtype=np.float64)
    np.subtract(longitudes, float(origin.lon), out=xy[:, 0])
    np.deg2rad(xy[:, 0], out=xy[:, 0])
    xy[:, 0] *= EARTH_RADIUS_M
    xy[:, 0] *= math.cos(math.radians(float(origin.lat)))
    np.subtract(latitudes, float(origin.lat), out=xy[:, 1])
    np.deg2rad(xy[:, 1], out=xy[:, 1])
    xy[:, 1] *= EARTH_RADIUS_M
    return xy


def _target_xy_chunk(
    latitudes: np.ndarray,
    longitudes: np.ndarray,
    origin: LatLon,
) -> np.ndarray:
    # scipy's cKDTree internally operates in doubles, so producing float64 here
    # avoids a second conversion/copy inside query(). The chunk bounds peak use.
    return _xy_m(latitudes, longitudes, origin)


def _cell_path_components(
    world: WorldModel,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    rf = world.rf_params
    tx_h = float(getattr(rf, "tx_height_m", 0.0) or 0.0)
    rx_h = float(getattr(rf, "rx_height_m", 0.0) or 0.0)
    freq_mhz = float(getattr(rf, "freq_mhz", 0.0) or 0.0)
    if getattr(rf, "dvt", None) is not None:
        freq_mhz = float(rf.dvt.frequency_mhz)

    count = len(world.cells)
    path = np.empty(count, dtype=np.float32)
    environment = np.empty(count, dtype=np.float32)
    terrain = np.empty(count, dtype=np.float32)
    los = np.empty(count, dtype=np.bool_)
    terrain_state = np.empty(count, dtype=np.uint8)

    for index, cell in enumerate(world.cells):
        d2 = max(float(cell.distance_m), 1.0)
        d3 = math.sqrt(d2 * d2 + (tx_h - rx_h) ** 2)
        if (
            bool(getattr(rf, "terrain_enabled", True))
            and getattr(cell, "z_rx_abs_m", None) is not None
            and world.z_tx_abs_m is not None
        ):
            dz = float(cell.z_rx_abs_m) - float(world.z_tx_abs_m)
            d3 = math.sqrt(d2 * d2 + dz * dz)

        scenario = _scenario_path_loss_db(
            distance_2d_m=d2,
            distance_3d_m=d3,
            freq_mhz=freq_mhz,
            tx_height_m=tx_h,
            rx_height_m=rx_h,
            is_los=bool(getattr(cell, "is_los", True)),
            rf_params=rf,
        )
        terrain_db = float(getattr(cell, "terrain_loss_db", 0.0) or 0.0)
        env_db = max(
            0.0,
            float(getattr(cell, "penetration_loss_db", 0.0) or 0.0)
            + float(getattr(cell, "shadow_loss_db", 0.0) or 0.0)
            + float(getattr(cell, "diffraction_loss_db", 0.0) or 0.0)
            + terrain_db
            - float(getattr(cell, "canyon_recovery_db", 0.0) or 0.0),
        )
        path[index] = scenario + env_db
        environment[index] = env_db
        terrain[index] = terrain_db
        los[index] = bool(getattr(cell, "is_los", True)) and bool(
            getattr(cell, "los_terrain", True)
        )
        terrain_state[index] = terrain_state_code(getattr(cell, "terrain_state", "los"))

    return path, environment, terrain, los, terrain_state


def _max_target_range_m(
    target_lat: np.ndarray,
    target_lon: np.ndarray,
    origin: LatLon,
) -> float:
    maximum = 0.0
    for start in range(0, target_lat.size, _RESAMPLE_CHUNK_POINTS):
        stop = min(start + _RESAMPLE_CHUNK_POINTS, target_lat.size)
        xy = _target_xy_chunk(target_lat[start:stop], target_lon[start:stop], origin)
        if xy.size:
            np.hypot(xy[:, 0], xy[:, 1], out=xy[:, 0])
            maximum = max(maximum, float(np.max(xy[:, 0])))
    return maximum


def _nearest_indices_chunked(
    tree: cKDTree,
    *,
    target_lat: np.ndarray,
    target_lon: np.ndarray,
    origin: LatLon,
) -> tuple[np.ndarray, np.ndarray]:
    count = int(target_lat.size)
    indices = np.empty(count, dtype=np.int32)
    errors = np.empty(count, dtype=np.float32)
    for start in range(0, count, _RESAMPLE_CHUNK_POINTS):
        stop = min(start + _RESAMPLE_CHUNK_POINTS, count)
        xy = _target_xy_chunk(target_lat[start:stop], target_lon[start:stop], origin)
        chunk_error, chunk_index = tree.query(xy, k=1, workers=-1)
        # Assignment performs the same casts without allocating two converted
        # chunk arrays on top of cKDTree's query outputs.
        indices[start:stop] = chunk_index
        errors[start:stop] = chunk_error
    return indices, errors


def build_reciprocal_propagation_field(
    *,
    primary_world: WorldModel,
    primary_grid: AttenuationGrid,
    map_provider: Optional[Any],
    terrain_provider: Optional[Any],
) -> ReciprocalPropagationField:
    """Build an RX-centered propagation field and resample it to target points."""

    config = primary_world.rf_params.channel_analysis
    if config is None:
        raise ValueError("reciprocal propagation requires channel_analysis configuration")
    receiver = config.receiver
    target = config.target
    rx = LatLon(lat=float(receiver.latitude), lon=float(receiver.longitude))

    # These become the authoritative aligned coordinates for the ISAC array bundle;
    # using NumPy once here avoids repeated list -> ndarray copies downstream.
    target_lat = np.asarray(primary_grid.cell_lat, dtype=np.float64)
    target_lon = np.asarray(primary_grid.cell_lon, dtype=np.float64)
    max_range = _max_target_range_m(target_lat, target_lon, rx)
    direct_distance = _surface_distance_m(rx.lat, rx.lon, primary_world.tx.lat, primary_world.tx.lon)
    max_range = max(max_range, direct_distance)

    # A coarser reciprocal lattice keeps the second environmental solve bounded;
    # the resulting propagation field is spatially resampled onto the primary grid.
    requested_step = float(getattr(config, "return_path_resolution_m", 50.0) or 50.0)
    step_m = max(float(primary_world.rf_params.step_m), requested_step)

    # Top-level assignments below do not mutate nested waveform models, so a shallow
    # Pydantic copy is sufficient and avoids cloning the complete RF configuration.
    return_rf: RFParams = primary_world.rf_params.model_copy(deep=False)
    return_rf.channel_analysis = None
    return_rf.sectors = None
    return_rf.max_range_m = max(step_m, float(max_range) + 2.0 * step_m)
    return_rf.step_m = step_m
    return_rf.tx_height_m = float(receiver.antenna_height_m_agl)
    return_rf.rx_height_m = float(target.height_m_agl)
    return_rf.site_altitude_m = float(receiver.altitude_m_amsl)
    # Do not terminate the reciprocal field based on a communications receiver
    # threshold. ISAC still needs the path-loss estimate for weak return regions.
    return_rf.termination_rsrp_dbm = -300.0

    reciprocal_world = build_world_model(
        tx=rx,
        rf_params=return_rf,
        views=[],
        map_provider=map_provider,
        terrain_provider=terrain_provider,
    )
    if not reciprocal_world.cells:
        raise ValueError("reciprocal propagation produced no cells")

    path, environment, terrain, los, terrain_state = _cell_path_components(reciprocal_world)
    sample_count = len(reciprocal_world.cells)
    sample_lat = np.fromiter((c.lat for c in reciprocal_world.cells), dtype=np.float64, count=sample_count)
    sample_lon = np.fromiter((c.lon for c in reciprocal_world.cells), dtype=np.float64, count=sample_count)

    # WorldCell Pydantic objects dominate the temporary reciprocal solve. No later
    # stage needs them once the compact numerical arrays have been extracted.
    reciprocal_world.cells.clear()

    sample_xy = _xy_m(sample_lat, sample_lon, rx)
    tree = cKDTree(sample_xy, compact_nodes=True, balanced_tree=True)
    del sample_xy, sample_lat, sample_lon

    indices, errors = _nearest_indices_chunked(
        tree,
        target_lat=target_lat,
        target_lon=target_lon,
        origin=rx,
    )

    direct_xy = _xy_m(
        np.asarray([primary_world.tx.lat], dtype=np.float64),
        np.asarray([primary_world.tx.lon], dtype=np.float64),
        rx,
    )
    direct_error, direct_index = tree.query(direct_xy, k=1)
    direct_i = int(np.asarray(direct_index).reshape(-1)[0])
    direct_err = float(np.asarray(direct_error).reshape(-1)[0])
    del tree, direct_xy

    # np.take writes compact aligned output arrays directly, avoiding the previous
    # ndarray -> Python-list -> ndarray round trip.
    aligned_path = np.empty(indices.size, dtype=np.float32)
    aligned_environment = np.empty(indices.size, dtype=np.float32)
    aligned_terrain = np.empty(indices.size, dtype=np.float32)
    aligned_los = np.empty(indices.size, dtype=np.bool_)
    aligned_state = np.empty(indices.size, dtype=np.uint8)
    np.take(path, indices, out=aligned_path)
    np.take(environment, indices, out=aligned_environment)
    np.take(terrain, indices, out=aligned_terrain)
    np.take(los, indices, out=aligned_los)
    np.take(terrain_state, indices, out=aligned_state)

    result = ReciprocalPropagationField(
        path_loss_db=aligned_path,
        environment_loss_db=aligned_environment,
        terrain_loss_db=aligned_terrain,
        los=aligned_los,
        terrain_state_code=aligned_state,
        sample_error_m=errors,
        direct_path_loss_db=float(path[direct_i]),
        direct_environment_loss_db=float(environment[direct_i]),
        direct_terrain_loss_db=float(terrain[direct_i]),
        direct_los=bool(los[direct_i]),
        direct_terrain_state_code=int(terrain_state[direct_i]),
        direct_sample_error_m=direct_err,
        metadata={
            "model": "environment_reciprocal",
            "source_latitude": rx.lat,
            "source_longitude": rx.lon,
            "source_height_m_agl": float(receiver.antenna_height_m_agl),
            "target_height_m_agl": float(target.height_m_agl),
            "resolution_m": step_m,
            "max_range_m": float(return_rf.max_range_m),
            "sample_count": sample_count,
            "mean_resample_error_m": float(np.mean(errors)) if errors.size else 0.0,
            "max_resample_error_m": float(np.max(errors)) if errors.size else 0.0,
            "direct_resample_error_m": direct_err,
            "terrain_enabled": bool(return_rf.terrain_enabled),
            "ray_mode": str(return_rf.ray_mode),
            "terrain_state_encoding": terrain_state_encoding_metadata(),
        },
    )
    del path, environment, terrain, los, terrain_state, indices, target_lat, target_lon
    return result

@dataclass(slots=True)
class TargetIlluminationField:
    """TX->candidate-target field evaluated at the ISAC target height."""

    incident_power_dbm: np.ndarray
    path_loss_db: np.ndarray
    environment_loss_db: np.ndarray
    terrain_loss_db: np.ndarray
    los: np.ndarray
    sample_error_m: np.ndarray
    metadata: dict[str, Any]


def build_target_illumination_field(
    *,
    primary_world: WorldModel,
    primary_grid: AttenuationGrid,
    map_provider: Optional[Any],
    terrain_provider: Optional[Any],
) -> TargetIlluminationField:
    """Build a TX-centered target-height propagation field and align it to the plan grid.

    Communications coverage and sensing illumination have different receiver/target
    heights. Reusing the communications incident-power field for an airborne target
    is physically wrong, so ISAC gets a separate, sequential target-height solve.
    The solve may be coarser than the output grid and is nearest-neighbour resampled
    with a reported error, matching the reciprocal-field memory strategy.
    """

    config = primary_world.rf_params.channel_analysis
    if config is None:
        raise ValueError("target illumination requires channel_analysis configuration")
    target = config.target
    target_lat = np.asarray(primary_grid.cell_lat, dtype=np.float64)
    target_lon = np.asarray(primary_grid.cell_lon, dtype=np.float64)
    requested_step = float(getattr(config, "return_path_resolution_m", 50.0) or 50.0)
    step_m = max(float(primary_world.rf_params.step_m), requested_step)

    target_rf: RFParams = primary_world.rf_params.model_copy(deep=False)
    target_rf.channel_analysis = None
    target_rf.step_m = step_m
    target_rf.rx_height_m = float(target.height_m_agl)
    target_rf.termination_rsrp_dbm = -300.0

    target_world = build_world_model(
        tx=primary_world.tx,
        rf_params=target_rf,
        views=[],
        map_provider=map_provider,
        terrain_provider=terrain_provider,
    )
    if not target_world.cells:
        raise ValueError("target-height illumination propagation produced no cells")

    # Import lazily to avoid a module cycle. DVT can emit propagation components
    # from the exact same attenuation loop, avoiding a second WorldCell traversal.
    from .attenuation_models import compute_attenuation_grid, _compute_single_dvt_grid

    is_dvt = str(getattr(target_rf, "technology", "") or "").strip().lower() == "dvt"
    if is_dvt:
        target_grid, world_path, world_environment, world_los = _compute_single_dvt_grid(
            target_world, capture_path_components=True
        )
        world_terrain = np.asarray(target_grid.terrain_loss_db, dtype=np.float32)
    else:
        target_grid = compute_attenuation_grid(target_world, apply_channel=False)
        world_path, world_environment, world_terrain, world_los, _ = _cell_path_components(target_world)

    sample_lat = np.asarray(target_grid.cell_lat, dtype=np.float64)
    sample_lon = np.asarray(target_grid.cell_lon, dtype=np.float64)
    sample_incident = np.asarray(target_grid.incident_power_isotropic_dbm, dtype=np.float32)
    illumination_sample_count = int(sample_incident.size)
    if sample_incident.size != sample_lat.size:
        raise ValueError("target-height illumination grid is not coordinate-aligned")
    if is_dvt and len(world_path) != sample_lat.size:
        raise ValueError("DVT target-height path components lost grid alignment")

    # DVT grid points are emitted one-for-one in WorldCell order. NR may contain
    # sector duplicates, so only NR needs a second physical-world coordinate tree.
    if not is_dvt:
        world_count = len(target_world.cells)
        world_lat = np.fromiter((c.lat for c in target_world.cells), dtype=np.float64, count=world_count)
        world_lon = np.fromiter((c.lon for c in target_world.cells), dtype=np.float64, count=world_count)
    target_world.cells.clear()

    origin = primary_world.tx
    grid_tree = cKDTree(_xy_m(sample_lat, sample_lon, origin), compact_nodes=True, balanced_tree=True)
    grid_indices, grid_errors = _nearest_indices_chunked(
        grid_tree, target_lat=target_lat, target_lon=target_lon, origin=origin,
    )
    del grid_tree, sample_lat, sample_lon

    aligned_incident = np.empty(grid_indices.size, dtype=np.float32)
    aligned_path = np.empty(grid_indices.size, dtype=np.float32)
    aligned_environment = np.empty(grid_indices.size, dtype=np.float32)
    aligned_terrain = np.empty(grid_indices.size, dtype=np.float32)
    aligned_los = np.empty(grid_indices.size, dtype=np.bool_)
    np.take(sample_incident, grid_indices, out=aligned_incident)

    if is_dvt:
        # Same source coordinates => same nearest indices/errors as the old second
        # KD-tree. Reuse them and preserve every aligned output value.
        np.take(world_path, grid_indices, out=aligned_path)
        np.take(world_environment, grid_indices, out=aligned_environment)
        np.take(world_terrain, grid_indices, out=aligned_terrain)
        np.take(world_los, grid_indices, out=aligned_los)
        world_errors = grid_errors
        del grid_indices
    else:
        del grid_indices
        world_tree = cKDTree(_xy_m(world_lat, world_lon, origin), compact_nodes=True, balanced_tree=True)
        world_indices, world_errors = _nearest_indices_chunked(
            world_tree, target_lat=target_lat, target_lon=target_lon, origin=origin,
        )
        del world_tree, world_lat, world_lon
        np.take(world_path, world_indices, out=aligned_path)
        np.take(world_environment, world_indices, out=aligned_environment)
        np.take(world_terrain, world_indices, out=aligned_terrain)
        np.take(world_los, world_indices, out=aligned_los)
        del world_indices

    del sample_incident, world_path, world_environment, world_terrain, world_los

    # Coordinate error from the actual target-height RF grid is the relevant error
    # for incident power; environment spatial error is also summarized separately.
    return TargetIlluminationField(
        incident_power_dbm=aligned_incident,
        path_loss_db=aligned_path,
        environment_loss_db=aligned_environment,
        terrain_loss_db=aligned_terrain,
        los=aligned_los,
        sample_error_m=grid_errors,
        metadata={
            "model": "environment_target_height_tx_field",
            "target_height_m_agl": float(target.height_m_agl),
            "resolution_m": step_m,
            "sample_count": illumination_sample_count,
            "mean_resample_error_m": float(np.mean(grid_errors)) if grid_errors.size else 0.0,
            "max_resample_error_m": float(np.max(grid_errors)) if grid_errors.size else 0.0,
            "mean_environment_resample_error_m": float(np.mean(world_errors)) if world_errors.size else 0.0,
            "max_environment_resample_error_m": float(np.max(world_errors)) if world_errors.size else 0.0,
            "terrain_enabled": bool(target_rf.terrain_enabled),
            "ray_mode": str(target_rf.ray_mode),
        },
    )
