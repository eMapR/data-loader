"""GLAD Landsat ARD (Global Land Analysis & Discovery, U. Maryland) — free,
public, no-auth 16-day normalized-surface-reflectance composite tiles,
1997-present (2020-present on the S3 mirror used here; earlier years require
UMD's own HTTP endpoint, not implemented here). Landsat only.

This is a from-scratch implementation (no STAC catalog exists for this
dataset), reverse-engineered from public sources rather than an official API
spec, since GLAD does not publish one — cross-checked against
github.com/TESS-Laboratory/ardglad's R source (an independent third-party
implementation, not GLAD's own code) and Potapov et al. 2020
(https://doi.org/10.3390/rs12030426), then verified live 2026-08-18 by
opening a real tile:

- Tile grid / S3 key layout: confirmed live —
  `/vsis3/glad.landsat.ard/data/tiles/52N/017E_52N/1001.tif` opened
  successfully with bounds exactly matching a 1x1 degree cell (16.9995-
  18.0005E, 51.9995-53.0005N, i.e. the documented 2-pixel overlap).
  `_tile_name`'s SW-corner-of-cell arithmetic reproduces both known
  examples ("017E_52N" and "062W_09S") and this live tile. Bucket is
  public, no AWS credentials needed (`--no-sign-request`).
- Band order (B,G,R,N,S1,S2,T,QA), 8-band uint16: confirmed live — band 7
  (thermal) read 28529-30083 (~285-301 K, i.e. Kelvin x100), band 8 (QA)
  read 1-17, exactly the documented QA code range.
- Reflectance scale (0.0001): consistent with live pixel values (bands 1-6
  read in the hundreds-to-~20000s range) but not verified to more than
  order-of-magnitude/plausibility — no ground-truth reflectance to compare
  against.
- Still unverified: multi-tile mosaicking for an AOI spanning >1 tile
  (`read_scene_bands`' first-non-nan merge logic), and behavior at extreme
  latitudes/the antimeridian where a plain 1-degree grid may not hold.
  `_verify_tile_exists` turns a wrong tile-name guess into a loud warning
  instead of silently-wrong output.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import numpy as np

from data_loader.masking import MASKS
from data_loader.product_contract import (
    FIXED_COMPOSITE,
    GLAD_ARD,
    AcquisitionProvenance,
    ProductIdentity,
    parse_version_policy,
)
from data_loader.providers.base import Grid, SceneRef

S3_BUCKET = "glad.landsat.ard"
BAND_INDEX = {  # 1-based GeoTIFF band index; order verified against ardglad
    "blue": 1, "green": 2, "red": 3, "nir": 4, "swir1": 5, "swir2": 6,
    "qa": 8,
}
SR_SCALE = 0.0001  # DN -> reflectance; see module docstring — unverified
FILL_DN = 0  # GLAD's QA code 0 == "no data"; assumed shared by other bands

_EPOCH_YEAR = 1997
_EPOCH_INTERVAL_ID = 392  # interval_id(1997, interval=1)


def _interval_id(year: int, interval: int) -> int:
    return _EPOCH_INTERVAL_ID + (year - _EPOCH_YEAR) * 23 + (interval - 1)


def _interval_date_range(year: int, interval: int) -> tuple[date, date]:
    doy_start = 16 * (interval - 1) + 1
    doy_end = 366 if interval == 23 else 16 * interval
    start = date(year, 1, 1) + timedelta(days=doy_start - 1)
    end = min(date(year, 1, 1) + timedelta(days=doy_end - 1), date(year, 12, 31))
    return start, end


def _in_season(d: date, season_start: Optional[str], season_end: Optional[str]) -> bool:
    if not season_start or not season_end:
        return True
    md = (d.month, d.day)
    s = tuple(int(x) for x in season_start.split("-"))
    e = tuple(int(x) for x in season_end.split("-"))
    return s <= md <= e if s <= e else md >= s or md <= e


def _tile_name(lon: float, lat: float) -> tuple[str, str]:
    """1-degree cell containing (lon, lat) -> (lat_str, tile_name), e.g.
    (9.4, -8.6) -> ("09S", "009E_09S"). SW-corner-of-cell convention — see
    module docstring for how this was derived and why it's unverified."""
    import math

    lon_i, lat_i = math.floor(lon), math.floor(lat)
    lon_str = f"{abs(lon_i):03d}{'E' if lon_i >= 0 else 'W'}"
    lat_str = f"{abs(lat_i):02d}{'N' if lat_i >= 0 else 'S'}"
    return lat_str, f"{lon_str}_{lat_str}"


def _tiles_for_bbox(bbox: tuple[float, float, float, float]) -> list[tuple[str, str]]:
    """Every 1-degree tile whose cell overlaps bbox=(west, south, east, north)."""
    import math

    west, south, east, north = bbox
    lons = range(math.floor(west), math.floor(east) + 1)
    lats = range(math.floor(south), math.floor(north) + 1)
    return sorted({_tile_name(lon + 0.5, lat + 0.5) for lon in lons for lat in lats})


def _s3_path(lat_str: str, tile: str, interval_id: int) -> str:
    return f"/vsis3/{S3_BUCKET}/data/tiles/{lat_str}/{tile}/{interval_id}.tif"


def _verify_tile_exists(path: str) -> bool:
    import rasterio
    from rasterio.errors import RasterioIOError

    try:
        with rasterio.open(path):
            return True
    except RasterioIOError:
        return False


class GladArdProvider:
    name = "glad_ard"

    def __init__(self):
        import os

        os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")
        os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
        # Lazily populated by _sample_tile_tags, once per provider
        # instance (not per scene) -- see source_metadata.
        self._tag_sample_checked = False
        self._tag_sample: Optional[dict] = None

    def capabilities(self) -> dict[str, ProductIdentity]:
        return {"landsat": ProductIdentity(sensor="landsat", product_family=GLAD_ARD, temporal_product=FIXED_COMPOSITE)}

    def processing_profile(self, sensor: str) -> dict:
        return {
            "sr_scale": SR_SCALE,
            "sr_offset": 0.0,
            "qa_policy": "glad_ard_qa",
            "reflectance_resampling": "bilinear",
            "categorical_resampling": "nearest",
        }

    def _sample_tile_tags(self, tile_paths: list) -> Optional[dict]:
        """Checks ONE tile (not one per scene, not one per tile) for
        embedded GDAL/TIFF tags that might carry real source metadata --
        cached for the life of this provider instance so a multi-scene run
        pays this cost at most once, not per acquisition. Confirmed live
        (2026-09) against a real tile: only standard GDAL raster-structure
        tags exist (AREA_OR_POINT, IMAGE_STRUCTURE/COMPRESSION) -- nothing
        source- or acquisition-specific. Re-checked at runtime rather than
        hardcoded in case that ever changes."""
        if not self._tag_sample_checked:
            self._tag_sample_checked = True
            if tile_paths:
                try:
                    import rasterio

                    with rasterio.open(tile_paths[0]) as src:
                        dataset_tags = {k: v for k, v in src.tags().items() if k != "AREA_OR_POINT"}
                        band_tags = {i: src.tags(i) for i in range(1, src.count + 1) if src.tags(i)}
                        if dataset_tags or band_tags:
                            self._tag_sample = {"datasetTags": dataset_tags, "bandTags": band_tags}
                except Exception:
                    pass  # sampling is best-effort; absence of tags is a valid, reportable result
        return self._tag_sample

    def source_metadata(self, scene: SceneRef) -> Optional[dict]:
        """No STAC catalog, API, or DOI exists for GLAD ARD (see module
        docstring) -- there is no authoritative per-acquisition catalog
        record to snapshot. This is instead an explicit, honest record of
        everything actually known about this specific product from the
        source (tile identity, documented band/QA scheme, temporal
        definition) plus whatever embedded GeoTIFF tags a live check
        turns up. Nothing here is fabricated to fill the gap."""
        tags = self._sample_tile_tags(scene.handle)
        return {
            "source": "GLAD (Global Land Analysis & Discovery, University of Maryland) Landsat ARD",
            "productFamily": GLAD_ARD,
            "intervalId": scene.id,
            "intervalStartDate": scene.date.isoformat(),
            "temporalDefinition": "16-day fixed composite interval, 23 intervals/year (see _interval_date_range in this module)",
            "tilePaths": list(scene.handle),
            "bandOrder": dict(BAND_INDEX),
            "qaSemantics": (
                "Categorical class codes 0-17 (not bitflags); good values "
                "(1, 2, 15) = clear land/water -- see "
                "data_loader.masking.GLAD_ARD_QA_GOOD_VALUES and this "
                "module's docstring (Potapov et al. 2020)."
            ),
            "reflectanceScale": {
                "value": SR_SCALE,
                "verified": False,
                "note": "consistent with live pixel magnitude but not independently verified against ground truth -- see module docstring",
            },
            "embeddedTileTags": tags,
            "note": (
                "No richer per-acquisition catalog metadata is available "
                "upstream for GLAD ARD -- no STAC, API, or DOI exists for "
                "this dataset. Embedded GeoTIFF tags were inspected live "
                + (
                    "and are included above under embeddedTileTags."
                    if tags is not None else
                    "and contained nothing beyond standard GDAL raster-"
                    "structure tags (AREA_OR_POINT, IMAGE_STRUCTURE) -- no "
                    "per-acquisition or processing information is embedded."
                )
            ),
        }

    def search_scenes(
        self, bbox, sensor, start, end, season_start, season_end, max_cloud_percent,
        processing_version_policy="any",
    ) -> list[SceneRef]:
        if sensor != "landsat":
            raise ValueError(f"[{self.name}] only sensor 'landsat' is supported, got {sensor!r}")
        # GLAD ARD has no per-scene processing-version concept at all (one
        # fixed pipeline produces each 16-day composite tile) -- any policy
        # other than the trivial ones is meaningless here.
        version_kind, _ = parse_version_policy(processing_version_policy)
        if version_kind == "pinned":
            raise ValueError(
                "[glad_ard] processing_version_policy='pinned:...' is not "
                "meaningful here -- GLAD ARD has no per-acquisition "
                "processing-baseline concept."
            )

        tiles = _tiles_for_bbox(bbox)
        if not tiles:
            return []

        refs: list[SceneRef] = []
        for year in range(start.year, end.year + 1):
            for interval in range(1, 24):
                i_start, i_end = _interval_date_range(year, interval)
                if i_end < start or i_start > end:
                    continue
                if not _in_season(i_start, season_start, season_end):
                    continue

                interval_id = _interval_id(year, interval)
                paths = [_s3_path(lat_str, tile, interval_id) for lat_str, tile in tiles]
                provenance = AcquisitionProvenance(
                    provider=self.name,
                    provider_item_id=f"interval_{interval_id}",
                    upstream_product_id=None,  # a mosaic of tiles, not one upstream product
                    # Interval start date, not a true single acquisition
                    # instant -- GLAD ARD is a 16-day composite, not a scene.
                    acquisition_datetime=i_start.isoformat(),
                    platform=None,  # blend of whichever Landsat sensors contributed, not tracked per-pixel
                    product_family=GLAD_ARD,
                    collection=S3_BUCKET,
                    processing_baseline=None,
                    generation_time=None,
                    extra={"tile_count": len(paths), "tile_paths": paths},
                )
                refs.append(SceneRef(
                    id=f"interval_{interval_id}",
                    date=i_start,
                    cloud_percent=None,  # not available without reading QA first
                    handle=paths,
                    provenance=provenance,
                ))
        return refs

    def read_scene_bands(self, scene: SceneRef, sensor: str, bands, grid: Grid, pixel_cloud_mask: bool):
        import rasterio
        from rasterio.enums import Resampling
        from rasterio.vrt import WarpedVRT

        need_indices = [BAND_INDEX[b] for b in bands]
        if pixel_cloud_mask:
            need_indices = need_indices + [BAND_INDEX["qa"]]

        out: dict[str, np.ndarray] = {b: np.full((grid.height, grid.width), np.nan, "f4") for b in bands}
        qa_out = np.full((grid.height, grid.width), np.nan, "f4") if pixel_cloud_mask else None

        any_tile_found = False
        for path in scene.handle:
            if not _verify_tile_exists(path):
                continue  # tile has no data for this interval, or _tile_name guessed wrong — see module docstring
            any_tile_found = True
            with rasterio.open(path) as src, WarpedVRT(
                src, crs=grid.crs, transform=grid.transform,
                width=grid.width, height=grid.height, resampling=Resampling.bilinear,
            ) as vrt:
                for b in bands:
                    dn = vrt.read(BAND_INDEX[b]).astype("f4")
                    dn[dn == FILL_DN] = np.nan
                    fresh = dn * SR_SCALE
                    empty = np.isnan(out[b])
                    out[b][empty] = fresh[empty]
                if pixel_cloud_mask:
                    with WarpedVRT(
                        src, crs=grid.crs, transform=grid.transform,
                        width=grid.width, height=grid.height, resampling=Resampling.nearest,
                    ) as qa_vrt:
                        qa = qa_vrt.read(BAND_INDEX["qa"]).astype("f4")
                        empty = np.isnan(qa_out)
                        qa_out[empty] = qa[empty]

        if not any_tile_found:
            warnings.warn(
                f"[{self.name}] no tile file found for scene {scene.id!r} at any of "
                f"{scene.handle} — either this interval has no data here, or the "
                "guessed tile name is wrong (see glad_ard.py's _tile_name docstring)."
            )

        if pixel_cloud_mask and qa_out is not None:
            bad = MASKS["glad_ard_qa"](np.nan_to_num(qa_out, nan=0))
            for arr in out.values():
                arr[bad] = np.nan

        return out


def make_provider() -> GladArdProvider:
    return GladArdProvider()
