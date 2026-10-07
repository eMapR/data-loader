"""Tests for the source-metadata snapshot layer:
data_loader.metadata_snapshot, each provider's source_metadata(), and
engine.py's wiring of AcquisitionProvenance.source_metadata_ref.

No network access or credentials required in the normal suite -- GEE's
source_metadata() is tested with `ee.Image`/`_ensure_init` mocked out
(skipped if earthengine-api isn't installed at all), and GLAD's tag
inspection is tested with `rasterio.open` mocked.
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_loader.metadata_snapshot import sanitize_filename, snapshot_ref, write_snapshot
from data_loader.product_contract import (
    GLAD_ARD,
    SCENE,
    USGS_C2_L2,
    AcquisitionProvenance,
    ProductIdentity,
)
from data_loader.providers.base import SceneRef


# --------------------------------------------------------------------------
# 1. data_loader.metadata_snapshot itself
# --------------------------------------------------------------------------

class WriteSnapshotTests(unittest.TestCase):
    def test_sanitizes_slashes_and_special_characters(self):
        self.assertEqual(
            sanitize_filename("LANDSAT/LE07/C02/T1_L2/LE07_045029_20230704"),
            "LANDSAT_LE07_C02_T1_L2_LE07_045029_20230704",
        )

    def test_snapshot_ref_is_relative_and_provider_namespaced(self):
        ref = snapshot_ref("planetary_computer", "LC08_L2SP_046029_20230715_02_T1")
        self.assertEqual(ref, "metadata/planetary_computer/LC08_L2SP_046029_20230715_02_T1.json")

    def test_write_snapshot_wraps_source_record_verbatim(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            record = {"id": "abc", "properties": {"sci:doi": "10.5066/XYZ"}, "weird_key": [1, 2, 3]}
            ref = write_snapshot(Path(tmp), "planetary_computer", "abc", record)
            doc = json.loads((Path(tmp) / ref).read_text())
            self.assertEqual(set(doc.keys()), {"snapshot", "sourceRecord"})
            self.assertEqual(doc["sourceRecord"], record)  # untouched, not renamed/reshaped
            self.assertEqual(doc["snapshot"]["provider"], "planetary_computer")
            self.assertEqual(doc["snapshot"]["providerItemId"], "abc")
            self.assertEqual(doc["snapshot"]["capturedBy"], "DataLoader")

    def test_write_snapshot_is_idempotent_within_and_across_calls(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            write_snapshot(Path(tmp), "p", "id1", {"v": 1})
            full_path = Path(tmp) / snapshot_ref("p", "id1")
            original_mtime = full_path.stat().st_mtime_ns
            # Second write with DIFFERENT content must not overwrite --
            # same (provider, item_id) means "the same selected product",
            # so the first captured snapshot wins.
            write_snapshot(Path(tmp), "p", "id1", {"v": 2})
            self.assertEqual(full_path.stat().st_mtime_ns, original_mtime)
            self.assertEqual(json.loads(full_path.read_text())["sourceRecord"], {"v": 1})


# --------------------------------------------------------------------------
# 2. StacProvider.source_metadata -- durable identity vs. ephemeral hrefs
# --------------------------------------------------------------------------

class _FakeAsset:
    def __init__(self, href):
        self.href = href


class _FakeStacItem:
    def __init__(self, item_id, collection, properties, assets):
        self.id = item_id
        self.collection = collection
        self.properties = properties
        self.assets = assets

    def to_dict(self):
        return {
            "id": self.id,
            "collection": self.collection,
            "properties": self.properties,
            "assets": {k: {"href": v.href} for k, v in self.assets.items()},
        }


class StacSourceMetadataTests(unittest.TestCase):
    def test_full_stac_item_preserved_including_field_not_in_normalized_provenance(self):
        from data_loader.providers.stac_common import StacProvider, StacConfig, SensorStacSpec
        from data_loader.product_contract import USGS_C2_L2

        item = _FakeStacItem(
            "LC08_L2SP_046029_20230715_02_T1", "landsat-c2-l2",
            {"sci:doi": "10.5066/P9OGBGM6", "eo:cloud_cover": 1.59},  # sci:doi is NOT in AcquisitionProvenance
            {"red": _FakeAsset("https://signed.example/...SR_B4.TIF?st=...&sig=abc123")},
        )
        scene = SceneRef(id=item.id, date=date(2023, 7, 15), cloud_percent=1.59, handle=item)
        provider = StacProvider(StacConfig(name="planetary_computer", stac_url="", needs_signing=True, sensors={}))

        record = provider.source_metadata(scene)
        self.assertEqual(record["properties"]["sci:doi"], "10.5066/P9OGBGM6")

    def test_signed_href_is_not_the_only_durable_identity(self):
        """A snapshot must carry stable identifiers (collection + id)
        alongside whatever href happened to be current at capture time --
        the href is documented as ephemeral, not relied on as identity."""
        from data_loader.providers.stac_common import StacProvider, StacConfig

        item = _FakeStacItem(
            "LC08_L2SP_046029_20230715_02_T1", "landsat-c2-l2", {},
            {"red": _FakeAsset("https://signed.example/...?sig=will-expire")},
        )
        scene = SceneRef(id=item.id, date=date(2023, 7, 15), cloud_percent=1.0, handle=item)
        provider = StacProvider(StacConfig(name="planetary_computer", stac_url="", needs_signing=True, sensors={}))

        record = provider.source_metadata(scene)
        self.assertEqual(record["id"], "LC08_L2SP_046029_20230715_02_T1")
        self.assertEqual(record["collection"], "landsat-c2-l2")
        # The href is present (fine, per spec) but is not itself the id/collection.
        self.assertIn("sig=", record["assets"]["red"]["href"])


# --------------------------------------------------------------------------
# 3. GLAD ARD -- embedded-tag inspection, both outcomes
# --------------------------------------------------------------------------

class _FakeRasterioDataset:
    def __init__(self, tags, band_tags=None, count=8):
        self._tags = tags
        self._band_tags = band_tags or {}
        self.count = count

    def tags(self, band=None):
        return self._tags if band is None else self._band_tags.get(band, {})

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class GladSourceMetadataTests(unittest.TestCase):
    def _scene(self):
        from data_loader.providers.glad_ard import GladArdProvider

        provider = GladArdProvider()
        scene = SceneRef(
            id="interval_990", date=date(2023, 1, 1), cloud_percent=None,
            handle=["/vsis3/glad.landsat.ard/data/tiles/44N/123W_44N/990.tif"],
        )
        return provider, scene

    def test_no_embedded_tags_produces_explicit_absence_note(self):
        """Matches what was actually found live against GLAD ARD: only
        standard GDAL raster-structure tags, nothing source-specific."""
        provider, scene = self._scene()
        fake_ds = _FakeRasterioDataset(tags={"AREA_OR_POINT": "Area"})
        with patch("rasterio.open", return_value=fake_ds):
            record = provider.source_metadata(scene)
        self.assertIsNone(record["embeddedTileTags"])
        self.assertIn("no per-acquisition or processing information is embedded", record["note"])
        self.assertEqual(record["productFamily"], GLAD_ARD)
        self.assertEqual(record["intervalId"], "interval_990")

    def test_embedded_tags_are_preserved_when_present(self):
        provider, scene = self._scene()
        fake_ds = _FakeRasterioDataset(
            tags={"AREA_OR_POINT": "Area", "PROCESSING_DATE": "2023-02-01"},
            band_tags={1: {"WAVELENGTH": "0.48um"}},
        )
        with patch("rasterio.open", return_value=fake_ds):
            record = provider.source_metadata(scene)
        self.assertEqual(record["embeddedTileTags"]["datasetTags"], {"PROCESSING_DATE": "2023-02-01"})
        self.assertEqual(record["embeddedTileTags"]["bandTags"], {1: {"WAVELENGTH": "0.48um"}})
        self.assertIn("included above", record["note"])

    def test_tag_sample_is_cached_across_scenes_not_reopened_per_scene(self):
        provider, scene1 = self._scene()
        scene2 = SceneRef(id="interval_1006", date=date(2023, 1, 17), cloud_percent=None, handle=scene1.handle)
        fake_ds = _FakeRasterioDataset(tags={"AREA_OR_POINT": "Area"})
        with patch("rasterio.open", return_value=fake_ds) as mock_open:
            provider.source_metadata(scene1)
            provider.source_metadata(scene2)
            self.assertEqual(mock_open.call_count, 1)

    def test_no_fabricated_fields(self):
        """The record must not claim a real processing_baseline/generation
        time -- GLAD ARD genuinely doesn't expose either."""
        provider, scene = self._scene()
        with patch("rasterio.open", return_value=_FakeRasterioDataset(tags={"AREA_OR_POINT": "Area"})):
            record = provider.source_metadata(scene)
        self.assertNotIn("processing_baseline", record)
        self.assertNotIn("generation_time", record)
        self.assertFalse(record["reflectanceScale"]["verified"])


# --------------------------------------------------------------------------
# 4. GEE source_metadata -- selected scenes only, no per-candidate cost
# --------------------------------------------------------------------------

try:
    import ee  # noqa: F401
    _EE_AVAILABLE = True
except ImportError:
    _EE_AVAILABLE = False


@unittest.skipUnless(_EE_AVAILABLE, "earthengine-api not installed")
class GeeSourceMetadataTests(unittest.TestCase):
    def test_source_metadata_calls_getinfo_once_for_the_selected_image_only(self):
        from data_loader.providers.gee import GeeProvider

        provider = GeeProvider(project="fake-project")
        scene = SceneRef(
            id="LANDSAT/LC08/C02/T1_L2/LC08_046029_20230715", date=date(2023, 7, 15), cloud_percent=1.0,
            handle={"image_id": "LANDSAT/LC08/C02/T1_L2/LC08_046029_20230715", "collection": "LANDSAT/LC08/C02/T1_L2"},
        )
        fake_image = MagicMock()
        fake_image.getInfo.return_value = {"id": scene.id, "properties": {"LANDSAT_PRODUCT_ID": "LC08_L2SP_046029_20230715_20230724_02_T1"}}

        with patch.object(GeeProvider, "_ensure_init"), patch("ee.Image", return_value=fake_image) as mock_image_ctor:
            record = provider.source_metadata(scene)

        mock_image_ctor.assert_called_once_with(scene.handle["image_id"])
        fake_image.getInfo.assert_called_once()
        self.assertEqual(record["properties"]["LANDSAT_PRODUCT_ID"], "LC08_L2SP_046029_20230715_20230724_02_T1")


# --------------------------------------------------------------------------
# 5. engine.py wiring: source_metadata_ref in provenance, dedup, composites
# --------------------------------------------------------------------------

class EngineSnapshotWiringTests(unittest.TestCase):
    def test_source_metadata_ref_present_and_file_written(self):
        import tempfile

        from tests.fakes import FakeProvider, july, make_config, read_items, run_with

        with tempfile.TemporaryDirectory() as tmp:
            run_with(FakeProvider(july(1)), make_config(tmp))
            row = read_items(tmp)[0]
            ref = row["provenance"]["source_metadata_ref"]
            self.assertEqual(ref, row["sourceMetadata"])
            snapshot = Path(tmp) / ref
            self.assertTrue(snapshot.exists())
            self.assertEqual(json.loads(snapshot.read_text())["sourceRecord"], {"raw": True, "id": "scene-0"})

    def test_same_scene_not_fetched_twice_within_one_run(self):
        """bands + indices files for one scene: one source_metadata() call."""
        import tempfile

        from tests.fakes import FakeProvider, july, make_config, run_with

        p = FakeProvider(july(1))
        with tempfile.TemporaryDirectory() as tmp:
            run_with(p, make_config(tmp, output={"bands": ["red", "nir"], "indices": ["ndvi"]}))
        self.assertEqual(p.source_metadata_calls.count("scene-0"), 1)

    def test_composite_sources_retain_source_metadata_refs(self):
        import tempfile

        from tests.fakes import FakeProvider, july, make_config, read_items, run_with

        with tempfile.TemporaryDirectory() as tmp:
            run_with(FakeProvider(july(3)), make_config(tmp, temporal_mode="annual_composite"))
            row = read_items(tmp)[0]
            self.assertEqual(len(row["sources"]), 3)
            for s in row["sources"]:
                self.assertIsNotNone(s["sourceMetadata"])
                self.assertTrue((Path(tmp) / s["sourceMetadata"]).exists())


if __name__ == "__main__":
    unittest.main()
