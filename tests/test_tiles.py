"""USGS ARD native tile grids (CONUS, Alaska, Hawaii), usgs_ard tile
discovery (whole tile, own region + grid only), and tile-AOI runs through
the engine. Network-free."""
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
from data_loader.tiles import ARD_REGIONS, UsgsArdGrid, item_tile_id
from tests.fakes import FakeProvider, july, make_config, read_items, run_with

GRID = UsgsArdGrid()
TILES_AOI = {"upper_left": None, "lower_right": None, "tiles": ["h003v004"]}


class ArdGridTests(unittest.TestCase):
    def test_parse_and_validate(self):
        self.assertEqual(GRID.parse("h003v004"), ("CU", 3, 4))
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


def _ard_item(item_id, h, v, region="CU"):
    return pystac.Item(item_id, None, None, datetime(2020, 7, 1, 18, tzinfo=timezone.utc), {
        "landsat:grid_region": region, "landsat:grid_horizontal": f"{h:02d}",
        "landsat:grid_vertical": f"{v:02d}", "eo:cloud_cover": 50.0})


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


class _SmallGrid(UsgsArdGrid):
    """The real tile ids and CRS, but 10x10 px so tests stay fast."""

    def default_grid(self, tile_id):
        xmin, _, _, ymax = self.bounds(tile_id)
        return Grid(crs=self.region(tile_id).crs, transform=Affine(15000, 0, xmin, 0, -15000, ymax),
                    width=10, height=10)

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

    def test_failures_retried_before_the_next_tile_starts(self):
        p = FakeProvider(july(2), native=True, tiles=True, fail={"scene-1": 1})
        p.tile_grid = _SmallGrid()
        with tempfile.TemporaryDirectory() as tmp:
            s = run_with(p, make_config(tmp, aoi={**TILES_AOI, "tiles": ["h003v004", "h003v005"]},
                                        grid={"resolution_m": None}, workers=1,
                                        output={"encoding": "native", "bands": ["red"], "qa_band": True}))
            rows = {r["key"]: r for r in read_items(tmp)}
        self.assertTrue(s.complete)
        # h003v004: scene-0, scene-1 (fails), scene-1 again; only then h003v005
        self.assertEqual(p.reads, ["scene-0", "scene-1", "scene-1", "scene-0", "scene-1"])
        self.assertEqual(rows["landsat/h003v004/scene-1"]["attempts"], 2)
        self.assertEqual(rows["landsat/h003v005/scene-1"]["attempts"], 1)

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


class RegionalGridTests(unittest.TestCase):
    """Alaska/Hawaii grids, and the CONUS/Alaska h/v collision: the same
    h/v is a different place on each regional grid, so a tile id must say
    which grid it is on and nothing may fall back to CONUS."""

    def test_region_qualified_ids_parse_to_their_own_grid(self):
        self.assertEqual(GRID.parse("AK_h021v008"), ("AK", 21, 8))
        self.assertEqual(GRID.parse("HI_h001v001"), ("HI", 1, 1))
        self.assertEqual(GRID.parse("CU_h003v004"), ("CU", 3, 4))

    def test_canonical_form_is_bare_for_conus_only(self):
        # bare CONUS ids keep existing datasets' keys, paths and identity
        self.assertEqual(GRID.canonical("CU_h003v004"), "h003v004")
        self.assertEqual(GRID.canonical("h003v004"), "h003v004")
        self.assertEqual(GRID.canonical("AK_h021v008"), "AK_h021v008")

    def test_bad_region_ids_rejected_with_a_hint(self):
        cases = {"XX_h001v001": "unknown ARD grid region 'XX'", "AK:h021v008": "AK_h021v008",
                 "ak_h021v008": "AK_h021v008", "AK_h026v000": "outside the Alaska ARD grid",
                 "AK_h000v014": "outside the Alaska ARD grid", "HI_h005v000": "outside the Hawaii ARD grid"}
        for bad, msg in cases.items():
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, msg):
                GRID.parse(bad)

    def test_bounds_match_known_stac_transforms(self):
        # STAC proj:transform origins: AK h020v008 (798285, 1274325), HI h001v001 (-294345, 2018895)
        self.assertEqual(GRID.bounds("AK_h020v008")[0::3], (798285.0, 1274325.0))
        self.assertEqual(GRID.bounds("HI_h001v001")[0::3], (-294345.0, 2018895.0))
        g = GRID.default_grid("AK_h021v008")
        self.assertEqual(g.crs, ARD_REGIONS["AK"].crs)
        self.assertEqual(tuple(g.transform)[:6], (30.0, 0.0, 948285.0, 0.0, -30.0, 1274325.0))

    def test_same_hv_is_a_different_place_per_region(self):
        w, s, e, n = GRID.bbox_lonlat("AK_h021v008")  # Northern Southeast Alaska
        self.assertTrue(-138 < w < e < -133 and 58 < s < n < 61, (w, s, e, n))
        w, s, e, n = GRID.bbox_lonlat("h021v008")     # CONUS: northern Illinois
        self.assertTrue(-90 < w < e < -86 and 40 < s < n < 42.5, (w, s, e, n))

    def test_antimeridian_tile_gets_full_longitude_range(self):
        w, s, e, n = GRID.bbox_lonlat("AK_h002v013")  # Aleutians, across 180
        self.assertEqual((w, e), (-180.0, 180.0))
        self.assertTrue(49 < s < n < 52, (s, n))
        w, _, e, _ = GRID.bbox_lonlat("AK_h000v010")  # westernmost, wholly east of 180
        self.assertTrue(171 < w < e < 175, (w, e))

    def test_describe_records_region_and_system(self):
        d = GRID.describe("AK_h021v008")
        self.assertEqual((d["id"], d["region"], d["system"], d["h"], d["v"]),
                         ("AK_h021v008", "AK", "usgs_ard_alaska", 21, 8))
        self.assertEqual(GRID.describe("CU_h003v004")["system"], "usgs_ard_conus")

    def test_item_tile_id_is_region_qualified(self):
        self.assertEqual(item_tile_id(_ard_item("x", 21, 8, "AK")), "AK_h021v008")
        self.assertEqual(item_tile_id(_ard_item("x", 21, 8, "CU")), "h021v008")
        self.assertIsNone(item_tile_id(pystac.Item("x", None, None, datetime(2020, 1, 1, tzinfo=timezone.utc), {
            "landsat:grid_horizontal": "21", "landsat:grid_vertical": "08"})))

    def test_grid_from_item_refuses_another_regions_item(self):
        item = _ard_item("LC08_CU_021008_20200701_20210101_02_SR", 21, 8, "CU")
        item.properties.update({"proj:wkt2": "WKT", "proj:shape": [5000, 5000],
                                "proj:transform": [30, 0, 584415, 0, -30, 2114805]})
        with self.assertRaisesRegex(ValueError, "is on the CU ARD grid"):
            GRID.grid_from_item("AK_h021v008", item)


class UsgsArdRegionalSearchTests(unittest.TestCase):
    def _provider(self, items):
        p = UsgsArdProvider(username="u", token="t")
        search = MagicMock()
        search.return_value.items.return_value = items
        p._client = MagicMock(search=search)
        return p, search

    def test_alaska_tile_searches_alaska_and_drops_conus_namesakes(self):
        p, search = self._provider([_ard_item("LC08_AK_021008_20200701_20210101_02_SR", 21, 8, "AK"),
                                    _ard_item("LC08_CU_021008_20200701_20210101_02_SR", 21, 8, "CU")])
        refs = p.search_tile("AK_h021v008", "landsat", date(2020, 1, 1), date(2020, 12, 31))
        self.assertEqual([r.id for r in refs], ["LC08_AK_021008_20200701_20210101_02_SR"])
        kw = search.call_args.kwargs
        self.assertEqual(kw["query"]["landsat:grid_region"], {"eq": "AK"})
        self.assertEqual(kw["bbox"], GRID.bbox_lonlat("AK_h021v008"))

    def test_bare_and_cu_ids_search_conus_only(self):
        for tile in ("h021v008", "CU_h021v008"):
            p, search = self._provider([_ard_item("LC08_AK_021008_20200701_20210101_02_SR", 21, 8, "AK"),
                                        _ard_item("LC08_CU_021008_20200701_20210101_02_SR", 21, 8, "CU")])
            refs = p.search_tile(tile, "landsat", date(2020, 1, 1), date(2020, 12, 31))
            with self.subTest(tile=tile):
                self.assertEqual([r.id for r in refs], ["LC08_CU_021008_20200701_20210101_02_SR"])
                self.assertEqual(search.call_args.kwargs["query"]["landsat:grid_region"], {"eq": "CU"})


class RegionalTileRunTests(unittest.TestCase):
    def _run(self, tmp, tiles):
        p = FakeProvider(july(1), native=True, tiles=True)
        p.tile_grid = _SmallGrid()
        run_with(p, make_config(tmp, aoi={**TILES_AOI, "tiles": tiles}, grid={"resolution_m": None},
                                output={"encoding": "native", "bands": ["red"], "qa_band": True}))
        return p

    def test_alaska_tile_run_keys_paths_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._run(tmp, ["AK_h021v008"])
            rows = read_items(tmp)
            m = json.loads((Path(tmp) / "manifest.json").read_text())
        self.assertEqual(p.search_calls[0][0], "AK_h021v008")
        self.assertEqual(rows[0]["key"], "landsat/AK_h021v008/scene-0")
        self.assertTrue(rows[0]["files"][0]["path"].startswith("landsat/AK_h021v008/2023/"))
        t = m["grids"]["landsat"]["AK_h021v008"]["tile"]
        self.assertEqual((t["region"], t["system"]), ("AK", "usgs_ard_alaska"))
        self.assertEqual(m["request"]["aoi"]["tiles"], ["AK_h021v008"])

    def test_cu_prefix_recorded_bare_and_resumes_a_bare_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp, ["h003v004"])                  # an existing (Oregon-style) dataset
            p = self._run(tmp, ["CU_h003v004"])           # same tile, qualified spelling
            m = json.loads((Path(tmp) / "manifest.json").read_text())
            rows = read_items(tmp)
        self.assertEqual(m["request"]["aoi"]["tiles"], ["h003v004"])
        self.assertEqual([r["key"] for r in rows], ["landsat/h003v004/scene-0"])
        self.assertEqual(p.reads, [])                     # nothing re-acquired

    def test_mixed_regions_in_one_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp, ["h021v008", "AK_h021v008"])
            keys = sorted(r["key"] for r in read_items(tmp))
        self.assertEqual(keys, ["landsat/AK_h021v008/scene-0", "landsat/h021v008/scene-0"])

    def test_same_tile_twice_rejected(self):
        p = FakeProvider(july(1), tiles=True)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp, aoi={**TILES_AOI, "tiles": ["h003v004", "CU_h003v004"]}, grid={"resolution_m": None})
            with self.assertRaisesRegex(ConfigError, "listed more than once"):
                run_with(p, cfg)
        self.assertEqual(p.search_calls, [])

    def test_unknown_region_rejected_before_network(self):
        p = FakeProvider(july(1), tiles=True)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp, aoi={**TILES_AOI, "tiles": ["AK:h021v008"]}, grid={"resolution_m": None})
            with self.assertRaisesRegex(ConfigError, "AK_h021v008"):
                run_with(p, cfg)
        self.assertEqual(p.search_calls, [])


if __name__ == "__main__":
    unittest.main()
