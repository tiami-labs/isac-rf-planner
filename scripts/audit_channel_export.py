#!/usr/bin/env python3
"""Audit a channel-analysis NPZ for settings, layer mappings, and aligned arrays."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

JSON_MEMBERS = {
    "metadata_json",
    "settings_json",
    "summary_json",
    "array_manifest_json",
    "coverage_layers_json",
    "schema_json",
}


def _json_member(product: Any, name: str, default: Any) -> Any:
    if name not in product.files:
        return default
    return json.loads(str(product[name]))


def audit(path: Path) -> dict[str, Any]:
    with np.load(path, allow_pickle=False) as product:
        members = list(product.files)
        data_members = [name for name in members if name not in JSON_MEMBERS and not name.endswith("_json")]
        metadata = _json_member(product, "metadata_json", {})
        settings = _json_member(product, "settings_json", metadata.get("rf_config", {}))
        summary = _json_member(product, "summary_json", metadata.get("summary", {}))
        manifest = _json_member(product, "array_manifest_json", {})
        coverage_layers = _json_member(product, "coverage_layers_json", metadata.get("coverage_layers", {}))

        point_count = int(product["latitude_deg"].size) if "latitude_deg" in members else 0
        misaligned = {}
        for name in data_members:
            arr = product[name]
            if arr.ndim == 1 and point_count and arr.size != point_count:
                misaligned[name] = {"size": int(arr.size), "expected": point_count}

        layer_checks = {}
        for layer_id, layer in coverage_layers.items():
            numeric_member = layer.get("numeric_member") or layer.get("array")
            layer_checks[layer_id] = {
                "numeric_member": numeric_member,
                "numeric_present": bool(numeric_member and numeric_member in members),
                "display_units": layer.get("units") or layer.get("display_unit"),
                "numeric_units": layer.get("numeric_units") or manifest.get(numeric_member, {}).get("unit"),
            }

        required_missing = sorted(JSON_MEMBERS - set(members))
        layer_numeric_missing = sorted(
            layer_id for layer_id, check in layer_checks.items() if not check["numeric_present"]
        )
        complete = not required_missing and not misaligned and not layer_numeric_missing
        return {
            "file": str(path.resolve()),
            "size_bytes": path.stat().st_size,
            "schema_version": metadata.get("schema_version") or _json_member(product, "schema_json", {}).get("schema_version"),
            "point_count": point_count,
            "members": members,
            "data_members": data_members,
            "settings_exported": bool(settings),
            "setting_keys": sorted(settings.keys()) if isinstance(settings, dict) else [],
            "summary_exported": bool(summary),
            "coverage_layers": layer_checks,
            "required_json_members_missing": required_missing,
            "layer_numeric_members_missing": layer_numeric_missing,
            "misaligned_arrays": misaligned,
            "complete_for_current_export_contract": complete,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("npz", type=Path)
    parser.add_argument("--json", action="store_true", help="Print JSON only")
    args = parser.parse_args()
    report = audit(args.npz)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["complete_for_current_export_contract"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
