"""StacProvider.search_scenes for Sentinel-2 against real catalog items
(tests/fixtures/sentinel2, fetched from Planetary Computer and Earth
Search over an area covering MGRS tiles 10TDP and 10TEP).

Regression for the 2026-10-15 audit: Planetary Computer names the tile
`s2:mgrs_tile`, which the acquisition key ignored, so both tiles of one
datatake fell into one group -- one was dropped as an "excluded processing
version", or the run failed on an unresolvable tie."""
from __future__ import annotations

import json
import unittest
from datetime import date
from pathlib import Path

import pystac

from data_loader.providers import aws_earth_search, planetary_computer
from data_loader.providers.stac_common import _s2_mgrs_tile, _s2_stable_key, item_sr_scale_offset

FIXTURES = Path(__file__).parent / "fixtures" / "sentinel2"


def _items(name):
    return [pystac.Item.from_dict(f) for f in json.loads((FIXTURES / name).read_text())["features"]]


class _Search:
    def __init__(self, items):
        self._items = items

    def items(self):
        return iter(self._items)


class _Client:
    def __init__(self, items):
        self._items = items

    def search(self, **kw):
        return _Search(self._items)


def _search(provider, fixture, policy="any", season=(None, None)):
    provider._client = _Client(_items(fixture))
    return provider.search_scenes((-123.05, 44.0, -122.95, 44.1), "sentinel2", date(2019, 1, 1), date(2022, 12, 31),
                                  season[0], season[1], 100, processing_version_policy=policy)


class TileIdentityTests(unittest.TestCase):
    def test_every_fixture_item_has_its_tile(self):
        for name in ("pc_10TDP_10TEP_2019-07.json", "es_10TDP_10TEP_2019-07.json", "es_10TDP_10TEP_2022-07.json"):
            for it in _items(name):
                self.assertIn(_s2_mgrs_tile(it), {"10TDP", "10TEP"}, it.id)

    def test_earth_search_grid_square_alone_is_not_used_as_the_tile(self):
        it = _items("es_10TDP_10TEP_2019-07.json")[0]
        del it.properties["grid:code"]
        self.assertIn(_s2_mgrs_tile(it), {"10TDP", "10TEP"})  # from mgrs:utm_zone + latitude_band + grid_square

    def test_tile_from_item_id_when_no_tile_property(self):
        it = _items("pc_10TDP_10TEP_2019-07.json")[0]
        del it.properties["s2:mgrs_tile"]
        self.assertEqual(_s2_mgrs_tile(it), "10TEP")  # S2A_MSIL2A_20190715T190921_R056_T10TEP_...

    def test_unidentifiable_item_is_its_own_group(self):
        it = _items("pc_10TDP_10TEP_2019-07.json")[0]
        for k in ("s2:mgrs_tile", "s2:product_uri", "s2:granule_id"):
            it.properties.pop(k, None)
        it.id = "no-tile-here"
        self.assertEqual(_s2_stable_key(it), ("item", "no-tile-here"))

    def test_missing_datatake_falls_back_to_acquisition_time_not_none(self):
        a, b = _items("pc_10TDP_10TEP_2019-07.json")[:2]
        for it in (a, b):
            del it.properties["s2:datatake_id"]
        self.assertNotIn(None, _s2_stable_key(a))
        self.assertNotEqual(_s2_stable_key(a), _s2_stable_key(b))  # same overpass, different tiles


class PlanetaryComputerSearchTests(unittest.TestCase):
    def test_both_tiles_of_every_datatake_are_kept(self):
        for fixture in ("pc_10TDP_10TEP_2019-07.json", "pc_10TDP_10TEP_2022-07.json"):
            for policy in ("any", "latest"):
                p = planetary_computer.make_provider()
                refs = _search(p, fixture, policy)
                self.assertEqual(sorted(r.id for r in refs), sorted(i.id for i in _items(fixture)), (fixture, policy))
                self.assertEqual(p.last_excluded_versions, [])

    def test_provenance_records_tile_and_offset(self):
        refs = _search(planetary_computer.make_provider(), "pc_10TDP_10TEP_2022-07.json")
        self.assertEqual({r.provenance.extra["mgrs_tile"] for r in refs}, {"10TDP", "10TEP"})
        self.assertEqual({r.provenance.extra["sr_offset"] for r in refs}, {-0.1})  # baseline 04.00, PC serves raw DN
        refs = _search(planetary_computer.make_provider(), "pc_10TDP_10TEP_2019-07.json")
        self.assertEqual({r.provenance.extra["sr_offset"] for r in refs}, {0.0})  # baseline 02.12

    def test_pinned_policy_keeps_only_that_baseline(self):
        p = planetary_computer.make_provider()
        self.assertEqual(len(_search(p, "pc_10TDP_10TEP_2019-07.json", "pinned:02.12")), 12)
        self.assertEqual(_search(p, "pc_10TDP_10TEP_2019-07.json", "pinned:05.00"), [])

    def test_season_filter(self):
        refs = _search(planetary_computer.make_provider(), "pc_10TDP_10TEP_2019-07.json", season=("07-10", "07-12"))
        self.assertEqual(sorted({r.date.day for r in refs}), [10, 12])


class EarthSearchSearchTests(unittest.TestCase):
    """Earth Search lists reprocessed 2019 granules twice (_0_ at the
    original baseline, _1_ at 05.00): one per tile per datatake must
    remain, and the other is recorded as excluded."""

    def test_reprocessed_duplicates_resolve_to_newest_per_tile(self):
        p = aws_earth_search.make_provider()
        refs = _search(p, "es_10TDP_10TEP_2019-07.json", "latest")
        self.assertEqual(len(refs), 12)
        self.assertEqual({r.provenance.processing_baseline for r in refs}, {"05.00"})
        self.assertEqual({(r.date, r.provenance.extra["mgrs_tile"]) for r in refs}.__len__(), 12)
        self.assertEqual(len(p.last_excluded_versions), 12)
        self.assertTrue(all(e["processing_baseline"] in ("02.12", "02.13") for e in p.last_excluded_versions))

    def test_offset_already_applied_on_earth_search(self):
        for fixture in ("es_10TDP_10TEP_2019-07.json", "es_10TDP_10TEP_2022-07.json"):
            spec = aws_earth_search.make_provider().config.sensors["sentinel2"]
            for it in _items(fixture):
                expected = 0.0 if it.properties.get("earthsearch:boa_offset_applied") else (
                    -0.1 if it.properties["s2:processing_baseline"] >= "04.00" else 0.0)
                self.assertEqual(item_sr_scale_offset(it, spec)[1], expected, it.id)

    def test_pinned_original_baseline(self):
        refs = _search(aws_earth_search.make_provider(), "es_10TDP_10TEP_2019-07.json", "pinned:02.12")
        self.assertEqual(len(refs), 6)  # 07-02, 07-05, 07-07 were 02.12 (x2 tiles); the rest 02.13

    def test_allow_mixed_keeps_everything(self):
        self.assertEqual(len(_search(aws_earth_search.make_provider(), "es_10TDP_10TEP_2019-07.json", "allow_mixed")), 24)

    def test_single_version_2022(self):
        refs = _search(aws_earth_search.make_provider(), "es_10TDP_10TEP_2022-07.json")
        self.assertEqual(len(refs), 12)


if __name__ == "__main__":
    unittest.main()
