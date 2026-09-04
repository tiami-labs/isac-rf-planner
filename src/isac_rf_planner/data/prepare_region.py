"""Build the local SQLite/R*Tree OSM geometry database used by DVT planning.

Supported inputs:
  * ``.osm.pbf`` / ``.pbf`` through the GDAL ``ogr2ogr`` OSM driver.
  * GeoJSON / GeoJSONSeq directly with the Python standard library.
  * GeoPackage through Fiona when installed.

The output contains the original polygon vertices.  It does not simplify or
resample OSM geometry.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

from ..geo.local_osm_provider import LOCAL_OSM_SCHEMA_VERSION
from ..geo.osm_map_provider import _estimate_osm_height_m, _extract_building_material

_OTHER_TAG_RE = re.compile(r'"((?:[^"\\]|\\.)*)"=>"((?:[^"\\]|\\.)*)"')


def _parse_other_tags(value: Any) -> Dict[str, str]:
    if not isinstance(value, str) or not value.strip():
        return {}
    out: Dict[str, str] = {}
    for key, item in _OTHER_TAG_RE.findall(value):
        out[bytes(key, "utf-8").decode("unicode_escape")] = bytes(
            item, "utf-8"
        ).decode("unicode_escape")
    return out


def _tags_from_properties(properties: Mapping[str, Any]) -> Dict[str, Any]:
    tags: Dict[str, Any] = {}
    for key, value in properties.items():
        if key in {"other_tags", "osm_way_id", "osm_id", "osm_type", "id", "fid"}:
            continue
        if value is None or value == "":
            continue
        tags[str(key)] = value
    tags.update(_parse_other_tags(properties.get("other_tags")))
    return tags


def _iter_geojson(path: Path) -> Iterator[Mapping[str, Any]]:
    suffixes = [s.lower() for s in path.suffixes]
    is_sequence = path.suffix.lower() in {".geojsonl", ".jsonl", ".geojsonseq"}
    if is_sequence:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip().lstrip("\x1e")
                if not line:
                    continue
                item = json.loads(line)
                if item.get("type") == "Feature":
                    yield item
        return

    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("type") == "FeatureCollection":
        for feature in payload.get("features") or []:
            if isinstance(feature, Mapping):
                yield feature
    elif payload.get("type") == "Feature":
        yield payload
    else:
        raise ValueError(f"Unsupported GeoJSON root in {path}: {payload.get('type')!r}")


def _iter_gpkg(path: Path) -> Iterator[Mapping[str, Any]]:
    try:
        import fiona
    except ImportError as exc:
        raise RuntimeError(
            "GeoPackage import requires Fiona. Install with: pip install fiona"
        ) from exc

    for layer in fiona.listlayers(path):
        with fiona.open(path, layer=layer) as source:
            for feature in source:
                yield {
                    "type": "Feature",
                    "id": feature.get("id"),
                    "properties": dict(feature.get("properties") or {}),
                    "geometry": dict(feature.get("geometry") or {}),
                }


def _extract_pbf_with_ogr2ogr(path: Path, temp_dir: Path) -> List[Path]:
    executable = shutil.which("ogr2ogr")
    if executable is None:
        raise RuntimeError(
            "PBF import requires the GDAL ogr2ogr executable with the OSM driver. "
            "Install gdal-bin, or convert the PBF to GeoJSON/GeoPackage first."
        )

    output = temp_dir / "multipolygons.geojsonseq"
    command = [
        executable,
        "-f",
        "GeoJSONSeq",
        str(output),
        str(path),
        "multipolygons",
        "-where",
        "building IS NOT NULL OR landuse IS NOT NULL OR natural IS NOT NULL",
        "-skipfailures",
    ]
    proc = subprocess.run(command, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            "ogr2ogr could not extract OSM multipolygons:\n"
            f"command: {' '.join(command)}\n"
            f"stderr: {proc.stderr.strip()}"
        )
    if not output.exists():
        raise RuntimeError("ogr2ogr completed without creating the extracted polygon file")
    return [output]


def _iter_input_features(path: Path, temp_dir: Path) -> Iterator[Mapping[str, Any]]:
    lower_name = path.name.lower()
    if lower_name.endswith(".osm.pbf") or lower_name.endswith(".pbf"):
        for extracted in _extract_pbf_with_ogr2ogr(path, temp_dir):
            yield from _iter_geojson(extracted)
        return
    if path.suffix.lower() == ".gpkg":
        yield from _iter_gpkg(path)
        return
    if path.suffix.lower() in {".geojson", ".json", ".geojsonl", ".jsonl", ".geojsonseq"}:
        yield from _iter_geojson(path)
        return
    raise ValueError(f"Unsupported input format: {path}")


def _polygon_rings(geometry: Mapping[str, Any]) -> Iterator[List[Sequence[float]]]:
    geometry_type = str(geometry.get("type") or "")
    coordinates = geometry.get("coordinates") or []
    if geometry_type == "Polygon":
        if coordinates and coordinates[0]:
            yield list(coordinates[0])
    elif geometry_type == "MultiPolygon":
        for polygon in coordinates:
            if polygon and polygon[0]:
                yield list(polygon[0])


def _ring_to_geometry(ring: Sequence[Sequence[float]]) -> List[Dict[str, float]]:
    geometry: List[Dict[str, float]] = []
    for coordinate in ring:
        if len(coordinate) < 2:
            continue
        lon = float(coordinate[0])
        lat = float(coordinate[1])
        geometry.append({"lat": lat, "lon": lon})
    if len(geometry) >= 3 and geometry[0] != geometry[-1]:
        geometry.append(dict(geometry[0]))
    return geometry


def _bbox(geometry: Sequence[Mapping[str, float]]) -> Tuple[float, float, float, float]:
    lons = [float(point["lon"]) for point in geometry]
    lats = [float(point["lat"]) for point in geometry]
    return min(lons), min(lats), max(lons), max(lats)


def _source_identity(feature: Mapping[str, Any], properties: Mapping[str, Any]) -> Tuple[str, str]:
    source_id = (
        properties.get("osm_id")
        or properties.get("osm_way_id")
        or properties.get("id")
        or feature.get("id")
        or "unknown"
    )
    source_type = str(properties.get("osm_type") or "osm")
    return str(source_id), source_type


def _initialize_database(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE features (
            id INTEGER PRIMARY KEY,
            source_id TEXT NOT NULL,
            source_type TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('building', 'landuse')),
            tags_json TEXT NOT NULL,
            geometry_json TEXT NOT NULL,
            material TEXT,
            height_m REAL,
            min_lon REAL NOT NULL,
            min_lat REAL NOT NULL,
            max_lon REAL NOT NULL,
            max_lat REAL NOT NULL
        );
        CREATE INDEX features_kind_idx ON features(kind);
        CREATE INDEX features_source_idx ON features(source_type, source_id);
        CREATE VIRTUAL TABLE feature_rtree USING rtree(
            id,
            min_lon, max_lon,
            min_lat, max_lat
        );
        """
    )


def build_database(
    input_paths: Sequence[Path],
    output_path: Path,
    *,
    overwrite: bool = False,
) -> Dict[str, int]:
    """Build a local geometry database and return inserted feature counts."""

    if not input_paths:
        raise ValueError("At least one input file is required")
    inputs = [Path(path).expanduser().resolve() for path in input_paths]
    for path in inputs:
        if not path.exists():
            raise FileNotFoundError(path)

    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        if not overwrite:
            raise FileExistsError(f"Output already exists: {output}; pass --overwrite to replace it")
        output.unlink()

    counts = {"building": 0, "landuse": 0, "skipped": 0}
    seen: set[Tuple[str, str, str, int]] = set()
    with tempfile.TemporaryDirectory(prefix="rf-osm-import-") as tmp:
        temp_dir = Path(tmp)
        conn = sqlite3.connect(str(output))
        try:
            _initialize_database(conn)
            next_id = 1
            for input_path in inputs:
                for feature in _iter_input_features(input_path, temp_dir):
                    properties = dict(feature.get("properties") or {})
                    tags = _tags_from_properties(properties)
                    source_id, source_type = _source_identity(feature, properties)
                    geometry_mapping = feature.get("geometry") or {}
                    rings = list(_polygon_rings(geometry_mapping))
                    if not rings:
                        counts["skipped"] += 1
                        continue

                    kinds: List[str] = []
                    if str(tags.get("building") or "").strip():
                        kinds.append("building")
                    if str(tags.get("landuse") or "").strip() or str(tags.get("natural") or "").strip():
                        kinds.append("landuse")
                    if not kinds:
                        counts["skipped"] += 1
                        continue

                    for ring_index, ring in enumerate(rings):
                        geometry = _ring_to_geometry(ring)
                        if len(geometry) < 4:
                            counts["skipped"] += 1
                            continue
                        min_lon, min_lat, max_lon, max_lat = _bbox(geometry)
                        for kind in kinds:
                            dedupe_key = (source_type, source_id, kind, ring_index)
                            if dedupe_key in seen:
                                continue
                            seen.add(dedupe_key)
                            material: Optional[str] = None
                            height_m: Optional[float] = None
                            if kind == "building":
                                material = _extract_building_material({"tags": tags})
                                height_m = _estimate_osm_height_m(tags)
                            conn.execute(
                                """
                                INSERT INTO features(
                                    id, source_id, source_type, kind,
                                    tags_json, geometry_json, material, height_m,
                                    min_lon, min_lat, max_lon, max_lat
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    next_id,
                                    source_id,
                                    source_type,
                                    kind,
                                    json.dumps(tags, separators=(",", ":"), sort_keys=True),
                                    json.dumps(geometry, separators=(",", ":")),
                                    material,
                                    height_m,
                                    min_lon,
                                    min_lat,
                                    max_lon,
                                    max_lat,
                                ),
                            )
                            conn.execute(
                                "INSERT INTO feature_rtree VALUES (?, ?, ?, ?, ?)",
                                (next_id, min_lon, max_lon, min_lat, max_lat),
                            )
                            counts[kind] += 1
                            next_id += 1

            metadata = {
                "schema_version": LOCAL_OSM_SCHEMA_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_files": json.dumps([str(path) for path in inputs]),
                "building_count": str(counts["building"]),
                "landuse_count": str(counts["landuse"]),
                "geometry_policy": "original exterior polygon vertices; no simplification",
            }
            conn.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)", metadata.items()
            )
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            try:
                output.unlink()
            except FileNotFoundError:
                pass
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass

    return counts


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare a local indexed OSM geometry database for DVT planning."
    )
    parser.add_argument(
        "--input",
        dest="inputs",
        type=Path,
        action="append",
        required=True,
        help="Input .osm.pbf, .pbf, .geojson, .geojsonseq, or .gpkg; repeat as needed.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/osm/rf_geometry.sqlite"),
        help="Output SQLite database path.",
    )
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing output database.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    counts = build_database(args.inputs, args.output, overwrite=args.overwrite)
    print(
        f"Prepared {args.output}: "
        f"{counts['building']} buildings, {counts['landuse']} landuse polygons, "
        f"{counts['skipped']} skipped features"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
