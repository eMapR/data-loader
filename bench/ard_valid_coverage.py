#!/usr/bin/env python
"""Actual valid-data coverage of an AOI by USGS ARD tiles, from QA_PIXEL
pixels rather than generalized STAC footprints.

For each tile of an acquisition config (`aoi.tiles`), every observation is
listed (usgs_ard.search_tile, anonymous STAC). A sample is read: per
sensor family (TM = LT04/LT05, ETM+ = LE07, OLI = LC08/LC09) the
observations with the lowest landsat:fill, then, for up to `--rounds`
refinement rounds, observations whose STAC footprint reaches AOI pixels
still without valid data. Only each observation's QA_PIXEL file is read,
at a coarse overview (`--res` m, nearest), through M2M signed URLs; a
pixel is valid when the QA fill bit (bit 0) is off.

The union of valid pixels is compared with the AOI, split into the part
inside the United States and the part outside it (Natural Earth 10 m
admin-0 polygons), per sensor family and overall. Union-of-sample
coverage is a LOWER bound: a pixel never valid in the sample may be valid
in an unsampled observation. Refinement rounds target exactly those
pixels, so remaining gaps are where no footprint reaches or where the
observations that reach it carry fill there.

Needs USGS_M2M_USERNAME / USGS_M2M_TOKEN. Makes 2 M2M calls per sampled
observation: don't run it alongside an archive run on the same account.

    python bench/ard_valid_coverage.py aois/acquisitions/usfs_r10_northern_se_alaska.yaml \
        aois/usfs_r10_northern_se_alaska.geojson --countries ne_10m_admin_0_countries.geojson

Writes <aoi stem>_valid_coverage.json next to the AOI and, with --raster,
a GeoTIFF of per-pixel valid counts (by family) on the tiles' grid.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

FAMILIES = {"TM": ("LT04", "LT05"), "ETM": ("LE07",), "OLI": ("LC08", "LC09")}


def family(item_id: str) -> str:
    return next(f for f, prefixes in FAMILIES.items() if item_id[:4] in prefixes)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("config")
    ap.add_argument("aoi")
    ap.add_argument("--countries", required=True, help="Natural Earth admin-0 countries (GeoJSON)")
    ap.add_argument("--per-family", type=int, default=6, help="lowest-fill observations per tile and family")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--per-round", type=int, default=8, help="extra observations per tile per round")
    ap.add_argument("--res", type=float, default=240.0)
    ap.add_argument("--raster", help="write per-pixel valid counts here (GeoTIFF)")
    args = ap.parse_args()

    import rasterio
    import shapely
    from pyproj import Transformer
    from rasterio.enums import Resampling
    from rasterio.features import rasterize
    from rasterio.transform import from_origin
    from shapely.geometry import shape
    from shapely.ops import transform as reproject

    from data_loader.config import load_config
    from data_loader.providers.usgs_ard import UsgsArdProvider, _tile_product_id
    from data_loader.tiles import ARD_TILE_M, UsgsArdGrid

    cfg = load_config(args.config)
    grid = UsgsArdGrid()
    tiles = [grid.canonical(t) for t in cfg.aoi.tiles]
    regions = {grid.region(t).code for t in tiles}
    if len(regions) != 1:
        raise SystemExit("all tiles must be on one ARD grid region")
    region = grid.region(tiles[0])

    # One coarse canvas on the region's grid covering every tile.
    b = np.array([grid.bounds(t) for t in tiles])
    x0, y0, x1, y1 = b[:, 0].min(), b[:, 1].min(), b[:, 2].max(), b[:, 3].max()
    res = args.res
    W, H = int(round((x1 - x0) / res)), int(round((y1 - y0) / res))
    T = from_origin(x0, y1, res, res)
    px_per_tile = int(round(ARD_TILE_M / res))

    fwd = Transformer.from_crs("EPSG:4326", region.crs, always_xy=True).transform
    inv = Transformer.from_crs(region.crs, "EPSG:4326", always_xy=True)

    def lonlat_geoms(path, where=None):
        feats = json.loads(Path(path).read_text())["features"]
        return [shape(f["geometry"]) for f in feats if where is None or where(f["properties"])]

    aoi = reproject(fwd, shapely.union_all(lonlat_geoms(args.aoi)))
    aoi_mask = rasterize([aoi], out_shape=(H, W), transform=T, dtype="uint8").astype(bool)
    near = shapely.box(*inv.transform_bounds(x0, y0, x1, y1)).buffer(2)
    usa = [g.intersection(near) for g in lonlat_geoms(args.countries, lambda p: p.get("ADM0_A3") == "USA")]
    usa = reproject(fwd, shapely.union_all([g for g in usa if not g.is_empty]))
    us_mask = rasterize([usa], out_shape=(H, W), transform=T, dtype="uint8").astype(bool)
    outside = aoi_mask & ~us_mask

    counts = {f: np.zeros((H, W), np.uint16) for f in FAMILIES}
    provider = UsgsArdProvider()
    problems = provider.credential_problems(["landsat"])
    if problems:
        raise SystemExit("; ".join(problems))

    def tile_window(t):
        xmin, _, _, ymax = grid.bounds(t)
        return int(round((ymax - y1) / -res)), int(round((xmin - x0) / res))

    def read_qa(ref, t) -> np.ndarray:
        tpid = _tile_product_id(ref.id)
        url = provider._band_url(tpid, "QA_PIXEL", ["QA_PIXEL"])
        with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_MAX_RETRY="4",
                          GDAL_HTTP_RETRY_DELAY="2"):
            with rasterio.open("/vsicurl/" + url) as src:
                qa = src.read(1, out_shape=(px_per_tile, px_per_tile), resampling=Resampling.nearest)
        provider._forget_tile(tpid)
        return (qa & 1) == 0

    log, sampled = [], {}
    today = date.today()
    for t in tiles:
        refs = provider.search_tile(t, "landsat", cfg.time.windows(today)[0].start, today)
        fills = {r.id: float(r.handle.properties.get("landsat:fill") or 0.0) for r in refs}
        by_family = {f: sorted((r for r in refs if family(r.id) == f), key=lambda r: fills[r.id]) for f in FAMILIES}
        chosen = [r for f in FAMILIES for r in by_family[f][:args.per_family]]
        sampled[t] = {"refs": refs, "fills": fills, "done": set()}
        r0, c0 = tile_window(t)
        tile_aoi = aoi_mask[r0:r0 + px_per_tile, c0:c0 + px_per_tile]

        def read_some(batch, why):
            for ref in batch:
                if ref.id in sampled[t]["done"]:
                    continue
                try:
                    valid = read_qa(ref, t)
                except Exception as e:
                    if "AUTH" in str(e) or "login" in str(e).lower():
                        # rejected credentials: stop at once rather than retry
                        # a login per observation
                        raise SystemExit(f"stopping: USGS M2M rejected the credentials ({str(e)[:160]})")
                    log.append({"tile": t, "id": ref.id, "error": str(e)[:200]})
                    continue
                sampled[t]["done"].add(ref.id)
                counts[family(ref.id)][r0:r0 + px_per_tile, c0:c0 + px_per_tile] += valid
                log.append({"tile": t, "id": ref.id, "why": why, "fill": fills[ref.id],
                            "valid_in_aoi_frac": round(float((valid & tile_aoi).sum() / max(tile_aoi.sum(), 1)), 4)})
                print(f"{t} {ref.id} fill={fills[ref.id]:.0f} ({why})", flush=True)

        read_some(chosen, "lowest fill")
        for rnd in range(args.rounds):
            union = sum(c for c in counts.values())[r0:r0 + px_per_tile, c0:c0 + px_per_tile] > 0
            gap = tile_aoi & ~union
            if not gap.any():
                break
            rows, cols = np.nonzero(gap)
            idx = np.linspace(0, len(rows) - 1, min(len(rows), 50)).astype(int)
            xs = x0 + (c0 + cols[idx] + 0.5) * res
            ys = y1 - (r0 + rows[idx] + 0.5) * res
            lons, lats = inv.transform(xs, ys)
            pts = shapely.points(lons, lats)
            scored = []
            for ref in refs:
                if ref.id in sampled[t]["done"]:
                    continue
                geom = provider.source_metadata(ref).get("geometry")
                if geom:
                    n = int(shapely.contains(shape(geom).buffer(0), pts).sum())
                    if n:
                        scored.append((n, -fills[ref.id], ref))
            scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
            if not scored:
                break
            read_some([s[2] for s in scored[:args.per_round]], f"refine {rnd + 1}: footprint reaches gap")

    failed = [r for r in log if "error" in r]
    if failed or any(not s["done"] for s in sampled.values()):
        raise SystemExit(f"not writing a result: {len(failed)} QA_PIXEL read(s) failed and/or a tile had no "
                         f"successful read (first error: {failed[0]['error'] if failed else 'none'}); "
                         "coverage from a partial sample would understate it")
    union = sum(c for c in counts.values()) > 0
    km2 = res * res / 1e6

    def frac(mask, sel):
        return round(float((mask & sel).sum() / max(sel.sum(), 1)), 4)

    gap = aoi_mask & ~union
    gap_info = None
    if gap.any():
        rows, cols = np.nonzero(gap)
        xs, ys = x0 + (cols + 0.5) * res, y1 - (rows + 0.5) * res
        lons, lats = inv.transform(xs, ys)
        gap_info = {"area_km2": round(float(gap.sum() * km2), 1),
                    "outside_us_km2": round(float((gap & outside).sum() * km2), 1),
                    "bbox_lonlat": [round(float(v), 3) for v in (min(lons), min(lats), max(lons), max(lats))]}
    out = {
        "config": args.config, "aoi": args.aoi, "grid_region": region.code, "tiles": tiles,
        "resolution_m": res, "built": today.isoformat(),
        "method": "union of QA_PIXEL fill-bit-off pixels over a sample of observations (lower bound)",
        "aoi_km2": round(float(aoi_mask.sum() * km2), 1),
        "aoi_outside_us_km2": round(float(outside.sum() * km2), 1),
        "valid_frac": {"aoi": frac(union, aoi_mask), "aoi_in_us": frac(union, aoi_mask & us_mask),
                       "aoi_outside_us": frac(union, outside)},
        "valid_frac_by_family": {f: {"aoi": frac(c > 0, aoi_mask), "aoi_in_us": frac(c > 0, aoi_mask & us_mask),
                                     "aoi_outside_us": frac(c > 0, outside)} for f, c in counts.items()},
        "never_valid_in_sample": gap_info,
        "sampled": {t: {"observations": len(s["refs"]), "read": len(s["done"])} for t, s in sampled.items()},
        "reads": log,
    }
    dest = Path(args.aoi).with_name(Path(args.aoi).stem + "_valid_coverage.json")
    dest.write_text(json.dumps(out, indent=1) + "\n")
    if args.raster:
        with rasterio.open(args.raster, "w", driver="GTiff", width=W, height=H, count=len(FAMILIES) + 2,
                           dtype="uint16", crs=region.crs, transform=T, compress="deflate") as dst:
            for i, f in enumerate(FAMILIES, 1):
                dst.write(counts[f], i)
                dst.set_band_description(i, f"valid_count_{f}")
            dst.write(aoi_mask.astype("uint16"), len(FAMILIES) + 1)
            dst.set_band_description(len(FAMILIES) + 1, "aoi")
            dst.write(outside.astype("uint16"), len(FAMILIES) + 2)
            dst.set_band_description(len(FAMILIES) + 2, "aoi_outside_us")
    print(json.dumps({k: out[k] for k in ("aoi_km2", "aoi_outside_us_km2", "valid_frac", "valid_frac_by_family",
                                          "never_valid_in_sample", "sampled")}, indent=1))
    print(f"wrote {dest}")


if __name__ == "__main__":
    main()
