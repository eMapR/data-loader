"""Scene-level concurrency (config.workers): workers really overlap,
the catalog is identical regardless of completion order or worker count,
one failed scene doesn't abort the run, and concurrent workers don't
duplicate metadata fetches/snapshots. Network-free (tests/fakes.py)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from data_loader.config import ConfigError
from tests.fakes import FakeProvider, july, make_config, read_items, run_with


def _strip_volatile(rows):
    return [{k: v for k, v in r.items() if k not in ("updated", "timing")} for r in rows]


class WorkersValidationTests(unittest.TestCase):
    def test_default_workers_is_one(self):
        self.assertEqual(make_config("unused").workers, 1)

    def test_workers_below_one_rejected(self):
        with self.assertRaisesRegex(ConfigError, "workers must be >= 1"):
            make_config("unused", workers=0)


class SceneModeConcurrencyTests(unittest.TestCase):
    def test_workers_actually_overlap(self):
        p = FakeProvider(july(6), delays={f"scene-{i}": 0.05 for i in range(6)})
        with tempfile.TemporaryDirectory() as tmp:
            run_with(p, make_config(tmp, workers=4))
        self.assertGreaterEqual(p.max_concurrent, 2)

    def test_single_worker_never_overlaps(self):
        p = FakeProvider(july(4), delays={f"scene-{i}": 0.02 for i in range(4)})
        with tempfile.TemporaryDirectory() as tmp:
            run_with(p, make_config(tmp, workers=1))
        self.assertEqual(p.max_concurrent, 1)

    def test_catalog_order_independent_of_completion_order(self):
        delays = {f"scene-{i}": 0.08 - i * 0.015 for i in range(5)}  # scene-0 slowest
        p = FakeProvider(july(5), delays=delays)
        with tempfile.TemporaryDirectory() as tmp:
            run_with(p, make_config(tmp, workers=4))
            ids = [r["itemId"] for r in read_items(tmp)]
        self.assertEqual(ids, [f"scene-{i}" for i in range(5)])
        self.assertNotEqual(p.reads, [f"scene-{i}" for i in range(5)])  # really finished out of order

    def test_same_catalog_regardless_of_worker_count(self):
        def catalog(workers):
            p = FakeProvider(july(5), delays={f"scene-{i}": 0.01 * (5 - i) for i in range(5)})
            with tempfile.TemporaryDirectory() as tmp:
                run_with(p, make_config(tmp, workers=workers))
                rows = read_items(tmp)
                for r in rows:  # same bytes on disk, independent of tmp dir
                    for f in r["files"]:
                        f.pop("path")
                return _strip_volatile(rows)

        self.assertEqual(catalog(1), catalog(4))


class FailureIsolationTests(unittest.TestCase):
    def test_one_failed_scene_does_not_abort_the_run_and_is_recorded(self):
        p = FakeProvider(july(5), fail={"scene-2": -1})
        with tempfile.TemporaryDirectory() as tmp:
            summary = run_with(p, make_config(tmp, workers=3))
            rows = {r["itemId"]: r for r in read_items(tmp)}
            manifest = json.loads((Path(tmp) / "manifest.json").read_text())
        self.assertEqual(summary.acquired_now, 4)
        self.assertEqual(summary.failed_now, 1)
        self.assertFalse(summary.complete)
        self.assertEqual(rows["scene-2"]["status"], "failed")
        self.assertIn("simulated failure", rows["scene-2"]["error"])
        self.assertEqual(rows["scene-2"]["attempts"], 3)  # retried within the run up to max_attempts
        self.assertEqual(p.reads.count("scene-2"), 3)
        self.assertEqual(manifest["coverage"]["counts"]["failed"], 1)
        self.assertFalse(manifest["dataset"]["complete"])

    def test_all_scenes_failing(self):
        p = FakeProvider(july(3), fail={f"scene-{i}": -1 for i in range(3)})
        with tempfile.TemporaryDirectory() as tmp:
            summary = run_with(p, make_config(tmp, workers=2))
            rows = read_items(tmp)
        self.assertEqual(summary.counts["failed"], 3)
        self.assertTrue(all(r["status"] == "failed" for r in rows))


class SnapshotRaceTests(unittest.TestCase):
    def test_shared_item_id_fetched_and_written_once(self):
        p = FakeProvider(july(6), delays={f"scene-{i}": 0.02 for i in range(6)},
                         shared_item_id={f"scene-{i}": "shared-item" for i in range(6)})
        with tempfile.TemporaryDirectory() as tmp:
            run_with(p, make_config(tmp, workers=6))
            snapshots = list((Path(tmp) / "metadata" / "fake").glob("*.json"))
        self.assertEqual(p.source_metadata_calls, ["shared-item"])
        self.assertEqual(len(snapshots), 1)


class CompositeConcurrencyTests(unittest.TestCase):
    def test_composite_scene_reads_use_workers(self):
        p = FakeProvider(july(4), delays={f"scene-{i}": 0.06 - i * 0.01 for i in range(4)})
        with tempfile.TemporaryDirectory() as tmp:
            run_with(p, make_config(tmp, temporal_mode="annual_composite", workers=4))
            rows = read_items(tmp)
        self.assertGreaterEqual(p.max_concurrent, 2)
        self.assertEqual([s["itemId"] for s in rows[0]["sources"]], [f"scene-{i}" for i in range(4)])


if __name__ == "__main__":
    unittest.main()
