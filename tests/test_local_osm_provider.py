"""Local OSM import/provider and DVT orchestration tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from isac_rf_planner.agents import rf_planning_agent
from isac_rf_planner.data.prepare_region import build_database
from isac_rf_planner.geo.local_osm_provider import LocalOSMProvider
from isac_rf_planner.geo.physical_spanning import StubMapProvider
from isac_rf_planner.pipeline.schemas import LatLon, RFParams
from isac_rf_planner.rf.dvt import DVTTransmitter


def _write_fixture_geojson(path: Path) -> None:
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": "building/1",
                "properties": {
                    "osm_id": "1",
                    "osm_type": "way",
                    "building": "yes",
                    "building:material": "brick",
                    "height": "15",
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [-120.99980, 37.99990],
                        [-120.99960, 37.99990],
                        [-120.99960, 38.00010],
                        [-120.99980, 38.00010],
                        [-120.99980, 37.99990],
                    ]],
                },
            },
            {
                "type": "Feature",
                "id": "forest/2",
                "properties": {
                    "osm_id": "2",
                    "osm_type": "relation",
                    "landuse": "forest",
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [-120.99945, 37.99990],
                        [-120.99920, 37.99990],
                        [-120.99920, 38.00010],
                        [-120.99945, 38.00010],
                        [-120.99945, 37.99990],
                    ]],
                },
            },
            {
                "type": "Feature",
                "id": "far/3",
                "properties": {"osm_id": "3", "building": "warehouse"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [-121.2000, 38.2000],
                        [-121.1990, 38.2000],
                        [-121.1990, 38.2010],
                        [-121.2000, 38.2010],
                        [-121.2000, 38.2000],
                    ]],
                },
            },
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _dvt_rf_params() -> RFParams:
    dvt = DVTTransmitter.model_validate(
        {
            "fc": 587_000_000.0,
            "fs": 10_000_000.0,
            "bandwidth": 6_000_000.0,
            "waveform": "atsc1",
            "tx": {
                "latitude": 38.0,
                "longitude": -121.0,
                "altitude": 5.0,
                "antennaHeight": 100.0,
                "name": "TEST",
            },
            "antenna": {"patternType": "omnidirectional"},
            "power": {"erpKw": 1.0, "polarization": "H"},
            "station": {"callSign": "TEST"},
        }
    )
    return RFParams(
        technology="dvt",
        dvt=dvt,
        freq_mhz=1.0,
        tx_power_dbm=0.0,
        max_range_m=20.0,
        step_m=20.0,
        dtheta_deg=180.0,
        terrain_enabled=False,
        compact_output=False,
    )


def test_prepare_and_query_local_osm_database(tmp_path):
    source = tmp_path / "fixture.geojson"
    database = tmp_path / "rf_geometry.sqlite"
    _write_fixture_geojson(source)

    counts = build_database([source], database)
    assert counts == {"building": 2, "landuse": 1, "skipped": 0}

    provider = LocalOSMProvider(database)
    center = LatLon(lat=38.0, lon=-121.0)
    provider.prefetch_all_data(center, 1000.0)

    assert len(provider._cached_buildings) == 1
    assert len(provider._cached_landuse) == 1
    assert provider._cached_buildings[0]["material"] == "brick"
    assert provider._cached_buildings[0]["height_m"] == pytest.approx(15.0)

    building_end = LatLon(lat=38.0, lon=-120.99970)
    forest_end = LatLon(lat=38.0, lon=-120.99930)
    assert provider.count_buildings_between(center, building_end) == 1
    assert provider.is_forest_between(center, forest_end) is True


def test_dvt_provider_resolves_local_database_without_overpass(tmp_path, monkeypatch):
    source = tmp_path / "fixture.geojson"
    database = tmp_path / "rf_geometry.sqlite"
    _write_fixture_geojson(source)
    build_database([source], database)
    monkeypatch.setenv("DVT_OSM_DATABASE", str(database))

    provider = rf_planning_agent._create_dvt_map_provider(_dvt_rf_params(), 100.0)
    assert isinstance(provider, LocalOSMProvider)
    assert provider.slice_height_m is None


def test_dvt_run_skips_street_snapping_and_streetview(monkeypatch):
    def fail_snap(*args, **kwargs):
        raise AssertionError("DVT must not call street snapping")

    class FailingStreetView:
        radius_m = 50.0

        def check_availability(self, *args, **kwargs):
            raise AssertionError("DVT must not call Street View")

    monkeypatch.setattr(rf_planning_agent, "snap_to_street", fail_snap)
    result = rf_planning_agent.run_rf_planning_for_point(
        lat=38.0,
        lon=-121.0,
        rf_params=_dvt_rf_params(),
        map_provider=StubMapProvider(),
        streetview_provider=FailingStreetView(),
    )

    assert result["snapped_tx"] == {"lat": 38.0, "lon": -121.0}
    assert result["snap_distance_m"] == pytest.approx(0.0)
    assert result["streetview_available"] is False
    assert result["vlm_used"] is False


def _network_fixture(center: LatLon):
    return (
        [
            {
                "id": 101,
                "osm_id": 101,
                "osm_type": "way",
                "tags": {"building": "yes", "height": "12"},
                "geometry": [
                    {"lat": center.lat - 0.0001, "lon": center.lon - 0.0001},
                    {"lat": center.lat - 0.0001, "lon": center.lon + 0.0001},
                    {"lat": center.lat + 0.0001, "lon": center.lon + 0.0001},
                    {"lat": center.lat + 0.0001, "lon": center.lon - 0.0001},
                    {"lat": center.lat - 0.0001, "lon": center.lon - 0.0001},
                ],
                "material": "unknown",
                "height_m": 12.0,
            }
        ],
        [
            {
                "id": 202,
                "osm_id": 202,
                "osm_type": "way",
                "tags": {"landuse": "forest"},
                "geometry": [
                    {"lat": center.lat - 0.0002, "lon": center.lon + 0.0002},
                    {"lat": center.lat - 0.0002, "lon": center.lon + 0.0004},
                    {"lat": center.lat + 0.0002, "lon": center.lon + 0.0004},
                    {"lat": center.lat + 0.0002, "lon": center.lon + 0.0002},
                    {"lat": center.lat - 0.0002, "lon": center.lon + 0.0002},
                ],
            }
        ],
    )


def test_automatic_aoi_cache_creates_fetches_and_reuses(tmp_path):
    from isac_rf_planner.geo.local_osm_provider import AutoCachingOSMProvider

    database = tmp_path / "rf_geometry.sqlite"
    center = LatLon(lat=38.0, lon=-121.0)
    calls = []

    def fetch_tile(tile_center, half_size_m):
        calls.append((tile_center, half_size_m))
        return _network_fixture(center)

    first = AutoCachingOSMProvider(database, fetch_tile=fetch_tile)
    first.prefetch_all_data(center, 20_000.0)

    assert database.exists()
    assert len(calls) == 1
    assert first.acquisition_metadata["cache_hit"] is False
    assert first.acquisition_metadata["logical_overpass_queries"] == 1
    assert len(first._cached_buildings) == 1
    assert len(first._cached_landuse) == 1

    def fail_fetch(*args, **kwargs):
        raise AssertionError("cached AOI must not contact OSM")

    second = AutoCachingOSMProvider(database, fetch_tile=fail_fetch)
    second.prefetch_all_data(center, 19_000.0)
    assert second.acquisition_metadata["cache_hit"] is True
    assert second.acquisition_metadata["logical_overpass_queries"] == 0
    assert len(second._cached_buildings) == 1


def test_automatic_aoi_cache_adaptively_splits_failed_large_request(tmp_path):
    from isac_rf_planner.geo.local_osm_provider import AutoCachingOSMProvider

    database = tmp_path / "rf_geometry.sqlite"
    center = LatLon(lat=38.0, lon=-121.0)
    calls = []

    def fetch_tile(tile_center, half_size_m):
        calls.append(half_size_m)
        if half_size_m > 5_000.0:
            return None
        return ([], [])

    provider = AutoCachingOSMProvider(
        database,
        fetch_tile=fetch_tile,
        min_half_size_m=5_000.0,
    )
    provider.prefetch_all_data(center, 20_000.0)

    # One full-area attempt, then the bounded 4x4 fallback layout.
    assert len(calls) == 17
    assert calls.count(20_000.0) == 1
    assert calls.count(5_000.0) == 16
    assert provider.acquisition_metadata["logical_overpass_queries"] == 17

    provider_again = AutoCachingOSMProvider(
        database,
        fetch_tile=lambda *_: (_ for _ in ()).throw(AssertionError("no network")),
    )
    provider_again.prefetch_all_data(center, 20_000.0)
    assert provider_again.acquisition_metadata["cache_hit"] is True


def test_click_and_run_path_acquires_then_reports_sqlite_cache(tmp_path):
    from isac_rf_planner.geo.local_osm_provider import AutoCachingOSMProvider

    database = tmp_path / "rf_geometry.sqlite"
    center = LatLon(lat=38.0, lon=-121.0)
    provider = AutoCachingOSMProvider(
        database,
        fetch_tile=lambda *_: _network_fixture(center),
    )

    result = rf_planning_agent.run_rf_planning_for_point(
        lat=center.lat,
        lon=center.lon,
        rf_params=_dvt_rf_params(),
        map_provider=provider,
    )

    assert result["geometry_source"]["type"] == "automatic_osm_aoi_cache"
    assert result["geometry_source"]["database"] == str(database.resolve())
    assert result["geometry_source"]["acquisition"]["cache_hit"] is False
    assert result["geometry_source"]["acquisition"]["logical_overpass_queries"] == 1


def test_real_20km_plus_margin_uses_one_full_query_then_25_tiles(tmp_path):
    from isac_rf_planner.geo.local_osm_provider import AutoCachingOSMProvider

    center = LatLon(lat=38.271667, lon=-121.506111)
    calls = []

    def fetch_tile(tile_center, half_size_m):
        calls.append(half_size_m)
        if half_size_m > 5_000.0:
            return None
        return ([], [])

    provider = AutoCachingOSMProvider(
        tmp_path / "rf_geometry.sqlite",
        fetch_tile=fetch_tile,
    )
    provider.prefetch_all_data(center, 20_050.0)

    assert len(calls) == 26
    assert calls[0] == pytest.approx(20_050.0)
    assert len(calls[1:]) == 25
    assert all(value == pytest.approx(4_010.0) for value in calls[1:])
    assert provider.acquisition_metadata["logical_overpass_queries"] == 26
