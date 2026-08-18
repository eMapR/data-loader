"""Orchestration: for each configured sensor, ask the configured provider
for either one composite per year or every individual scene, split the
result into requested raw-band/index outputs, and (if `output.write_files`)
write GeoTIFFs + a manifest entry per file.

Provider-agnostic by construction: this module only calls
`Provider.search_scenes` / `read_scene_bands` / (optionally)
`read_annual_composite` — it doesn't know or care whether those are backed
by a STAC endpoint or Earth Engine.
"""
from __future__ import annotations

import re
import warnings
from datetime import date
from pathlib import Path

import numpy as np

from data_loader.aoi import auto_utm_epsg, target_grid
from data_loader.config import Config
from data_loader.indices import compute_index, required_raw_bands
from data_loader.manifest import write_manifest
from data_loader.providers import get_provider
from data_loader.providers.base import Grid


def _sanitize(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", s)


def _split_outputs(raw_bands, output):
    bands_out = {b: raw_bands[b] for b in output.bands} if output.bands else {}
    indices_out = {i: compute_index(i, raw_bands) for i in output.indices} if output.indices else {}
    return bands_out, indices_out


def _composite_from_scenes(provider, config: Config, sensor, year, raw_bands_needed, grid):
    year_start = max(config.date_range.start, date(year, 1, 1))
    year_end = min(config.date_range.end, date(year, 12, 31))
    if year_start > year_end:
        return None
    refs = provider.search_scenes(
        config.aoi.bbox, sensor, year_start, year_end,
        config.date_range.season_start, config.date_range.season_end,
        config.filters.max_cloud_percent,
    )
    if not refs:
        return None
    stacks = {b: [] for b in raw_bands_needed}
    for scene in refs:
        bands = provider.read_scene_bands(scene, sensor, raw_bands_needed, grid, config.filters.pixel_cloud_mask)
        for b in raw_bands_needed:
            stacks[b].append(bands[b])
    reducer = np.nanmedian if config.reduce == "median" else np.nanmean
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return {b: reducer(np.stack(arrs), axis=0).astype("f4") for b, arrs in stacks.items()}


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


def _manifest_entry(path, output_dir, kind, band_names, grid, sensor, provider_name, **extra):
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
    }


def run(config: Config) -> dict:
    provider = get_provider(config.provider, **config.provider_options)
    output_dir = Path(config.output.dir)
    manifest_entries: list[dict] = []
    result: dict = {}

    raw_bands_needed = sorted(set(config.output.bands) | required_raw_bands(config.output.indices))

    for sensor_spec in config.sensors:
        sensor = sensor_spec.name
        res = sensor_spec.resolution()
        target_epsg = config.output.target_epsg or auto_utm_epsg(config.aoi.bbox)
        transform, w, h = target_grid(config.aoi.bbox, target_epsg, res)
        grid = Grid(crs=target_epsg, transform=transform, width=w, height=h)
        sensor_dir = output_dir / sensor

        if config.temporal_mode == "annual_composite":
            composites = {}
            fast_path = getattr(provider, "read_annual_composite", None)
            for year in range(config.date_range.start.year, config.date_range.end.year + 1):
                data = None
                if fast_path is not None:
                    data = fast_path(
                        config.aoi.bbox, sensor, year,
                        config.date_range.season_start, config.date_range.season_end,
                        raw_bands_needed, grid,
                        config.filters.max_cloud_percent, config.filters.pixel_cloud_mask,
                        config.reduce,
                    )
                if data is None:
                    data = _composite_from_scenes(provider, config, sensor, year, raw_bands_needed, grid)
                if data is None:
                    continue

                bands_out, indices_out = _split_outputs(data, config.output)
                composites[year] = {"bands": bands_out, "indices": indices_out}

                if config.output.write_files:
                    if bands_out:
                        p = sensor_dir / f"bands_{year}.tif"
                        names = _write_geotiff(p, bands_out, grid)
                        manifest_entries.append(_manifest_entry(
                            p, output_dir, "bands", names, grid, sensor, config.provider,
                            year=year, reduce=config.reduce,
                            pixelCloudMask=config.filters.pixel_cloud_mask,
                        ))
                    if indices_out:
                        p = sensor_dir / f"indices_{year}.tif"
                        names = _write_geotiff(p, indices_out, grid)
                        manifest_entries.append(_manifest_entry(
                            p, output_dir, "indices", names, grid, sensor, config.provider,
                            year=year, reduce=config.reduce,
                            pixelCloudMask=config.filters.pixel_cloud_mask,
                        ))
            result[sensor] = {"grid": grid, "composites": composites}

        else:  # scene mode
            refs = provider.search_scenes(
                config.aoi.bbox, sensor, config.date_range.start, config.date_range.end,
                config.date_range.season_start, config.date_range.season_end,
                config.filters.max_cloud_percent,
            )
            scenes_out = []
            for scene in refs:
                data = provider.read_scene_bands(
                    scene, sensor, raw_bands_needed, grid, config.filters.pixel_cloud_mask
                )
                bands_out, indices_out = _split_outputs(data, config.output)
                scenes_out.append({
                    "date": scene.date, "id": scene.id,
                    "bands": bands_out, "indices": indices_out,
                })

                if config.output.write_files:
                    tag = f"{scene.date.isoformat()}_{_sanitize(scene.id)}"
                    if bands_out:
                        p = sensor_dir / f"bands_{tag}.tif"
                        names = _write_geotiff(p, bands_out, grid)
                        manifest_entries.append(_manifest_entry(
                            p, output_dir, "bands", names, grid, sensor, config.provider,
                            date=scene.date.isoformat(), sceneId=scene.id,
                            cloudPercent=scene.cloud_percent,
                            pixelCloudMask=config.filters.pixel_cloud_mask,
                        ))
                    if indices_out:
                        p = sensor_dir / f"indices_{tag}.tif"
                        names = _write_geotiff(p, indices_out, grid)
                        manifest_entries.append(_manifest_entry(
                            p, output_dir, "indices", names, grid, sensor, config.provider,
                            date=scene.date.isoformat(), sceneId=scene.id,
                            cloudPercent=scene.cloud_percent,
                            pixelCloudMask=config.filters.pixel_cloud_mask,
                        ))
            result[sensor] = {"grid": grid, "scenes": scenes_out}

    if config.output.write_files:
        write_manifest(output_dir / "manifest.json", manifest_entries)
    return result
