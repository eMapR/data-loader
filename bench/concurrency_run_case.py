#!/usr/bin/env python
"""Runs the actual scene-level concurrency implementation
(data_loader.engine._run_scene_mode -- not a synthetic harness) once, at a
given `workers` count, over a fixed, representative large-Landsat workload.

Design note (why search and the read window are decoupled here): an
earlier version of this script passed one shared AOI bbox to
data_loader.engine.run() for both scene discovery AND the output grid. At
a ~19M-pixel window that bbox is wide enough to intersect several
neighboring WRS-2 path/rows, so search_scenes silently returned dozens of
scenes instead of the intended handful -- an uncontrolled, much larger
workload than sized for, which is what triggered an OOM kill on this
machine. Fixed by searching a small bbox to get a fixed, known scene list,
then reading each of those scenes at a large, independently-sized window
via data_loader.engine._run_scene_mode directly (engine.py's actual
concurrency dispatcher) -- the workload size is now fully controlled by
this script, not an accident of search geometry.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import resource
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(BENCH_DIR))

from data_loader.aoi import auto_utm_epsg, target_grid
from data_loader.config import AOI, Config, DateRange, Filters, OutputSpec, SensorSpec
from data_loader.engine import _processing_metadata, _resolve_contract, _run_scene_mode
from data_loader.providers import get_provider
from data_loader.providers.base import Grid

from timing import Stopwatch, peak_rss_bytes

COMMON_BANDS = ("blue", "green", "red", "nir", "swir1", "swir2")

# Small search bbox (path044/row029 area) -- returns a fixed, known set of
# real low/moderate-cloud Landsat scenes across summer 2023 (verified live
# 2026-09-16: 16 scenes). NOT used for the read window -- see module
# docstring for why those must stay decoupled.
SEARCH_BBOX = (-119.95, 44.46, -119.65, 44.72)
SEARCH_START = date(2023, 6, 1)
SEARCH_END = date(2023, 9, 15)
MAX_CLOUD_PERCENT = 30
NUM_SCENES = 8  # first N (by date) of the scenes search finds -- fixed workload size across all worker counts

# ~5M-pixel window (matches the Phase 2 large-window validation's ~5M size
# class directly, for comparability; deliberately smaller than the ~10M/
# ~20M windows also validated there -- keeps this benchmark's total
# wall-clock/memory footprint modest across 4 separate worker-count runs
# on a memory-constrained machine, while still exercising real large-COG
# reads well above the old ~2M baseline), centered on the same scene
# geometry centroid used there (see bench/large_window_cases.py).
WINDOW_BBOX = (-120.21816250863378, 44.29565352570772, -119.37183749136622, 44.90234647429227)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--result-file", required=True)
    ap.add_argument("--num-scenes", type=int, default=NUM_SCENES)
    args = ap.parse_args()
    num_scenes = args.num_scenes

    config = Config(
        aoi=AOI(upper_left=(WINDOW_BBOX[0], WINDOW_BBOX[3]), lower_right=(WINDOW_BBOX[2], WINDOW_BBOX[1])),
        provider="planetary_computer",
        sensors=[SensorSpec(name="landsat")],
        date_range=DateRange(start=SEARCH_START, end=SEARCH_END),
        filters=Filters(max_cloud_percent=MAX_CLOUD_PERCENT, pixel_cloud_mask=True),
        temporal_mode="scene", reduce="median",
        output=OutputSpec(
            bands=COMMON_BANDS, indices=("ndvi", "nbr"),
            dir=args.output_dir, write_files=True,
        ),
        workers=args.workers,
    )

    captured = io.StringIO()
    errors = []
    cpu_before = resource.getrusage(resource.RUSAGE_SELF)
    with Stopwatch() as sw:
        try:
            with contextlib.redirect_stdout(captured):
                provider = get_provider(config.provider)
                refs = provider.search_scenes(
                    SEARCH_BBOX, "landsat", SEARCH_START, SEARCH_END, None, None,
                    MAX_CLOUD_PERCENT, "any",
                )
                refs = sorted(refs, key=lambda r: r.date)[:num_scenes]

                sensor_spec = config.sensors[0]
                contract = _resolve_contract(provider, "landsat", sensor_spec)
                res = sensor_spec.resolution()
                target_epsg = auto_utm_epsg(config.aoi.bbox)
                transform, w, h = target_grid(config.aoi.bbox, target_epsg, res)
                grid = Grid(crs=target_epsg, transform=transform, width=w, height=h)
                processing = _processing_metadata(provider, "landsat", config, contract, target_epsg, res)
                product = asdict(contract)
                output_dir = Path(config.output.dir)
                sensor_dir = output_dir / "landsat"
                raw_bands_needed = sorted(set(COMMON_BANDS) | {"red", "nir", "swir2"})  # ndvi/nbr inputs already in COMMON_BANDS

                scenes_out, manifest_entries, failed = _run_scene_mode(
                    provider, config, "landsat", sensor_dir, refs, raw_bands_needed, grid,
                    output_dir, product, processing, snapshot_cache={},
                )
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")
    cpu_after = resource.getrusage(resource.RUSAGE_SELF)
    wall_s = sw.seconds

    stdout_text = captured.getvalue()
    print(stdout_text, end="")

    scenes_ok = len(scenes_out) if not errors else 0
    n_failed = len(failed) if not errors else None
    pixels_per_scene = w * h
    band_pixels_per_scene = pixels_per_scene * 7  # 6 reflectance + QA

    cpu_s = (cpu_after.ru_utime + cpu_after.ru_stime) - (cpu_before.ru_utime + cpu_before.ru_stime)

    out = {
        "workers": args.workers,
        "num_scenes_requested": num_scenes,
        "wall_s": wall_s,
        "cpu_s": cpu_s,
        "cpu_utilization_pct": (100.0 * cpu_s / wall_s) if wall_s else None,
        "scenes_read_ok": scenes_ok,
        "scenes_failed": n_failed,
        "grid_width": w, "grid_height": h,
        "pixels_per_scene": pixels_per_scene,
        "band_pixels_per_scene": band_pixels_per_scene,
        "total_pixels": pixels_per_scene * scenes_ok,
        "total_band_pixels": band_pixels_per_scene * scenes_ok,
        "scenes_per_hour": (scenes_ok / wall_s * 3600) if wall_s and scenes_ok else None,
        "pixels_per_hour": (pixels_per_scene * scenes_ok / wall_s * 3600) if wall_s and scenes_ok else None,
        "band_pixels_per_hour": (band_pixels_per_scene * scenes_ok / wall_s * 3600) if wall_s and scenes_ok else None,
        "network_retries_observed": (
            stdout_text.count("STAC search retry") + stdout_text.count("re-signing and retrying")
        ),
        "peak_rss_bytes": peak_rss_bytes(),
        "errors": errors,
    }
    Path(args.result_file).write_text(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
