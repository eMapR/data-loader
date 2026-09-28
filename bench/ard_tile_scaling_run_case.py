#!/usr/bin/env python
"""Runs the discovery + read-sample measurement for exactly ONE tier, as a
standalone subprocess. See ard_tile_scaling_bench.py for the full design
rationale.

Subprocess isolation here is not optional bookkeeping: an early version of
this benchmark ran all four tiers sequentially inside one long-lived
Python process and found near-zero ("0.01s") read times for scenes that
recurred across tiers -- confirmed to be GDAL's /vsicurl block cache
serving a scene's bytes from the previous tier's read rather than a fresh
network fetch (verified by inspecting the actual scene ids sampled per
tier: the same displayId showed ~3s on first read, ~0.01s on a later
tier's "read" of the identical scene). Each tier therefore gets its own
process, exactly like every other benchmark in this directory
(run_case.py, large_window_run_case.py, concurrency_run_case.py) --
established for the same reason (no provider-level global state, and here
specifically no GDAL curl cache, leaks between measurements).
"""
from __future__ import annotations

import argparse
import json
import resource
import subprocess
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Optional

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

import numpy as np

from data_loader.aoi import auto_utm_epsg, target_grid
from data_loader.providers import get_provider
from data_loader.providers.base import Grid

BANDS = ["blue", "green", "red", "nir", "swir1", "swir2"]
START, END = date(2023, 6, 1), date(2023, 9, 15)
MAX_CLOUD = 30

H_ORIGIN, V_ORIGIN, TILE_SIZE_M = -2565585, 3314805, 150_000

TIERS = {
    "1tile": [(3, 4)],
    "2tile": [(3, 4), (4, 4)],
    "4tile": [(3, 4), (4, 4), (3, 5), (4, 5)],
    "8tile": [(2, 4), (3, 4), (4, 4), (5, 4), (2, 5), (3, 5), (4, 5), (5, 5)],
}

READ_WINDOW_BBOX = (-122.4327, 44.23148, -122.34258, 44.28595)


def _tile_union_bbox_wgs84(tiles, margin_frac: float = 0.05):
    import pyproj

    aea = pyproj.CRS.from_proj4(
        "+proj=aea +lat_1=29.5 +lat_2=45.5 +lat_0=23 +lon_0=-96 "
        "+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
    )
    to_wgs = pyproj.Transformer.from_crs(aea, "EPSG:4326", always_xy=True)

    hs, vs = [t[0] for t in tiles], [t[1] for t in tiles]
    h0, h1, v0, v1 = min(hs), max(hs), min(vs), max(vs)
    ulx = H_ORIGIN + h0 * TILE_SIZE_M
    uly = V_ORIGIN - v0 * TILE_SIZE_M
    lrx = H_ORIGIN + (h1 + 1) * TILE_SIZE_M
    lry = V_ORIGIN - (v1 + 1) * TILE_SIZE_M
    m = TILE_SIZE_M * margin_frac
    ulx, uly, lrx, lry = ulx - m, uly + m, lrx + m, lry - m
    pts = [(ulx, uly), (lrx, uly), (lrx, lry), (ulx, lry),
           ((ulx + lrx) / 2, uly), ((ulx + lrx) / 2, lry),
           (ulx, (uly + lry) / 2), (lrx, (uly + lry) / 2)]
    wgs = [to_wgs.transform(x, y) for x, y in pts]
    lons, lats = [p[0] for p in wgs], [p[1] for p in wgs]
    return (min(lons), min(lats), max(lons), max(lats))


def _peak_rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return (rss if sys.platform == "darwin" else rss * 1024) / 1e6


def _net_bytes() -> Optional[int]:
    try:
        out = subprocess.run(["netstat", "-ib"], capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return None
        total, seen = 0, set()
        for line in out.stdout.splitlines()[1:]:
            f = line.split()
            if len(f) >= 10 and f[0] not in seen and f[2].startswith("<Link"):
                seen.add(f[0])
                total += int(f[6])
        return total or None
    except Exception:
        return None


def read_sample(provider_name: str, refs: list, n: int) -> dict:
    provider = get_provider(provider_name)
    bbox = READ_WINDOW_BBOX
    target_epsg = auto_utm_epsg(bbox)
    transform, w, h = target_grid(bbox, target_epsg, 30.0)
    grid = Grid(crs=target_epsg, transform=transform, width=w, height=h)
    pixels = w * h
    band_pixels = pixels * (len(BANDS) + 1)

    sample = sorted(refs, key=lambda r: (r.cloud_percent if r.cloud_percent is not None else 999))[:n]
    reads, failures = [], 0
    net0 = _net_bytes()
    for ref in sample:
        t0 = time.perf_counter()
        try:
            out = provider.read_scene_bands(ref, "landsat", BANDS, grid, pixel_cloud_mask=True)
            dt = time.perf_counter() - t0
            valid = float(np.mean([np.mean(~np.isnan(a)) for a in out.values()]))
            reads.append({"id": ref.id, "read_s": dt, "valid_fraction": valid})
        except Exception as e:
            failures += 1
            reads.append({"id": ref.id, "error": f"{type(e).__name__}: {e}"})
    net1 = _net_bytes()
    net_mb = round((net1 - net0) / 1e6, 2) if net0 is not None and net1 is not None else None

    ok = [r for r in reads if "read_s" in r]
    mean_s = float(np.mean([r["read_s"] for r in ok])) if ok else None
    return {
        "provider": provider_name,
        "sampled_units": len(sample),
        "grid": f"{w}x{h}",
        "pixels": pixels,
        "band_pixels": band_pixels,
        "mean_read_s": mean_s,
        "median_read_s": float(np.median([r["read_s"] for r in ok])) if ok else None,
        "s_per_million_pixels": (mean_s / (pixels / 1e6)) if mean_s else None,
        "s_per_million_band_pixels": (mean_s / (band_pixels / 1e6)) if mean_s else None,
        "net_mb_indicative": net_mb,
        "mb_per_unit_indicative": round(net_mb / max(len(ok), 1), 2) if net_mb is not None else None,
        "failures": failures,
        "mean_valid_fraction": float(np.mean([r["valid_fraction"] for r in ok])) if ok else None,
        "reads": reads,
    }


def run_tier(tier_name: str, sample_units: int) -> dict:
    tiles = TIERS[tier_name]
    bbox = _tile_union_bbox_wgs84(tiles)
    target = {(f"{h:02d}", f"{v:02d}") for h, v in tiles}

    ard = get_provider("usgs_ard")
    t0 = time.perf_counter()
    ard_refs_raw = ard.search_scenes(bbox, "landsat", START, END, None, None, MAX_CLOUD, "any")
    ard_discovery_s = time.perf_counter() - t0
    ard_refs = [
        r for r in ard_refs_raw
        if (r.provenance.extra.get("grid_horizontal"), r.provenance.extra.get("grid_vertical")) in target
    ]
    found_tiles = {
        (r.provenance.extra.get("grid_horizontal"), r.provenance.extra.get("grid_vertical")) for r in ard_refs
    }
    scene_counts = [r.provenance.extra.get("scene_count") for r in ard_refs if r.provenance.extra.get("scene_count")]

    pc = get_provider("planetary_computer")
    t0 = time.perf_counter()
    pc_refs = pc.search_scenes(bbox, "landsat", START, END, None, None, MAX_CLOUD, "any")
    pc_discovery_s = time.perf_counter() - t0
    pc_dates = Counter(r.date for r in pc_refs)
    pc_dupes = sum(v - 1 for v in pc_dates.values() if v > 1)

    ard_read = read_sample("usgs_ard", ard_refs, sample_units)
    pc_read = read_sample("planetary_computer", pc_refs, sample_units)

    ard_units, pc_units = len(ard_refs), len(pc_refs)
    ard_total_projected = ard_read["mean_read_s"] * ard_units + ard_discovery_s
    pc_total_projected = pc_read["mean_read_s"] * pc_units + pc_discovery_s

    return {
        "tier": tier_name,
        "num_tiles": len(tiles),
        "target_tiles": sorted(tiles),
        "bbox": bbox,
        "ard": {
            "units": ard_units,
            "distinct_tiles_present": sorted(found_tiles),
            "discovery_s": ard_discovery_s,
            "mean_wrs_scene_count_per_tile_date": float(np.mean(scene_counts)) if scene_counts else None,
            "total_wrs_scene_equivalents": int(sum(scene_counts)) if scene_counts else None,
        },
        "pc": {
            "units": pc_units,
            "distinct_dates": len(pc_dates),
            "same_date_duplicates": pc_dupes,
            "discovery_s": pc_discovery_s,
        },
        "ard_read_sample": ard_read,
        "pc_read_sample": pc_read,
        "projected_total_s": {"usgs_ard": ard_total_projected, "planetary_computer": pc_total_projected},
        "projected_winner": "usgs_ard" if ard_total_projected < pc_total_projected else "planetary_computer",
        "projected_ratio": max(ard_total_projected, pc_total_projected) / min(ard_total_projected, pc_total_projected),
        "peak_rss_mb": _peak_rss_mb(),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", required=True, choices=list(TIERS))
    ap.add_argument("--sample-units", type=int, default=5)
    ap.add_argument("--result-file", required=True)
    args = ap.parse_args()

    result = run_tier(args.tier, args.sample_units)
    Path(args.result_file).write_text(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
