#!/usr/bin/env python
"""Build DataLoader study-area AOIs from an AOI spec (aois/aois.yaml).

For each entry: read the source layer (read-only), select features with
`where` (attribute == value), repair invalid geometry (shapely make_valid,
polygonal parts only), dissolve to one (Multi)Polygon, check the feature
count and area against `expect`, and write

    <spec dir>/<id>.geojson   EPSG:4326, one feature, provenance properties
    <spec dir>/registry.json  per-AOI source path + sha256 of every sidecar,
                              source CRS, area, bbox, validity and checks

Needs geopandas (e.g. the prithvi-ref-assessment conda env):
    python bench/prepare_aois.py aois/aois.yaml
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import shapely
import yaml

EQUAL_AREA = "EPSG:5070"  # CONUS Albers; Alaska entries use EPSG:3338
AREA_TOL = 0.01


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def polygonal(geom):
    """make_valid can return a GeometryCollection; keep only polygon parts."""
    geom = shapely.make_valid(geom)
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    parts = [g for g in getattr(geom, "geoms", []) if g.geom_type in ("Polygon", "MultiPolygon")]
    return shapely.union_all(parts)


def build(entry: dict, root: Path, out_dir: Path) -> dict:
    src = root / entry["source"]
    gdf = gpd.read_file(src)
    sel = gdf
    for k, v in entry["where"].items():
        sel = sel[sel[k].astype(str) == str(v)]
    n_invalid = int((~sel.is_valid).sum())
    geom = shapely.union_all([polygonal(g) for g in sel.geometry])
    one = gpd.GeoDataFrame(geometry=[geom], crs=sel.crs)
    lonlat = one.to_crs(4326)
    lon_c = lonlat.geometry.iloc[0].centroid.x
    area_crs = "EPSG:3338" if lon_c < -129.0 else EQUAL_AREA
    area = float(one.to_crs(area_crs).area.iloc[0] / 1e6)

    exp = entry.get("expect", {})
    checks = {
        "features_selected": len(sel),
        "features_ok": len(sel) == exp.get("features", len(sel)),
        "area_ok": abs(area - exp["area_km2"]) / exp["area_km2"] <= AREA_TOL if "area_km2" in exp else None,
        "invalid_source_features": n_invalid,
        "output_valid": bool(lonlat.is_valid.iloc[0]),
    }
    if not (checks["features_ok"] and checks["area_ok"] is not False and checks["output_valid"]):
        raise SystemExit(f"{entry['id']}: checks failed: {checks}")

    sidecars = sorted(p for p in src.parent.glob(src.stem + ".*") if p.is_file())
    props = {"aoi_id": entry["id"], "name": entry["name"], "program": entry.get("program"),
             "source": entry["source"], "where": entry["where"], "area_km2": round(area, 1)}
    out = out_dir / f"{entry['id']}.geojson"
    lonlat.assign(**{k: [json.dumps(v) if isinstance(v, dict) else v] for k, v in props.items()}) \
        .to_file(out, driver="GeoJSON", COORDINATE_PRECISION=7)
    return {
        "id": entry["id"], "name": entry["name"], "program": entry.get("program"),
        "file": out.name, "file_sha256": sha256(out),
        "source": {"path": str(src), "where": entry["where"],
                   "crs": sel.crs.to_string(), "crs_name": sel.crs.name,
                   "sha256": {p.name: sha256(p) for p in sidecars}},
        "geometry": {"type": geom.geom_type, "parts": len(getattr(geom, "geoms", [geom])),
                     "area_km2": round(area, 1), "area_crs": area_crs,
                     "bbox_lonlat": [round(x, 6) for x in lonlat.total_bounds]},
        "checks": checks,
        "note": entry.get("note"),
    }


def main(spec_path: str):
    spec_path = Path(spec_path)
    spec = yaml.safe_load(spec_path.read_text())
    root, out_dir = Path(spec["source_root"]), spec_path.parent
    rows = [build(e, root, out_dir) for e in spec["aois"]]
    registry = {"spec": spec_path.name, "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "builder": "bench/prepare_aois.py", "crs": "EPSG:4326", "aois": rows}
    (out_dir / "registry.json").write_text(json.dumps(registry, indent=1) + "\n")
    for r in rows:
        g, c = r["geometry"], r["checks"]
        print(f"{r['id']:<30} {g['type']:<12} parts={g['parts']:<3} {g['area_km2']:>9.1f} km2  "
              f"src={r['source']['crs']}  invalid_src={c['invalid_source_features']}")
    print(f"wrote {len(rows)} AOIs + registry.json to {out_dir}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "aois/aois.yaml")
