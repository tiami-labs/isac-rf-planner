"""Address / place lookup → lat/lon for the RF planner."""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_COORD_RE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$"
)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
USER_AGENT = "agentic-rf-planner/1.0 (+local)"


def _parse_coordinate_query(query: str) -> Optional[Dict[str, Any]]:
    m = _COORD_RE.match(query.strip())
    if not m:
        return None
    lat = float(m.group(1))
    lon = float(m.group(2))
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return {
        "lat": lat,
        "lon": lon,
        "formatted_address": f"{lat:.6f}, {lon:.6f}",
        "place_id": None,
        "types": ["coordinates"],
        "confidence": "exact",
    }


def _google_geocode(query: str, api_key: str, limit: int) -> List[Dict[str, Any]]:
    resp = requests.get(
        GOOGLE_GEOCODE_URL,
        params={"address": query, "key": api_key},
        timeout=12,
    )
    resp.raise_for_status()
    data = resp.json()
    status = str(data.get("status") or "")
    if status not in ("OK", "ZERO_RESULTS"):
        raise RuntimeError(f"Google Geocoding API status={status}: {data.get('error_message') or data}")
    results: List[Dict[str, Any]] = []
    for item in (data.get("results") or [])[:limit]:
        loc = (item.get("geometry") or {}).get("location") or {}
        lat = loc.get("lat")
        lon = loc.get("lng")
        if lat is None or lon is None:
            continue
        results.append(
            {
                "lat": float(lat),
                "lon": float(lon),
                "formatted_address": str(item.get("formatted_address") or query),
                "place_id": item.get("place_id"),
                "types": list(item.get("types") or []),
                "confidence": str((item.get("geometry") or {}).get("location_type") or "unknown"),
            }
        )
    return results


def _nominatim_geocode(query: str, limit: int) -> List[Dict[str, Any]]:
    resp = requests.get(
        NOMINATIM_URL,
        params={"q": query, "format": "json", "limit": max(1, min(limit, 10))},
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=12,
    )
    resp.raise_for_status()
    data = resp.json()
    results: List[Dict[str, Any]] = []
    for item in data or []:
        if item.get("lat") is None or item.get("lon") is None:
            continue
        results.append(
            {
                "lat": float(item["lat"]),
                "lon": float(item["lon"]),
                "formatted_address": str(item.get("display_name") or query),
                "place_id": str(item.get("place_id") or ""),
                "types": [str(item.get("type") or "place")],
                "confidence": str(item.get("class") or "unknown"),
            }
        )
    return results


def geocode_query(query: str, *, limit: int = 5) -> Dict[str, Any]:
    """Resolve a free-text address or ``lat,lon`` string to coordinates."""
    q = str(query or "").strip()
    if not q:
        raise ValueError("query is required")

    coord = _parse_coordinate_query(q)
    if coord:
        return {
            "status": "ok",
            "query": q,
            "provider": "coordinates",
            "results": [coord],
        }

    lim = max(1, min(int(limit), 10))
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY") or os.environ.get("GOOGLE_MAPS_APIKEY") or ""

    if api_key:
        try:
            results = _google_geocode(q, api_key, lim)
            if results:
                return {"status": "ok", "query": q, "provider": "google", "results": results}
        except Exception as exc:
            logger.warning("Google geocode failed, trying Nominatim: %s", exc)

    try:
        results = _nominatim_geocode(q, lim)
    except Exception as exc:
        logger.error("Nominatim geocode failed: %s", exc)
        raise RuntimeError(f"Geocoding failed: {exc}") from exc

    if not results:
        return {"status": "not_found", "query": q, "provider": "nominatim", "results": []}

    provider = "nominatim" if not api_key else "nominatim_fallback"
    return {"status": "ok", "query": q, "provider": provider, "results": results}
