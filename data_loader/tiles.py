"""Provider tile grids: named, fixed tiles a dataset can be built from
(`aoi.tiles`), read on the provider's own pixel grid with no resampling.

A provider supports tiles by exposing a `tile_grid` attribute with this
module's TileGrid interface. Only USGS Landsat ARD has one today; another
tiled product (e.g. Sentinel-2 MGRS from Copernicus) would add its own.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from data_loader.providers.base import Grid


@dataclass(frozen=True)
class TileGrid:
    """Interface + shared bits. Subclasses implement parse/bbox_lonlat/
    default_grid and say how a discovered item maps to a tile."""

    system: str
    description: str

    def validate(self, tile_id: str) -> None:
        self.parse(tile_id)

    def parse(self, tile_id: str):  # pragma: no cover - interface
        raise NotImplementedError

    def bbox_lonlat(self, tile_id: str) -> tuple[float, float, float, float]:  # pragma: no cover
        raise NotImplementedError

    def default_grid(self, tile_id: str) -> Grid:  # pragma: no cover
        raise NotImplementedError

    def describe(self, tile_id: str) -> dict:  # pragma: no cover
        raise NotImplementedError


# CONUS ARD grid: Albers Equal-Area Conic (WGS84), 5000 x 5000 px tiles at
# 30 m, upper-left corner of tile h000v000 at (-2565585, 3314805). Matches
# every LandsatLook STAC item's proj:transform (e.g. h003v004 ->
# -2115585, 2714805). h runs 0-32 west to east, v 0-21 north to south.
ARD_CONUS_CRS = ("+proj=aea +lat_0=23 +lon_0=-96 +lat_1=29.5 +lat_2=45.5 "
                 "+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs")
ARD_ULX, ARD_ULY = -2565585.0, 3314805.0
ARD_TILE_PX, ARD_RES_M = 5000, 30.0
ARD_TILE_M = ARD_TILE_PX * ARD_RES_M
ARD_MAX_H, ARD_MAX_V = 32, 21
_ARD_TILE_RE = re.compile(r"h(\d{3})v(\d{3})")


@dataclass(frozen=True)
class UsgsArdConusGrid(TileGrid):
    system: str = "usgs_ard_conus"
    description: str = "USGS Landsat ARD CONUS tile grid (Albers Equal-Area, 5000x5000 px at 30 m)"

    def parse(self, tile_id: str) -> tuple[int, int]:
        m = _ARD_TILE_RE.fullmatch(tile_id)
        if not m:
            raise ValueError(f"tile {tile_id!r}: expected a CONUS ARD tile id like 'h003v004'")
        h, v = int(m.group(1)), int(m.group(2))
        if h > ARD_MAX_H or v > ARD_MAX_V:
            raise ValueError(f"tile {tile_id!r}: outside the CONUS ARD grid (h 0-{ARD_MAX_H}, v 0-{ARD_MAX_V})")
        return h, v

    def bounds(self, tile_id: str) -> tuple[float, float, float, float]:
        """(xmin, ymin, xmax, ymax) in the ARD Albers CRS."""
        h, v = self.parse(tile_id)
        x0, y1 = ARD_ULX + h * ARD_TILE_M, ARD_ULY - v * ARD_TILE_M
        return x0, y1 - ARD_TILE_M, x0 + ARD_TILE_M, y1

    def bbox_lonlat(self, tile_id: str) -> tuple[float, float, float, float]:
        """lon/lat bbox enclosing the tile, from densified edges (a straight
        Albers edge is curved in lon/lat, so corners alone aren't enough)."""
        import numpy as np
        from pyproj import Transformer

        xmin, ymin, xmax, ymax = self.bounds(tile_id)
        e = np.linspace(0, ARD_TILE_M, 21)
        xs = np.concatenate([xmin + e, xmin + e, np.full(21, xmin), np.full(21, xmax)])
        ys = np.concatenate([np.full(21, ymax), np.full(21, ymin), ymax - e, ymax - e])
        lons, lats = Transformer.from_crs(ARD_CONUS_CRS, "EPSG:4326", always_xy=True).transform(xs, ys)
        return float(min(lons)), float(min(lats)), float(max(lons)), float(max(lats))

    def default_grid(self, tile_id: str) -> Grid:
        from rasterio.transform import from_origin

        xmin, _, _, ymax = self.bounds(tile_id)
        return Grid(crs=ARD_CONUS_CRS, transform=from_origin(xmin, ymax, ARD_RES_M, ARD_RES_M),
                    width=ARD_TILE_PX, height=ARD_TILE_PX)

    def grid_from_item(self, tile_id: str, item) -> Grid:
        """The tile's grid exactly as the source files declare it (STAC
        proj:wkt2/proj:transform/proj:shape), so native-grid reads compare
        equal to the files and need no warp. Falls back to default_grid."""
        from rasterio.transform import Affine

        props = getattr(item, "properties", None) or {}
        wkt, transform, shape = props.get("proj:wkt2"), props.get("proj:transform"), props.get("proj:shape")
        if not wkt or not transform or not shape:
            return self.default_grid(tile_id)
        grid = Grid(crs=wkt, transform=Affine(*transform[:6]), width=int(shape[1]), height=int(shape[0]))
        if not grid.transform.almost_equals(self.default_grid(tile_id).transform):
            raise ValueError(f"[usgs_ard] item {getattr(item, 'id', '?')} does not sit on tile {tile_id}'s grid")
        return grid

    def describe(self, tile_id: str) -> dict:
        h, v = self.parse(tile_id)
        return {"system": self.system, "id": tile_id, "h": h, "v": v, "region": "CU",
                "boundsAlbers": list(self.bounds(tile_id)), "bboxLonLat": list(self.bbox_lonlat(tile_id))}


def tile_id_for(h: int, v: int) -> str:
    return f"h{h:03d}v{v:03d}"


def item_tile_id(item) -> Optional[str]:
    """CONUS ARD tile id of a LandsatLook STAC item, from its grid props."""
    props = getattr(item, "properties", None) or {}
    h, v = props.get("landsat:grid_horizontal"), props.get("landsat:grid_vertical")
    if h is None or v is None:
        return None
    return tile_id_for(int(h), int(v))
