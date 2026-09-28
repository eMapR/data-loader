"""Tests for Planetary Computer signed-URL expiration handling
(data_loader.providers.stac_common.StacProvider): durable scene identity is
the STAC collection/item/asset, signed URLs are resolved lazily at read
time, and a read that fails in a way consistent with an expired SAS token
is re-signed and retried a small, bounded number of times.

No network access required -- rasterio.open/WarpedVRT and
planetary_computer.sign are mocked.
"""
from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from rasterio.errors import RasterioIOError

from data_loader.product_contract import USGS_C2_L2
from data_loader.providers.base import Grid, SceneRef
from data_loader.providers.stac_common import (
    SensorStacSpec,
    StacConfig,
    StacProvider,
    _looks_like_expired_auth,
)


class _FakeAsset:
    def __init__(self, href):
        self.href = href


class _FakeItem:
    """Stands in for a pystac Item -- only the attributes read_scene_bands
    actually touches (`.id`, `.assets`)."""

    def __init__(self, item_id, assets):
        self.id = item_id
        self.assets = assets

    def to_dict(self):
        return {"id": self.id, "assets": {k: {"href": v.href} for k, v in self.assets.items()}}


class _FakeVRT:
    """Stands in for rasterio.vrt.WarpedVRT -- always returns a constant
    2x2 array regardless of grid, since these tests are about
    signing/retry control flow, not resampling."""

    def __init__(self, src, **kwargs):
        self._src = src

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, band):
        return np.ones((2, 2), dtype="f4")


class _FakeSrc:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _pc_provider(max_sign_retries: int = 2) -> StacProvider:
    config = StacConfig(
        name="planetary_computer",
        stac_url="https://planetarycomputer.microsoft.com/api/stac/v1",
        needs_signing=True,
        sensors={
            "landsat": SensorStacSpec(
                collection="landsat-c2-l2",
                product_family=USGS_C2_L2,
                band_map={"blue": "blue", "qa": "qa_pixel"},
                qa_kind="landsat_qa_pixel",
                sr_scale=2.75e-5, sr_offset=-0.2,
            ),
        },
    )
    return StacProvider(config, max_sign_retries=max_sign_retries)


def _scene() -> SceneRef:
    item = _FakeItem(
        "LC08_L2SP_046029_20230715_02_T1",
        {"blue": _FakeAsset("https://landsateuwest.blob.core.windows.net/.../SR_B2.TIF")},
    )
    return SceneRef(id=item.id, date=date(2023, 7, 15), cloud_percent=1.0, handle=item)


def _grid() -> Grid:
    from rasterio.transform import Affine

    return Grid(crs="EPSG:32610", transform=Affine.identity(), width=2, height=2)


class AuthFailureDetectionTests(unittest.TestCase):
    def test_403_variants_detected(self):
        self.assertTrue(_looks_like_expired_auth(RasterioIOError(
            "'/vsicurl/https://...': HTTP response code: 403"
        )))
        self.assertTrue(_looks_like_expired_auth(Exception(
            "Server returned HTTP response code: 401 for URL: ..."
        )))
        self.assertTrue(_looks_like_expired_auth(Exception("<Code>AuthenticationFailed</Code>")))
        self.assertTrue(_looks_like_expired_auth(Exception("ExpiredAuthenticationToken")))

    def test_unrelated_failures_not_treated_as_auth(self):
        self.assertFalse(_looks_like_expired_auth(RasterioIOError(
            "'/vsicurl/https://...': HTTP response code: 404"
        )))
        self.assertFalse(_looks_like_expired_auth(RasterioIOError("No such file or directory")))


class NormalReadTests(unittest.TestCase):
    def test_valid_url_reads_once_and_signs_once(self):
        provider = _pc_provider()
        scene = _scene()
        grid = _grid()
        sign_calls = []

        def fake_sign(href, copy=True):
            sign_calls.append(href)
            return href + "?st=SIGNED"

        with patch("planetary_computer.sign", side_effect=fake_sign), \
             patch("rasterio.open", return_value=_FakeSrc()) as mock_open, \
             patch("rasterio.vrt.WarpedVRT", _FakeVRT):
            out = provider.read_scene_bands(scene, "landsat", ["blue"], grid, pixel_cloud_mask=False)

        self.assertIn("blue", out)
        self.assertEqual(sign_calls, ["https://landsateuwest.blob.core.windows.net/.../SR_B2.TIF"])
        mock_open.assert_called_once_with(sign_calls[0] + "?st=SIGNED")


class StaleUrlRefreshTests(unittest.TestCase):
    def test_403_then_refresh_then_success(self):
        provider = _pc_provider(max_sign_retries=2)
        scene = _scene()
        grid = _grid()
        sign_calls = []

        def fake_sign(href, copy=True):
            sign_calls.append(href)
            return f"{href}?st=SIGNED{len(sign_calls)}"

        open_calls = []

        def fake_open(href):
            open_calls.append(href)
            if len(open_calls) == 1:
                raise RasterioIOError("'/vsicurl/...': HTTP response code: 403")
            return _FakeSrc()

        with patch("planetary_computer.sign", side_effect=fake_sign), \
             patch("rasterio.open", side_effect=fake_open), \
             patch("rasterio.vrt.WarpedVRT", _FakeVRT):
            out = provider.read_scene_bands(scene, "landsat", ["blue"], grid, pixel_cloud_mask=False)

        self.assertIn("blue", out)
        self.assertEqual(len(open_calls), 2)
        self.assertEqual(len(sign_calls), 2)  # re-signed exactly once, not repeatedly
        self.assertNotEqual(open_calls[0], open_calls[1])  # a genuinely different signed URL


class RepeatedFailureBoundedTests(unittest.TestCase):
    def test_repeated_403_fails_after_bounded_attempts(self):
        provider = _pc_provider(max_sign_retries=2)
        scene = _scene()
        grid = _grid()
        open_calls = []

        def fake_open(href):
            open_calls.append(href)
            raise RasterioIOError("'/vsicurl/...': HTTP response code: 403")

        with patch("planetary_computer.sign", side_effect=lambda href, copy=True: href + "?resigned"), \
             patch("rasterio.open", side_effect=fake_open), \
             patch("rasterio.vrt.WarpedVRT", _FakeVRT):
            with self.assertRaises(RasterioIOError):
                provider.read_scene_bands(scene, "landsat", ["blue"], grid, pixel_cloud_mask=False)

        # max_sign_retries=2 -> at most 3 total attempts (1 initial + 2 retries).
        self.assertEqual(len(open_calls), 3)

    def test_non_auth_error_is_not_retried(self):
        provider = _pc_provider(max_sign_retries=2)
        scene = _scene()
        grid = _grid()
        open_calls = []

        def fake_open(href):
            open_calls.append(href)
            raise RasterioIOError("No such file or directory")

        with patch("planetary_computer.sign", side_effect=lambda href, copy=True: href), \
             patch("rasterio.open", side_effect=fake_open), \
             patch("rasterio.vrt.WarpedVRT", _FakeVRT):
            with self.assertRaises(RasterioIOError):
                provider.read_scene_bands(scene, "landsat", ["blue"], grid, pixel_cloud_mask=False)

        self.assertEqual(len(open_calls), 1)  # fails fast, no wasted retries


class IdentityUnchangedAfterRefreshTests(unittest.TestCase):
    def test_scene_ref_and_item_unchanged_after_a_refresh(self):
        """The refresh must never mutate the durable SceneRef/STAC item --
        source_metadata()/provenance built from it must stay stable
        regardless of how many times its assets were (re-)signed."""
        provider = _pc_provider(max_sign_retries=2)
        scene = _scene()
        original_href = scene.handle.assets["blue"].href
        original_dict = scene.handle.to_dict()
        grid = _grid()
        open_calls = []

        def fake_open(href):
            open_calls.append(href)
            if len(open_calls) == 1:
                raise RasterioIOError("HTTP response code: 403")
            return _FakeSrc()

        with patch("planetary_computer.sign", side_effect=lambda href, copy=True: href + "?resigned"), \
             patch("rasterio.open", side_effect=fake_open), \
             patch("rasterio.vrt.WarpedVRT", _FakeVRT):
            provider.read_scene_bands(scene, "landsat", ["blue"], grid, pixel_cloud_mask=False)

        self.assertEqual(scene.handle.assets["blue"].href, original_href)
        self.assertEqual(scene.handle.to_dict(), original_dict)
        self.assertEqual(scene.id, "LC08_L2SP_046029_20230715_02_T1")
        # source_metadata() (used for the provenance snapshot) must reflect
        # the same untouched, unsigned identity.
        from data_loader.providers.stac_common import StacProvider as SP

        record = provider.source_metadata(scene) if hasattr(provider, "source_metadata") else None
        self.assertEqual(record, original_dict)


class EarthSearchUnaffectedTests(unittest.TestCase):
    def test_no_signing_no_retry_plumbing_for_non_signing_provider(self):
        """needs_signing=False (Earth Search) must never attempt to sign or
        retry -- a genuine failure there raises immediately, exactly as
        before this change."""
        config = StacConfig(
            name="aws_earth_search",
            stac_url="https://earth-search.aws.element84.com/v1",
            needs_signing=False,
            sensors={
                "landsat": SensorStacSpec(
                    collection="landsat-c2-l2",
                    product_family=USGS_C2_L2,
                    band_map={"blue": "blue", "qa": "qa_pixel"},
                    qa_kind="landsat_qa_pixel",
                    sr_scale=2.75e-5, sr_offset=-0.2,
                ),
            },
        )
        provider = StacProvider(config)
        scene = _scene()
        grid = _grid()
        open_calls = []

        def fake_open(href):
            open_calls.append(href)
            raise RasterioIOError("HTTP response code: 403")

        with patch("planetary_computer.sign") as mock_sign, \
             patch("rasterio.open", side_effect=fake_open), \
             patch("rasterio.vrt.WarpedVRT", _FakeVRT):
            with self.assertRaises(RasterioIOError):
                provider.read_scene_bands(scene, "landsat", ["blue"], grid, pixel_cloud_mask=False)

        mock_sign.assert_not_called()
        self.assertEqual(len(open_calls), 1)  # no retry attempted


if __name__ == "__main__":
    unittest.main()
