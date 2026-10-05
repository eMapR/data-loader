"""Tests for bench/ard_tile_history_bench.py's config loading and uint16
writer (no network)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "bench"))

import ard_tile_history_bench as ath  # noqa: E402


def _cfg(text: str):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(text)
    return ath.load_config(f.name)


class LoadConfigTests(unittest.TestCase):
    def test_minimal_config_gets_defaults(self):
        cfg = _cfg("tiles: [h003v004]\ndate_range: {start: '1990-01-01'}\n")
        self.assertEqual(cfg.tiles, ("h003v004",))
        self.assertEqual(cfg.start, date(1990, 1, 1))
        self.assertIsNone(cfg.end)
        self.assertEqual(cfg.bands, ath.CANONICAL_BANDS)
        self.assertEqual((cfg.save_format, cfg.save_qa_pixel, cfg.cloud_mask, cfg.workers), ("uint16", False, True, 4))
        self.assertEqual(cfg.output_dir, REPO / "bench" / "results" / "ard_tile_history")
        self.assertIsNone(cfg.summary_dir)

    def test_shipped_configs_load(self):
        h = ath.load_config(REPO / "bench" / "configs" / "ard_tile_history_h003v004.yaml")
        self.assertEqual((h.tiles, h.end, h.save_qa_pixel), (("h003v004",), date(2026, 10, 2), False))
        o = ath.load_config(REPO / "bench" / "configs" / "ard_tile_history_oregon.yaml")
        self.assertEqual(len(o.tiles), 23)
        self.assertEqual(len(set(o.tiles)), 23)
        self.assertTrue(o.save_qa_pixel)

    def test_rejects_mistakes(self):
        base = "tiles: [h003v004]\ndate_range: {start: '1990-01-01'}\n"
        cases = {
            "unknown config key": base + "worker: 8\n",
            "expected CONUS ARD id": "tiles: [h3v4]\ndate_range: {start: '1990-01-01'}\n",
            "save.format": base + "save: {format: int16}\n",
            "save: unknown": base + "save: {qa: true}\n",
            "bands": base + "bands: [blue, thermal]\n",
            "before start": "tiles: [h003v004]\ndate_range: {start: '2000-01-01', end: '1999-01-01'}\n",
            "workers": base + "workers: 0\n",
            "tiles": "date_range: {start: '1990-01-01'}\n",
        }
        for msg, text in cases.items():
            with self.subTest(msg), self.assertRaisesRegex(ValueError, msg):
                _cfg(text)

    def test_parse_tile(self):
        self.assertEqual(ath.parse_tile("h003v004"), (3, 4))
        with self.assertRaises(ValueError):
            ath.parse_tile("003004")


class WriteUint16Tests(unittest.TestCase):
    def _grid(self):
        from rasterio.transform import Affine
        from data_loader.providers.base import Grid
        return Grid(crs="EPSG:5070", transform=Affine(30, 0, 0, 0, -30, 0), width=3, height=2)

    def test_dn_roundtrip_nodata_and_qa_band(self):
        import rasterio
        dn = np.array([[7273, 20000, 43636], [10000, 0, 30000]], dtype="u2")
        refl = dn.astype("f4") * 2.75e-5 - 0.2
        refl[1, 1] = np.nan  # fill / masked
        qa = np.array([[21824, 22080, 1], [21824, 1, 55052]], dtype="u2")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.tif"
            ath._write_uint16(p, {"qa_pixel": qa, "blue": refl, "nir": refl}, self._grid())
            with rasterio.open(p) as src:
                self.assertEqual(src.descriptions, ("blue", "nir", "qa_pixel"))  # qa last
                np.testing.assert_array_equal(src.read(1), dn)                 # lossless DN
                np.testing.assert_array_equal(src.read(3), qa)                 # QA untouched
                self.assertEqual(src.nodata, 0)
                self.assertEqual(src.scales, (2.75e-5, 2.75e-5, 1.0))
                self.assertEqual(src.offsets, (-0.2, -0.2, 0.0))


if __name__ == "__main__":
    unittest.main()
