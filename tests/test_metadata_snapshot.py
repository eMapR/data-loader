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

from data_loader.config import AOI, Config, DateRange, Filters, OutputSpec, SensorSpec
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

class _FakeProviderWithSnapshot:
    """Like test_product_contract.py's _FakeProvider, plus a
    call-counted source_metadata so dedup-within-a-run is verifiable."""

    name = "fake"

    def __init__(self, identity: ProductIdentity, num_scenes: int = 2):
        self._identity = identity
        self.num_scenes = num_scenes
        self.last_excluded_versions: list = []
        self.source_metadata_calls: list = []

    def capabilities(self):
        return {self._identity.sensor: self._identity}

    def processing_profile(self, sensor):
        return {}

    def search_scenes(self, bbox, sensor, start, end, season_start, season_end, max_cloud_percent, processing_version_policy="any"):
        import numpy as np  # noqa: F401

        refs = []
        for i in range(self.num_scenes):
            refs.append(SceneRef(
                id=f"fake-scene-{i}", date=start, cloud_percent=1.0,
                handle={"i": i},
                provenance=AcquisitionProvenance(
                    provider=self.name, provider_item_id=f"fake-scene-{i}",
                    upstream_product_id=f"UPSTREAM_{i}",
                    acquisition_datetime=f"{start.isoformat()}T00:00:00+00:00",
                    platform="fake-sat", product_family=self._identity.product_family,
                    collection="fake-collection",
                ),
            ))
        return refs

    def read_scene_bands(self, scene, sensor, bands, grid, pixel_cloud_mask):
        import numpy as np

        return {b: np.full((grid.height, grid.width), 0.5, dtype="f4") for b in bands}

    def source_metadata(self, scene):
        self.source_metadata_calls.append(scene.id)
        return {"raw": True, "id": scene.id}


def _tiny_config(temporal_mode: str, output_dir: str, bands=("red",), indices=()) -> Config:
    return Config(
        aoi=AOI(upper_left=(-122.43, 44.29), lower_right=(-122.40, 44.27)),
        provider="fake",
        sensors=[SensorSpec(name="landsat", resolution_m=300.0)],
        date_range=DateRange(start=date(2023, 7, 1), end=date(2023, 7, 31)),
        filters=Filters(max_cloud_percent=60, pixel_cloud_mask=False),
        temporal_mode=temporal_mode, reduce="median",
        output=OutputSpec(bands=bands, indices=indices, dir=output_dir, write_files=True),
    )


class EngineSnapshotWiringTests(unittest.TestCase):
    def test_source_metadata_ref_present_and_file_written(self):
        provider = _FakeProviderWithSnapshot(ProductIdentity("landsat", USGS_C2_L2, SCENE), num_scenes=1)
        with patch("data_loader.engine.get_provider", return_value=provider):
            import tempfile
            from data_loader.engine import run

            with tempfile.TemporaryDirectory() as tmp:
                run(_tiny_config("scene", tmp))
                doc = json.loads((Path(tmp) / "manifest.json").read_text())
                prov = doc["files"][0]["scenes"][0]["provenance"]
                self.assertIsNotNone(prov["source_metadata_ref"])
                snapshot_path = Path(tmp) / prov["source_metadata_ref"]
                self.assertTrue(snapshot_path.exists())
                self.assertEqual(json.loads(snapshot_path.read_text())["sourceRecord"], {"raw": True, "id": "fake-scene-0"})

    def test_same_scene_not_fetched_twice_within_one_run(self):
        """A scene contributing to both a bands file and an indices file
        (two manifest entries) must only trigger one source_metadata()
        call -- this is the "avoid unnecessarily multiplying network
        calls" guarantee for providers like GEE where that call is real
        network I/O."""
        provider = _FakeProviderWithSnapshot(ProductIdentity("landsat", USGS_C2_L2, SCENE), num_scenes=1)
        with patch("data_loader.engine.get_provider", return_value=provider):
            import tempfile
            from data_loader.engine import run

            with tempfile.TemporaryDirectory() as tmp:
                run(_tiny_config("scene", tmp, bands=("red",), indices=("ndvi",)))
                self.assertEqual(provider.source_metadata_calls.count("fake-scene-0"), 1)

    def test_composite_scenes_retain_source_metadata_refs(self):
        provider = _FakeProviderWithSnapshot(ProductIdentity("landsat", USGS_C2_L2, SCENE), num_scenes=3)
        with patch("data_loader.engine.get_provider", return_value=provider):
            import tempfile
            from data_loader.engine import run

            with tempfile.TemporaryDirectory() as tmp:
                run(_tiny_config("annual_composite", tmp))
                doc = json.loads((Path(tmp) / "manifest.json").read_text())
                entry = next(e for e in doc["files"] if e["kind"] == "bands")
                self.assertEqual(len(entry["scenes"]), 3)
                for s in entry["scenes"]:
                    ref = s["provenance"]["source_metadata_ref"]
                    self.assertIsNotNone(ref)
                    self.assertTrue((Path(tmp) / ref).exists())


if __name__ == "__main__":
    unittest.main()
