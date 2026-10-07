#!/usr/bin/env python
"""[Historical: drives the pre-1.0 DataLoader engine/config API. Re-run it from the
`pre-1.0` git tag; see docs/development/benchmarks/README.md.]

Runs exactly one benchmark case (one provider/AOI/date/repetition
combination) as a standalone process, so peak RSS and any provider-level
global state (HTTP sessions, GDAL config, EE init) never leak between runs.

Reads a case spec from --case-file, writes one JSON result document to
--result-file. All *scientific* operations reuse existing data_loader code
directly: providers.get_provider, Provider.search_scenes/read_scene_bands/
read_annual_composite, aoi.auto_utm_epsg/target_grid, indices.compute_index/
required_raw_bands, engine._write_geotiff/_sanitize. Nothing in data_loader/
is modified; the only "new" control flow is the small amount of composite
book-keeping (np.stack + reduce) pulled out of engine._composite_from_scenes
so read time and reduce time can be measured separately.

The result is written to a file rather than parsed from stdout deliberately:
providers print diagnostics to stdout themselves (e.g.
stac_common.StacProvider's retry messages), and that's fine here since nothing
tries to parse this process's stdout as data.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
import warnings
from datetime import date, datetime, timezone
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

import numpy as np

from data_loader.aoi import auto_utm_epsg, target_grid
from data_loader.engine import _sanitize, _write_geotiff
from data_loader.indices import compute_index, required_raw_bands
from data_loader.providers import get_provider
from data_loader.providers.base import Grid

from timing import Stopwatch, peak_rss_bytes  # bench/timing.py


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def _valid_fraction(bands: dict) -> float:
    fracs = [float(np.mean(~np.isnan(arr))) for arr in bands.values()]
    return float(np.mean(fracs)) if fracs else float("nan")


def _write_outputs(band_dict, index_dict, out_dir: Path, tag: str, grid: Grid, requested_bands):
    write_s = 0.0
    file_bytes = 0
    written = []
    if requested_bands:
        bands_out = {b: band_dict[b] for b in requested_bands if b in band_dict}
        if bands_out:
            p = out_dir / f"bands_{tag}.tif"
            with Stopwatch() as sw:
                _write_geotiff(p, bands_out, grid)
            write_s += sw.seconds
            file_bytes += p.stat().st_size
            written.append(str(p))
    if index_dict:
        p = out_dir / f"indices_{tag}.tif"
        with Stopwatch() as sw:
            _write_geotiff(p, index_dict, grid)
        write_s += sw.seconds
        file_bytes += p.stat().st_size
        written.append(str(p))
    return write_s, file_bytes, written


def _forced_login(provider) -> None:
    """Force whatever lazy auth/client-open a provider does on first real
    call, now, so it's timed as init rather than silently bleeding into the
    first discovery/read measurement. Duck-typed on private method names
    across providers rather than a shared protocol method, since adding one
    would mean touching data_loader/ -- acceptable for a benchmark-only,
    best-effort timing split."""
    for method_name in ("_ensure_login", "_ensure_init", "_open_client"):
        method = getattr(provider, method_name, None)
        if method is not None:
            method()
            return


def _glad_seam_check(case: dict, band_dict: dict, grid: Grid, target_epsg: str, tile_counts: list) -> dict:
    """Cheap, provider-specific correctness probe -- only invoked when
    case['glad_tile_check'] is set. NOT a validation of the mosaic; see the
    'note' field in the returned dict."""
    from data_loader import pixel_index

    result = {
        "tiles_involved_per_scene": tile_counts,
        "max_tiles_involved": max(tile_counts) if tile_counts else None,
    }
    boundary = case.get("expected_seam_boundary")
    if not boundary or not band_dict:
        result["seam_flag"] = None
        result["note"] = "no expected_seam_boundary configured for this case, or no data returned"
        return result

    sample_band = next(iter(band_dict.values()))
    h, w = sample_band.shape
    bbox = tuple(case["aoi_bbox"])
    mid_lat = (bbox[1] + bbox[3]) / 2
    mid_lon = (bbox[0] + bbox[2]) / 2

    if boundary["axis"] == "lon":
        r, c = pixel_index(grid.transform, boundary["value"], mid_lat, target_epsg)
        idx, axis_len, seam_axis = c, w, 0
    else:
        r, c = pixel_index(grid.transform, mid_lon, boundary["value"], target_epsg)
        idx, axis_len, seam_axis = r, h, 1

    lo, hi = max(0, idx - 2), min(axis_len, idx + 3)
    valid = ~np.isnan(sample_band)
    if seam_axis == 0:
        seam_valid = float(np.mean(valid[:, lo:hi])) if hi > lo else float("nan")
        elsewhere = np.delete(valid, np.s_[lo:hi], axis=1)
    else:
        seam_valid = float(np.mean(valid[lo:hi, :])) if hi > lo else float("nan")
        elsewhere = np.delete(valid, np.s_[lo:hi], axis=0)
    elsewhere_valid = float(np.mean(elsewhere)) if elsewhere.size else float("nan")

    result["seam_pixel_index"] = idx
    result["seam_band_valid_fraction"] = seam_valid
    result["elsewhere_valid_fraction"] = elsewhere_valid
    result["seam_flag"] = bool(
        seam_valid == seam_valid  # not NaN
        and elsewhere_valid == elsewhere_valid
        and seam_valid < 0.5 * elsewhere_valid
    )
    result["note"] = (
        "seam_flag is a heuristic (narrow NaN-stripe check at the expected "
        "tile-boundary column/row vs. elsewhere). seam_flag=False is NOT "
        "proof the mosaic is correct -- only that this one cheap check "
        "didn't fire. Manually inspect the output GeoTIFF before trusting "
        "multi-tile GLAD ARD output."
    )
    return result


def run_case(case: dict) -> dict:
    errors: list[str] = []
    metrics = {
        "init_auth_s": None, "discovery_s": None, "read_total_s": None,
        "reduce_s": None, "index_s": None, "write_s": None, "total_s": None,
    }
    scenes_discovered = None
    scenes_read_ok = 0
    scenes_failed = 0
    scene_records: list[dict] = []
    output_files: list[str] = []
    output_file_bytes = 0
    valid_pixel_fraction = None
    peak_rss_after_read_bytes = None
    peak_rss_after_reduce_bytes = None
    theoretical_stack_floor_bytes = None
    glad_check = None

    total_sw = Stopwatch()
    total_sw.__enter__()

    bbox = tuple(case["aoi_bbox"])
    sensor = case["sensor"]
    bands = list(case["bands"])
    indices_wanted = list(case["indices"])
    raw_bands_needed = sorted(set(bands) | required_raw_bands(indices_wanted))
    pixel_cloud_mask = case["pixel_cloud_mask"]
    max_cloud_percent = case["max_cloud_percent"]
    season_start = case.get("season_start")
    season_end = case.get("season_end")

    target_epsg = auto_utm_epsg(bbox)
    transform, w, h = target_grid(bbox, target_epsg, case["resolution_m"])
    grid = Grid(crs=target_epsg, transform=transform, width=w, height=h)

    out_dir = Path(case["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        with Stopwatch() as sw_init:
            provider = get_provider(case["provider"], **case.get("provider_options", {}))
            _forced_login(provider)
        metrics["init_auth_s"] = sw_init.seconds

        composite_mode = case["composite_mode"]

        if composite_mode == "fast_path":
            year = case["composite_year"]
            metrics["discovery_s"] = 0.0
            with Stopwatch() as sw_read:
                composite = provider.read_annual_composite(
                    bbox, sensor, year, season_start, season_end,
                    raw_bands_needed, grid, max_cloud_percent, pixel_cloud_mask,
                    case["reduce"],
                )
            metrics["read_total_s"] = sw_read.seconds
            metrics["reduce_s"] = 0.0
            if composite is None:
                raise RuntimeError(
                    f"provider {case['provider']!r} returned None from "
                    "read_annual_composite (fast path declined) -- this "
                    "case assumed the fast path was available"
                )
            peak_rss_after_read_bytes = peak_rss_bytes()
            peak_rss_after_reduce_bytes = peak_rss_after_read_bytes
            valid_pixel_fraction = _valid_fraction(composite)

            with Stopwatch() as sw_idx:
                indices_out = {i: compute_index(i, composite) for i in indices_wanted}
            metrics["index_s"] = sw_idx.seconds

            write_s, output_file_bytes, output_files = _write_outputs(
                composite, indices_out, out_dir, f"composite_{year}", grid, bands
            )
            metrics["write_s"] = write_s

        else:
            if composite_mode == "local_reduce":
                year = case["composite_year"]
                year_start = max(_parse_date(case["date_start"]), date(year, 1, 1))
                year_end = min(_parse_date(case["date_end"]), date(year, 12, 31))
            else:  # "scene"
                year_start = _parse_date(case["date_start"])
                year_end = _parse_date(case["date_end"])

            with Stopwatch() as sw_disc:
                refs = provider.search_scenes(
                    bbox, sensor, year_start, year_end, season_start, season_end,
                    max_cloud_percent,
                )
            metrics["discovery_s"] = sw_disc.seconds
            scenes_discovered = len(refs)

            read_total_s = 0.0
            per_scene_bands = []
            glad_tile_counts = []
            for ref in refs:
                try:
                    with Stopwatch() as sw_read:
                        band_dict = provider.read_scene_bands(
                            ref, sensor, raw_bands_needed, grid, pixel_cloud_mask
                        )
                    read_total_s += sw_read.seconds
                    scenes_read_ok += 1
                    vf = _valid_fraction(band_dict)
                    scene_records.append({
                        "id": ref.id, "date": ref.date.isoformat(),
                        "cloud_percent": ref.cloud_percent,
                        "read_seconds": sw_read.seconds,
                        "valid_pixel_fraction": vf,
                    })
                    per_scene_bands.append((ref, band_dict))
                    if case.get("glad_tile_check") and isinstance(ref.handle, list):
                        glad_tile_counts.append(len(ref.handle))
                except Exception as e:
                    scenes_failed += 1
                    errors.append(f"read_scene_bands({ref.id}): {e}")
            metrics["read_total_s"] = read_total_s
            peak_rss_after_read_bytes = peak_rss_bytes()

            if composite_mode == "local_reduce":
                theoretical_stack_floor_bytes = (
                    len(per_scene_bands) * len(raw_bands_needed) * grid.height * grid.width * 4
                )
                stacks = {b: [] for b in raw_bands_needed}
                for _, band_dict in per_scene_bands:
                    for b in raw_bands_needed:
                        stacks[b].append(band_dict[b])
                reducer = np.nanmedian if case["reduce"] == "median" else np.nanmean
                with Stopwatch() as sw_reduce:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", RuntimeWarning)
                        composite = {
                            b: reducer(np.stack(arrs), axis=0).astype("f4")
                            for b, arrs in stacks.items()
                        }
                metrics["reduce_s"] = sw_reduce.seconds
                peak_rss_after_reduce_bytes = peak_rss_bytes()
                valid_pixel_fraction = _valid_fraction(composite) if composite else None

                with Stopwatch() as sw_idx:
                    indices_out = {i: compute_index(i, composite) for i in indices_wanted}
                metrics["index_s"] = sw_idx.seconds

                write_s, output_file_bytes, output_files = _write_outputs(
                    composite, indices_out, out_dir, f"composite_{year}", grid, bands
                )
                metrics["write_s"] = write_s

                if case.get("glad_tile_check"):
                    glad_check = _glad_seam_check(case, composite, grid, target_epsg, glad_tile_counts)

            else:  # scene mode
                metrics["reduce_s"] = None
                index_s_total = 0.0
                write_s_total = 0.0
                vf_list = []
                for ref, band_dict in per_scene_bands:
                    with Stopwatch() as sw_idx:
                        indices_out = {i: compute_index(i, band_dict) for i in indices_wanted}
                    index_s_total += sw_idx.seconds
                    vf_list.append(_valid_fraction(band_dict))
                    tag = f"{ref.date.isoformat()}_{_sanitize(ref.id)}"
                    ws, fb, files = _write_outputs(band_dict, indices_out, out_dir, tag, grid, bands)
                    write_s_total += ws
                    output_file_bytes += fb
                    output_files.extend(files)
                metrics["index_s"] = index_s_total
                metrics["write_s"] = write_s_total
                valid_pixel_fraction = float(np.mean(vf_list)) if vf_list else None

                if case.get("glad_tile_check") and per_scene_bands:
                    _, first_bands = per_scene_bands[0]
                    glad_check = _glad_seam_check(case, first_bands, grid, target_epsg, glad_tile_counts)

    except Exception as e:
        errors.append(f"{type(e).__name__}: {e}")
        errors.append(traceback.format_exc())

    total_sw.__exit__(None, None, None)
    metrics["total_s"] = total_sw.seconds

    return {
        **{k: case[k] for k in case if k != "provider_options"},
        "provider_options_keys": sorted(case.get("provider_options", {})),
        "target_epsg": target_epsg,
        "grid_width": w, "grid_height": h,
        "scenes_discovered": scenes_discovered,
        "scenes_read_ok": scenes_read_ok,
        "scenes_failed": scenes_failed,
        "scenes": scene_records,
        **metrics,
        "peak_rss_after_read_bytes": peak_rss_after_read_bytes,
        "peak_rss_after_reduce_bytes": peak_rss_after_reduce_bytes,
        "peak_rss_final_bytes": peak_rss_bytes(),
        "theoretical_stack_floor_bytes": theoretical_stack_floor_bytes,
        "output_files": output_files,
        "output_file_bytes": output_file_bytes,
        "valid_pixel_fraction": valid_pixel_fraction,
        "glad_tile_check": glad_check,
        "errors": errors,
        "timestamp": datetime.now(timezone.utc).isoformat(),
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
