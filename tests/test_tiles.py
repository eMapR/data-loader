"""USGS ARD CONUS tile grid, usgs_ard tile discovery (whole tile, own grid
only), and a tile-AOI run through the engine. Network-free."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pystac
from rasterio.transform import Affine

from data_loader.config import ConfigError
from data_loader.providers.base import Grid
from data_loader.providers.usgs_ard import UsgsArdProvider
from data_loader.tiles import ARD_CONUS_CRS, UsgsArdConusGrid, item_tile_id
from tests.fakes import FakeProvider, july, make_config, read_items, run_with

GRID = UsgsArdConusGrid()
TILES_AOI = {"upper_left": None, "lower_right": None, "tiles": ["h003v004"]}


class ArdGridTests(unittest.TestCase):
    def test_parse_and_validate(self):
        self.assertEqual(GRID.parse("h003v004"), (3, 4))
        for bad in ("h3v4", "h033v004", "h003v022", "H003V004"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                GRID.parse(bad)

    def test_bounds_match_known_tile(self):
        # h003v004's STAC proj:transform origin is (-2115585, 2714805)
        self.assertEqual(GRID.bounds("h003v004"), (-2115585.0, 2564805.0, -1965585.0, 2714805.0))
        g = GRID.default_grid("h003v004")
        self.assertEqual((g.width, g.height), (5000, 5000))
        self.assertEqual(tuple(g.transform)[:6], (30.0, 0.0, -2115585.0, 0.0, -30.0, 2714805.0))

    def test_bbox_lonlat_covers_central_oregon(self):
        w, s, e, n = GRID.bbox_lonlat("h003v004")
        self.assertTrue(-123.5 < w < -122.5 and 43 < s < 44 and -121.5 < e < -120.5 and 44.5 < n < 45.5, (w, s, e, n))

    def test_grid_from_item_uses_item_projection(self):
        item = pystac.Item("x", None, None, datetime(2020, 1, 1, tzinfo=timezone.utc), {
            "proj:wkt2": "WKT", "proj:transform": [30, 0, -2115585, 0, -30, 2714805], "proj:shape": [5000, 5000]})
        g = GRID.grid_from_item("h003v004", item)
        self.assertEqual(g.crs, "WKT")
        with self.assertRaisesRegex(ValueError, "does not sit on tile"):
            GRID.grid_from_item("h004v004", item)


def _ard_item(item_id, h, v):
    return pystac.Item(item_id, None, None, datetime(2020, 7, 1, 18, tzinfo=timezone.utc), {
        "landsat:grid_horizontal": f"{h:02d}", "landsat:grid_vertical": f"{v:02d}", "eo:cloud_cover": 50.0})


class UsgsArdSearchTileTests(unittest.TestCase):
    def _provider(self, items):
        p = UsgsArdProvider(username="u", token="t")
        search = MagicMock()
        search.return_value.items.return_value = items
        p._client = MagicMock(search=search)
        return p, search

    def test_keeps_only_the_tiles_own_items_and_searches_whole_tile(self):
        p, search = self._provider([_ard_item("LC08_CU_003004_20200701_20210101_02_SR", 3, 4),
                                    _ard_item("LC08_CU_004004_20200701_20210101_02_SR", 4, 4)])
        refs = p.search_tile("h003v004", "landsat", date(2020, 1, 1), date(2020, 12, 31))
        self.assertEqual([r.id for r in refs], ["LC08_CU_003004_20200701_20210101_02_SR"])
        kw = search.call_args.kwargs
        self.assertEqual(kw["bbox"], GRID.bbox_lonlat("h003v004"))
        self.assertEqual(kw["query"]["landsat:grid_horizontal"], {"eq": "03"})
        self.assertNotIn("eo:cloud_cover", kw["query"])  # no cloud filter unless asked

    def test_handles_are_compact_but_keep_the_verbatim_record(self):
        from data_loader.providers.usgs_ard import CompactItem

        item = _ard_item("LC08_CU_003004_20200701_20210101_02_SR", 3, 4)
        item.add_asset("blue", pystac.Asset("https://example/blue.tif"))
        p, _ = self._provider([item])
        ref = p.search_tile("h003v004", "landsat", date(2020, 1, 1), date(2020, 12, 31))[0]
        self.assertIsInstance(ref.handle, CompactItem)
        self.assertEqual(ref.handle.properties["landsat:grid_horizontal"], "03")
        self.assertEqual(p.source_metadata(ref), item.to_dict())

    def test_cloud_filter_only_below_100(self):
        p, search = self._provider([])
        p.search_tile("h003v004", "landsat", date(2020, 1, 1), date(2020, 12, 31), 30)
        self.assertEqual(search.call_args.kwargs["query"]["eo:cloud_cover"], {"lte": 30})

    def test_item_tile_id(self):
        self.assertEqual(item_tile_id(_ard_item("x", 3, 4)), "h003v004")

    def test_credential_problems(self):
        self.assertEqual(UsgsArdProvider(username="u", token="t").credential_problems(), [])
        p = UsgsArdProvider(username="", token="")
        p.username = p.token = None
        self.assertIn("USGS_M2M_USERNAME", p.credential_problems()[0])


class _SmallGrid(UsgsArdConusGrid):
    """The real tile ids and CRS, but 10x10 px so tests stay fast."""

    def default_grid(self, tile_id):
        xmin, _, _, ymax = self.bounds(tile_id)
        return Grid(crs=ARD_CONUS_CRS, transform=Affine(15000, 0, xmin, 0, -15000, ymax), width=10, height=10)

    def grid_from_item(self, tile_id, item):
        return self.default_grid(tile_id)


class TileRunTests(unittest.TestCase):
    def test_tile_aoi_run_writes_per_tile_layout_and_grid_description(self):
        p = FakeProvider(july(2), native=True, tiles=True)
        p.tile_grid = _SmallGrid()
        with tempfile.TemporaryDirectory() as tmp:
            run_with(p, make_config(tmp, aoi=TILES_AOI, grid={"resolution_m": None},
                                    output={"encoding": "native", "bands": ["red"], "qa_band": True}))
            rows = read_items(tmp)
            m = json.loads((Path(tmp) / "manifest.json").read_text())
        self.assertEqual(rows[0]["key"], "landsat/h003v004/scene-0")
        self.assertTrue(rows[0]["files"][0]["path"].startswith("landsat/h003v004/2023/"))
        g = m["grids"]["landsat"]["h003v004"]
        self.assertEqual(g["tile"]["system"], "usgs_ard_conus")
        self.assertEqual((g["tile"]["h"], g["tile"]["v"]), (3, 4))
        self.assertIn("none", m["processing"]["resampling"])
        self.assertEqual(p.search_calls[0][0], "h003v004")

    def test_tiles_need_a_provider_with_a_tile_grid(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ConfigError, "has no tile grid"):
                run_with(FakeProvider(july(1)), make_config(tmp, aoi=TILES_AOI, grid={"resolution_m": None}))

    def test_bad_tile_id_rejected_before_network(self):
        p = FakeProvider(july(1), tiles=True)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp, aoi={**TILES_AOI, "tiles": ["h099v004"]}, grid={"resolution_m": None})
            with self.assertRaisesRegex(ConfigError, "outside the CONUS ARD grid"):
                run_with(p, cfg)
        self.assertEqual(p.search_calls, [])


if __name__ == "__main__":
    unittest.main()
