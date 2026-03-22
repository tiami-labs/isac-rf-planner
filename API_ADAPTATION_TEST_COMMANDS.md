# API adaptation test commands

Assume the server is running at `http://127.0.0.1:8000` and a `/3d` tab is open.

## 1) Ping
```bash
curl -s http://127.0.0.1:8000/api/ping | jq
```

## 2) Planner-phase diagnostics
```bash
curl -s -X POST http://127.0.0.1:8000/api/debug/planner-phase \
  -H 'Content-Type: application/json' \
  -d '{"phase":"curl_probe","detail":"manual planner-phase check","t":1234567890}' | jq

curl -s http://127.0.0.1:8000/api/debug/planner-state | jq
```

## 3) Remote publish + poll (coverage payload)
```bash
curl -s "http://127.0.0.1:8000/api/ui/remote-plan-rf/poll?since_seq=0" | jq

curl -s -X POST http://127.0.0.1:8000/api/ui/remote-plan-rf \
  -H 'Content-Type: application/json' \
  -d '{
    "lat": 37.7749,
    "lon": -122.4194,
    "ray_mode": "3d_osm",
    "tx_height_m": 10.0,
    "rx_height_m": 1.5,
    "max_range_m": 600.0,
    "step_m": 20.0,
    "dtheta_deg": 15.0
  }' | jq

curl -s "http://127.0.0.1:8000/api/ui/remote-plan-rf/poll?since_seq=0" | jq
```

## 4) Remote publish + poll (3D RT payload)
Requires cached mesh profiles for this TX/resolution.
```bash
curl -s -X POST http://127.0.0.1:8000/api/ui/remote-plan-rf \
  -H 'Content-Type: application/json' \
  -d '{
    "lat": 37.7749,
    "lon": -122.4194,
    "ray_mode": "3d_rt",
    "tx_height_m": 10.0,
    "rx_height_m": 1.5,
    "rx_lat": 37.7755,
    "rx_lon": -122.4182,
    "max_range_m": 600.0,
    "step_m": 20.0,
    "dtheta_deg": 15.0
  }' | jq

curl -s "http://127.0.0.1:8000/api/ui/remote-plan-rf/poll?since_seq=0" | jq
```

## 5) Combined plan + trace
Requires cached mesh profiles for this TX/resolution.
```bash
curl -s -X POST http://127.0.0.1:8000/api/3d/plan-and-trace \
  -H 'Content-Type: application/json' \
  -d '{
    "lat": 37.7749,
    "lon": -122.4194,
    "rx_lat": 37.7755,
    "rx_lon": -122.4182,
    "ray_mode": "3d_rt",
    "tx_height_m": 10.0,
    "rx_height_m": 1.5,
    "max_range_m": 600.0,
    "step_m": 20.0,
    "dtheta_deg": 15.0
  }' | jq
```

## 6) Replay a curl result in the browser
- Open `/3d`
- Paste any response body from `/api/plan`, `/api/3d/plan-and-trace` (use `.plan`), or `/api/ui/remote-plan-rf/poll` (use `.plan`) into the JSON panel
- Click **Apply plan JSON to map**

Or in DevTools console:
```javascript
await window.RFPlanner3DApplyPlan(payload)
```
