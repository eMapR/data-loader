#!/usr/bin/env python
"""Orchestrator for the ARD tile-boundary scaling benchmark: dispatches
each AOI tier (1/2/4/8 ARD tiles) as its own subprocess
(ard_tile_scaling_run_case.py) so GDAL's /vsicurl block cache and any
provider-level global state never leak between tiers -- see that module's
docstring for why this matters (an in-process version of this benchmark
measured near-zero "read" times for scenes that recurred across tiers).

Usage:
    python bench/ard_tile_scaling_bench.py
    python bench/ard_tile_scaling_bench.py --tiers 1tile 2tile
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
TIERS = ["1tile", "2tile", "4tile", "8tile"]


def run_one(tier: str, sample_units: int) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        result_file = Path(tmp) / "result.json"
        proc = subprocess.run(
            [sys.executable, str(BENCH_DIR / "ard_tile_scaling_run_case.py"),
             "--tier", tier, "--sample-units", str(sample_units),
             "--result-file", str(result_file)],
            capture_output=True, text=True,
        )
        if result_file.exists():
            result = json.loads(result_file.read_text())
        else:
            result = {"tier": tier, "error": f"no result file (exit {proc.returncode})", "stderr": (proc.stderr or "")[-4000:]}
        result["subprocess_returncode"] = proc.returncode
        return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", nargs="+", default=TIERS, choices=TIERS)
    ap.add_argument("--sample-units", type=int, default=5)
    ap.add_argument("--out", default=str(BENCH_DIR / "results" / "ard" / "ard_tile_scaling.json"))
    args = ap.parse_args()

    results = []
    for tier in args.tiers:
        print(f"[{tier}] running as subprocess...", flush=True)
        r = run_one(tier, args.sample_units)
        results.append(r)
        if "error" in r:
            print(f"  ERROR: {r['error']}\n{r.get('stderr','')}")
            continue
        print(f"  ARD: {r['ard']['units']} units, tiles {r['ard']['distinct_tiles_present']}, "
              f"discovery {r['ard']['discovery_s']:.2f}s, mean_scene_count {r['ard']['mean_wrs_scene_count_per_tile_date']:.2f}")
        print(f"  PC:  {r['pc']['units']} units, {r['pc']['distinct_dates']} dates, "
              f"{r['pc']['same_date_duplicates']} dupes, discovery {r['pc']['discovery_s']:.2f}s")
        print(f"  read: ARD mean={r['ard_read_sample']['mean_read_s']:.2f}s  PC mean={r['pc_read_sample']['mean_read_s']:.2f}s")
        print(f"  projected total: ARD={r['projected_total_s']['usgs_ard']:.1f}s  "
              f"PC={r['projected_total_s']['planetary_computer']:.1f}s  "
              f"winner={r['projected_winner']} ({r['projected_ratio']:.2f}x)")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"sample_units_per_tier": args.sample_units, "results": results}, indent=2, default=str))
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
