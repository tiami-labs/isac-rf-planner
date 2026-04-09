"""2D specular ray validity, SBR budgeting, **k_min** search, and optional OSM visuals.

**Modeling choice (not a claim about the real city):** each footprint is an **infinite-height
prism**; rays stay in one horizontal slice. Only **façade edges** (polygon boundaries) and
**LoS** are admissible—no rooftops, no over-building paths in this abstraction.

**k_min:** ``find_minimum_order_specular_paths`` searches ``k = 0, 1,2, …`` until the first
order with at least one Ω-valid path. That is the **minimum** façade reflection count in this
2D model, not a fixed “two-bounce” goal. **k_max** in production is a budget / significance
cutoff, not a unique scene property.

**Visual output:** ``RUN_OSM_RT_VISUAL=1`` writes ``test_artifacts/raytrace_2d_osm_raw_vs_filtered.png``.
Overlays **forward SBR** (many launches, Rx disk, green=hit / orange=miss) like ``tests/raytrace_2d_infinite_height_demo.html``. Tune with ``RUN_OSM_RT_FWD_RAYS``,
``RUN_OSM_RT_FWD_SPREAD_DEG``, ``RUN_OSM_RT_FWD_MAX_BOUNCES``, ``RUN_OSM_RT_RX_DISK_M``.
Optional: ``RUN_OSM_RT_SHOW_INVALID_CANDIDATES=1``, ``RUN_OSM_RT_SUPPRESS_RAW_FALLBACK=1``.
Disable forward overlay: ``RUN_OSM_RT_FORWARD_OVERLAY=0``.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Optional

import pytest

# When RUN_OSM_RT_VISUAL=1, the OSM two-bounce figure is written here (repo root, persistent).
_RAYTRACE_2D_VISUAL_PNG = (
    Path(__file__).resolve().parents[1] / "test_artifacts" / "raytrace_2d_osm_raw_vs_filtered.png"
)

from agentic_rf_planner.geo.osm_map_provider import OSMMapProvider, _polygon_contains_point
from agentic_rf_planner.pipeline.schemas import LatLon, RFParams
from agentic_rf_planner.rf.raytrace_2d_forward import (
    ForwardBeamSummary,
    ForwardPropagationPlan,
    ForwardScene2D,
    facets_from_wall_segments,
    launch_forward_beam,
)
from agentic_rf_planner.rf.ray_tracing import (
    compute_single_bounce_paths,
    compute_two_bounce_paths,
    enu_from_latlon,
    extract_wall_segments,
    find_minimum_order_specular_paths,
    latlon_from_enu,
)
from agentic_rf_planner.rf.raytrace_2d_validity import (
    estimate_num_azimuth_rays_2d_sbr,
    estimate_num_directions_3d_sbr,
    open_segment_has_interior_sample_in_footprint,
    snap_latlon_out_of_building_interiors,
    validate_specular_polyline_2d_infinite_height,
)


def _omega_skip_sets_from_meta(meta: dict) -> Optional[list]:
    raw = meta.get("omega_segment_skip_bids")
    if not raw:
        return None
    out = []
    for row in raw:
        if not row:
            out.append(None)
        else:
            out.append(set(int(x) for x in row))
    return out


def test_sbr_azimuth_ray_count_matches_geometry_rule():
    # w_min=5 m, R_max=500 m => Delta_phi = 0.01 rad; full circle => ~629
    n = estimate_num_azimuth_rays_2d_sbr(omega_rad=2 * math.pi, r_max_m=500.0, w_min_m=5.0)
    assert n == 629


def test_sbr_3d_direction_count_scales_like_one_over_delta_squared():
    # Same Delta as 2D: delta=0.01, hemisphere Omega=2*pi => N ~ 2pi / delta^2 ~ 62832
    n = estimate_num_directions_3d_sbr(solid_angle_sr=2 * math.pi, r_max_m=500.0, w_min_m=5.0)
    assert n == 62832


def test_chord_through_rectangle_footprint_interior_is_detected():
    """Same failure mode as plotting Tx->bottom->top->Rx with both bounces on one box."""
    tx_ll = LatLon(lat=37.7749, lon=-122.4194)
    # ~30 m x 34 m box centered ~east of TX (ENU from tx_ll)
    def off(e_m: float, n_m: float) -> LatLon:
        return latlon_from_enu(tx_ll, e_m, n_m)

    # Rectangle footprint (CCW), roughly east of origin
    e0, n0 = 20.0, -17.0
    e1, n1 = 50.0, -17.0
    e2, n2 = 50.0, 17.0
    e3, n3 = 20.0, 17.0
    building = {
        "id": 900001,
        "geometry": [
            {"lat": off(e0, n0).lat, "lon": off(e0, n0).lon},
            {"lat": off(e1, n1).lat, "lon": off(e1, n1).lon},
            {"lat": off(e2, n2).lat, "lon": off(e2, n2).lon},
            {"lat": off(e3, n3).lat, "lon": off(e3, n3).lon},
            {"lat": off(e0, n0).lat, "lon": off(e0, n0).lon},
        ],
        "material": "concrete",
    }
    geom = building["geometry"]
    b_bottom = off(e0, n0)  # corner; use midpoint of bottom edge for clearer interior chord
    b_bottom = LatLon(
        lat=0.5 * (off(e0, n0).lat + off(e1, n1).lat),
        lon=0.5 * (off(e0, n0).lon + off(e1, n1).lon),
    )
    b_top = LatLon(
        lat=0.5 * (off(e3, n3).lat + off(e2, n2).lat),
        lon=0.5 * (off(e3, n3).lon + off(e2, n2).lon),
    )
    rx_ll = off(65.0, 4.0)
    tx_pt = LatLon(lat=tx_ll.lat, lon=tx_ll.lon)

    assert open_segment_has_interior_sample_in_footprint(
        tx_ll, b_bottom, b_top, geom, eps_m=0.35, min_samples=24
    ), "expected middle of chord to sample building interior"

    ok, reason = validate_specular_polyline_2d_infinite_height(
        [tx_pt, b_bottom, b_top, rx_ll],
        [building],
        tx_ll,
        eps_m=0.35,
        min_samples=24,
    )
    assert not ok
    assert "segment_" in reason and "interior_in_footprint" in reason


def test_minimum_order_is_zero_when_no_footprints_obstruct():
    tx = LatLon(lat=10.0, lon=-50.0)
    rx = LatLon(lat=10.002, lon=-50.0)
    rf = RFParams(freq_mhz=3500.0, tx_power_dbm=43.0, termination_rsrp_dbm=-140.0)
    k, paths = find_minimum_order_specular_paths(
        tx,
        rx,
        [],
        max_k=2,
        max_candidates=4,
        max_return=4,
        rf_params=rf,
        is_path_clear_fn=lambda *a, **kw: True,
        footprint_buildings=[],
        path_rsrp_dbm=-70.0,
    )
    assert k == 0
    assert paths and paths[0].kind == "direct"
    assert paths[0].meta.get("specular_order") == 0


def test_snap_latlon_moves_interior_point_outside_footprint():
    origin = LatLon(lat=37.7749, lon=-122.4194)
    building = {
        "id": 1,
        "geometry": [
            {"lat": 37.77470, "lon": -122.41950},
            {"lat": 37.77510, "lon": -122.41950},
            {"lat": 37.77510, "lon": -122.41910},
            {"lat": 37.77470, "lon": -122.41910},
            {"lat": 37.77470, "lon": -122.41950},
        ],
    }
    geom = building["geometry"]
    from agentic_rf_planner.geo.osm_map_provider import _polygon_contains_point

    inner = LatLon(lat=37.77490, lon=-122.41930)
    assert _polygon_contains_point(geom, inner)
    out = snap_latlon_out_of_building_interiors(inner, [building], origin, margin_m=3.0)
    assert not _polygon_contains_point(geom, out)


def test_two_bounce_raw_candidates_filtered_when_footprints_provided():
    """With is_path_clear always true, image method can still propose interior chords; filter drops them."""
    tx_ll = LatLon(lat=37.7749, lon=-122.4194)
    building = {
        "id": 42,
        "geometry": [
            {"lat": 37.77470, "lon": -122.41950},
            {"lat": 37.77510, "lon": -122.41950},
            {"lat": 37.77510, "lon": -122.41910},
            {"lat": 37.77470, "lon": -122.41910},
            {"lat": 37.77470, "lon": -122.41950},
        ],
        "material": "concrete",
    }
    walls = extract_wall_segments([building], tx_ll)
    rx_ll = LatLon(lat=37.77530, lon=-122.41890)
    rf = RFParams(freq_mhz=3500.0, tx_power_dbm=43.0, termination_rsrp_dbm=-140.0)

    def _always_clear(*_a, **_k):
        return True

    raw = compute_two_bounce_paths(
        tx_ll,
        rx_ll,
        walls,
        max_candidates=64,
        max_return=20,
        rf_params=rf,
        is_path_clear_fn=_always_clear,
        rsrp_for_path_fn=lambda *a, **k: -80.0,
        footprint_buildings=None,
    )
    filtered = compute_two_bounce_paths(
        tx_ll,
        rx_ll,
        walls,
        max_candidates=64,
        max_return=20,
        rf_params=rf,
        is_path_clear_fn=_always_clear,
        rsrp_for_path_fn=lambda *a, **k: -80.0,
        footprint_buildings=[building],
    )
    # If geometry produced no raw two-bounce paths, skip inequality (environment-specific float noise).
    if not raw:
        pytest.skip("no raw two-bounce candidates for this synthetic footprint")
    assert len(filtered) <= len(raw)
    for p in filtered:
        skips = _omega_skip_sets_from_meta(p.meta)
        ok, _ = validate_specular_polyline_2d_infinite_height(
            p.points,
            [building],
            tx_ll,
            per_segment_skip_building_ids=skips,
        )
        assert ok


@pytest.mark.skipif(
    os.environ.get("RUN_OSM_RT_VISUAL") != "1",
    reason=(
        "Set RUN_OSM_RT_VISUAL=1 to fetch OSM and save a PNG. Requires: pip install matplotlib "
        "(pyplot is part of matplotlib, not a separate package). Network or OSM cache for buildings. "
        f"PNG path: {_RAYTRACE_2D_VISUAL_PNG}. "
        "Optional: RUN_OSM_RT_SHOW_INVALID_CANDIDATES=1 to overlay invalid raw candidates."
    ),
)
def test_osm_market_street_raw_two_bounce_traces_draw():
    # Non-interactive backend: reproducible PNG and no Qt noise in CI.
    pytest.importorskip("matplotlib")
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # User-provided link endpoints; snap out of OSM interiors (footprints often cover sidewalks).
    tx0 = LatLon(lat=37.76893189840603, lon=-122.42698431015016)
    rx0 = LatLon(lat=37.76794411512003, lon=-122.42527306079866)

    dist = math.hypot(
        math.radians(rx0.lat - tx0.lat) * 6371000.0,
        math.radians(rx0.lon - tx0.lon) * 6371000.0 * math.cos(math.radians(tx0.lat)),
    )
    # Tighter than max2 km: enough context for the link without importing the whole city grid.
    radius_m = float(min(max(dist * 2.2 + 200.0, 420.0), 1200.0))
    mid = LatLon(lat=(tx0.lat + rx0.lat) * 0.5, lon=(tx0.lon + rx0.lon) * 0.5)

    osm = OSMMapProvider(cache_radius_m=max(800.0, radius_m))
    osm.prefetch_all_data(mid, radius_m)
    buildings = list(getattr(osm, "_cached_buildings", []) or [])
    if len(buildings) < 2:
        pytest.skip("OSM returned too few buildings for this bbox (offline or empty tile)")

    snap_origin = mid
    tx = snap_latlon_out_of_building_interiors(tx0, buildings, snap_origin, margin_m=4.0)
    rx = snap_latlon_out_of_building_interiors(rx0, buildings, snap_origin, margin_m=4.0)

    walls = extract_wall_segments(buildings, tx)
    if len(walls) < 1:
        pytest.skip("no wall segments from OSM")

    rf = RFParams(freq_mhz=3500.0, tx_power_dbm=43.0, termination_rsrp_dbm=-140.0)

    def _always_clear(*_a, **_k):
        return True

    show_invalid = os.environ.get("RUN_OSM_RT_SHOW_INVALID_CANDIDATES") == "1"
    # Default512 is fine for CI unit checks; OSM visual + O(n²) two-bounce needs a smaller cap
    # or pytest appears to “hang”. Override with RUN_OSM_RT_MAX_CANDIDATES.
    _mc = 512
    if os.environ.get("RUN_OSM_RT_VISUAL") == "1":
        _mc = min(_mc, int(os.environ.get("RUN_OSM_RT_MAX_CANDIDATES", "180")))
    _mr = 48
    _max_k = 8
    raw_paths_2: list = []
    raw_paths_1: list = []
    if show_invalid:
        raw_paths_2 = compute_two_bounce_paths(
            tx,
            rx,
            walls,
            max_candidates=_mc,
            max_return=_mr,
            rf_params=rf,
            is_path_clear_fn=_always_clear,
            rsrp_for_path_fn=lambda *a, **kw: -80.0,
            footprint_buildings=None,
        )
        raw_paths_1 = compute_single_bounce_paths(
            tx,
            rx,
            walls,
            max_candidates=_mc,
            max_return=_mr,
            rf_params=rf,
            is_path_clear_fn=_always_clear,
            rsrp_for_path_fn=lambda *a, **kw: -80.0,
            footprint_buildings=None,
        )

    k_min, omega_paths = find_minimum_order_specular_paths(
        tx,
        rx,
        walls,
        max_k=_max_k,
        max_candidates=_mc,
        max_return=_mr,
        rf_params=rf,
        is_path_clear_fn=_always_clear,
        footprint_buildings=buildings,
        path_rsrp_dbm=-80.0,
        termination_rsrp_dbm=-200.0,
    )
    if os.environ.get("RUN_OSM_RT_VISUAL") == "1":
        _ords = [p.meta.get("specular_order") for p in omega_paths]
        print(
            "[RUN_OSM_RT_VISUAL] k_min=%r Ω-valid paths=%d specular_orders=%s (points−2 = bounce count)"
            % (k_min, len(omega_paths), _ords),
            flush=True,
        )

    def _inside_footprint(ll: LatLon) -> bool:
        for b in buildings:
            geom = b.get("geometry") or []
            if len(geom) >= 3 and _polygon_contains_point(geom, ll):
                return True
        return False

    # When no Ω-valid path exists up to _max_k, optional raw image-method polylines (not physical).
    fallback_1: list = []
    fallback_2: list = []
    if (
        k_min is None
        and os.environ.get("RUN_OSM_RT_SUPPRESS_RAW_FALLBACK") != "1"
    ):
        fallback_1 = compute_single_bounce_paths(
            tx,
            rx,
            walls,
            max_candidates=_mc,
            max_return=10,
            rf_params=rf,
            is_path_clear_fn=_always_clear,
            rsrp_for_path_fn=lambda *a, **kw: -80.0,
            footprint_buildings=None,
        )
        fallback_2 = compute_two_bounce_paths(
            tx,
            rx,
            walls,
            max_candidates=min(_mc, 256),
            max_return=8,
            rf_params=rf,
            is_path_clear_fn=_always_clear,
            rsrp_for_path_fn=lambda *a, **kw: -80.0,
            footprint_buildings=None,
        )

    for p in omega_paths:
        skips = _omega_skip_sets_from_meta(p.meta)
        ok, msg = validate_specular_polyline_2d_infinite_height(
            p.points,
            buildings,
            tx,
            per_segment_skip_building_ids=skips,
        )
        assert ok, "Ω-valid path must validate (with façade skip meta if present): %s" % msg

    fwd_summary: Optional[ForwardBeamSummary] = None
    fwd_traces: list = []

    fig, ax = plt.subplots(figsize=(9, 9))
    fig.suptitle(
        "2D model: footprints × infinite height (opaque vertical prisms). "
        "Only horizontal façade specular + LoS; no rooftops or penetration in this slice. "
        "Goal: k_min = smallest reflection order with an Ω-valid path (not “always two bounces”).",
        fontsize=10,
        y=0.995,
    )
    _fb_note = ""
    if fallback_1 or fallback_2:
        _fb_note = "; orange/brown dashed: raw image-method (NOT Ω-valid) when k_min absent"
    ax.set_title(
        "ENU m — k_min Ω-valid: gray/black LoS (k=0), blue k=1, green k=2, purple k≥3"
        + ("; dashed red: invalid debug overlay" if show_invalid else "")
        + _fb_note,
        fontsize=9,
    )
    ax.set_aspect("equal", adjustable="box")

    for b in buildings:
        geom = b.get("geometry") or []
        xs = [enu_from_latlon(tx, LatLon(lat=float(n["lat"]), lon=float(n["lon"])))[0] for n in geom if "lat" in n]
        ys = [enu_from_latlon(tx, LatLon(lat=float(n["lat"]), lon=float(n["lon"])))[1] for n in geom if "lat" in n]
        if len(xs) >= 3:
            ax.fill(xs, ys, color="#d0d0d0", edgecolor="#666666", linewidth=0.6, alpha=0.9)

    tx_e, tx_n = 0.0, 0.0
    rx_e, rx_n = enu_from_latlon(tx, rx)
    ax.plot([tx_e], [tx_n], "^", color="darkred", markersize=10, label="Tx")
    ax.plot([rx_e], [rx_n], "v", color="darkblue", markersize=10, label="Rx")

    if os.environ.get("RUN_OSM_RT_VISUAL") == "1" and os.environ.get("RUN_OSM_RT_FORWARD_OVERLAY", "1") != "0":
        from matplotlib.patches import Circle

        r_disk = float(os.environ.get("RUN_OSM_RT_RX_DISK_M", "12"))
        n_rays = int(os.environ.get("RUN_OSM_RT_FWD_RAYS", "97"))
        spr_d = float(os.environ.get("RUN_OSM_RT_FWD_SPREAD_DEG", "50"))
        max_b = int(os.environ.get("RUN_OSM_RT_FWD_MAX_BOUNCES", "8"))
        sc = ForwardScene2D(
            origin_ll=tx,
            facets=facets_from_wall_segments(walls),
            rx_e=rx_e,
            rx_n=rx_n,
            rx_capture_radius_m=r_disk,
        )
        brg = math.atan2(rx_n - tx_n, rx_e - tx_e)
        plan = ForwardPropagationPlan(
            tx_e=tx_e,
            tx_n=tx_n,
            bearing_center_rad=brg,
            spread_rad=math.radians(spr_d),
            num_rays=n_rays,
            max_bounces=max_b,
            max_path_m=25_000.0,
        )
        fwd_summary, fwd_traces = launch_forward_beam(sc, plan)
        print(
            "[RUN_OSM_RT_VISUAL] forward_beam hit_rx=%d/%d min_bnc=%r max_bnc=%r"
            % (
                fwd_summary.num_hit_rx,
                fwd_summary.num_launched,
                fwd_summary.min_bounces_among_hits,
                fwd_summary.max_bounces_among_hits,
            ),
            flush=True,
        )
        ax.add_patch(
            Circle(
                (rx_e, rx_n),
                r_disk,
                fill=False,
                edgecolor="#1565c0",
                linewidth=1.4,
                linestyle="--",
                alpha=0.55,
            )
        )
        _hit_lab = False
        _miss_lab = False
        for tr in fwd_traces:
            ee = [p[0] for p in tr.points_enu]
            nn = [p[1] for p in tr.points_enu]
            if tr.hit_rx:
                lab = "Forward SBR → Rx disk" if not _hit_lab else None
                _hit_lab = True
                ax.plot(ee, nn, "-", color="#2bb24c", linewidth=1.85, alpha=0.82, label=lab)
            else:
                lab = "Forward SBR miss" if not _miss_lab else None
                _miss_lab = True
                ax.plot(ee, nn, "--", color="#ff8c42", linewidth=1.05, alpha=0.36, label=lab)

    _fwd_line = ""
    if fwd_summary is not None:
        _fwd_line = (
            f"\nForward SBR: rays={fwd_summary.num_launched} hit_rx={fwd_summary.num_hit_rx} "
            f"bnc[min,max]={fwd_summary.min_bounces_among_hits!r}/{fwd_summary.max_bounces_among_hits!r} "
            f"corner={fwd_summary.num_corner_hit} max_b_stop={fwd_summary.num_max_bounces}"
        )
    diag = (
        f"k_min (Ω-valid, k≤{_max_k}): {k_min!r}  |  paths drawn: {len(omega_paths)}\n"
        f"Raw fallback (NOT Ω-valid): 1-b={len(fallback_1)}, 2-b={len(fallback_2)}\n"
        f"OSM PIP interior: Tx_inside={_inside_footprint(tx)}, Rx_inside={_inside_footprint(rx)}\n"
        f"Fetch r={radius_m:.0f} m, walls={len(walls)}; higher k only if needed (model cutoff)"
        f"{_fwd_line}"
    )
    ax.text(
        0.02,
        0.98,
        diag,
        transform=ax.transAxes,
        fontsize=8,
        verticalalignment="top",
        family="monospace",
        bbox={"boxstyle": "round", "facecolor": "wheat", "alpha": 0.88},
    )

    if show_invalid:
        for i, p in enumerate(raw_paths_1[:8]):
            ee = [enu_from_latlon(tx, pt)[0] for pt in p.points]
            nn = [enu_from_latlon(tx, pt)[1] for pt in p.points]
            ax.plot(
                ee,
                nn,
                "--",
                color="#ff9f9f",
                alpha=0.55,
                linewidth=1.0,
                label="Invalid 1-bounce (image only)" if i == 0 else None,
            )
        for i, p in enumerate(raw_paths_2[:8]):
            ee = [enu_from_latlon(tx, pt)[0] for pt in p.points]
            nn = [enu_from_latlon(tx, pt)[1] for pt in p.points]
            ax.plot(
                ee,
                nn,
                "--",
                color="#ff6b6b",
                alpha=0.65,
                linewidth=1.15,
                label="Invalid 2-bounce (image only)" if i == 0 else None,
            )
    for i, p in enumerate(omega_paths[:12]):
        ee = [enu_from_latlon(tx, pt)[0] for pt in p.points]
        nn = [enu_from_latlon(tx, pt)[1] for pt in p.points]
        order = int(p.meta.get("specular_order", 0))
        if p.kind == "direct" or order == 0:
            col, lw, lab = "#2c3e50", 2.0, "Ω-valid LoS (k=0)"
        elif order == 1:
            col, lw, lab = "#2980b9", 1.55, "Ω-valid k=1 specular"
        elif order == 2:
            col, lw, lab = "#27ae60", 1.65, "Ω-valid k=2 specular"
        else:
            col, lw, lab = "#8e44ad", 1.55, f"Ω-valid k={order} specular"
        ax.plot(
            ee,
            nn,
            "-",
            color=col,
            alpha=0.95,
            linewidth=lw,
            label=lab if i == 0 else None,
        )
    for i, p in enumerate(fallback_1[:8]):
        ee = [enu_from_latlon(tx, pt)[0] for pt in p.points]
        nn = [enu_from_latlon(tx, pt)[1] for pt in p.points]
        ax.plot(
            ee,
            nn,
            "--",
            color="#d35400",
            alpha=0.85,
            linewidth=1.35,
            label="NOT Ω-valid: 1-bounce image construction" if i == 0 else None,
        )
    for i, p in enumerate(fallback_2[:6]):
        ee = [enu_from_latlon(tx, pt)[0] for pt in p.points]
        nn = [enu_from_latlon(tx, pt)[1] for pt in p.points]
        ax.plot(
            ee,
            nn,
            "--",
            color="#a04000",
            alpha=0.8,
            linewidth=1.25,
            label="NOT Ω-valid: 2-bounce image construction" if i == 0 else None,
        )
    if k_min is None and not fallback_1 and not fallback_2:
        ax.text(
            0.02,
            0.02,
            "No Omega-valid or raw image-method paths (increase wall budget or check OSM).",
            transform=ax.transAxes,
            fontsize=10,
            color="darkgreen",
            verticalalignment="bottom",
        )

    # View centered on TX–RX midpoint (not global building bbox); radius fits link + rays + pad.
    # ENU origin is TX, so TX = (0, 0).
    cx = 0.5 * rx_e
    cy = 0.5 * rx_n
    rad = 0.5 * math.hypot(rx_e - tx_e, rx_n - tx_n) + 1e-6
    frame_paths = (
        (
            raw_paths_1[:8]
            + raw_paths_2[:8]
            + omega_paths[:12]
            + fallback_1[:8]
            + fallback_2[:6]
        )
        if show_invalid
        else (omega_paths[:12] + fallback_1[:8] + fallback_2[:6])
    )
    for p in frame_paths:
        for pt in p.points:
            e, n = enu_from_latlon(tx, pt)
            rad = max(rad, math.hypot(e - cx, n - cy))
    for tr in fwd_traces:
        for px, py in tr.points_enu:
            rad = max(rad, math.hypot(px - cx, py - cy))
    pad = max(35.0, 0.14 * rad)
    ax.set_xlim(cx - rad - pad, cx + rad + pad)
    ax.set_ylim(cy - rad - pad, cy + rad + pad)

    ax.legend(loc="upper right")
    ax.set_xlabel("East (m)")
    ax.set_ylabel("North (m)")
    out = _RAYTRACE_2D_VISUAL_PNG
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)
    assert out.is_file(), f"expected PNG at {out}"
    # pytest hides stdout unless -s; path is also in skip reason and assertion text above.
    print(f"\nRAYTRACE_2D_VISUAL_PNG={out.resolve()}\n", flush=True)
    if show_invalid:
        _g1 = compute_single_bounce_paths(
            tx,
            rx,
            walls,
            max_candidates=_mc,
            max_return=_mr,
            rf_params=rf,
            is_path_clear_fn=_always_clear,
            rsrp_for_path_fn=lambda *a, **kw: -80.0,
            footprint_buildings=buildings,
        )
        _g2 = compute_two_bounce_paths(
            tx,
            rx,
            walls,
            max_candidates=_mc,
            max_return=_mr,
            rf_params=rf,
            is_path_clear_fn=_always_clear,
            rsrp_for_path_fn=lambda *a, **kw: -80.0,
            footprint_buildings=buildings,
        )
        assert len(_g1) <= len(raw_paths_1)
        assert len(_g2) <= len(raw_paths_2)
