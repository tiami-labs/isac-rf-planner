import json
import time

from fastapi.testclient import TestClient

from agentic_rf_planner.api import rest


def test_raytrace_stream_emits_progress_and_result(monkeypatch):
    def fake_compute(req, progress=None):
        if progress is not None:
            progress({"type": "heartbeat", "stage": "fake_start", "detail": "begin", "elapsed_s": 0.0})
        time.sleep(0.01)
        if progress is not None:
            progress({"type": "heartbeat", "stage": "fake_done", "detail": "end", "elapsed_s": 0.01})
        return {
            "tx": {"lat": req.tx_lat, "lon": req.tx_lon},
            "rx": {"lat": req.rx_lat, "lon": req.rx_lon},
            "paths": [],
            "los": False,
            "diagnostics": {"osm_walls": 0, "raw_paths": 0, "returned": 0, "los": False},
            "message": "ok",
        }

    monkeypatch.setattr(rest, "_compute_raytrace_paths_response", fake_compute)

    client = TestClient(rest.app)
    response = client.post(
        "/api/raytrace_paths/stream",
        json={
            "tx_lat": 37.0,
            "tx_lon": -122.0,
            "rx_lat": 37.1,
            "rx_lon": -122.1,
        },
    )
    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.strip().splitlines() if line.strip()]
    assert any(evt.get("type") == "heartbeat" and evt.get("stage") == "fake_start" for evt in events)
    assert events[-1]["type"] == "result"
    assert events[-1]["payload"]["message"] == "ok"
