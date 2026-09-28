"""Tests for the USGS Landsat Collection 2 ARD provider
(data_loader.providers.usgs_ard): distinct product family from scene-based
USGS_C2_L2, tile-identity provenance, and the direct-USGS read path
(M2M per-band signed URLs -- individual COGs, never bundles, no AWS).

No network access required -- the M2M HTTP calls and rasterio.open are
mocked. Real pystac.Item/Asset objects are used so `.properties`/`.assets`
behave realistically. M2M response shapes here mirror what was captured
live on 2026-09-18 (see the provider module docstring).
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pystac

from data_loader.product_contract import SCENE, USGS_ARD_SR, USGS_C2_L2, ProductIdentity
from data_loader.providers.base import Grid, SceneRef
from data_loader.providers.usgs_ard import (
    BAND_DOWNLOAD_PRODUCT_CODE,
    M2M_DATASET,
    UsgsArdProvider,
    _tile_product_id,
)

TILE_ID = "LC09_CU_003004_20230909_20230914_02"
STAC_ITEM_ID = f"{TILE_ID}_SR"


def _ard_item(item_id=STAC_ITEM_ID, platform="LANDSAT_9", scene_count=3) -> pystac.Item:
    item = pystac.Item(
        id=item_id,
        geometry={"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]},
        bbox=[0, 0, 1, 1],
        datetime=datetime(2023, 9, 9, 18, 56, 29, tzinfo=timezone.utc),
        properties={
            "platform": platform,
            "eo:cloud_cover": 0.108,
            "landsat:grid_horizontal": "03",
            "landsat:grid_vertical": "04",
            "landsat:grid_region": "CU",
            "landsat:scene_count": scene_count,
            "landsat:cloud_shadow_cover": 0.2281,
            "landsat:snow_ice_cover": 0.0105,
            "landsat:fill": 43.2456,
            "start_datetime": "2023-09-09T18:56:17.0Z",
            "end_datetime": "2023-09-09T18:56:41.0Z",
            "proj:epsg": None,
            "proj:shape": [5000, 5000],
            "proj:transform": [30, 0, -2115585, 0, -30, 2714805],
            "proj:wkt2": 'PROJCS["AEA WGS84",...]',
        },
    )
    item.assets = {
        "blue": pystac.Asset(href="https://landsatlook.usgs.gov/tile/.../SR_B2.TIF"),
        "qa_pixel": pystac.Asset(href="https://landsatlook.usgs.gov/tile/.../QA_PIXEL.TIF"),
    }
    return item


def _scene() -> SceneRef:
    item = _ard_item()
    return SceneRef(id=item.id, date=date(2023, 9, 9), cloud_percent=0.108, handle=item)


def _grid() -> Grid:
    from rasterio.transform import Affine

    return Grid(crs="EPSG:32610", transform=Affine.identity(), width=2, height=2)


def _download_options_response(product_codes=("D772", "D773", BAND_DOWNLOAD_PRODUCT_CODE)):
    """Mirrors the live `download-options` shape: several bundle products
    plus the per-band D771 product whose secondaryDownloads are individual
    files."""
    options = []
    for code in product_codes:
        opt = {"id": f"opt_{code}", "productCode": code, "productName": f"product {code}", "secondaryDownloads": []}
        if code == BAND_DOWNLOAD_PRODUCT_CODE:
            opt["secondaryDownloads"] = [
                {"entityId": f"{TILE_ID}_{suffix}.TIF", "id": "sec_id", "filesize": 1000}
                for suffix in ("SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B6", "SR_B7",
                               "QA_PIXEL", "ST_B10", "TOA_B1", "QA_RADSAT")
            ]
        options.append(opt)
    return options


class CapabilitiesTests(unittest.TestCase):
    def test_distinct_product_family_from_scene_based_c2_l2(self):
        identity = UsgsArdProvider().capabilities()["landsat"]
        self.assertEqual(identity, ProductIdentity("landsat", USGS_ARD_SR, SCENE))
        self.assertNotEqual(identity.product_family, USGS_C2_L2)

    def test_processing_profile_declares_native_grid_and_direct_usgs_path(self):
        profile = UsgsArdProvider().processing_profile("landsat")
        self.assertIn("Albers", profile["nativeGrid"])
        self.assertIn("no AWS", profile["accessPath"])
        self.assertEqual(profile["sr_scale"], 2.75e-5)


class TileProductIdTests(unittest.TestCase):
    def test_strips_stac_product_suffix(self):
        self.assertEqual(_tile_product_id(STAC_ITEM_ID), TILE_ID)

    def test_rejects_unrecognized_id(self):
        with self.assertRaisesRegex(ValueError, "unrecognized ARD item id"):
            _tile_product_id("not-an-ard-id")


class SearchScenesTests(unittest.TestCase):
    def _provider_with_items(self, items):
        provider = UsgsArdProvider(username="u", token="t")
        fake_client = MagicMock()
        fake_client.search.return_value.items.return_value = iter(items)
        provider._client = fake_client
        return provider

    def test_rejects_non_landsat_sensor(self):
        with self.assertRaisesRegex(ValueError, "only supports sensor='landsat'"):
            UsgsArdProvider().search_scenes((0, 0, 1, 1), "sentinel2", None, None, None, None, 60)

    def test_rejects_pinned_version_policy(self):
        with self.assertRaisesRegex(ValueError, "no reprocessing-baseline concept"):
            UsgsArdProvider().search_scenes((0, 0, 1, 1), "landsat", None, None, None, None, 60, "pinned:05.00")

    def test_discovery_needs_no_credentials(self):
        """Discovery goes through the public LandsatLook STAC API -- only
        reading bytes needs an EROS account."""
        provider = UsgsArdProvider(username=None, token=None)
        fake_client = MagicMock()
        fake_client.search.return_value.items.return_value = iter([_ard_item()])
        provider._client = fake_client
        refs = provider.search_scenes((0, 0, 1, 1), "landsat", date(2023, 1, 1), date(2023, 12, 31), None, None, 60)
        self.assertEqual(len(refs), 1)

    def test_tile_identity_and_ard_provenance(self):
        provider = self._provider_with_items([_ard_item()])
        refs = provider.search_scenes((0, 0, 1, 1), "landsat", date(2023, 1, 1), date(2023, 12, 31), None, None, 60)

        ref = refs[0]
        self.assertEqual(ref.id, STAC_ITEM_ID)
        self.assertEqual(ref.date, date(2023, 9, 9))

        prov = ref.provenance
        self.assertEqual(prov.product_family, USGS_ARD_SR)
        self.assertEqual(prov.platform, "landsat-9")
        self.assertEqual(prov.extra["ard_tile_product_id"], TILE_ID)
        self.assertEqual(prov.extra["grid_region"], "CU")
        self.assertEqual(prov.extra["grid_horizontal"], "03")
        self.assertEqual(prov.extra["grid_vertical"], "04")
        # The structural marker that an ARD tile is not one WRS-2 scene.
        self.assertEqual(prov.extra["scene_count"], 3)
        self.assertEqual(prov.generation_time, "20230914")

    def test_search_retries_transient_failures(self):
        provider = UsgsArdProvider(retries=3)
        fake_client = MagicMock()
        fake_client.search.side_effect = [RuntimeError("flaky"), MagicMock(items=lambda: iter([_ard_item()]))]
        provider._client = fake_client
        with patch("time.sleep"):
            refs = provider.search_scenes((0, 0, 1, 1), "landsat", date(2023, 1, 1), date(2023, 12, 31), None, None, 60)
        self.assertEqual(len(refs), 1)
        self.assertEqual(fake_client.search.call_count, 2)


class SignedBandUrlTests(unittest.TestCase):
    def _provider(self):
        provider = UsgsArdProvider(username="u", token="t")
        provider._session = MagicMock()
        provider._session_born = 1e12  # far future so _ensure_login short-circuits
        return provider

    def test_requests_only_the_needed_bands_not_all_files(self):
        """The core "individual bands, not bundles" guarantee: a read of 6
        SR bands + QA must request exactly those 7 files, never the tile's
        other ~35 files and never a bundle product."""
        provider = self._provider()
        posted = {}

        def fake_post(endpoint, payload):
            posted[endpoint] = payload
            if endpoint == "download-options":
                return _download_options_response()
            return {
                "availableDownloads": [
                    {"entityId": d["entityId"], "url": f"https://signed/{d['entityId']}?requestSignature=x"}
                    for d in payload["downloads"]
                ],
                "preparingDownloads": [],
            }

        with patch.object(UsgsArdProvider, "_m2m_post", side_effect=fake_post):
            urls = provider._signed_band_urls(
                TILE_ID, ["SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B6", "SR_B7", "QA_PIXEL"]
            )

        self.assertEqual(len(urls), 7)
        requested = {d["entityId"] for d in posted["download-request"]["downloads"]}
        self.assertEqual(len(requested), 7)
        self.assertNotIn(f"{TILE_ID}_ST_B10.TIF", requested)  # other products not pulled
        self.assertNotIn(f"{TILE_ID}_TOA_B1.TIF", requested)

    def test_batches_all_bands_into_one_download_request(self):
        provider = self._provider()
        calls = []

        def fake_post(endpoint, payload):
            calls.append(endpoint)
            if endpoint == "download-options":
                return _download_options_response()
            return {
                "availableDownloads": [
                    {"entityId": d["entityId"], "url": f"https://signed/{d['entityId']}"}
                    for d in payload["downloads"]
                ],
                "preparingDownloads": [],
            }

        with patch.object(UsgsArdProvider, "_m2m_post", side_effect=fake_post):
            provider._signed_band_urls(TILE_ID, ["SR_B2", "SR_B3", "QA_PIXEL"])

        self.assertEqual(calls.count("download-request"), 1)  # batched, not one call per band

    def test_different_tiles_dont_deadlock_or_corrupt_each_others_cache(self):
        """The per-tile lock's actual job: two threads minting URLs for
        DIFFERENT tiles must not deadlock, and each must get back its own
        tile's URLs (not another tile's) -- regardless of how the
        underlying M2M calls are scheduled relative to each other. (They
        are, in fact, additionally serialized at the network-call level by
        _m2m_call_lock -- see the dedicated test for that -- but this test
        only asserts the per-tile bookkeeping is race-free.)

        `patch.object` is applied ONCE around the whole thread
        start/join (not inside each thread's target) -- entering/exiting
        a mock.patch context concurrently from multiple threads is itself
        unsafe and was observed to corrupt unrelated tests run afterward
        in the same process."""
        import threading

        provider = self._provider()

        def fake_post(endpoint, payload):
            if endpoint == "download-options":
                tile_id = payload["entityIds"][0]
                return [{
                    "id": "opt_D771", "productCode": BAND_DOWNLOAD_PRODUCT_CODE,
                    "secondaryDownloads": [{"entityId": f"{tile_id}_SR_B2.TIF", "id": "sec"}],
                }]
            return {
                "availableDownloads": [
                    {"entityId": d["entityId"], "url": f"https://signed/{d['entityId']}"}
                    for d in payload["downloads"]
                ],
                "preparingDownloads": [],
            }

        results = {}

        def mint(tile_id):
            results[tile_id] = provider._signed_band_urls(tile_id, ["SR_B2"])

        with patch.object(UsgsArdProvider, "_m2m_post", side_effect=fake_post):
            threads = [threading.Thread(target=mint, args=(f"TILE_{i}",)) for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        self.assertEqual(len(results), 2)
        self.assertEqual(results["TILE_0"], {"SR_B2": "https://signed/TILE_0_SR_B2.TIF"})
        self.assertEqual(results["TILE_1"], {"SR_B2": "https://signed/TILE_1_SR_B2.TIF"})

    def test_m2m_network_calls_across_different_tiles_are_serialized(self):
        """The account-level restriction, verified live under real scene-
        level concurrency: M2M rejects genuinely concurrent requests from
        one account with HTTP 500 `RATE_LIMIT: Your account does not
        support multiple requests at a time`. The fix is _m2m_call_lock,
        a GLOBAL lock around the actual `session.post()` in _m2m_post --
        this test exercises the real _m2m_post (mocking only
        `session.post`, not `_m2m_post` itself, so the lock is genuinely
        under test) and confirms two different tiles' M2M calls serialize
        rather than overlap: total wall time for 2 concurrent mints (each
        needing 2 M2M calls) should be close to 4x the per-call delay, not
        ~1x."""
        import threading
        import time

        provider = self._provider()
        delay = 0.1

        def fake_session_post(url, json=None, timeout=None):
            time.sleep(delay)
            resp = MagicMock(status_code=200, ok=True)
            if url.endswith("download-options"):
                tile_id = json["entityIds"][0]
                resp.json.return_value = {"data": [{
                    "id": "opt_D771", "productCode": BAND_DOWNLOAD_PRODUCT_CODE,
                    "secondaryDownloads": [{"entityId": f"{tile_id}_SR_B2.TIF", "id": "sec"}],
                }]}
            else:  # download-request
                resp.json.return_value = {"data": {
                    "availableDownloads": [
                        {"entityId": d["entityId"], "url": f"https://signed/{d['entityId']}"}
                        for d in json["downloads"]
                    ],
                    "preparingDownloads": [],
                }}
            return resp

        provider._session.post.side_effect = fake_session_post
        results = {}

        def mint(tile_id):
            results[tile_id] = provider._signed_band_urls(tile_id, ["SR_B2"])

        t0 = time.perf_counter()
        threads = [threading.Thread(target=mint, args=(f"TILE_{i}",)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        elapsed = time.perf_counter() - t0

        self.assertEqual(len(results), 2)
        # 2 tiles x 2 M2M calls each = 4 total network calls, fully
        # serialized -> ~4*delay. Allow slack, but this must clearly
        # exceed 2x delay (which is what unserialized/2-way-parallel
        # would look like) to prove serialization is actually happening.
        self.assertGreater(elapsed, 3 * delay)

    def test_rate_limit_error_retries_with_backoff(self):
        """Defense in depth: even with the client-side lock preventing
        THIS process's own concurrent M2M calls, another process/session
        sharing the same EROS account could still trigger RATE_LIMIT --
        it must be retried with backoff, not raised immediately."""
        provider = self._provider()
        attempts = []

        def fake_session_post(url, json=None, timeout=None):
            attempts.append(url)
            if len(attempts) < 3:
                resp = MagicMock(status_code=500, ok=False)
                resp.json.return_value = {"errorCode": "RATE_LIMIT", "errorMessage": "Your account does not support multiple requests at a time."}
            else:
                resp = MagicMock(status_code=200, ok=True)
                resp.json.return_value = {"data": [{"id": "opt", "productCode": BAND_DOWNLOAD_PRODUCT_CODE, "secondaryDownloads": []}]}
            return resp

        provider.retries = 4
        provider._session.post.side_effect = fake_session_post
        with patch("time.sleep"):
            provider._m2m_post("download-options", {"datasetName": M2M_DATASET, "entityIds": [TILE_ID]})

        self.assertEqual(len(attempts), 3)  # 2 rate-limited attempts, then success

    def test_rate_limit_exhausting_retries_raises_clearly(self):
        provider = self._provider()

        def fake_session_post(url, json=None, timeout=None):
            resp = MagicMock(status_code=500, ok=False)
            resp.json.return_value = {"errorCode": "RATE_LIMIT", "errorMessage": "Your account does not support multiple requests at a time."}
            return resp

        provider.retries = 2
        provider._session.post.side_effect = fake_session_post
        with patch("time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "unreachable/rate-limited after 2 attempts"):
                provider._m2m_post("download-options", {})

    def test_same_tile_requested_concurrently_mints_once(self):
        """The other half of the guarantee: concurrent reads of the SAME
        tile must still coalesce to one network round trip, not one per
        thread."""
        import threading
        import time

        provider = self._provider()
        call_count = {"n": 0}
        count_lock = threading.Lock()

        def fake_post(endpoint, payload):
            if endpoint == "download-options":
                return _download_options_response()
            with count_lock:
                call_count["n"] += 1
            time.sleep(0.05)
            return {
                "availableDownloads": [
                    {"entityId": d["entityId"], "url": f"https://signed/{d['entityId']}"}
                    for d in payload["downloads"]
                ],
                "preparingDownloads": [],
            }

        def mint():
            provider._band_url(TILE_ID, "SR_B2", ["SR_B2"])

        with patch.object(UsgsArdProvider, "_m2m_post", side_effect=fake_post):
            threads = [threading.Thread(target=mint) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(call_count["n"], 1)  # coalesced, not 4 separate mints

    def test_label_respects_m2m_50_char_limit(self):
        provider = self._provider()
        captured = {}

        def fake_post(endpoint, payload):
            if endpoint == "download-options":
                return _download_options_response()
            captured["label"] = payload["label"]
            return {
                "availableDownloads": [
                    {"entityId": d["entityId"], "url": "https://signed/x"} for d in payload["downloads"]
                ],
                "preparingDownloads": [],
            }

        with patch.object(UsgsArdProvider, "_m2m_post", side_effect=fake_post):
            provider._signed_band_urls(TILE_ID, ["SR_B2"])

        self.assertLessEqual(len(captured["label"]), 50)

    def test_raises_when_per_band_product_absent(self):
        """If USGS ever stopped offering D771, falling back to a bundle
        would silently change the access model -- fail loudly instead."""
        provider = self._provider()

        def fake_post(endpoint, payload):
            return _download_options_response(product_codes=("D772", "D773"))

        with patch.object(UsgsArdProvider, "_m2m_post", side_effect=fake_post):
            with self.assertRaisesRegex(RuntimeError, "no per-band download product"):
                provider._signed_band_urls(TILE_ID, ["SR_B2"])

    def test_raises_when_m2m_queues_instead_of_returning_urls(self):
        provider = self._provider()

        def fake_post(endpoint, payload):
            if endpoint == "download-options":
                return _download_options_response()
            return {"availableDownloads": [], "preparingDownloads": [{"entityId": "x"}]}

        with patch.object(UsgsArdProvider, "_m2m_post", side_effect=fake_post):
            with self.assertRaisesRegex(RuntimeError, "did not return"):
                provider._signed_band_urls(TILE_ID, ["SR_B2"])


class ReadSceneBandsTests(unittest.TestCase):
    class _FakeVRT:
        def __init__(self, src, **kw):
            self._src = src

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, band):
            if self._src == "qa":
                arr = np.zeros((2, 2), dtype="uint16")
                arr[0, 0] = 1 << 3  # cloud bit
                return arr
            return np.full((2, 2), 10000, dtype="uint16")

    def _patched_read(self, provider, bands, pixel_cloud_mask):
        opened = []

        def fake_open(path):
            opened.append(path)
            tag = "qa" if "QA_PIXEL" in path else "band"
            m = MagicMock()
            m.__enter__ = lambda s: tag
            m.__exit__ = lambda *a: False
            return m

        def fake_urls(tile_id, suffixes):
            return {s: f"https://signed/{tile_id}_{s}.TIF?requestSignature=x" for s in suffixes}

        with patch.object(UsgsArdProvider, "_signed_band_urls", side_effect=fake_urls), \
             patch("rasterio.open", side_effect=fake_open), \
             patch("rasterio.vrt.WarpedVRT", self._FakeVRT):
            out = provider.read_scene_bands(_scene(), "landsat", bands, _grid(), pixel_cloud_mask)
        return out, opened

    def test_reads_through_vsicurl_and_applies_scale_offset(self):
        provider = UsgsArdProvider(username="u", token="t")
        out, opened = self._patched_read(provider, ["blue"], False)
        self.assertTrue(all(p.startswith("/vsicurl/https://signed/") for p in opened))
        self.assertAlmostEqual(float(out["blue"][0, 0]), 10000 * 2.75e-5 - 0.2, places=6)

    def test_qa_mask_uses_collection2_bitmask(self):
        provider = UsgsArdProvider(username="u", token="t")
        out, _ = self._patched_read(provider, ["blue"], True)
        self.assertTrue(np.isnan(out["blue"][0, 0]))   # cloud bit -> masked
        self.assertFalse(np.isnan(out["blue"][0, 1]))  # clear -> kept

    def test_read_without_credentials_raises_actionable_error(self):
        # Constructed with no explicit credentials AND no env fallback --
        # patch.dict(clear=True) matters because the provider legitimately
        # reads USGS_M2M_USERNAME/TOKEN from the environment, which is
        # often populated on a developer machine.
        with patch.dict("os.environ", {}, clear=True):
            provider = UsgsArdProvider(username=None, token=None)
        with self.assertRaisesRegex(RuntimeError, "USGS EROS credentials"):
            provider.read_scene_bands(_scene(), "landsat", ["blue"], _grid(), False)

    def test_expired_signed_url_is_reminted_once(self):
        provider = UsgsArdProvider(username="u", token="t")
        mint_calls = []
        open_calls = []

        def fake_urls(tile_id, suffixes):
            mint_calls.append(suffixes)
            return {s: f"https://signed/{tile_id}_{s}.TIF?sig={len(mint_calls)}" for s in suffixes}

        def fake_open(path):
            open_calls.append(path)
            if len(open_calls) == 1:
                raise RuntimeError("HTTP response code: 403")
            m = MagicMock()
            m.__enter__ = lambda s: "band"
            m.__exit__ = lambda *a: False
            return m

        with patch.object(UsgsArdProvider, "_signed_band_urls", side_effect=fake_urls), \
             patch("rasterio.open", side_effect=fake_open), \
             patch("rasterio.vrt.WarpedVRT", self._FakeVRT):
            out = provider.read_scene_bands(_scene(), "landsat", ["blue"], _grid(), False)

        self.assertIn("blue", out)
        self.assertEqual(len(open_calls), 2)   # one retry
        self.assertEqual(len(mint_calls), 2)   # re-minted, bounded

    def test_non_auth_read_error_is_not_retried(self):
        provider = UsgsArdProvider(username="u", token="t")
        open_calls = []

        def fake_open(path):
            open_calls.append(path)
            raise RuntimeError("No such file or directory")

        with patch.object(UsgsArdProvider, "_signed_band_urls",
                          side_effect=lambda t, s: {x: "https://signed/x" for x in s}), \
             patch("rasterio.open", side_effect=fake_open), \
             patch("rasterio.vrt.WarpedVRT", self._FakeVRT):
            with self.assertRaises(RuntimeError):
                provider.read_scene_bands(_scene(), "landsat", ["blue"], _grid(), False)

        self.assertEqual(len(open_calls), 1)  # fails fast


class TransportRetryTests(unittest.TestCase):
    def test_connection_errors_retry_api_errors_do_not(self):
        import requests

        provider = UsgsArdProvider(username="u", token="t", retries=3)
        session = MagicMock()
        session.post.side_effect = requests.ConnectionError("refused")
        provider._session = session
        provider._session_born = 1e12

        with patch("time.sleep"), \
             patch.object(UsgsArdProvider, "_ensure_login", return_value=session):
            with self.assertRaisesRegex(RuntimeError, "unreachable/rate-limited after 3 attempts"):
                provider._m2m_post("download-options", {})
        self.assertEqual(session.post.call_count, 3)

    def test_api_error_raises_immediately_without_retry(self):
        provider = UsgsArdProvider(username="u", token="t", retries=3)
        session = MagicMock()
        resp = MagicMock(status_code=200, ok=True)
        resp.json.return_value = {"errorCode": "INPUT_INVALID", "errorMessage": "bad"}
        session.post.return_value = resp
        provider._session = session
        provider._session_born = 1e12

        with patch.object(UsgsArdProvider, "_ensure_login", return_value=session):
            with self.assertRaisesRegex(RuntimeError, "INPUT_INVALID"):
                provider._m2m_post("download-request", {})
        self.assertEqual(session.post.call_count, 1)  # deterministic error, not retried


class SourceMetadataTests(unittest.TestCase):
    def test_returns_full_stac_item_without_ephemeral_signed_urls(self):
        provider = UsgsArdProvider()
        record = provider.source_metadata(_scene())
        self.assertEqual(record["id"], STAC_ITEM_ID)
        self.assertEqual(record["properties"]["landsat:scene_count"], 3)
        # Signed M2M read URLs are ephemeral and must never be recorded as
        # durable provenance.
        self.assertNotIn("requestSignature", str(record))


if __name__ == "__main__":
    unittest.main()
