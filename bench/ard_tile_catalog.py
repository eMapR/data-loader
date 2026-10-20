#!/usr/bin/env python
"""Index of which DataLoader dataset holds each ARD tile, across archives,
and which tiles each study area needs: shared imagery without duplicate
files.

Each tile is acquired once, into whichever tile-archive dataset first
needs it (Oregon, a study area's own dataset, ...). Study areas never own
imagery; they are AOIs that resolve to tiles (aois/ard_tile_plan.json),
and tiles resolve to datasets through this catalog. Datasets are only
treated as interchangeable for a tile when their product spec matches
(product family, encoding, bands, QA band, masking, cloud filter), so a
pipeline never mixes, say, a masked float32 copy with the native archive.

    python bench/ard_tile_catalog.py /vol/v1/data [more roots...] \
        --plan aois/ard_tile_plan.json --out /vol/v1/data/ard_tile_catalog.json

Reads only manifest.json files (no imagery, no network). Exit code 1 if a
study area in --plan needs a tile no dataset holds.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def find_manifests(root: Path, depth: int = 3):
    if (root / "manifest.json").exists():
        yield root / "manifest.json"
        return
    if depth == 0:
        return
    for p in sorted(root.iterdir()) if root.is_dir() else []:
        if p.is_dir() and not p.name.startswith("."):
            yield from find_manifests(p, depth - 1)


def product_spec(m: dict, sensor: str) -> dict:
    req, out, flt = m.get("request", {}), m.get("request", {}).get("output", {}), m.get("request", {}).get("filters", {})
    return {"provider": req.get("provider"), "productFamily": m.get("products", {}).get(sensor, {}).get("productFamily"),
            "encoding": out.get("encoding"), "bands": out.get("bands"), "qaBand": out.get("qa_band"),
            "pixelCloudMask": flt.get("pixel_cloud_mask"), "maxCloudPercent": flt.get("max_cloud_percent"),
            "temporalMode": req.get("temporal_mode")}


def tile_key(info: dict, grid_id: str) -> str:
    """Region-qualified key for every tile, CONUS included (`CU_h003v004`),
    so the catalog itself never relies on the bare-means-CONUS shorthand."""
    t = info.get("tile") or {}
    region = t.get("region", "CU")
    return f"{region}_h{t['h']:03d}v{t['v']:03d}" if "h" in t else grid_id


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("roots", nargs="+")
    ap.add_argument("--plan", help="aois/ard_tile_plan.json: resolve each study area's tiles")
    ap.add_argument("--out")
    args = ap.parse_args()

    tiles: dict[str, list] = {}
    datasets = []
    for root in args.roots:
        for mp in find_manifests(Path(root)):
            m = json.loads(mp.read_text())
            if m.get("schema") != "dataloader-manifest":
                continue
            ds = {"path": str(mp.parent), "schemaVersion": m.get("schemaVersion"),
                  "updated": m.get("dataset", {}).get("updated"), "complete": m.get("dataset", {}).get("complete"),
                  "coverage": {k: m.get("coverage", {}).get(k) for k in ("discoveredThrough", "firstAcquired",
                                                                         "lastAcquired", "counts")}}
            datasets.append(ds)
            for sensor, grids in (m.get("grids") or {}).items():
                for grid_id, info in grids.items():
                    if info.get("kind") != "tile":
                        continue
                    tiles.setdefault(tile_key(info, grid_id), []).append(
                        {"dataset": ds["path"], "sensor": sensor, "grid": grid_id,
                         "product": product_spec(m, sensor)})

    catalog = {"schema": "ard-tile-catalog", "version": 1, "roots": args.roots,
               "datasets": datasets, "tiles": dict(sorted(tiles.items()))}
    missing_any = False
    if args.plan:
        plan = json.loads(Path(args.plan).read_text())
        areas = {}
        for a in plan["aois"]:
            need = [f"{t['region']}_{t['tile']}" for t in a["tiles"]]
            held = {k: [h["dataset"] for h in tiles.get(k, [])] for k in need}
            missing = [k for k, v in held.items() if not v]
            missing_any |= bool(missing)
            areas[a["aoi_id"]] = {"tiles": held, "missing": missing}
            print(f"{a['aoi_id']:<30} {len(need) - len(missing)}/{len(need)} tiles held"
                  + (f"; missing {', '.join(missing)}" if missing else ""))
        catalog["study_areas"] = areas
    shared = {k: v for k, v in tiles.items() if len({h["dataset"] for h in v}) > 1}
    print(f"{len(datasets)} datasets, {len(tiles)} tiles, {len(shared)} held by more than one dataset")
    if args.out:
        Path(args.out).write_text(json.dumps(catalog, indent=1) + "\n")
        print(f"wrote {args.out}")
    return 1 if missing_any else 0


if __name__ == "__main__":
    sys.exit(main())
