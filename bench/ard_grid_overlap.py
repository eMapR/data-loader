#!/usr/bin/env python
"""Where do the USGS ARD regional grids (CONUS, Alaska, Hawaii) overlap on
the ground, and does USGS actually publish tiles there on both grids?

Answers what an AOI near a grid boundary should be told is "missing": the
library names missing tiles from every grid of a product, so a tile that
exists on paper on two grids could be suggested twice, or a grid tile USGS
never publishes could be suggested at all.

    python bench/ard_grid_overlap.py [--out bench/results/ard_grid_overlap.json]

Geometry is computed from data_loader.tiles (no network); publication is
checked with one LandsatLook STAC search per candidate tile (public, no
M2M account needed).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from shapely.geometry import box, shape
from shapely.ops import transform as shp_transform

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from data_loader.providers.usgs_ard import COLLECTION, STAC_URL  # noqa: E402
from data_loader.tiles import ARD_REGIONS, ARD_TILE_M, tile_id_for  # noqa: E402


def footprints(code: str) -> dict:
    """tile id -> lon/lat footprint for every h/v in the region's published range
    (tiles that wrap across the antimeridian are left out)."""
    from pyproj import Transformer

    r = ARD_REGIONS[code]
    tr = Transformer.from_crs(r.crs, "EPSG:4326", always_xy=True)
    out = {}
    for h in range(r.max_h + 1):
        for v in range(r.max_v + 1):
            x0, y1 = r.ulx + h * ARD_TILE_M, r.uly - v * ARD_TILE_M
            g = shp_transform(tr.transform, box(x0, y1 - ARD_TILE_M, x0 + ARD_TILE_M, y1).segmentize(7500))
            w, _, e, _ = g.bounds
            if e - w < 180:
                out[tile_id_for(h, v, code)] = (code, h, v, g)
    return out


def published(client, code: str, h: int, v: int) -> int:
    """Number of SR items USGS has for this tile (capped at 1: exists or not)."""
    s = client.search(collections=[COLLECTION], max_items=1, query={
        "landsat:grid_region": {"eq": code}, "landsat:grid_horizontal": {"eq": f"{h:02d}"},
        "landsat:grid_vertical": {"eq": f"{v:02d}"}})
    return sum(1 for _ in s.items())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(REPO / "bench" / "results" / "ard_grid_overlap.json"))
    ap.add_argument("--aoi", action="append", default=[], help="vector files to test (repeat)")
    a = ap.parse_args()
    from pystac_client import Client
    from shapely.strtree import STRtree

    fp = {c: footprints(c) for c in ARD_REGIONS}
    client = Client.open(STAC_URL)
    cache: dict = {}

    def pub(tid):
        if tid not in cache:
            c, h, v, _ = next(x for x in fp.values() if tid in x)[tid]
            cache[tid] = published(client, c, h, v)
        return cache[tid]

    result = {"pairs": {}, "aois": {}}
    codes = sorted(ARD_REGIONS)
    for i, a_code in enumerate(codes):
        for b_code in codes[i + 1:]:
            b_ids = list(fp[b_code])
            tree = STRtree([fp[b_code][t][3] for t in b_ids])
            pairs = []
            for ta, (_, _, _, ga) in fp[a_code].items():
                for j in tree.query(ga, predicate="intersects"):
                    tb = b_ids[j]
                    inter = ga.intersection(fp[b_code][tb][3])
                    if inter.area > 0:
                        pairs.append((ta, tb))
            a_tiles, b_tiles = sorted({p[0] for p in pairs}), sorted({p[1] for p in pairs})
            pa = {t: pub(t) for t in a_tiles}
            pb = {t: pub(t) for t in b_tiles}
            both = [(ta, tb) for ta, tb in pairs if pa[ta] and pb[tb]]
            result["pairs"][f"{a_code}-{b_code}"] = {
                "overlapping_tile_pairs_on_paper": len(pairs),
                f"{a_code}_tiles_in_overlap": len(a_tiles), f"{a_code}_published": sum(map(bool, pa.values())),
                f"{b_code}_tiles_in_overlap": len(b_tiles), f"{b_code}_published": sum(map(bool, pb.values())),
                "pairs_published_on_both_grids": both,
                f"{a_code}_published_tiles": sorted(t for t, n in pa.items() if n),
                f"{b_code}_published_tiles": sorted(t for t, n in pb.items() if n),
            }
            print(f"{a_code}-{b_code}: {len(pairs)} tile pairs overlap on paper; "
                  f"{a_code} {sum(map(bool, pa.values()))}/{len(a_tiles)} published, "
                  f"{b_code} {sum(map(bool, pb.values()))}/{len(b_tiles)} published; "
                  f"published on both grids: {len(both)}")
            for ta, tb in both[:20]:
                print(f"    {ta} <-> {tb}")

    import geopandas as gpd

    for path in a.aoi:
        g = gpd.read_file(path).to_crs(4326).geometry.union_all()
        row = {}
        for code in codes:
            hits = [t for t, (_, _, _, f) in fp[code].items() if f.intersects(g) and f.intersection(g).area > 0]
            row[code] = {"on_paper": len(hits), "published": sorted(t for t in hits if pub(t))}
        result["aois"][Path(path).name] = row
        print(f"{Path(path).name}: " + "; ".join(f"{c} {row[c]['on_paper']} on paper / "
                                                  f"{len(row[c]['published'])} published" for c in codes))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(result, indent=1))
    print(f"STAC searches: {len(cache)}; wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
