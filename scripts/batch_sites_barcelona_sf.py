#!/usr/bin/env python3
"""POST /api/plan for Barcelona Sagrada Família pilot (3 gNBs, 4 sensor RX sites)."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.batch_sites_from_table import (  # noqa: E402
    DEFAULT_RX_HEIGHT_M,
    ang_span,
    build_request_body,
)

FREQ_MHZ = 3500.0
CH_BW_MHZ = 40.0
ANT_GAIN_DBI = 17.0

RX_SITES_ALL = [
    {"id": "S1", "lat": 41.403591198767, "lon": 2.1727185191258, "name": "CAT0133 sensor"},
    {"id": "S2", "lat": 41.404983, "lon": 2.175850, "name": "CAT6990 sensor"},
    {"id": "S3", "lat": 41.40380825186, "lon": 2.1756515958584, "name": "CAT6607 sensor"},
    {"id": "S4", "lat": 41.404678, "lon": 2.173200, "name": "kiosko_2 sensor"},
]


def make_sector(
    sector_id: str,
    az: float,
    hbw: float,
    *,
    tx_power_dbm: float,
    pci: int | None = None,
    vbw: float = 10.0,
) -> dict:
    s0, s1 = ang_span(az, hbw)
    sec = {
        "sector_id": sector_id,
        "sector_type": "angle",
        "start_angle_deg": round(s0, 4),
        "end_angle_deg": round(s1, 4),
        "azimuth_deg": float(az),
        "beamwidth_h_deg": float(hbw),
        "beamwidth_v_deg": float(vbw),
        "freq_mhz": FREQ_MHZ,
        "tx_power_dbm": float(tx_power_dbm),
        "channel_bandwidth_mhz": CH_BW_MHZ,
        "tx_antenna_gain_dbi": ANT_GAIN_DBI,
    }
    if pci is not None:
        sec["pci"] = int(pci)
    return sec


GNB_SITES = [
    {
        "name": "CAT0133",
        "lat": 41.403591198767,
        "lon": 2.1727185191258,
        "tx_height_m": 10.0,
        "sectors": [
            make_sector("CATX0133P1A", 70, 65, tx_power_dbm=53.01, pci=800),
            make_sector("CATX0133P2A", 200, 65, tx_power_dbm=53.01, pci=801),
            make_sector("CATX0133P3A", 310, 65, tx_power_dbm=53.01, pci=802),
        ],
    },
    {
        "name": "CAT6990",
        "lat": 41.404983,
        "lon": 2.175850,
        "tx_height_m": 10.0,
        "sectors": [
            make_sector("CATX6990P1", 225, 65, tx_power_dbm=53.01),
        ],
    },
    {
        "name": "CAT6607",
        "lat": 41.40380825186,
        "lon": 2.1756515958584,
        "tx_height_m": 3.0,
        "sectors": [
            make_sector("CATX6607P1E", 25, 82, tx_power_dbm=43.01, pci=5, vbw=43.0),
            make_sector("CATX6607P2E", 220, 82, tx_power_dbm=43.01, pci=6, vbw=43.0),
        ],
    },
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("base", nargs="?", default="http://127.0.0.1:8000")
    p.add_argument("--ray-mode", default="3d_osm")
    p.add_argument("--max-range-m", type=float, default=2500.0)
    p.add_argument("--step-m", type=float, default=5.0)
    p.add_argument("--dtheta-deg", type=float, default=5.0)
    p.add_argument("--rx-height-m", type=float, default=DEFAULT_RX_HEIGHT_M)
    p.add_argument(
        "--only",
        choices=[s["name"] for s in GNB_SITES],
        default=None,
        help="Run a single gNB by site name (e.g. CAT0133).",
    )
    p.add_argument(
        "--tx-height-m",
        type=float,
        default=None,
        help="Override TX height for all selected sites (default: per-site table).",
    )
    p.add_argument("--no-publish-ui", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    url = f"{args.base.rstrip('/')}/api/plan"
    sites = [s for s in GNB_SITES if args.only is None or s["name"] == args.only]
    if not sites:
        print(f"No site matched --only {args.only!r}", flush=True)
        return 2
    n = len(sites)
    print(f"Barcelona pilot: {n} gNBs, {len(RX_SITES_ALL)} RX sensor sites, {FREQ_MHZ} MHz", flush=True)

    for idx, site in enumerate(sites, 1):
        tx_h = float(args.tx_height_m) if args.tx_height_m is not None else float(site["tx_height_m"])
        body = build_request_body(
            lat=site["lat"],
            lon=site["lon"],
            sectors=site["sectors"],
            ray_mode=str(args.ray_mode).strip().lower(),
            publish_ui=not args.no_publish_ui,
            max_range_m=args.max_range_m,
            step_m=args.step_m,
            dtheta_deg=args.dtheta_deg,
            tx_height_m=tx_h,
            rx_height_m=args.rx_height_m,
            rx_sites=RX_SITES_ALL,
        )
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        t0 = time.time()
        print(
            f"[{idx}/{n}] {site['name']} lat={site['lat']} lon={site['lon']} "
            f"sectors={len(site['sectors'])} tx_h={tx_h}m ...",
            flush=True,
        )
        try:
            with urllib.request.urlopen(req, timeout=3_600) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            err_b = e.read()
            print(f"  HTTP {e.code}: {err_b.decode('utf-8', errors='replace')[:2000]}", flush=True)
            return 1
        except Exception as e:
            print(f"  failed: {e}", flush=True)
            return 1
        elapsed = time.time() - t0
        try:
            out = json.loads(raw)
            eff = out.get("ray_mode") or out.get("requested_ray_mode")
            ba = out.get("building_area_sqm")
            print(f"  OK 200 in {elapsed:.1f}s ray_mode={eff!r} building_area_sqm={ba}", flush=True)
        except Exception:
            print(f"  OK 200 in {elapsed:.1f}s", flush=True)
        if idx < n:
            time.sleep(0.3)

    print("Done.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
