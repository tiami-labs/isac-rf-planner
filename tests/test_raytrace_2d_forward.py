"""Unit tests for forward SBR (``raytrace_2d_forward``), aligned with the HTML demo rules."""

from __future__ import annotations

import math

from agentic_rf_planner.pipeline.schemas import LatLon
from agentic_rf_planner.rf.raytrace_2d_forward import (
    ForwardPropagationPlan,
    ForwardScene2D,
    launch_forward_beam,
    trace_forward_specular_ray,
)


def test_forward_los_hits_rx_disk_with_no_facets():
    origin = LatLon(lat=37.0, lon=-122.0)
    scene = ForwardScene2D(
        origin_ll=origin,
        facets=[],
        rx_e=150.0,
        rx_n=0.0,
        rx_capture_radius_m=10.0,
    )
    r = trace_forward_specular_ray(
        scene,
        angle_rad=0.0,
        max_bounces=4,
        max_path_m=500.0,
    )
    assert r.hit_rx
    assert r.bounce_count == 0
    assert r.status == "hit_rx"


def test_forward_beam_summary_counts_hits():
    origin = LatLon(lat=37.0, lon=-122.0)
    # Empty scene: wide beam still reaches Rx disk (no blocking facets in this cone).
    scene = ForwardScene2D(
        origin_ll=origin,
        facets=[],
        rx_e=80.0,
        rx_n=0.0,
        rx_capture_radius_m=18.0,
    )
    plan = ForwardPropagationPlan(
        tx_e=0.0,
        tx_n=0.0,
        bearing_center_rad=0.0,
        spread_rad=math.radians(8.0),
        num_rays=11,
        max_bounces=2,
        max_path_m=200.0,
    )
    summary, results = launch_forward_beam(scene, plan)
    assert summary.num_launched == 11
    assert all(r.hit_rx for r in results)
    assert summary.num_hit_rx == 11
    assert summary.min_bounces_among_hits == 0
