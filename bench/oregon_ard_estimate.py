#!/usr/bin/env python
"""How long would the full ARD Landsat history for all of Oregon take?

Scales the measured h003v004 tile-history run
(docs/benchmarks/data/ard_tile_history_h003v004.json) to every CONUS ARD
tile that touches Oregon:

1. Oregon's boundary (PublicaMundi us-states GeoJSON, a low-resolution
   generalization -- fine at 150 km tile scale) is projected into the ARD
   Albers grid and rasterized at 1 km to find each tile's Oregon share.
2. Every observation of each tile over the same period is listed from the
   public LandsatLook STAC (no credentials), searching the WHOLE tile
   extent: an ARD item's geometry is its data footprint, so a small-AOI
   search misses edge "slivers" from neighboring WRS-2 paths. (The
   h003v004 run's discovery used a 0.1 deg AOI and so acquired 2,411 of the
   tile's 3,885 observations; the 1,474 it missed are ~92% fill.)
3. Data volume per observation scales with (100 - landsat:fill), using
   the run's own observations as the reference.

Time range (one machine, ONE M2M account, run code as measured):
    high   every observation costs what the run's did (15.9 s effective
           at 4 workers): slivers as expensive as full tiles
    low    time scales with data volume instead, but never faster than
           one serial M2M mint per observation (~4-6 s, serial pilot) --
           M2M allows one request at a time per account
A sliver's real cost is in between: little data, but full minting and
per-file overhead.

    python bench/oregon_ard_estimate.py

Writes docs/benchmarks/data/oregon_ard_estimate.json.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

import numpy as np
import requests

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "bench"))
from ard_tile_history_bench import ARD_TILE_M as TILE_M, ARD_ULX as ULX, ARD_ULY as ULY, tile_bbox_lonlat  # noqa: E402
RUN = REPO / "docs" / "benchmarks" / "data" / "ard_tile_history_h003v004.json"
TILE_DIR = REPO / "bench" / "results" / "ard_tile_history" / "h003v004"
OUT = REPO / "docs" / "benchmarks" / "data" / "oregon_ard_estimate.json"

STATES_URL = "https://raw.githubusercontent.com/PublicaMundi/MappingAPI/master/data/geojson/us-states.json"
STAC_URL = "https://landsatlook.usgs.gov/stac-server"
COLLECTION = "landsat-c2ard-sr"
RES = 1000.0  # rasterization resolution for Oregon share
SLIVER_FILL = 80  # landsat:fill >= this -> counted as a sliver


def oregon_tiles(crs_wkt: str) -> dict[tuple[int, int], dict]:
    """(h, v) -> Oregon share of the tile and the tile's lon/lat bbox."""
    from pyproj import Transformer
    from rasterio.features import rasterize
    from rasterio.transform import from_origin

    states = requests.get(STATES_URL, timeout=60).json()
    geom = next(f["geometry"] for f in states["features"] if f["properties"]["name"] == "Oregon")
    fwd = Transformer.from_crs("EPSG:4326", crs_wkt, always_xy=True)

    def proj(rings):
        return [[list(fwd.transform(x, y)) for x, y in ring] for ring in rings]

    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    pgeom = {"type": "MultiPolygon", "coordinates": [proj(p) for p in polys]}
    xs = [x for p in pgeom["coordinates"] for r in p for x, _ in r]
    ys = [y for p in pgeom["coordinates"] for r in p for _, y in r]
    x0, y1 = min(xs) - RES, max(ys) + RES
    w, h = int((max(xs) - x0) / RES) + 2, int((y1 - min(ys)) / RES) + 2
    mask = rasterize([pgeom], out_shape=(h, w), transform=from_origin(x0, y1, RES, RES), dtype="uint8")

    rows, cols = np.nonzero(mask)
    px = x0 + (cols + 0.5) * RES
    py = y1 - (rows + 0.5) * RES
    th = ((px - ULX) // TILE_M).astype(int)
    tv = ((ULY - py) // TILE_M).astype(int)
    tiles: dict[tuple[int, int], dict] = {}
    per_tile_px = (TILE_M / RES) ** 2
    for key in set(zip(th.tolist(), tv.tolist())):
        sel = (th == key[0]) & (tv == key[1])
        # Whole-tile bbox: an ARD item's geometry is its data footprint, which
        # varies by overpass, so a point/AOI search would miss observations.
        tiles[key] = {"oregon_share": float(sel.sum() / per_tile_px), "bbox": tile_bbox_lonlat(*key)}
    return tiles



def tile_items(client, h: int, v: int, bbox, start: str, end: str) -> list[float]:
    """landsat:fill of every observation of one tile in [start, end]."""
    q = {"landsat:grid_horizontal": {"eq": f"{h:02d}"}, "landsat:grid_vertical": {"eq": f"{v:02d}"},
         "landsat:grid_region": {"eq": "CU"}}
    s = client.search(collections=[COLLECTION], bbox=bbox, datetime=f"{start}/{end}", query=q,
                      limit=500, fields={"include": ["properties.landsat:fill"], "exclude": ["assets", "links"]})
    return [float(it["properties"].get("landsat:fill") or 0.0) for it in s.items_as_dicts()]


def main():
    from pystac_client import Client

    run = json.loads(RUN.read_text())
    start, end = run["period"]
    grid = json.loads((TILE_DIR / "grid.json").read_text())
    serial_pilot = [json.loads(l) for l in open(TILE_DIR / "pilot_vsicurl.jsonl")]
    mint_s = st.mean(r["mint_s"] for r in serial_pilot if r.get("phase") == "serial" and r["mint_s"] > 0)

    # Reference: data volume of the observations the run actually acquired.
    run_items = json.loads((TILE_DIR / "items.json").read_text())
    run_data = sum(1 - (it["properties"].get("landsat:fill") or 0) / 100 for it in run_items)
    gb_per_data, saved_gb_per_data = run["bytes_gb"] / run_data, run["saved_gb"] / run_data
    s_per_obs = 3600 / run["obs_per_hour"]
    s_per_data = run["wall_h"] * 3600 / run_data

    tiles = oregon_tiles(grid["crs_wkt"])
    client = Client.open(STAC_URL)
    rows = []
    for (h, v), t in sorted(tiles.items()):
        fills = tile_items(client, h, v, t["bbox"], start, end)
        data = sum(1 - f / 100 for f in fills)
        rows.append({"tile": f"h{h:03d}v{v:03d}", "oregon_share": round(t["oregon_share"], 3),
                     "observations": len(fills), "slivers": sum(f >= SLIVER_FILL for f in fills),
                     "data_equiv": round(data, 1),
                     "est_transfer_gb": round(data * gb_per_data, 1),
                     "est_saved_gb": round(data * saved_gb_per_data, 1),
                     "hours_high": round(len(fills) * s_per_obs / 3600, 2),
                     "hours_low": round(max(data * s_per_data, len(fills) * mint_s) / 3600, 2)})

    def totals(rs):
        return {"tiles": len(rs), "observations": sum(r["observations"] for r in rs),
                "slivers": sum(r["slivers"] for r in rs),
                "transfer_tb": round(sum(r["est_transfer_gb"] for r in rs) / 1e3, 2),
                "saved_uint16_tb": round(sum(r["est_saved_gb"] for r in rs) / 1e3, 2),
                "hours_low": round(sum(r["hours_low"] for r in rs), 1),
                "hours_high": round(sum(r["hours_high"] for r in rs), 1)}

    summary = {
        "basis": f"{run['tile']} run: {run['acquired']} obs in {run['wall_h']} h ({run['obs_per_hour']} obs/h, 4 workers)",
        "period": [start, end],
        "boundary": "PublicaMundi us-states GeoJSON (generalized), rasterized at 1 km in ARD Albers",
        "model": {"s_per_obs_high": round(s_per_obs, 2), "s_per_full_tile_equiv_low": round(s_per_data, 2),
                  "m2m_mint_floor_s_per_obs": round(mint_s, 2), "sliver_fill_threshold": SLIVER_FILL},
        "all_tiles": totals(rows),
        "tiles_ge_5pct_oregon": totals([r for r in rows if r["oregon_share"] >= 0.05]),
        "per_tile": rows,
    }
    OUT.write_text(json.dumps(summary, indent=1))

    print(f"{'tile':<10}{'OR':>5}{'obs':>6}{'slivers':>8}{'xfer GB':>8}{'saved GB':>9}{'hours':>12}")
    for r in rows:
        print(f"{r['tile']:<10}{r['oregon_share']:>5.2f}{r['observations']:>6}{r['slivers']:>8}"
              f"{r['est_transfer_gb']:>8.0f}{r['est_saved_gb']:>9.0f}{r['hours_low']:>6.1f}-{r['hours_high']:<5.1f}")
    for name in ("all_tiles", "tiles_ge_5pct_oregon"):
        t = summary[name]
        print(f"{name}: {t['tiles']} tiles, {t['observations']} obs ({t['slivers']} slivers), "
              f"~{t['transfer_tb']} TB transfer, ~{t['saved_uint16_tb']} TB saved, "
              f"{t['hours_low']}-{t['hours_high']} h ({t['hours_low'] / 24:.1f}-{t['hours_high'] / 24:.1f} days)")
    print(f"wrote {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
