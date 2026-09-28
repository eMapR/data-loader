#!/usr/bin/env python
"""Runs exactly one large-COG-window read (one entry of
large_window_cases.CASES, one repetition) as a standalone subprocess, so
peak RSS never leaks between window sizes. See large_window_cases.py for
why these specific windows/scenes were chosen.

Measures, for six reflectance bands (blue/green/red/nir/swir1/swir2) + the
QA band, through Planetary Computer: total read time, per-band read time,
target pixels, band-pixels, seconds/M-pixel, seconds/M-band-pixel, valid
pixel fraction, and peak RSS. Reuses data_loader.providers.get_provider /
Provider.read_scene_bands directly -- nothing in data_loader/ is modified
for this benchmark.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(BENCH_DIR))

import numpy as np

from data_loader.aoi import auto_utm_epsg, target_grid
from data_loader.providers import get_provider
from data_loader.providers.base import Grid

from timing import Stopwatch, peak_rss_bytes
from large_window_cases import COMMON_BANDS


def run_case(case: dict) -> dict:
    errors: list[str] = []
    metrics = {"init_s": None, "search_s": None, "read_total_s": None, "per_band_s": {}}
    total_sw = Stopwatch()
    total_sw.__enter__()

    window_bbox = tuple(case["window_bbox"])
    target_epsg = auto_utm_epsg(window_bbox)
    transform, w, h = target_grid(window_bbox, target_epsg, 30.0)
    grid = Grid(crs=target_epsg, transform=transform, width=w, height=h)
    target_pixels_actual = w * h
    band_pixels_actual = target_pixels_actual * (len(COMMON_BANDS) + 1)  # +1 for QA

    scenes_read_ok = 0
    scenes_failed = 0
    valid_pixel_fraction = None
    peak_rss_after_bytes = None
    # stac_common.StacProvider prints a diagnostic line on every search
    # retry and every re-sign-and-retry (expired-URL) attempt -- capturing
    # stdout during the measured operations and counting those lines is a
    # non-invasive way to record retries without instrumenting
    # data_loader/ itself for this benchmark.
    captured_stdout = io.StringIO()

    try:
        ctx = contextlib.redirect_stdout(captured_stdout)
        ctx.__enter__()
        with Stopwatch() as sw_init:
            provider = get_provider("planetary_computer")
        metrics["init_s"] = sw_init.seconds

        d = date.fromisoformat(case["date"])
        with Stopwatch() as sw_search:
            refs = provider.search_scenes(
                tuple(case["search_bbox"]), "landsat",
                d - timedelta(days=1), d + timedelta(days=1),
                None, None, 100, "any",
            )
        metrics["search_s"] = sw_search.seconds

        scene = next((r for r in refs if r.id == case["scene_id"]), None)
        if scene is None:
            raise RuntimeError(
                f"scene {case['scene_id']!r} not found in a live search over "
                f"{case['search_bbox']} on {d} -- {[r.id for r in refs]}"
            )

        # Per-band timing: each canonical reflectance band read alone
        # (pixel_cloud_mask=False so no QA read is folded into a band's
        # own timing), then the QA band alone (bands=[] + mask=True reads
        # only qa_pixel -- see stac_common.read_scene_bands). This costs
        # 7 extra network reads beyond the single combined read below, an
        # accepted, deliberate overhead for this diagnostic benchmark.
        for b in COMMON_BANDS:
            with Stopwatch() as sw_b:
                provider.read_scene_bands(scene, "landsat", [b], grid, pixel_cloud_mask=False)
            metrics["per_band_s"][b] = sw_b.seconds
        with Stopwatch() as sw_qa:
            provider.read_scene_bands(scene, "landsat", [], grid, pixel_cloud_mask=True)
        metrics["per_band_s"]["qa"] = sw_qa.seconds

        # The actual measurement of record: one combined read of all 6
        # reflectance bands + QA mask, matching real DataLoader usage
        # (engine.py's raw_bands_needed + pixel_cloud_mask=True).
        with Stopwatch() as sw_read:
            band_dict = provider.read_scene_bands(
                scene, "landsat", list(COMMON_BANDS), grid, pixel_cloud_mask=True
            )
        metrics["read_total_s"] = sw_read.seconds
        scenes_read_ok = 1
        peak_rss_after_bytes = peak_rss_bytes()
        fracs = [float(np.mean(~np.isnan(arr))) for arr in band_dict.values()]
        valid_pixel_fraction = float(np.mean(fracs)) if fracs else None

    except Exception as e:
        scenes_failed = 1
        errors.append(f"{type(e).__name__}: {e}")
        errors.append(traceback.format_exc())
    finally:
        ctx.__exit__(None, None, None)

    captured = captured_stdout.getvalue()
    if captured:
        print(captured, end="")  # still surface it in this process's own stdout
    network_retries_observed = (
        captured.count("STAC search retry") + captured.count("re-signing and retrying")
    )

    total_sw.__exit__(None, None, None)
    read_total_s = metrics["read_total_s"]
    seconds_per_million_pixels = (
        (read_total_s / (target_pixels_actual / 1e6)) if read_total_s is not None else None
    )
    seconds_per_million_band_pixels = (
        (read_total_s / (band_pixels_actual / 1e6)) if read_total_s is not None else None
    )

    return {
        "window_name": case["window_name"],
        "scene_id": case["scene_id"],
        "target_pixels_requested": case["target_pixels"],
        "target_pixels_actual": target_pixels_actual,
        "band_pixels_actual": band_pixels_actual,
        "grid_width": w, "grid_height": h, "target_epsg": target_epsg,
        **metrics,
        "seconds_per_million_pixels": seconds_per_million_pixels,
        "seconds_per_million_band_pixels": seconds_per_million_band_pixels,
        "scenes_read_ok": scenes_read_ok,
        "scenes_failed": scenes_failed,
        "network_retries_observed": network_retries_observed,
        "valid_pixel_fraction": valid_pixel_fraction,
        "peak_rss_after_read_bytes": peak_rss_after_bytes,
        "peak_rss_final_bytes": peak_rss_bytes(),
        "total_s": total_sw.seconds,
        "errors": errors,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case-file", required=True)
    ap.add_argument("--result-file", required=True)
    args = ap.parse_args()

    case = json.loads(Path(args.case_file).read_text())
    result = run_case(case)
    Path(args.result_file).write_text(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
