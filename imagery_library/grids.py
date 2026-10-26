"""Tile geometry, for any grid DataLoader records, plus per-grid-system
helpers for naming tiles the library does not hold yet.

Footprints come from the manifest's own grid record (CRS + transform +
size), so they work for every grid a DataLoader tile dataset can have;
only `tiles_for_aoi` needs to know a grid system's layout, and grid
systems without a helper simply report uncovered area without tile ids.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
from shapely.geometry import Polygon, box
from shapely.ops import transform as shp_transform

EQUAL_AREA = "EPSG:6933"  # global equal-area CRS for area shares


def footprint_lonlat(grid: dict, n: int = 21) -> Polygon:
    """The tile's outline in lon/lat, densified (a straight projected edge
    is curved in lon/lat). `grid` is a manifest grids.<sensor>.<tile> record."""
    from pyproj import Transformer
    from rasterio.crs import CRS

    a, b, c, d, e, f = grid["transform"][:6]
    w, h = grid["width"], grid["height"]
    t = np.linspace(0, 1, n)
    cols = np.concatenate([t * w, np.full(n, w), (1 - t) * w, np.zeros(n)])
    rows = np.concatenate([np.zeros(n), t * h, np.full(n, h), (1 - t) * h])
    xs, ys = a * cols + b * rows + c, d * cols + e * rows + f
    crs = CRS.from_wkt(grid["crsWkt"]) if grid.get("crsWkt") else CRS.from_user_input(grid["epsg"])
    lons, lats = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform(xs, ys)
    return _split_antimeridian(np.asarray(lons), np.asarray(lats))


def _split_antimeridian(lons, lats):
    """A ring that crosses +/-180 (westernmost Aleutian tiles) becomes two
    polygons, one each side, instead of one spanning the whole globe."""
    if lons.max() - lons.min() <= 180:
        return Polygon(zip(lons, lats))
    from shapely.affinity import translate
    from shapely.geometry import MultiPolygon

    ring = Polygon(zip(np.where(lons < 0, lons + 360, lons), lats))
    east = ring.intersection(box(0, -90, 180, 90))
    west = translate(ring.intersection(box(180, -90, 360, 90)), xoff=-360)
    parts = [g for g in (east, west) if not g.is_empty]
    return MultiPolygon([p for g in parts for p in getattr(g, "geoms", [g])])


def to_equal_area(geom):
    from pyproj import Transformer

    tr = Transformer.from_crs("EPSG:4326", EQUAL_AREA, always_xy=True)
    return shp_transform(tr.transform, geom)


# -- per grid system ---------------------------------------------------------

def _ard_region(system: str):
    from data_loader.tiles import ARD_REGIONS

    for r in ARD_REGIONS.values():
        if r.system == system:
            return r
    return None


def tiles_for_aoi(system: str, aoi_lonlat) -> Optional[list[dict]]:
    """Tiles of `system` that intersect the AOI, with the share of the AOI
    in each; None if the library has no helper for this grid system."""
    from pyproj import Transformer

    from data_loader.tiles import ARD_TILE_M, tile_id_for

    r = _ard_region(system)
    if r is None:
        return None
    tr = Transformer.from_crs("EPSG:4326", r.crs, always_xy=True)
    g = shp_transform(tr.transform, aoi_lonlat)
    if g.is_empty or g.area == 0:
        return []
    xmin, ymin, xmax, ymax = g.bounds
    out = []
    h0, h1 = max(0, math.floor((xmin - r.ulx) / ARD_TILE_M)), min(r.max_h, math.floor((xmax - r.ulx) / ARD_TILE_M))
    v0, v1 = max(0, math.floor((r.uly - ymax) / ARD_TILE_M)), min(r.max_v, math.floor((r.uly - ymin) / ARD_TILE_M))
    for h in range(h0, h1 + 1):
        for v in range(v0, v1 + 1):
            t = box(r.ulx + h * ARD_TILE_M, r.uly - (v + 1) * ARD_TILE_M, r.ulx + (h + 1) * ARD_TILE_M,
                    r.uly - v * ARD_TILE_M)
            a = g.intersection(t).area
            if a > 0:
                out.append({"grid_system": system, "tile": tile_id_for(h, v, r.code), "aoi_share": a / g.area})
    return sorted(out, key=lambda x: -x["aoi_share"])


def grid_system_of(tile: str, systems: list) -> tuple[str, str]:
    """(grid system, canonical tile id) for a tile id, among a product's grid systems."""
    from data_loader.tiles import UsgsArdGrid

    grid = UsgsArdGrid()
    try:
        system, canonical = grid.region(tile).system, grid.canonical(tile)
    except ValueError:
        system, canonical = None, tile
    if system in systems:
        return system, canonical
    if len(systems) == 1 and system is None:
        return systems[0], canonical
    raise ValueError(f"tile {tile!r}: not on any of this product's grids ({', '.join(systems)})")
