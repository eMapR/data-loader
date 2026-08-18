"""Config-driven, multi-provider imagery data loader.

Point it at a YAML/JSON config (AOI, sensor(s), date range, provider,
annual-composite-vs-scene, raw bands and/or indices) and it writes plain
GeoTIFFs + a manifest.json to an output directory — usable from any
downstream geoprocessing program, not just Python.

    python -m data_loader --config examples/annual_composite.yaml

For in-process use:

    from data_loader import load_config, run

    result = run(load_config("examples/annual_composite.yaml"))
"""
from __future__ import annotations

from data_loader.config import load_config
from data_loader.engine import run

__all__ = [
    "load_config",
    "run",
    "fetch_annual_nbr",
    "pixel_index",
]


def fetch_annual_nbr(
    bbox,
    start,
    end,
    *,
    target_epsg=None,
    res=30.0,
    max_cloud=60.0,
    months=(6, 7, 8, 9),
    provider="planetary_computer",
    cache_path=None,
    **kwargs,
):
    """Back-compat shim for the original single-purpose loader.

    Wraps the new config-driven engine to reproduce the old call shape
    and return shape (annual NBR array, years, transform) for existing
    callers (e.g. `example.py`) — new code should build a Config via
    `load_config`/`Config(...)` instead, which supports scene mode,
    multiple sensors, raw bands, and other indices.
    """
    from datetime import date

    import numpy as np

    from data_loader.config import AOI, Config, DateRange, Filters, OutputSpec, SensorSpec

    season_start = f"{months[0]:02d}-01" if months else None
    season_end = f"{months[-1]:02d}-28" if months else None

    west, south, east, north = bbox
    cfg = Config(
        aoi=AOI(upper_left=(west, north), lower_right=(east, south)),
        provider=provider,
        sensors=[SensorSpec(name="landsat", resolution_m=res)],
        date_range=DateRange(
            start=date(start, 1, 1), end=date(end, 12, 31),
            season_start=season_start, season_end=season_end,
        ),
        filters=Filters(max_cloud_percent=max_cloud, pixel_cloud_mask=True),
        temporal_mode="annual_composite",
        output=OutputSpec(bands=(), indices=("nbr",), dir="__fetch_annual_nbr_tmp__",
                           target_epsg=target_epsg, write_files=False),
    )
    result = run(cfg)
    landsat = result["landsat"]
    years = np.asarray(sorted(landsat["composites"]), dtype=np.int32)
    annual = np.stack([landsat["composites"][y]["indices"]["nbr"] for y in years]).astype("f4")
    return annual, years, landsat["grid"].transform


def pixel_index(transform, lon, lat, target_epsg):
    """Lon/lat (EPSG:4326) -> (row, col) into an array on `transform`'s grid."""
    from pyproj import Transformer
    from rasterio.transform import rowcol

    tf = Transformer.from_crs("EPSG:4326", target_epsg, always_xy=True)
    x, y = tf.transform(lon, lat)
    r, c = rowcol(transform, x, y)
    return int(r), int(c)
