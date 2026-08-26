import math
from types import SimpleNamespace

import numpy as np
import pytest

from agentic_rf_planner.pipeline.schemas import LatLon, RFParams
from agentic_rf_planner.rf.background_scatter import (
    _select_candidate_wall_segments,
    build_static_background_channel,
)
from agentic_rf_planner.rf.isac_reanalysis import (
    _selected_static_background,
    _static_background_grid_metrics,
)
from agentic_rf_planner.rf.ray_tracing import (
    _wall_segment_link_relevance_m,
    enu_from_latlon,
    extract_wall_segments,
    latlon_from_enu,
)


def _node(origin: LatLon, east_m: float, north_m: float) -> dict:
    p = latlon_from_enu(origin, east_m, north_m)
    return {"lat": p.lat, "lon": p.lon}


def _rect(origin: LatLon, bid: int, x0: float, y0: float, x1: float, y1: float, material="concrete") -> dict:
    return {
        "id": bid,
        "material": material,
        "geometry": [
            _node(origin, x0, y0), _node(origin, x1, y0),
            _node(origin, x1, y1), _node(origin, x0, y1),
            _node(origin, x0, y0),
        ],
    }


def test_streamed_wall_candidate_selection_matches_legacy_stable_ranking():
    origin = LatLon(lat=38.0, lon=-121.0)
    rx = latlon_from_enu(origin, 2000.0, 250.0)
    buildings = [
        _rect(origin, i, 50.0 + i * 37.0, -80.0 + (i % 7) * 30.0,
              75.0 + i * 37.0, -55.0 + (i % 7) * 30.0,
              material="brick" if i % 2 else "concrete")
        for i in range(80)
    ]
    keep = 37
    selected, total = _select_candidate_wall_segments(buildings, origin, rx, keep)

    legacy = extract_wall_segments(buildings, origin)
    rx_e, rx_n = enu_from_latlon(origin, rx)
    expected = sorted(
        enumerate(legacy),
        key=lambda item: (
            _wall_segment_link_relevance_m(item[1], 0.0, 0.0, rx_e, rx_n),
            item[0],
        ),
    )[:keep]
    expected_walls = [seg for _, seg in expected]

    assert total == len(legacy)
    assert selected == expected_walls


def test_static_background_builds_mapped_material_specular_paths_without_calibration():
    origin = LatLon(lat=38.0, lon=-121.0)
    rx = latlon_from_enu(origin, 100.0, 0.0)
    building = _rect(origin, 123, 20.0, 20.0, 80.0, 25.0, material="concrete")

    class FakeOSM:
        def __init__(self):
            self._cached_buildings = [building]

        def get_buildings_along_ray(self, _a, _b):
            return []

    rf = RFParams(
        technology="5g_nr", waveform="5g_nr", freq_mhz=1000.0,
        tx_power_dbm=43.0, terrain_enabled=False, path_loss_model="fspl",
    )
    receiver = SimpleNamespace(
        latitude=rx.lat, longitude=rx.lon,
        echo_antenna_gain_dbi=10.0, feeder_loss_db=1.0,
    )
    channel = build_static_background_channel(
        map_provider=FakeOSM(), tx=origin, rf_params=rf, receiver=receiver,
        max_wall_candidates=16, max_paths=8,
    )

    assert channel is not None
    assert channel.candidate_buildings == 1
    assert channel.candidate_walls == 4
    assert channel.paths
    assert all(path.building_id == 123 for path in channel.paths)
    assert all(path.material == "concrete" for path in channel.paths)
    assert all(path.excess_delay_s > 0.0 for path in channel.paths)
    summary = channel.summary()
    assert summary["calibrated_absolute_clutter_power"] is False
    assert summary["doppler_model"] == "static_objects_zero_physical_doppler"
    assert "landcover_bistatic_sigma0_calibration" in summary["unsupported_without_additional_evidence"]
    assert "waveform_ambiguity_sidelobe_response" in summary["unsupported_without_additional_evidence"]


def test_static_background_delay_doppler_cell_metrics_count_same_cell_paths_without_power_model():
    path_delays = np.array([2.0e-6, 2.1e-6, 7.0e-6], dtype=np.float64)
    target_delays = np.array([2.0e-6, 2.0e-6, 6.8e-6], dtype=np.float64)
    target_doppler = np.array([0.1, 1.0, 0.0], dtype=np.float32)

    nearest_us, overlap, path_count = _static_background_grid_metrics(
        target_excess_delay_s=target_delays,
        target_doppler_hz=target_doppler,
        path_delays_s=path_delays,
        delay_resolution_s=1.0e-6,
        doppler_resolution_hz=1.0,
    )

    assert overlap.tolist() == [True, False, True]
    assert path_count.tolist() == [2, 0, 1]
    assert nearest_us.tolist() == pytest.approx([0.0, 0.0, 0.2], abs=2e-5)


def test_selected_static_background_attributes_same_cell_reflectors_geometry_only():
    summary = {
        "static_background_channel": {
            "status": "paths_available",
            "model": "osm_2d_single_bounce_specular_geometry",
            "candidate_walls": 100,
            "geometric_specular_candidates": 12,
            "evaluated_wall_candidates": 12,
            "candidate_search_complete": True,
            "accepted_paths": 3,
            "paths": [
                {"building_id": 11, "material": "brick", "excess_delay_s": 2.0e-6, "total_path_m": 1001.0},
                {"building_id": 12, "material": "concrete", "excess_delay_s": 2.1e-6, "total_path_m": 1001.1},
                {"building_id": 99, "material": "glass", "excess_delay_s": 8.0e-6, "total_path_m": 1008.0},
            ],
        }
    }
    selected = _selected_static_background(
        summary,
        target_excess_delay_s=2.0e-6,
        target_doppler_hz=0.1,
        delay_resolution_s=1.0e-6,
        doppler_resolution_hz=1.0,
    )
    assert selected["available"] is True
    assert selected["same_cell_contributor_count"] == 2
    assert selected["nearest_static_path"]["building_id"] == 11
    assert [row["building_id"] for row in selected["same_cell_contributors"]] == [11, 12]
    assert selected["delay_doppler_path_count"] == 3
    assert all(row["doppler_hz"] == 0.0 for row in selected["delay_doppler_paths"])
    assert "same_cell_modeled_clutter_power_dbm" not in selected


def test_zero_path_priority_pass_falls_back_beyond_candidate_budget(monkeypatch):
    import agentic_rf_planner.rf.background_scatter as bs
    origin = LatLon(lat=38.0, lon=-121.0)
    rx = latlon_from_enu(origin, 1000.0, 0.0)
    buildings = [
        _rect(origin, i, 430.0, 20.0 + i * 18.0, 570.0, 28.0 + i * 18.0, material="concrete")
        for i in range(12)
    ]
    class FakeOSM:
        def __init__(self): self._cached_buildings = buildings
        def get_buildings_along_ray(self, _a, _b): return []
    # Reject any reflector from the bounded first-pass IDs, but allow later
    # facade candidates. This proves an empty bounded pass is not treated as an
    # empty physical scene.
    def fake_visible(_osm, _p0, _p1, excluded_building_id):
        return int(excluded_building_id or 0) >= 4
    monkeypatch.setattr(bs, "_visible_except_reflector", fake_visible)
    rf = RFParams(technology="5g_nr", waveform="5g_nr", freq_mhz=1000.0,
                  tx_power_dbm=43.0, terrain_enabled=False, path_loss_model="fspl")
    receiver = SimpleNamespace(latitude=rx.lat, longitude=rx.lon,
                               echo_antenna_gain_dbi=0.0, feeder_loss_db=0.0)
    channel = build_static_background_channel(
        map_provider=FakeOSM(), tx=origin, rf_params=rf, receiver=receiver,
        max_wall_candidates=2, max_paths=2,
    )
    assert channel is not None
    assert channel.paths
    assert channel.rejection_counts["fallback_visibility_scan_used"] == 1
    assert any((p.building_id or 0) >= 4 for p in channel.paths)
