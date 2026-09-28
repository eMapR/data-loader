#!/usr/bin/env python
"""Orchestrator for the complete end-to-end LandTrendr-ready pipeline
comparison: dispatches landtrendr_ready_run_case.py as its own subprocess
per provider (GDAL /vsicurl cache isolation -- see that module's
docstring), then verifies the two outputs are scientifically comparable
by directly diffing pixel values on shared acquisition dates.

Usage:
    python bench/landtrendr_ready_bench.py
    python bench/landtrendr_ready_bench.py --providers usgs_ard
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
PROVIDERS = ["usgs_ard", "planetary_computer"]


PHASE_KEYS = ["discovery_s", "acquisition_s", "mosaic_s", "indices_s", "write_s"]


def run_one(provider: str, results_dir: Path, aoi_preset: str = "2tile", max_attempts: int = 30,
            sample_dates: int | None = None) -> dict:
    """Invokes landtrendr_ready_run_case.py repeatedly against the SAME
    --output-dir until it reports no dates remaining, accumulating each
    attempt's phase timers (each attempt's timers cover only the dates it
    newly processed -- see that script's resumability docstring). This
    project's benchmarks have repeatedly been killed mid-run by external
    system memory pressure unrelated to their own modest footprint; rather
    than lose all progress on a kill, each attempt resumes from whatever
    the previous one had already written to disk. `discovery_s` is real
    work repeated on every attempt (a fresh search_scenes call, cheap --
    single-digit seconds) and is summed like the others, so the reported
    total honestly includes that repeated cost rather than hiding it.

    The accumulator itself is ALSO persisted to disk
    (`<output_dir>/bench_progress.json`, updated after every attempt), not
    just held in this function's local variables -- if THIS orchestrator
    process is itself killed mid-run (observed live: the external memory-
    pressure supervisor kills the whole backgrounded shell, orchestrator
    included, not just the subprocess it launched), re-running this script
    resumes both the underlying pipeline's file-level progress AND the
    accumulated cross-invocation timing, rather than only recovering the
    former and silently losing precise phase-time accounting for whatever
    was completed before the kill."""
    output_dir = results_dir / "output" / provider
    output_dir.mkdir(parents=True, exist_ok=True)
    progress_path = output_dir / "bench_progress.json"

    accumulated_timers = {k: 0.0 for k in PHASE_KEYS}
    total_wall_s = total_cpu_s = total_net_mb = total_retries = 0.0
    peak_rss_mb = 0.0
    attempts_so_far = 0
    if progress_path.exists():
        try:
            saved = json.loads(progress_path.read_text())
            accumulated_timers = saved["timers"]
            total_wall_s, total_cpu_s = saved["wall_s"], saved["cpu_s"]
            total_net_mb, total_retries = saved["net_mb_indicative"], saved["retries_observed"]
            peak_rss_mb, attempts_so_far = saved["peak_rss_mb"], saved["attempts"]
            print(f"    resuming orchestrator progress from a prior interrupted run "
                  f"({attempts_so_far} attempt(s) already recorded)", flush=True)
        except Exception:
            pass

    def _save_progress():
        progress_path.write_text(json.dumps({
            "timers": accumulated_timers, "wall_s": total_wall_s, "cpu_s": total_cpu_s,
            "net_mb_indicative": total_net_mb, "retries_observed": total_retries,
            "peak_rss_mb": peak_rss_mb, "attempts": attempts_so_far,
        }, indent=2, default=str))

    last_result = None
    for _ in range(max_attempts):
        attempts_so_far += 1
        with tempfile.TemporaryDirectory() as tmp:
            result_file = Path(tmp) / "result.json"
            proc = subprocess.run(
                [sys.executable, str(BENCH_DIR / "landtrendr_ready_run_case.py"),
                 "--provider", provider, "--aoi-preset", aoi_preset,
                 "--output-dir", str(output_dir),
                 "--result-file", str(result_file)]
                + (["--sample-dates", str(sample_dates)] if sample_dates else []),
                capture_output=True, text=True,
            )
            if not result_file.exists():
                print(f"    [attempt {attempts_so_far}] no result file (exit {proc.returncode}, likely killed) "
                      f"-- retrying, resuming from disk state", flush=True)
                _save_progress()
                continue
            result = json.loads(result_file.read_text())
            for k in PHASE_KEYS:
                accumulated_timers[k] += result["timers"][k]
            total_wall_s += result["wall_s"]
            total_cpu_s += result["cpu_s"]
            total_net_mb += result.get("net_mb_indicative") or 0.0
            total_retries += result.get("retries_observed") or 0
            peak_rss_mb = max(peak_rss_mb, result.get("peak_rss_mb") or 0.0)
            last_result = result
            _save_progress()
            remaining = result["dates_remaining"]
            print(f"    [attempt {attempts_so_far}] processed {result['dates_processed_this_invocation']} date(s) "
                  f"this attempt, {remaining} remaining", flush=True)
            if remaining <= 0:
                break
    else:
        print(f"    WARNING: {provider} did not finish within {max_attempts} attempts")

    if last_result is None:
        # Every attempt this call was killed before producing a result --
        # accumulated progress from EARLIER orchestrator invocations (if
        # any) is still safe on disk; report what's known so far rather
        # than nothing.
        return {"provider": provider, "error": "no attempt completed in this invocation",
                "attempts": attempts_so_far, "output_dir": str(output_dir)}

    last_result["timers"] = accumulated_timers
    last_result["wall_s"] = total_wall_s
    last_result["cpu_s"] = total_cpu_s
    last_result["net_mb_indicative"] = round(total_net_mb, 2)
    last_result["retries_observed"] = total_retries
    last_result["peak_rss_mb"] = peak_rss_mb
    last_result["attempts"] = attempts_so_far
    last_result["output_dir"] = str(output_dir)
    return last_result


def compare_shared_dates(results: dict[str, dict], results_dir: Path) -> dict:
    """Loads each provider's per-date bands GeoTIFF for every date BOTH
    produced output for, and computes mean/max-abs-diff/correlation per
    band -- the same technique used to verify PC/ARD scientific agreement
    in the acquisition-only benchmarks, applied here to the actual
    MOSAICKED, MASKED, LandTrendr-ready composite (not a single scene)."""
    import numpy as np
    import rasterio

    if "usgs_ard" not in results or "planetary_computer" not in results:
        return {"note": "both providers must have run for a comparability check"}
    ard_dates = set(results["usgs_ard"].get("output_dates", []))
    pc_dates = set(results["planetary_computer"].get("output_dates", []))
    shared = sorted(ard_dates & pc_dates)

    per_band_diffs: dict[str, list[float]] = {}
    per_date_stats = []
    for d in shared:
        ard_path = Path(results["usgs_ard"]["output_dir"]) / "landsat" / f"bands_{d}.tif"
        pc_path = Path(results["planetary_computer"]["output_dir"]) / "landsat" / f"bands_{d}.tif"
        if not (ard_path.exists() and pc_path.exists()):
            continue
        with rasterio.open(ard_path) as a_src, rasterio.open(pc_path) as p_src:
            a_names = [a_src.descriptions[i] for i in range(a_src.count)]
            p_names = [p_src.descriptions[i] for i in range(p_src.count)]
            a_data = a_src.read()
            p_data = p_src.read()
        date_stats = {"date": d}
        for i, name in enumerate(a_names):
            if name not in p_names:
                continue
            j = p_names.index(name)
            a_band, p_band = a_data[i], p_data[j]
            mask = ~np.isnan(a_band) & ~np.isnan(p_band)
            if mask.sum() < 10:
                continue
            diff = a_band[mask] - p_band[mask]
            corr = float(np.corrcoef(a_band[mask], p_band[mask])[0, 1]) if mask.sum() > 1 else None
            date_stats[name] = {
                "mean_diff": float(np.mean(diff)), "max_abs_diff": float(np.max(np.abs(diff))),
                "correlation": corr, "n_compared_pixels": int(mask.sum()),
            }
            per_band_diffs.setdefault(name, []).append(float(np.mean(diff)))
        per_date_stats.append(date_stats)

    overall = {
        band: {"mean_of_mean_diffs": float(sum(v) / len(v)) if v else None, "n_dates": len(v)}
        for band, v in per_band_diffs.items()
    }
    return {
        "ard_dates": len(ard_dates), "pc_dates": len(pc_dates), "shared_dates": len(shared),
        "shared_date_list": shared, "overall_by_band": overall, "per_date": per_date_stats,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--providers", nargs="+", default=PROVIDERS, choices=PROVIDERS)
    ap.add_argument("--aoi-preset", default="2tile", choices=["2tile", "4tile", "8tile"])
    ap.add_argument("--sample-dates", type=int, default=None,
                    help="smoke-test mode: process only N evenly spaced dates per provider")
    ap.add_argument("--results-dir", default=None)
    args = ap.parse_args()

    # "2tile" keeps the original directory name (already has completed
    # results from the earlier round) for backward compatibility; only
    # "4tile" gets a new, separate directory.
    default_dirname = "landtrendr_ready" if args.aoi_preset == "2tile" else f"landtrendr_ready_{args.aoi_preset}"
    if args.sample_dates:
        default_dirname += f"_smoke{args.sample_dates}"
    results_dir = Path(args.results_dir) if args.results_dir else BENCH_DIR / "results" / default_dirname
    results_dir.mkdir(parents=True, exist_ok=True)

    results = {}
    for provider in args.providers:
        print(f"[{provider}] running complete pipeline (aoi_preset={args.aoi_preset})...", flush=True)
        r = run_one(provider, results_dir, aoi_preset=args.aoi_preset, sample_dates=args.sample_dates)
        results[provider] = r
        if "error" in r:
            print(f"  ERROR: {r['error']}\n{r.get('stderr','')}")
            continue
        t = r["timers"]
        total_phase = sum(t.values())
        print(f"  wall={r['wall_s']:.1f}s  source_units={r['num_source_units']}  "
              f"output_dates={r['num_output_dates']}  failures={len(r['failures'])}  "
              f"retries={r['retries_observed']}")
        print(f"  phases: discovery={t['discovery_s']:.1f}s ({100*t['discovery_s']/total_phase:.1f}%)  "
              f"acquisition={t['acquisition_s']:.1f}s ({100*t['acquisition_s']/total_phase:.1f}%)  "
              f"mosaic={t['mosaic_s']:.1f}s ({100*t['mosaic_s']/total_phase:.1f}%)  "
              f"indices={t['indices_s']:.1f}s ({100*t['indices_s']/total_phase:.1f}%)  "
              f"write={t['write_s']:.1f}s ({100*t['write_s']/total_phase:.1f}%)")
        print(f"  output: {r['output_file_count']} files, {r['output_bytes']/1e6:.1f} MB  "
              f"net_indicative={r['net_mb_indicative']} MB  peak_rss={r['peak_rss_mb']:.0f} MB")

    if len(results) == 2 and all("error" not in r for r in results.values()):
        print("\n[comparability check] diffing shared-date outputs...", flush=True)
        comparability = compare_shared_dates(results, results_dir)
        print(f"  shared dates: {comparability['shared_dates']} "
              f"(ARD {comparability['ard_dates']}, PC {comparability['pc_dates']})")
        for band, stats in comparability.get("overall_by_band", {}).items():
            print(f"  {band}: mean-of-mean-diffs={stats['mean_of_mean_diffs']:.6f} over {stats['n_dates']} dates")
    else:
        comparability = None

    out_path = results_dir / f"landtrendr_ready_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out_path.write_text(json.dumps({"results": results, "comparability": comparability}, indent=2, default=str))
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
