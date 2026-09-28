#!/usr/bin/env python
"""Benchmark orchestrator. Expands cases.py's static matrix into
(case, repetition) tasks, shuffles the run order (so no single provider
consistently benefits from provider-side CDN/cache warm-up by always running
last in a block), and runs each task as its own `run_case.py` subprocess --
so peak RSS and any provider-level global state never leak between runs.

Usage:
    python bench/run_benchmark.py --only pc_landsat_small_1month --repetitions 1
    python bench/run_benchmark.py --repetitions 3            # full matrix

Nothing here imports data_loader directly -- only run_case.py (a separate
process per case) does. Results are appended as JSONL to bench/results/.
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
sys.path.insert(0, str(BENCH_DIR))

from cases import CASES
from env_info import collect_env_metadata


def build_tasks(cases, repetitions, seed):
    tasks = [(case, rep) for case in cases for rep in range(1, repetitions + 1)]
    random.Random(seed).shuffle(tasks)
    return tasks


def run_one(case: dict, repetition: int, results_dir: Path) -> dict:
    case_run = dict(case)
    case_run["repetition"] = repetition
    case_run["cache_state"] = "cold"  # no warm-run support yet -- see bench design notes
    case_run["output_dir"] = str(results_dir / "output" / case["case_name"] / f"rep{repetition}")

    with tempfile.TemporaryDirectory() as tmp:
        case_file = Path(tmp) / "case.json"
        result_file = Path(tmp) / "result.json"
        case_file.write_text(json.dumps(case_run, default=str))

        proc = subprocess.run(
            [sys.executable, str(BENCH_DIR / "run_case.py"),
             "--case-file", str(case_file), "--result-file", str(result_file)],
            capture_output=True, text=True,
        )

        if result_file.exists():
            result = json.loads(result_file.read_text())
        else:
            result = {
                **case_run,
                "errors": [
                    f"run_case.py produced no result file (exit code {proc.returncode})",
                    (proc.stderr or "")[-4000:],
                ],
            }
        result["subprocess_returncode"] = proc.returncode
        result["subprocess_stderr_tail"] = (proc.stderr or "")[-2000:]
        return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repetitions", type=int, default=3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--only", action="append", default=None,
                     help="case_name to include (repeatable); default: all cases")
    ap.add_argument("--results-dir", default=str(BENCH_DIR / "results"))
    args = ap.parse_args()

    cases = CASES if not args.only else [c for c in CASES if c["case_name"] in args.only]
    if args.only:
        missing = set(args.only) - {c["case_name"] for c in cases}
        if missing:
            raise SystemExit(f"Unknown case name(s): {sorted(missing)}")

    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = results_dir / f"benchmark_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl"

    env_meta = collect_env_metadata(REPO_ROOT)
    tasks = build_tasks(cases, args.repetitions, args.seed)

    print(f"Running {len(tasks)} task(s) ({len(cases)} case(s) x {args.repetitions} rep(s)), "
          f"order seed={args.seed} -> {jsonl_path}")

    with open(jsonl_path, "a") as out:
        for i, (case, rep) in enumerate(tasks, 1):
            print(f"[{i}/{len(tasks)}] {case['case_name']} rep{rep} "
                  f"(provider={case['provider']})...", end=" ", flush=True)
            result = run_one(case, rep, results_dir)
            record = {"env": env_meta, "provider_order_seed": args.seed, **result}
            out.write(json.dumps(record, default=str) + "\n")
            out.flush()
            status = "ok" if not result.get("errors") else "ERRORS"
            print(f"{status} total_s={result.get('total_s')}")

    print(f"\nDone. Results: {jsonl_path}")


if __name__ == "__main__":
    main()
