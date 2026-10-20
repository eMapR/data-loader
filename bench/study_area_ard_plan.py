#!/usr/bin/env python
"""USGS ARD tile plan for study-area AOIs: which native ARD tiles each AOI
touches, how many observations each tile has, and what a complete-tile
archive of them would cost -- before downloading anything.

Inputs: aois/registry.json (bench/prepare_aois.py) and the Oregon archive
config (for overlap). Per AOI:

1. The AOI is projected into its ARD grid (CONUS `CU` or Alaska `AK`,
   picked by grid coverage) and intersected with the grid's 150 km tiles.
   Reported per tile: share of the AOI in the tile and share of the tile
   covered by the AOI.
2. Every observation of each tile, 1990-01-01 to today, is listed from the
   public LandsatLook STAC (anonymous; whole-tile bbox + region/h/v filter,
   as usgs_ard.search_tile does), with landsat:fill.
3. Cost, scaled from the measured Oregon archive run (docs/large-jobs.md,
   docs/development/benchmarks/data/oregon_ard_estimate.json): stored and
   downloaded bytes scale with the observation's data share (100 - fill),
   calibrated so Oregon's 23 tiles come to the measured ~8.3 TB stored;
   time uses Oregon's measured 8,300-10,500 observations/day (one M2M
   account, 4 workers).

Grids (projection, origin, h/v range per region) come from
data_loader.tiles.ARD_REGIONS, the same table DataLoader acquires with.

    python bench/study_area_ard_plan.py aois/registry.json examples/oregon_landsat_ard_archive.yaml

Writes aois/ard_tile_plan.json.
"""
from __future__ import annotations

import json
import math
import sys
import time
from datetime import date
from pathlib import Path

import geopandas as gpd
import numpy as np
import requests
import yaml
from shapely.geometry import box

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from data_loader.tiles import ARD_REGIONS, ARD_TILE_M as TILE_M  # noqa: E402

STAC = "https://landsatlook.usgs.gov/stac-server/search"
COLLECTION = "landsat-c2ard-sr"
START = "1990-01-01"
SLIVER_FILL = 80
OREGON_EST = REPO / "docs" / "development" / "benchmarks" / "data" / "oregon_ard_estimate.json"
OREGON_STORED_TB = 8.3                     # measured, docs/large-jobs.md (DataLoader 1.0 run, Oct 2026)
OREGON_OBS_PER_DAY = (8300, 10500)         # measured range, same run


def region_for(lonlat_geom) -> str:
    return "AK" if lonlat_geom.centroid.x < -129.0 and lonlat_geom.centroid.y > 51 else "CU"


def tiles_for(aoi: gpd.GeoDataFrame, region: str) -> list[dict]:
    r = ARD_REGIONS[region]
    crs, ulx, uly = r.crs, r.ulx, r.uly
    g = aoi.to_crs(crs).geometry.iloc[0]
    xmin, ymin, xmax, ymax = g.bounds
    rows = []
    for h in range(math.floor((xmin - ulx) / TILE_M), math.floor((xmax - ulx) / TILE_M) + 1):
        for v in range(math.floor((uly - ymax) / TILE_M), math.floor((uly - ymin) / TILE_M) + 1):
            t = box(ulx + h * TILE_M, uly - (v + 1) * TILE_M, ulx + (h + 1) * TILE_M, uly - v * TILE_M)
            a = g.intersection(t).area
            if a > 0:
                tl = gpd.GeoSeries([t], crs=crs).to_crs(4326).geometry.iloc[0]
                rows.append({"tile": f"h{h:03d}v{v:03d}", "region": region, "h": h, "v": v,
                             "aoi_share": a / g.area, "tile_covered": a / t.area,
                             "bbox": [round(x, 4) for x in tl.bounds]})
    return sorted(rows, key=lambda r: -r["aoi_share"])


def tile_fills(region: str, h: int, v: int, bbox, end: str) -> list[float]:
    body = {"collections": [COLLECTION], "bbox": bbox, "limit": 500,
            "datetime": f"{START}T00:00:00Z/{end}T23:59:59Z",
            "query": {"landsat:grid_region": {"eq": region}, "landsat:grid_horizontal": {"eq": f"{h:02d}"},
                      "landsat:grid_vertical": {"eq": f"{v:02d}"}},
            "fields": {"include": ["id", "properties.landsat:fill"], "exclude": ["assets", "links", "geometry"]}}
    fills, seen = [], set()
    while body:
        for attempt in range(5):
            try:
                r = requests.post(STAC, json=body, timeout=180)
                r.raise_for_status()
                break
            except requests.RequestException:
                if attempt == 4:
                    raise
                time.sleep(5 * (attempt + 1))
        d = r.json()
        for f in d["features"]:
            if f["id"] not in seen:
                seen.add(f["id"])
                fills.append(float(f["properties"].get("landsat:fill") or 0.0))
        nxt = next((l for l in d.get("links", []) if l.get("rel") == "next"), None)
        body = ({**body, **nxt["body"]} if nxt and nxt.get("body") else None) if d["features"] else None
    return fills


def main(registry_path: str, oregon_cfg: str):
    registry = json.loads(Path(registry_path).read_text())
    oregon_tiles = set(yaml.safe_load(Path(oregon_cfg).read_text())["aoi"]["tiles"])
    est = json.loads(OREGON_EST.read_text())
    oregon_de = sum(t["data_equiv"] for t in est["per_tile"])
    stored_gb_per_de = OREGON_STORED_TB * 1e3 / oregon_de
    xfer_gb_per_de = est["all_tiles"]["transfer_tb"] * 1e3 / oregon_de
    end = date.today().isoformat()
    aoi_dir = Path(registry_path).parent

    plans, all_tiles = [], {}
    for entry in registry["aois"]:
        aoi = gpd.read_file(aoi_dir / entry["file"])
        region = region_for(aoi.geometry.iloc[0])
        tiles = tiles_for(aoi, region)
        for t in tiles:
            key = (region, t["tile"])
            if key not in all_tiles:
                fills = tile_fills(region, t["h"], t["v"], t["bbox"], end)
                de = sum(1 - f / 100 for f in fills)
                n = len(fills)
                all_tiles[key] = {"observations": n, "slivers": sum(f >= SLIVER_FILL for f in fills),
                                  "mean_fill": round(float(np.mean(fills)), 1) if fills else None,
                                  "data_equiv": round(de, 1),
                                  "stored_gb": round(de * stored_gb_per_de, 1),
                                  "transfer_gb": round(de * xfer_gb_per_de, 1),
                                  "days": [round(n / OREGON_OBS_PER_DAY[1], 2), round(n / OREGON_OBS_PER_DAY[0], 2)]}
            t.update(all_tiles[key])
            t["in_oregon_archive"] = region == "CU" and t["tile"] in oregon_tiles
            t["aoi_share"], t["tile_covered"] = round(t["aoi_share"], 4), round(t["tile_covered"], 4)
            del t["h"], t["v"]
        new = [t for t in tiles if not t["in_oregon_archive"]]

        def tot(ts):
            return {"tiles": len(ts), "observations": sum(t["observations"] for t in ts),
                    "slivers": sum(t["slivers"] for t in ts),
                    "stored_tb": round(sum(t["stored_gb"] for t in ts) / 1e3, 2),
                    "transfer_tb": round(sum(t["transfer_gb"] for t in ts) / 1e3, 2),
                    "days": [round(sum(t["days"][0] for t in ts), 1), round(sum(t["days"][1] for t in ts), 1)]}
        plans.append({"aoi_id": entry["id"], "grid_region": region, "total": tot(tiles),
                      "new_vs_oregon_archive": tot(new), "tiles": tiles})

    uniq = list(all_tiles.values())
    new_uniq = [v for (reg, tid), v in all_tiles.items() if not (reg == "CU" and tid in oregon_tiles)]
    out = {"built": end, "period": [START, end],
           "model": {"basis": "Oregon archive run (DataLoader 1.0, 4 workers, one M2M account, Oct 2026)",
                     "stored_gb_per_data_equiv": round(stored_gb_per_de, 4),
                     "transfer_gb_per_data_equiv": round(xfer_gb_per_de, 4),
                     "obs_per_day": OREGON_OBS_PER_DAY, "sliver_fill_threshold": SLIVER_FILL,
                     "data_equiv": "sum over observations of (100 - landsat:fill)/100"},
           "grids": {k: {"proj4": r.crs, "ul": [r.ulx, r.uly]} for k, r in ARD_REGIONS.items() if k in ("CU", "AK")},
           "all_aois_unique_tiles": {"tiles": len(uniq), "observations": sum(v["observations"] for v in uniq),
                                     "stored_tb": round(sum(v["stored_gb"] for v in uniq) / 1e3, 2),
                                     "transfer_tb": round(sum(v["transfer_gb"] for v in uniq) / 1e3, 2)},
           "all_aois_unique_new_tiles": {"tiles": len(new_uniq),
                                         "observations": sum(v["observations"] for v in new_uniq),
                                         "stored_tb": round(sum(v["stored_gb"] for v in new_uniq) / 1e3, 2),
                                         "transfer_tb": round(sum(v["transfer_gb"] for v in new_uniq) / 1e3, 2),
                                         "days": [round(sum(v["days"][0] for v in new_uniq), 1),
                                                  round(sum(v["days"][1] for v in new_uniq), 1)]},
           "aois": plans}
    (aoi_dir / "ard_tile_plan.json").write_text(json.dumps(out, indent=1) + "\n")

    for p in plans:
        print(f"\n{p['aoi_id']} ({p['grid_region']})")
        print(f"  {'tile':<9}{'aoi%':>6}{'cov%':>6}{'obs':>6}{'sliv':>6}{'fill':>6}{'GB':>6}{'OR':>4}")
        for t in p["tiles"]:
            print(f"  {t['tile']:<9}{100*t['aoi_share']:>6.1f}{100*t['tile_covered']:>6.1f}{t['observations']:>6}"
                  f"{t['slivers']:>6}{t['mean_fill'] or 0:>6.0f}{t['stored_gb']:>6.0f}{'Y' if t['in_oregon_archive'] else '':>4}")
        a, n = p["total"], p["new_vs_oregon_archive"]
        print(f"  total {a['tiles']} tiles {a['observations']} obs {a['stored_tb']} TB stored {a['transfer_tb']} TB xfer "
              f"{a['days'][0]}-{a['days'][1]} d | new vs Oregon: {n['tiles']} tiles {n['observations']} obs {n['stored_tb']} TB "
              f"{n['days'][0]}-{n['days'][1]} d")
    print("\nall AOIs, unique tiles:", out["all_aois_unique_tiles"])
    print("all AOIs, unique tiles not in Oregon archive:", out["all_aois_unique_new_tiles"])


if __name__ == "__main__":
    main(*(sys.argv[1:] or ["aois/registry.json", "examples/oregon_landsat_ard_archive.yaml"]))
