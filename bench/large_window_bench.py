#!/usr/bin/env python
"""Orchestrator for the large-COG-window validation benchmark -- see
large_window_cases.py for the case matrix and rationale. Runs each
(case, repetition) as its own large_window_run_case.py subprocess (so peak
RSS never leaks between window sizes) and appends results as JSONL to
bench/results/large_window/.

Usage:
    python bench/large_window_bench.py
    python bench/large_window_bench.py --only primary_35M
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
sys.path.insert(0, str(BENCH_DIR))

from large_window_cases import CASES


def run_one(case: dict, repetition: int) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        case_file = Path(tmp) / "case.json"
        result_file = Path(tmp) / "result.json"
        case_file.write_text(json.dumps(case, default=str))

        proc = subprocess.run(
            [sys.executable, str(BENCH_DIR / "large_window_run_case.py"),
             "--case-file", str(case_file), "--result-file", str(result_file)],
            capture_output=True, text=True,
        )
        if result_file.exists():
            result = json.loads(result_file.read_text())
        else:
            result = {
                **case,
                "errors": [f"no result file (exit code {proc.returncode})", (proc.stderr or "")[-4000:]],
            }
        result["repetition"] = repetition
        result["subprocess_returncode"] = proc.returncode
        result["subprocess_stderr_tail"] = (proc.stderr or "")[-2000:]
        return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", default=None, help="window_name to include (repeatable)")
    ap.add_argument("--results-dir", default=str(BENCH_DIR / "results" / "large_window"))
    args = ap.parse_args()

    cases = CASES if not args.only else [c for c in CASES if c["window_name"] in args.only]
    if args.only:
        missing = set(args.only) - {c["window_name"] for c in cases}
        if missing:
            raise SystemExit(f"Unknown window_name(s): {sorted(missing)}")

    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = results_dir / f"large_window_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl"

    tasks = [(case, rep) for case in cases for rep in range(1, case["repetitions"] + 1)]
    print(f"Running {len(tasks)} large-window read(s) -> {jsonl_path}")

    with open(jsonl_path, "a") as out:
        for i, (case, rep) in enumerate(tasks, 1):
            print(f"[{i}/{len(tasks)}] {case['window_name']} rep{rep} "
                  f"(target_pixels={case['target_pixels']:,})...", end=" ", flush=True)
            result = run_one(case, rep)
            out.write(json.dumps(result, default=str) + "\n")
            out.flush()
            status = "ok" if not result.get("errors") else "ERRORS"
            print(f"{status} read_total_s={result.get('read_total_s')}")

    print(f"\nDone. Results: {jsonl_path}")


if __name__ == "__main__":
    main()
