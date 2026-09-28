"""Orchestration: for each configured sensor, ask the configured provider
for either one composite per year or every individual scene, split the
result into requested raw-band/index outputs, and (if `output.write_files`)
write GeoTIFFs + a manifest entry per file.

Provider-agnostic by construction: this module only calls
`Provider.search_scenes` / `read_scene_bands` / (optionally)
`read_annual_composite` — it doesn't know or care whether those are backed
by a STAC endpoint or Earth Engine. Which upstream *product* those calls
actually return is governed separately by data_loader.product_contract:
`_resolve_contract` below checks the configured provider's declared
capability for each sensor against the request before any data is fetched,
so a provider/sensor combination that doesn't supply the expected product
family fails loudly instead of silently substituting a different one.

Concurrency (`config.workers`, default 1): a scene is the unit of work.
Scene mode dispatches each scene's full read/mask/index/write/provenance
pipeline (`_process_one_scene` below) to a thread pool; annual-composite's
local-reduce fallback parallelizes only the per-scene band reads that feed
the reduce (the reduce/write itself stays serial, unchanged). Output is
always assembled back in original scene order (an index-addressed results
list, not append-as-completed) so manifests/composites are byte-identical
regardless of which worker finishes first. `workers=1` takes the exact
same code path as `workers>1` (a one-worker thread pool), not a separate
serial branch, so there is only one implementation to trust.
"""
from __future__ import annotations

import re
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path
from typing import Optional

import numpy as np

from data_loader.aoi import auto_utm_epsg, target_grid
from data_loader.config import Config, SensorSpec
from data_loader.indices import compute_index, required_raw_bands
from data_loader.manifest import write_manifest
from data_loader.metadata_snapshot import write_snapshot
from data_loader.product_contract import ProductContract, resolve_contract
from data_loader.providers import get_provider
from data_loader.providers.base import Grid, SceneRef


def _sanitize(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", s)


def _split_outputs(raw_bands, output):
    bands_out = {b: raw_bands[b] for b in output.bands} if output.bands else {}
    indices_out = {i: compute_index(i, raw_bands) for i in output.indices} if output.indices else {}
    return bands_out, indices_out


def _resolve_contract(provider, sensor: str, sensor_spec: SensorSpec) -> ProductContract:
    identity = provider.capabilities().get(sensor)
    if identity is None:
        raise ValueError(
            f"provider {provider.name!r} does not support sensor {sensor!r} "
            f"(declared capabilities: {sorted(provider.capabilities())})"
        )
    return resolve_contract(
        sensor, identity, sensor_spec.product_family, sensor_spec.processing_version_policy
    )


def _scene_manifest_record(
    scene: SceneRef, provider, output_dir: Path, write_files: bool, snapshot_cache: dict,
    snapshot_lock: Optional[threading.Lock] = None,
) -> dict:
    """Builds the manifest's per-scene provenance record, and -- the one
    part that actually does work beyond formatting -- resolves and writes
    (or reuses) that scene's full source-metadata snapshot, filling in
    `provenance.source_metadata_ref`. `snapshot_cache` is shared across one
    `run()` call, keyed by (provider, provider_item_id): the same scene can
    legitimately appear in more than one manifest entry (a bands file and
    an indices file both citing it, or the same scene contributing to more
    than one composite year at a season-window boundary), and this ensures
    a provider whose source_metadata() costs a network call (GEE) is only
    ever asked for it once per scene per run, not once per manifest entry.

    `snapshot_lock`, when given, guards the whole check-fetch-write
    critical section so concurrent scene-mode workers (see
    `_process_one_scene`) can't race on the same cache entry or the same
    on-disk snapshot file -- `write_snapshot` is itself idempotent, but the
    lock also avoids two threads redundantly paying for a provider's
    network-backed source_metadata() call for the same scene.
    """
    provenance = scene.provenance
    if write_files and provenance is not None:
        cache_key = (provenance.provider, provenance.provider_item_id)

        def _resolve_ref():
            if cache_key in snapshot_cache:
                return snapshot_cache[cache_key]
            get_source_metadata = getattr(provider, "source_metadata", None)
            record = get_source_metadata(scene) if get_source_metadata is not None else None
            ref = (
                write_snapshot(output_dir, provenance.provider, provenance.provider_item_id, record)
                if record is not None else None
            )
            snapshot_cache[cache_key] = ref
            return ref

        if snapshot_lock is not None:
            with snapshot_lock:
                ref = _resolve_ref()
        else:
            ref = _resolve_ref()
        if ref is not None:
            provenance = replace(provenance, source_metadata_ref=ref)
    return {
        "date": scene.date.isoformat(),
        "cloudPercent": scene.cloud_percent,
        "provenance": asdict(provenance) if provenance is not None else None,
    }


def _processing_metadata(provider, sensor: str, config: Config, contract: ProductContract, target_epsg: str, res: float) -> dict:
    profile = {}
    get_profile = getattr(provider, "processing_profile", None)
    if get_profile is not None:
        profile = dict(get_profile(sensor))
    return {
        "provider": config.provider,
        "targetCrs": target_epsg,
        "resolutionM": res,
        "requestedBands": list(config.output.bands),
        "indices": list(config.output.indices),
        "pixelCloudMask": config.filters.pixel_cloud_mask,
        "reducer": config.reduce if config.temporal_mode == "annual_composite" else None,
        "processingVersionPolicy": contract.processing_version_policy,
        "normalizationApplied": [],  # v1: no DataLoader-side harmonization is performed yet
        **profile,
    }


def _read_scenes_concurrently(provider, sensor, refs, raw_bands_needed, grid, pixel_cloud_mask, workers: int):
    """Read every scene in `refs` (order-independent I/O), returning a list
    of (scene, band_dict-or-None, error-or-None) in the SAME order as
    `refs` regardless of which worker finished first -- a plain
    index-addressed results list, not append-as-completed. A single
    ThreadPoolExecutor(max_workers=1) is used even for the serial
    (workers=1) case so there is only one code path to trust."""
    results: list = [None] * len(refs)

    def task(i, scene):
        try:
            bands = provider.read_scene_bands(scene, sensor, raw_bands_needed, grid, pixel_cloud_mask)
            return i, scene, bands, None
        except Exception as e:
            return i, scene, None, e

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, scene, bands, err in ex.map(lambda t: task(*t), enumerate(refs)):
            results[i] = (scene, bands, err)
    return results


def _composite_from_scenes(provider, config: Config, sensor, year, raw_bands_needed, grid, processing_version_policy):
    year_start = max(config.date_range.start, date(year, 1, 1))
    year_end = min(config.date_range.end, date(year, 12, 31))
    if year_start > year_end:
        return None, [], [], []
    refs = provider.search_scenes(
        config.aoi.bbox, sensor, year_start, year_end,
        config.date_range.season_start, config.date_range.season_end,
        config.filters.max_cloud_percent, processing_version_policy,
    )
    excluded = getattr(provider, "last_excluded_versions", [])
    if not refs:
        return None, [], excluded, []

    read_results = _read_scenes_concurrently(
        provider, sensor, refs, raw_bands_needed, grid, config.filters.pixel_cloud_mask, config.workers,
    )
    used_refs = []
    failed_scenes = []
    stacks = {b: [] for b in raw_bands_needed}
    for scene, bands, err in read_results:
        if err is not None:
            failed_scenes.append((scene.id, str(err)))
            print(f"[engine] scene {scene.id} FAILED (excluded from composite): {err}")
            continue
        used_refs.append(scene)
        for b in raw_bands_needed:
            stacks[b].append(bands[b])
    if not used_refs:
        return None, [], excluded, failed_scenes
    reducer = np.nanmedian if config.reduce == "median" else np.nanmean
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        data = {b: reducer(np.stack(arrs), axis=0).astype("f4") for b, arrs in stacks.items()}
    return data, used_refs, excluded, failed_scenes


def _write_geotiff(path: Path, band_dict: dict, grid: Grid) -> list[str]:
    import rasterio

    names = list(band_dict)
    data = np.stack([band_dict[n] for n in names]).astype("f4")
    profile = {
        "driver": "GTiff", "dtype": "float32", "count": len(names),
        "height": grid.height, "width": grid.width,
        "crs": grid.crs, "transform": grid.transform,
        "nodata": np.nan, "compress": "deflate", "tiled": True,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data)
        for i, n in enumerate(names, start=1):
            dst.set_band_description(i, n)
    return names


def _manifest_entry(
    path, output_dir, kind, band_names, grid, sensor, provider_name,
    product: dict, processing: dict, scenes: Optional[list], **extra,
):
    return {
        "file": str(path.relative_to(output_dir)),
        "provider": provider_name,
        "sensor": sensor,
        "kind": kind,  # "bands" | "indices"
        "bandNames": band_names,
        "crs": grid.crs,
        "width": grid.width,
        "height": grid.height,
        "transform": list(grid.transform)[:6],
        **extra,
        "product": product,
        "processing": processing,
        "scenes": scenes,
    }


def _process_one_scene(
    provider, config: Config, sensor: str, sensor_dir: Path, scene: SceneRef,
    raw_bands_needed: list, grid: Grid, output_dir: Path, product: dict, processing: dict,
    snapshot_cache: dict, snapshot_lock: threading.Lock,
):
    """Everything scene mode does for exactly one scene -- band read, QA
    mask, index computation, GeoTIFF write(s), provenance/manifest-entry
    construction -- so it can run as one self-contained unit of work in
    engine.run()'s thread pool. Raises on failure (caught by the caller,
    which attributes the failure to this scene's id) rather than
    swallowing anything here."""
    data = provider.read_scene_bands(
        scene, sensor, raw_bands_needed, grid, config.filters.pixel_cloud_mask
    )
    bands_out, indices_out = _split_outputs(data, config.output)
    scenes_out_entry = {"date": scene.date, "id": scene.id, "bands": bands_out, "indices": indices_out}

    manifest_entries: list[dict] = []
    if config.output.write_files:
        tag = f"{scene.date.isoformat()}_{_sanitize(scene.id)}"
        scene_manifest = [
            _scene_manifest_record(scene, provider, output_dir, config.output.write_files, snapshot_cache, snapshot_lock)
        ]
        if bands_out:
            p = sensor_dir / f"bands_{tag}.tif"
            names = _write_geotiff(p, bands_out, grid)
            manifest_entries.append(_manifest_entry(
                p, output_dir, "bands", names, grid, sensor, config.provider,
                product, processing, scene_manifest,
                date=scene.date.isoformat(), sceneId=scene.id,
                cloudPercent=scene.cloud_percent,
                pixelCloudMask=config.filters.pixel_cloud_mask,
            ))
        if indices_out:
            p = sensor_dir / f"indices_{tag}.tif"
            names = _write_geotiff(p, indices_out, grid)
            manifest_entries.append(_manifest_entry(
                p, output_dir, "indices", names, grid, sensor, config.provider,
                product, processing, scene_manifest,
                date=scene.date.isoformat(), sceneId=scene.id,
                cloudPercent=scene.cloud_percent,
                pixelCloudMask=config.filters.pixel_cloud_mask,
            ))
    return scenes_out_entry, manifest_entries


def _run_scene_mode(
    provider, config: Config, sensor: str, sensor_dir: Path, refs: list,
    raw_bands_needed: list, grid: Grid, output_dir: Path, product: dict, processing: dict,
    snapshot_cache: dict,
) -> tuple[list, list, list]:
    """Dispatches _process_one_scene for every scene in `refs` across
    `config.workers` threads (1 == a one-worker pool, not a separate serial
    branch). Returns (scenes_out, manifest_entries, failed_scenes) with
    scenes_out/manifest_entries in the SAME order `refs` was given in,
    regardless of completion order -- see module docstring."""
    snapshot_lock = threading.Lock()
    results: list = [None] * len(refs)
    failed_scenes: list[tuple[str, str]] = []

    def task(i, scene):
        try:
            return i, _process_one_scene(
                provider, config, sensor, sensor_dir, scene, raw_bands_needed, grid,
                output_dir, product, processing, snapshot_cache, snapshot_lock,
            ), None
        except Exception as e:
            return i, None, (scene.id, str(e))

    with ThreadPoolExecutor(max_workers=config.workers) as ex:
        for i, ok_result, err in ex.map(lambda t: task(*t), enumerate(refs)):
            if err is not None:
                failed_scenes.append(err)
                print(f"[engine] scene {err[0]} FAILED: {err[1]}")
            else:
                results[i] = ok_result

    scenes_out: list = []
    manifest_entries: list[dict] = []
    for r in results:
        if r is None:
            continue  # that scene failed -- see failed_scenes
        scenes_out_entry, entries = r
        scenes_out.append(scenes_out_entry)
        manifest_entries.extend(entries)
    return scenes_out, manifest_entries, failed_scenes


def run(config: Config) -> dict:
    provider = get_provider(config.provider, **config.provider_options)
    output_dir = Path(config.output.dir)
    manifest_entries: list[dict] = []
    excluded_versions_by_sensor: dict[str, list[dict]] = {}
    failed_scenes_by_sensor: dict[str, list[dict]] = {}
    # (provider, provider_item_id) -> source_metadata_ref, shared across
    # this whole run -- see _scene_manifest_record.
    snapshot_cache: dict = {}
    result: dict = {}

    raw_bands_needed = sorted(set(config.output.bands) | required_raw_bands(config.output.indices))

    for sensor_spec in config.sensors:
        sensor = sensor_spec.name
        contract = _resolve_contract(provider, sensor, sensor_spec)
        res = sensor_spec.resolution()
        target_epsg = config.output.target_epsg or auto_utm_epsg(config.aoi.bbox)
        transform, w, h = target_grid(config.aoi.bbox, target_epsg, res)
        grid = Grid(crs=target_epsg, transform=transform, width=w, height=h)
        sensor_dir = output_dir / sensor
        processing = _processing_metadata(provider, sensor, config, contract, target_epsg, res)
        product = asdict(contract)

        if config.temporal_mode == "annual_composite":
            composites = {}
            fast_path = getattr(provider, "read_annual_composite", None)
            for year in range(config.date_range.start.year, config.date_range.end.year + 1):
                data = None
                scenes_manifest: Optional[list] = None
                used_fast_path = False
                if fast_path is not None:
                    data = fast_path(
                        config.aoi.bbox, sensor, year,
                        config.date_range.season_start, config.date_range.season_end,
                        raw_bands_needed, grid,
                        config.filters.max_cloud_percent, config.filters.pixel_cloud_mask,
                        config.reduce,
                    )
                    used_fast_path = data is not None
                if data is None:
                    data, refs, excluded, failed = _composite_from_scenes(
                        provider, config, sensor, year, raw_bands_needed, grid,
                        contract.processing_version_policy,
                    )
                    scenes_manifest = [
                        _scene_manifest_record(r, provider, output_dir, config.output.write_files, snapshot_cache)
                        for r in refs
                    ]
                    if excluded:
                        excluded_versions_by_sensor.setdefault(sensor, []).extend(excluded)
                    if failed:
                        failed_scenes_by_sensor.setdefault(sensor, []).extend(
                            {"sceneId": sid, "error": msg, "year": year} for sid, msg in failed
                        )
                if data is None:
                    continue
                if used_fast_path:
                    # Ask the provider whether it can tell us which images
                    # actually fed its server-side reduce (see gee.py's
                    # read_annual_composite/last_composite_scenes) -- this
                    # is a batched, pre-reduce metadata recovery, not a
                    # per-pixel one, so it doesn't undermine the fast path.
                    # A provider that can't/doesn't report this (the
                    # attribute is simply absent) leaves scenes_manifest
                    # None, and compositeNote explains why below.
                    fast_path_refs = getattr(provider, "last_composite_scenes", None)
                    scenes_manifest = (
                        [
                            _scene_manifest_record(r, provider, output_dir, config.output.write_files, snapshot_cache)
                            for r in fast_path_refs
                        ]
                        if fast_path_refs else None
                    )

                bands_out, indices_out = _split_outputs(data, config.output)
                composites[year] = {"bands": bands_out, "indices": indices_out}

                composite_note = (
                    None if scenes_manifest is not None else
                    "server-side composite (provider read_annual_composite fast path) -- "
                    "provider does not report which images fed the reduce, so no "
                    "per-scene contributing list is available for this composite"
                )

                if config.output.write_files:
                    if bands_out:
                        p = sensor_dir / f"bands_{year}.tif"
                        names = _write_geotiff(p, bands_out, grid)
                        manifest_entries.append(_manifest_entry(
                            p, output_dir, "bands", names, grid, sensor, config.provider,
                            product, processing, scenes_manifest,
                            year=year, reduce=config.reduce,
                            pixelCloudMask=config.filters.pixel_cloud_mask,
                            compositeNote=composite_note,
                        ))
                    if indices_out:
                        p = sensor_dir / f"indices_{year}.tif"
                        names = _write_geotiff(p, indices_out, grid)
                        manifest_entries.append(_manifest_entry(
                            p, output_dir, "indices", names, grid, sensor, config.provider,
                            product, processing, scenes_manifest,
                            year=year, reduce=config.reduce,
                            pixelCloudMask=config.filters.pixel_cloud_mask,
                            compositeNote=composite_note,
                        ))
            result[sensor] = {"grid": grid, "composites": composites}

        else:  # scene mode
            refs = provider.search_scenes(
                config.aoi.bbox, sensor, config.date_range.start, config.date_range.end,
                config.date_range.season_start, config.date_range.season_end,
                config.filters.max_cloud_percent, contract.processing_version_policy,
            )
            excluded = getattr(provider, "last_excluded_versions", [])
            if excluded:
                excluded_versions_by_sensor.setdefault(sensor, []).extend(excluded)

            scenes_out, scene_manifest_entries, failed = _run_scene_mode(
                provider, config, sensor, sensor_dir, refs, raw_bands_needed, grid,
                output_dir, product, processing, snapshot_cache,
            )
            manifest_entries.extend(scene_manifest_entries)
            if failed:
                failed_scenes_by_sensor.setdefault(sensor, []).extend(
                    {"sceneId": sid, "error": msg} for sid, msg in failed
                )
            result[sensor] = {"grid": grid, "scenes": scenes_out}

    if failed_scenes_by_sensor:
        total_failed = sum(len(v) for v in failed_scenes_by_sensor.values())
        print(f"[engine] {total_failed} scene(s) failed and were excluded from output -- see failedScenes in manifest.json")

    if config.output.write_files:
        write_manifest(
            output_dir / "manifest.json", manifest_entries,
            excludedProcessingVersions=excluded_versions_by_sensor,
            failedScenes=failed_scenes_by_sensor,
        )
    return result
