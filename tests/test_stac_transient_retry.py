"""Tests for the generic transient-network-error retry in
data_loader.providers.stac_common (StacProvider.read_scene_bands):
ordinary timeouts/connection drops are retried with backoff, independent
of and in addition to the existing expired-signed-URL retry, and — unlike
that one — apply to EVERY StacProvider regardless of `needs_signing` (so
Earth Search benefits too, not just Planetary Computer).

This closes a real gap found live during the 2026-09-20 complete-pipeline
benchmark: a genuine `RasterioIOError: CURL error: Recv failure:
Operation timed out` failed a scene outright because only expired-auth
errors were retried at the time.

No network access required -- rasterio.open/WarpedVRT are mocked.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from rasterio.errors import RasterioIOError

from data_loader.product_contract import USGS_C2_L2
from data_loader.providers.base import Grid, SceneRef
from data_loader.providers.stac_common import (
    SensorStacSpec,
    StacConfig,
    StacProvider,
    _looks_like_transient_network_error,
)


class _FakeAsset:
    def __init__(self, href):
        self.href = href


class _FakeItem:
    def __init__(self, item_id, assets):
        self.id = item_id
        self.assets = assets

    def to_dict(self):
        return {"id": self.id, "assets": {k: {"href": v.href} for k, v in self.assets.items()}}


class _FakeVRT:
    def __init__(self, src, **kwargs):
        pass

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


def _provider(needs_signing: bool, max_transient_retries: int = 3, max_sign_retries: int = 2) -> StacProvider:
    config = StacConfig(
        name="planetary_computer" if needs_signing else "aws_earth_search",
        stac_url="https://example/stac",
        needs_signing=needs_signing,
        sensors={
            "landsat": SensorStacSpec(
                collection="landsat-c2-l2", product_family=USGS_C2_L2,
                band_map={"blue": "blue", "qa": "qa_pixel"},
                qa_kind="landsat_qa_pixel", sr_scale=2.75e-5, sr_offset=-0.2,
            ),
        },
    )
    return StacProvider(config, max_sign_retries=max_sign_retries, max_transient_retries=max_transient_retries)


def _scene() -> SceneRef:
    item = _FakeItem("LC08_L2SP_046029_20230715_02_T1", {"blue": _FakeAsset("https://example/SR_B2.TIF")})
    return SceneRef(id=item.id, date=date(2023, 7, 15), cloud_percent=1.0, handle=item)


def _grid() -> Grid:
    from rasterio.transform import Affine

    return Grid(crs="EPSG:32610", transform=Affine.identity(), width=2, height=2)


class DetectionTests(unittest.TestCase):
    def test_recognizes_the_real_error_hit_live(self):
        self.assertTrue(_looks_like_transient_network_error(
            RasterioIOError("CURL error: Recv failure: Operation timed out")
        ))

    def test_recognizes_generic_warpedvrt_wrapper_message(self):
        self.assertTrue(_looks_like_transient_network_error(
            RasterioIOError("Read failed. See previous exception for details.")
        ))

    def test_recognizes_common_variants(self):
        for msg in [
            "Connection timed out",
            "connection reset by peer",
            "Connection refused",
            "Could not connect to server",
            "Temporary failure in name resolution",
            "Could not resolve host: example.com",
            "SSL connect error",
            "EOF occurred in violation of protocol",
            "Broken pipe",
            "Send failure: Broken pipe",
        ]:
            with self.subTest(msg=msg):
                self.assertTrue(_looks_like_transient_network_error(Exception(msg)))

    def test_does_not_match_auth_or_unrelated_errors(self):
        for msg in [
            "HTTP response code: 403",
            "AuthenticationFailed",
            "No such file or directory",
            "HTTP response code: 404",
        ]:
            with self.subTest(msg=msg):
                self.assertFalse(_looks_like_transient_network_error(Exception(msg)))


class RetrySucceedsTests(unittest.TestCase):
    def test_transient_error_then_success(self):
        provider = _provider(needs_signing=False)
        scene = _scene()
        grid = _grid()
        open_calls = []

        def fake_open(href):
            open_calls.append(href)
            if len(open_calls) == 1:
                raise RasterioIOError("CURL error: Recv failure: Operation timed out")
            return _FakeSrc()

        with patch("rasterio.open", side_effect=fake_open), \
             patch("rasterio.vrt.WarpedVRT", _FakeVRT), \
             patch("time.sleep") as mock_sleep:
            out = provider.read_scene_bands(scene, "landsat", ["blue"], grid, pixel_cloud_mask=False)

        self.assertIn("blue", out)
        self.assertEqual(len(open_calls), 2)
        mock_sleep.assert_called_once_with(2)  # first transient retry backs off 2s

    def test_multiple_transient_errors_with_increasing_backoff(self):
        provider = _provider(needs_signing=False, max_transient_retries=3)
        scene = _scene()
        grid = _grid()
        open_calls = []

        def fake_open(href):
            open_calls.append(href)
            if len(open_calls) <= 2:
                raise RasterioIOError("Connection reset by peer")
            return _FakeSrc()

        with patch("rasterio.open", side_effect=fake_open), \
             patch("rasterio.vrt.WarpedVRT", _FakeVRT), \
             patch("time.sleep") as mock_sleep:
            out = provider.read_scene_bands(scene, "landsat", ["blue"], grid, pixel_cloud_mask=False)

        self.assertIn("blue", out)
        self.assertEqual(len(open_calls), 3)
        self.assertEqual([c.args[0] for c in mock_sleep.call_args_list], [2, 4])  # backoff increases

    def test_no_signing_needed_and_no_resign_attempted_on_transient_path(self):
        """A transient network error must retry the SAME href -- no
        re-signing, even for a needs_signing=True provider, since the
        failure has nothing to do with the URL's validity."""
        provider = _provider(needs_signing=True)
        scene = _scene()
        grid = _grid()
        open_calls = []

        def fake_open(href):
            open_calls.append(href)
            if len(open_calls) == 1:
                raise RasterioIOError("Operation timed out")
            return _FakeSrc()

        with patch("planetary_computer.sign", side_effect=lambda href, copy=True: href + "?st=SIGNED"), \
             patch("rasterio.open", side_effect=fake_open), \
             patch("rasterio.vrt.WarpedVRT", _FakeVRT), \
             patch("time.sleep"):
            provider.read_scene_bands(scene, "landsat", ["blue"], grid, pixel_cloud_mask=False)

        self.assertEqual(open_calls[0], open_calls[1])  # identical href both times, no re-sign


class RetryBoundedTests(unittest.TestCase):
    def test_exhausting_transient_budget_raises(self):
        provider = _provider(needs_signing=False, max_transient_retries=2)
        scene = _scene()
        grid = _grid()
        open_calls = []

        def fake_open(href):
            open_calls.append(href)
            raise RasterioIOError("Operation timed out")

        with patch("rasterio.open", side_effect=fake_open), \
             patch("rasterio.vrt.WarpedVRT", _FakeVRT), \
             patch("time.sleep"):
            with self.assertRaises(RasterioIOError):
                provider.read_scene_bands(scene, "landsat", ["blue"], grid, pixel_cloud_mask=False)

        self.assertEqual(len(open_calls), 3)  # 1 initial + 2 retries, never more

    def test_earth_search_benefits_too_not_just_pc(self):
        """The core generalization this fix provides: needs_signing=False
        (Earth Search) previously had NO retry path at all for this
        failure class."""
        provider = _provider(needs_signing=False, max_transient_retries=1)
        scene = _scene()
        grid = _grid()
        open_calls = []

        def fake_open(href):
            open_calls.append(href)
            if len(open_calls) == 1:
                raise RasterioIOError("Read failed. See previous exception for details.")
            return _FakeSrc()

        with patch("rasterio.open", side_effect=fake_open), \
             patch("rasterio.vrt.WarpedVRT", _FakeVRT), \
             patch("time.sleep"):
            out = provider.read_scene_bands(scene, "landsat", ["blue"], grid, pixel_cloud_mask=False)

        self.assertIn("blue", out)
        self.assertEqual(len(open_calls), 2)


class IndependentBudgetsTests(unittest.TestCase):
    def test_sign_and_transient_retries_dont_share_or_starve_each_others_budget(self):
        """A read that hits BOTH failure classes across its attempts must
        get the full budget of each, not a combined/shared counter."""
        provider = _provider(needs_signing=True, max_sign_retries=1, max_transient_retries=1)
        scene = _scene()
        grid = _grid()
        open_calls = []

        def fake_open(href):
            open_calls.append(href)
            if len(open_calls) == 1:
                raise RasterioIOError("HTTP response code: 403")  # auth -> re-sign
            if len(open_calls) == 2:
                raise RasterioIOError("Operation timed out")  # transient -> backoff+retry same href
            return _FakeSrc()

        with patch("planetary_computer.sign", side_effect=lambda href, copy=True: href + "?resigned"), \
             patch("rasterio.open", side_effect=fake_open), \
             patch("rasterio.vrt.WarpedVRT", _FakeVRT), \
             patch("time.sleep"):
            out = provider.read_scene_bands(scene, "landsat", ["blue"], grid, pixel_cloud_mask=False)

        self.assertIn("blue", out)
        self.assertEqual(len(open_calls), 3)  # both retry paths used, one each, within their own budgets


if __name__ == "__main__":
    unittest.main()
