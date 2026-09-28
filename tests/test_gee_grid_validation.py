"""Regression tests for the GEE grid/region download bug: Earth Engine's
getDownloadURL HTTP-succeeded while silently returning a raster on a wildly
wrong pixel grid (roughly the whole source scene's extent instead of the
requested AOI) with all-zero pixel data. `_validate_grid_download` is what
now catches that class of failure loudly instead of letting it through.

These tests exercise `_validate_grid_download`/`_grid_bounds` directly with
synthetic rasterio-like objects -- no network access or Earth Engine
credentials required, so they run in any environment with the base
requirements (rasterio, numpy) installed. A separate, credential-gated
integration test at the bottom exercises the real `_grid_region`/`_download`
path end-to-end when GEE credentials are available.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import rasterio.transform
from rasterio.coords import BoundingBox
from rasterio.crs import CRS
from rasterio.transform import Affine

from data_loader.providers.base import Grid
from data_loader.providers.gee import _grid_bounds, _validate_grid_download


class _FakeRasterioDataset:
    """Minimal stand-in for an opened rasterio dataset -- only the
    attributes _validate_grid_download actually reads."""

    def __init__(self, height, width, crs, transform, bounds):
        self.height = height
        self.width = width
        self.crs = crs
        self.transform = transform
        self.bounds = bounds


def _make_grid(width=10, height=8, res=30.0, crs="EPSG:32610", x0=500000.0, y0=5000000.0) -> Grid:
    transform = Affine(res, 0.0, x0, 0.0, -res, y0)
    return Grid(crs=crs, transform=transform, width=width, height=height)


def _matching_dataset(grid: Grid) -> _FakeRasterioDataset:
    bounds = rasterio.transform.array_bounds(grid.height, grid.width, grid.transform)
    return _FakeRasterioDataset(
        height=grid.height, width=grid.width,
        crs=CRS.from_user_input(grid.crs), transform=grid.transform,
        bounds=BoundingBox(*bounds),
    )


class ValidateGridDownloadTests(unittest.TestCase):
    def test_matching_grid_passes(self):
        grid = _make_grid()
        src = _matching_dataset(grid)
        data = np.ones((1, grid.height, grid.width), dtype="f4")
        _validate_grid_download(src, data, grid, ["red"])  # must not raise

    def test_dimension_mismatch_raises(self):
        grid = _make_grid()
        src = _matching_dataset(grid)
        src.width = grid.width + 1
        data = np.ones((1, grid.height, src.width), dtype="f4")
        with self.assertRaisesRegex(RuntimeError, "did not honor the requested grid size"):
            _validate_grid_download(src, data, grid, ["red"])

    def test_crs_mismatch_raises(self):
        grid = _make_grid()
        src = _matching_dataset(grid)
        src.crs = CRS.from_epsg(4326)
        data = np.ones((1, grid.height, grid.width), dtype="f4")
        with self.assertRaisesRegex(RuntimeError, "does not match requested"):
            _validate_grid_download(src, data, grid, ["red"])

    def test_transform_mismatch_raises(self):
        """Regression test for the actual production bug: Earth Engine
        returned a raster with the requested width/height/CRS but a
        geotransform corresponding to roughly the whole source Landsat
        scene rather than the small requested AOI."""
        grid = _make_grid()
        src = _matching_dataset(grid)
        src.transform = Affine(946.22, 0.0, 394770.8, 0.0, -1150.02, 5056978.8)
        data = np.ones((1, grid.height, grid.width), dtype="f4")
        with self.assertRaisesRegex(RuntimeError, "geotransform mismatch"):
            _validate_grid_download(src, data, grid, ["red"])

    def test_bounds_no_overlap_raises(self):
        """Covers a dataset whose *transform* matches the target grid (so
        the earlier transform check passes) but whose self-reported
        *bounds* don't agree with that transform -- i.e. internally
        inconsistent/corrupted raster metadata, which the transform check
        alone can't catch since it never looks at `src.bounds`."""
        grid = _make_grid()
        src = _matching_dataset(grid)
        src.bounds = BoundingBox(
            grid.transform.c + 10_000_000, grid.transform.f - 10_000_000,
            grid.transform.c + 10_000_100, grid.transform.f - 9_999_900,
        )
        data = np.ones((1, grid.height, grid.width), dtype="f4")
        with self.assertRaisesRegex(RuntimeError, "do not overlap"):
            _validate_grid_download(src, data, grid, ["red"])

    def test_all_zero_raster_raises(self):
        """Regression test for the actual production bug: the corrupted
        download's pixel data was all zero."""
        grid = _make_grid()
        src = _matching_dataset(grid)
        data = np.zeros((1, grid.height, grid.width), dtype="f4")
        with self.assertRaisesRegex(RuntimeError, "entirely zero"):
            _validate_grid_download(src, data, grid, ["red"])

    def test_partial_zero_is_allowed(self):
        """A raster that is legitimately zero in only some pixels (e.g.
        real edge-of-swath fill) must NOT be flagged as broken."""
        grid = _make_grid()
        src = _matching_dataset(grid)
        data = np.ones((1, grid.height, grid.width), dtype="f4")
        data[0, 0, 0] = 0.0
        _validate_grid_download(src, data, grid, ["red"])  # must not raise


class GridBoundsTests(unittest.TestCase):
    def test_grid_bounds_matches_transform(self):
        grid = _make_grid(width=10, height=8, res=30.0, x0=500000.0, y0=5000000.0)
        xmin, ymin, xmax, ymax = _grid_bounds(grid)
        self.assertEqual((xmin, ymax), (500000.0, 5000000.0))
        self.assertEqual(xmax - xmin, 10 * 30.0)
        self.assertEqual(ymax - ymin, 8 * 30.0)


@unittest.skipUnless(
    os.environ.get("GEE_PROJECT"),
    "integration test requires live Earth Engine credentials + GEE_PROJECT",
)
class GeeDownloadIntegrationTest(unittest.TestCase):
    """End-to-end check of the real _grid_region/_download path (not just
    the synthetic validation tests above) against a real, small Earth
    Engine request. Opt-in only -- needs GEE_PROJECT set and prior
    `earthengine-authenticate`."""

    def test_download_matches_requested_grid(self):
        from data_loader.providers import get_provider

        grid = _make_grid(width=20, height=16, res=30.0, x0=545250.0, y0=4903860.0)
        provider = get_provider("gee", gee_project=os.environ["GEE_PROJECT"])
        provider._ensure_init()
        import ee

        img = ee.Image("LANDSAT/LC08/C02/T1_L2/LC08_046029_20230715").select(["SR_B4"])
        data = provider._download(img, ["SR_B4"], grid)  # must not raise
        self.assertEqual(data.shape, (1, grid.height, grid.width))
        self.assertTrue(np.any(data))


if __name__ == "__main__":
    unittest.main()
