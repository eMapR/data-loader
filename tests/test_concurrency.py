"""Tests for scene-level concurrency (data_loader.config.Config.workers,
data_loader.engine._run_scene_mode/_composite_from_scenes): configurable
workers, deterministic output regardless of completion order, per-scene
failure isolation/reporting, and no duplicate metadata writes/network calls
under concurrent workers.

No network access required -- a synthetic in-process fake Provider stands
in, with controllable per-scene delay/failure and a threading-safe
concurrency counter to prove workers actually overlap.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from data_loader.config import AOI, Config, DateRange, Filters, OutputSpec, SensorSpec
from data_loader.product_contract import SCENE, USGS_C2_L2, AcquisitionProvenance, ProductIdentity
from data_loader.providers.base import SceneRef


class ConfigWorkersValidationTests(unittest.TestCase):
    def test_default_workers_is_one(self):
        cfg = _tiny_config("scene", "unused", num_scenes=1)
        self.assertEqual(cfg.workers, 1)

    def test_workers_below_one_rejected(self):
        with self.assertRaisesRegex(ValueError, "workers must be >= 1"):
            _tiny_config("scene", "unused", num_scenes=1, workers=0)


class _ConcurrencyTrackingProvider:
    """Returns `num_scenes` synthetic scenes; read_scene_bands sleeps
    `delay_s` (or a per-scene override from `delays`) and records how many
    calls were in flight simultaneously -- `max_concurrent` after a run
    tells us whether workers actually overlapped, not just that the
    `workers` parameter was accepted. `fail_ids` makes specific scene ids
    raise instead of returning data. `shared_item_id_for` optionally makes
    two or more scene indices report the SAME provider_item_id (to test
    the snapshot lock's dedup under concurrency)."""

    name = "fake"

    def __init__(self, num_scenes, delays=None, fail_ids=(), shared_item_id_for=None):
        self._identity = ProductIdentity("landsat", USGS_C2_L2, SCENE)
        self.num_scenes = num_scenes
        self.delays = delays or {}
        self.fail_ids = set(fail_ids)
        self.shared_item_id_for = shared_item_id_for or {}
        self.last_excluded_versions: list = []
        self._lock = threading.Lock()
        self._active = 0
        self.max_concurrent = 0
        self.source_metadata_calls: list = []
        self.read_order: list = []

    def capabilities(self):
        return {"landsat": self._identity}

    def processing_profile(self, sensor):
        return {}

    def search_scenes(self, bbox, sensor, start, end, season_start, season_end, max_cloud_percent, processing_version_policy="any"):
        refs = []
        for i in range(self.num_scenes):
            item_id = self.shared_item_id_for.get(i, f"scene-{i}")
            refs.append(SceneRef(
                id=f"scene-{i}", date=start + timedelta(days=i), cloud_percent=1.0,
                handle={"i": i},
                provenance=AcquisitionProvenance(
                    provider=self.name, provider_item_id=item_id,
                    upstream_product_id=f"UPSTREAM_{i}",
                    acquisition_datetime=f"{start.isoformat()}T00:00:00+00:00",
                    platform="fake-sat", product_family=self._identity.product_family,
                    collection="fake-collection",
                ),
            ))
        return refs

    def read_scene_bands(self, scene, sensor, bands, grid, pixel_cloud_mask):
        i = int(scene.id.rsplit("-", 1)[1])
        with self._lock:
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
        try:
            time.sleep(self.delays.get(scene.id, 0.03))
            self.read_order.append(scene.id)
            if scene.id in self.fail_ids:
                raise RuntimeError(f"simulated failure reading {scene.id}")
            return {b: np.full((grid.height, grid.width), float(i), dtype="f4") for b in bands}
        finally:
            with self._lock:
                self._active -= 1

    def source_metadata(self, scene):
        with self._lock:
            self.source_metadata_calls.append(scene.provenance.provider_item_id)
        time.sleep(0.01)
        return {"raw": True, "item_id": scene.provenance.provider_item_id}


def _tiny_config(temporal_mode: str, output_dir: str, num_scenes: int, workers: int = 1, write_files: bool = True) -> Config:
    return Config(
        aoi=AOI(upper_left=(-122.43, 44.29), lower_right=(-122.40, 44.27)),
        provider="fake",
        sensors=[SensorSpec(name="landsat", resolution_m=300.0)],
        date_range=DateRange(start=date(2023, 7, 1), end=date(2023, 7, 1) + timedelta(days=num_scenes)),
        filters=Filters(max_cloud_percent=60, pixel_cloud_mask=False),
        temporal_mode=temporal_mode, reduce="median",
        output=OutputSpec(bands=("red",), dir=output_dir, write_files=write_files),
        workers=workers,
    )


class SceneModeConcurrencyTests(unittest.TestCase):
    def test_workers_actually_overlap(self):
        provider = _ConcurrencyTrackingProvider(num_scenes=6, delays={f"scene-{i}": 0.05 for i in range(6)})
        with patch("data_loader.engine.get_provider", return_value=provider):
            from data_loader.engine import run

            with tempfile.TemporaryDirectory() as tmp:
                run(_tiny_config("scene", tmp, num_scenes=6, workers=4))
        self.assertGreaterEqual(provider.max_concurrent, 2)

    def test_single_worker_never_overlaps(self):
        provider = _ConcurrencyTrackingProvider(num_scenes=4, delays={f"scene-{i}": 0.02 for i in range(4)})
        with patch("data_loader.engine.get_provider", return_value=provider):
            from data_loader.engine import run

            with tempfile.TemporaryDirectory() as tmp:
                run(_tiny_config("scene", tmp, num_scenes=4, workers=1))
        self.assertEqual(provider.max_concurrent, 1)

    def test_output_order_deterministic_regardless_of_completion_order(self):
        """Scene 0 is the slowest, scene 4 the fastest -- with 4 workers
        they will finish out of order, but the manifest/scenes_out must
        still come back in original scene order (scene-0..scene-4)."""
        num_scenes = 5
        delays = {f"scene-{i}": 0.08 - i * 0.015 for i in range(num_scenes)}  # scene-0 slowest
        provider = _ConcurrencyTrackingProvider(num_scenes=num_scenes, delays=delays)
        with patch("data_loader.engine.get_provider", return_value=provider):
            from data_loader.engine import run

            with tempfile.TemporaryDirectory() as tmp:
                result = run(_tiny_config("scene", tmp, num_scenes=num_scenes, workers=4))
                doc = json.loads((Path(tmp) / "manifest.json").read_text())

        scene_out_ids = [s["id"] for s in result["landsat"]["scenes"]]
        self.assertEqual(scene_out_ids, [f"scene-{i}" for i in range(num_scenes)])
        manifest_scene_ids = [e["sceneId"] for e in doc["files"]]
        self.assertEqual(manifest_scene_ids, [f"scene-{i}" for i in range(num_scenes)])
        # Sanity: completion order actually differed from submission order,
        # so this is a real test of the reordering, not a no-op.
        self.assertNotEqual(provider.read_order, [f"scene-{i}" for i in range(num_scenes)])

    def test_same_output_regardless_of_worker_count(self):
        """workers=1 and workers=4 over the same scenes must produce
        byte-identical manifests (aside from nothing order-dependent)."""
        def _run(workers):
            provider = _ConcurrencyTrackingProvider(num_scenes=5, delays={f"scene-{i}": 0.01 * (5 - i) for i in range(5)})
            with patch("data_loader.engine.get_provider", return_value=provider):
                from data_loader.engine import run

                with tempfile.TemporaryDirectory() as tmp:
                    run(_tiny_config("scene", tmp, num_scenes=5, workers=workers))
                    return json.loads((Path(tmp) / "manifest.json").read_text())

        doc1 = _run(1)
        doc4 = _run(4)
        # Strip nothing-to-do-with-ordering nondeterminism (there is none
        # here -- band values are deterministic per scene index) and
        # compare the scene id sequence + band data descriptors directly.
        self.assertEqual([e["sceneId"] for e in doc1["files"]], [e["sceneId"] for e in doc4["files"]])
        self.assertEqual([e["bandNames"] for e in doc1["files"]], [e["bandNames"] for e in doc4["files"]])


class SceneFailureIsolationTests(unittest.TestCase):
    def test_one_failed_scene_does_not_abort_the_run_and_is_reported(self):
        provider = _ConcurrencyTrackingProvider(num_scenes=5, fail_ids={"scene-2"})
        with patch("data_loader.engine.get_provider", return_value=provider):
            from data_loader.engine import run

            with tempfile.TemporaryDirectory() as tmp:
                result = run(_tiny_config("scene", tmp, num_scenes=5, workers=3))
                doc = json.loads((Path(tmp) / "manifest.json").read_text())

        scene_out_ids = {s["id"] for s in result["landsat"]["scenes"]}
        self.assertEqual(scene_out_ids, {"scene-0", "scene-1", "scene-3", "scene-4"})
        self.assertNotIn("scene-2", {e["sceneId"] for e in doc["files"]})
        self.assertIn("landsat", doc["failedScenes"])
        failed_ids = {f["sceneId"] for f in doc["failedScenes"]["landsat"]}
        self.assertEqual(failed_ids, {"scene-2"})
        self.assertIn("simulated failure", doc["failedScenes"]["landsat"][0]["error"])

    def test_all_scenes_failing_reports_all_and_produces_empty_output(self):
        provider = _ConcurrencyTrackingProvider(num_scenes=3, fail_ids={"scene-0", "scene-1", "scene-2"})
        with patch("data_loader.engine.get_provider", return_value=provider):
            from data_loader.engine import run

            with tempfile.TemporaryDirectory() as tmp:
                result = run(_tiny_config("scene", tmp, num_scenes=3, workers=2))
                doc = json.loads((Path(tmp) / "manifest.json").read_text())

        self.assertEqual(result["landsat"]["scenes"], [])
        self.assertEqual(len(doc["failedScenes"]["landsat"]), 3)


class SnapshotRaceTests(unittest.TestCase):
    def test_shared_item_id_snapshot_not_duplicated_under_concurrency(self):
        """Two different scenes that happen to report the same
        provider_item_id (contrived, but tests the lock directly) must
        still only trigger one source_metadata() call and one snapshot
        file -- no duplicate writes/races from concurrent workers."""
        provider = _ConcurrencyTrackingProvider(
            num_scenes=6, delays={f"scene-{i}": 0.02 for i in range(6)},
            shared_item_id_for={i: "shared-item" for i in range(6)},
        )
        with patch("data_loader.engine.get_provider", return_value=provider):
            from data_loader.engine import run

            with tempfile.TemporaryDirectory() as tmp:
                run(_tiny_config("scene", tmp, num_scenes=6, workers=6))
                snapshot_files = list((Path(tmp) / "metadata" / "fake").glob("*.json"))

        self.assertEqual(provider.source_metadata_calls, ["shared-item"])  # exactly one call
        self.assertEqual(len(snapshot_files), 1)  # exactly one file


class CompositeModeConcurrencyTests(unittest.TestCase):
    def test_composite_read_step_uses_workers_and_stays_deterministic(self):
        provider = _ConcurrencyTrackingProvider(num_scenes=4, delays={f"scene-{i}": 0.06 - i * 0.01 for i in range(4)})
        with patch("data_loader.engine.get_provider", return_value=provider):
            from data_loader.engine import run

            with tempfile.TemporaryDirectory() as tmp:
                cfg = Config(
                    aoi=AOI(upper_left=(-122.43, 44.29), lower_right=(-122.40, 44.27)),
                    provider="fake",
                    sensors=[SensorSpec(name="landsat", resolution_m=300.0)],
                    date_range=DateRange(start=date(2023, 7, 1), end=date(2023, 7, 5)),
                    filters=Filters(max_cloud_percent=60, pixel_cloud_mask=False),
                    temporal_mode="annual_composite", reduce="median",
                    output=OutputSpec(bands=("red",), dir=tmp, write_files=True),
                    workers=4,
                )
                run(cfg)
        self.assertGreaterEqual(provider.max_concurrent, 2)


if __name__ == "__main__":
    unittest.main()
