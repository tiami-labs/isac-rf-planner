#!/usr/bin/env python3
"""
POST /api/plan once per site (lat, lon), multiple sectors per site.

The JSON body matches what the **/3d** dashboard sends: ``planner_3d.js`` ``buildPlanRequestBody``
(literals + ``getNumber`` / ``getString`` fallbacks), cross-checked against ``index_3d.html``
initial ``value=`` / selected ``<option>`` attributes — **not** guessed from ``PlanRequest`` or YAML.

Only site-specific fields differ: ``lat``, ``lon``, ``sectors``, and root ``freq_mhz`` /
``tx_power_dbm`` / ``channel_bandwidth_mhz`` / ``tx_antenna_gain_dbi`` taken from the first
sector row (table-driven carriers). ``rx_sites`` (per TX index) mark magenta RX points on /3d.
``HEIGHT_FT`` in ``RAW_ROWS`` is ignored for geometry.

Default CLI ``ray_mode`` is **3d_osm** (``index_3d.html`` default selection). Use ``--ray-mode 2d``
only for the Leaflet ``/`` app.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from collections import defaultdict
from copy import deepcopy
from typing import Any

# Ground-truth: index_3d.html — input id="tx-height-m" value="10.0"
# (same fallback as planner_3d.js getNumber("tx-height-m", 10.0)).
DEFAULT_TX_HEIGHT_M = 10.0
# index_3d.html — input id="rx-height-m" value="1.5"
DEFAULT_RX_HEIGHT_M = 1.5

# Mirror planner_3d.js buildPlanRequestBody() object literals + getNumber/getString defaults.
# When changing the dashboard defaults, update: index_3d.html, planner_3d.js, and this dict.
DASHBOARD_3D_PLAN_ENVELOPE: dict[str, Any] = {
    "noise_figure_db": 7.0,
    "subcarrier_spacing_khz": 30.0,
    "num_resource_blocks": 100,
    "channel_bandwidth_mhz": 40.0,
    "tx_antenna_gain_dbi": 17.0,
    "mimo_mode": "MIMO",
    "enable_link_adaptation": True,
    "fixed_modulation": None,
    "electrical_tilt_deg": 0.0,
    "mechanical_tilt_deg": 0.0,
    "vertical_beamwidth_deg": 8.0,
    "max_vertical_attenuation_db": 30.0,
    "path_loss_model": "3gpp_38901",
    "propagation_scenario": "umi_street_canyon",
    "max_horizontal_attenuation_db": 30.0,
    "front_to_back_attenuation_db": 25.0,
    "shadow_loss_db": 6.0,
    "shadow_decay_db_per_100m": 4.0,
    "diffraction_base_loss_db": 6.0,
    "diffraction_slope_db_per_100m": 3.0,
    "canyon_recovery_max_db": 8.0,
    "canyon_recovery_slope_db_per_100m": 6.0,
    "termination_rsrp_dbm": -140.0,
    "shadow_loss_cap_db": 20.0,
    "diffraction_loss_cap_db": 16.0,
    "building_attenuation": {
        "materials": {
            "concrete": 5.0,
            "brick": 3.5,
            "wood": 1.25,
            "glass": 2.75,
            "metal": 14.75,
            "unknown": 5.0,
        },
        "overall": {"scale": 0.75, "reduction_db": 6.0},
    },
}

# PID, LONGITUDE, LATITUDE, NRARFCN, Azimuth_Adj, BEAMWIDTH, HEIGHT_FT (unused for API tx height), V_WIDTH, H_WIDTH, ANT_GAIN, TXPOWER, BANDWIDTH
RAW_ROWS: list[tuple] = [
    (325, -97.09761111, 32.73766667, 659334, 128, 65, 76, 7, 65, 17.6, 55.1, 100),
    (694, -97.09761111, 32.73766667, 659334, 248, 65, 76, 7, 65, 17.6, 55.1, 100),
    (622, -97.09761111, 32.73766667, 659334, 8, 65, 76, 7, 65, 17.8, 55.1, 100),
    (322, -97.0916, 32.73891667, 659334, 128, 65, 60, 7, 65, 17.6, 55.1, 100),
    (308, -97.0916, 32.73891667, 659334, 248, 65, 60, 7, 65, 17.6, 55.1, 100),
    (304, -97.0916, 32.73891667, 659334, 8, 65, 60, 7, 65, 17.6, 55.1, 100),
    (578, -97.08292379, 32.74347014, 659334, 254, 66, 75, 8, 66, 19, 53.6, 100),
    (139, -97.08292379, 32.74347014, 659334, 131, 66, 75, 8, 66, 19.1, 53.6, 100),
    (691, -97.08292379, 32.74347014, 659334, 11, 66, 75, 8, 66, 19.1, 53.6, 100),
    (593, -97.0765, 32.74708333, 659334, 8, 65, 74, 7, 65, 17.6, 55.1, 100),
    (468, -97.0765, 32.74708333, 659334, 248, 65, 74, 7, 65, 17.6, 55.1, 100),
    (565, -97.0765, 32.74708333, 659334, 128, 65, 74, 7, 65, 17.6, 55.1, 100),
    (197, -97.098357, 32.747617, 659334, 85, 12, 55, 11.62, 12, 23.1, 21.6, 100),
    (391, -97.098357, 32.747617, 659334, 145, 12, 55, 11.62, 12, 23.1, 21.6, 100),
    (180, -97.098357, 32.747617, 659334, 45, 12, 55, 11.62, 12, 23.1, 21.6, 100),
    (181, -97.098357, 32.747617, 659334, 25, 12, 55, 11.62, 12, 23.1, 21.6, 100),
    (514, -97.098357, 32.747617, 659334, 25, 12, 55, 11.62, 12, 23.1, 21.6, 100),
    (682, -97.098357, 32.747617, 659334, 5, 12, 55, 11.62, 12, 23.1, 21.6, 100),
    (154, -97.098357, 32.747617, 659334, 127, 66, 55, 8, 66, 18.9, 21.6, 100),
    (239, -97.085338, 32.751177, 659334, 195, 12, 90, 11.62, 12, 23.1, 21.6, 100),
    (509, -97.085338, 32.751177, 659334, 352, 66, 90, 8, 66, 18.9, 21.6, 100),
    (159, -97.085338, 32.751177, 659334, 195, 12, 90, 11.62, 12, 23.1, 21.6, 100),
    (179, -97.085338, 32.751177, 659334, 255, 12, 90, 11.62, 12, 23.1, 21.6, 100),
    (541, -97.085338, 32.751177, 659334, 315, 12, 90, 11.62, 12, 23.1, 21.6, 100),
    (332, -97.085338, 32.751177, 659334, 215, 12, 90, 11.62, 12, 23.1, 21.6, 100),
    (574, -97.085338, 32.751177, 659334, 175, 12, 90, 11.62, 12, 23.1, 21.6, 100),
]

# RX sites keyed by TX index (1-based) = enumeration order of sites sorted by (lat, lon), matching this script.
RX_SITES_BY_TX_INDEX: dict[int, list[dict[str, Any]]] = {
    1: [
        {
            "id": "rx1_1",
            "lat": 32.74879392838919,
            "lon": -97.09569261063106,
            "name": "ATT Stadium Parking 10",
        }
    ],
    2: [
        {
            "id": "rx2_1",
            "lat": 32.74971890122719,
            "lon": -97.09070261249333,
            "name": "ATT Stadium Parking 2",
        }
    ],
    3: [
        {
            "id": "rx3_1",
            "lat": 32.74671762459562,
            "lon": -97.09023461735731,
            "name": "ATT Stadium Parking 5",
        }
    ],
    4: [
        {
            "id": "rx4_1",
            "lat": 32.74647653401865,
            "lon": -97.09459867198457,
            "name": "ATT Stadium Parking 7",
        }
    ],
}


def nr_arfcn_to_freq_mhz(n_ref: int) -> float:
    """3GPP TS 38.104, FR1 global: F_MHz = 3000 + 0.015 * (N_REF - 600000)."""
    return 3000.0 + 0.015 * (float(n_ref) - 600_000.0)


def ang_span(az: float, bw: float) -> tuple[float, float]:
    a = (az - bw / 2.0) % 360.0
    b = (az + bw / 2.0) % 360.0
    return (a, b)


def build_sector(
    row: tuple,
    *,
    freq_mhz: float,
) -> dict[str, Any]:
    (
        pid,
        _lon,
        _lat,
        _n_arfcn,
        az,
        beam_bw,
        _h_ft,
        v_w,
        h_w,
        ant_g,
        txp,
        ch_bw,
    ) = row
    s0, s1 = ang_span(az, beam_bw)
    return {
        "sector_id": f"pid_{pid}",
        "sector_type": "angle",
        "start_angle_deg": round(s0, 4),
        "end_angle_deg": round(s1, 4),
        "azimuth_deg": float(az),
        "beamwidth_h_deg": float(h_w),
        "beamwidth_v_deg": float(v_w),
        "freq_mhz": float(freq_mhz),
        "tx_power_dbm": float(txp),
        "channel_bandwidth_mhz": float(ch_bw),
        "tx_antenna_gain_dbi": float(ant_g),
    }


def build_request_body(
    lat: float,
    lon: float,
    sectors: list[dict[str, Any]],
    ray_mode: str,
    publish_ui: bool,
    max_range_m: float,
    step_m: float,
    dtheta_deg: float,
    tx_height_m: float,
    rx_height_m: float,
    rx_sites: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Same fields as /3d ``buildPlanRequestBody``, except ``publish_ui`` for remote polling."""
    body = deepcopy(DASHBOARD_3D_PLAN_ENVELOPE)
    s0 = sectors[0]
    body["lat"] = lat
    body["lon"] = lon
    body["ray_mode"] = ray_mode
    body["sectors"] = sectors
    body["publish_ui"] = bool(publish_ui)
    body["tx_height_m"] = float(tx_height_m)
    body["rx_height_m"] = float(rx_height_m)
    body["max_range_m"] = float(max_range_m)
    body["step_m"] = float(step_m)
    body["dtheta_deg"] = float(dtheta_deg)
    body["freq_mhz"] = float(s0["freq_mhz"])
    body["tx_power_dbm"] = float(s0["tx_power_dbm"])
    body["channel_bandwidth_mhz"] = float(s0["channel_bandwidth_mhz"])
    body["tx_antenna_gain_dbi"] = float(s0.get("tx_antenna_gain_dbi", body["tx_antenna_gain_dbi"]))
    if rx_sites:
        body["rx_sites"] = rx_sites
    return body


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("base", nargs="?", default="http://127.0.0.1:8000", help="API root (default: %(default)s)")
    p.add_argument(
        "--ray-mode",
        default="3d_osm",
        help="Must match the UI you compare against. Default 3d_osm (same as /3d). Use 2d for Leaflet /.",
    )
    p.add_argument("--step-m", type=float, default=5.0, help="Grid step (default 5, match /3d dr-m)")
    p.add_argument("--max-range-m", type=float, default=2500.0, help="Max range in meters (default 2.5 km, match /3d)")
    p.add_argument("--dtheta-deg", type=float, default=5.0, help="Bearing step cap (adaptive dtheta also applies in OSM modes)")
    p.add_argument("--no-publish-ui", action="store_true", help="Do not push results to the remote UI poll queue")
    p.add_argument(
        "--sector-freq-mhz",
        type=float,
        default=None,
        help="If set, use this for every sector's freq_mhz and root freq (e.g. 3500 to match Advanced RF when not using ARFCN-derived values).",
    )
    p.add_argument(
        "--tx-height-m",
        type=float,
        default=DEFAULT_TX_HEIGHT_M,
        help=f"TX height (default {DEFAULT_TX_HEIGHT_M}, index_3d.html #tx-height-m).",
    )
    p.add_argument(
        "--rx-height-m",
        type=float,
        default=DEFAULT_RX_HEIGHT_M,
        help=f"RX height (default {DEFAULT_RX_HEIGHT_M}, index_3d.html #rx-height-m).",
    )
    p.add_argument(
        "--limit-sites",
        type=int,
        default=None,
        metavar="N",
        help="Process only the first N sites after sorting by (lat, lon). Omit for all sites.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    base = args.base.rstrip("/")
    url = f"{base}/api/plan"

    groups: dict[tuple[float, float], list[tuple]] = defaultdict(list)
    for row in RAW_ROWS:
        lat, lon = float(row[2]), float(row[1])
        key = (round(lat, 8), round(lon, 8))
        groups[key].append(row)

    site_keys_all = sorted(groups.keys(), key=lambda t: (t[0], t[1]))
    lim = args.limit_sites
    if lim is not None and lim < 1:
        print("--limit-sites must be >= 1", flush=True)
        return 2
    site_keys = site_keys_all[:lim] if lim is not None else site_keys_all
    n_total = len(site_keys_all)
    n_sites = len(site_keys)
    suffix = f" (of {n_total} total)" if lim is not None else ""
    print(
        f"Sites: {n_sites}{suffix} (sectors per site: { [len(groups[k]) for k in site_keys] })",
        flush=True,
    )
    print(
        f"ray_mode={args.ray_mode!r} tx_h={args.tx_height_m} rx_h={args.rx_height_m} "
        f"step_m={args.step_m} max_range_m={args.max_range_m} "
        f"sector_freq={'ARFCN' if args.sector_freq_mhz is None else args.sector_freq_mhz!r}",
        flush=True,
    )

    for idx, (lat, lon) in enumerate(site_keys, 1):
        rows = groups[(lat, lon)]
        n_arfcn = int(rows[0][3])
        base_freq = (
            float(args.sector_freq_mhz)
            if args.sector_freq_mhz is not None
            else round(nr_arfcn_to_freq_mhz(n_arfcn), 3)
        )
        sectors = [build_sector(r, freq_mhz=base_freq) for r in rows]
        rx_list = RX_SITES_BY_TX_INDEX.get(idx, [])
        body = build_request_body(
            lat=lat,
            lon=lon,
            sectors=sectors,
            ray_mode=str(args.ray_mode).strip().lower(),
            publish_ui=not args.no_publish_ui,
            max_range_m=args.max_range_m,
            step_m=args.step_m,
            dtheta_deg=args.dtheta_deg,
            tx_height_m=args.tx_height_m,
            rx_height_m=args.rx_height_m,
            rx_sites=rx_list if rx_list else None,
        )
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        t0 = time.time()
        print(
            f"[{idx}/{n_sites}] POST lat={lat} lon={lon} sectors={len(sectors)} "
            f"rx_sites={len(rx_list)} tx_h={args.tx_height_m} rx_h={args.rx_height_m} ...",
            flush=True,
        )
        try:
            with urllib.request.urlopen(req, timeout=3_600) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            err_b = e.read()
            try:
                err_j = json.loads(err_b)
                detail = err_j.get("detail", err_b.decode("utf-8", errors="replace")[:2000])
            except Exception:
                detail = err_b.decode("utf-8", errors="replace")[:2000]
            print(f"  HTTP {e.code}: {detail}", flush=True)
            return 1
        except Exception as e:
            print(f"  request failed: {e}", flush=True)
            return 1
        elapsed = time.time() - t0
        try:
            out = json.loads(raw)
            eff = out.get("ray_mode") or out.get("requested_ray_mode")
            print(
                f"  OK 200 in {elapsed:.1f}s eff_ray_mode={eff!r} keys~{list(out.keys())[:8]}",
                flush=True,
            )
        except Exception:
            print(f"  OK 200 in {elapsed:.1f}s (huge or non-JSON body)", flush=True)
        if idx < n_sites:
            time.sleep(0.3)

    print("Done. If /3d is open, the last plan published to the remote queue is the one you see first.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
