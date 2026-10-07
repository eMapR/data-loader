#!/usr/bin/env python
"""Orchestrator: benchmarks scene-level concurrency (data_loader.engine's
config.workers) for BOTH usgs_ard and planetary_computer over the same
representative 4-tile Oregon workload, at 1/2/4/8 workers each. Dispatches
each (provider, workers) combination as its own subprocess
(ard_pc_concurrency_run_case.py) -- see that module's docstring for why
process isolation matters here (GDAL /vsicurl cache leakage across runs,
discovered the hard way in the tile-boundary scaling benchmark).

Answers, from measured data (see docs/development/DATALOADER_DEVELOPMENT_REPORT.md,
"ARD vs PC concurrency scaling"):
- Does concurrency close the ~3-4x per-unit gap between ARD and PC found
  in the serial tile-boundary benchmark, or does a fixed cost remain?
- What is each provider's own scaling efficiency relative to its 1-worker
  baseline?
- Are outputs scientifically identical regardless of worker count (band
  means + a byte-for-byte file checksum on a fixed canary unit)?

Usage:
    python bench/ard_pc_concurrency_bench.py
    python bench/ard_pc_concurrency_bench.py --providers usgs_ard --workers 1 4
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
WORKER_COUNTS = [1, 2, 4, 8]
PROVIDERS = ["usgs_ard", "planetary_computer"]


def run_one(provider: str, workers: int, num_units: int, results_dir: Path) -> dict:
    output_dir = results_dir / "output" / f"{provider}_workers_{workers}"
    with tempfile.TemporaryDirectory() as tmp:
        result_file = Path(tmp) / "result.json"
        proc = subprocess.run(
            [sys.executable, str(BENCH_DIR / "ard_pc_concurrency_run_case.py"),
             "--provider", provider, "--workers", str(workers),
             "--num-units", str(num_units),
             "--output-dir", str(output_dir), "--result-file", str(result_file)],
            capture_output=True, text=True,
        )
        if result_file.exists():
            result = json.loads(result_file.read_text())
        else:
            result = {"provider": provider, "workers": workers,
                      "error": f"no result file (exit {proc.returncode})",
                      "stderr": (proc.stderr or "")[-4000:]}
        result["subprocess_returncode"] = proc.returncode
        return result


def analyze(results: list[dict]) -> dict:
    by_provider: dict[str, dict[int, dict]] = {}
    for r in results:
        if "error" in r:
            continue
        by_provider.setdefault(r["provider"], {})[r["workers"]] = r

    summary = {}
    for provider, by_workers in by_provider.items():
        if 1 not in by_workers:
            continue
        base_wall = by_workers[1]["wall_s"]
        rows = []
        for w in WORKER_COUNTS:
            if w not in by_workers:
                continue
            r = by_workers[w]
            speedup = base_wall / r["wall_s"] if r["wall_s"] else None
            efficiency = speedup / w if speedup else None
            rows.append({
                "workers": w, "wall_s": r["wall_s"], "speedup": speedup, "efficiency": efficiency,
                "units_per_hour": r["units_per_hour"], "mean_s_per_unit": r["mean_s_per_unit"],
                "cpu_utilization_pct": r["cpu_utilization_pct"], "peak_rss_mb": r["peak_rss_mb"],
                "scenes_failed": r["scenes_failed"],
                "network_retries_or_reminting_observed": r["network_retries_or_reminting_observed"],
            })
        summary[provider] = rows

    # Cross-check "scientifically identical output": the canary file's
    # sha256 and band means must match across every worker count for a
    # given provider.
    identity_check = {}
    for provider, by_workers in by_provider.items():
        hashes = {w: r.get("canary_file_sha256") for w, r in by_workers.items()}
        means = {w: r.get("canary_band_means") for w, r in by_workers.items()}
        unique_hashes = set(h for h in hashes.values() if h is not None)
        identity_check[provider] = {
            "hashes_by_workers": hashes,
            "identical_across_workers": len(unique_hashes) <= 1,
            "means_by_workers": means,
        }

    return {"scaling": summary, "output_identity_check": identity_check}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--providers", nargs="+", default=PROVIDERS, choices=PROVIDERS)
    ap.add_argument("--workers", nargs="+", type=int, default=WORKER_COUNTS)
    ap.add_argument("--num-units", type=int, default=16)
    ap.add_argument("--results-dir", default=str(BENCH_DIR / "results" / "concurrency_ard_pc"))
    args = ap.parse_args()

    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for provider in args.providers:
        for workers in args.workers:
            print(f"[{provider} workers={workers}] running (num_units={args.num_units})...", flush=True)
            r = run_one(provider, workers, args.num_units, results_dir)
            results.append(r)
            if "error" in r:
                print(f"  ERROR: {r['error']}\n{r.get('stderr','')}")
                continue
            print(f"  wall={r['wall_s']:.1f}s  ok={r['scenes_read_ok']}  failed={r['scenes_failed']}  "
                  f"units/hr={r['units_per_hour']:.1f}  cpu={r['cpu_utilization_pct']:.1f}%  "
                  f"peak_rss={r['peak_rss_mb']:.0f}MB  retries={r['network_retries_or_reminting_observed']}")

    analysis = analyze(results)
    print("\n=== Scaling efficiency relative to 1-worker baseline ===")
    for provider, rows in analysis["scaling"].items():
        print(f"\n{provider}:")
        for row in rows:
            print(f"  workers={row['workers']:2d}  wall={row['wall_s']:7.1f}s  "
                  f"speedup={row['speedup']:.2f}x  efficiency={row['efficiency']:.1%}  "
                  f"units/hr={row['units_per_hour']:.1f}")

    print("\n=== Output identity check (same units in, same bytes out, regardless of workers) ===")
    for provider, chk in analysis["output_identity_check"].items():
        print(f"{provider}: identical_across_workers={chk['identical_across_workers']}")
        for w, h in chk["hashes_by_workers"].items():
            print(f"    workers={w}: sha256={h}")

    out_path = results_dir / f"concurrency_ard_pc_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out_path.write_text(json.dumps({"num_units": args.num_units, "results": results, "analysis": analysis}, indent=2, default=str))
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
