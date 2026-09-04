"""Persist waveform-agnostic channel-analysis grids as machine-readable NPZ files.

Large products are written one member at a time.  The previous ``np.savez`` path
first converted every Python list into an ndarray and retained all of those arrays
simultaneously, creating a large export-time memory spike.
"""

from __future__ import annotations

import json
import math
import os
import re
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

import numpy as np

_PRODUCT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_COORDINATE_ARRAYS = {"latitude_deg", "longitude_deg"}
_BOOLEAN_ARRAYS = {
    "detectable",
    "detectable_screening",
    "detectable_qualified",
    "snr_noise_interference_ok",
    "doppler_resolved",
    "doppler_ambiguous",
    "direct_residual_ok",
    "dynamic_range_ok",
    "return_environment_valid",
    "return_los",
    "tx_los_terrain",
    "tx_target_los",
}
_UINT8_ARRAYS = {"return_terrain_state_code", "tx_terrain_state_code", "constraint_failure_code"}


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


def _product_array(name: str, values: Any) -> np.ndarray:
    """Use compact stable dtypes without copying arrays already in that form."""

    if name in _COORDINATE_ARRAYS:
        return np.ascontiguousarray(values, dtype=np.float64)
    if name in _BOOLEAN_ARRAYS:
        return np.ascontiguousarray(values, dtype=np.bool_)
    if name in _UINT8_ARRAYS:
        return np.ascontiguousarray(values, dtype=np.uint8)

    arr = np.asarray(values)
    if arr.dtype.kind in "fiu":
        return np.ascontiguousarray(arr, dtype=np.float32)
    if arr.dtype.kind == "b":
        return np.ascontiguousarray(arr, dtype=np.bool_)
    if arr.dtype.kind == "O":
        # Object arrays require pickle and are intentionally not allowed in the
        # machine-readable product. Convert textual metadata to plain Unicode.
        return np.ascontiguousarray(arr.astype(str))
    return np.ascontiguousarray(arr)


def _write_npy_member(archive: zipfile.ZipFile, name: str, array: np.ndarray) -> None:
    with archive.open(f"{name}.npy", mode="w", force_zip64=True) as member:
        np.lib.format.write_array(member, array, allow_pickle=False)


def write_channel_product(
    *,
    arrays: Mapping[str, Any] | Iterable[tuple[str, Any]],
    metadata: Mapping[str, Any],
) -> Dict[str, Any]:
    """Write a compressed array bundle with bounded export-time memory.

    Each member is converted, compressed, and released before the next member is
    processed.  Coordinates remain float64; numerical channel layers use float32;
    masks use bool; compact categorical encodings use uint8.
    """

    directory = resolve_channel_product_dir()
    directory.mkdir(parents=True, exist_ok=True)
    product_id = uuid.uuid4().hex
    path = directory / f"{product_id}.npz"
    temporary = directory / f".{product_id}.npz.tmp"

    written_names: list[str] = []
    point_count = 0
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            array_items = arrays.items() if isinstance(arrays, Mapping) else arrays
            for raw_name, values in array_items:
                if values is None:
                    continue
                name = str(raw_name)
                arr = _product_array(name, values)
                if arr.size == 0:
                    continue
                _write_npy_member(archive, name, arr)
                written_names.append(name)
                if name == "latitude_deg":
                    point_count = int(arr.size)
                del arr

            metadata_payload = dict(metadata)
            metadata_payload.setdefault(
                "storage",
                {
                    "coordinates": "float64",
                    "numeric_layers": "float32",
                    "masks": "bool",
                    "categorical_codes": "uint8",
                    "writer": "streaming_npz",
                },
            )
            metadata_array = np.asarray(
                json.dumps(metadata_payload, separators=(",", ":"), sort_keys=True)
            )
            _write_npy_member(archive, "metadata_json", metadata_array)
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise

    return {
        "product_id": product_id,
        "format": "npz",
        "content_type": "application/octet-stream",
        "download_url": f"/api/channel-analysis/products/{product_id}",
        "point_count": point_count,
        "arrays": sorted(written_names),
        "metadata_member": "metadata_json",
        "size_bytes": int(path.stat().st_size),
        "storage": {
            "coordinates": "float64",
            "numeric_layers": "float32",
            "masks": "bool",
            "categorical_codes": "uint8",
            "writer": "streaming_npz",
        },
    }


def probe_channel_product(product_id: str, *, latitude: float, longitude: float) -> Optional[Dict[str, Any]]:
    """Return the nearest candidate-target record from a persisted channel product."""

    path = channel_product_path(product_id)
    if path is None or not path.is_file():
        return None
    with np.load(path, allow_pickle=False) as product:
        if "latitude_deg" not in product or "longitude_deg" not in product:
            return None
        lat = np.asarray(product["latitude_deg"], dtype=np.float64)
        lon = np.asarray(product["longitude_deg"], dtype=np.float64)
        if lat.size == 0 or lon.size != lat.size:
            return None

        lat0 = np.radians(float(latitude))
        # Reuse dx as the distance accumulator to avoid holding dy, dx and d2
        # simultaneously for large products.
        dx = np.radians(lon - float(longitude)) * 6_371_000.0 * np.cos(lat0)
        dx *= dx
        dy = np.radians(lat - float(latitude)) * 6_371_000.0
        dx += dy * dy
        index = int(np.argmin(dx))
        query_distance_m = float(math.sqrt(float(dx[index])))
        del dy

        values: Dict[str, Any] = {}
        for name in product.files:
            if name == "metadata_json":
                continue
            arr = product[name]
            if arr.ndim == 1 and arr.size == lat.size:
                value = arr[index]
                if isinstance(value, np.generic):
                    value = value.item()
                values[name] = value

        metadata: Dict[str, Any] = {}
        if "metadata_json" in product:
            metadata = json.loads(str(product["metadata_json"]))

    return {
        "index": index,
        "query": {"latitude": float(latitude), "longitude": float(longitude)},
        "target": {"latitude": float(lat[index]), "longitude": float(lon[index])},
        "query_distance_m": query_distance_m,
        "values": values,
        "transmitter": metadata.get("transmitter"),
        "receiver": (metadata.get("summary") or {}).get("receiver"),
        "target_model": (metadata.get("summary") or {}).get("target"),
        "motion": (metadata.get("summary") or {}).get("motion"),
        "processing": (metadata.get("summary") or {}).get("processing"),
        "return_path": (metadata.get("summary") or {}).get("return_path"),
    }
