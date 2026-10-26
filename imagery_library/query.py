"""Find imagery by AOI and date range, without knowing tile ids or which
project first acquired it. Reads only the catalog (no imagery, no network).

    from imagery_library import open_library
    lib = open_library("/path/to/library")
    r = lib.query("my_aoi.geojson", start="2000-06-01", end="2020-09-30")
    r.tiles          # GeoDataFrame: tiles the AOI touches, with aoi_share
    r.observations   # DataFrame: one row per observation (date, platform, cloud, path, ...)
    r.paths()        # absolute GeoTIFF paths
    r.missing        # tiles the library does not hold yet (ask a maintainer)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from imagery_library.grids import tiles_for_aoi, to_equal_area
from imagery_library.layout import Library, LibraryError


def read_aoi(aoi):
    """A lon/lat shapely geometry from a vector file path, a GeoDataFrame /
    GeoSeries, a shapely geometry (lon/lat) or a (west, south, east, north) bbox."""
    import geopandas as gpd
    from shapely.geometry import box
    from shapely.geometry.base import BaseGeometry

    if isinstance(aoi, (str, Path)):
        g = gpd.read_file(aoi)
        if g.crs is None:
            raise LibraryError(f"{aoi}: no CRS; save the AOI with its CRS (GeoJSON is lon/lat by definition)")
        return g.to_crs(4326).geometry.union_all() if hasattr(g.geometry, "union_all") else \
            g.to_crs(4326).geometry.unary_union
    if isinstance(aoi, (gpd.GeoDataFrame, gpd.GeoSeries)):
        g = aoi.to_crs(4326)
        geom = g.geometry if isinstance(g, gpd.GeoDataFrame) else g
        return geom.union_all() if hasattr(geom, "union_all") else geom.unary_union
    if isinstance(aoi, BaseGeometry):
        return aoi
    if isinstance(aoi, (tuple, list)) and len(aoi) == 4:
        return box(*aoi)
    raise LibraryError(f"AOI {type(aoi).__name__}: expected a vector file, GeoDataFrame, geometry or bbox")


def _iso(d) -> Optional[str]:
    return d.isoformat() if isinstance(d, date) else (str(d) if d is not None else None)


@dataclass
class QueryResult:
    library: Path
    aoi: object
    tiles: object                   # GeoDataFrame
    observations: object            # DataFrame
    covered_share: float            # share of the AOI inside tiles the library holds (for the chosen products)
    missing: list = field(default_factory=list)   # [{"product", "grid_system", "tile", "aoi_share"}]
    unavailable_tiles: list = field(default_factory=list)  # catalogued, imagery not present here

    def paths(self) -> list[Path]:
        return [self.library / p for p in self.observations["path"]]

    def summary(self) -> str:
        n_tiles = len(self.tiles)
        tb = float(self.observations["bytes"].sum()) / 1e12 if len(self.observations) else 0.0
        lines = [f"AOI intersects {n_tiles} library tile(s): "
                 + ", ".join(f"{r.grid_system}/{r.tile} ({r.aoi_share:.1%})" for r in self.tiles.itertuples()),
                 f"{len(self.observations)} observations, {tb:.3f} TB"
                 + (f", {self.observations['date'].min()} .. {self.observations['date'].max()}"
                    if len(self.observations) else ""),
                 f"AOI covered by library tiles: {self.covered_share:.1%}"]
        if self.missing:
            lines.append("missing (not in the library yet): "
                         + ", ".join(f"{m['grid_system']}/{m['tile']} ({m['aoi_share']:.1%})" for m in self.missing))
        if self.unavailable_tiles:
            lines.append("catalogued but imagery not present here: " + ", ".join(self.unavailable_tiles))
        return "\n".join(lines)


def query(lib: Library, aoi, *, start=None, end=None, product: Optional[str] = None, version: Optional[str] = None,
          platforms: Optional[list] = None, max_cloud_percent: Optional[float] = None,
          min_valid_fraction: Optional[float] = None, months: Optional[list] = None) -> QueryResult:
    import geopandas as gpd
    import pandas as pd

    from imagery_library.catalog import current_catalog

    cat = current_catalog(lib)
    geom = read_aoi(aoi)
    if geom.is_empty:
        raise LibraryError("AOI is empty")
    tiles = gpd.read_parquet(cat / "tiles.parquet")
    if product is not None:
        tiles = tiles[tiles["product"] == product]
    if version is not None:
        tiles = tiles[tiles["version"] == version]
    hit = tiles[tiles.intersects(geom)].copy()
    aoi_ea = to_equal_area(geom)
    hit["aoi_share"] = [to_equal_area(g).intersection(aoi_ea).area / aoi_ea.area for g in hit.geometry]
    hit = hit[hit["aoi_share"] > 0].sort_values("aoi_share", ascending=False)

    obs = pd.read_parquet(cat / "observations.parquet")
    keys = set(zip(hit["product"], hit["version"], hit["grid_system"], hit["tile"]))
    if len(obs):
        obs = obs[[k in keys for k in zip(obs["product"], obs["version"], obs["grid_system"], obs["tile"])]]
        if start is not None:
            obs = obs[obs["date"] >= _iso(start)]
        if end is not None:
            obs = obs[obs["date"] <= _iso(end)]
        if platforms:
            obs = obs[obs["platform"].isin(list(platforms))]
        if max_cloud_percent is not None:
            obs = obs[obs["cloud_percent"].isna() | (obs["cloud_percent"] <= max_cloud_percent)]
        if min_valid_fraction is not None:
            obs = obs[obs["valid_fraction"].isna() | (obs["valid_fraction"] >= min_valid_fraction)]
        if months:
            obs = obs[pd.to_datetime(obs["date"]).dt.month.isin(list(months))]
        obs = obs.sort_values(["date", "tile"]).reset_index(drop=True)

    products = [p for p in lib.products() if (product is None or p.name == product)
                and (version is None or p.version == version)]
    missing, covered = [], 0.0
    for p in products:
        mine = hit[(hit["product"] == p.name) & (hit["version"] == p.version)]
        union = to_equal_area(mine.geometry.union_all() if hasattr(mine.geometry, "union_all")
                              else mine.geometry.unary_union) if len(mine) else None
        share = union.intersection(aoi_ea).area / aoi_ea.area if union is not None else 0.0
        covered = max(covered, share)
        held = set(mine["tile"])
        for system in p.grid_systems:
            for t in tiles_for_aoi(system, geom) or []:
                if t["tile"] not in held:
                    missing.append({"product": p.key, **t})
    unavailable = sorted(f"{r.grid_system}/{r.tile}" for r in hit.itertuples() if not r.imagery_present)
    return QueryResult(lib.root, geom, hit.reset_index(drop=True), obs, covered, missing, unavailable)
