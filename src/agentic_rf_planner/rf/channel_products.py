"""Persist waveform-agnostic channel-analysis grids as machine-readable NPZ files."""

from __future__ import annotations

import json
import os
import re
import uuid
import zipfile
from collections import OrderedDict
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Mapping, Optional

import numpy as np

_PRODUCT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_PRODUCT_CACHE_LOCK = RLock()
_PRODUCT_CACHE: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()


def get_cached_channel_product(product_id: str) -> Optional[Dict[str, Any]]:
    """Return the latest in-memory product arrays for low-latency UI inspection."""

    normalized = str(product_id or "").strip().lower()
    with _PRODUCT_CACHE_LOCK:
        cached = _PRODUCT_CACHE.get(normalized)
        if cached is None:
            return None
        _PRODUCT_CACHE.move_to_end(normalized)
        return cached


def _cache_channel_product(product_id: str, arrays: Mapping[str, np.ndarray], metadata: Mapping[str, Any]) -> None:
    max_products = max(0, int(os.environ.get("RF_CHANNEL_PRODUCT_MEMORY_CACHE", "1") or "1"))
    if max_products <= 0:
        return
    with _PRODUCT_CACHE_LOCK:
        _PRODUCT_CACHE[product_id] = {"arrays": dict(arrays), "metadata": dict(metadata)}
        _PRODUCT_CACHE.move_to_end(product_id)
        while len(_PRODUCT_CACHE) > max_products:
            _PRODUCT_CACHE.popitem(last=False)


def resolve_channel_product_dir() -> Path:
    """Return the product directory used by both the planner and download route."""

    configured = str(os.environ.get("RF_CHANNEL_PRODUCT_DIR", "") or "").strip()
    root = Path(configured).expanduser() if configured else Path.cwd() / "data" / "channel_analysis"
    return root.resolve()


def channel_product_path(product_id: str) -> Optional[Path]:
    """Resolve a validated product ID without allowing path traversal."""

    normalized = str(product_id or "").strip().lower()
    if not _PRODUCT_ID_RE.fullmatch(normalized):
        return None
    return resolve_channel_product_dir() / f"{normalized}.npz"


def write_channel_product(
    *,
    arrays: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> Dict[str, Any]:
    """Write a compressed array bundle and return API-facing product metadata."""

    directory = resolve_channel_product_dir()
    directory.mkdir(parents=True, exist_ok=True)
    product_id = uuid.uuid4().hex
    path = directory / f"{product_id}.npz"

    payload: Dict[str, np.ndarray] = {}
    for name, values in arrays.items():
        if values is None:
            continue
        arr = np.asarray(values)
        if arr.size == 0:
            continue
        payload[str(name)] = arr
    metadata_out = dict(metadata)
    declared_units = metadata_out.get("array_units", {})
    if not isinstance(declared_units, Mapping):
        declared_units = {}
    array_manifest: Dict[str, Any] = {}
    for name, arr in payload.items():
        unit = declared_units.get(name)
        if unit is None and name.startswith("rsrp_by_sector__"):
            unit = "dBm"
        array_manifest[name] = {
            "dtype": str(arr.dtype),
            "shape": [int(value) for value in arr.shape],
            "units": unit,
            "point_aligned": bool(arr.ndim == 1 and arr.shape[0] == len(payload.get("latitude_deg", []))),
        }
    metadata_out["array_manifest"] = array_manifest
    metadata_out["settings_complete"] = bool(metadata_out.get("rf_config") is not None)
    payload["metadata_json"] = np.asarray(
        json.dumps(metadata_out, separators=(",", ":"), sort_keys=True)
    )
    compression_level = min(9, max(0, int(os.environ.get("RF_CHANNEL_PRODUCT_COMPRESSION_LEVEL", "1") or "1")))
    compression = zipfile.ZIP_STORED if compression_level == 0 else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(
        path,
        mode="w",
        compression=compression,
        compresslevel=(compression_level if compression != zipfile.ZIP_STORED else None),
        allowZip64=True,
    ) as archive:
        for name, arr in payload.items():
            with archive.open(f"{name}.npy", mode="w", force_zip64=True) as member:
                np.lib.format.write_array(member, np.asarray(arr), allow_pickle=False)
    _cache_channel_product(
        product_id,
        {name: arr for name, arr in payload.items() if name != "metadata_json"},
        metadata_out,
    )

    return {
        "product_id": product_id,
        "format": "npz",
        "content_type": "application/octet-stream",
        "download_url": f"/api/channel-analysis/products/{product_id}",
        "point_count": int(len(payload.get("latitude_deg", np.asarray([])))),
        "arrays": sorted(name for name in payload if name != "metadata_json"),
        "metadata_member": "metadata_json",
        "size_bytes": int(path.stat().st_size),
    }
