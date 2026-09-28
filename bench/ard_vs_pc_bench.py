#!/usr/bin/env python
"""Head-to-head benchmark: direct-USGS ARD (provider `usgs_ard`, M2M signed
URLs -- no AWS) vs. Planetary Computer (provider `planetary_computer`) over
the same AOI/time window.

Deliberately measures the two providers on the SAME target grid and the
same requested bands, so the comparison isolates transport/access cost.
Note the workloads are not one-for-one comparable in unit terms: PC returns
per-scene UTM footprints while ARD returns fixed Albers tiles that can each
mosaic 2-3 WRS-2 scenes, so ARD typically yields fewer units covering the
same AOI/time -- that difference is itself part of what is being measured
(see `units` and the per-unit vs. per-AOI figures in the report).

Bytes transferred are measured by a GDAL/curl-level counter
(`rasterio` -> CPL_CURL_VERBOSE is unreliable for this), so instead this
script reports the network bytes GDAL actually pulled via
`rasterio.Env(CPL_DEBUG=...)`-independent means: it wraps reads and diffs
process-level network counters where available, and otherwise reports None
rather than guessing. See `_net_bytes`.

Usage:
    python bench/ard_vs_pc_bench.py --aoi small --reps 2
"""
from __future__ import annotations

import argparse
import json
import resource
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Optional

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(BENCH_DIR))

import numpy as np

from data_loader.aoi import auto_utm_epsg, target_grid
from data_loader.providers import get_provider
from data_loader.providers.base import Grid

BANDS = ["blue", "green", "red", "nir", "swir1", "swir2"]

AOIS = {
    # ~8km x 6km Oregon Coast Range -- the project's standard small AOI.
    "small": (-122.4327, 44.23148, -122.34258, 44.28595),
    # ~40km x 45km, same AOI the Oregon stress test uses as its large stratum.
    "medium": (-122.75, 44.05, -122.25, 44.45),
}

START, END = date(2023, 6, 1), date(2023, 9, 15)
MAX_CLOUD = 30


def _peak_rss_bytes() -> int:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(rss if sys.platform == "darwin" else rss * 1024)


def _net_bytes() -> Optional[int]:
    """System-wide bytes received, read from `netstat -ib` on macOS (no
    psutil dependency in this project). System-wide, so it is only
    meaningful because these benchmark reads are the dominant network
    activity while running -- reported as *indicative*, not exact. Returns
    None on platforms/parse failures rather than fabricating a number."""
    try:
        out = subprocess.run(["netstat", "-ib"], capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return None
        total = 0
        seen = set()
        for line in out.stdout.splitlines()[1:]:
            f = line.split()
            # Per-interface rows repeat for each address family; count each
            # interface once (the <Link#...> row carries the byte totals).
            if len(f) >= 10 and f[0] not in seen and f[2].startswith("<Link"):
                seen.add(f[0])
                total += int(f[6])
        return total or None
    except Exception:
        return None


def bench_provider(provider_name: str, bbox, reps: int, max_units: int) -> dict:
    provider = get_provider(provider_name)

    target_epsg = auto_utm_epsg(bbox)
    transform, w, h = target_grid(bbox, target_epsg, 30.0)
    grid = Grid(crs=target_epsg, transform=transform, width=w, height=h)
    pixels = w * h
    band_pixels = pixels * (len(BANDS) + 1)  # + QA

    t0 = time.perf_counter()
    refs = provider.search_scenes(bbox, "landsat", START, END, None, None, MAX_CLOUD, "any")
    discovery_s = time.perf_counter() - t0
    units_discovered = len(refs)

    # Deterministic subset: clearest units first, so both providers are
    # measured on comparably cloud-free data rather than whatever order the
    # catalog returned.
    refs = sorted(refs, key=lambda r: (r.cloud_percent if r.cloud_percent is not None else 999))[:max_units]

    per_unit = []
    failures = 0
    net_before = _net_bytes()
    for rep in range(reps):
        for ref in refs:
            t1 = time.perf_counter()
            try:
                out = provider.read_scene_bands(ref, "landsat", BANDS, grid, pixel_cloud_mask=True)
                dt = time.perf_counter() - t1
                valid = float(np.mean([np.mean(~np.isnan(a)) for a in out.values()]))
                per_unit.append({"id": ref.id, "rep": rep + 1, "read_s": dt, "valid_fraction": valid})
            except Exception as e:
                failures += 1
                per_unit.append({"id": ref.id, "rep": rep + 1, "error": f"{type(e).__name__}: {e}"})
    net_after = _net_bytes()
    net_mb = (
        round((net_after - net_before) / 1e6, 1)
        if net_before is not None and net_after is not None else None
    )

    ok = [u for u in per_unit if "read_s" in u]
    read_times = [u["read_s"] for u in ok]
    mean_read_s = float(np.mean(read_times)) if read_times else None

    return {
        "provider": provider_name,
        "units_discovered": units_discovered,
        "units_read": len(refs),
        "reps": reps,
        "discovery_s": discovery_s,
        "grid": f"{w}x{h}",
        "pixels_per_unit": pixels,
        "band_pixels_per_unit": band_pixels,
        "mean_read_s_per_unit": mean_read_s,
        "median_read_s_per_unit": float(np.median(read_times)) if read_times else None,
        "total_read_s": float(np.sum(read_times)) if read_times else None,
        "s_per_million_pixels": (mean_read_s / (pixels / 1e6)) if mean_read_s else None,
        "s_per_million_band_pixels": (mean_read_s / (band_pixels / 1e6)) if mean_read_s else None,
        "net_mb_received_indicative": net_mb,
        "mb_per_unit_indicative": round(net_mb / max(len(ok), 1), 1) if net_mb is not None else None,
        "failures": failures,
        "peak_rss_mb": round(_peak_rss_bytes() / 1e6, 1),
        "mean_valid_fraction": float(np.mean([u["valid_fraction"] for u in ok])) if ok else None,
        "per_unit": per_unit,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--aoi", choices=sorted(AOIS), default="small")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--max-units", type=int, default=5)
    ap.add_argument("--providers", action="append", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    providers = args.providers or ["usgs_ard", "planetary_computer"]
    bbox = AOIS[args.aoi]

    results = {"aoi": args.aoi, "bbox": bbox, "start": str(START), "end": str(END),
               "max_cloud_percent": MAX_CLOUD, "bands": BANDS, "results": []}

    for name in providers:
        print(f"[{name}] benchmarking (aoi={args.aoi}, reps={args.reps}, max_units={args.max_units})...", flush=True)
        r = bench_provider(name, bbox, args.reps, args.max_units)
        results["results"].append(r)
        print(f"  discovery={r['discovery_s']:.2f}s units={r['units_read']} "
              f"mean_read={r['mean_read_s_per_unit']:.2f}s failures={r['failures']}", flush=True)

    out_path = Path(args.out) if args.out else BENCH_DIR / "results" / "ard" / f"ard_vs_pc_{args.aoi}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
