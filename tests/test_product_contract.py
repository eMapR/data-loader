"""Tests for the product-contract / provenance layer:
data_loader.product_contract, each provider's capabilities()/
processing_profile(), stac_common.py's Sentinel-2 version selection, and
engine.py's manifest provenance (per-scene and composite).

Everything here uses synthetic data or fake providers -- no network access
or credentials required. Real STAC/GEE/M2M items are approximated with
minimal fakes carrying only the fields the code under test actually reads.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from data_loader.product_contract import (
    ESA_S2_L2A,
    ESA_S2_L2A_HARMONIZED,
    FIXED_COMPOSITE,
    GLAD_ARD,
    SCENE,
    USGS_C2_L2,
    AcquisitionProvenance,
    ProductContract,
    ProductIdentity,
    VersionCandidate,
    parse_version_policy,
    resolve_contract,
    select_processing_versions,
)
from data_loader.providers import get_provider
from data_loader.providers.base import SceneRef


# --------------------------------------------------------------------------
# 1. Provider/product capability matching + unsupported-product rejection
# --------------------------------------------------------------------------

class CapabilityMatchingTests(unittest.TestCase):
    """None of these need network or credentials -- capabilities()/
    processing_profile() are static, I/O-free on every provider."""

    def test_planetary_computer_landsat_is_usgs_c2_l2_scene(self):
        pc = get_provider("planetary_computer")
        self.assertEqual(
            pc.capabilities()["landsat"],
            ProductIdentity("landsat", USGS_C2_L2, SCENE),
        )

    def test_earth_search_landsat_is_usgs_c2_l2_scene(self):
        es = get_provider("aws_earth_search")
        self.assertEqual(
            es.capabilities()["landsat"],
            ProductIdentity("landsat", USGS_C2_L2, SCENE),
        )

    def test_gee_landsat_is_usgs_c2_l2_scene(self):
        gee = get_provider("gee")
        self.assertEqual(
            gee.capabilities()["landsat"],
            ProductIdentity("landsat", USGS_C2_L2, SCENE),
        )

    def test_usgs_m2m_landsat_is_usgs_c2_l2_scene(self):
        m2m = get_provider("usgs_m2m")
        self.assertEqual(
            m2m.capabilities()["landsat"],
            ProductIdentity("landsat", USGS_C2_L2, SCENE),
        )

    def test_landsat_product_contract_agrees_across_all_four_providers(self):
        """The Landsat audit's central claim, encoded as a test: PC, Earth
        Search, GEE, and M2M all declare the same ProductIdentity for
        landsat."""
        identities = {
            name: get_provider(name).capabilities()["landsat"]
            for name in ("planetary_computer", "aws_earth_search", "gee", "usgs_m2m")
        }
        self.assertEqual(len(set(identities.values())), 1, identities)

    def test_glad_is_distinct_from_usgs_c2_l2(self):
        glad = get_provider("glad_ard")
        identity = glad.capabilities()["landsat"]
        self.assertEqual(identity.product_family, GLAD_ARD)
        self.assertNotEqual(identity.product_family, USGS_C2_L2)
        self.assertEqual(identity.temporal_product, FIXED_COMPOSITE)

    def test_gee_sentinel2_is_harmonized_not_raw_esa_l2a(self):
        """GEE's COPERNICUS/S2_SR_HARMONIZED must not be labeled the same
        product_family as PC/Earth Search's raw esa_s2_l2a."""
        gee = get_provider("gee")
        identity = gee.capabilities()["sentinel2"]
        self.assertEqual(identity.product_family, ESA_S2_L2A_HARMONIZED)
        self.assertNotEqual(identity.product_family, ESA_S2_L2A)

    def test_pc_and_earth_search_sentinel2_agree_and_differ_from_gee(self):
        pc_id = get_provider("planetary_computer").capabilities()["sentinel2"]
        es_id = get_provider("aws_earth_search").capabilities()["sentinel2"]
        gee_id = get_provider("gee").capabilities()["sentinel2"]
        self.assertEqual(pc_id, es_id)
        self.assertNotEqual(pc_id, gee_id)


class ResolveContractTests(unittest.TestCase):
    def test_default_none_accepts_whatever_provider_supplies(self):
        identity = ProductIdentity("landsat", GLAD_ARD, FIXED_COMPOSITE)
        contract = resolve_contract("landsat", identity, None, "any")
        self.assertEqual(contract.product_family, GLAD_ARD)

    def test_mismatched_explicit_request_is_rejected(self):
        identity = ProductIdentity("landsat", GLAD_ARD, FIXED_COMPOSITE)
        with self.assertRaisesRegex(ValueError, "not the same scientific product"):
            resolve_contract("landsat", identity, USGS_C2_L2, "any")

    def test_matching_explicit_request_is_accepted(self):
        identity = ProductIdentity("landsat", USGS_C2_L2, SCENE)
        contract = resolve_contract("landsat", identity, USGS_C2_L2, "latest")
        self.assertEqual(contract, ProductContract("landsat", USGS_C2_L2, SCENE, "latest"))

    def test_malformed_policy_is_rejected(self):
        identity = ProductIdentity("landsat", USGS_C2_L2, SCENE)
        with self.assertRaises(ValueError):
            resolve_contract("landsat", identity, None, "whatever-i-feel-like")


# --------------------------------------------------------------------------
# 2. Processing-version policy parsing + selection
# --------------------------------------------------------------------------

class ParseVersionPolicyTests(unittest.TestCase):
    def test_simple_policies(self):
        self.assertEqual(parse_version_policy("any"), ("any", None))
        self.assertEqual(parse_version_policy("latest"), ("latest", None))
        self.assertEqual(parse_version_policy("allow_mixed"), ("allow_mixed", None))

    def test_pinned_policy(self):
        self.assertEqual(parse_version_policy("pinned:05.00"), ("pinned", "05.00"))

    def test_unknown_policy_rejected(self):
        with self.assertRaises(ValueError):
            parse_version_policy("whatever")
        with self.assertRaises(ValueError):
            parse_version_policy("pinned:")


def _candidate(key, baseline, generation_time, payload):
    return VersionCandidate(key=key, baseline=baseline, generation_time=generation_time, payload=payload)


class SelectProcessingVersionsTests(unittest.TestCase):
    """Synthetic Sentinel-2-shaped duplicate scenario: one acquisition
    (same key) with an original (02.14) and a reprocessed (05.00) version,
    plus one acquisition with only a single version -- modeled directly on
    the real Earth Search duplicates found for 2020-07-14/19/29."""

    def setUp(self):
        self.duplicated = [
            _candidate(("GS2A_2020", "T10TDR"), "02.14", "2020-07-29T23:31:17Z", "old"),
            _candidate(("GS2A_2020", "T10TDR"), "05.00", "2023-05-05T09:45:43Z", "new"),
        ]
        self.single = [_candidate(("GS2B_2020b", "T10TDR"), "02.14", "2020-07-14T23:21:16Z", "only")]

    def test_duplicate_grouping_by_stable_key_not_item_suffix(self):
        """The two duplicated candidates share one key despite having no
        naming relationship in `payload` -- grouping must key off `key`,
        not any provider-side item id/suffix."""
        selected = select_processing_versions(self.duplicated, "latest")
        self.assertEqual(len(selected), 1)

    def test_latest_picks_highest_baseline(self):
        selected = select_processing_versions(self.duplicated, "latest")
        self.assertEqual(selected, ["new"])

    def test_any_behaves_like_latest_by_default(self):
        """"any" must never silently include both -- it resolves to the
        same safe choice as "latest"."""
        selected = select_processing_versions(self.duplicated, "any")
        self.assertEqual(selected, ["new"])

    def test_pinned_baseline_selects_only_matching_item(self):
        selected = select_processing_versions(self.duplicated, "pinned:02.14")
        self.assertEqual(selected, ["old"])

    def test_pinned_baseline_with_no_match_drops_the_acquisition(self):
        selected = select_processing_versions(self.duplicated, "pinned:99.99")
        self.assertEqual(selected, [])

    def test_allow_mixed_returns_both(self):
        selected = select_processing_versions(self.duplicated, "allow_mixed")
        self.assertEqual(set(selected), {"old", "new"})

    def test_single_version_acquisition_passes_through_under_every_policy(self):
        for policy in ("any", "latest", "pinned:02.14", "allow_mixed"):
            with self.subTest(policy=policy):
                self.assertEqual(select_processing_versions(self.single, policy), ["only"])

    def test_unresolvable_tie_raises_rather_than_picking_silently(self):
        """Two candidates, same key, identical baseline AND generation
        time -- a genuine ambiguity "latest" cannot break. Must fail
        loudly, not arbitrarily pick one."""
        tied = [
            _candidate(("k", "t"), "05.00", "2023-01-01T00:00:00Z", "a"),
            _candidate(("k", "t"), "05.00", "2023-01-01T00:00:00Z", "b"),
        ]
        with self.assertRaisesRegex(RuntimeError, "cannot resolve a single processing version"):
            select_processing_versions(tied, "latest")

    def test_unresolvable_tie_does_not_raise_under_allow_mixed(self):
        tied = [
            _candidate(("k", "t"), "05.00", "2023-01-01T00:00:00Z", "a"),
            _candidate(("k", "t"), "05.00", "2023-01-01T00:00:00Z", "b"),
        ]
        selected = select_processing_versions(tied, "allow_mixed")
        self.assertEqual(set(selected), {"a", "b"})


# --------------------------------------------------------------------------
# 3. stac_common.py's Sentinel-2/Landsat provenance-building helpers
# --------------------------------------------------------------------------

class _FakeAsset:
    def __init__(self, href):
        self.href = href


class _FakeItem:
    """Stands in for a pystac Item -- only the attributes stac_common.py
    actually reads (`.id`, `.properties`, `.datetime`, `.assets`)."""

    def __init__(self, id, properties, dt, assets=None):
        self.id = id
        self.properties = properties
        self.datetime = dt
        self.assets = assets or {}


class StacProvenanceTests(unittest.TestCase):
    def test_landsat_product_id_extracted_from_asset_href(self):
        from data_loader.providers.stac_common import _extract_landsat_product_id

        item = _FakeItem(
            "LC08_L2SP_046029_20230715_02_T1", {},
            datetime(2023, 7, 15, tzinfo=timezone.utc),
            assets={"red": _FakeAsset(
                "https://example/.../LC08_L2SP_046029_20230715_20230724_02_T1/LC08_L2SP_046029_20230715_20230724_02_T1_SR_B4.TIF"
            )},
        )
        self.assertEqual(
            _extract_landsat_product_id(item),
            "LC08_L2SP_046029_20230715_20230724_02_T1",
        )

    def test_s2_stable_key_strips_baseline_suffix(self):
        from data_loader.providers.stac_common import _s2_stable_key

        old = {"s2:datatake_id": "GS2A_20200719T190921_026508_N02.14", "grid:code": "MGRS-10TDR"}
        new = {"s2:datatake_id": "GS2A_20200719T190921_026508_N05.00", "grid:code": "MGRS-10TDR"}
        self.assertEqual(_s2_stable_key(old), _s2_stable_key(new))

    def test_s2_stable_key_distinguishes_different_tiles(self):
        from data_loader.providers.stac_common import _s2_stable_key

        a = {"s2:datatake_id": "GS2A_20200719T190921_026508_N02.14", "grid:code": "MGRS-10TDR"}
        b = {"s2:datatake_id": "GS2A_20200719T190921_026508_N02.14", "grid:code": "MGRS-10TDS"}
        self.assertNotEqual(_s2_stable_key(a), _s2_stable_key(b))

    def test_provenance_preserves_full_timestamp_and_baseline(self):
        from data_loader.product_contract import ESA_S2_L2A
        from data_loader.providers.stac_common import SensorStacSpec, _build_provenance

        spec = SensorStacSpec(
            collection="sentinel-2-l2a", product_family=ESA_S2_L2A,
            band_map={"qa": "scl"}, qa_kind="sentinel2_scl", sr_scale=0.0001, sr_offset=0.0,
        )
        item = _FakeItem(
            "S2A_10TDR_20200714_0_L2A",
            {
                "s2:product_uri": "S2B_MSIL2A_20200714T190919_N0214_R056_T10TDR_20200714T232116.SAFE",
                "s2:processing_baseline": "02.14",
                "s2:generation_time": "2020-07-14T23:21:16.000000Z",
                "s2:datatake_id": "GS2B_20200714T190919_017528_N02.14",
                "platform": "sentinel-2b",
            },
            datetime(2020, 7, 14, 19, 21, 57, 558000, tzinfo=timezone.utc),
        )
        prov = _build_provenance("aws_earth_search", item, spec)
        self.assertEqual(prov.acquisition_datetime, "2020-07-14T19:21:57.558000+00:00")
        self.assertEqual(prov.processing_baseline, "02.14")
        self.assertEqual(prov.generation_time, "2020-07-14T23:21:16.000000Z")
        self.assertEqual(
            prov.upstream_product_id,
            "S2B_MSIL2A_20200714T190919_N0214_R056_T10TDR_20200714T232116.SAFE",
        )


# --------------------------------------------------------------------------
# 4. engine.py: provenance in items.jsonl (scene mode + composite mode)
# --------------------------------------------------------------------------

class EngineProvenanceTests(unittest.TestCase):
    def test_scene_rows_record_full_provenance_and_product(self):
        import json
        import tempfile

        from tests.fakes import FakeProvider, july, make_config, read_items, run_with

        with tempfile.TemporaryDirectory() as tmp:
            run_with(FakeProvider(july(2)), make_config(tmp))
            row = read_items(tmp)[0]
            manifest = json.loads((Path(tmp) / "manifest.json").read_text())
        self.assertEqual(manifest["products"]["landsat"]["productFamily"], USGS_C2_L2)
        self.assertEqual(row["provenance"]["upstream_product_id"], "UPSTREAM_scene-0")
        self.assertIn("+00:00", row["provenance"]["acquisition_datetime"])

    def test_composite_row_lists_every_contributing_scene(self):
        import tempfile

        from tests.fakes import FakeProvider, july, make_config, read_items, run_with

        with tempfile.TemporaryDirectory() as tmp:
            run_with(FakeProvider(july(2)), make_config(tmp, temporal_mode="annual_composite"))
            row = read_items(tmp)[0]
        self.assertEqual(row["type"], "composite")
        self.assertEqual({s["provenance"]["upstream_product_id"] for s in row["sources"]},
                         {"UPSTREAM_scene-0", "UPSTREAM_scene-1"})

    def test_unsupported_product_family_request_fails_before_any_read(self):
        import tempfile

        from data_loader.config import ConfigError
        from tests.fakes import FakeProvider, july, make_config, run_with

        p = FakeProvider(july(2), product_family=GLAD_ARD)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp, sensors=[{"name": "landsat", "product_family": USGS_C2_L2}])
            with self.assertRaisesRegex(ConfigError, "not the same scientific product"):
                run_with(p, cfg)
        self.assertEqual(p.reads, [])


if __name__ == "__main__":
    unittest.main()
