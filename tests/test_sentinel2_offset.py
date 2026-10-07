"""Sentinel-2 L2A reflectance offset (ESA processing baseline >= 04.00 adds
-1000 DN; see stac_common.item_sr_scale_offset). Network-free."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

import numpy as np
import pystac

from data_loader.providers import aws_earth_search, planetary_computer
from data_loader.providers.stac_common import _build_provenance, item_sr_scale_offset

PC_S2 = planetary_computer.CONFIG.sensors["sentinel2"]
ES_S2 = aws_earth_search.CONFIG.sensors["sentinel2"]
PC_LANDSAT = planetary_computer.CONFIG.sensors["landsat"]


def _item(dt, baseline=None, raster_bands=None, asset_key="B08"):
    props = {} if baseline is None else {"s2:processing_baseline": baseline}
    it = pystac.Item("x", None, None, dt, props)
    extra = {} if raster_bands is None else {"raster:bands": raster_bands}
    it.add_asset(asset_key, pystac.Asset("https://example/b.tif", extra_fields=extra))
    return it


OLD = datetime(2021, 7, 19, tzinfo=timezone.utc)
NEW = datetime(2023, 7, 14, tzinfo=timezone.utc)


class Sentinel2OffsetTests(unittest.TestCase):
    def test_baseline_before_04_has_no_offset(self):
        self.assertEqual(item_sr_scale_offset(_item(OLD, "03.00"), PC_S2, "B08"), (0.0001, 0.0))

    def test_baseline_04_and_later_subtracts_0_1(self):
        for b in ("04.00", "05.09", "05.10"):
            self.assertEqual(item_sr_scale_offset(_item(NEW, b), PC_S2, "B08"), (0.0001, -0.1), b)

    def test_declared_raster_bands_offset_wins(self):
        it = _item(NEW, "05.09", [{"scale": 0.0001, "offset": -0.1}], asset_key="nir")
        self.assertEqual(item_sr_scale_offset(it, ES_S2, "nir"), (0.0001, -0.1))
        it = _item(NEW, "05.09", [{"scale": 0.0001, "offset": 0.0}], asset_key="nir")
        self.assertEqual(item_sr_scale_offset(it, ES_S2, "nir"), (0.0001, 0.0))

    def test_missing_baseline_falls_back_to_acquisition_date(self):
        self.assertEqual(item_sr_scale_offset(_item(OLD), PC_S2, "B08")[1], 0.0)
        self.assertEqual(item_sr_scale_offset(_item(NEW), PC_S2, "B08")[1], -0.1)

    def test_landsat_is_unaffected(self):
        self.assertEqual(item_sr_scale_offset(_item(NEW, "05.09"), PC_LANDSAT, "nir08"), (2.75e-5, -0.2))

    def test_offset_recorded_in_provenance(self):
        prov = _build_provenance("planetary_computer", _item(NEW, "05.10"), PC_S2)
        self.assertEqual(prov.extra["sr_offset"], -0.1)

    def test_dark_water_reflectance_matches_across_baselines(self):
        """The live check behind the fix: open-ocean NIR is ~DN 19 at
        baseline 03.00 and ~DN 1019+ at 05.x; both must come out near 0."""
        from data_loader.masking import dn_to_reflectance
        for dt, baseline, dn in ((OLD, "03.00", 19), (NEW, "05.09", 1019)):
            scale, offset = item_sr_scale_offset(_item(dt, baseline), PC_S2, "B08")
            r = dn_to_reflectance(np.array([dn], "u2"), scale, offset)[0]
            self.assertAlmostEqual(float(r), 0.0019, places=4)


if __name__ == "__main__":
    unittest.main()
