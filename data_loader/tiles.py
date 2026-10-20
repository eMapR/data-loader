"""Provider tile grids: named, fixed tiles a dataset can be built from
(`aoi.tiles`), read on the provider's own pixel grid with no resampling.

A provider supports tiles by exposing a `tile_grid` attribute with this
module's TileGrid interface. Only USGS Landsat ARD has one today; another
tiled product (e.g. Sentinel-2 MGRS from Copernicus) would add its own.

USGS ARD has one native grid per region (CONUS, Alaska, Hawaii), each with
its own Albers projection and origin, and each numbering its tiles from
h000v000. The same h/v therefore names a different place in each region,
so a tile id carries its region: `AK_h021v008`, `HI_h001v001`,
`CU_h003v004` -- the order of USGS's own product ids
(`LC09_AK_021008_...`). A bare `h003v004` means CONUS, as it always has;
CONUS tiles are recorded in that bare form so existing datasets (keys,
paths, manifests) are unchanged. Every other region must be named.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NamedTuple, Optional

from data_loader.providers.base import Grid


@dataclass(frozen=True)
class TileGrid:
    """Interface + shared bits. Subclasses implement parse/bbox_lonlat/
    default_grid and say how a discovered item maps to a tile."""

    system: str
    description: str

    def validate(self, tile_id: str) -> None:
        self.parse(tile_id)

    def canonical(self, tile_id: str) -> str:
        """The one spelling a tile is recorded under (keys, paths,
        manifest). Validates `tile_id`."""
        self.parse(tile_id)
        return tile_id

    def parse(self, tile_id: str):  # pragma: no cover - interface
        raise NotImplementedError

    def bbox_lonlat(self, tile_id: str) -> tuple[float, float, float, float]:  # pragma: no cover
        raise NotImplementedError

    def default_grid(self, tile_id: str) -> Grid:  # pragma: no cover
        raise NotImplementedError

    def describe(self, tile_id: str) -> dict:  # pragma: no cover
        raise NotImplementedError


@dataclass(frozen=True)
class ArdRegion:
    """One USGS ARD native grid: Albers Equal-Area (WGS84), 5000 x 5000 px
    tiles at 30 m, h running west to east and v north to south from the
    upper-left corner of tile h000v000."""

    code: str      # landsat:grid_region, and the tile id prefix
    name: str
    system: str    # manifest grids.<tile>.tile.system
    crs: str
    ulx: float
    uly: float
    max_h: int
    max_v: int


# Origins match every LandsatLook STAC item's proj:transform, e.g. CONUS
# h003v004 -> (-2115585, 2714805), Alaska h020v008 -> (798285, 1274325),
# Hawaii h001v001 -> (-294345, 2018895). h/v ranges are the tiles USGS
# publishes items for (STAC landsat:grid_horizontal/vertical, 2026-10).
ARD_REGIONS = {
    "CU": ArdRegion("CU", "CONUS", "usgs_ard_conus",
                    "+proj=aea +lat_0=23 +lon_0=-96 +lat_1=29.5 +lat_2=45.5 "
                    "+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs",
                    -2565585.0, 3314805.0, 32, 21),
    "AK": ArdRegion("AK", "Alaska", "usgs_ard_alaska",
                    "+proj=aea +lat_0=50 +lon_0=-154 +lat_1=55 +lat_2=65 "
                    "+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs",
                    -2201715.0, 2474325.0, 25, 13),
    "HI": ArdRegion("HI", "Hawaii", "usgs_ard_hawaii",
                    "+proj=aea +lat_0=3 +lon_0=-157 +lat_1=8 +lat_2=18 "
                    "+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs",
                    -444345.0, 2168895.0, 4, 2),
}
DEFAULT_REGION = "CU"

# CONUS names kept for callers that predate the other regions.
ARD_CONUS_CRS = ARD_REGIONS["CU"].crs
ARD_ULX, ARD_ULY = ARD_REGIONS["CU"].ulx, ARD_REGIONS["CU"].uly
ARD_TILE_PX, ARD_RES_M = 5000, 30.0
ARD_TILE_M = ARD_TILE_PX * ARD_RES_M
ARD_MAX_H, ARD_MAX_V = ARD_REGIONS["CU"].max_h, ARD_REGIONS["CU"].max_v
_ARD_TILE_RE = re.compile(r"(?:([A-Z]{2})_)?h(\d{3})v(\d{3})")
_TILE_FORMS = "'h003v004' (CONUS) or region-qualified 'CU_h003v004', 'AK_h021v008', 'HI_h001v001'"


class ArdTile(NamedTuple):
    region: str
    h: int
    v: int


@dataclass(frozen=True)
class UsgsArdGrid(TileGrid):
    system: str = "usgs_ard"
    description: str = ("USGS Landsat ARD native tile grids (CONUS, Alaska, Hawaii; Albers Equal-Area, "
                        "5000x5000 px at 30 m)")

    def parse(self, tile_id: str) -> ArdTile:
        m = _ARD_TILE_RE.fullmatch(tile_id) if isinstance(tile_id, str) else None
        if not m:
            hint = ""
            if isinstance(tile_id, str) and re.fullmatch(r"[A-Za-z]{2}[:\-/ ]?h\d{3}v\d{3}", tile_id):
                hint = f" (write the region in capitals with an underscore, e.g. '{tile_id[:2].upper()}_{tile_id[-8:]}')"
            raise ValueError(f"tile {tile_id!r}: expected an ARD tile id like {_TILE_FORMS}{hint}")
        code = m.group(1) or DEFAULT_REGION
        region = ARD_REGIONS.get(code)
        if region is None:
            raise ValueError(f"tile {tile_id!r}: unknown ARD grid region {code!r} "
                             f"(known: {', '.join(sorted(ARD_REGIONS))})")
        h, v = int(m.group(2)), int(m.group(3))
        if h > region.max_h or v > region.max_v:
            raise ValueError(f"tile {tile_id!r}: outside the {region.name} ARD grid "
                             f"(h 0-{region.max_h}, v 0-{region.max_v})")
        return ArdTile(code, h, v)

    def canonical(self, tile_id: str) -> str:
        t = self.parse(tile_id)
        return tile_id_for(t.h, t.v, t.region)

    def region(self, tile_id: str) -> ArdRegion:
        return ARD_REGIONS[self.parse(tile_id).region]

    def bounds(self, tile_id: str) -> tuple[float, float, float, float]:
        """(xmin, ymin, xmax, ymax) in the tile's own region's Albers CRS."""
        t = self.parse(tile_id)
        r = ARD_REGIONS[t.region]
        x0, y1 = r.ulx + t.h * ARD_TILE_M, r.uly - t.v * ARD_TILE_M
        return x0, y1 - ARD_TILE_M, x0 + ARD_TILE_M, y1

    def bbox_lonlat(self, tile_id: str) -> tuple[float, float, float, float]:
        """lon/lat bbox enclosing the tile, from densified edges (a straight
        Albers edge is curved in lon/lat, so corners alone aren't enough).
        A tile across the antimeridian (westernmost Aleutians) gets the full
        longitude range: a superset, which is all a tile search needs."""
        import numpy as np
        from pyproj import Transformer

        xmin, ymin, xmax, ymax = self.bounds(tile_id)
        e = np.linspace(0, ARD_TILE_M, 21)
        xs = np.concatenate([xmin + e, xmin + e, np.full(21, xmin), np.full(21, xmax)])
        ys = np.concatenate([np.full(21, ymax), np.full(21, ymin), ymax - e, ymax - e])
        lons, lats = Transformer.from_crs(self.region(tile_id).crs, "EPSG:4326", always_xy=True).transform(xs, ys)
        west, east = float(min(lons)), float(max(lons))
        if east - west > 180:  # wrapped across +/-180
            west, east = -180.0, 180.0
        return west, float(min(lats)), east, float(max(lats))

    def default_grid(self, tile_id: str) -> Grid:
        from rasterio.transform import from_origin

        xmin, _, _, ymax = self.bounds(tile_id)
        return Grid(crs=self.region(tile_id).crs, transform=from_origin(xmin, ymax, ARD_RES_M, ARD_RES_M),
                    width=ARD_TILE_PX, height=ARD_TILE_PX)

    def grid_from_item(self, tile_id: str, item) -> Grid:
        """The tile's grid exactly as the source files declare it (STAC
        proj:wkt2/proj:transform/proj:shape), so native-grid reads compare
        equal to the files and need no warp. Falls back to default_grid.
        Refuses an item from another region or another tile."""
        from rasterio.transform import Affine

        props = getattr(item, "properties", None) or {}
        region = props.get("landsat:grid_region")
        if region is not None and region != self.parse(tile_id).region:
            raise ValueError(f"[usgs_ard] item {getattr(item, 'id', '?')} is on the {region} ARD grid, "
                             f"not tile {tile_id}'s")
        wkt, transform, shape = props.get("proj:wkt2"), props.get("proj:transform"), props.get("proj:shape")
        if not wkt or not transform or not shape:
            return self.default_grid(tile_id)
        grid = Grid(crs=wkt, transform=Affine(*transform[:6]), width=int(shape[1]), height=int(shape[0]))
        if not grid.transform.almost_equals(self.default_grid(tile_id).transform):
            raise ValueError(f"[usgs_ard] item {getattr(item, 'id', '?')} does not sit on tile {tile_id}'s grid")
        return grid

    def describe(self, tile_id: str) -> dict:
        t = self.parse(tile_id)
        r = ARD_REGIONS[t.region]
        return {"system": r.system, "id": self.canonical(tile_id), "h": t.h, "v": t.v, "region": t.region,
                "boundsAlbers": list(self.bounds(tile_id)), "bboxLonLat": list(self.bbox_lonlat(tile_id))}


# Kept so existing imports keep working; it now knows every ARD region.
UsgsArdConusGrid = UsgsArdGrid


def tile_id_for(h: int, v: int, region: str = DEFAULT_REGION) -> str:
    """Canonical tile id: bare for CONUS, region-qualified otherwise."""
    base = f"h{h:03d}v{v:03d}"
    return base if region == DEFAULT_REGION else f"{region}_{base}"


def item_tile_id(item) -> Optional[str]:
    """Canonical ARD tile id of a LandsatLook STAC item, from its grid
    region + h/v. None if the item doesn't say which grid it is on."""
    props = getattr(item, "properties", None) or {}
    region = props.get("landsat:grid_region")
    h, v = props.get("landsat:grid_horizontal"), props.get("landsat:grid_vertical")
    if region is None or h is None or v is None:
        return None
    return tile_id_for(int(h), int(v), region)
