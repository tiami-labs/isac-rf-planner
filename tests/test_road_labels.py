from isac_rf_planner.geo.road_labels import (
    build_road_label_candidates,
    classify_road_importance,
    fetch_road_labels,
    polyline_anchor_point,
    rank_road_labels,
)


def test_classify_road_importance_splits_major_and_minor() -> None:
    assert classify_road_importance("primary") == "major"
    assert classify_road_importance("residential") == "minor"


def test_polyline_anchor_point_returns_midpoint_along_geometry() -> None:
    anchor = polyline_anchor_point(
        [
            {"lat": 0.0, "lon": 0.0},
            {"lat": 0.0, "lon": 0.001},
            {"lat": 0.0, "lon": 0.002},
        ]
    )
    assert anchor is not None
    assert abs(anchor[0] - 0.0) < 1e-9
    assert abs(anchor[1] - 0.001) < 1e-5


def test_rank_road_labels_dedupes_by_name_and_prefers_higher_priority() -> None:
    candidates = build_road_label_candidates(
        [
            {
                "tags": {"name": "Main St", "highway": "residential"},
                "geometry": [{"lat": 0.0, "lon": 0.0}, {"lat": 0.0, "lon": 0.001}],
            },
            {
                "tags": {"name": "Main St", "highway": "primary"},
                "geometry": [{"lat": 0.0, "lon": 0.0}, {"lat": 0.0, "lon": 0.003}],
            },
            {
                "tags": {"name": "2nd Ave", "highway": "service"},
                "geometry": [{"lat": 0.0, "lon": 0.0}, {"lat": 0.001, "lon": 0.0}],
            },
        ]
    )

    ranked = rank_road_labels(candidates, major_limit=10, minor_limit=10)

    assert [item["name"] for item in ranked] == ["Main St", "2nd Ave"]
    assert ranked[0]["importance"] == "major"
    assert ranked[1]["importance"] == "minor"


def test_rank_road_labels_dedupes_same_name_globally() -> None:
    candidates = [
        {
            "name": "Market Street",
            "highway": "primary",
            "importance": "major",
            "anchor_lat": 0.0,
            "anchor_lon": 0.0,
            "length_m": 1200.0,
            "priority": 4,
            "anchor_index": 0,
        },
        {
            "name": "Market Street",
            "highway": "primary",
            "importance": "major",
            "anchor_lat": 0.0,
            "anchor_lon": 0.01,
            "length_m": 1200.0,
            "priority": 4,
            "anchor_index": 1,
        },
    ]
    ranked = rank_road_labels(candidates, major_limit=10, minor_limit=10)
    assert len(ranked) == 1


def test_fetch_road_labels_uses_cached_osm_data(monkeypatch) -> None:
    cached_elements = [
        {
            "tags": {"name": "Cached St", "highway": "residential"},
            "geometry": [{"lat": 0.0, "lon": 0.0}, {"lat": 0.0, "lon": 0.001}],
        }
    ]

    monkeypatch.setattr(
        "isac_rf_planner.geo.road_labels.load_cached_osm_data",
        lambda center, radius_m, data_type: cached_elements,
    )
    monkeypatch.setattr(
        "isac_rf_planner.geo.road_labels.save_cached_osm_data",
        lambda center, radius_m, data_type, data: None,
    )

    def fail_post(*args, **kwargs):
        raise AssertionError("requests.post should not be called on cache hit")

    monkeypatch.setattr("isac_rf_planner.geo.road_labels.requests.post", fail_post)

    labels = fetch_road_labels(0.0, 0.0, radius_m=2000.0, major_limit=10, minor_limit=10)
    assert len(labels) == 1
    assert labels[0]["name"] == "Cached St"
