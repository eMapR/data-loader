"""Find imagery by AOI and date range, without knowing tile ids or which
project first acquired it. Reads only the catalog (no imagery, no network).

    from imagery_library import open_library
    lib = open_library("/path/to/library")
    r = lib.query("my_aoi.geojson", start="2000-06-01", end="2020-09-30")
    print(r.summary())
    r.tiles          # GeoDataFrame: tiles the AOI touches, aoi_share, acquisition/verification state
    r.observations   # DataFrame: one row per observation (date, platform, cloud, path, verified, ...)
    r.paths()        # absolute GeoTIFF paths
    r.missing        # tiles the source publishes that the library does not hold yet (ask a maintainer)

Every tile carries two states (see catalog.py): acquisition (complete /
incomplete / updating) and verification (verified / partially_verified /
failed / unverified); every observation a `verified` flag.
`verified_only=True` returns only observations a checksum verify has covered.
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

    def union(s):
        return s.union_all() if hasattr(s, "union_all") else s.unary_union

    if isinstance(aoi, (str, Path)):
        g = gpd.read_file(aoi)
        if g.crs is None:
            raise LibraryError(f"{aoi}: no CRS; save the AOI with its CRS (GeoJSON is lon/lat by definition)")
        return union(g.to_crs(4326).geometry)
    if isinstance(aoi, (gpd.GeoDataFrame, gpd.GeoSeries)):
        g = aoi.to_crs(4326)
        return union(g.geometry if isinstance(g, gpd.GeoDataFrame) else g)
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
    covered_share: float            # share of the AOI inside tiles the library holds
    published_share: Optional[float] = None  # share inside tiles the source publishes (None: unknown)
    missing: list = field(default_factory=list)   # [{"product", "grid_system", "tile", "aoi_share"}]
    unavailable_tiles: list = field(default_factory=list)  # catalogued, imagery not present here
    excluded_unverified: int = 0    # observations dropped by verified_only

    def paths(self) -> list[Path]:
        return [self.library / p for p in self.observations["path"]]

    def summary(self) -> str:
        o = self.observations
        tb = float(o["bytes"].sum()) / 1e12 if len(o) else 0.0
        lines = [f"AOI intersects {len(self.tiles)} library tile(s)" + (":" if len(self.tiles) else "")]
        for r in self.tiles.itertuples():
            n = int((o["tile"] == r.tile).sum()) if len(o) else 0
            state = r.acquisition_state
            if r.acquisition_state == "incomplete" and (r.failed or r.pending):
                state += f" ({r.failed} failed, {r.pending} pending)"
            ver = r.verification_state + (f" {str(r.last_verified)[:10]}" if r.last_verified else "")
            lines.append(f"  {r.grid_system}/{r.tile:<12} {r.aoi_share:6.1%} of AOI  {n:>6} obs  "
                         f"acquisition: {state:<12} verification: {ver}")
        nv = int(o["verified"].sum()) if len(o) else 0
        lines.append(f"{len(o)} observations ({nv} verified, {len(o) - nv} not yet verified), {tb:.3f} TB"
                     + (f", {o['date'].min()} .. {o['date'].max()}" if len(o) else ""))
        if self.excluded_unverified:
            lines.append(f"verified_only: left out {self.excluded_unverified} observations not yet covered by a verify")
        if (self.tiles["acquisition_state"] == "updating").any():
            lines.append("note: tiles marked 'updating' are being acquired now; their newest observations "
                         "appear after the next catalog build")
        lines.append(f"AOI covered by library tiles: {self.covered_share:.1%}")
        by_product: dict = {}
        for m in self.missing:
            by_product.setdefault(m["product"], []).append(m)
        for prod, ms in by_product.items():
            lines.append(f"missing from {prod} (published by the source, not in the library yet): "
                         + ", ".join(f"{m['grid_system']}/{m['tile']} ({m['aoi_share']:.1%})" for m in ms))
        if self.published_share is not None and self.published_share < 0.9995:
            lines.append(f"outside the product: {1 - self.published_share:.1%} of the AOI has no tile the source "
                         "publishes (ocean, or outside the source's coverage)")
        if self.unavailable_tiles:
            lines.append("catalogued but imagery not present here: " + ", ".join(self.unavailable_tiles))
        return "\n".join(lines)


def query(lib: Library, aoi, *, start=None, end=None, product: Optional[str] = None, version: Optional[str] = None,
          platforms: Optional[list] = None, max_cloud_percent: Optional[float] = None,
          min_valid_fraction: Optional[float] = None, months: Optional[list] = None,
          verified_only: bool = False) -> QueryResult:
    import geopandas as gpd
    import pandas as pd

    from imagery_library.catalog import current_catalog

    cat = current_catalog(lib)  # resolved once: this query reads one build throughout
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
    excluded = 0
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
        if verified_only:
            excluded = int((~obs["verified"]).sum())
            obs = obs[obs["verified"]]
        obs = obs.sort_values(["date", "tile"]).reset_index(drop=True)

    products = [p for p in lib.products() if (product is None or p.name == product)
                and (version is None or p.version == version)]
    missing, covered, published_share = [], 0.0, None
    for p in products:
        mine = hit[(hit["product"] == p.name) & (hit["version"] == p.version)]
        covered = max(covered, float(mine["aoi_share"].sum()))  # a grid's tiles do not overlap
        held = set(zip(mine["grid_system"], mine["tile"]))
        pub = p.published_tiles()
        share = 0.0
        known = True
        for system in p.grid_systems:
            cands = tiles_for_aoi(system, geom)
            if cands is None:
                known = False
                continue
            for t in cands:
                if pub is not None and t["tile"] not in pub.get(system, ()):
                    continue  # USGS has nothing there: not "missing"
                share += t["aoi_share"]
                if (system, t["tile"]) not in held:
                    missing.append({"product": p.key, **t})
        if known and pub is not None:
            published_share = max(published_share or 0.0, min(share, 1.0))
    unavailable = sorted(f"{r.grid_system}/{r.tile}" for r in hit.itertuples() if not r.imagery_present)
    return QueryResult(lib.root, geom, hit.reset_index(drop=True), obs, min(covered, 1.0), published_share,
                       missing, unavailable, excluded)
