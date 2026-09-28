#!/usr/bin/env python
"""Runs data_loader.engine's ACTUAL scene-level concurrency dispatcher
(`engine._run_scene_mode`) for one (provider, workers) combination, as a
standalone subprocess. See ard_pc_concurrency_bench.py for why: an earlier
benchmark in this project found that running multiple worker-count
measurements sequentially inside one long-lived process let GDAL's
/vsicurl block cache silently serve bytes from an earlier run, producing
impossible near-zero read times -- so, as with every other benchmark here,
each measurement gets its own fresh process.

Workload: a FIXED, deterministic list of real Landsat units (ARD
tile-dates or PC scenes) discovered once from the same real 4-tile Oregon
AOI used in the tile-boundary scaling benchmark, sorted by date and sliced
to a fixed count -- identical across every worker count for a given
provider, so "scientifically identical output" can be checked directly
(same units in, and this benchmark verifies the same pixel bytes out).
The actual READ window is a separate, small, fixed bbox nested inside that
AOI (not the AOI's own ~4-tile footprint) -- decoupling search scope from
read-window size is the same fix applied after this project's earlier
concurrency benchmark caused an OOM by accidentally scaling the read
window with a large search bbox.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import resource
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Optional

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

from data_loader.aoi import auto_utm_epsg, target_grid
from data_loader.config import AOI, Config, DateRange, Filters, OutputSpec, SensorSpec
from data_loader.engine import _processing_metadata, _resolve_contract, _run_scene_mode
from data_loader.providers import get_provider
from data_loader.providers.base import Grid

BANDS = ["blue", "green", "red", "nir", "swir1", "swir2"]
START, END = date(2023, 6, 1), date(2023, 9, 15)
MAX_CLOUD = 30
NUM_UNITS = 16

# The "4tile" AOI from the tile-boundary scaling benchmark (h03v04, h04v04,
# h03v05, h04v05) -- used here only for DISCOVERING a real, representative
# candidate pool, never as the read window (see module docstring).
DISCOVERY_BBOX = (-123.2267, 42.02234, -118.41916, 45.47862)
ARD_TARGET_TILES = {("03", "04"), ("04", "04"), ("03", "05"), ("04", "05")}

# Fixed small read window (~8x6 km, the same one used throughout the
# tile-boundary scaling benchmark) -- held constant across every worker
# count and every provider so per-unit cost isolates transport/concurrency
# effects, not window-size effects (already characterized separately).
READ_WINDOW_BBOX = (-122.4327, 44.23148, -122.34258, 44.28595)


def _discover_fixed_units(provider_name: str, n: int) -> list:
    provider = get_provider(provider_name)
    refs = provider.search_scenes(DISCOVERY_BBOX, "landsat", START, END, None, None, MAX_CLOUD, "any")
    if provider_name == "usgs_ard":
        refs = [
            r for r in refs
            if (r.provenance.extra.get("grid_horizontal"), r.provenance.extra.get("grid_vertical")) in ARD_TARGET_TILES
        ]
    refs = sorted(refs, key=lambda r: (r.date, r.id))
    return refs[:n]


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True, choices=["usgs_ard", "planetary_computer"])
    ap.add_argument("--workers", type=int, required=True)
    ap.add_argument("--num-units", type=int, default=NUM_UNITS)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--result-file", required=True)
    args = ap.parse_args()

    provider = get_provider(args.provider)
    refs = _discover_fixed_units(args.provider, args.num_units)
    unit_ids = [r.id for r in refs]

    config = Config(
        aoi=AOI(upper_left=(READ_WINDOW_BBOX[0], READ_WINDOW_BBOX[3]), lower_right=(READ_WINDOW_BBOX[2], READ_WINDOW_BBOX[1])),
        provider=args.provider,
        sensors=[SensorSpec(name="landsat")],
        date_range=DateRange(start=START, end=END),
        filters=Filters(max_cloud_percent=MAX_CLOUD, pixel_cloud_mask=True),
        temporal_mode="scene", reduce="median",
        output=OutputSpec(bands=tuple(BANDS), indices=(), dir=args.output_dir, write_files=True),
        workers=args.workers,
    )

    sensor_spec = config.sensors[0]
    contract = _resolve_contract(provider, "landsat", sensor_spec)
    res = sensor_spec.resolution()
    target_epsg = auto_utm_epsg(config.aoi.bbox)
    transform, w, h = target_grid(config.aoi.bbox, target_epsg, res)
    grid = Grid(crs=target_epsg, transform=transform, width=w, height=h)
    processing = _processing_metadata(provider, "landsat", config, contract, target_epsg, res)
    product = asdict(contract)
    output_dir = Path(args.output_dir)
    sensor_dir = output_dir / "landsat"

    captured = io.StringIO()
    cpu_before = resource.getrusage(resource.RUSAGE_SELF)
    net_before = _net_bytes()
    errors = []
    scenes_out, manifest_entries, failed = [], [], []
    with contextlib.redirect_stdout(captured):
        t0 = time.perf_counter()
        try:
            scenes_out, manifest_entries, failed = _run_scene_mode(
                provider, config, "landsat", sensor_dir, refs, BANDS, grid,
                output_dir, product, processing, snapshot_cache={},
            )
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")
        wall_s = time.perf_counter() - t0
    cpu_after = resource.getrusage(resource.RUSAGE_SELF)
    net_after = _net_bytes()

    stdout_text = captured.getvalue()
    print(stdout_text, end="")

    cpu_s = (cpu_after.ru_utime + cpu_after.ru_stime) - (cpu_before.ru_utime + cpu_before.ru_stime)
    peak_rss = cpu_after.ru_maxrss
    peak_rss_mb = (peak_rss if sys.platform == "darwin" else peak_rss * 1024) / 1e6
    net_mb = round((net_after - net_before) / 1e6, 2) if net_before is not None and net_after is not None else None

    scenes_ok = len(scenes_out)
    canary_id = unit_ids[0] if unit_ids else None
    canary_mean = None
    canary_file_sha256 = None
    if scenes_ok and scenes_out[0]["id"] == canary_id:
        import numpy as np

        canary_mean = {b: float(np.nanmean(scenes_out[0]["bands"][b])) for b in BANDS}
        # Output filename is derived from (date, sanitized id) only -- NOT
        # from worker count -- so the canary unit (first by our fixed sort)
        # writes to the identical relative path regardless of --workers,
        # letting the orchestrator diff these files byte-for-byte across
        # worker-count runs to confirm "scientifically identical output".
        canary_entry = next((m for m in manifest_entries if m.get("sceneId") == canary_id and m["kind"] == "bands"), None)
        if canary_entry is not None:
            canary_path = output_dir / canary_entry["file"]
            canary_file_sha256 = hashlib.sha256(canary_path.read_bytes()).hexdigest()

    out = {
        "provider": args.provider,
        "workers": args.workers,
        "num_units_requested": args.num_units,
        "unit_ids": unit_ids,
        "wall_s": wall_s,
        "cpu_s": cpu_s,
        "cpu_utilization_pct": (100.0 * cpu_s / wall_s) if wall_s else None,
        "scenes_read_ok": scenes_ok,
        "scenes_failed": len(failed),
        "failed_ids": [f[0] for f in failed],
        "units_per_hour": (scenes_ok / wall_s * 3600) if wall_s and scenes_ok else None,
        "mean_s_per_unit": (wall_s / scenes_ok) if scenes_ok else None,
        "grid": f"{w}x{h}",
        "pixels": w * h,
        "network_retries_or_reminting_observed": (
            stdout_text.count("re-signing and retrying") + stdout_text.count("re-minting and retrying")
            + stdout_text.count("STAC search retry") + stdout_text.count("transport retry")
        ),
        "net_mb_indicative": net_mb,
        "peak_rss_mb": peak_rss_mb,
        "canary_unit_id": canary_id,
        "canary_band_means": canary_mean,
        "canary_file_sha256": canary_file_sha256,
        "errors": errors,
    }
    Path(args.result_file).write_text(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
