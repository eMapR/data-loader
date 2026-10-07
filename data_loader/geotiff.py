"""GeoTIFF writing for every output DataLoader produces.

Files are written to a temporary name and renamed into place, so a file
under its final name is always complete -- a run killed mid-write leaves
only a `*.tmp` behind, which the next run overwrites.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional

import numpy as np

from data_loader.providers.base import Grid


def _profile(grid: Grid, count: int, dtype: str, nodata) -> dict:
    return {
        "driver": "GTiff", "dtype": dtype, "count": count,
        "height": grid.height, "width": grid.width, "crs": grid.crs, "transform": grid.transform,
        "nodata": nodata, "compress": "deflate", "predictor": 3 if dtype.startswith("float") else 2,
        "tiled": True, "blockxsize": 256, "blockysize": 256, "bigtiff": "IF_SAFER",
        # Deflate is the slow part of writing a full ARD tile (~16 s single
        # threaded); GDAL compresses blocks in parallel with this.
        "num_threads": "ALL_CPUS",
    }


def write_raster(path: Path, bands: dict[str, np.ndarray], grid: Grid, *, dtype: str, nodata,
                 scales: Optional[dict] = None, offsets: Optional[dict] = None,
                 tags: Optional[dict] = None) -> list[str]:
    """Write `bands` (name -> 2-D array, in order) as one multi-band GeoTIFF.
    Band descriptions are the band names; scales/offsets (per band name)
    go into the standard GDAL band metadata. Returns the band order."""
    import rasterio

    names = list(bands)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with rasterio.open(tmp, "w", **_profile(grid, len(names), dtype, nodata)) as dst:
        for i, n in enumerate(names, start=1):
            dst.write(np.asarray(bands[n]).astype(dtype, copy=False), i)
            dst.set_band_description(i, n)
        if scales is not None:
            dst.scales = [float(scales.get(n, 1.0)) for n in names]
        if offsets is not None:
            dst.offsets = [float(offsets.get(n, 0.0)) for n in names]
        if tags:
            dst.update_tags(**{k: str(v) for k, v in tags.items() if v is not None})
    os.replace(tmp, path)
    return names


def write_float32(path: Path, bands: dict[str, np.ndarray], grid: Grid, tags: Optional[dict] = None) -> list[str]:
    """Float32, NaN nodata (reflectance, indices, composites)."""
    return write_raster(path, bands, grid, dtype="float32", nodata=np.nan, tags=tags)


def sha256(path: Path, chunk: int = 8 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()
