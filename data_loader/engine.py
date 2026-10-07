"""The engine: config -> provider -> standardized imagery + manifest.

    plan, grids = discover(config)         # what exists (metadata only)
    summary = run(config)                  # discover + acquire what's missing

One code path for every kind of request. A small AOI pulled once and a
multi-decade tile archive differ only in configuration (aoi.tiles +
grid.crs: native + output.encoding: native); both produce the same
dataset layout and manifest (data_loader.dataset), and both are
resumable: re-running a config into the same output directory skips what
is already acquired, retries failures up to max_attempts, and adds
anything newly available (an open-ended or extended time range is how a
dataset is updated).

Provider-agnostic: the engine only calls the Provider interface
(providers/base.py). Which upstream product a provider supplies is checked
against the request before anything is fetched (product_contract), so a
provider that can't supply what was asked fails loudly instead of
substituting something else.

Units of work: one unit per (sensor, grid, acquisition) in scene mode, or
per (sensor, grid, season) in annual_composite mode. Scene units run in a
thread pool of `workers`; composite units run one at a time with their
scene reads spread over the pool. Every finished unit is journaled
immediately, so a killed run loses at most the units in flight.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from data_loader._version import __version__
from data_loader.aoi import auto_utm_epsg, target_grid
from data_loader.config import Config, ConfigError, SensorSpec, TimeWindow
from data_loader.dataset import DatasetWriter, open_dataset
from data_loader.geotiff import sha256, write_float32, write_raster
from data_loader.indices import compute_index, required_raw_bands
from data_loader.masking import MASKS, QA_DESCRIPTIONS, describe_mask, dn_to_reflectance
from data_loader.metadata_snapshot import write_snapshot
from data_loader.product_contract import ProductContract, resolve_contract
from data_loader.providers import get_provider
from data_loader.providers.base import Grid, SceneRef

CATALOG_EVERY_UNITS = 200
CATALOG_EVERY_S = 600
PROGRESS_EVERY_S = 60


@dataclass
class Unit:
    key: str
    kind: str  # "scene" | "composite"
    sensor: str
    grid_id: str
    grid: Grid
    window: TimeWindow
    scene: Optional[SceneRef] = None
    scenes: list = field(default_factory=list)  # composite candidates


@dataclass
class SensorPlan:
    sensor: str
    contract: ProductContract
    grids: dict  # grid_id -> Grid
    units: list
    filtered: list  # (SceneRef, grid_id, TimeWindow, reason)
    excluded_versions: list


@dataclass
class Plan:
    config: Config
    today: date
    windows: list
    sensors: list


@dataclass
class RunSummary:
    output_dir: Path
    manifest: Path
    counts: dict  # status -> rows, whole dataset
    acquired_now: int
    failed_now: int
    already_done: int
    elapsed_s: float

    @property
    def complete(self) -> bool:
        return self.counts.get("pending", 0) == 0 and self.counts.get("failed", 0) == 0


def _sanitize(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)


# -- request checks (no network) ---------------------------------------------

def _resolve_contract(provider, sensor_spec: SensorSpec) -> ProductContract:
    identity = provider.capabilities().get(sensor_spec.name)
    if identity is None:
        raise ConfigError(f"provider {provider.name!r} does not supply sensor {sensor_spec.name!r} "
                          f"(it supplies: {', '.join(sorted(provider.capabilities()))})")
    try:
        return resolve_contract(sensor_spec.name, identity, sensor_spec.product_family,
                                sensor_spec.processing_version_policy)
    except ValueError as e:
        raise ConfigError(str(e)) from None


def _supports_native(provider) -> bool:
    return callable(getattr(provider, "read_scene_native", None))


def make_provider(config: Config):
    try:
        return get_provider(config.provider, **config.provider_options)
    except (ValueError, TypeError) as e:
        raise ConfigError(f"provider: {e}") from None


def check_request(config: Config, provider=None) -> list[str]:
    """Raise ConfigError for anything this provider can't do as configured;
    return warnings (e.g. missing read credentials). No network access."""
    provider = provider or make_provider(config)
    for s in config.sensors:
        _resolve_contract(provider, s)
    if (config.output.encoding == "native" or config.output.qa_band) and not _supports_native(provider):
        what = "output.encoding: native" if config.output.encoding == "native" else "output.qa_band"
        raise ConfigError(f"{what}: provider {provider.name!r} can't return source values/QA; "
                          "supported by usgs_ard, planetary_computer and aws_earth_search")
    if config.aoi.tiles:
        tile_grid = getattr(provider, "tile_grid", None)
        if tile_grid is None or not callable(getattr(provider, "search_tile", None)):
            raise ConfigError(f"aoi.tiles: provider {provider.name!r} has no tile grid; use a bbox "
                              "(tiles are supported by usgs_ard)")
        for t in config.aoi.tiles:
            try:
                tile_grid.validate(t)
            except ValueError as e:
                raise ConfigError(f"aoi.tiles: {e}") from None
    check = getattr(provider, "credential_problems", None)
    return list(check([s.name for s in config.sensors])) if callable(check) else []


# -- discovery ------------------------------------------------------------------

def _bbox_grid(config: Config, sensor: str) -> tuple[Grid, dict]:
    crs = config.grid.crs if config.grid.crs != "auto" else auto_utm_epsg(config.aoi.bbox)
    res = config.grid.resolution(sensor)
    transform, w, h = target_grid(config.aoi.bbox, crs, res)
    grid = Grid(crs=crs, transform=transform, width=w, height=h)
    return grid, {"kind": "aoi", "crs": crs, "resolutionM": res, "transform": list(transform)[:6],
                  "width": w, "height": h,
                  "aoi": {"upperLeft": list(config.aoi.upper_left), "lowerRight": list(config.aoi.lower_right)}}


def _tile_grid_info(provider, tile_id: str, grid: Grid) -> dict:
    from rasterio.crs import CRS

    crs = CRS.from_user_input(grid.crs)
    return {"kind": "tile", "tile": provider.tile_grid.describe(tile_id), "crsWkt": crs.to_wkt(),
            "epsg": crs.to_epsg(), "resolutionM": abs(grid.transform.a),
            "transform": list(grid.transform)[:6], "width": grid.width, "height": grid.height}


def _fast_path_ok(provider, config: Config, grid_id: str, w: TimeWindow, today: date) -> bool:
    """A provider-side annual composite (GEE) computes one calendar year's
    season; use it only when that is exactly this window."""
    if not callable(getattr(provider, "read_annual_composite", None)) or grid_id != "aoi":
        return False
    season = config.time.season
    if season is not None and season.wraps:
        return False
    full = config.time._season_bounds(w.season_year)
    return w.start == full[0] and w.end == min(full[1], today)


def discover(config: Config, provider=None, *, today: Optional[date] = None,
             log: Callable[[str], None] = print) -> tuple[Plan, dict]:
    """Search the provider for everything the request covers. Returns the
    plan (units to produce, scenes filtered out) and grid descriptions."""
    provider = provider or make_provider(config)
    today = today or date.today()
    windows = config.time.windows(today)
    if not windows:
        raise ConfigError("time: the requested period has no dates up to today")
    start, end = windows[0].start, windows[-1].end
    window_of: dict = {}

    def window_for(d: date) -> Optional[TimeWindow]:
        if d not in window_of:
            window_of[d] = next((w for w in windows if w.contains(d)), None)
        return window_of[d]

    sensors, grid_infos = [], {}
    for spec in config.sensors:
        sensor = spec.name
        contract = _resolve_contract(provider, spec)
        groups = []  # (grid_id, Grid, refs)
        excluded = []
        if config.aoi.tiles:
            for tile in config.aoi.tiles:
                refs = provider.search_tile(tile, sensor, start, end, None)
                grid = (provider.tile_grid.grid_from_item(tile, refs[0].handle) if refs
                        else provider.tile_grid.default_grid(tile))
                groups.append((tile, grid, refs))
                grid_infos.setdefault(sensor, {})[tile] = _tile_grid_info(provider, tile, grid)
        else:
            grid, info = _bbox_grid(config, sensor)
            refs = provider.search_scenes(config.aoi.bbox, sensor, start, end, None, None, 100.0,
                                          contract.processing_version_policy)
            excluded = list(getattr(provider, "last_excluded_versions", []) or [])
            groups.append(("aoi", grid, refs))
            grid_infos.setdefault(sensor, {})["aoi"] = info

        units, filtered, grids = [], [], {}
        max_cloud = config.filters.max_cloud_percent
        for grid_id, grid, refs in groups:
            grids[grid_id] = grid
            by_season: dict[int, list] = {}
            in_windows = 0
            for ref in sorted(refs, key=lambda r: (r.date, r.id)):
                w = window_for(ref.date)
                if w is None:
                    continue  # outside the season windows
                in_windows += 1
                if max_cloud < 100 and ref.cloud_percent is not None and ref.cloud_percent > max_cloud:
                    filtered.append((ref, grid_id, w, f"scene cloud cover {ref.cloud_percent:.1f}% > "
                                                      f"filters.max_cloud_percent {max_cloud:g}"))
                elif config.temporal_mode == "scene":
                    units.append(Unit(key=f"{sensor}/{grid_id}/{ref.id}", kind="scene", sensor=sensor,
                                      grid_id=grid_id, grid=grid, window=w, scene=ref))
                else:
                    by_season.setdefault(w.season_year, []).append(ref)
            n_filtered = sum(1 for f in filtered if f[1] == grid_id)
            log(f"[discover] {sensor} {grid_id}: {in_windows} acquisitions in {len(windows)} time window(s) "
                f"{start}..{end}" + (f", {n_filtered} over the cloud filter" if n_filtered else ""))
            if config.temporal_mode == "annual_composite":
                for w in windows:
                    if w.season_year in by_season or _fast_path_ok(provider, config, grid_id, w, today):
                        units.append(Unit(key=f"{sensor}/{grid_id}/composite/{w.season_year}", kind="composite",
                                          sensor=sensor, grid_id=grid_id, grid=grid, window=w,
                                          scenes=by_season.get(w.season_year, [])))
        sensors.append(SensorPlan(sensor=sensor, contract=contract, grids=grids, units=units,
                                  filtered=filtered, excluded_versions=excluded))
    return Plan(config=config, today=today, windows=windows, sensors=sensors), grid_infos


# -- rows -----------------------------------------------------------------------

def _scene_row_base(unit: Unit, scene: SceneRef) -> dict:
    prov = scene.provenance
    extra = (prov.extra if prov is not None else {}) or {}
    return {
        "key": unit.key, "type": "scene", "sensor": unit.sensor, "grid": unit.grid_id,
        "seasonYear": unit.window.season_year, "date": scene.date.isoformat(),
        "datetime": prov.acquisition_datetime if prov is not None else None,
        "itemId": scene.id, "platform": prov.platform if prov is not None else None,
        "eo:cloud_cover": scene.cloud_percent, "fillPercent": extra.get("fill_percent"),
    }


def _composite_row_base(unit: Unit) -> dict:
    return {"key": unit.key, "type": "composite", "sensor": unit.sensor, "grid": unit.grid_id,
            "seasonYear": unit.window.season_year, "windowStart": unit.window.start.isoformat(),
            "windowEnd": unit.window.end.isoformat(), "candidateItems": sorted(s.id for s in unit.scenes)}


def _row_base(unit: Unit) -> dict:
    return _scene_row_base(unit, unit.scene) if unit.kind == "scene" else _composite_row_base(unit)


# -- acquisition ----------------------------------------------------------------

@dataclass
class _RunContext:
    config: Config
    provider: object
    root: Path
    today: date
    bands_needed: list
    snapshot_lock: threading.Lock = field(default_factory=threading.Lock)
    snapshots: dict = field(default_factory=dict)


def _source_record(ctx: _RunContext, scene: SceneRef) -> tuple[Optional[dict], Optional[str]]:
    """(provenance dict, path of the verbatim provider metadata snapshot).
    Each snapshot is fetched/written once per item per run."""
    prov = scene.provenance
    if prov is None:
        return None, None
    key = (prov.provider, prov.provider_item_id)
    with ctx.snapshot_lock:
        if key in ctx.snapshots:
            ref = ctx.snapshots[key]
        else:
            get = getattr(ctx.provider, "source_metadata", None)
            record = get(scene) if callable(get) else None
            ref = write_snapshot(ctx.root, prov.provider, prov.provider_item_id, record) if record is not None else None
            ctx.snapshots[key] = ref
    d = asdict(prov)
    d["source_metadata_ref"] = ref
    return d, ref


def _file_entry(ctx: _RunContext, path: Path, band_set: str, **extra) -> dict:
    return {"path": str(path.relative_to(ctx.root)), "bandSet": band_set, "bytes": path.stat().st_size,
            "sha256": sha256(path), **{k: v for k, v in extra.items() if v is not None}}


def _read_reflectance(ctx: _RunContext, scene: SceneRef, sensor: str, grid: Grid, want_qa: bool):
    """Float32 reflectance for ctx.bands_needed (+ raw QA if want_qa),
    masked per filters.pixel_cloud_mask."""
    cfg, provider = ctx.config, ctx.provider
    if _supports_native(provider):
        enc = provider.native_encoding(sensor)
        nr = provider.read_scene_native(scene, sensor, ctx.bands_needed, grid,
                                        want_qa or cfg.filters.pixel_cloud_mask)
        refl = {b: dn_to_reflectance(nr.bands[b], nr.scale[b], nr.offset[b]) for b in ctx.bands_needed}
        if cfg.filters.pixel_cloud_mask:
            bad = MASKS[enc.qa_kind](nr.qa)
            for arr in refl.values():
                arr[bad] = np.nan
        return refl, nr.qa
    return provider.read_scene_bands(scene, sensor, ctx.bands_needed, grid, cfg.filters.pixel_cloud_mask), None


def _acquire_scene(ctx: _RunContext, unit: Unit) -> dict:
    cfg, out = ctx.config, ctx.config.output
    scene, sensor, grid = unit.scene, unit.sensor, unit.grid
    stem = f"{sensor}/{unit.grid_id}/{scene.date.year}/{scene.date.isoformat()}_{_sanitize(scene.id)}"
    tags = {"DATALOADER_ITEM_ID": scene.id, "DATALOADER_DATE": scene.date.isoformat(),
            "DATALOADER_SENSOR": sensor, "DATALOADER_PROVIDER": cfg.provider, "DATALOADER_GRID": unit.grid_id}
    t0 = time.perf_counter()
    files = []
    if out.encoding == "native":
        enc = ctx.provider.native_encoding(sensor)
        nr = ctx.provider.read_scene_native(scene, sensor, list(out.bands), grid,
                                            out.qa_band or cfg.filters.pixel_cloud_mask)
        read_s = time.perf_counter() - t0
        arrays = {b: nr.bands[b] for b in out.bands}
        if cfg.filters.pixel_cloud_mask:
            bad = MASKS[enc.qa_kind](nr.qa)
            for arr in arrays.values():
                arr[bad] = nr.nodata
        valid = float(np.mean(arrays[out.bands[0]] != nr.nodata))
        scales, offsets = dict(nr.scale), dict(nr.offset)
        if out.qa_band:
            arrays[enc.qa_name] = nr.qa
            scales[enc.qa_name], offsets[enc.qa_name] = 1.0, 0.0
        path = ctx.root / f"{stem}_bands.tif"
        write_raster(path, arrays, grid, dtype=enc.data_type, nodata=nr.nodata, scales=scales, offsets=offsets,
                     tags=tags)
        varies = {b: [nr.scale[b], nr.offset[b]] for b in out.bands
                  if (nr.scale[b], nr.offset[b]) != (enc.scale, enc.offset)}
        files.append(_file_entry(ctx, path, "bands", bandScaleOffset=varies or None))
    else:
        refl, qa = _read_reflectance(ctx, scene, sensor, grid, out.qa_band)
        read_s = time.perf_counter() - t0
        first = None
        if out.bands:
            arrays = {b: refl[b] for b in out.bands}
            if out.qa_band:
                arrays[ctx.provider.native_encoding(sensor).qa_name] = qa.astype("float32")
            path = ctx.root / f"{stem}_bands.tif"
            write_float32(path, arrays, grid, tags=tags)
            files.append(_file_entry(ctx, path, "bands"))
            first = arrays[out.bands[0]]
        if out.indices:
            arrays = {i: compute_index(i, refl) for i in out.indices}
            path = ctx.root / f"{stem}_indices.tif"
            write_float32(path, arrays, grid, tags=tags)
            files.append(_file_entry(ctx, path, "indices"))
            first = first if first is not None else arrays[out.indices[0]]
        valid = float(np.mean(~np.isnan(first)))
    prov, ref = _source_record(ctx, scene)
    return {**_scene_row_base(unit, scene), "status": "acquired", "validFraction": round(valid, 6),
            "files": files, "provenance": prov, "sourceMetadata": ref,
            "timing": {"readS": round(read_s, 2), "totalS": round(time.perf_counter() - t0, 2)}}


def _acquire_composite(ctx: _RunContext, unit: Unit, pool: ThreadPoolExecutor) -> dict:
    cfg, out, provider = ctx.config, ctx.config.output, ctx.provider
    w, sensor, grid = unit.window, unit.sensor, unit.grid
    t0 = time.perf_counter()
    data, used, failed_sources = None, [], []
    method = "local"
    if _fast_path_ok(provider, cfg, unit.grid_id, w, ctx.today):
        season = cfg.time.season
        data = provider.read_annual_composite(
            cfg.aoi.bbox, sensor, w.season_year, season.start if season else None, season.end if season else None,
            ctx.bands_needed, grid, cfg.filters.max_cloud_percent, cfg.filters.pixel_cloud_mask, cfg.reduce)
        if data is not None:
            method = "provider"
            used = list(getattr(provider, "last_composite_scenes", None) or [])
    if data is None:
        futures = {pool.submit(_read_reflectance, ctx, s, sensor, grid, False): s for s in unit.scenes}
        results = {}
        for fu in as_completed(futures):
            s = futures[fu]
            try:
                results[s.id] = fu.result()[0]
            except Exception as e:
                failed_sources.append({"itemId": s.id, "error": f"{type(e).__name__}: {e}"[:500]})
        used = [s for s in unit.scenes if s.id in results]  # discovery order, not completion order
        if not used:
            raise RuntimeError(f"no scene of season {w.season_year} could be read "
                               f"({len(failed_sources)} of {len(unit.scenes)} failed)")
        reducer = np.nanmedian if cfg.reduce == "median" else np.nanmean
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            data = {b: reducer(np.stack([results[s.id][b] for s in used]), axis=0).astype("f4")
                    for b in ctx.bands_needed}
        failed_sources.sort(key=lambda f: f["itemId"])
    stem = f"{sensor}/{unit.grid_id}/composites/{w.season_year}_{cfg.reduce}"
    tags = {"DATALOADER_SEASON_YEAR": w.season_year, "DATALOADER_WINDOW": f"{w.start}/{w.end}",
            "DATALOADER_REDUCER": cfg.reduce, "DATALOADER_SENSOR": sensor, "DATALOADER_PROVIDER": cfg.provider}
    files, first = [], None
    if out.bands:
        arrays = {b: data[b] for b in out.bands}
        path = ctx.root / f"{stem}_bands.tif"
        write_float32(path, arrays, grid, tags=tags)
        files.append(_file_entry(ctx, path, "bands"))
        first = arrays[out.bands[0]]
    if out.indices:
        arrays = {i: compute_index(i, data) for i in out.indices}
        path = ctx.root / f"{stem}_indices.tif"
        write_float32(path, arrays, grid, tags=tags)
        files.append(_file_entry(ctx, path, "indices"))
        first = first if first is not None else arrays[out.indices[0]]
    sources = []
    for s in used:
        prov, ref = _source_record(ctx, s)
        sources.append({"itemId": s.id, "date": s.date.isoformat(), "eo:cloud_cover": s.cloud_percent,
                        "provenance": prov, "sourceMetadata": ref})
    row = {**_composite_row_base(unit), "status": "acquired", "method": method,
           "validFraction": round(float(np.mean(~np.isnan(first))), 6), "files": files,
           "sources": sources, "failedSources": failed_sources,
           "timing": {"totalS": round(time.perf_counter() - t0, 2)}}
    if method == "provider" and not sources:
        row["note"] = "provider-side composite; the provider did not report which images contributed"
    return row


# -- manifest -------------------------------------------------------------------

def _git_commit() -> Optional[str]:
    try:
        return subprocess.run(["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=5).stdout.strip() or None
    except Exception:
        return None


def _band_sets(config: Config, provider, sensor: str) -> dict:
    out = config.output
    bands = []
    if out.encoding == "native":
        enc = provider.native_encoding(sensor)
        for i, b in enumerate(out.bands, 1):
            entry = {"index": i, "name": b, "role": "reflectance", "dataType": enc.data_type,
                     "scale": enc.scale, "offset": enc.offset, "nodata": enc.nodata,
                     "unit": "surface reflectance = value * scale + offset"}
            if sensor == "sentinel2":
                entry["scaleOffsetVaries"] = ("per file, see files[].bandScaleOffset (processing baseline "
                                              ">= 04.00 uses offset -0.1)")
            bands.append(entry)
    else:
        for i, b in enumerate(out.bands, 1):
            bands.append({"index": i, "name": b, "role": "reflectance", "dataType": "float32", "scale": 1.0,
                          "offset": 0.0, "nodata": "NaN", "unit": "surface reflectance"})
    if out.qa_band:
        enc = provider.native_encoding(sensor)
        bands.append({"index": len(bands) + 1, "name": enc.qa_name, "role": "qa",
                      "dataType": enc.qa_data_type if out.encoding == "native" else "float32",
                      "scale": 1.0, "offset": 0.0, "nodata": None, "qa": QA_DESCRIPTIONS[enc.qa_kind],
                      "note": "raw source QA values, unmodified" + (
                          "" if out.encoding == "native" else " (stored as float32; values are exact integers)")})
    indices = [{"index": i, "name": n, "role": "index", "dataType": "float32", "nodata": "NaN"}
               for i, n in enumerate(out.indices, 1)]
    return {"bands": bands, "indices": indices}


def _qa_kind(provider, sensor: str) -> Optional[str]:
    if _supports_native(provider):
        return provider.native_encoding(sensor).qa_kind
    profile = getattr(provider, "processing_profile", None)
    return profile(sensor).get("qa_policy") if callable(profile) else None


def manifest_core(plan: Plan, provider, grid_infos: dict) -> dict:
    cfg = plan.config
    sensors = [sp.sensor for sp in plan.sensors]
    if cfg.filters.pixel_cloud_mask:
        mask = {"applied": True, "rules": {s: describe_mask(_qa_kind(provider, s)) for s in sensors
                                           if _qa_kind(provider, s) in QA_DESCRIPTIONS}}
    else:
        mask = {"applied": False, "note": "no cloud/shadow masking: every source pixel is kept"
                                          + ("; the QA band is stored for downstream masking" if cfg.output.qa_band
                                             else "")}
    profile = getattr(provider, "processing_profile", None)
    return {
        "dataset": {"dataloaderVersion": __version__, "gitCommit": _git_commit()},
        "request": cfg.to_dict(),
        "products": {sp.sensor: {"sensor": sp.sensor, "provider": cfg.provider,
                                 "productFamily": sp.contract.product_family,
                                 "temporalProduct": sp.contract.temporal_product,
                                 "processingVersionPolicy": sp.contract.processing_version_policy}
                     for sp in plan.sensors},
        "processing": {
            "temporalMode": cfg.temporal_mode,
            "reducer": cfg.reduce if cfg.temporal_mode == "annual_composite" else None,
            "encoding": cfg.output.encoding,
            "sceneCloudFilter": {"maxCloudPercent": cfg.filters.max_cloud_percent,
                                 "note": "scenes above this are listed in items.jsonl with status 'filtered'; "
                                         "scenes with unknown cloud cover are kept"},
            "pixelCloudMask": mask,
            "fill": "source fill (SR DN 0) is nodata in every encoding",
            "resampling": ("none: read on the provider's native tile grid" if cfg.aoi.tiles else
                           "bilinear for reflectance, nearest for QA"),
            "normalizationApplied": [],
            "providerProfile": {s: dict(profile(s)) for s in sensors} if callable(profile) else {},
        },
        "bandSets": {s: _band_sets(cfg, provider, s) for s in sensors},
        "grids": grid_infos,
        "coverage": {
            "timeWindows": [{"seasonYear": w.season_year, "start": w.start.isoformat(), "end": w.end.isoformat()}
                            for w in plan.windows],
            "openEnded": cfg.time.open_ended,
            "discoveredThrough": plan.today.isoformat(),
            "excludedProcessingVersions": {sp.sensor: sp.excluded_versions for sp in plan.sensors
                                           if sp.excluded_versions},
        },
    }


# -- run ------------------------------------------------------------------------

def run(config: Config, *, today: Optional[date] = None, log: Callable[[str], None] = print,
        provider=None) -> RunSummary:
    """Discover and acquire everything `config` asks for that isn't already
    in its output directory. Safe to re-run; see the module docstring."""
    t_start = time.perf_counter()
    provider = provider or make_provider(config)
    problems = check_request(config, provider)
    if problems:
        raise ConfigError("cannot read from this provider yet:\n  " + "\n  ".join(problems))
    root = Path(config.output.dir)
    writer = DatasetWriter(root, config.to_dict())
    try:
        plan, grid_infos = discover(config, provider, today=today, log=log)
        core = manifest_core(plan, provider, grid_infos)
        ctx = _RunContext(config=config, provider=provider, root=root, today=plan.today,
                          bands_needed=list(config.output.bands) + sorted(
                              required_raw_bands(config.output.indices) - set(config.output.bands)))

        todo, already = [], 0
        for sp in plan.sensors:
            for ref, grid_id, w, reason in sp.filtered:
                u = Unit(key=f"{sp.sensor}/{grid_id}/{ref.id}", kind="scene", sensor=sp.sensor, grid_id=grid_id,
                         grid=sp.grids[grid_id], window=w, scene=ref)
                writer.note({**_scene_row_base(u, ref), "status": "filtered", "reason": reason})
            for u in sp.units:
                old = writer.row(u.key) or {}
                stale_composite = (u.kind == "composite" and old.get("status") == "acquired"
                                   and old.get("candidateItems") != sorted(s.id for s in u.scenes))
                if not stale_composite and writer.is_acquired(u.key):
                    already += 1
                    continue
                attempts = 0 if old.get("status") == "acquired" else int(old.get("attempts", 0))
                if attempts >= config.max_attempts:
                    writer.note({**_row_base(u), "status": "failed", "attempts": attempts, "error": old.get("error")})
                    continue
                writer.note({**_row_base(u), "status": "pending", "attempts": attempts})
                todo.append((u, attempts))
        writer.write_catalog(core, complete=False)
        log(f"[run] {len(todo)} to acquire, {already} already done -> {root}")

        counter = {"done": 0, "failed": 0}
        last = {"catalog": time.monotonic(), "progress": time.monotonic()}
        t_work = time.perf_counter()

        def finish(u: Unit, attempts: int, row: Optional[dict], err: Optional[BaseException]):
            if err is not None:
                row = {**_row_base(u), "status": "failed", "attempts": attempts + 1,
                       "error": f"{type(err).__name__}: {err}"[:1000]}
                counter["failed"] += 1
                log(f"[run] {u.key} FAILED (attempt {attempts + 1}/{config.max_attempts}): {row['error']}")
            else:
                row["attempts"] = attempts + 1
                counter["done"] += 1
            writer.record(row)
            now = time.monotonic()
            if now - last["progress"] >= PROGRESS_EVERY_S:
                last["progress"] = now
                n = counter["done"] + counter["failed"]
                rate = (time.perf_counter() - t_work) / n
                log(f"[run] {n}/{len(todo)} ({counter['failed']} failed), {rate:.1f} s/unit, "
                    f"~{rate * (len(todo) - n) / 3600:.1f} h left")
            if writer.since_catalog >= CATALOG_EVERY_UNITS or now - last["catalog"] >= CATALOG_EVERY_S:
                last["catalog"] = now
                writer.write_catalog(core, complete=False)

        with ThreadPoolExecutor(max_workers=config.workers) as pool:
            try:
                if config.temporal_mode == "scene":
                    futures = {pool.submit(_acquire_scene, ctx, u): (u, a) for u, a in todo}
                    for fu in as_completed(futures):
                        u, a = futures[fu]
                        try:
                            row, err = fu.result(), None
                        except Exception as e:
                            row, err = None, e
                        finish(u, a, row, err)
                else:
                    for u, a in todo:
                        try:
                            row, err = _acquire_composite(ctx, u, pool), None
                        except Exception as e:
                            row, err = None, e
                        finish(u, a, row, err)
            except BaseException:
                pool.shutdown(wait=True, cancel_futures=True)
                writer.write_catalog(core, complete=False)
                raise

        counts = writer.counts()
        manifest = writer.write_catalog(core, complete=counts["pending"] == 0 and counts["failed"] == 0)
        log(f"[run] done: {counter['done']} acquired, {counter['failed']} failed this run; dataset: "
            + ", ".join(f"{k} {v}" for k, v in counts.items() if v))
        return RunSummary(output_dir=root, manifest=manifest, counts=counts, acquired_now=counter["done"],
                          failed_now=counter["failed"], already_done=already,
                          elapsed_s=time.perf_counter() - t_start)
    finally:
        writer.close()


def plan_summary(config: Config, *, today: Optional[date] = None, log: Callable[[str], None] = print) -> dict:
    """Discovery only: what the request covers and how much of it the
    output directory already has. Reads nothing but metadata."""
    provider = make_provider(config)
    warnings_ = check_request(config, provider)
    plan, _ = discover(config, provider, today=today, log=log)
    acquired_keys = set()
    root = Path(config.output.dir)
    if (root / "manifest.json").exists():
        acquired_keys = {r["key"] for r in open_dataset(root).items(status="acquired")}
    rows = []
    for sp in plan.sensors:
        for grid_id in sp.grids:
            units = [u for u in sp.units if u.grid_id == grid_id]
            rows.append({"sensor": sp.sensor, "grid": grid_id, "units": len(units),
                         "acquired": sum(u.key in acquired_keys for u in units),
                         "filtered": sum(1 for f in sp.filtered if f[1] == grid_id)})
    return {"span": (plan.windows[0].start, plan.windows[-1].end), "windows": len(plan.windows),
            "temporalMode": config.temporal_mode, "grids": rows, "warnings": warnings_}
