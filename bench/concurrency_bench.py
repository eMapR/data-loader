#!/usr/bin/env python
"""Orchestrator for the scene-level concurrency scaling benchmark (see
docs/development/DATALOADER_DEVELOPMENT_REPORT.md, "Measured concurrency scaling").

Runs the SAME representative large-Landsat workload (concurrency_run_case.
AOI_BBOX: a ~19M-pixel window -- this project's measured average real
Oregon per-scene intersection -- at path044/row029, 2023-06-01..09-15,
max_cloud_percent=30, giving 8 real scenes) through the full DataLoader
engine at workers=1,2,4,8, one subprocess per worker count.

Usage:
    python bench/concurrency_bench.py
    python bench/concurrency_bench.py --workers 1 --workers 4
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


def run_one(workers: int, results_dir: Path) -> dict:
    output_dir = results_dir / "output" / f"workers_{workers}"
    with tempfile.TemporaryDirectory() as tmp:
        result_file = Path(tmp) / "result.json"
        proc = subprocess.run(
            [sys.executable, str(BENCH_DIR / "concurrency_run_case.py"),
             "--workers", str(workers),
             "--output-dir", str(output_dir),
             "--result-file", str(result_file)],
            capture_output=True, text=True,
        )
        if result_file.exists():
            result = json.loads(result_file.read_text())
        else:
            result = {"workers": workers, "errors": [f"no result file (exit {proc.returncode})", (proc.stderr or "")[-4000:]]}
        result["subprocess_returncode"] = proc.returncode
        result["subprocess_stderr_tail"] = (proc.stderr or "")[-2000:]
        return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, action="append", default=None)
    ap.add_argument("--results-dir", default=str(BENCH_DIR / "results" / "concurrency"))
    args = ap.parse_args()

    worker_counts = args.workers or [1, 2, 4, 8]
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = results_dir / f"concurrency_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl"

    print(f"Running concurrency scaling benchmark for workers={worker_counts} -> {jsonl_path}")
    with open(jsonl_path, "a") as out:
        for w in worker_counts:
            print(f"[workers={w}] running...", end=" ", flush=True)
            result = run_one(w, results_dir)
            out.write(json.dumps(result, default=str) + "\n")
            out.flush()
            status = "ok" if not result.get("errors") else "ERRORS"
            print(f"{status} wall_s={result.get('wall_s')} scenes_read_ok={result.get('scenes_read_ok')}")

    print(f"\nDone. Results: {jsonl_path}")


if __name__ == "__main__":
    main()
