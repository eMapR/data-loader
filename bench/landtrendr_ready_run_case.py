#!/usr/bin/env python
"""[Historical: drives the pre-1.0 DataLoader engine/config API. Re-run it from the
`pre-1.0` git tag; see docs/development/benchmarks/README.md.]

Complete end-to-end pipeline benchmark for ONE provider, run as its own
subprocess (see landtrendr_ready_bench.py for why: GDAL /vsicurl cache
isolation, established the hard way earlier in this project).

This is the first benchmark in the ARD-vs-PC series that measures more
than acquisition: the deliverable for both providers is a genuine
"LandTrendr-ready" per-date time series on one shared grid -- one
QA-masked, MOSAICKED GeoTIFF per unique acquisition date (not per scene,
not an annual composite), covering same-date fragments regardless of how
many WRS-2 scenes (Planetary Computer) or ARD tiles (usgs_ard) contributed
to that date. Neither provider's raw output already looks like this:
- Planetary Computer's scene mode (data_loader.engine) writes one file per
  SCENE, never merging same-date WRS-2 fragments -- exactly the
  "downstream mosaicking work" flagged as uncounted in the acquisition-
  only benchmarks. This script does that merge.
- usgs_ard tiles are already mosaicked WITHIN one tile (USGS did that
  upstream, see product_contract.USGS_ARD_SR), but this AOI deliberately
  straddles TWO ARD tiles (h03v04/h04v04), so a date can still need two
  tiles merged -- this script does that merge too, by the same logic path
  (a mosaic step is required for both providers here; ARD's is just
  needed less often).

Design choices, made explicit because they affect what "scientifically
comparable" means for the output:
- QA masking happens BEFORE the same-date merge, per contributing unit
  (each unit's own cloud/shadow pixels become NaN via the provider's
  existing pixel_cloud_mask=True), so a masked-out pixel in one
  contributing scene/tile never suppresses a genuinely valid pixel from
  another contributing scene/tile covering the same location -- the merge
  is "first valid wins" per pixel (data_loader.providers.glad_ard.py's own
  multi-tile merge uses the identical idiom, for the same reason).
- A second NaN source is also handled: fill/no-source-coverage pixels.
  Neither stac_common.py's nor usgs_ard.py's read_scene_bands checks
  src.nodata explicitly, but WarpedVRT propagates the source's nodata
  (DN=0 for Collection 2 SR) automatically, so a fill pixel comes back as
  exactly `0 * sr_scale + sr_offset == sr_offset` -- a numerically exact,
  unambiguous sentinel (real reflectance data cannot coincide with it,
  since that requires DN==0 exactly). This script detects that sentinel
  post-read and treats it as NaN before merging, so an out-of-footprint
  region from one unit never overwrites another unit's real data, and
  never survives into the final composite if nothing else covers it.
- Indices (NDVI, NBR) are computed from the merged, masked composite --
  the same data_loader.indices.compute_index already used elsewhere in
  this codebase -- not from individual pre-merge units.

Phases timed separately per the user's request: discovery (once),
acquisition (sum of all read_scene_bands calls), mosaic/QA-fill handling
(numpy merge across same-date units), indices, and GeoTIFF writing --
reported both as per-date-summed totals and as a fraction of total wall
time.
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
from collections import defaultdict
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Optional

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

import numpy as np

from data_loader.aoi import auto_utm_epsg, target_grid
from data_loader.engine import _write_geotiff
from data_loader.indices import compute_index, required_raw_bands
from data_loader.manifest import write_manifest
from data_loader.providers import get_provider
from data_loader.providers.base import Grid

BANDS = ["blue", "green", "red", "nir", "swir1", "swir2"]
INDICES = ["ndvi", "nbr"]
RAW_BANDS_NEEDED = sorted(set(BANDS) | required_raw_bands(INDICES))

# Two AOI presets, both centered on real ARD tile boundaries (verified
# live), sized deliberately differently from their nominal "N tiles" tile
# COUNT: unit count (how many tile-dates/scenes are discovered) depends
# only on which tiles/scenes the AOI touches, not on how large the read
# window is within them (confirmed live: 60km and 70km variants of the
# 4tile AOI below discover the identical 137/83 units) -- so window size
# is chosen independently, for practicality, while tile/scene COUNT is
# chosen by AOI placement. A literal full N-tile block (150km/tile) would
# put a single-unit read window at 100-200M+ pixels, which -- given the
# already-established super-linear cost-vs-window-size relationship --
# would take hours per read; this decouples "how many tiles does the AOI
# span" (the variable of interest) from "how large is each individual
# read" (the practical constraint), the same design principle already
# used to decouple search scope from read-window size elsewhere in this
# project's benchmarks.
AOI_PRESETS = {
    # ~40x40 km straddling the h03v04/h04v04 boundary -- small and
    # practical, guarantees genuine multi-unit-per-date mosaicking for
    # both providers (every ARD date needs 2 tiles merged; a meaningful
    # PC fraction needs 2+ WRS-2 scenes merged).
    "2tile": (-121.34043486484151, 44.18462971642364, -120.72318365543096, 44.624698906445126),
    # ~60x60 km centered on the exact 4-way corner where h03v04, h04v04,
    # h03v05, h04v05 meet -- verified live to discover all 4 tiles (137
    # ARD units, 36 distinct dates, 101 same-date duplicates; 83 PC units,
    # 44 distinct dates, 39 duplicates) with the same date range/season/
    # cloud filter as "2tile". Real UTM grid: 2523x2504 = 6.32M pixels
    # (larger than the naive WGS84-degree estimate, due to reprojection).
    "4tile": (-121.2469, 43.42574, -120.33118, 44.08403),
    # 4x2 block h02-h05 x v04-v05, straddling the same v04/v05 boundary
    # as "4tile". Unlike 2tile->4tile, window size CANNOT be held small
    # here: to touch 4 tile columns a rectangle must span >2 full tiles
    # (>300 km), and because the Albers ARD grid is rotated relative to
    # lon/lat (the v04/v05 boundary climbs ~0.7 deg latitude across the
    # span), the smallest lon/lat bbox that stays on that boundary
    # end-to-end is ~310x125 km. Real UTM grid: 10009x4118 = 41.2M pixels
    # (6.5x "4tile"), so this preset tests tile count AND window growth
    # together -- the latter is exactly what open question #20 asks about.
    "8tile": (-122.62, 43.22, -118.93, 44.26),
}
AOI_BBOX = AOI_PRESETS["2tile"]  # overridden by --aoi-preset in main()
# When set, process only this many evenly spaced dates out of all
# discovered dates (smoke-test mode): enough to measure real per-unit
# cost/memory/output size at a preset's window, without the full run.
# Discovery still covers the full period, so the result carries the full
# per-date unit counts needed to project a full run's cost.
SAMPLE_DATES: Optional[int] = None


def _evenly_spaced(items: list, k: Optional[int]) -> list:
    if not k or k >= len(items):
        return list(items)
    if k == 1:
        return [items[len(items) // 2]]
    return [items[round(i * (len(items) - 1) / (k - 1))] for i in range(k)]

# 2 years, peak growing season only (standard practice for a Landsat
# time-series stack -- minimizes snow/phenology/cloud noise) -- a
# "representative multi-year period" sized to keep this a small, practical
# test rather than a multi-hour run (see bench/ard_tile_scaling_bench.py
# and friends for what a full-year, multi-year, or Oregon-scale sweep
# actually costs). Held IDENTICAL across every AOI preset, so preset is
# the only variable between comparisons.
START, END = date(2022, 1, 1), date(2023, 12, 31)
SEASON_START, SEASON_END = "06-15", "08-31"
MAX_CLOUD_PERCENT = 40


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


def _fill_to_nan(band_dict: dict, sr_offset: float, atol: float = 1e-6) -> None:
    """Marks fill/out-of-footprint pixels (WarpedVRT-propagated source
    nodata, DN=0 -> exactly `sr_offset` post scale/offset) as NaN, in
    place, across every band -- fill affects the whole pixel (shared
    footprint), not one band independently. See module docstring for why
    this exact-value check is safe."""
    if not band_dict:
        return
    any_band = next(iter(band_dict.values()))
    is_fill = np.isclose(any_band, sr_offset, atol=atol)
    # A pixel already NaN (QA-masked) stays NaN; a fill pixel becomes NaN
    # in every band, consistently.
    for arr in band_dict.values():
        arr[is_fill] = np.nan


def run_provider(provider_name: str, output_dir: Path) -> dict:
    provider = get_provider(provider_name)
    sr_offset = provider.processing_profile("landsat")["sr_offset"]

    target_epsg = auto_utm_epsg(AOI_BBOX)
    transform, w, h = target_grid(AOI_BBOX, target_epsg, 30.0)
    grid = Grid(crs=target_epsg, transform=transform, width=w, height=h)

    timers = {"discovery_s": 0.0, "acquisition_s": 0.0, "mosaic_s": 0.0, "indices_s": 0.0, "write_s": 0.0}
    net0 = _net_bytes()

    t0 = time.perf_counter()
    refs = provider.search_scenes(
        AOI_BBOX, "landsat", START, END, SEASON_START, SEASON_END, MAX_CLOUD_PERCENT, "any",
    )
    timers["discovery_s"] = time.perf_counter() - t0

    by_date: dict[date, list] = defaultdict(list)
    for r in refs:
        by_date[r.date].append(r)

    sensor_dir = output_dir / "landsat"
    sensor_dir.mkdir(parents=True, exist_ok=True)
    # Resumability: this benchmark's runs are long enough (tens of minutes)
    # to occasionally be killed by external system memory pressure
    # unrelated to this process's own (modest, ~150-500MB) footprint --
    # observed live, repeatedly, on this machine. Rather than lose all
    # progress, a date whose both output files already exist on disk is
    # skipped entirely (not re-read, re-mosaicked, or re-written) -- the
    # orchestrator re-invokes this same script with the same --output-dir
    # until nothing new is left to do, so a kill only costs whatever was
    # in flight at that moment. Any existing manifest.json's entries are
    # preserved and extended, not overwritten.
    manifest_path = output_dir / "manifest.json"
    manifest_entries: list[dict] = []
    if manifest_path.exists():
        try:
            manifest_entries = json.loads(manifest_path.read_text()).get("files", [])
        except Exception:
            manifest_entries = []

    # Dates where EVERY contributing unit failed can never succeed on a
    # naive resume (the same units will just fail again) -- persisted
    # separately from manifest_entries (which only records successful
    # dates) so a retry loop can tell "genuinely done" apart from "still
    # has real work left", and so failures from an earlier, killed
    # invocation aren't silently lost from the final report.
    failed_dates_path = output_dir / "failed_dates.json"
    failures: list[dict] = []
    permanently_failed_dates: set[str] = set()
    if failed_dates_path.exists():
        try:
            prior = json.loads(failed_dates_path.read_text())
            failures = prior.get("failures", [])
            permanently_failed_dates = set(prior.get("permanently_failed_dates", []))
        except Exception:
            pass

    def _date_already_done(d: date) -> bool:
        tag = d.isoformat()
        if tag in permanently_failed_dates:
            return True
        return (sensor_dir / f"bands_{tag}.tif").exists() and (sensor_dir / f"indices_{tag}.tif").exists()

    contributing_unit_counts = []
    max_units_any_date = 0
    dates_processed_this_invocation = 0

    selected_dates = _evenly_spaced(sorted(by_date), SAMPLE_DATES)
    # Per-unit read timings, appended after every unit (not returned
    # in-memory only) so they survive a kill and accumulate across the
    # orchestrator's resume attempts -- the basis for per-unit cost
    # estimates at this preset's window size.
    unit_reads_path = output_dir / "unit_reads.jsonl"

    for d in selected_dates:
        if _date_already_done(d):
            continue
        units = sorted(by_date[d], key=lambda r: (r.cloud_percent if r.cloud_percent is not None else 999))
        contributing_unit_counts.append(len(units))
        max_units_any_date = max(max_units_any_date, len(units))

        composite: dict[str, np.ndarray] = {b: np.full((h, w), np.nan, dtype="f4") for b in RAW_BANDS_NEEDED}
        contributing_ids = []
        for unit in units:
            t0 = time.perf_counter()
            ok = False
            try:
                band_dict = provider.read_scene_bands(unit, "landsat", RAW_BANDS_NEEDED, grid, pixel_cloud_mask=True)
                ok = True
            except Exception as e:
                failures.append({"date": d.isoformat(), "unit_id": unit.id, "error": f"{type(e).__name__}: {e}"})
                continue
            finally:
                read_s = time.perf_counter() - t0
                timers["acquisition_s"] += read_s
                with unit_reads_path.open("a") as f:
                    f.write(json.dumps({"date": d.isoformat(), "unit_id": unit.id,
                                        "read_s": read_s, "ok": ok}) + "\n")

            t0 = time.perf_counter()
            _fill_to_nan(band_dict, sr_offset)
            for b in RAW_BANDS_NEEDED:
                empty = np.isnan(composite[b])
                composite[b][empty] = band_dict[b][empty]
            timers["mosaic_s"] += time.perf_counter() - t0
            contributing_ids.append(unit.id)

        if not contributing_ids:
            permanently_failed_dates.add(d.isoformat())
        # Persisted after every date (success or failure) -- not just at
        # the very end -- so a kill mid-run never loses failure records
        # from dates processed earlier in this same invocation, and so a
        # permanently-failed date is never endlessly re-attempted on resume.
        failed_dates_path.write_text(json.dumps(
            {"failures": failures, "permanently_failed_dates": sorted(permanently_failed_dates)},
            indent=2, default=str,
        ))
        if not contributing_ids:
            continue  # every contributing unit for this date failed

        t0 = time.perf_counter()
        bands_out = {b: composite[b] for b in BANDS}
        indices_out = {i: compute_index(i, composite) for i in INDICES}
        timers["indices_s"] += time.perf_counter() - t0

        t0 = time.perf_counter()
        tag = d.isoformat()
        bp = sensor_dir / f"bands_{tag}.tif"
        bnames = _write_geotiff(bp, bands_out, grid)
        ip = sensor_dir / f"indices_{tag}.tif"
        inames = _write_geotiff(ip, indices_out, grid)
        timers["write_s"] += time.perf_counter() - t0

        manifest_entries.append({
            "date": tag, "kind": "bands", "file": str(bp.relative_to(output_dir)),
            "bandNames": bnames, "contributingUnits": contributing_ids,
            "numContributingUnits": len(contributing_ids),
            "provenance": [asdict(u.provenance) for u in units if u.provenance is not None],
        })
        manifest_entries.append({
            "date": tag, "kind": "indices", "file": str(ip.relative_to(output_dir)),
            "bandNames": inames, "contributingUnits": contributing_ids,
            "numContributingUnits": len(contributing_ids),
        })
        dates_processed_this_invocation += 1
        # Written after EVERY date, not just at the end -- if this
        # invocation is killed by external memory pressure partway
        # through, dates already completed stay recorded (and, via
        # _date_already_done's file check above, correctly skipped on the
        # next resume regardless of whether this write races the kill).
        write_manifest(manifest_path, manifest_entries, provider=provider_name)

    net1 = _net_bytes()

    output_files = list(sensor_dir.glob("*.tif")) if sensor_dir.exists() else []
    output_bytes = sum(f.stat().st_size for f in output_files)
    num_output_dates = len({e["date"] for e in manifest_entries if e["kind"] == "bands"})

    return {
        "provider": provider_name,
        "grid": f"{w}x{h}", "target_epsg": target_epsg,
        "num_source_units": len(refs),
        "num_dates_discovered": len(by_date),
        "num_output_dates": num_output_dates,
        "num_permanently_failed_dates": len(permanently_failed_dates),
        "dates_processed_this_invocation": dates_processed_this_invocation,
        "dates_remaining": sum(1 for d in selected_dates
                               if not _date_already_done(d) and d.isoformat() not in permanently_failed_dates),
        "sampled_dates": [d.isoformat() for d in selected_dates] if SAMPLE_DATES else None,
        "units_per_date_all": {d.isoformat(): len(v) for d, v in sorted(by_date.items())},
        "mean_contributing_units_per_date": float(np.mean(contributing_unit_counts)) if contributing_unit_counts else None,
        "max_contributing_units_any_date": max_units_any_date,
        "dates_needing_mosaic": sum(1 for c in contributing_unit_counts if c > 1),
        "failures": failures,
        "timers": timers,
        "net_mb_indicative": round((net1 - net0) / 1e6, 2) if net0 is not None and net1 is not None else None,
        "output_file_count": len(output_files),
        "output_bytes": output_bytes,
        "output_dates": sorted({e["date"] for e in manifest_entries if e["kind"] == "bands"}),
    }


def main():
    global AOI_BBOX, SAMPLE_DATES

    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True, choices=["usgs_ard", "planetary_computer"])
    ap.add_argument("--aoi-preset", default="2tile", choices=list(AOI_PRESETS))
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--result-file", required=True)
    ap.add_argument("--sample-dates", type=int, default=None)
    args = ap.parse_args()

    AOI_BBOX = AOI_PRESETS[args.aoi_preset]
    SAMPLE_DATES = args.sample_dates
    output_dir = Path(args.output_dir)
    captured = io.StringIO()
    cpu_before = resource.getrusage(resource.RUSAGE_SELF)

    with contextlib.redirect_stdout(captured):
        t0 = time.perf_counter()
        result = run_provider(args.provider, output_dir)
        wall_s = time.perf_counter() - t0

    cpu_after = resource.getrusage(resource.RUSAGE_SELF)
    print(captured.getvalue(), end="")

    cpu_s = (cpu_after.ru_utime + cpu_after.ru_stime) - (cpu_before.ru_utime + cpu_before.ru_stime)
    peak_rss = cpu_after.ru_maxrss
    peak_rss_mb = (peak_rss if sys.platform == "darwin" else peak_rss * 1024) / 1e6

    result["wall_s"] = wall_s
    result["cpu_s"] = cpu_s
    result["peak_rss_mb"] = peak_rss_mb
    result["retries_observed"] = (
        captured.getvalue().count("re-signing and retrying")
        + captured.getvalue().count("re-minting and retrying")
        + captured.getvalue().count("rate-limit retry")
        + captured.getvalue().count("transport retry")
        + captured.getvalue().count("STAC search retry")
    )

    Path(args.result_file).write_text(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
