"""Fast hypothesis reevaluation for persisted ISAC scene products.

A full plan builds the expensive physical scene once. This module changes target
RCS/motion and processing/detection assumptions in O(N) numerical work without
re-running OSM, terrain, TX propagation, or reciprocal RX propagation.
"""

from __future__ import annotations

from functools import lru_cache
import base64
import io
import json
import math
from types import SimpleNamespace
from typing import Any, Mapping

import numpy as np
from scipy.spatial import cKDTree

from ..geo.heatmap import attenuation_grid_to_png_ellipse, prepare_ellipse_heatmap_geometry
from .channel_analysis import (
    SPEED_OF_LIGHT_M_S, ChannelProcessing, ChannelReceiver, ChannelTarget, TargetMotion,
    bistatic_geometry, thermal_noise_power_dbm, coherent_processing_gain_db,
)
from .channel_products import channel_product_path




def _json_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None

SUPPORTED_LAYERS = {
    "bistatic_echo": ("Bistatic echo power", "dBm"),
    "bistatic_snr": ("Post-processing echo SNR", "dB"),
    "bistatic_margin": ("SNR margin vs effective N+I", "dB"),
    "rcs_margin": ("RCS margin for enabled power constraints", "dB"),
    "minimum_detectable_rcs": ("Minimum RCS for enabled power constraints", "dBsm"),
    "bistatic_doppler": ("Signed bistatic Doppler", "Hz"),
    "doppler_sensitivity": ("Bistatic Doppler sensitivity", "Hz/(m/s)"),
    "minimum_detectable_speed": ("Best-heading minimum detectable speed", "m/s"),
    "required_cancellation": ("Required direct-path cancellation", "dB"),
    "direct_residual_margin": ("Direct-path constraint margin", "dB"),
    "screening_detectable": ("Screening detectability", "flag"),
    "qualified_detectable": ("Processing-qualified detectability", "flag"),
    "bistatic_detectable": ("Processing-qualified detectability", "flag"),
    "static_clutter_delay_separation": ("Nearest static facade clutter delay separation", "us"),
    "static_clutter_overlap": ("Static facade clutter overlap in ideal delay-Doppler cell", "flag"),
    "static_clutter_path_count": ("Mapped static facade paths in target delay-Doppler cell", "count"),
    "target_measurement_cell": ("Selected-target ideal delay-Doppler ambiguity cell", "flag"),
}


def _merge_model(model_cls: Any, base: Mapping[str, Any], override: Mapping[str, Any] | None) -> Any:
    payload = dict(base or {})
    if override:
        payload.update(dict(override))
    return model_cls.model_validate(payload)


def _metadata(product: Any) -> dict[str, Any]:
    if "metadata_json" not in product:
        return {}
    return json.loads(str(product["metadata_json"]))


def _product_metadata(product_id: str) -> dict[str, Any]:
    path = channel_product_path(product_id)
    if path is None or not path.is_file():
        raise FileNotFoundError(product_id)
    with np.load(path, allow_pickle=False) as product:
        return _metadata(product)


def _static_background_path_rows(summary: Mapping[str, Any]) -> list[dict[str, Any]]:
    background = summary.get("static_background_channel") or {}
    rows = background.get("paths") or []
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def _static_background_arrays(summary: Mapping[str, Any]) -> np.ndarray:
    """Return geometry-only mapped-facade excess delays."""
    rows = _static_background_path_rows(summary)
    if not rows:
        return np.empty(0, dtype=np.float64)
    delays = np.fromiter(
        (float(row.get("excess_delay_s", math.nan)) for row in rows),
        dtype=np.float64,
        count=len(rows),
    )
    return delays[np.isfinite(delays)]


def _group_static_background_by_delay_bin(
    delays_s: np.ndarray,
    *,
    delay_resolution_s: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Return occupied ideal-delay bins and mapped-path counts."""
    if delays_s.size == 0:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int32)
    res = max(float(delay_resolution_s), 1.0e-15)
    bins = np.rint(np.asarray(delays_s, dtype=np.float64) / res).astype(np.int64)
    unique, counts = np.unique(bins, return_counts=True)
    return unique, counts.astype(np.int32, copy=False)


def _static_background_grid_metrics(
    *,
    target_excess_delay_s: np.ndarray,
    target_doppler_hz: np.ndarray,
    path_delays_s: np.ndarray,
    delay_resolution_s: float,
    doppler_resolution_hz: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return geometry-only static-facade delay/Doppler occupancy metrics.

    Returns ``(nearest_delay_separation_us, overlap, same_cell_path_count)``.
    No facade amplitude is synthesized.
    """
    delays = np.asarray(target_excess_delay_s, dtype=np.float64)
    doppler = np.asarray(target_doppler_hz, dtype=np.float32)
    count = int(delays.size)
    nearest_us = np.full(count, np.inf, dtype=np.float32)
    overlap = np.zeros(count, dtype=np.bool_)
    path_count = np.zeros(count, dtype=np.int32)
    if path_delays_s.size == 0 or count == 0:
        return nearest_us, overlap, path_count

    sorted_delays = np.sort(np.asarray(path_delays_s, dtype=np.float64))
    idx = np.searchsorted(sorted_delays, delays, side="left")
    left_idx = np.clip(idx - 1, 0, sorted_delays.size - 1)
    right_idx = np.clip(idx, 0, sorted_delays.size - 1)
    nearest = np.minimum(
        np.abs(delays - sorted_delays[left_idx]),
        np.abs(delays - sorted_delays[right_idx]),
    )
    nearest_us[:] = (nearest * 1.0e6).astype(np.float32)

    occupied_bins, occupied_counts = _group_static_background_by_delay_bin(
        path_delays_s, delay_resolution_s=delay_resolution_s
    )
    if occupied_bins.size == 0:
        return nearest_us, overlap, path_count
    target_bins = np.rint(delays / max(float(delay_resolution_s), 1.0e-15)).astype(np.int64)
    pos = np.searchsorted(occupied_bins, target_bins, side="left")
    valid_pos = pos < occupied_bins.size
    matched = np.zeros(count, dtype=np.bool_)
    matched[valid_pos] = occupied_bins[pos[valid_pos]] == target_bins[valid_pos]
    same_doppler_cell = np.abs(doppler) <= np.float32(
        0.5 * max(float(doppler_resolution_hz), 1.0e-12)
    )
    overlap[:] = matched & same_doppler_cell
    if np.any(overlap):
        path_count[overlap] = occupied_counts[pos[overlap]]
    return nearest_us, overlap, path_count


def _selected_static_background(
    summary: Mapping[str, Any],
    *,
    target_excess_delay_s: float,
    target_doppler_hz: float,
    delay_resolution_s: float,
    doppler_resolution_hz: float,
) -> dict[str, Any]:
    """Describe mapped-facade geometry around one target measurement cell."""
    rows = _static_background_path_rows(summary)
    background = dict(summary.get("static_background_channel") or {})
    common = {
        "model": background.get("model"),
        "status": background.get("status"),
        "power_basis": "none_geometry_only",
        "absolute_scatter_power_available": False,
        "calibrated_absolute_clutter_power": False,
        "candidate_walls": background.get("candidate_walls"),
        "geometric_specular_candidates": background.get("geometric_specular_candidates"),
        "retained_visibility_candidates": background.get("retained_visibility_candidates"),
        "evaluated_wall_candidates": background.get("evaluated_wall_candidates"),
        "candidate_search_complete": background.get("candidate_search_complete"),
        "geometry_search_complete": background.get("geometry_search_complete"),
        "accepted_paths": background.get("accepted_paths", 0),
        "rejection_counts": background.get("rejection_counts") or {},
        "delay_basis": background.get("delay_basis", "2d_horizontal_path_geometry"),
        "ideal_bin_model_only": True,
        "waveform_ambiguity_sidelobes_modeled": False,
        "delay_resolution_s": float(delay_resolution_s),
        "doppler_resolution_hz": float(doppler_resolution_hz),
    }
    if not rows:
        status = str(background.get("status") or "")
        reason = (
            "visibility search incomplete; no accepted mapped facade path in evaluated candidates"
            if status == "incomplete_no_visible_specular_paths"
            else "no accepted mapped facade paths"
        )
        return {"available": False, "reason": reason, **common}

    target_bin = int(round(float(target_excess_delay_s) / max(float(delay_resolution_s), 1.0e-15)))
    same_doppler = abs(float(target_doppler_hz)) <= 0.5 * max(float(doppler_resolution_hz), 1.0e-12)
    contributors: list[dict[str, Any]] = []
    nearest = None
    nearest_sep = math.inf
    delay_doppler_paths: list[dict[str, Any]] = []
    for row in rows:
        delay = _json_number(row.get("excess_delay_s"))
        if delay is None:
            continue
        sep = abs(delay - float(target_excess_delay_s))
        if sep < nearest_sep:
            nearest_sep = sep
            nearest = dict(row)
        item = {
            "building_id": row.get("building_id"),
            "material": str(row.get("material") or "unknown"),
            "bounce_latitude_deg": _json_number(row.get("bounce_latitude_deg")),
            "bounce_longitude_deg": _json_number(row.get("bounce_longitude_deg")),
            "tx_to_bounce_range_m": _json_number(row.get("tx_to_bounce_range_m")),
            "bounce_to_rx_range_m": _json_number(row.get("bounce_to_rx_range_m")),
            "total_range_m": _json_number(row.get("total_range_m")),
            "excess_range_m": _json_number(row.get("excess_range_m")),
            "excess_delay_s": delay,
            "doppler_hz": 0.0,
        }
        delay_doppler_paths.append(item)
        if same_doppler and int(round(delay / max(float(delay_resolution_s), 1.0e-15))) == target_bin:
            contributors.append(item)

    contributors.sort(key=lambda row: abs(float(row["excess_delay_s"]) - float(target_excess_delay_s)))
    delay_doppler_paths.sort(key=lambda row: float(row["excess_delay_s"]))
    return {
        "available": True,
        **common,
        "target_delay_bin": target_bin,
        "target_in_static_doppler_cell": bool(same_doppler),
        "nearest_static_path_delay_separation_s": _json_number(nearest_sep),
        "nearest_static_path": nearest,
        "same_cell_contributor_count": len(contributors),
        "same_cell_contributors": contributors[:20],
        "delay_doppler_paths": delay_doppler_paths,
        "delay_doppler_path_count": len(delay_doppler_paths),
    }


@lru_cache(maxsize=2)
def _cached_heatmap_geometry(product_id: str, size: int):
    """Bounded cache: spatial binning is invariant across target hypotheses."""

    path = channel_product_path(product_id)
    if path is None or not path.is_file():
        raise FileNotFoundError(product_id)
    with np.load(path, allow_pickle=False) as product:
        lat = np.asarray(product["latitude_deg"], dtype=np.float64)
        lon = np.asarray(product["longitude_deg"], dtype=np.float64)
        meta = _metadata(product)
    tx = meta.get("transmitter") or {}
    rf = meta.get("rf_config") or {}
    tx_lat = float(tx.get("latitude"))
    tx_lon = float(tx.get("longitude"))
    radius_m = float(rf.get("max_range_m", 0.0) or 0.0)
    step_m = float(rf.get("step_m", 20.0) or 20.0)
    dtheta_deg = float(rf.get("dtheta_deg", 0.25) or 0.25)

    class _Grid:
        def __init__(self):
            self.tx = SimpleNamespace(lat=tx_lat, lon=tx_lon)
            self.rf_params = SimpleNamespace(max_range_m=radius_m, step_m=step_m, dtheta_deg=dtheta_deg)
        def channel_array(self, name: str):
            return lat if name == "cell_lat" else lon if name == "cell_lon" else None

    return prepare_ellipse_heatmap_geometry(_Grid(), size=int(size))


@lru_cache(maxsize=1)
def _cached_scene_target_tree(product_id: str):
    """Spatial index for exact-click interpolation; built once per active product."""
    path = channel_product_path(product_id)
    if path is None or not path.is_file():
        raise FileNotFoundError(product_id)
    with np.load(path, allow_pickle=False) as product:
        lat = np.asarray(product["latitude_deg"], dtype=np.float64)
        lon = np.asarray(product["longitude_deg"], dtype=np.float64)
        meta = _metadata(product)
    tx = meta.get("transmitter") or {}
    origin_lat = float(tx.get("latitude"))
    origin_lon = float(tx.get("longitude"))
    earth_m = 6_371_000.0
    x = np.radians(lon - origin_lon) * earth_m * math.cos(math.radians(origin_lat))
    y = np.radians(lat - origin_lat) * earth_m
    xy = np.column_stack((x, y))
    tree = cKDTree(xy, compact_nodes=True, balanced_tree=True)
    return tree, origin_lat, origin_lon


def _scene_target_neighbors(
    product_id: str,
    latitude: float,
    longitude: float,
    *,
    k: int = 4,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    tree, origin_lat, origin_lon = _cached_scene_target_tree(product_id)
    earth_m = 6_371_000.0
    qx = math.radians(float(longitude) - origin_lon) * earth_m * math.cos(math.radians(origin_lat))
    qy = math.radians(float(latitude) - origin_lat) * earth_m
    kk = max(1, min(int(k), int(tree.n)))
    dist, idx = tree.query([qx, qy], k=kk)
    dist = np.atleast_1d(np.asarray(dist, dtype=np.float64))
    idx = np.atleast_1d(np.asarray(idx, dtype=np.int64))
    if dist[0] <= 1.0e-6:
        weights = np.zeros(dist.size, dtype=np.float64)
        weights[0] = 1.0
    else:
        inv = 1.0 / np.maximum(dist, 0.25) ** 2
        weights = inv / np.sum(inv)
    return idx, dist, weights


def _interp_product_value(
    product: Any,
    name: str,
    indices: np.ndarray,
    weights: np.ndarray,
    *,
    default: float = math.nan,
) -> float:
    if name not in product:
        return float(default)
    arr = np.asarray(product[name])
    vals = np.asarray(arr[indices], dtype=np.float64)
    finite = np.isfinite(vals)
    if not np.any(finite):
        return float(default)
    w = np.asarray(weights, dtype=np.float64)[finite]
    if float(np.sum(w)) <= 0.0:
        return float(vals[finite][0])
    w /= np.sum(w)
    return float(np.dot(vals[finite], w))


def _nearest_product_bool(product: Any, name: str, nearest_index: int) -> bool | None:
    if name not in product:
        return None
    return bool(np.asarray(product[name], dtype=np.bool_)[int(nearest_index)])


def _horizontal_surface_distance_scalar(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1 = math.radians(float(lat1)); p2 = math.radians(float(lat2))
    dp = p2 - p1; dl = math.radians(float(lon2) - float(lon1))
    h = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * 6_371_000.0 * math.asin(min(1.0, math.sqrt(max(0.0, h))))


def _horizontal_excess_delay_scalar(
    tx_lat: float, tx_lon: float, target_lat: float, target_lon: float, rx_lat: float, rx_lon: float
) -> float:
    total = (
        _horizontal_surface_distance_scalar(tx_lat, tx_lon, target_lat, target_lon)
        + _horizontal_surface_distance_scalar(target_lat, target_lon, rx_lat, rx_lon)
    )
    direct = _horizontal_surface_distance_scalar(tx_lat, tx_lon, rx_lat, rx_lon)
    return max(0.0, total - direct) / SPEED_OF_LIGHT_M_S


def _horizontal_excess_delay_array(
    lat: np.ndarray, lon: np.ndarray, *, tx_lat: float, tx_lon: float, rx_lat: float, rx_lon: float
) -> np.ndarray:
    """Vectorized spherical 2-D excess delay used only by the geometry-only facade channel."""
    r = 6_371_000.0
    latr = np.radians(np.asarray(lat, dtype=np.float64))
    lonr = np.radians(np.asarray(lon, dtype=np.float64))
    def dist_to(p_lat: float, p_lon: float) -> np.ndarray:
        plat = math.radians(float(p_lat)); plon = math.radians(float(p_lon))
        dlat = latr - plat; dlon = lonr - plon
        h = np.sin(dlat * 0.5) ** 2 + math.cos(plat) * np.cos(latr) * np.sin(dlon * 0.5) ** 2
        np.clip(h, 0.0, 1.0, out=h)
        return 2.0 * r * np.arcsin(np.sqrt(h))
    dtx = dist_to(tx_lat, tx_lon)
    drx = dist_to(rx_lat, rx_lon)
    direct = _horizontal_surface_distance_scalar(tx_lat, tx_lon, rx_lat, rx_lon)
    out = dtx + drx
    out -= direct
    np.maximum(out, 0.0, out=out)
    out /= SPEED_OF_LIGHT_M_S
    return out


def _render_binary_target_overlay(
    product_id: str, mask: np.ndarray, *, size: int, label: str
) -> dict[str, Any]:
    """Render only true measurement-cell samples; false samples remain transparent."""
    geometry = _cached_heatmap_geometry(product_id, int(size))
    values = np.asarray(mask, dtype=np.bool_)
    if values.size != geometry.point_count:
        raise ValueError("target measurement overlay length mismatch")
    pix = np.zeros(int(size) * int(size), dtype=np.uint8)
    sample_idx = geometry.sample_indices
    selected = values[sample_idx]
    if np.any(selected):
        np.maximum.at(pix, geometry.linear_pixel_indices[selected], np.uint8(1))
    pix = pix.reshape((int(size), int(size)))
    rgba = np.zeros((int(size), int(size), 4), dtype=np.uint8)
    rgba[..., 0][pix > 0] = 255
    rgba[..., 1][pix > 0] = 235
    rgba[..., 2][pix > 0] = 59
    alpha = np.minimum(geometry.feather_alpha, np.uint8(150))
    rgba[..., 3][pix > 0] = alpha[pix > 0]
    from PIL import Image
    image = Image.fromarray(rgba, mode="RGBA")
    buf = io.BytesIO(); image.save(buf, format="PNG", optimize=True)
    return {
        "png_b64": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii"),
        "width": int(size), "height": int(size), "radius_m": float(geometry.radius_m),
        "layer": "target_measurement_cell", "label": label, "units": "flag",
        "actual_min": 0.0, "actual_max": 1.0,
        "point_count": int(np.count_nonzero(values)),
    }


def _render_layer(product_id: str, values: np.ndarray, *, size: int, layer: str) -> dict[str, Any]:
    geometry = _cached_heatmap_geometry(product_id, int(size))

    # Avoid rebuilding an AttenuationGrid/Pydantic object for interactive analysis.
    dummy = SimpleNamespace(channel_array=lambda name: None)
    if layer in {"screening_detectable", "qualified_detectable", "bistatic_detectable", "static_clutter_overlap", "target_measurement_cell"}:
        vmin, vmax = 0.0, 1.0
    elif layer == "static_clutter_path_count":
        finite = values[np.isfinite(values)]
        vmin, vmax = 0.0, max(1.0, float(np.nanpercentile(finite, 99.0)) if finite.size else 1.0)
    elif layer == "static_clutter_delay_separation":
        finite = values[np.isfinite(values)]
        vmin, vmax = 0.0, float(np.nanpercentile(finite, 95.0)) if finite.size else 1.0
    elif layer == "doppler_sensitivity":
        finite = values[np.isfinite(values)]
        vmin, vmax = 0.0, float(np.nanpercentile(finite, 99.0)) if finite.size else 1.0
    elif layer == "minimum_detectable_speed":
        finite = values[np.isfinite(values)]
        vmin, vmax = 0.0, float(np.nanpercentile(finite, 95.0)) if finite.size else 1.0
    elif layer == "required_cancellation":
        vmin, vmax = 0.0, 120.0
    elif layer == "minimum_detectable_rcs":
        finite = values[np.isfinite(values)]
        if finite.size:
            vmin, vmax = float(np.nanpercentile(finite, 2.0)), float(np.nanpercentile(finite, 98.0))
        else:
            vmin, vmax = -30.0, 30.0
    elif layer in {"bistatic_margin", "rcs_margin", "direct_residual_margin"}:
        vmin, vmax = -40.0, 30.0
    elif layer == "bistatic_snr":
        vmin, vmax = -40.0, 40.0
    elif layer == "bistatic_echo":
        vmin, vmax = -200.0, -80.0
    elif layer == "bistatic_doppler":
        finite = np.abs(values[np.isfinite(values)])
        span = max(1.0, float(np.nanpercentile(finite, 98.0)) if finite.size else 500.0)
        vmin, vmax = -span, span
    else:
        vmin = vmax = None

    rendered = attenuation_grid_to_png_ellipse(
        dummy,
        size=int(size),
        vmin=vmin,
        vmax=vmax,
        rsrp_values=values,
        geometry=geometry,
    )
    rendered["layer"] = layer
    rendered["label"], rendered["units"] = SUPPORTED_LAYERS[layer]
    return rendered



def _exact_selected_target_state(
    product: Any,
    *,
    summary: Mapping[str, Any],
    tx_meta: Mapping[str, Any],
    receiver_model: ChannelReceiver,
    target: ChannelTarget,
    motion: TargetMotion,
    processing: ChannelProcessing,
    frequency_hz: float,
    bandwidth_hz: float,
    noise_power_dbm: float,
    used_gain_db: float,
    processing_qualified: bool,
    direct_received_dbm: float,
    residual_direct_dbm: float,
    receiver_chain_db: float,
    return_excess_delta_db: float,
    rcs_m2: float,
    selected_lat: float,
    selected_lon: float,
    indices: np.ndarray,
    distances: np.ndarray,
    weights: np.ndarray,
    doppler_threshold_hz: float,
    doppler_resolution_hz: float,
) -> dict[str, Any]:
    """Evaluate one placed target at its exact coordinate using cached scene interpolation."""
    nearest_i = int(indices[0])
    z_ground = _interp_product_value(product, "z_ground_m", indices, weights, default=0.0)
    target_abs = float(z_ground) + float(target.height_m_agl)
    tx_abs = float(tx_meta.get("absolute_height_m", 0.0) or 0.0)
    rx_abs = float(receiver_model.absolute_height_m)

    def geom_for(test_motion: TargetMotion):
        return bistatic_geometry(
            tx_latitude_deg=float(tx_meta.get("latitude")),
            tx_longitude_deg=float(tx_meta.get("longitude")),
            tx_altitude_m=tx_abs,
            target_latitude_deg=float(selected_lat),
            target_longitude_deg=float(selected_lon),
            target_altitude_m=target_abs,
            receiver_latitude_deg=float(receiver_model.latitude),
            receiver_longitude_deg=float(receiver_model.longitude),
            receiver_altitude_m=rx_abs,
            target_motion=test_motion,
            frequency_hz=float(frequency_hz),
        )

    geometry = geom_for(motion)
    east_coeff = geom_for(TargetMotion(speedMps=1.0, headingDegTrue=90.0, climbRateMps=0.0)).doppler_hz
    north_coeff = geom_for(TargetMotion(speedMps=1.0, headingDegTrue=0.0, climbRateMps=0.0)).doppler_hz
    up_coeff = geom_for(TargetMotion(speedMps=0.0, headingDegTrue=0.0, climbRateMps=1.0)).doppler_hz
    sensitivity = math.sqrt(east_coeff * east_coeff + north_coeff * north_coeff + up_coeff * up_coeff)
    min_speed = doppler_threshold_hz / sensitivity if sensitivity > 1.0e-12 else math.inf

    echo_base = _interp_product_value(product, "echo_geometry_base_dbm", indices, weights)
    rcs_dbsm = 10.0 * math.log10(max(float(rcs_m2), 1.0e-30))
    echo_dbm = echo_base + float(receiver_chain_db) + rcs_dbsm - float(return_excess_delta_db)
    snr_margin = (
        echo_dbm - float(noise_power_dbm) + float(used_gain_db)
        - float(processing.processing_loss_db) - float(processing.required_snr_db)
    )
    post_snr = snr_margin + float(processing.required_snr_db)
    direct_margin = echo_dbm - (
        float(residual_direct_dbm) + float(processing.required_echo_to_residual_direct_db)
    )
    if processing.max_receiver_dynamic_range_db is None:
        dynamic_margin = math.nan
        dynamic_ok = not bool(processing.require_dynamic_range_constraint)
    else:
        dynamic_margin = echo_dbm + float(processing.max_receiver_dynamic_range_db) - float(direct_received_dbm)
        dynamic_ok = (dynamic_margin >= 0.0) if processing.require_dynamic_range_constraint else True

    power_margin = snr_margin
    if processing.require_direct_path_constraint:
        power_margin = min(power_margin, direct_margin)
    if processing.require_dynamic_range_constraint:
        power_margin = min(power_margin, dynamic_margin if math.isfinite(dynamic_margin) else -math.inf)

    doppler_hz = float(geometry.doppler_hz)
    doppler_resolved = abs(doppler_hz) >= float(doppler_threshold_hz)
    doppler_ambiguous = (
        False if processing.pulse_repetition_frequency_hz is None
        else abs(doppler_hz) > float(processing.pulse_repetition_frequency_hz) / 2.0
    )
    snr_ok = snr_margin >= 0.0
    direct_ok = (
        direct_margin >= 0.0 if processing.require_direct_path_constraint else True
    )
    environment_valid = bool(_nearest_product_bool(product, "return_environment_valid", nearest_i))
    screening = bool(snr_ok and doppler_resolved and not doppler_ambiguous and direct_ok and dynamic_ok and environment_valid)

    failed: list[str] = []
    if not snr_ok: failed.append("snr_noise_interference")
    if not doppler_resolved: failed.append("doppler_resolution")
    if doppler_ambiguous: failed.append("doppler_ambiguity")
    if processing.require_direct_path_constraint and not direct_ok: failed.append("direct_residual")
    if processing.require_dynamic_range_constraint and not dynamic_ok: failed.append("dynamic_range")
    if not environment_valid: failed.append("environment_return")
    if not processing_qualified: failed.append("processing_qualification")
    failure_code = (
        (0 if snr_ok else 1)
        | (0 if doppler_resolved else 2)
        | (4 if doppler_ambiguous else 0)
        | (8 if processing.require_direct_path_constraint and not direct_ok else 0)
        | (16 if processing.require_dynamic_range_constraint and not dynamic_ok else 0)
        | (0 if processing_qualified else 32)
        | (0 if environment_valid else 64)
    )

    tx_path = _interp_product_value(product, "tx_target_path_loss_db", indices, weights)
    tx_env = _interp_product_value(product, "tx_target_environment_loss_db", indices, weights)
    tx_terrain = _interp_product_value(product, "tx_target_terrain_loss_db", indices, weights)
    tx_sample = _interp_product_value(product, "tx_target_sample_error_m", indices, weights)
    return_path = _interp_product_value(product, "return_path_loss_db", indices, weights) + float(return_excess_delta_db)
    return_env = _interp_product_value(product, "return_environment_loss_db", indices, weights)
    return_terrain = _interp_product_value(product, "return_terrain_loss_db", indices, weights)
    return_sample = _interp_product_value(product, "return_sample_error_m", indices, weights)
    incident = _interp_product_value(product, "incident_isotropic_power_dbm", indices, weights)
    tx_los = _nearest_product_bool(product, "tx_target_los", nearest_i)
    return_los = _nearest_product_bool(product, "return_los", nearest_i)

    horizontal_excess_delay = _horizontal_excess_delay_scalar(
        float(tx_meta.get("latitude")), float(tx_meta.get("longitude")),
        float(selected_lat), float(selected_lon),
        float(receiver_model.latitude), float(receiver_model.longitude),
    )
    static_bg = _selected_static_background(
        summary,
        target_excess_delay_s=horizontal_excess_delay,
        target_doppler_hz=doppler_hz,
        delay_resolution_s=1.0 / max(float(bandwidth_hz), 1.0),
        doppler_resolution_hz=float(doppler_resolution_hz),
    )

    speed_norm = math.sqrt(float(motion.speed_mps) ** 2 + float(motion.climb_rate_mps) ** 2)
    min_rcs = float(rcs_m2) * (10.0 ** (-power_margin / 10.0)) if math.isfinite(power_margin) else math.inf
    return {
        "index": nearest_i,
        "latitude": float(selected_lat),
        "longitude": float(selected_lon),
        "query_distance_m": float(distances[0]),
        "scene_interpolation": {
            "method": "inverse_distance_squared_4_nearest",
            "neighbor_indices": [int(v) for v in indices.tolist()],
            "neighbor_distances_m": [float(v) for v in distances.tolist()],
            "max_neighbor_distance_m": float(np.max(distances)),
        },
        "target_height_agl_m": float(target.height_m_agl),
        "ground_height_m_amsl": float(z_ground),
        "absolute_height_m": float(target_abs),
        "bistatic_rcs_m2": float(rcs_m2),
        "tx_target_range_m": float(geometry.tx_target_range_m),
        "target_receiver_range_m": float(geometry.target_receiver_range_m),
        "bistatic_path_range_m": float(geometry.bistatic_path_range_m),
        "excess_path_range_m": float(geometry.excess_path_range_m),
        "excess_delay_s": float(geometry.excess_delay_s),
        "background_horizontal_excess_delay_s": float(horizontal_excess_delay),
        "bistatic_angle_deg": float(geometry.bistatic_angle_deg),
        "incident_isotropic_power_dbm": _json_number(incident),
        "tx_target_path_loss_db": _json_number(tx_path),
        "tx_target_environment_loss_db": _json_number(tx_env),
        "tx_target_terrain_loss_db": _json_number(tx_terrain),
        "tx_target_los": tx_los,
        "tx_target_sample_error_m": _json_number(tx_sample),
        "return_path_loss_db": _json_number(return_path),
        "return_environment_loss_db": _json_number(return_env),
        "return_terrain_loss_db": _json_number(return_terrain),
        "return_los": return_los,
        "return_sample_error_m": _json_number(return_sample),
        "return_environment_valid": environment_valid,
        "echo_power_dbm": _json_number(echo_dbm),
        "postprocessing_snr_db": _json_number(post_snr),
        "detection_margin_db": _json_number(snr_margin),
        "rcs_margin_db": _json_number(power_margin),
        "minimum_detectable_rcs_m2": _json_number(min_rcs),
        "doppler_hz": _json_number(doppler_hz),
        "doppler_east_hz_per_mps": _json_number(east_coeff),
        "doppler_north_hz_per_mps": _json_number(north_coeff),
        "doppler_up_hz_per_mps": _json_number(up_coeff),
        "doppler_sensitivity_hz_per_mps": _json_number(sensitivity),
        "motion_doppler_sensitivity_hz_per_mps": _json_number(doppler_hz / speed_norm if speed_norm > 0 else 0.0),
        "minimum_detectable_speed_mps": _json_number(min_speed),
        "echo_to_residual_direct_db": _json_number(echo_dbm - float(residual_direct_dbm)),
        "direct_residual_margin_db": _json_number(direct_margin),
        "required_cancellation_db": _json_number(max(0.0, float(direct_received_dbm) - echo_dbm + float(processing.required_echo_to_residual_direct_db))),
        "required_dynamic_range_db": _json_number(float(direct_received_dbm) - echo_dbm),
        "dynamic_range_margin_db": _json_number(dynamic_margin),
        "snr_noise_interference_ok": bool(snr_ok),
        "doppler_resolved": bool(doppler_resolved),
        "doppler_ambiguous": bool(doppler_ambiguous),
        "direct_residual_ok": bool(direct_ok),
        "dynamic_range_ok": bool(dynamic_ok),
        "detectable_screening": bool(screening),
        "detectable_qualified": bool(screening and processing_qualified),
        "constraint_failure_code": int(failure_code),
        "failed_constraints": failed,
        "static_background": static_bg,
    }


def evaluate_channel_product(
    product_id: str,
    *,
    receiver_override: Mapping[str, Any] | None = None,
    target_override: Mapping[str, Any] | None = None,
    motion_override: Mapping[str, Any] | None = None,
    processing_override: Mapping[str, Any] | None = None,
    layer: str = "qualified_detectable",
    selected_latitude: float | None = None,
    selected_longitude: float | None = None,
    image_size: int = 1024,
) -> dict[str, Any]:
    """Reevaluate a target/process hypothesis against a persisted physical scene.

    Time is O(N).  Peak numerical scratch keeps a small constant number of N-sized
    arrays and reuses them in place; OSM/terrain/world objects are never rebuilt.
    """

    if layer not in SUPPORTED_LAYERS:
        raise ValueError(f"unsupported ISAC layer: {layer}")
    size = max(256, min(1536, int(image_size)))
    path = channel_product_path(product_id)
    if path is None or not path.is_file():
        raise FileNotFoundError(product_id)

    with np.load(path, allow_pickle=False) as product:
        meta = _metadata(product)
        summary = meta.get("summary") or {}
        target = _merge_model(ChannelTarget, summary.get("target") or {}, target_override)
        motion = _merge_model(TargetMotion, summary.get("motion") or {}, motion_override)
        processing = _merge_model(ChannelProcessing, summary.get("processing") or {}, processing_override)
        scene_target = ChannelTarget.model_validate(summary.get("target") or {})
        if abs(float(target.height_m_agl) - float(scene_target.height_m_agl)) > 1.0e-9:
            raise ValueError("target height changes propagation geometry and requires a new RF plan")

        required = {
            "echo_geometry_base_dbm", "doppler_east_hz_per_mps",
            "doppler_north_hz_per_mps", "doppler_up_hz_per_mps",
            "doppler_sensitivity_hz_per_mps", "return_environment_valid",
            "latitude_deg", "longitude_deg",
        }
        missing = sorted(required.difference(product.files))
        if missing:
            raise ValueError(f"product predates reusable ISAC scene basis: missing {', '.join(missing)}")

        base_receiver = ChannelReceiver.model_validate(summary.get("receiver") or {})
        receiver_model = _merge_model(ChannelReceiver, summary.get("receiver") or {}, receiver_override)
        for field_name in ("latitude", "longitude", "altitude_m_amsl", "antenna_height_m_agl"):
            if abs(float(getattr(receiver_model, field_name)) - float(getattr(base_receiver, field_name))) > 1.0e-9:
                raise ValueError("receiver position/height changes propagation geometry and requires a new RF plan")
        receiver = receiver_model.model_dump(by_alias=True)
        direct = summary.get("direct_path") or {}
        tx_meta = meta.get("transmitter") or {}
        rf_meta = meta.get("rf_config") or {}
        frequency_hz = float(summary.get("frequency_hz") or (float(rf_meta.get("freq_mhz", 0.0) or 0.0) * 1e6) or 1.0)
        bandwidth_hz = float(processing.processing_bandwidth_hz or summary.get("waveform_bandwidth_hz") or 1.0)
        thermal_noise_dbm = thermal_noise_power_dbm(bandwidth_hz, float(receiver_model.noise_figure_db))
        interference_dbm = processing.interference_plus_clutter_power_dbm
        if interference_dbm is None:
            noise_power_dbm = thermal_noise_dbm
        else:
            noise_power_dbm = 10.0 * math.log10(
                10.0 ** (thermal_noise_dbm / 10.0) + 10.0 ** (float(interference_dbm) / 10.0)
            )
        ideal_gain_db = coherent_processing_gain_db(bandwidth_hz, processing.coherent_integration_s)
        gain_qualified = processing.effective_processing_gain_db is not None
        interference_qualified = (
            interference_dbm is not None or not processing.require_interference_input_for_qualification
        )
        processing_qualified = bool(gain_qualified and interference_qualified)
        used_gain_db = float(processing.effective_processing_gain_db if gain_qualified else ideal_gain_db)
        receiver_chain_db = (
            float(receiver_model.echo_antenna_gain_dbi)
            - float(receiver_model.feeder_loss_db)
            - float(processing.system_loss_db)
        )
        return_excess_delta_db = float(receiver_model.return_path_excess_loss_db - base_receiver.return_path_excess_loss_db)
        rcs_m2 = float(target.bistatic_rcs_m2)
        rcs_dbsm = 10.0 * math.log10(max(rcs_m2, 1.0e-30))

        # Resolve a clicked/typed target at its exact requested coordinate.  Scene
        # propagation values are interpolated from the four nearest cached samples;
        # bistatic geometry/Doppler are evaluated at the exact coordinate.
        selected_index: int | None = None
        selected_lat = selected_lon = selected_query_distance_m = None
        selected_indices = selected_distances = selected_weights = None
        if selected_latitude is not None and selected_longitude is not None:
            selected_indices, selected_distances, selected_weights = _scene_target_neighbors(
                product_id, float(selected_latitude), float(selected_longitude), k=4
            )
            selected_index = int(selected_indices[0])
            selected_lat = float(selected_latitude)
            selected_lon = float(selected_longitude)
            selected_query_distance_m = float(selected_distances[0])

        # Echo array is the scene basis repurposed in-place into current echo power.
        echo = np.asarray(product["echo_geometry_base_dbm"], dtype=np.float32)
        count = int(echo.size)
        echo_base_selected = (
            _interp_product_value(product, "echo_geometry_base_dbm", selected_indices, selected_weights)
            if selected_indices is not None and selected_weights is not None
            else None
        )
        echo += np.float32(receiver_chain_db + rcs_dbsm - return_excess_delta_db)

        # SNR margin versus effective N+I: post-SNR minus required SNR. No separate pre/post arrays
        # are retained; selected scalars and rendered layer are derived as needed.
        snr_margin = echo.copy()
        snr_margin += np.float32(
            -noise_power_dbm + used_gain_db
            - float(processing.processing_loss_db) - float(processing.required_snr_db)
        )
        thermal_ok = snr_margin >= 0.0
        thermal_count = int(np.count_nonzero(thermal_ok))
        selected_snr_margin = float(snr_margin[selected_index]) if selected_index is not None else None

        # Direct-reference constraint. Compute one margin array, use it to update
        # screening/power margin, then release unless it is the requested layer.
        direct_received = float(direct.get("received_power_dbm", math.nan))
        if math.isfinite(direct_received):
            direct_received += (
                float(receiver_model.direct_antenna_gain_dbi - base_receiver.direct_antenna_gain_dbi)
                - float(receiver_model.feeder_loss_db - base_receiver.feeder_loss_db)
                - float(receiver_model.direct_path_excess_loss_db - base_receiver.direct_path_excess_loss_db)
            )
        residual_direct = direct_received - float(processing.direct_path_cancellation_db)
        direct_current = dict(direct)
        direct_current.update({
            "received_power_dbm": _json_number(direct_received),
            "residual_after_cancellation_dbm": _json_number(residual_direct),
            "required_echo_threshold_dbm": _json_number(residual_direct + float(processing.required_echo_to_residual_direct_db)),
            "carrier_to_noise_interference_db": _json_number(direct_received - noise_power_dbm),
            "receiver_chain_reanalyzed": True,
        })
        direct_margin = echo.copy()
        direct_margin -= np.float32(residual_direct + float(processing.required_echo_to_residual_direct_db))
        if processing.require_direct_path_constraint:
            direct_ok = direct_margin >= 0.0
        else:
            direct_ok = np.ones(count, dtype=np.bool_)
        direct_count = int(np.count_nonzero(direct_ok))
        selected_direct_margin = float(direct_margin[selected_index]) if selected_index is not None else None

        # Power margin is the minimum enabled power-domain constraint. Reuse the
        # SNR margin buffer when that layer is not itself requested.
        power_margin = snr_margin.copy() if layer == "bistatic_margin" else snr_margin
        if processing.require_direct_path_constraint:
            np.minimum(power_margin, direct_margin, out=power_margin)

        if processing.max_receiver_dynamic_range_db is None:
            if processing.require_dynamic_range_constraint:
                dynamic_ok = np.zeros(count, dtype=np.bool_)
                power_margin.fill(-np.inf)
            else:
                dynamic_ok = np.ones(count, dtype=np.bool_)
            selected_dynamic_margin = math.nan
        else:
            # margin = capability - (direct - echo) = echo + capability - direct
            dynamic_margin = echo.copy()
            dynamic_margin += np.float32(float(processing.max_receiver_dynamic_range_db) - direct_received)
            if processing.require_dynamic_range_constraint:
                dynamic_ok = dynamic_margin >= 0.0
                np.minimum(power_margin, dynamic_margin, out=power_margin)
            else:
                dynamic_ok = np.ones(count, dtype=np.bool_)
            selected_dynamic_margin = float(dynamic_margin[selected_index]) if selected_index is not None else math.nan
            if layer != "direct_residual_margin":
                # This is not a renderable layer today, so release after constraints.
                del dynamic_margin
        dynamic_count = int(np.count_nonzero(dynamic_ok))
        selected_power_margin = float(power_margin[selected_index]) if selected_index is not None else None

        # Doppler from reusable local-ENU coefficients. Basis members are loaded and
        # released sequentially so three full coefficient arrays never coexist.
        heading = math.radians(float(motion.heading_deg_true))
        east = float(motion.speed_mps) * math.sin(heading)
        north = float(motion.speed_mps) * math.cos(heading)
        up = float(motion.climb_rate_mps)
        doppler = np.asarray(product["doppler_east_hz_per_mps"], dtype=np.float32)
        doppler *= np.float32(east)
        coeff = np.asarray(product["doppler_north_hz_per_mps"], dtype=np.float32)
        np.multiply(coeff, np.float32(north), out=coeff)
        doppler += coeff
        del coeff
        coeff = np.asarray(product["doppler_up_hz_per_mps"], dtype=np.float32)
        np.multiply(coeff, np.float32(up), out=coeff)
        doppler += coeff
        del coeff

        doppler_resolution_hz = 1.0 / float(processing.coherent_integration_s)
        doppler_threshold_hz = max(
            doppler_resolution_hz,
            float(processing.clutter_notch_hz),
            float(processing.minimum_detectable_doppler_hz),
        )
        doppler_resolved = np.abs(doppler) >= doppler_threshold_hz
        if processing.pulse_repetition_frequency_hz is None:
            ambiguous = np.zeros(count, dtype=np.bool_)
        else:
            ambiguous = np.abs(doppler) > float(processing.pulse_repetition_frequency_hz) / 2.0
        doppler_resolved_count = int(np.count_nonzero(doppler_resolved))

        static_clutter_delay_sep_us = None
        static_clutter_overlap = None
        static_clutter_path_count = None
        need_static_background = (
            layer in {"static_clutter_delay_separation", "static_clutter_overlap", "static_clutter_path_count"}
            or selected_index is not None
        )
        if need_static_background:
            path_delays = _static_background_arrays(summary)
            if path_delays.size:
                target_lats = np.asarray(product["latitude_deg"], dtype=np.float64)
                target_lons = np.asarray(product["longitude_deg"], dtype=np.float64)
                target_delays_2d = _horizontal_excess_delay_array(
                    target_lats, target_lons,
                    tx_lat=float(tx_meta.get("latitude")), tx_lon=float(tx_meta.get("longitude")),
                    rx_lat=float(receiver.get("latitude")), rx_lon=float(receiver.get("longitude")),
                )
                static_clutter_delay_sep_us, static_clutter_overlap, static_clutter_path_count = _static_background_grid_metrics(
                    target_excess_delay_s=target_delays_2d,
                    target_doppler_hz=doppler,
                    path_delays_s=path_delays,
                    delay_resolution_s=1.0 / max(float(bandwidth_hz), 1.0),
                    doppler_resolution_hz=doppler_resolution_hz,
                )
                del target_lats, target_lons, target_delays_2d
            del path_delays

        # Environmental validity is a scene constraint. Build screening in-place as
        # one bool vector rather than retaining separate derived detectability masks.
        environment_valid = np.asarray(product["return_environment_valid"], dtype=np.bool_)
        environment_count = int(np.count_nonzero(environment_valid))
        screening = thermal_ok.copy()
        screening &= doppler_resolved
        screening &= ~ambiguous
        screening &= direct_ok
        screening &= dynamic_ok
        screening &= environment_valid
        screening_count = int(np.count_nonzero(screening))
        qualified_count = screening_count if processing_qualified else 0

        # Sensitivity is loaded only when requested or when a selected-target table
        # needs it. Most layer changes therefore avoid another N-sized array.
        need_sensitivity = layer in {"doppler_sensitivity", "minimum_detectable_speed"} or selected_index is not None
        sensitivity = None
        min_speed = None
        if need_sensitivity:
            sensitivity = np.asarray(product["doppler_sensitivity_hz_per_mps"], dtype=np.float32)
            if layer == "minimum_detectable_speed" or selected_index is not None:
                min_speed = np.empty_like(sensitivity)
                np.divide(
                    np.float32(doppler_threshold_hz), sensitivity,
                    out=min_speed, where=sensitivity > np.float32(1.0e-9),
                )
                min_speed[sensitivity <= np.float32(1.0e-9)] = np.inf

        selected = None
        target_measurement_mask = None
        target_measurement_overlay = None
        if selected_indices is not None and selected_distances is not None and selected_weights is not None:
            selected = _exact_selected_target_state(
                product,
                summary=summary, tx_meta=tx_meta, receiver_model=receiver_model,
                target=target, motion=motion, processing=processing,
                frequency_hz=frequency_hz, bandwidth_hz=bandwidth_hz,
                noise_power_dbm=noise_power_dbm, used_gain_db=used_gain_db,
                processing_qualified=processing_qualified, direct_received_dbm=direct_received,
                residual_direct_dbm=residual_direct, receiver_chain_db=receiver_chain_db,
                return_excess_delta_db=return_excess_delta_db, rcs_m2=rcs_m2,
                selected_lat=float(selected_lat), selected_lon=float(selected_lon),
                indices=selected_indices, distances=selected_distances, weights=selected_weights,
                doppler_threshold_hz=doppler_threshold_hz, doppler_resolution_hz=doppler_resolution_hz,
            )
            if "excess_delay_s" in product.files:
                grid_delay = np.asarray(product["excess_delay_s"], dtype=np.float64)
                target_delay = float(selected["excess_delay_s"])
                measurement_delay_basis = "persisted_3d_bistatic_excess_delay"
            else:
                target_lats = np.asarray(product["latitude_deg"], dtype=np.float64)
                target_lons = np.asarray(product["longitude_deg"], dtype=np.float64)
                grid_delay = _horizontal_excess_delay_array(
                    target_lats, target_lons,
                    tx_lat=float(tx_meta.get("latitude", 0.0)), tx_lon=float(tx_meta.get("longitude", 0.0)),
                    rx_lat=float(receiver_model.latitude), rx_lon=float(receiver_model.longitude),
                )
                del target_lats, target_lons
                target_delay = float(selected["background_horizontal_excess_delay_s"])
                measurement_delay_basis = "horizontal_fallback_product_predates_3d_delay_array"
            target_doppler = float(selected["doppler_hz"])
            delay_half = 0.5 / max(float(bandwidth_hz), 1.0)
            doppler_half = 0.5 * float(doppler_resolution_hz)
            delay_match = np.abs(grid_delay - target_delay) <= delay_half
            doppler_match = np.abs(doppler - target_doppler) <= np.float32(doppler_half)
            target_measurement_mask = delay_match & doppler_match
            selected["measurement_cell"] = {
                "basis": "ideal_delay_doppler_resolution_cell_no_sidelobes",
                "delay_basis": measurement_delay_basis,
                "delay_resolution_s": 1.0 / max(float(bandwidth_hz), 1.0),
                "doppler_resolution_hz": float(doppler_resolution_hz),
                "delay_cell_point_count": int(np.count_nonzero(delay_match)),
                "doppler_cell_point_count": int(np.count_nonzero(doppler_match)),
                "joint_cell_point_count": int(np.count_nonzero(target_measurement_mask)),
            }
            target_measurement_overlay = _render_binary_target_overlay(
                product_id, target_measurement_mask, size=size,
                label="Locations sharing the selected target's ideal delay-Doppler cell",
            )
            del grid_delay, delay_match, doppler_match

        # Pick exactly one rendered full-grid result. Derived values use algebraic
        # forms that avoid exponential arrays unless the selected scalar needs m².
        if layer == "bistatic_echo":
            layer_values = echo
        elif layer == "bistatic_snr":
            layer_values = snr_margin.copy()
            layer_values += np.float32(processing.required_snr_db)
        elif layer == "bistatic_margin":
            layer_values = snr_margin
        elif layer == "rcs_margin":
            layer_values = power_margin
        elif layer == "minimum_detectable_rcs":
            layer_values = np.float32(rcs_dbsm) - power_margin
        elif layer == "bistatic_doppler":
            layer_values = doppler
        elif layer == "doppler_sensitivity":
            assert sensitivity is not None
            layer_values = sensitivity
        elif layer == "minimum_detectable_speed":
            assert min_speed is not None
            layer_values = min_speed
        elif layer == "required_cancellation":
            layer_values = np.float32(direct_received + float(processing.required_echo_to_residual_direct_db)) - echo
            np.maximum(layer_values, np.float32(0.0), out=layer_values)
        elif layer == "direct_residual_margin":
            layer_values = direct_margin
        elif layer == "static_clutter_delay_separation":
            layer_values = static_clutter_delay_sep_us if static_clutter_delay_sep_us is not None else np.full(count, np.inf, dtype=np.float32)
        elif layer == "static_clutter_overlap":
            layer_values = static_clutter_overlap.astype(np.float32) if static_clutter_overlap is not None else np.zeros(count, dtype=np.float32)
        elif layer == "static_clutter_path_count":
            layer_values = static_clutter_path_count.astype(np.float32) if static_clutter_path_count is not None else np.zeros(count, dtype=np.float32)
        elif layer == "target_measurement_cell":
            if target_measurement_mask is None:
                raise ValueError("target_measurement_cell requires a selected target location")
            layer_values = target_measurement_mask.astype(np.float32)
        elif layer in {"screening_detectable"}:
            layer_values = screening.astype(np.float32)
        else:  # qualified_detectable / compatibility alias
            layer_values = screening.astype(np.float32) if processing_qualified else np.zeros(count, dtype=np.float32)


        counts = {
            "grid_points": count,
            "snr_noise_interference_ok": thermal_count,
            "doppler_resolved": doppler_resolved_count,
            "doppler_ambiguous": int(np.count_nonzero(ambiguous)),
            "direct_residual_ok": direct_count,
            "dynamic_range_ok": dynamic_count,
            "environment_return_valid": environment_count,
            "screening_detectable": screening_count,
            "qualified_detectable": qualified_count,
            "static_clutter_overlap": int(np.count_nonzero(static_clutter_overlap)) if static_clutter_overlap is not None else 0,
            "target_measurement_cell": int(np.count_nonzero(target_measurement_mask)) if target_measurement_mask is not None else 0,
        }

        # Keep only the selected render array alive before rasterization. The
        # geometry cache has bounded cardinality and stores compact pixel mappings.
        keep = layer_values
        del echo, doppler, thermal_ok, doppler_resolved, ambiguous, direct_ok, dynamic_ok, environment_valid, screening
        if snr_margin is not keep and snr_margin is not power_margin:
            del snr_margin
        if power_margin is not keep:
            del power_margin
        if direct_margin is not keep:
            del direct_margin
        if sensitivity is not None and sensitivity is not keep:
            del sensitivity
        if min_speed is not None and min_speed is not keep:
            del min_speed
        if static_clutter_delay_sep_us is not None and static_clutter_delay_sep_us is not keep:
            del static_clutter_delay_sep_us
        if static_clutter_overlap is not None and static_clutter_overlap is not keep:
            del static_clutter_overlap
        if static_clutter_path_count is not None and static_clutter_path_count is not keep:
            del static_clutter_path_count

        heatmap = (
            target_measurement_overlay
            if layer == "target_measurement_cell" and target_measurement_overlay is not None
            else _render_layer(product_id, np.asarray(keep, dtype=np.float32), size=size, layer=layer)
        )
        return {
            "product_id": product_id,
            "scene_reused": True,
            "complexity": {
                "time": "O(N)",
                "world_rebuild": False,
                "scratch": "small constant number of N-sized compact arrays plus bounded raster scratch",
            },
            "requires_world_rebuild": ["target.heightMagl", "receiver latitude/longitude/site altitude/antenna height", "TX/RF/environment configuration"],
            "layer": heatmap,
            "target": target.model_dump(by_alias=True),
            "motion": motion.model_dump(by_alias=True),
            "processing": processing.model_dump(by_alias=True),
            "processing_assessment": {
                "bandwidth_hz": bandwidth_hz,
                "thermal_noise_power_dbm": thermal_noise_dbm,
                "interference_plus_clutter_power_dbm": interference_dbm,
                "effective_noise_plus_interference_dbm": noise_power_dbm,
                "ideal_time_bandwidth_gain_db": ideal_gain_db,
                "used_processing_gain_db": used_gain_db,
                "gain_source": "explicit_effective_gain" if gain_qualified else "ideal_time_bandwidth_screening",
                "effective_gain_qualified": gain_qualified,
                "interference_input_qualified": interference_qualified,
                "qualified": processing_qualified,
                "doppler_resolution_hz": doppler_resolution_hz,
                "doppler_detection_threshold_hz": doppler_threshold_hz,
                "mapped_static_background_used_for_qualified_detection": False,
            },
            "counts": counts,
            "selected_target": selected,
            "target_measurement_overlay": target_measurement_overlay,
            "transmitter": meta.get("transmitter"),
            "receiver": receiver_model.model_dump(by_alias=True),
            "scene_fidelity": (meta.get("summary") or {}).get("model_fidelity"),
            "static_background_channel": {k: v for k, v in (summary.get("static_background_channel") or {}).items() if k != "paths"},
            "direct_path": direct_current,
        }



def evaluate_channel_product_bundle(
    product_id: str,
    *,
    receiver_override: Mapping[str, Any] | None = None,
    target_override: Mapping[str, Any] | None = None,
    motion_override: Mapping[str, Any] | None = None,
    processing_override: Mapping[str, Any] | None = None,
    layers: list[str] | tuple[str, ...] | None = None,
    image_size: int = 1024,
) -> dict[str, Any]:
    """Render multiple ISAC capability layers from one physical-scene read.

    The fixed exported layer set is evaluated in one numerical pass. Large float
    buffers are rendered and repurposed sequentially so peak scratch is bounded
    by a small constant number of O(N) compact arrays rather than O(layer_count*N).
    """

    requested = list(dict.fromkeys(layers or [
        k for k in SUPPORTED_LAYERS if k not in {"bistatic_detectable", "target_measurement_cell"}
    ]))
    if not requested:
        raise ValueError("at least one ISAC layer is required")
    invalid = [name for name in requested if name not in SUPPORTED_LAYERS]
    if invalid:
        raise ValueError(f"unsupported ISAC layer(s): {', '.join(invalid)}")
    size = max(256, min(1536, int(image_size)))
    path = channel_product_path(product_id)
    if path is None or not path.is_file():
        raise FileNotFoundError(product_id)

    want = set(requested)
    rendered: dict[str, dict[str, Any]] = {}

    def emit(name: str, values: np.ndarray) -> None:
        if name in want:
            rendered[name] = _render_layer(product_id, np.asarray(values, dtype=np.float32), size=size, layer=name)

    with np.load(path, allow_pickle=False) as product:
        meta = _metadata(product)
        summary = meta.get("summary") or {}
        target = _merge_model(ChannelTarget, summary.get("target") or {}, target_override)
        motion = _merge_model(TargetMotion, summary.get("motion") or {}, motion_override)
        processing = _merge_model(ChannelProcessing, summary.get("processing") or {}, processing_override)
        scene_target = ChannelTarget.model_validate(summary.get("target") or {})
        if abs(float(target.height_m_agl) - float(scene_target.height_m_agl)) > 1.0e-9:
            raise ValueError("target height changes propagation geometry and requires a new RF plan")

        required = {
            "echo_geometry_base_dbm", "doppler_east_hz_per_mps",
            "doppler_north_hz_per_mps", "doppler_up_hz_per_mps",
            "doppler_sensitivity_hz_per_mps", "return_environment_valid",
        }
        missing = sorted(required.difference(product.files))
        if missing:
            raise ValueError(f"product predates reusable ISAC scene basis: missing {', '.join(missing)}")

        base_receiver = ChannelReceiver.model_validate(summary.get("receiver") or {})
        receiver_model = _merge_model(ChannelReceiver, summary.get("receiver") or {}, receiver_override)
        for field_name in ("latitude", "longitude", "altitude_m_amsl", "antenna_height_m_agl"):
            if abs(float(getattr(receiver_model, field_name)) - float(getattr(base_receiver, field_name))) > 1.0e-9:
                raise ValueError("receiver position/height changes propagation geometry and requires a new RF plan")

        direct = summary.get("direct_path") or {}
        rf_meta = meta.get("rf_config") or {}
        frequency_hz = float(summary.get("frequency_hz") or (float(rf_meta.get("freq_mhz", 0.0) or 0.0) * 1e6) or 1.0)
        bandwidth_hz = float(processing.processing_bandwidth_hz or summary.get("waveform_bandwidth_hz") or 1.0)
        thermal_noise_dbm = thermal_noise_power_dbm(bandwidth_hz, float(receiver_model.noise_figure_db))
        interference_dbm = processing.interference_plus_clutter_power_dbm
        if interference_dbm is None:
            noise_power_dbm = thermal_noise_dbm
        else:
            noise_power_dbm = 10.0 * math.log10(
                10.0 ** (thermal_noise_dbm / 10.0) + 10.0 ** (float(interference_dbm) / 10.0)
            )
        ideal_gain_db = coherent_processing_gain_db(bandwidth_hz, processing.coherent_integration_s)
        gain_qualified = processing.effective_processing_gain_db is not None
        interference_qualified = interference_dbm is not None or not processing.require_interference_input_for_qualification
        processing_qualified = bool(gain_qualified and interference_qualified)
        used_gain_db = float(processing.effective_processing_gain_db if gain_qualified else ideal_gain_db)
        receiver_chain_db = (
            float(receiver_model.echo_antenna_gain_dbi)
            - float(receiver_model.feeder_loss_db)
            - float(processing.system_loss_db)
        )
        return_excess_delta_db = float(receiver_model.return_path_excess_loss_db - base_receiver.return_path_excess_loss_db)
        rcs_m2 = float(target.bistatic_rcs_m2)
        rcs_dbsm = 10.0 * math.log10(max(rcs_m2, 1.0e-30))
        static_layer_names = {
            "static_clutter_delay_separation", "static_clutter_overlap", "static_clutter_path_count",
        }
        need_static_background_bundle = bool(want & static_layer_names)

        # 1) Echo + effective noise/interference detector. Emit thermal-only products before the margin
        # buffer is repurposed into the multi-constraint power margin.
        echo = np.asarray(product["echo_geometry_base_dbm"], dtype=np.float32)
        count = int(echo.size)
        echo += np.float32(receiver_chain_db + rcs_dbsm - return_excess_delta_db)
        emit("bistatic_echo", echo)

        snr_margin = echo.copy()
        snr_margin += np.float32(
            -noise_power_dbm + used_gain_db
            - float(processing.processing_loss_db) - float(processing.required_snr_db)
        )
        thermal_ok = snr_margin >= 0.0
        thermal_count = int(np.count_nonzero(thermal_ok))
        if "bistatic_snr" in want:
            post_snr = snr_margin.copy()
            post_snr += np.float32(processing.required_snr_db)
            emit("bistatic_snr", post_snr)
            del post_snr
        emit("bistatic_margin", snr_margin)

        # 2) Direct-reference + dynamic-range constraints. Render direct margin,
        # then reuse that same float buffer for dynamic-range math if required.
        direct_received = float(direct.get("received_power_dbm", math.nan))
        if math.isfinite(direct_received):
            direct_received += (
                float(receiver_model.direct_antenna_gain_dbi - base_receiver.direct_antenna_gain_dbi)
                - float(receiver_model.feeder_loss_db - base_receiver.feeder_loss_db)
                - float(receiver_model.direct_path_excess_loss_db - base_receiver.direct_path_excess_loss_db)
            )
        residual_direct = direct_received - float(processing.direct_path_cancellation_db)
        direct_current = dict(direct)
        direct_current.update({
            "received_power_dbm": _json_number(direct_received),
            "residual_after_cancellation_dbm": _json_number(residual_direct),
            "required_echo_threshold_dbm": _json_number(residual_direct + float(processing.required_echo_to_residual_direct_db)),
            "carrier_to_noise_interference_db": _json_number(direct_received - noise_power_dbm),
            "receiver_chain_reanalyzed": True,
        })
        direct_margin = echo.copy()
        direct_margin -= np.float32(residual_direct + float(processing.required_echo_to_residual_direct_db))
        emit("direct_residual_margin", direct_margin)
        direct_ok = direct_margin >= 0.0 if processing.require_direct_path_constraint else np.ones(count, dtype=np.bool_)
        direct_count = int(np.count_nonzero(direct_ok))

        if "required_cancellation" in want:
            required_cancellation = np.float32(direct_received + float(processing.required_echo_to_residual_direct_db)) - echo
            np.maximum(required_cancellation, np.float32(0.0), out=required_cancellation)
            emit("required_cancellation", required_cancellation)
            del required_cancellation

        # snr_margin is intentionally repurposed into the enabled power-constraint margin.
        power_margin = snr_margin
        if processing.require_direct_path_constraint:
            np.minimum(power_margin, direct_margin, out=power_margin)

        if processing.max_receiver_dynamic_range_db is None:
            if processing.require_dynamic_range_constraint:
                dynamic_ok = np.zeros(count, dtype=np.bool_)
                power_margin.fill(-np.inf)
            else:
                dynamic_ok = np.ones(count, dtype=np.bool_)
        else:
            direct_margin[:] = echo
            direct_margin += np.float32(float(processing.max_receiver_dynamic_range_db) - direct_received)
            if processing.require_dynamic_range_constraint:
                dynamic_ok = direct_margin >= 0.0
                np.minimum(power_margin, direct_margin, out=power_margin)
            else:
                dynamic_ok = np.ones(count, dtype=np.bool_)
        dynamic_count = int(np.count_nonzero(dynamic_ok))
        del direct_margin

        emit("rcs_margin", power_margin)
        if "minimum_detectable_rcs" in want:
            min_rcs_dbsm = np.float32(rcs_dbsm) - power_margin
            emit("minimum_detectable_rcs", min_rcs_dbsm)
            del min_rcs_dbsm

        # Echo is no longer needed after the power-domain layers have been emitted.
        del echo

        # 3) Kinematic basis. ENU basis arrays are loaded one at a time.
        heading = math.radians(float(motion.heading_deg_true))
        east = float(motion.speed_mps) * math.sin(heading)
        north = float(motion.speed_mps) * math.cos(heading)
        up = float(motion.climb_rate_mps)
        doppler = np.asarray(product["doppler_east_hz_per_mps"], dtype=np.float32)
        doppler *= np.float32(east)
        coeff = np.asarray(product["doppler_north_hz_per_mps"], dtype=np.float32)
        np.multiply(coeff, np.float32(north), out=coeff)
        doppler += coeff
        del coeff
        coeff = np.asarray(product["doppler_up_hz_per_mps"], dtype=np.float32)
        np.multiply(coeff, np.float32(up), out=coeff)
        doppler += coeff
        del coeff
        emit("bistatic_doppler", doppler)

        doppler_resolution_hz = 1.0 / float(processing.coherent_integration_s)
        doppler_threshold_hz = max(
            doppler_resolution_hz,
            float(processing.clutter_notch_hz),
            float(processing.minimum_detectable_doppler_hz),
        )
        doppler_resolved = np.abs(doppler) >= doppler_threshold_hz
        if processing.pulse_repetition_frequency_hz is None:
            ambiguous = np.zeros(count, dtype=np.bool_)
        else:
            ambiguous = np.abs(doppler) > float(processing.pulse_repetition_frequency_hz) / 2.0
        doppler_resolved_count = int(np.count_nonzero(doppler_resolved))
        ambiguous_count = int(np.count_nonzero(ambiguous))

        static_overlap_count = 0
        if need_static_background_bundle and "excess_delay_s" in product:
            path_delays = _static_background_arrays(summary)
            # Facade paths use horizontal 2-D excess-delay geometry. Recompute the
            # candidate-target delay on the same basis before comparing cells.
            tx_meta = meta.get("transmitter") or {}
            target_lats = np.asarray(product["latitude_deg"], dtype=np.float64)
            target_lons = np.asarray(product["longitude_deg"], dtype=np.float64)
            target_delays = _horizontal_excess_delay_array(
                target_lats, target_lons,
                tx_lat=float(tx_meta.get("latitude", 0.0)),
                tx_lon=float(tx_meta.get("longitude", 0.0)),
                rx_lat=float(receiver_model.latitude),
                rx_lon=float(receiver_model.longitude),
            )
            clutter_delay_sep_us, clutter_overlap, clutter_path_count = _static_background_grid_metrics(
                target_excess_delay_s=target_delays,
                target_doppler_hz=doppler,
                path_delays_s=path_delays,
                delay_resolution_s=1.0 / max(float(bandwidth_hz), 1.0),
                doppler_resolution_hz=doppler_resolution_hz,
            )
            del target_lats, target_lons, target_delays, path_delays
            static_overlap_count = int(np.count_nonzero(clutter_overlap))
            emit("static_clutter_delay_separation", clutter_delay_sep_us)
            emit("static_clutter_overlap", clutter_overlap.astype(np.float32))
            emit("static_clutter_path_count", clutter_path_count.astype(np.float32))
            del clutter_delay_sep_us, clutter_overlap, clutter_path_count
        elif need_static_background_bundle:
            empty_sep = np.full(count, np.inf, dtype=np.float32)
            empty_flag = np.zeros(count, dtype=np.float32)
            emit("static_clutter_delay_separation", empty_sep)
            emit("static_clutter_overlap", empty_flag)
            emit("static_clutter_path_count", np.zeros(count, dtype=np.float32))
            del empty_sep, empty_flag

        del doppler

        if "doppler_sensitivity" in want or "minimum_detectable_speed" in want:
            sensitivity = np.asarray(product["doppler_sensitivity_hz_per_mps"], dtype=np.float32)
            emit("doppler_sensitivity", sensitivity)
            if "minimum_detectable_speed" in want:
                valid_sensitivity = sensitivity > np.float32(1.0e-9)
                np.divide(
                    np.float32(doppler_threshold_hz), sensitivity,
                    out=sensitivity, where=valid_sensitivity,
                )
                sensitivity[~valid_sensitivity] = np.inf
                del valid_sensitivity
                emit("minimum_detectable_speed", sensitivity)
            del sensitivity

        # 4) Final constraint masks. Only booleans survive this stage.
        environment_valid = np.asarray(product["return_environment_valid"], dtype=np.bool_)
        environment_count = int(np.count_nonzero(environment_valid))
        screening = thermal_ok
        screening &= doppler_resolved
        screening &= ~ambiguous
        screening &= direct_ok
        screening &= dynamic_ok
        screening &= environment_valid
        screening_count = int(np.count_nonzero(screening))
        qualified_count = screening_count if processing_qualified else 0
        if "screening_detectable" in want:
            emit("screening_detectable", screening.astype(np.float32))
        if "qualified_detectable" in want or "bistatic_detectable" in want:
            if processing_qualified:
                qualified = screening.astype(np.float32)
            else:
                qualified = np.zeros(count, dtype=np.float32)
            emit("qualified_detectable", qualified)
            if "bistatic_detectable" in want:
                rendered["bistatic_detectable"] = dict(rendered["qualified_detectable"])
                rendered["bistatic_detectable"]["layer"] = "bistatic_detectable"
                rendered["bistatic_detectable"]["label"], rendered["bistatic_detectable"]["units"] = SUPPORTED_LAYERS["bistatic_detectable"]
            del qualified

        counts = {
            "grid_points": count,
            "snr_noise_interference_ok": thermal_count,
            "doppler_resolved": doppler_resolved_count,
            "doppler_ambiguous": ambiguous_count,
            "direct_residual_ok": direct_count,
            "dynamic_range_ok": dynamic_count,
            "environment_return_valid": environment_count,
            "screening_detectable": screening_count,
            "qualified_detectable": qualified_count,
            "static_clutter_overlap": static_overlap_count,
        }

        return {
            "product_id": product_id,
            "scene_reused": True,
            "complexity": {
                "time": "O(N) for fixed exported layer set; each raster necessarily consumes its N values",
                "world_rebuild": False,
                "scene_reads": 1,
                "scratch": "<=3 primary float32 N-vectors at power stage, then <=1 float32 N-vector at kinematic stage, plus bool masks and bounded raster scratch",
            },
            "requires_world_rebuild": ["target.heightMagl", "receiver latitude/longitude/site altitude/antenna height", "TX/RF/environment configuration"],
            "layers": rendered,
            "target": target.model_dump(by_alias=True),
            "motion": motion.model_dump(by_alias=True),
            "processing": processing.model_dump(by_alias=True),
            "processing_assessment": {
                "bandwidth_hz": bandwidth_hz,
                "thermal_noise_power_dbm": thermal_noise_dbm,
                "interference_plus_clutter_power_dbm": interference_dbm,
                "effective_noise_plus_interference_dbm": noise_power_dbm,
                "ideal_time_bandwidth_gain_db": ideal_gain_db,
                "used_processing_gain_db": used_gain_db,
                "gain_source": "explicit_effective_gain" if gain_qualified else "ideal_time_bandwidth_screening",
                "effective_gain_qualified": gain_qualified,
                "interference_input_qualified": interference_qualified,
                "qualified": processing_qualified,
                "doppler_resolution_hz": doppler_resolution_hz,
                "doppler_detection_threshold_hz": doppler_threshold_hz,
                "mapped_static_background_used_for_qualified_detection": False,
            },
            "counts": counts,
            "transmitter": meta.get("transmitter"),
            "receiver": receiver_model.model_dump(by_alias=True),
            "scene_fidelity": (meta.get("summary") or {}).get("model_fidelity"),
            "static_background_channel": {k: v for k, v in (summary.get("static_background_channel") or {}).items() if k != "paths"},
            "direct_path": direct_current,
        }
